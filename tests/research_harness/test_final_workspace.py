"""Final grant 뒤의 일회성 workspace와 최소 실행 context를 검증한다."""

from dataclasses import asdict, replace
import inspect
import json
from pathlib import Path

import pytest

import autoresearch.research_harness as facade
import autoresearch.research_harness.workspace as workspace_module
from autoresearch.research_harness.fixture_models import CandidateDataManifestV2
from autoresearch.research_harness.workspace import CandidateWorkspaceRequest, WorkspaceError
from tests.research_harness.test_final_candidate_data_view import FinalCase, final_case as final_case
from tests.research_harness.test_workspace import (
    _git, candidate_fixture as candidate_fixture, repository as repository,
)


def test_final_workspace_has_separate_entry_point() -> None:
    assert callable(getattr(workspace_module, "open_final_candidate_workspace", None))
    assert "grant" not in inspect.signature(workspace_module.open_candidate_workspace).parameters


def _request(repository: tuple[Path, str], final_case: FinalCase, root: Path) -> CandidateWorkspaceRequest:
    return CandidateWorkspaceRequest(repository[0], repository[1], root, final_case[0].judge)


def test_final_facade_exports_are_explicit() -> None:
    assert {"prepare_final_candidate_metadata", "materialize_final_candidate_data_view",
            "open_final_candidate_workspace"} <= set(facade.__all__)


def test_final_workspaces_pair_same_inputs_and_keep_context_minimal(repository, final_case: FinalCase, tmp_path: Path) -> None:
    fixture, source, metadata, grant = final_case
    receipts = []
    for name in ("baseline", "candidate"):
        root = tmp_path / name
        with workspace_module.open_final_candidate_workspace(
            _request(repository, final_case, root), source=source, metadata=metadata, grant=grant,
        ) as workspace:
            manifest = CandidateDataManifestV2.model_validate_json((root / "harness_in/candidate-view.json").read_bytes())
            assert workspace.evaluation_id == str(fixture.judge.final_holdout_id)
            assert manifest.evaluation_id == workspace.evaluation_id
            assert set(asdict(workspace.process)) == {"cwd", "slate", "predictions", "environment"}
            assert workspace.process.slate == root / "harness_in/slate.parquet"
            assert workspace.process.predictions == root / "harness_out/predictions.csv"
            rendered = json.dumps(asdict(workspace.process), default=str)
            assert str(fixture.fixture_root) not in rendered
            assert "final-holdout-consumed" not in rendered
            assert _git(root, "rev-parse", "HEAD") == repository[1]
            assert not workspace.inspect_changes().changed_paths
            receipts.append((workspace.candidate_view_sha256, (root / "harness_in/metadata/users.parquet").read_bytes()))
        assert not root.exists()
        assert str(root.resolve()) not in _git(repository[0], "worktree", "list", "--porcelain")
    assert receipts[0] == receipts[1]
    assert grant._authorizes(fixture.judge)


@pytest.mark.parametrize("grant", [None, object()])
def test_invalid_final_grant_does_not_create_worktree(repository, final_case: FinalCase, tmp_path: Path, grant) -> None:
    _, source, metadata, _ = final_case
    root = tmp_path / "candidate"
    with pytest.raises(WorkspaceError, match="final_grant"):
        with workspace_module.open_final_candidate_workspace(
            _request(repository, final_case, root), source=source, metadata=metadata, grant=grant,
        ):
            pytest.fail("invalid grant was exposed")
    assert not root.exists()


def test_final_workspace_cleans_up_on_bad_metadata(repository, final_case: FinalCase, tmp_path: Path) -> None:
    fixture, source, metadata, grant = final_case
    root = tmp_path / "candidate"
    wrong = replace(metadata, evaluation_id=fixture.judge.validation_id)
    with pytest.raises(WorkspaceError, match="candidate_data_view"):
        with workspace_module.open_final_candidate_workspace(
            _request(repository, final_case, root), source=source, metadata=wrong, grant=grant,
        ):
            pytest.fail("wrong metadata was exposed")
    assert not root.exists()
    assert str(root.resolve()) not in _git(repository[0], "worktree", "list", "--porcelain")


def test_final_workspace_cleans_up_after_body_failure(repository, final_case: FinalCase, tmp_path: Path) -> None:
    _, source, metadata, grant = final_case
    root = tmp_path / "candidate"
    with pytest.raises(RuntimeError, match="prediction failed"):
        with workspace_module.open_final_candidate_workspace(
            _request(repository, final_case, root), source=source, metadata=metadata, grant=grant,
        ):
            raise RuntimeError("prediction failed")
    assert not root.exists()
    assert grant.evidence.marker_path.exists()
