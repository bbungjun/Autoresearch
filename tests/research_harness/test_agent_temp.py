"""실제 권한 조작 없이 고정 coding temp 회수와 경계 보존을 검증한다."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from autoresearch.research_harness import _agent_temp as temp


def _workspace(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    (cwd / ".git").write_text("gitdir: sentinel")
    (cwd / "harness_out").mkdir()
    return cwd, temp.register(cwd)


def _receipt() -> dict[str, object]:
    return {"status": "failed", "removed_count": 0}


def test_clean_only_anchor_children_preserves_boundaries_and_sentinels(tmp_path: Path) -> None:
    cwd, registration = _workspace(tmp_path)
    sentinel = cwd / "harness_out/keep.txt"
    sentinel.write_text("untouched")
    tree = cwd / "harness_out/.agent-tmp/pytest/users"
    tree.mkdir(parents=True)
    (tree / "cache").write_text("scratch")
    receipt = _receipt()
    temp.clean(cwd, registration, receipt)
    assert receipt == {"status": "complete", "removed_count": 3, "object_count": 3}
    temp.validate(cwd, registration, empty=True)
    assert sentinel.read_text() == "untouched"
    assert (cwd / ".git").read_text() == "gitdir: sentinel"


@pytest.mark.parametrize("name", [".git", "harness_out", "harness_out/.agent-tmp"])
def test_replaced_boundary_fails_before_deletion(tmp_path: Path, name: str) -> None:
    cwd, registration = _workspace(tmp_path)
    path = cwd / name
    path.rename(path.with_name(path.name + "-old"))
    if name == ".git":
        path.write_text("gitdir: sentinel")
    else:
        path.mkdir()
    with pytest.raises((ValueError, FileNotFoundError)):
        temp.clean(cwd, registration, _receipt())


def test_same_inode_git_content_change_is_rejected(tmp_path: Path) -> None:
    cwd, registration = _workspace(tmp_path)
    (cwd / ".git").write_text("gitdir: changed")
    with pytest.raises(ValueError, match="git_boundary_changed"):
        temp.clean(cwd, registration, _receipt())


def test_oversized_git_pointer_is_rejected_before_deleting(tmp_path: Path) -> None:
    cwd, registration = _workspace(tmp_path)
    (cwd / ".git").write_bytes(b"x" * 16385)
    scratch = cwd / "harness_out/.agent-tmp/keep"
    scratch.touch()
    with pytest.raises(ValueError, match="git_boundary_changed"):
        temp.clean(cwd, registration, _receipt())
    assert scratch.exists()


def test_reparse_directory_is_rejected_before_descent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd, registration = _workspace(tmp_path)
    junction = cwd / "harness_out/.agent-tmp/junction"
    junction.mkdir()
    (junction / "sentinel").touch()
    original = temp._identity
    visited = []

    def identity(path: Path, *, directory: bool) -> list[int]:
        visited.append(path)
        if path == junction:
            raise ValueError("unsafe_object")
        return original(path, directory=directory)

    monkeypatch.setattr(temp, "_identity", identity)
    with pytest.raises(ValueError, match="unsafe_object"):
        temp.clean(cwd, registration, _receipt())
    assert junction / "sentinel" not in visited


def test_hardlink_preflight_deletes_nothing(tmp_path: Path) -> None:
    cwd, registration = _workspace(tmp_path)
    anchor = cwd / "harness_out/.agent-tmp"
    sentinel = tmp_path / "outside"
    sentinel.write_text("safe")
    (anchor / "ordinary").write_text("preserved")
    os.link(sentinel, anchor / "linked")
    receipt = _receipt()
    with pytest.raises(ValueError, match="unsafe_object"):
        temp.clean(cwd, registration, receipt)
    assert receipt["removed_count"] == 0
    assert (anchor / "ordinary").exists() and sentinel.read_text() == "safe"


def test_object_limit_preflight_deletes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd, registration = _workspace(tmp_path)
    anchor = cwd / "harness_out/.agent-tmp"
    (anchor / "a").touch()
    (anchor / "b").touch()
    monkeypatch.setattr(temp, "_LIMIT", 1)
    with pytest.raises(ValueError, match="temp_object_limit"):
        temp.clean(cwd, registration, _receipt())
    assert len(list(anchor.iterdir())) == 2


def test_partial_failure_counts_successful_deletions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd, registration = _workspace(tmp_path)
    anchor = cwd / "harness_out/.agent-tmp"
    (anchor / "a").touch()
    (anchor / "b").touch()
    original = Path.unlink
    calls = 0

    def fail_second(path: Path, *args, **kwargs) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("denied")
        original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_second)
    receipt = _receipt()
    with pytest.raises(PermissionError):
        temp.clean(cwd, registration, receipt)
    assert receipt["removed_count"] == 1 and len(list(anchor.iterdir())) == 1


def test_isolated_worker_consumes_stdin_without_importing_candidate_code(tmp_path: Path) -> None:
    cwd, registration = _workspace(tmp_path)
    (cwd / "json.py").write_text("raise RuntimeError('candidate import')")
    (cwd / "harness_out/.agent-tmp/scratch").write_text("temporary")
    result = subprocess.run((sys.executable, "-I", "-S", str(Path(temp.__file__).resolve())),
                            cwd=cwd, input=json.dumps(registration), text=True, capture_output=True, check=False)
    assert result.returncode == 0
    assert json.loads(result.stdout)["removed_count"] == 1
    assert (cwd / "json.py").exists()


def test_worker_rejects_arbitrary_path_argument(tmp_path: Path) -> None:
    cwd, registration = _workspace(tmp_path)
    result = subprocess.run((sys.executable, "-I", "-S", str(Path(temp.__file__).resolve()), str(tmp_path)),
                            cwd=cwd, input=json.dumps(registration), text=True, capture_output=True, check=False)
    assert result.returncode == 1 and json.loads(result.stdout)["removed_count"] == 0
