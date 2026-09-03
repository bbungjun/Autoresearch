"""Research Harness의 Codex 전용 trusted release launcher.

[파이프라인] 실험 코드 작성 직전 부모가 process tree를 소유한 뒤 CLI를 시작한다.
[기능] 1-byte gate 뒤 bounded prompt를 stdin으로 전달하고 CLI 종료 상태를 반환한다.
[비책임] 설정·환경 검증, timeout과 descendant 회수, 응답 판정은 coding_agent가 담당한다.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def main() -> int:
    """부모의 release 이후에만 agent를 시작한다."""
    if len(sys.argv) < 3 or sys.stdin.buffer.read(1) != b"\x00":
        return 127
    try:
        with Path(sys.argv[1]).open("rb") as stream:
            prompt = stream.read(1024 * 1024 + 1)
        if len(prompt) > 1024 * 1024:
            return 127
        process = subprocess.Popen(sys.argv[2:], stdin=subprocess.PIPE, close_fds=True)
        process.communicate(input=prompt)
        return process.returncode
    except (OSError, subprocess.SubprocessError):
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
