"""실험 결과와 재개 checkpoint를 append-only Trial Ledger에 보존한다.

[파이프라인] Candidate 실행과 Judge 판정 뒤부터 Controller 재개 판단 전까지의
실험 증거 저장 구간을 담당한다.

[기능] canonical JSONL append, idempotency key 충돌 검사, 파일 동기화, 손상 탐지,
마지막 미완성 쓰기 복구와 checkpoint 조회를 하나의 영속 경계로 제공한다.

[비책임] 실험 실행·채점·판정, final holdout 소비 권한 발급, 원격·분산 저장소의
합의는 담당하지 않는다.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum, unique
import errno
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Iterator

from autoresearch.research_harness.consumption_registry import (
    FinalConsumptionEvidence,
)
from autoresearch.research_harness._filesystem import sync_directory


_CONTRACT_VERSION = "trial-ledger-v1"
_LEDGER_FILENAME = "experiment-ledger.jsonl"
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_DIFF_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EVALUATION_ID_PATTERN = re.compile(r"eval_[0-9a-f]{64}\Z")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


@unique
class LedgerErrorCode(StrEnum):
    """Controller가 기록 재시도 여부를 결정할 수 있는 오류 코드."""

    INVALID_REQUEST = "invalid_request"
    IO_FAILED = "io_failed"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INTEGRITY_VIOLATION = "integrity_violation"


@dataclass(slots=True)
class LedgerError(Exception):
    """민감한 경로와 원본 payload를 노출하지 않는 Ledger 오류."""

    code: LedgerErrorCode
    stage: str

    def __str__(self) -> str:
        return f"{self.code.value}: stage={self.stage}"


@dataclass(frozen=True, slots=True)
class LedgerMetric:
    """한 trial에서 측정한 metric 하나."""

    name: str
    value: float | None


@dataclass(frozen=True, slots=True)
class LedgerArtifactEvidence:
    """재현에 필요한 artifact의 위치와 내용 digest."""

    name: str
    uri: str
    sha256: str


@dataclass(frozen=True, slots=True)
class TrialRecord:
    """Candidate 변경과 Judge 결과를 연결하는 구조화된 실험 기록."""

    trial_id: str
    split: str
    base_sha: str
    candidate_sha: str | None
    diff_fingerprint: str | None
    evaluation_id: str
    seed: int
    metrics: tuple[LedgerMetric, ...]
    decision: str
    reason_code: str
    duration_ms: int
    failure_reason_code: str | None
    artifacts: tuple[LedgerArtifactEvidence, ...]
    champion_lineage: tuple[str, ...]
    final_consumption: FinalConsumptionEvidence | None


@dataclass(frozen=True, slots=True)
class CheckpointRecord:
    """Controller가 이미 완료한 stage를 건너뛰기 위한 durable checkpoint."""

    checkpoint_id: str
    stage: str
    trial_id: str
    completed_at: datetime
    artifacts: tuple[LedgerArtifactEvidence, ...]
    final_consumption: FinalConsumptionEvidence | None


LedgerRecord = TrialRecord | CheckpointRecord


@dataclass(frozen=True, slots=True)
class LedgerAppendReceipt:
    """append가 새 기록을 만들었는지 나타내는 receipt."""

    sequence: int
    created: bool


@dataclass(frozen=True, slots=True)
class TrialLedgerState:
    """검증된 Ledger를 순서대로 재생한 immutable 상태."""

    last_sequence: int
    trials: tuple[TrialRecord, ...]
    checkpoints: tuple[CheckpointRecord, ...]
    completed_checkpoint_ids: frozenset[str]
    registry_evidence: tuple[FinalConsumptionEvidence, ...]
    recovered_trailing_bytes: int

    def completed(self, checkpoint_id: str) -> bool:
        """Return whether a checkpoint id is already durable."""

        return checkpoint_id in self.completed_checkpoint_ids

    def checkpoint(self, checkpoint_id: str) -> CheckpointRecord:
        """Return one durable checkpoint, raising KeyError when absent."""

        for checkpoint in self.checkpoints:
            if checkpoint.checkpoint_id == checkpoint_id:
                return checkpoint
        raise KeyError(checkpoint_id)


@dataclass(frozen=True, slots=True)
class TrialLedger:
    """단일 로컬 run의 append-only Ledger 경계."""

    path: Path
    _initial_recovered_trailing_bytes: int = 0

    def append(self, record: LedgerRecord) -> LedgerAppendReceipt:
        """Append one record durably or return its idempotent prior sequence."""

        _validate_record(record)
        with _locked_ledger(self.path) as descriptor:
            state = _read_state(descriptor)
            existing = _find_existing(descriptor, record)
            if existing is not None:
                sequence, previous = existing
                if previous == record:
                    return LedgerAppendReceipt(sequence=sequence, created=False)
                raise LedgerError(
                    LedgerErrorCode.IDEMPOTENCY_CONFLICT,
                    "append_conflict",
                )

            sequence = state.last_sequence + 1
            payload = _canonical_line(sequence, record)
            try:
                os.lseek(descriptor, 0, os.SEEK_END)
                _write_all(descriptor, payload)
                _sync_file(descriptor)
            except OSError:
                raise LedgerError(LedgerErrorCode.IO_FAILED, "append_sync") from None
            return LedgerAppendReceipt(sequence=sequence, created=True)

    def read_state(self) -> TrialLedgerState:
        """Validate and replay the durable Ledger."""

        with _locked_ledger(self.path) as descriptor:
            state = _read_state(descriptor)
        recovered = max(
            state.recovered_trailing_bytes,
            self._initial_recovered_trailing_bytes,
        )
        if recovered == state.recovered_trailing_bytes:
            return state
        return TrialLedgerState(
            last_sequence=state.last_sequence,
            trials=state.trials,
            checkpoints=state.checkpoints,
            completed_checkpoint_ids=state.completed_checkpoint_ids,
            registry_evidence=state.registry_evidence,
            recovered_trailing_bytes=recovered,
        )


def open_trial_ledger(path: Path) -> TrialLedger:
    """Open, validate, and recover an existing local Trial Ledger."""

    normalized = _validated_path(path)
    with _locked_ledger(normalized) as descriptor:
        state = _read_state(descriptor)
    return TrialLedger(normalized, state.recovered_trailing_bytes)


def _validated_path(path: Path) -> Path:
    try:
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError
        if path.name != _LEDGER_FILENAME:
            raise ValueError
        parent = path.parent.resolve(strict=True)
        if not parent.is_dir():
            raise ValueError
        normalized = parent / _LEDGER_FILENAME
        if os.path.lexists(normalized):
            status = os.lstat(normalized)
            if not stat.S_ISREG(status.st_mode):
                raise ValueError
        return normalized
    except (OSError, RuntimeError, ValueError):
        raise LedgerError(LedgerErrorCode.INVALID_REQUEST, "ledger_path") from None


@contextmanager
def _locked_ledger(path: Path) -> Iterator[int]:
    lock_path = path.with_name(f".{path.name}.lock")
    lock_descriptor: int | None = None
    lock_identity: tuple[int, int] | None = None
    lock_acquired = False
    ledger_descriptor: int | None = None
    try:
        lock_descriptor, lock_identity = _open_lock(lock_path)
        _acquire_lock(lock_descriptor)
        lock_acquired = True
        if not _open_lock_matches(lock_path, lock_descriptor, lock_identity):
            raise LedgerError(LedgerErrorCode.IO_FAILED, "lock_validation")
        _initialize_locked_file(lock_descriptor)
        ledger_descriptor, _ = _open_data_file(path)
        _sync_file(ledger_descriptor)
        sync_directory(path.parent)
        yield ledger_descriptor
    except LedgerError:
        raise
    except OSError:
        raise LedgerError(LedgerErrorCode.IO_FAILED, "ledger_io") from None
    finally:
        if ledger_descriptor is not None:
            try:
                os.close(ledger_descriptor)
            except OSError:
                pass
        if lock_descriptor is not None and lock_acquired:
            try:
                _release_lock(lock_descriptor)
            except OSError:
                pass
        if lock_descriptor is not None:
            try:
                os.close(lock_descriptor)
            except OSError:
                pass


def _open_lock(path: Path) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if _safe_regular_file_identity(path) is None:
            raise LedgerError(LedgerErrorCode.IO_FAILED, "lock_validation") from None
        descriptor = os.open(path, flags)
    try:
        identity = _safe_regular_file_identity(path)
        if identity is None or not _open_lock_matches(path, descriptor, identity):
            raise LedgerError(LedgerErrorCode.IO_FAILED, "lock_validation")
        return descriptor, identity
    except (LedgerError, OSError):
        os.close(descriptor)
        raise


def _initialize_locked_file(descriptor: int) -> None:
    size = os.fstat(descriptor).st_size
    os.lseek(descriptor, 0, os.SEEK_SET)
    if size == 0:
        _write_all(descriptor, b"0")
        os.fsync(descriptor)
        return
    if size != 1 or os.read(descriptor, 1) != b"0":
        raise LedgerError(LedgerErrorCode.IO_FAILED, "lock_validation")


def _open_lock_matches(
    path: Path,
    descriptor: int,
    expected_identity: tuple[int, int],
) -> bool:
    try:
        opened = os.fstat(descriptor)
    except OSError:
        return False
    return (
        _safe_regular_file_identity(path) == expected_identity
        and (opened.st_dev, opened.st_ino) == expected_identity
        and stat.S_ISREG(opened.st_mode)
        and opened.st_nlink == 1
    )


def _safe_regular_file_identity(path: Path) -> tuple[int, int] | None:
    try:
        status = path.lstat()
        resolved = path.resolve(strict=True)
        absolute = path.absolute()
    except (OSError, RuntimeError):
        return None
    reparse = getattr(status, "st_file_attributes", 0) & 0x400
    if (
        reparse
        or not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or os.path.normcase(resolved) != os.path.normcase(absolute)
    ):
        return None
    return (status.st_dev, status.st_ino)


def _acquire_lock(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        while True:
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if not _windows_lock_is_contended(error):
                    raise
                time.sleep(0.01)
            else:
                return
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX)


def _release_lock(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _windows_lock_is_contended(error: OSError) -> bool:
    winerror = getattr(error, "winerror", None)
    if winerror is not None:
        return winerror in {33, 36}
    return error.errno in {errno.EACCES, errno.EDEADLK}


def _open_data_file(path: Path) -> tuple[int, bool]:
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        descriptor = os.open(path, flags)
        created = False
    try:
        identity = _safe_regular_file_identity(path)
        if identity is None or not _open_lock_matches(path, descriptor, identity):
            raise LedgerError(LedgerErrorCode.INVALID_REQUEST, "ledger_file")
        return descriptor, created
    except (LedgerError, OSError):
        os.close(descriptor)
        raise


def _read_state(descriptor: int) -> TrialLedgerState:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        payload = b"".join(chunks)
    except OSError:
        raise LedgerError(LedgerErrorCode.IO_FAILED, "ledger_read") from None

    recovered = 0
    if payload and not payload.endswith(b"\n"):
        complete_length = payload.rfind(b"\n") + 1
        recovered = len(payload) - complete_length
        try:
            os.ftruncate(descriptor, complete_length)
            os.fsync(descriptor)
        except OSError:
            raise LedgerError(LedgerErrorCode.IO_FAILED, "recovery_sync") from None
        payload = payload[:complete_length]

    trials: list[TrialRecord] = []
    checkpoints: list[CheckpointRecord] = []
    seen_trials: set[str] = set()
    seen_checkpoints: set[str] = set()
    evidence: list[FinalConsumptionEvidence] = []
    try:
        lines = payload.splitlines(keepends=True)
        for expected_sequence, line in enumerate(lines):
            envelope = json.loads(line, parse_constant=_reject_json_constant)
            if not isinstance(envelope, dict) or set(envelope) != {
                "contract_version",
                "sequence",
                "record_type",
                "payload",
            }:
                raise ValueError
            sequence = envelope["sequence"]
            if (
                envelope["contract_version"] != _CONTRACT_VERSION
                or isinstance(sequence, bool)
                or sequence != expected_sequence
            ):
                raise ValueError
            record = _record_from_payload(envelope["record_type"], envelope["payload"])
            _validate_record(record)
            if line != _canonical_line(expected_sequence, record):
                raise ValueError
            if isinstance(record, TrialRecord):
                if record.trial_id in seen_trials:
                    raise ValueError
                seen_trials.add(record.trial_id)
                trials.append(record)
            else:
                if record.checkpoint_id in seen_checkpoints:
                    raise ValueError
                seen_checkpoints.add(record.checkpoint_id)
                checkpoints.append(record)
            if record.final_consumption is not None and record.final_consumption not in evidence:
                evidence.append(record.final_consumption)
    except (JSONDecodeError, LedgerError, TypeError, ValueError):
        raise LedgerError(
            LedgerErrorCode.INTEGRITY_VIOLATION,
            "ledger_validation",
        ) from None

    return TrialLedgerState(
        last_sequence=len(lines) - 1,
        trials=tuple(trials),
        checkpoints=tuple(checkpoints),
        completed_checkpoint_ids=frozenset(seen_checkpoints),
        registry_evidence=tuple(evidence),
        recovered_trailing_bytes=recovered,
    )


JSONDecodeError = json.JSONDecodeError


def _reject_json_constant(_: str) -> None:
    raise ValueError


def _find_existing(
    descriptor: int,
    record: LedgerRecord,
) -> tuple[int, LedgerRecord] | None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        payload = b"".join(chunks)
        for sequence, line in enumerate(payload.splitlines()):
            envelope = json.loads(line, parse_constant=_reject_json_constant)
            item = _record_from_payload(envelope["record_type"], envelope["payload"])
            if (
                isinstance(record, TrialRecord)
                and isinstance(item, TrialRecord)
                and item.trial_id == record.trial_id
            ) or (
                isinstance(record, CheckpointRecord)
                and isinstance(item, CheckpointRecord)
                and item.checkpoint_id == record.checkpoint_id
            ):
                return sequence, item
    except (JSONDecodeError, KeyError, OSError, TypeError, ValueError):
        raise LedgerError(
            LedgerErrorCode.INTEGRITY_VIOLATION,
            "ledger_validation",
        ) from None
    return None


def _canonical_line(sequence: int, record: LedgerRecord) -> bytes:
    envelope = {
        "contract_version": _CONTRACT_VERSION,
        "payload": _record_payload(record),
        "record_type": "trial" if isinstance(record, TrialRecord) else "checkpoint",
        "sequence": sequence,
    }
    try:
        return (
            json.dumps(
                envelope,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise LedgerError(LedgerErrorCode.INVALID_REQUEST, "serialization") from None


def _record_payload(record: LedgerRecord) -> dict[str, object]:
    artifacts = [
        {"name": item.name, "sha256": item.sha256, "uri": item.uri}
        for item in record.artifacts
    ]
    final = _evidence_payload(record.final_consumption)
    if isinstance(record, TrialRecord):
        return {
            "artifacts": artifacts,
            "base_sha": record.base_sha,
            "candidate_sha": record.candidate_sha,
            "champion_lineage": list(record.champion_lineage),
            "decision": record.decision,
            "diff_fingerprint": record.diff_fingerprint,
            "duration_ms": record.duration_ms,
            "evaluation_id": record.evaluation_id,
            "failure_reason_code": record.failure_reason_code,
            "final_consumption": final,
            "metrics": [
                {"name": metric.name, "value": metric.value}
                for metric in record.metrics
            ],
            "reason_code": record.reason_code,
            "seed": record.seed,
            "split": record.split,
            "trial_id": record.trial_id,
        }
    return {
        "artifacts": artifacts,
        "checkpoint_id": record.checkpoint_id,
        "completed_at": _timestamp(record.completed_at),
        "final_consumption": final,
        "stage": record.stage,
        "trial_id": record.trial_id,
    }


def _evidence_payload(evidence: FinalConsumptionEvidence | None) -> object:
    if evidence is None:
        return None
    return {
        "marker_path": str(evidence.marker_path),
        "marker_sha256": evidence.marker_sha256,
    }


def _record_from_payload(record_type: object, payload: object) -> LedgerRecord:
    if not isinstance(payload, dict):
        raise ValueError
    if record_type == "trial":
        required = {
            "trial_id", "split", "base_sha", "candidate_sha", "diff_fingerprint",
            "evaluation_id", "seed", "metrics", "decision", "reason_code",
            "duration_ms", "failure_reason_code", "artifacts", "champion_lineage",
            "final_consumption",
        }
        if set(payload) != required:
            raise ValueError
        return TrialRecord(
            trial_id=payload["trial_id"], split=payload["split"],
            base_sha=payload["base_sha"], candidate_sha=payload["candidate_sha"],
            diff_fingerprint=payload["diff_fingerprint"],
            evaluation_id=payload["evaluation_id"], seed=payload["seed"],
            metrics=tuple(_metric(item) for item in _list(payload["metrics"])),
            decision=payload["decision"], reason_code=payload["reason_code"],
            duration_ms=payload["duration_ms"],
            failure_reason_code=payload["failure_reason_code"],
            artifacts=tuple(_artifact(item) for item in _list(payload["artifacts"])),
            champion_lineage=tuple(_list(payload["champion_lineage"])),
            final_consumption=_evidence(payload["final_consumption"]),
        )
    if record_type == "checkpoint":
        required = {
            "checkpoint_id", "stage", "trial_id", "completed_at", "artifacts",
            "final_consumption",
        }
        if set(payload) != required:
            raise ValueError
        return CheckpointRecord(
            checkpoint_id=payload["checkpoint_id"], stage=payload["stage"],
            trial_id=payload["trial_id"],
            completed_at=_parse_timestamp(payload["completed_at"]),
            artifacts=tuple(_artifact(item) for item in _list(payload["artifacts"])),
            final_consumption=_evidence(payload["final_consumption"]),
        )
    raise ValueError


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError
    return value


def _metric(value: object) -> LedgerMetric:
    if not isinstance(value, dict) or set(value) != {"name", "value"}:
        raise ValueError
    return LedgerMetric(name=value["name"], value=value["value"])


def _artifact(value: object) -> LedgerArtifactEvidence:
    if not isinstance(value, dict) or set(value) != {"name", "uri", "sha256"}:
        raise ValueError
    return LedgerArtifactEvidence(
        name=value["name"], uri=value["uri"], sha256=value["sha256"]
    )


def _evidence(value: object) -> FinalConsumptionEvidence | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"marker_path", "marker_sha256"}:
        raise ValueError
    return FinalConsumptionEvidence(
        marker_path=Path(value["marker_path"]),
        marker_sha256=value["marker_sha256"],
    )


def _validate_record(record: LedgerRecord) -> None:
    try:
        if isinstance(record, TrialRecord):
            _validate_identifier(record.trial_id)
            if record.split not in {"validation", "final_holdout"}:
                raise ValueError
            if _SHA_PATTERN.fullmatch(record.base_sha) is None:
                raise ValueError
            if (record.candidate_sha is None) != (record.diff_fingerprint is None):
                raise ValueError
            if record.candidate_sha is not None and (
                _SHA_PATTERN.fullmatch(record.candidate_sha) is None
                or _DIFF_PATTERN.fullmatch(record.diff_fingerprint or "") is None
            ):
                raise ValueError
            if _EVALUATION_ID_PATTERN.fullmatch(record.evaluation_id) is None:
                raise ValueError
            if isinstance(record.seed, bool) or not isinstance(record.seed, int) or record.seed < 0:
                raise ValueError
            if isinstance(record.duration_ms, bool) or not isinstance(record.duration_ms, int) or record.duration_ms < 0:
                raise ValueError
            _validate_identifier(record.decision)
            _validate_identifier(record.reason_code)
            if record.failure_reason_code is not None:
                _validate_identifier(record.failure_reason_code)
            _validate_metrics(record.metrics)
            _validate_artifacts(record.artifacts)
            if any(_SHA_PATTERN.fullmatch(item) is None for item in record.champion_lineage):
                raise ValueError
            _validate_evidence(record.final_consumption)
            if (record.split == "final_holdout") != (record.final_consumption is not None):
                raise ValueError
            return
        if isinstance(record, CheckpointRecord):
            _validate_identifier(record.checkpoint_id)
            _validate_identifier(record.stage)
            _validate_identifier(record.trial_id)
            if not isinstance(record.completed_at, datetime) or record.completed_at.tzinfo is None:
                raise ValueError
            _timestamp(record.completed_at)
            _validate_artifacts(record.artifacts)
            _validate_evidence(record.final_consumption)
            return
        raise ValueError
    except (AttributeError, OSError, TypeError, ValueError):
        raise LedgerError(LedgerErrorCode.INVALID_REQUEST, "record_validation") from None


def _validate_identifier(value: object) -> None:
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError


def _validate_metrics(metrics: object) -> None:
    if not isinstance(metrics, tuple):
        raise ValueError
    names: set[str] = set()
    for metric in metrics:
        if not isinstance(metric, LedgerMetric):
            raise ValueError
        _validate_identifier(metric.name)
        if metric.name in names:
            raise ValueError
        names.add(metric.name)
        if metric.value is not None and (
            not isinstance(metric.value, float) or not math.isfinite(metric.value)
        ):
            raise ValueError


def _validate_artifacts(artifacts: object) -> None:
    if not isinstance(artifacts, tuple):
        raise ValueError
    names: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, LedgerArtifactEvidence):
            raise ValueError
        _validate_identifier(artifact.name)
        if artifact.name in names:
            raise ValueError
        names.add(artifact.name)
        if not isinstance(artifact.uri, str) or not artifact.uri or "\n" in artifact.uri:
            raise ValueError
        if _DIGEST_PATTERN.fullmatch(artifact.sha256) is None:
            raise ValueError


def _validate_evidence(evidence: FinalConsumptionEvidence | None) -> None:
    if evidence is None:
        return
    if (
        not isinstance(evidence, FinalConsumptionEvidence)
        or not evidence.marker_path.is_absolute()
        or _DIGEST_PATTERN.fullmatch(evidence.marker_sha256) is None
    ):
        raise ValueError


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if _timestamp(parsed) != value:
        raise ValueError
    return parsed


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError
        offset += written


def _sync_file(descriptor: int) -> None:
    os.fsync(descriptor)
