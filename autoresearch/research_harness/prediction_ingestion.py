"""불신 candidate prediction을 Judge 소유 사본으로 봉인한다.

[파이프라인] candidate가 ``predictions.csv``를 산출한 뒤 Sealed Judge가 target과 결합하기
전, 파일 identity·크기·parser 자원 계약을 고정하는 구간을 담당한다.

[기능] source를 no-follow·non-blocking 방식으로 한 번만 열어 exclusive Judge 사본을 만들고,
격리 parser가 만든 정규화 행만 scoring에 전달하는 opaque receipt를 반환한다.
허용 ASCII의 JSON escaping 확장을 104 MiB parsed 상한으로 수용하며 CSV·시간·메모리
상한은 각각 65 MiB·10초·256 MiB로 유지한다.

[비책임] candidate 프로세스 실행·회수는 Task 5a LocalRunner가, target·metric 계산은
``judge``가, coverage·sigma 판정은 ``judge_decision``이 담당한다.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256 as sha256_digest
import json
from pathlib import Path
import os
import stat
import subprocess
import sys
from typing import NamedTuple

from autoresearch.research_harness.judge_errors import JudgeError, JudgeErrorCode
from autoresearch.research_harness.prediction_parser import (
    MAX_PREDICTION_ROWS,
    PredictionRow,
)


MAX_PREDICTION_BYTES = 65 * 1024 * 1024
PARSER_TIMEOUT_SECONDS = 10.0
PARSER_MEMORY_BYTES = 256 * 1024 * 1024
# 69 + 2*128 escaped identifier bytes + 24 float bytes + 11 JSON syntax + LF
# = at most 361 bytes/row; 300k rows fit below 104 MiB.
_MAX_PARSED_BYTES = 104 * 1024 * 1024
_MAX_PARSED_ROW_BYTES = 512
_COPY_CHUNK_BYTES = 1024 * 1024


class _SourceSignature(NamedTuple):
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True, init=False, repr=False)
class SealedPredictionReceipt:
    """ingestion만 만들 수 있는 Judge 소유 prediction과 parsed rows 증거."""

    _path: Path
    _size_bytes: int
    _sha256: str
    _copy_signature: _SourceSignature
    _parsed_path: Path
    _parsed_signature: _SourceSignature
    _parsed_sha256: str

    def __init__(self, *_: object, **__: object) -> None:
        raise JudgeError(JudgeErrorCode.INVALID_PREDICTIONS, "sealed_receipt")

    @classmethod
    def _from_verified(
        cls,
        *,
        path: Path,
        size_bytes: int,
        sha256: str,
        copy_signature: _SourceSignature,
        parsed_path: Path,
        parsed_signature: _SourceSignature,
        parsed_sha256: str,
    ) -> SealedPredictionReceipt:
        receipt = object.__new__(cls)
        object.__setattr__(receipt, "_path", path)
        object.__setattr__(receipt, "_size_bytes", size_bytes)
        object.__setattr__(receipt, "_sha256", sha256)
        object.__setattr__(receipt, "_copy_signature", copy_signature)
        object.__setattr__(receipt, "_parsed_path", parsed_path)
        object.__setattr__(receipt, "_parsed_signature", parsed_signature)
        object.__setattr__(receipt, "_parsed_sha256", parsed_sha256)
        return receipt

    @property
    def path(self) -> Path:
        """Ledger evidence용 Judge CSV 사본 경로."""

        return self._path

    @property
    def size_bytes(self) -> int:
        """봉인된 CSV byte 크기."""

        return self._size_bytes

    @property
    def sha256(self) -> str:
        """봉인된 CSV SHA-256."""

        return self._sha256

    def __repr__(self) -> str:
        return "SealedPredictionReceipt(<opaque>)"


def seal_prediction_copy(
    candidate_prediction: Path,
    judge_copy: Path,
) -> SealedPredictionReceipt:
    """candidate prediction을 exclusive Judge 사본과 parsed rows로 봉인한다."""

    parsed_copy = judge_copy.with_name(f"{judge_copy.name}.parsed.jsonl")
    source_fd: int | None = None
    destination_fd: int | None = None
    destination_created = False
    parsed_created = False
    parsed_owner: _SourceSignature | None = None
    completed = False
    try:
        source_fd = _open_regular_nofollow(candidate_prediction, stage="source_contract")
        before = _source_signature(source_fd)
        if before.size > MAX_PREDICTION_BYTES:
            raise _invalid("source_contract")

        destination_fd = os.open(
            judge_copy,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _binary_flag(),
            0o600,
        )
        destination_created = True
        digest = sha256_digest()
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

        parsed_owner = _reserve_parsed_copy(parsed_copy)
        parsed_created = True
        _run_isolated_parser(judge_copy, parsed_copy, parsed_owner)
        copy_signature, copy_sha256 = _file_evidence(
            judge_copy,
            max_bytes=MAX_PREDICTION_BYTES,
        )
        parsed_signature, parsed_sha256 = _file_evidence(
            parsed_copy,
            max_bytes=_MAX_PARSED_BYTES,
        )
        if copy_sha256 != digest.hexdigest() or copy_signature.size != copied:
            raise _invalid("sealed_copy_identity")
        receipt = SealedPredictionReceipt._from_verified(
            path=judge_copy,
            size_bytes=copied,
            sha256=copy_sha256,
            copy_signature=copy_signature,
            parsed_path=parsed_copy,
            parsed_signature=parsed_signature,
            parsed_sha256=parsed_sha256,
        )
        completed = True
        return receipt
    except JudgeError:
        raise
    except (OSError, TypeError, ValueError):
        raise _invalid("ingestion_io") from None
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        if source_fd is not None:
            os.close(source_fd)
        if not completed:
            if destination_created:
                _remove_incomplete_copy(judge_copy)
            if parsed_created and parsed_owner is not None:
                _remove_owned_copy(parsed_copy, parsed_owner)


def iter_sealed_prediction_rows(
    receipt: SealedPredictionReceipt,
) -> Iterator[PredictionRow]:
    """receipt identity를 재검증하고 worker가 만든 행만 streaming 반환한다."""

    if not isinstance(receipt, SealedPredictionReceipt):
        raise _invalid("sealed_receipt")
    copy_signature, copy_sha256 = _file_evidence(
        receipt._path,
        max_bytes=MAX_PREDICTION_BYTES,
    )
    if (
        copy_signature != receipt._copy_signature
        or copy_signature.size != receipt._size_bytes
        or copy_sha256 != receipt._sha256
    ):
        raise _invalid("sealed_copy_identity")
    yield from _iter_parsed_rows(receipt)


def _iter_parsed_rows(receipt: SealedPredictionReceipt) -> Iterator[PredictionRow]:
    fd = _open_regular_nofollow(receipt._parsed_path, stage="parsed_copy_identity")
    before = _source_signature(fd)
    digest = sha256_digest()
    size = 0
    row_count = 0
    after = before
    try:
        try:
            with os.fdopen(fd, "rb", closefd=True) as stream:
                fd = -1
                while True:
                    raw_line = stream.readline(_MAX_PARSED_ROW_BYTES + 1)
                    if not raw_line:
                        break
                    size += len(raw_line)
                    digest.update(raw_line)
                    row_count += 1
                    if (
                        len(raw_line) > _MAX_PARSED_ROW_BYTES
                        or size > _MAX_PARSED_BYTES
                        or row_count > MAX_PREDICTION_ROWS
                    ):
                        raise _invalid("parsed_copy_contract")
                    yield _decode_parsed_row(raw_line)
                after = _signature_from_stat(os.fstat(stream.fileno()))
        except JudgeError:
            raise
        except (OSError, TypeError, ValueError):
            raise _invalid("parsed_copy_identity") from None
    finally:
        if fd >= 0:
            os.close(fd)
    if (
        before != after
        or after != receipt._parsed_signature
        or size != after.size
        or digest.hexdigest() != receipt._parsed_sha256
    ):
        raise _invalid("parsed_copy_identity")


def _decode_parsed_row(raw_line: bytes) -> PredictionRow:
    try:
        values = json.loads(raw_line)
        if (
            not isinstance(values, list)
            or len(values) != 4
            or not all(isinstance(value, str) for value in values[:3])
            or isinstance(values[3], bool)
            or not isinstance(values[3], (int, float))
        ):
            raise ValueError
        return PredictionRow(
            evaluation_id=values[0],
            slate_id=values[1],
            video_id=values[2],
            score=float(values[3]),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise _invalid("parsed_copy_contract") from None


def _open_regular_nofollow(path: Path, *, stage: str) -> int:
    try:
        source_stat = os.lstat(path)
    except (OSError, TypeError, ValueError):
        raise _invalid(stage) from None
    if not stat.S_ISREG(source_stat.st_mode):
        raise _invalid(stage)
    flags = os.O_RDONLY | _binary_flag() | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, TypeError, ValueError):
        raise _invalid(stage) from None
    try:
        opened = _source_signature(fd)
        if not stat.S_ISREG(opened.mode) or opened != _signature_from_stat(source_stat):
            raise _invalid(stage)
    except JudgeError:
        os.close(fd)
        raise
    except (OSError, TypeError, ValueError):
        os.close(fd)
        raise _invalid(stage) from None
    return fd


def _file_evidence(path: Path, *, max_bytes: int) -> tuple[_SourceSignature, str]:
    fd = _open_regular_nofollow(path, stage="sealed_copy_identity")
    before = _source_signature(fd)
    digest = sha256_digest()
    size = 0
    try:
        try:
            while True:
                chunk = os.read(fd, min(_COPY_CHUNK_BYTES, max_bytes + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise _invalid("sealed_copy_identity")
                digest.update(chunk)
            after = _source_signature(fd)
        except JudgeError:
            raise
        except (OSError, TypeError, ValueError):
            raise _invalid("sealed_copy_identity") from None
    finally:
        os.close(fd)
    if before != after or size != before.size:
        raise _invalid("sealed_copy_identity")
    return after, digest.hexdigest()


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


def _reserve_parsed_copy(parsed_copy: Path) -> _SourceSignature:
    try:
        fd = os.open(
            parsed_copy,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _binary_flag(),
            0o600,
        )
    except (OSError, TypeError, ValueError):
        raise _invalid("parsed_destination_exists") from None
    try:
        return _source_signature(fd)
    finally:
        os.close(fd)


def _run_isolated_parser(
    judge_copy: Path,
    parsed_copy: Path,
    parsed_owner: _SourceSignature,
) -> None:
    try:
        completed = subprocess.run(  # noqa: S603 - argv는 고정 모듈과 정수 상한이다.
            [
                sys.executable,
                str(Path(__file__).with_name("prediction_parser_worker.py")),
                str(PARSER_MEMORY_BYTES),
                str(_MAX_PARSED_BYTES),
                str(judge_copy),
                str(parsed_copy),
                str(parsed_owner.device),
                str(parsed_owner.inode),
            ],
            check=False,
            capture_output=True,
            timeout=PARSER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise _invalid("parser_timeout") from None
    if completed.returncode != 0:
        raise _invalid("parser_subprocess")


def _remove_incomplete_copy(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _remove_owned_copy(path: Path, owner: _SourceSignature) -> None:
    try:
        current = _signature_from_stat(os.lstat(path))
        if (
            stat.S_ISREG(current.mode)
            and current.device == owner.device
            and current.inode == owner.inode
        ):
            path.unlink()
    except OSError:
        pass


def _binary_flag() -> int:
    return getattr(os, "O_BINARY", 0)


def _invalid(stage: str) -> JudgeError:
    return JudgeError(JudgeErrorCode.INVALID_PREDICTIONS, stage)
