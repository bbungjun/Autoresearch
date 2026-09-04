"""실제 권한 조작 없이 coding temp 회수와 경계 보존을 검증한다.

[파이프라인] Coding 종료 뒤 workspace 회수 전의 고정 temp anchor 구간이다.
[기능] 회수 preflight·실패·sentinel 보존을 검사하며, 테스트가 만든 하드링크는
assertion 실패 시에도 해제하여 바깥 coding temp에 잔류하지 않게 한다.
[비책임] 회수 구현은 _agent_temp, sandbox 실행은 coding_agent 소유다.
"""

import json
import os
from pathlib import Path
import shutil
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
    assert receipt == {"status": "complete", "removed_count": 4, "object_count": 4}
    temp.validate(cwd, registration, empty=True)
    assert sentinel.read_text() == "untouched"
    assert (cwd / ".git").read_text() == "gitdir: sentinel"


def test_missing_runtime_root_is_already_clean_when_anchor_matches(tmp_path: Path) -> None:
    cwd, registration = _workspace(tmp_path)
    (cwd / "harness_out/.agent-tmp/runtime").rmdir()
    receipt = _receipt()

    temp.clean(cwd, registration, receipt)

    assert receipt == {"status": "complete", "removed_count": 0, "object_count": 0}


def test_renamed_nonempty_anchor_fails_without_deleting_leftover(tmp_path: Path) -> None:
    cwd, registration = _workspace(tmp_path)
    anchor = cwd / "harness_out/.agent-tmp"
    (anchor / "runtime/leftover.bin").write_text("preserved")
    renamed = cwd / "harness_out/renamed-agent-tmp"
    anchor.rename(renamed)

    with pytest.raises(FileNotFoundError):
        temp.clean(cwd, registration, _receipt())

    assert (renamed / "runtime/leftover.bin").read_text() == "preserved"


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
    alias = anchor / "linked"
    os.link(sentinel, alias)
    try:
        receipt = _receipt()
        with pytest.raises(ValueError, match="unsafe_object"):
            temp.clean(cwd, registration, receipt)
        assert receipt["removed_count"] == 0
        assert (anchor / "ordinary").exists() and sentinel.read_text() == "safe"
    finally:
        alias.unlink()


def test_object_limit_preflight_deletes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd, registration = _workspace(tmp_path)
    anchor = cwd / "harness_out/.agent-tmp"
    (anchor / "a").touch()
    (anchor / "b").touch()
    monkeypatch.setattr(temp, "_LIMIT", 1)
    with pytest.raises(ValueError, match="temp_object_limit"):
        temp.clean(cwd, registration, _receipt())
    assert len(list(anchor.iterdir())) == 3


def test_object_limit_stops_streaming_directory_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd, registration = _workspace(tmp_path)
    runtime = cwd / "harness_out/.agent-tmp/runtime"
    for name in ("one", "two", "three"):
        (runtime / name).write_text(name)
    original_scandir = os.scandir
    consumed = 0

    class GuardedScan:
        def __init__(self, path: Path) -> None:
            self._entries = original_scandir(path)

        def __enter__(self) -> "GuardedScan":
            return self

        def __exit__(self, *args: object) -> None:
            self._entries.close()

        def __iter__(self) -> "GuardedScan":
            return self

        def __next__(self) -> os.DirEntry[str]:
            nonlocal consumed
            entry = next(self._entries)
            consumed += 1
            if consumed > 2:
                raise AssertionError("object limit 뒤의 entry를 읽음")
            return entry

    monkeypatch.setattr(temp.os, "scandir", GuardedScan)
    monkeypatch.setattr(temp, "_LIMIT", 1)

    with pytest.raises(ValueError, match="temp_object_limit"):
        temp.clean(cwd, registration, _receipt())

    assert consumed == 2
    assert sorted(path.name for path in runtime.iterdir()) == ["one", "three", "two"]


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
    assert receipt["removed_count"] == 2 and len(list(anchor.iterdir())) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path regression")
def test_clean_removes_registered_file_beyond_legacy_max_path(tmp_path: Path) -> None:
    cwd, registration = _workspace(tmp_path)
    logical_parent = cwd / "harness_out/.agent-tmp/runtime"
    for index in range(4):
        logical_parent /= f"segment-{index}-" + "x" * 64
    logical_file = logical_parent / "part-0.parquet"
    extended_runtime = Path("\\\\?\\" + str(cwd / "harness_out/.agent-tmp/runtime"))
    extended_file = Path("\\\\?\\" + str(logical_file))
    extended_file.parent.mkdir(parents=True)
    extended_file.write_bytes(b"fixture")
    assert len(str(logical_file)) > 260
    receipt = _receipt()

    try:
        temp.clean(cwd, registration, receipt)
    finally:
        if extended_runtime.exists():
            shutil.rmtree(extended_runtime)

    assert receipt["status"] == "complete"
    assert receipt["removed_count"] == receipt["object_count"]
    temp.validate(cwd, registration, empty=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path regression")
def test_io_path_uses_extended_unc_prefix() -> None:
    path = Path(r"\\server\share\workspace")

    assert str(temp._io_path(path)) == r"\\?\UNC\server\share\workspace"


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path regression")
def test_isolated_worker_removes_file_beyond_legacy_max_path(tmp_path: Path) -> None:
    cwd, registration = _workspace(tmp_path)
    logical_parent = cwd / "harness_out/.agent-tmp/runtime"
    for index in range(4):
        logical_parent /= f"segment-{index}-" + "x" * 64
    logical_file = logical_parent / "part-0.parquet"
    extended_file = Path("\\\\?\\" + str(logical_file))
    extended_file.parent.mkdir(parents=True)
    extended_file.write_bytes(b"fixture")

    result = subprocess.run(
        (sys.executable, "-I", "-S", str(Path(temp.__file__).resolve())),
        cwd=cwd,
        input=json.dumps(registration),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "complete"
    assert receipt["removed_count"] == receipt["object_count"]
    temp.validate(cwd, registration, empty=True)

def test_isolated_worker_consumes_stdin_without_importing_candidate_code(tmp_path: Path) -> None:
    cwd, registration = _workspace(tmp_path)
    (cwd / "json.py").write_text("raise RuntimeError('candidate import')")
    (cwd / "harness_out/.agent-tmp/scratch").write_text("temporary")
    result = subprocess.run((sys.executable, "-I", "-S", str(Path(temp.__file__).resolve())),
                            cwd=cwd, input=json.dumps(registration), text=True, capture_output=True, check=False)
    assert result.returncode == 0
    assert json.loads(result.stdout)["removed_count"] == 2
    assert (cwd / "json.py").exists()


def test_worker_rejects_arbitrary_path_argument(tmp_path: Path) -> None:
    cwd, registration = _workspace(tmp_path)
    result = subprocess.run((sys.executable, "-I", "-S", str(Path(temp.__file__).resolve()), str(tmp_path)),
                            cwd=cwd, input=json.dumps(registration), text=True, capture_output=True, check=False)
    assert result.returncode == 1 and json.loads(result.stdout)["removed_count"] == 0
