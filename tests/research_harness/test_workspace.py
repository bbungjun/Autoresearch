from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import subprocess

import pytest
import autoresearch.research_harness.workspace as workspace_module

from autoresearch.research_harness import (
    CandidateWorkspaceRequest,
    LocalEvaluationFixtureRequest,
    WorkspaceError,
    WorkspaceErrorCode,
    build_local_evaluation_fixture,
    open_candidate_workspace,
)
from autoresearch.research_harness.local_evaluation_fixture import (
    FixtureActionLogSource,
)


EVALUATION_DATE = date(2026, 9, 1)


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    (root / "README.md").write_text("baseline\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'baseline'\n", encoding="utf-8"
    )
    (root / ".gitignore").write_text("*.parquet\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "README.md", "pyproject.toml")
    _git(root, "commit", "-m", "baseline")
    return root, _git(root, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def candidate_fixture(tmp_path_factory: pytest.TempPathFactory):
    state_root = tmp_path_factory.mktemp("workspace-judge")
    receipt = build_local_evaluation_fixture(
        LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 1937)
    )
    return receipt, FixtureActionLogSource(
        receipt.fixture_root,
        receipt.descriptor_sha256,
    )


def _request(
    repository: tuple[Path, str],
    candidate_fixture,
    root: Path,
) -> CandidateWorkspaceRequest:
    repository_root, base_sha = repository
    receipt, _ = candidate_fixture
    return CandidateWorkspaceRequest(
        repository_root=repository_root,
        base_sha=base_sha,
        workspace_root=root,
        judge=receipt.judge,
    )


def test_workspace_is_detached_at_exact_sha_and_removed_on_exit(
    repository,
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, source = candidate_fixture
    root = tmp_path / "candidate"

    with open_candidate_workspace(_request(repository, candidate_fixture, root), source=source) as workspace:
        assert workspace.root == root
        assert workspace.base_sha == repository[1]
        assert _git(root, "rev-parse", "HEAD") == repository[1]
        assert _git(root, "branch", "--show-current") == ""
        assert root.exists()

    assert not root.exists()
    assert str(root.resolve()) not in _git(repository[0], "worktree", "list", "--porcelain")


def test_workspace_is_removed_when_candidate_body_raises(
    repository,
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, source = candidate_fixture
    root = tmp_path / "candidate"

    with pytest.raises(RuntimeError, match="candidate failed"):
        with open_candidate_workspace(
            _request(repository, candidate_fixture, root), source=source
        ):
            raise RuntimeError("candidate failed")

    assert not root.exists()


def test_workspace_exposes_only_validation_candidate_view_and_output_contract(
    repository,
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, source = candidate_fixture
    root = tmp_path / "candidate"

    with open_candidate_workspace(_request(repository, candidate_fixture, root), source=source) as workspace:
        visible = sorted(
            path.relative_to(root).as_posix()
            for path in (root / "harness_in").rglob("*")
            if path.is_file()
        )
        assert visible == [
            "harness_in/candidate-view.json",
            "harness_in/history/action_log/dt=2026-08-30/part-0.parquet",
            "harness_in/history/action_log/dt=2026-08-31/part-0.parquet",
            "harness_in/slate.parquet",
        ]
        assert workspace.process.slate == root / "harness_in/slate.parquet"
        assert workspace.process.predictions == root / "harness_out/predictions.csv"
        assert workspace.process.cwd == root
        assert workspace.process.predictions.parent.is_dir()
        assert not any("label" in path.lower() or "final" in path.lower() for path in visible)
        assert all("dt=2026-09" not in path for path in visible)


def test_candidate_environment_is_a_minimum_allowlist_without_remote_credentials(
    repository,
    candidate_fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = {
        "GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "google.json"),
        "GOOGLE_CLOUD_PROJECT": "secret-project",
        "CLOUDSDK_CONFIG": str(tmp_path / "cloudsdk"),
        "BIGQUERY_TOKEN": "secret-bq",
        "GCS_SECRET": "secret-gcs",
        "GITHUB_TOKEN": "secret-github",
        "AWS_SECRET_ACCESS_KEY": "secret-aws",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    _, source = candidate_fixture

    with open_candidate_workspace(
        _request(repository, candidate_fixture, tmp_path / "candidate"), source=source
    ) as workspace:
        environment = workspace.process.environment_dict()
        assert set(environment) <= {
            "COMSPEC",
            "PATH",
            "PATHEXT",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONUNBUFFERED",
            "SYSTEMROOT",
            "WINDIR",
        }
        assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
        assert environment["PYTHONUNBUFFERED"] == "1"
        serialized = repr(workspace.process)
        assert all(name not in environment for name in secrets)
        assert all(value not in serialized for value in secrets.values())
        assert str(candidate_fixture[0].judge.snapshot_root) not in serialized
        assert "fixture_seed" not in serialized


def test_change_inspection_allows_data_and_dependency_changes_and_is_deterministic(
    repository,
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, source = candidate_fixture

    with open_candidate_workspace(
        _request(repository, candidate_fixture, tmp_path / "candidate"), source=source
    ) as workspace:
        (workspace.root / "pyproject.toml").write_text(
            "[project]\nname = 'candidate'\n", encoding="utf-8"
        )
        (workspace.root / "derived.parquet").write_bytes(b"PAR1 candidate data")
        first = workspace.inspect_changes()
        second = workspace.inspect_changes()

        assert _git(workspace.root, "check-ignore", "derived.parquet") == "derived.parquet"
        assert first == second
        assert first.diff_fingerprint.startswith("sha256:")
        assert first.changed_paths == ("derived.parquet", "pyproject.toml")


def test_change_fingerprint_changes_for_content_path_and_deletion(
    repository,
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, source = candidate_fixture

    with open_candidate_workspace(
        _request(repository, candidate_fixture, tmp_path / "candidate"), source=source
    ) as workspace:
        (workspace.root / "candidate.txt").write_text("one", encoding="utf-8")
        content_one = workspace.inspect_changes().diff_fingerprint
        (workspace.root / "candidate.txt").write_text("two", encoding="utf-8")
        content_two = workspace.inspect_changes().diff_fingerprint
        (workspace.root / "renamed.txt").write_text("two", encoding="utf-8")
        (workspace.root / "candidate.txt").unlink()
        renamed = workspace.inspect_changes().diff_fingerprint
        (workspace.root / "README.md").unlink()
        deleted = workspace.inspect_changes().diff_fingerprint

        assert len({content_one, content_two, renamed, deleted}) == 4


def test_change_inspection_rejects_credential_value_without_exposing_it(
    repository,
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, source = candidate_fixture
    secret = "ghp_" + "a" * 36

    with open_candidate_workspace(
        _request(repository, candidate_fixture, tmp_path / "candidate"), source=source
    ) as workspace:
        (workspace.root / "notes.txt").write_text(secret, encoding="utf-8")
        with pytest.raises(WorkspaceError) as captured:
            workspace.inspect_changes()

    assert captured.value.code is WorkspaceErrorCode.CREDENTIAL_DETECTED
    assert secret not in str(captured.value)


def test_committed_candidate_change_is_still_compared_with_sealed_base_sha(
    repository,
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, source = candidate_fixture
    secret = "ghp_" + "c" * 36

    with open_candidate_workspace(
        _request(repository, candidate_fixture, tmp_path / "candidate"), source=source
    ) as workspace:
        (workspace.root / "committed.txt").write_text(secret, encoding="utf-8")
        _git(workspace.root, "add", "committed.txt")
        _git(workspace.root, "commit", "-m", "candidate commit")
        with pytest.raises(WorkspaceError) as captured:
            workspace.inspect_changes()

    assert captured.value.code is WorkspaceErrorCode.CREDENTIAL_DETECTED
    assert secret not in str(captured.value)


def test_aws_secret_assignment_is_rejected(
    repository,
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, source = candidate_fixture

    with open_candidate_workspace(
        _request(repository, candidate_fixture, tmp_path / "candidate"), source=source
    ) as workspace:
        (workspace.root / "aws.env").write_text(
            "AWS_SECRET_ACCESS_KEY=" + "a" * 40,
            encoding="utf-8",
        )
        with pytest.raises(WorkspaceError) as captured:
            workspace.inspect_changes()

    assert captured.value.code is WorkspaceErrorCode.CREDENTIAL_DETECTED


def test_deleting_a_preexisting_credential_is_not_rejected(
    repository,
    candidate_fixture,
    tmp_path: Path,
) -> None:
    repository_root, _ = repository
    secret_path = repository_root / "remove-me.txt"
    secret_path.write_text("ghp_" + "b" * 36, encoding="utf-8")
    _git(repository_root, "add", "remove-me.txt")
    _git(repository_root, "commit", "-m", "add old fixture")
    base_sha = _git(repository_root, "rev-parse", "HEAD")
    receipt, source = candidate_fixture
    request = CandidateWorkspaceRequest(
        repository_root=repository_root,
        base_sha=base_sha,
        workspace_root=tmp_path / "candidate",
        judge=receipt.judge,
    )

    with open_candidate_workspace(request, source=source) as workspace:
        (workspace.root / "remove-me.txt").unlink()
        changes = workspace.inspect_changes()

    assert changes.changed_paths == ("remove-me.txt",)


@pytest.mark.parametrize("invalid_sha", ("main", "a" * 39, "A" * 40, "g" * 40))
def test_request_rejects_noncanonical_base_sha(
    repository,
    candidate_fixture,
    tmp_path: Path,
    invalid_sha: str,
) -> None:
    receipt, source = candidate_fixture
    request = CandidateWorkspaceRequest(
        repository_root=repository[0],
        base_sha=invalid_sha,
        workspace_root=tmp_path / "candidate",
        judge=receipt.judge,
    )

    with pytest.raises(WorkspaceError) as captured:
        with open_candidate_workspace(request, source=source):
            pass

    assert captured.value.code is WorkspaceErrorCode.INVALID_REQUEST


def test_existing_workspace_path_is_never_reused_or_removed(
    repository,
    candidate_fixture,
    tmp_path: Path,
) -> None:
    root = tmp_path / "candidate"
    root.mkdir()
    marker = root / "owned-by-user"
    marker.write_text("keep", encoding="utf-8")
    _, source = candidate_fixture

    with pytest.raises(WorkspaceError) as captured:
        with open_candidate_workspace(_request(repository, candidate_fixture, root), source=source):
            pass

    assert captured.value.code is WorkspaceErrorCode.WORKSPACE_CONFLICT
    assert marker.read_text(encoding="utf-8") == "keep"


def test_unknown_well_formed_base_sha_is_rejected_before_workspace_creation(
    repository,
    candidate_fixture,
    tmp_path: Path,
) -> None:
    receipt, source = candidate_fixture
    root = tmp_path / "candidate"
    request = CandidateWorkspaceRequest(
        repository_root=repository[0],
        base_sha="0" * 40,
        workspace_root=root,
        judge=receipt.judge,
    )

    with pytest.raises(WorkspaceError) as captured:
        with open_candidate_workspace(request, source=source):
            pass

    assert captured.value.code is WorkspaceErrorCode.INVALID_REQUEST
    assert not root.exists()


def test_workspace_cannot_be_created_inside_source_repository(
    repository,
    candidate_fixture,
) -> None:
    receipt, source = candidate_fixture
    root = repository[0] / "candidate"
    request = CandidateWorkspaceRequest(
        repository_root=repository[0],
        base_sha=repository[1],
        workspace_root=root,
        judge=receipt.judge,
    )

    with pytest.raises(WorkspaceError) as captured:
        with open_candidate_workspace(request, source=source):
            pass

    assert captured.value.code is WorkspaceErrorCode.WORKSPACE_CONFLICT
    assert not root.exists()


def test_cleanup_fails_closed_when_git_metadata_cannot_be_released(
    repository,
    candidate_fixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, source = candidate_fixture

    with pytest.raises(WorkspaceError) as captured:
        with open_candidate_workspace(
            _request(repository, candidate_fixture, tmp_path / "candidate"), source=source
        ):
            monkeypatch.setattr(
                workspace_module,
                "_run_git_result",
                lambda *_args: subprocess.CompletedProcess(
                    args=[], returncode=1, stdout=b"", stderr=b"failed"
                ),
            )

    assert captured.value.code is WorkspaceErrorCode.CLEANUP_FAILED


def test_cleanup_preserves_a_replacement_directory_it_does_not_own(
    repository,
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, source = candidate_fixture
    root = tmp_path / "candidate"
    moved = tmp_path / "moved-worktree"
    marker = root / "owned-by-other.txt"

    with pytest.raises(WorkspaceError) as captured:
        with open_candidate_workspace(
            _request(repository, candidate_fixture, root), source=source
        ):
            root.rename(moved)
            root.mkdir()
            marker.write_text("keep", encoding="utf-8")

    assert captured.value.code is WorkspaceErrorCode.CLEANUP_FAILED
    assert marker.read_text(encoding="utf-8") == "keep"


def test_fingerprint_is_independent_of_git_rename_configuration(
    repository,
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, source = candidate_fixture

    with open_candidate_workspace(
        _request(repository, candidate_fixture, tmp_path / "candidate"), source=source
    ) as workspace:
        _git(workspace.root, "mv", "README.md", "renamed.md")
        _git(workspace.root, "config", "diff.renames", "true")
        enabled = workspace.inspect_changes()
        _git(workspace.root, "config", "diff.renames", "false")
        disabled = workspace.inspect_changes()

    assert enabled == disabled
    assert enabled.changed_paths == ("README.md", "renamed.md")


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable mode contract")
def test_untracked_executable_mode_changes_fingerprint(
    repository,
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, source = candidate_fixture

    with open_candidate_workspace(
        _request(repository, candidate_fixture, tmp_path / "candidate"), source=source
    ) as workspace:
        script = workspace.root / "candidate-script"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o644)
        regular = workspace.inspect_changes().diff_fingerprint
        script.chmod(0o755)
        executable = workspace.inspect_changes().diff_fingerprint

    assert regular != executable


def test_public_workspace_contract_has_no_final_or_judge_path(
    repository,
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, source = candidate_fixture

    with open_candidate_workspace(
        _request(repository, candidate_fixture, tmp_path / "candidate"), source=source
    ) as workspace:
        assert not hasattr(workspace, "judge")
        assert not hasattr(workspace, "final_holdout")
        assert not hasattr(workspace.process, "judge")
        assert not hasattr(workspace.process, "final_holdout")
        assert os.path.commonpath(
            [workspace.root, candidate_fixture[0].judge.snapshot_root]
        ) != str(workspace.root)
