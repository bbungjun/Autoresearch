"""자율 실험 시작과 Controller 재개 사이의 불변 입력을 보존한다.

[파이프라인] Judge가 metadata를 준비한 뒤 candidate 실행을 시작하기 전 구간이다.
[기능] 실행 계약과 두 split의 원래 metadata bytes를 write-once 게시하고 검증·복구한다.
[비책임] 원천 조회·final 권한은 candidate_data_view/consumption_registry,
checkpoint append와 candidate checkout 분리는 실행 호출자가 담당한다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import re
import shutil
from tempfile import mkdtemp

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.research_harness._filesystem import sync_directory
from autoresearch.research_harness.candidate_metadata import (
    _USER_SCHEMA, _VIDEO_SCHEMA, select_metadata_as_of,
)
from autoresearch.research_harness.controller import ResearchBudget
from autoresearch.research_harness.evaluation_artifacts import CanonicalValue, canonical_json_bytes
from autoresearch.research_harness.evaluation_snapshot_models import ArtifactReceipt
from autoresearch.research_harness.feedback import ExperimentCard
from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode
from autoresearch.research_harness.fixture_models import (
    JudgeSnapshotHandoff, PreparedCandidateMetadata, PreparedMetadataArtifact,
)
from autoresearch.research_harness.judge_decision import JudgeMetric
from autoresearch.research_harness.ledger import LedgerArtifactEvidence
from autoresearch.research_harness.local_evaluation_fixture import (
    _acquire_descriptor_lock, _open_lock_matches, _prepare_descriptor_lock,
    _release_descriptor_lock, _resolved_without_link, _safe_regular_file_identity, _safe_tree,
)


_FILES = frozenset({"manifest.json", "validation/users.parquet", "validation/videos.parquet",
                    "final/users.parquet", "final/videos.parquet"})
_ENTRIES = _FILES | {"validation", "final"}
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_EVALUATION = re.compile(r"eval_[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class RunInputContract:
    """재개 때 바꿀 수 없는 초기 연구 입력; private 경로는 candidate에 전달하지 않는다."""

    initial_card: ExperimentCard
    budget: ResearchBudget
    baseline_sha: str
    champion_sha: str
    handoff: JudgeSnapshotHandoff = field(repr=False)
    judge_state_root: Path = field(repr=False)
    baseline_sigmas: tuple[tuple[str, float], ...]
    screening_seed: int
    confirmation_seeds: tuple[int, ...]
    runtime_json: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class FrozenRunInputs:
    """게시·복구한 metadata와 checkpoint에 연결할 manifest 증거."""

    validation_metadata: PreparedCandidateMetadata
    final_metadata: PreparedCandidateMetadata = field(repr=False)
    manifest_sha256: str
    artifact: LedgerArtifactEvidence = field(repr=False)


def freeze_run_inputs(
    root: Path, *, contract: RunInputContract,
    validation_metadata: PreparedCandidateMetadata, final_metadata: PreparedCandidateMetadata,
) -> FrozenRunInputs:
    """실행 입력을 게시하거나 완전히 동일한 기존 입력만 재사용한다.

    Args:
        root: fixture/candidate 밖에 있는 Judge-owned 절대 run 디렉터리.
        contract: strict runtime 모델로 검증한 canonical JSON을 포함한 실행 계약.
        validation_metadata: 해당 validation 평가의 이미 준비된 metadata bytes.
        final_metadata: Judge 측에만 준비된 해당 final 평가의 metadata bytes.

    Returns:
        두 metadata bundle과 manifest의 ledger artifact evidence.

    Raises:
        StageCError: 잘못된 입력, 기존 내용 차이 또는 파일 게시/검증 실패.
    """
    try:
        contract_value = _contract_value(contract)
        _validate_root(root, contract)
        bundles = (validation_metadata, final_metadata)
        manifest = _manifest_bytes(contract, contract_value, bundles)
        _prepare_root(root)
        if not _resolved_without_link(root):
            raise ValueError
        target = root / "run-inputs"
        lock_path = root / ".run-inputs.lock"
        identity = _prepare_descriptor_lock(lock_path)
        with lock_path.open("r+b") as lock_file:
            _acquire_descriptor_lock(lock_file)
            try:
                if not _open_lock_matches(lock_path, lock_file.fileno(), identity):
                    raise ValueError
                if os.path.lexists(target):
                    result = _load(target, contract, contract_value)
                    if result.manifest_sha256 != sha256(manifest).hexdigest():
                        raise ValueError
                    # A previous rename may have succeeded before its parent sync failed.
                    sync_directory(root)
                    return result
                staging = Path(mkdtemp(prefix=".run-inputs-staging-", dir=root))
                try:
                    _write_staging(staging, manifest, bundles)
                    _load(staging, contract, contract_value)
                    staging.rename(target)
                    sync_directory(root)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
                return _load(target, contract, contract_value)
            finally:
                _release_descriptor_lock(lock_file)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError,
            OverflowError, pa.ArrowException, StageCError):
        raise _error("run_inputs_freeze") from None


def load_run_inputs(root: Path, *, expected_contract: RunInputContract) -> FrozenRunInputs:
    """원천을 조회하지 않고 immutable run 입력을 복구한다.

    Args:
        root: 최초 게시 때 사용한 Judge-owned 절대 run 디렉터리.
        expected_contract: 재개 호출자가 독립적으로 구성한 동일 실행 계약.

    Returns:
        검증된 두 bundle과 checkpoint digest 대조에 사용할 artifact evidence.

    Raises:
        StageCError: 계약 drift, 파일 누락·변조·alias 또는 읽기 실패.
    """
    try:
        contract_value = _contract_value(expected_contract)
        _validate_root(root, expected_contract)
        return _load(root / "run-inputs", expected_contract, contract_value)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RuntimeError,
            OverflowError, pa.ArrowException, StageCError):
        raise _error("run_inputs_load") from None


def _contract_value(contract: RunInputContract) -> dict[str, CanonicalValue]:
    if not isinstance(contract, RunInputContract):
        raise ValueError
    card, budget, handoff = contract.initial_card, contract.budget, contract.handoff
    if (not isinstance(card, ExperimentCard) or not isinstance(budget, ResearchBudget)
            or not isinstance(handoff, JudgeSnapshotHandoff)
            or type(budget.max_trials) is not int or budget.max_trials <= 0
            or type(budget.max_duration_seconds) not in {int, float}
            or not isfinite(budget.max_duration_seconds) or budget.max_duration_seconds <= 0
            or not isinstance(contract.baseline_sha, str) or not _SHA.fullmatch(contract.baseline_sha)
            or not isinstance(contract.champion_sha, str) or not _SHA.fullmatch(contract.champion_sha)
            or type(contract.confirmation_seeds) is not tuple
            or len(contract.confirmation_seeds) != 5
            or any(type(seed) is not int or not 0 <= seed <= 2**32 - 1
                   for seed in (contract.screening_seed, *contract.confirmation_seeds))
            or len(set((contract.screening_seed, *contract.confirmation_seeds))) != 6
            or type(contract.baseline_sigmas) is not tuple):
        raise ValueError
    for pair in contract.baseline_sigmas:
        if (type(pair) is not tuple or len(pair) != 2 or not isinstance(pair[0], str)
                or type(pair[1]) not in {int, float} or not isfinite(pair[1]) or pair[1] < 0):
            raise ValueError
    sigmas = dict(contract.baseline_sigmas)
    if len(sigmas) != len(contract.baseline_sigmas) or set(sigmas) != {m.value for m in JudgeMetric}:
        raise ValueError
    for value, pattern in ((handoff.snapshot_fingerprint, _DIGEST), (handoff.manifest_sha256, _DIGEST),
                           (handoff.validation_id, _EVALUATION), (handoff.final_holdout_id, _EVALUATION)):
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError
    if handoff.validation_id == handoff.final_holdout_id:
        raise ValueError
    for path in (handoff.snapshot_root, contract.judge_state_root):
        _validate_absolute_path(path)
    if not isinstance(contract.runtime_json, str):
        raise ValueError
    runtime = json.loads(contract.runtime_json)
    if (not isinstance(runtime, dict)
            or json.dumps(runtime, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False) != contract.runtime_json):
        raise ValueError
    return {
        "initial_card": json.loads(card.canonical_summary()),
        "budget": {"max_trials": budget.max_trials, "max_duration_seconds": float(budget.max_duration_seconds)},
        "baseline_sha": contract.baseline_sha, "champion_sha": contract.champion_sha,
        "handoff": {"snapshot_fingerprint": str(handoff.snapshot_fingerprint),
                    "manifest_sha256": handoff.manifest_sha256,
                    "validation_id": str(handoff.validation_id), "final_holdout_id": str(handoff.final_holdout_id),
                    "snapshot_root": str(handoff.snapshot_root)},
        "judge_state_root": str(contract.judge_state_root),
        "baseline_sigmas": {name: float(value) for name, value in sigmas.items()},
        "screening_seed": contract.screening_seed,
        "confirmation_seeds": list(contract.confirmation_seeds), "runtime": runtime,
    }


def _validate_absolute_path(path: Path) -> None:
    if (not isinstance(path, Path) or not path.is_absolute() or "\0" in str(path)
            or path.resolve() != path.absolute()
            or any(p.exists() and not _resolved_without_link(p) for p in (path, *path.parents))):
        raise ValueError


def _validate_root(root: Path, contract: RunInputContract) -> None:
    _validate_absolute_path(root)
    for private in (contract.judge_state_root, contract.handoff.snapshot_root):
        if root.is_relative_to(private) or private.is_relative_to(root):
            raise ValueError


def _prepare_root(root: Path) -> None:
    missing: list[Path] = []
    current = root
    while not current.exists():
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        path.mkdir(exist_ok=True)
        if not _resolved_without_link(path):
            raise ValueError
        sync_directory(path.parent)


def _validate_bundle(bundle: PreparedCandidateMetadata, contract: RunInputContract, *, final: bool) -> None:
    expected_id = contract.handoff.final_holdout_id if final else contract.handoff.validation_id
    if (not isinstance(bundle, PreparedCandidateMetadata)
            or bundle.snapshot_fingerprint != contract.handoff.snapshot_fingerprint
            or bundle.evaluation_id != expected_id):
        raise ValueError
    for name, artifact, schema, key in (("users", bundle.users, _USER_SCHEMA, "user_id"),
                                         ("videos", bundle.videos, _VIDEO_SCHEMA, "video_id")):
        # Reconstruction revalidates even a caller-mutated frozen object.
        PreparedMetadataArtifact(artifact.receipt, artifact.payload)
        if artifact.receipt.relative_path != f"metadata/{name}.parquet":
            raise ValueError
        table = pq.ParquetFile(pa.BufferReader(artifact.payload)).read()
        if table.schema != schema or table.num_rows != artifact.receipt.rows:
            raise ValueError
        requests = pa.table({key: pa.array([], type=pa.string()),
                             "event_timestamp": pa.array([], type=pa.timestamp("us", tz="UTC"))})
        select_metadata_as_of(table, requests, entity_key=key)
        if not table.equals(table.sort_by([(key, "ascending"), ("available_at", "ascending")])):
            raise ValueError


def _manifest_bytes(
    contract: RunInputContract, contract_value: dict[str, CanonicalValue],
    bundles: tuple[PreparedCandidateMetadata, PreparedCandidateMetadata],
) -> bytes:
    metadata: dict[str, CanonicalValue] = {}
    for split, bundle in zip(("validation", "final"), bundles, strict=True):
        _validate_bundle(bundle, contract, final=split == "final")
        metadata[split] = {"snapshot_fingerprint": str(bundle.snapshot_fingerprint),
                           "evaluation_id": str(bundle.evaluation_id),
                           "users": asdict(bundle.users.receipt), "videos": asdict(bundle.videos.receipt)}
    return canonical_json_bytes({"contract_version": "run-inputs-v1", "contract": contract_value,
                                 "metadata": metadata})


def _read_file(path: Path) -> bytes:
    identity = _safe_regular_file_identity(path)
    if identity is None:
        raise ValueError
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        payload = stream.read()
        after = os.fstat(stream.fileno())
    if (identity != (before.st_dev, before.st_ino) or _safe_regular_file_identity(path) != identity
            or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns):
        raise ValueError
    return payload


def _load(
    target: Path, contract: RunInputContract, contract_value: dict[str, CanonicalValue],
) -> FrozenRunInputs:
    if (not _safe_tree(target)
            or {p.relative_to(target).as_posix() for p in target.rglob("*")} != _ENTRIES):
        raise ValueError
    manifest = _read_file(target / "manifest.json")
    value = json.loads(manifest)
    if not isinstance(value, dict) or value.get("contract") != contract_value:
        raise ValueError
    bundles: list[PreparedCandidateMetadata] = []
    for split in ("validation", "final"):
        meta = value["metadata"][split]
        artifacts = [PreparedMetadataArtifact(ArtifactReceipt(**meta[name]), _read_file(target / split / f"{name}.parquet"))
                     for name in ("users", "videos")]
        bundles.append(PreparedCandidateMetadata(meta["snapshot_fingerprint"], meta["evaluation_id"], *artifacts))
    validation, final = bundles
    if manifest != _manifest_bytes(contract, contract_value, (validation, final)):
        raise ValueError
    digest = sha256(manifest).hexdigest()
    return FrozenRunInputs(validation, final, digest,
                           LedgerArtifactEvidence("run-inputs", (target / "manifest.json").as_uri(), digest))


def _write_staging(
    staging: Path, manifest: bytes,
    bundles: tuple[PreparedCandidateMetadata, PreparedCandidateMetadata],
) -> None:
    for split, bundle in zip(("validation", "final"), bundles, strict=True):
        (staging / split).mkdir()
        for name, artifact in (("users", bundle.users), ("videos", bundle.videos)):
            _write_file(staging / split / f"{name}.parquet", artifact.payload)
        sync_directory(staging / split)
    _write_file(staging / "manifest.json", manifest)
    sync_directory(staging)


def _write_file(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _error(stage: str) -> StageCError:
    return StageCError(StageCErrorCode.CANDIDATE_VIEW_CONFLICT, stage)
