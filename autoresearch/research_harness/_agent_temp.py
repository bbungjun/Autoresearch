"""Coding prepare의 등록된 임시 산출물을 생성 주체에서 회수한다.

[파이프라인] Agent 프로세스 종료와 candidate 증거 보존 이후, workspace 회수 직전이다.
[기능] 고정 temp anchor identity 등록과 disposable runtime root 생성, bounded 전체
사전 검증, 자식만 삭제하는 stdlib-only trusted worker를 제공한다. 실행 시 cwd가
등록 workspace와 일치해야 한다.
[비책임] Sandbox 실행·timeout은 coding_agent, 전체 workspace 회수는 workspace 소유다.
ACL·소유권 변경이나 동시 악성 파일 교체에 대한 별도 격리는 제공하지 않는다.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import stat
import sys


_ANCHOR = "harness_out/.agent-tmp"
_RUNTIME = f"{_ANCHOR}/runtime"
_LIMIT = 10000
_BOUNDARIES = (".", ".git", "harness_out", _ANCHOR)


def _identity(path: Path, *, directory: bool) -> list[int]:
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or getattr(info, "st_file_attributes", 0) & 0x400
            or (not directory and info.st_nlink != 1)):
        raise ValueError("unsafe_object")
    return [info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)]


def _ancestors(path: Path) -> None:
    for parent in (path, *path.parents):
        _identity(parent, directory=True)


def _git_digest(cwd: Path) -> str:
    path = cwd / ".git"
    before = _identity(path, directory=False)
    with path.open("rb") as stream:
        payload = stream.read(16385)
    if len(payload) > 16384 or _identity(path, directory=False) != before:
        raise ValueError("git_boundary_changed")
    return hashlib.sha256(payload).hexdigest()


def register(cwd: Path) -> dict[str, object]:
    """검증된 workspace에 새 anchor를 만들고 삭제 불가 경계만 등록한다."""
    _ancestors(cwd)
    _identity(cwd / ".git", directory=False)
    _identity(cwd / "harness_out", directory=True)
    (cwd / _ANCHOR).mkdir()
    (cwd / _RUNTIME).mkdir()
    return {"version": "agent-temp-v1", "git_sha256": _git_digest(cwd), "identities": {
        name: _identity(cwd / name, directory=name != ".git") for name in _BOUNDARIES
    }}


def validate(cwd: Path, registration: dict[str, object], *, empty: bool = False) -> None:
    """등록 경계의 교체·alias와 선택적으로 회수 뒤 잔여 항목을 거부한다."""
    _ancestors(cwd)
    if set(registration) != {"version", "identities", "git_sha256"} or registration["version"] != "agent-temp-v1":
        raise ValueError("invalid_registration")
    identities = registration["identities"]
    if not isinstance(identities, dict) or set(identities) != set(_BOUNDARIES):
        raise ValueError("invalid_registration")
    for name in _BOUNDARIES:
        if _identity(cwd / name, directory=name != ".git") != identities[name]:
            raise ValueError("boundary_changed")
    if _git_digest(cwd) != registration["git_sha256"]:
        raise ValueError("git_boundary_changed")
    if empty and any((cwd / _ANCHOR).iterdir()):
        raise ValueError("temp_not_empty")


def clean(cwd: Path, registration: dict[str, object], receipt: dict[str, object]) -> None:
    """전체 preflight 성공 뒤 고정 anchor의 자식만 postorder로 회수한다."""
    validate(cwd, registration)
    pending = [cwd / _ANCHOR]
    targets: list[tuple[Path, bool, list[int]]] = []
    while pending:
        parent = pending.pop()
        for path in parent.iterdir():
            directory = stat.S_ISDIR(path.lstat().st_mode)
            identity = _identity(path, directory=directory)
            targets.append((path, directory, identity))
            if len(targets) > _LIMIT:
                raise ValueError("temp_object_limit")
            if directory:
                pending.append(path)
    receipt["object_count"] = len(targets)
    for path, directory, identity in reversed(targets):
        validate(cwd, registration)
        _ancestors(path.parent)
        if _identity(path, directory=directory) != identity:
            raise ValueError("temp_changed")
        if directory:
            path.rmdir()
        else:
            path.unlink()
        receipt["removed_count"] = int(receipt["removed_count"]) + 1
    validate(cwd, registration, empty=True)
    receipt["status"] = "complete"


def main() -> int:
    """삭제 경로 인자를 받지 않고 stdin의 고정 경계 identity만 소비한다."""
    receipt: dict[str, object] = {"status": "failed", "object_count": None, "removed_count": 0}
    try:
        if len(sys.argv) != 1:
            raise ValueError("unexpected_arguments")
        payload = sys.stdin.buffer.read(16385)
        if len(payload) > 16384:
            raise ValueError("registration_limit")
        registration = json.loads(payload)
        if not isinstance(registration, dict):
            raise ValueError("invalid_registration")
        clean(Path.cwd(), registration, receipt)
    except (OSError, ValueError, TypeError) as error:
        receipt["error_type"] = type(error).__name__
    except (KeyboardInterrupt, SystemExit):
        receipt["status"] = "interrupted"
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
