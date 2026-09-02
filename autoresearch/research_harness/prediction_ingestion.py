"""불신 candidate prediction을 Judge 소유 사본으로 봉인한다.

[파이프라인] candidate가 ``predictions.csv``를 산출한 뒤 Sealed Judge가 target과 결합하기
전, 파일 identity·크기·parser 자원 계약을 고정하는 구간을 담당한다.

[기능] source를 no-follow 방식으로 한 번만 열고 같은 file descriptor에서 exclusive Judge
사본을 만든 뒤, 격리 subprocess의 공통 parser를 통과한 receipt를 반환한다.

[비책임] candidate 프로세스 실행·회수는 Task 5a LocalRunner가, target·metric 계산은
``judge``가, coverage·sigma 판정은 ``judge_decision``이 담당한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import os
import stat
import subprocess
import sys
from typing import NamedTuple

from autoresearch.research_harness.judge import (
    JudgeError,
    JudgeErrorCode,
)


MAX_PREDICTION_BYTES = 65 * 1024 * 1024
PARSER_TIMEOUT_SECONDS = 10.0
PARSER_MEMORY_BYTES = 256 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024


class _SourceSignature(NamedTuple):
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class SealedPredictionReceipt:
    """검증된 Judge 소유 prediction 사본의 identity 증거."""

    path: Path
    size_bytes: int
    sha256: str


def seal_prediction_copy(
    candidate_prediction: Path,
    judge_copy: Path,
) -> SealedPredictionReceipt:
    """candidate prediction을 exclusive Judge 사본으로 복사·검증한다."""

    source_fd: int | None = None
    destination_fd: int | None = None
    destination_created = False
    try:
        source_fd = _open_source(candidate_prediction)
        before = _source_signature(source_fd)
        if not stat.S_ISREG(before.mode) or before.size > MAX_PREDICTION_BYTES:
            raise _invalid("source_contract")

        destination_fd = os.open(
            judge_copy,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _binary_flag(),
            0o600,
        )
        destination_created = True
        digest = sha256()
        copied = 0
        while True:
            chunk = os.read(
                source_fd,
                min(_COPY_CHUNK_BYTES, MAX_PREDICTION_BYTES + 1 - copied),
            )
            if not chunk:
                break
            copied += len(chunk)
            if copied > MAX_PREDICTION_BYTES:
                raise _invalid("size_limit")
            _write_all(destination_fd, chunk)
            digest.update(chunk)

        after = _source_signature(source_fd)
        if before != after or copied != before.size:
            raise _invalid("source_changed")
        os.fsync(destination_fd)
        os.close(destination_fd)
        destination_fd = None
        _run_isolated_parser(judge_copy)
        return SealedPredictionReceipt(
            path=judge_copy,
            size_bytes=copied,
            sha256=digest.hexdigest(),
        )
    except JudgeError:
        if destination_fd is not None:
            os.close(destination_fd)
            destination_fd = None
        if destination_created:
            _remove_incomplete_copy(judge_copy)
        raise
    except (OSError, TypeError, ValueError):
        if destination_fd is not None:
            os.close(destination_fd)
            destination_fd = None
        if destination_created:
            _remove_incomplete_copy(judge_copy)
        raise _invalid("ingestion_io") from None
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        if source_fd is not None:
            os.close(source_fd)


def _open_source(candidate_prediction: Path) -> int:
    source_stat = os.lstat(candidate_prediction)
    if stat.S_ISLNK(source_stat.st_mode):
        raise _invalid("source_symlink")
    flags = os.O_RDONLY | _binary_flag()
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(candidate_prediction, flags | no_follow)
    try:
        opened = _source_signature(fd)
        lstat_signature = _signature_from_stat(source_stat)
        if opened != lstat_signature:
            raise _invalid("source_identity")
    except (JudgeError, OSError):
        os.close(fd)
        raise
    return fd


def _source_signature(fd: int) -> _SourceSignature:
    return _signature_from_stat(os.fstat(fd))


def _signature_from_stat(value: os.stat_result) -> _SourceSignature:
    return _SourceSignature(
        device=value.st_dev,
        inode=value.st_ino,
        mode=value.st_mode,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
    )


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _run_isolated_parser(judge_copy: Path) -> None:
    try:
        completed = subprocess.run(  # noqa: S603 - argv는 고정 모듈과 정수 상한이다.
            [
                sys.executable,
                str(Path(__file__).with_name("prediction_parser_worker.py")),
                str(PARSER_MEMORY_BYTES),
                str(judge_copy),
            ],
            check=False,
            capture_output=True,
            timeout=PARSER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise _invalid("parser_timeout") from None
    if completed.returncode != 0:
        raise _invalid("parser_subprocess")


def _remove_incomplete_copy(judge_copy: Path) -> None:
    try:
        judge_copy.unlink(missing_ok=True)
    except OSError:
        pass


def _binary_flag() -> int:
    return getattr(os, "O_BINARY", 0)


def _invalid(stage: str) -> JudgeError:
    return JudgeError(JudgeErrorCode.INVALID_PREDICTIONS, stage)
