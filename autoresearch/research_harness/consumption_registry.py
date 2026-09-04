"""Final holdout의 전역 단일 소비 권한을 durable marker로 선점한다.

[파이프라인] validation 반복이 끝나 champion이 고정된 뒤, Judge가 final target을 만들기
직전에 평가 snapshot과 전역 소비 상태를 결속하는 구간을 담당한다.

[기능] 기존 Judge 상태 루트의 evaluation별 marker를 원자 생성·동기화하고, marker evidence와
직접 만들 수 없는 final 소비 grant를 반환한다.
검증된 로컬 Windows 장경로는 내부 registry I/O에서만 extended path로 변환한다.

[비책임] final prediction 실행·채점·판정, Trial Ledger 기록, Controller의 복구 순서는 담당하지
않는다. marker 삭제와 같은 OS 사용자에 대한 완전한 filesystem 격리도 제공하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum, unique
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat

from autoresearch.research_harness.fixture_errors import StageCError
from autoresearch.research_harness._filesystem import sync_directory
from autoresearch.research_harness.fixture_models import JudgeSnapshotHandoff
from autoresearch.research_harness.local_evaluation_fixture import (
    _io_path,
    _validated_judge_snapshot,
)


_CONTRACT_VERSION = "final-consumption-v1"
_REGISTRY_DIRECTORY = "final-holdout-consumed"
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_EVALUATION_ID_PATTERN = re.compile(r"eval_[0-9a-f]{64}\Z")
_GRANT_TOKEN = object()
_MAX_MARKER_BYTES = 4096


@unique
class ConsumptionRegistryErrorCode(StrEnum):
    """Controller가 final 평가 시작 여부를 결정할 수 있는 오류 코드."""

    INVALID_REQUEST = "invalid_request"
    STATE_UNAVAILABLE = "state_unavailable"
    ALREADY_CONSUMED = "already_consumed"
    INTEGRITY_VIOLATION = "integrity_violation"


@dataclass(slots=True)
class ConsumptionRegistryError(Exception):
    """민감한 경로와 evaluation ID를 노출하지 않는 registry 오류."""

    code: ConsumptionRegistryErrorCode
    stage: str

    def __str__(self) -> str:
        return f"{self.code.value}: stage={self.stage}"


@dataclass(frozen=True, slots=True)
class FinalConsumptionEvidence:
    """Trial Ledger가 보존할 전역 marker의 절대 좌표와 내용 digest."""

    marker_path: Path
    marker_sha256: str


@dataclass(frozen=True, slots=True)
class FinalConsumptionRequest:
    """하나의 검증된 final holdout 소비를 선점하는 Harness 소유 입력."""

    judge_state_root: Path
    handoff: JudgeSnapshotHandoff
    baseline_sha: str
    candidate_sha: str
    started_at: datetime


@dataclass(frozen=True, slots=True, init=False, repr=False)
class FinalConsumptionGrant:
    """한 marker claim과 같은 snapshot에만 쓸 수 있는 opaque final 권한."""

    _snapshot_root: Path
    _snapshot_fingerprint: str
    _manifest_sha256: str
    _final_evaluation_id: str
    _evidence: FinalConsumptionEvidence
    _token: object

    def __init__(self, *_: object, **__: object) -> None:
        raise ConsumptionRegistryError(
            ConsumptionRegistryErrorCode.INVALID_REQUEST,
            "grant_construction",
        )

    @property
    def evidence(self) -> FinalConsumptionEvidence:
        """Return immutable marker evidence for the Trial Ledger."""

        return self._evidence

    def _authorizes(self, handoff: JudgeSnapshotHandoff) -> bool:
        try:
            return (
                self._token is _GRANT_TOKEN
                and _io_path(self._snapshot_root).resolve(strict=True)
                == _io_path(handoff.snapshot_root).resolve(strict=True)
                and self._snapshot_fingerprint == str(handoff.snapshot_fingerprint)
                and self._manifest_sha256 == handoff.manifest_sha256
                and self._final_evaluation_id == str(handoff.final_holdout_id)
                and self._evidence.marker_path.name == self._final_evaluation_id
                and self._evidence.marker_path.parent
                == self._snapshot_root.parents[2] / _REGISTRY_DIRECTORY
                and _DIGEST_PATTERN.fullmatch(self._evidence.marker_sha256) is not None
                and self._evidence.marker_path.is_absolute()
                and sha256(_read_regular_file(self._evidence.marker_path)).hexdigest()
                == self._evidence.marker_sha256
            )
        except (AttributeError, OSError, RuntimeError):
            return False

    def __repr__(self) -> str:
        return "FinalConsumptionGrant(<opaque>)"


def claim_final_consumption(
    request: FinalConsumptionRequest,
    *,
    prior_evidence: FinalConsumptionEvidence | None = None,
) -> FinalConsumptionGrant:
    """Atomically consume one final holdout and return its opaque Judge grant."""

    handoff = _validated_handoff(request)
    state_root, registry_root = _validated_registry_root(
        request.judge_state_root,
        handoff,
    )
    del state_root
    evaluation_id = str(handoff.final_holdout_id)
    marker_path = registry_root / evaluation_id
    payload = _marker_payload(request, evaluation_id)
    evidence = FinalConsumptionEvidence(
        marker_path=marker_path,
        marker_sha256=sha256(payload).hexdigest(),
    )

    if prior_evidence is not None:
        _verify_prior_evidence(prior_evidence, evidence)
    if os.path.lexists(_io_path(marker_path)):
        raise ConsumptionRegistryError(
            ConsumptionRegistryErrorCode.ALREADY_CONSUMED,
            "marker_exists",
        )

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(_io_path(marker_path), flags, 0o600)
    except FileExistsError:
        raise ConsumptionRegistryError(
            ConsumptionRegistryErrorCode.ALREADY_CONSUMED,
            "marker_exists",
        ) from None
    except OSError:
        raise ConsumptionRegistryError(
            ConsumptionRegistryErrorCode.STATE_UNAVAILABLE,
            "marker_create",
        ) from None

    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    except OSError:
        raise ConsumptionRegistryError(
            ConsumptionRegistryErrorCode.STATE_UNAVAILABLE,
            "marker_write",
        ) from None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass

    try:
        sync_directory(_io_path(registry_root))
    except OSError:
        raise ConsumptionRegistryError(
            ConsumptionRegistryErrorCode.STATE_UNAVAILABLE,
            "registry_sync",
        ) from None
    return _issue_grant(handoff, evidence)


def _issue_grant(
    handoff: JudgeSnapshotHandoff,
    evidence: FinalConsumptionEvidence,
) -> FinalConsumptionGrant:
    grant = object.__new__(FinalConsumptionGrant)
    object.__setattr__(
        grant,
        "_snapshot_root",
        _canonical_public_path(_io_path(handoff.snapshot_root).resolve(strict=True)),
    )
    object.__setattr__(grant, "_snapshot_fingerprint", str(handoff.snapshot_fingerprint))
    object.__setattr__(grant, "_manifest_sha256", handoff.manifest_sha256)
    object.__setattr__(grant, "_final_evaluation_id", str(handoff.final_holdout_id))
    object.__setattr__(grant, "_evidence", evidence)
    object.__setattr__(grant, "_token", _GRANT_TOKEN)
    return grant


def _validated_handoff(request: FinalConsumptionRequest) -> JudgeSnapshotHandoff:
    try:
        if not isinstance(request, FinalConsumptionRequest):
            raise ValueError
        if (
            _SHA_PATTERN.fullmatch(request.baseline_sha) is None
            or _SHA_PATTERN.fullmatch(request.candidate_sha) is None
            or request.started_at.tzinfo is None
            or request.started_at.utcoffset() is None
        ):
            raise ValueError
        verified, _manifest = _validated_judge_snapshot(
            request.handoff.snapshot_root,
            expected_fingerprint=str(request.handoff.snapshot_fingerprint),
        )
        if verified != request.handoff:
            raise ValueError
        if (
            _EVALUATION_ID_PATTERN.fullmatch(str(verified.final_holdout_id)) is None
            or _DIGEST_PATTERN.fullmatch(verified.manifest_sha256) is None
            or _DIGEST_PATTERN.fullmatch(str(verified.snapshot_fingerprint)) is None
        ):
            raise ValueError
        return verified
    except (AttributeError, OSError, RuntimeError, StageCError, TypeError, ValueError):
        raise ConsumptionRegistryError(
            ConsumptionRegistryErrorCode.INVALID_REQUEST,
            "request_validation",
        ) from None


def _validated_registry_root(
    configured_root: Path,
    handoff: JudgeSnapshotHandoff,
) -> tuple[Path, Path]:
    try:
        if not isinstance(configured_root, Path) or not configured_root.is_absolute():
            raise ValueError
        io_state_root = _io_path(configured_root).resolve(strict=True)
        io_registry_root = _io_path(
            configured_root / _REGISTRY_DIRECTORY
        ).resolve(strict=True)
        io_snapshot_root = _io_path(handoff.snapshot_root).resolve(strict=True)
        expected_state_root = io_snapshot_root.parents[2]
        if (
            not io_state_root.is_dir()
            or not io_registry_root.is_dir()
            or io_registry_root.parent != io_state_root
            or io_snapshot_root.parent.name != "by-hash"
            or io_snapshot_root.parent.parent.name != "evaluation-snapshots"
            or io_state_root != expected_state_root
        ):
            raise ValueError
        with os.scandir(io_registry_root):
            pass
        return (
            _canonical_public_path(io_state_root),
            _canonical_public_path(io_registry_root),
        )
    except (IndexError, OSError, RuntimeError, ValueError):
        raise ConsumptionRegistryError(
            ConsumptionRegistryErrorCode.STATE_UNAVAILABLE,
            "state_root_validation",
        ) from None


def _marker_payload(request: FinalConsumptionRequest, evaluation_id: str) -> bytes:
    payload = {
        "baseline_sha": request.baseline_sha,
        "candidate_sha": request.candidate_sha,
        "contract_version": _CONTRACT_VERSION,
        "evaluation_id": evaluation_id,
        "started_at": request.started_at.astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
    }
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _verify_prior_evidence(
    provided: FinalConsumptionEvidence,
    expected: FinalConsumptionEvidence,
) -> None:
    try:
        if (
            not isinstance(provided, FinalConsumptionEvidence)
            or provided.marker_path != expected.marker_path
            or _DIGEST_PATTERN.fullmatch(provided.marker_sha256) is None
            or not os.path.lexists(_io_path(expected.marker_path))
            or sha256(_read_regular_file(expected.marker_path)).hexdigest()
            != provided.marker_sha256
        ):
            raise ValueError
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ConsumptionRegistryError(
            ConsumptionRegistryErrorCode.INTEGRITY_VIOLATION,
            "prior_evidence_validation",
        ) from None


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(_io_path(path), flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_MARKER_BYTES:
            raise OSError
        chunks: list[bytes] = []
        remaining = _MAX_MARKER_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > _MAX_MARKER_BYTES:
            raise OSError
        return payload
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError
        offset += written


def _canonical_public_path(path: Path) -> Path:
    """Return a resolved Windows I/O path without exposing its device prefix."""

    value = str(path)
    if os.name != "nt":
        return path
    if value.startswith("\\\\?\\UNC\\"):
        return Path(f"\\\\{value[8:]}")
    if value.startswith("\\\\?\\"):
        return Path(value[4:])
    return path
