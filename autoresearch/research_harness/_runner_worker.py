"""LocalRunner의 Windows/POSIX 공통 trusted launcher.

[파이프라인] Harness가 candidate process-tree 소유 경계를 만든 뒤 고정 예측 명령을 시작하는
아주 작은 중간 구간을 담당한다.

[기능] stdin의 1-byte release gate를 기다린 다음 전달받은 고정 argv를 stdin 없이 실행하고
candidate exit code를 그대로 전달한다.

[비책임] 요청·환경·경로 검증, Job Object/process group 생성, timeout·tree 회수와 결과 판정은
부모 ``runner`` 모듈이 담당한다.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


_RELEASE_BYTE = b"\x00"
_LAUNCH_FAILURE_EXIT_CODE = 127
_ALLOWED_ENVIRONMENT = frozenset(
    {
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
    }
)


def _publish_status(path: Path, status: bytes) -> bool:
    """부모 소유 임시 경로에 candidate start 결과를 한 번 기록한다."""

    descriptor = -1
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(descriptor, status)
        os.fsync(descriptor)
    except OSError:
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return True


def main() -> int:
    """Release 뒤 candidate를 시작하고 그 exit code를 반환한다."""

    if len(sys.argv) < 3:
        return _LAUNCH_FAILURE_EXIT_CODE
    status_path = Path(sys.argv[1])
    if sys.stdin.buffer.read(1) != _RELEASE_BYTE:
        _publish_status(status_path, b"F")
        return _LAUNCH_FAILURE_EXIT_CODE
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in _ALLOWED_ENVIRONMENT
    }
    try:
        process = subprocess.Popen(
            sys.argv[2:],
            stdin=subprocess.DEVNULL,
            env=environment,
            close_fds=True,
        )
    except OSError:
        _publish_status(status_path, b"F")
        return _LAUNCH_FAILURE_EXIT_CODE
    if not _publish_status(status_path, b"S"):
        process.kill()
        process.wait()
        return _LAUNCH_FAILURE_EXIT_CODE
    try:
        return process.wait()
    except BaseException:
        return _LAUNCH_FAILURE_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
