"""Validation과 권한이 확인된 final candidate data view의 local materializer.

[파이프라인] Stage B 평가 snapshot 게시 뒤와 격리된 candidate 학습·실행 앞에서
Judge handoff를 재검증하고 선택한 slate와 과거 action log를 별도 root에 게시한다.

[기능] source provenance·Judge state/source와 destination의 격리·filesystem identity·
Parquet receipt를 fail-closed로 확인하고, candidate-safe canonical manifest와 독립 byte
copy를 write-once atomic directory로 materialize한다.
명시적으로 선택한 v2 경로는 검증된 fixture의 metadata를 한 번 준비하고 동일한 게시
검증을 거쳐 두 metadata 파일을 추가한다. Final 전용 interface는 실제 소비 grant를
게시·재사용 직전에 재확인하며, 기존 validation v1/v2 interface는 그대로 유지한다.
신뢰된 로컬 snapshot/source의 Windows 긴 경로는 내부 I/O 표현으로만 변환한다.

[비책임] git worktree·subprocess 구성은 workspace, final 소비 권한 발급은
consumption_registry, metric/Judge 판정은 judge가 담당한다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from hashlib import sha256
import os
from pathlib import Path
import shutil
import stat
from tempfile import mkdtemp
from typing import BinaryIO, Final

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pydantic import ValidationError

from autoresearch.research_harness.candidate_metadata import (
    _USER_SCHEMA, _VIDEO_SCHEMA,
    normalize_user_metadata, normalize_video_metadata, select_metadata_as_of,
)
from autoresearch.research_harness.consumption_registry import FinalConsumptionGrant
from autoresearch.research_harness.evaluation_artifacts import WRITER_OPTIONS, canonical_json_bytes
from autoresearch.research_harness.evaluation_snapshot_models import (
    ArtifactReceipt,
)
from autoresearch.research_harness.evaluation_source import ActionLogSource
from autoresearch.research_harness.evaluation_source_models import SourcePartitionReceipt
from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode
from autoresearch.research_harness.fixture_models import (
    CandidateDataManifest,
    CandidateDataManifestV2,
    CandidateDataViewReceipt,
    CandidateDataViewRequest,
    CandidateHistoryReceipt,
    FixtureDescriptor,
    FixtureInputReceipt,
    FixturePartitionReceipt,
    JudgeSnapshotHandoff,
    PreparedCandidateMetadata,
    PreparedMetadataArtifact,
)
from autoresearch.research_harness.local_evaluation_fixture import (
    FixtureActionLogSource,
    _acquire_descriptor_lock,
    _io_path,
    _open_lock_matches,
    _prepare_descriptor_lock,
    _release_descriptor_lock,
    _require_fixture_source_provenance,
    _resolved_without_link,
    _safe_regular_file_identity,
    _safe_tree,
    _tree_is_exact,
    _validated_judge_snapshot,
)


_MANIFEST_NAME: Final = "candidate-view.json"
_TARGET_NAME: Final = "harness_in"


@dataclass(frozen=True, slots=True)
class _SourcePayload:
    payload: bytes
    identity: tuple[int, int] | None


def materialize_candidate_data_view(
    request: CandidateDataViewRequest,
    *,
    source: ActionLogSource,
) -> CandidateDataViewReceipt:
    """Materialize the validation-only data allowed inside a candidate workspace."""

    return _materialize_candidate_data_view(request, source=source, metadata=None)


def materialize_candidate_data_view_v2(
    request: CandidateDataViewRequest,
    *,
    source: ActionLogSource,
    metadata: PreparedCandidateMetadata,
) -> CandidateDataViewReceipt:
    """준비한 validation metadata를 포함한 v2를 write-once로 게시한다.

    Args:
        request: 검증할 Judge handoff와 독립 destination root.
        source: snapshot과 동일한 action log source.
        metadata: prepare_candidate_metadata가 같은 평가용으로 준비한 불변 bundle.

    Returns:
        두 metadata 파일의 receipt를 포함하는 v2 manifest와 전체 view digest.

    Raises:
        StageCError: 입력 identity·파일 무결성·기존 target이 일치하지 않는 경우.
    """
    if not isinstance(metadata, PreparedCandidateMetadata):
        raise _error(StageCErrorCode.CANDIDATE_VIEW_CONFLICT, "metadata_bundle_required")
    return _materialize_candidate_data_view(request, source=source, metadata=metadata)


def materialize_final_candidate_data_view(
    request: CandidateDataViewRequest,
    *,
    source: ActionLogSource,
    metadata: PreparedCandidateMetadata,
    grant: FinalConsumptionGrant,
) -> CandidateDataViewReceipt:
    """유효한 소비 grant에 결속된 final slate와 metadata v2를 게시한다.

    Args:
        request: Judge handoff와 독립 destination root.
        source: snapshot과 동일한 action log source.
        metadata: Judge가 같은 final 평가용으로 미리 준비한 불변 bundle.
        grant: 기존 registry가 발급한 동일 snapshot의 소비 권한.

    Returns:
        final 입력의 전체 manifest와 불변 파일 receipt.

    Raises:
        StageCError: grant·marker·입력 identity 또는 게시 무결성이 잘못된 경우.
    """
    _require_final_grant(grant, request.judge)
    if not isinstance(metadata, PreparedCandidateMetadata):
        raise _error(StageCErrorCode.CANDIDATE_VIEW_CONFLICT, "metadata_bundle_required")
    return _materialize_candidate_data_view(request, source=source, metadata=metadata, grant=grant)


def _require_final_grant(grant: FinalConsumptionGrant, judge: JudgeSnapshotHandoff) -> None:
    if not isinstance(grant, FinalConsumptionGrant) or not grant._authorizes(judge):
        raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "final_candidate_grant")


def _materialize_candidate_data_view(
    request: CandidateDataViewRequest,
    *,
    source: ActionLogSource,
    metadata: PreparedCandidateMetadata | None,
    grant: FinalConsumptionGrant | None = None,
) -> CandidateDataViewReceipt:
    destination_root = request.destination_root
    _require_safe_request(destination_root, request.judge.snapshot_root)
    handoff, manifest = _validated_judge_snapshot(
        request.judge.snapshot_root,
        expected_fingerprint=str(request.judge.snapshot_fingerprint),
    )
    if handoff != request.judge:
        raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "judge_handoff_identity")
    split = manifest.validation if grant is None else manifest.final_holdout
    history = manifest.window.candidate_history_partitions
    if any(
        receipt.dt >= manifest.window.evaluation_start_date
        or receipt not in manifest.source.partitions
        for receipt in history
    ):
        raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "candidate_history_identity")
    if not _source_identity_matches(
        source,
        manifest.source.root,
        manifest.source.partitions,
    ):
        raise _error(
            StageCErrorCode.JUDGE_HANDOFF_INVALID,
            "candidate_source_identity",
        ) from None
    _require_source_disjoint(destination_root, source, history)
    judge_state_root = _require_fixture_source_provenance(source, handoff)
    if judge_state_root is not None:
        _require_disjoint_root(destination_root, judge_state_root)
    history_payloads = _load_history_payloads(source, history)
    slate_source = request.judge.snapshot_root.joinpath(
        *split.artifacts.slate.relative_path.split("/")
    )
    slate_identity = _safe_regular_file_identity(_io_path(slate_source))
    if slate_identity is None:
        raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "candidate_slate_alias")

    candidate_manifest = CandidateDataManifest(
        contract_version="candidate-data-view-v1",
        evaluation_id=split.evaluation_id,
        evaluation_start_date=manifest.window.evaluation_start_date,
        complete_history_label_end_date=(
            manifest.window.complete_history_label_end_date
        ),
        slate=ArtifactReceipt(
            relative_path="slate.parquet",
            rows=split.artifacts.slate.rows,
            sha256=split.artifacts.slate.sha256,
        ),
        history_partitions=tuple(
            CandidateHistoryReceipt(
                dt=receipt.dt,
                relative_path=(
                    f"history/action_log/dt={receipt.dt.isoformat()}/part-0.parquet"
                ),
                rows=receipt.rows,
                sha256=receipt.sha256,
            )
            for receipt in history
        ),
    )
    if metadata is not None:
        if (
            metadata.snapshot_fingerprint != request.judge.snapshot_fingerprint
            or metadata.evaluation_id != candidate_manifest.evaluation_id
        ):
            raise _error(StageCErrorCode.CANDIDATE_VIEW_CONFLICT, "metadata_evaluation_identity")
        try:
            candidate_manifest = CandidateDataManifestV2.model_validate({
                **candidate_manifest.model_dump(),
                "contract_version": "candidate-data-view-v2",
                "metadata_contract": "candidate-metadata-v1",
                "user_metadata": asdict(metadata.users.receipt),
                "video_metadata": asdict(metadata.videos.receipt),
            })
            requests = _metadata_requests(
                _read_verified_local(slate_source, split.artifacts.slate),
                history_payloads,
            )
            for key, artifact in (("user_id", metadata.users), ("video_id", metadata.videos)):
                _validate_metadata_artifact(artifact, requests, key)
        except (ValidationError, OSError, pa.ArrowException, ValueError, OverflowError):
            raise _error(StageCErrorCode.CANDIDATE_VIEW_CONFLICT, "metadata_bundle_validation") from None
    manifest_bytes = canonical_json_bytes(candidate_manifest.model_dump(mode="json"))
    manifest_digest = sha256(manifest_bytes).hexdigest()
    target = destination_root / _TARGET_NAME
    lock_path = destination_root / ".harness-in.lock"
    try:
        lock_identity = _prepare_descriptor_lock(lock_path)
    except StageCError:
        raise _error(StageCErrorCode.CANDIDATE_VIEW_CONFLICT, "candidate_lock_prepare") from None
    try:
        with lock_path.open("r+b") as lock_file:
            _lock_checked(lock_path, lock_file, lock_identity)
            try:
                if target.exists() or target.is_symlink():
                    if not _view_is_valid(
                        target,
                        candidate_manifest,
                        manifest_bytes,
                        slate_identity,
                        tuple(item.identity for item in history_payloads),
                    ):
                        raise _error(
                            StageCErrorCode.CANDIDATE_VIEW_CONFLICT,
                            "candidate_existing_view",
                        ) from None
                    if grant is not None:
                        _require_final_grant(grant, request.judge)
                    return CandidateDataViewReceipt(
                        root=target,
                        manifest=candidate_manifest,
                        manifest_sha256=manifest_digest,
                        reused=True,
                    )
                staging = Path(
                    mkdtemp(prefix=".harness-in-staging-", dir=destination_root)
                )
                try:
                    _write_staging(
                        staging,
                        request.judge.snapshot_root,
                        split.artifacts.slate,
                        history,
                        history_payloads,
                        manifest_bytes,
                        slate_identity,
                        metadata,
                    )
                    if not _view_is_valid(
                        staging,
                        candidate_manifest,
                        manifest_bytes,
                        slate_identity,
                        tuple(item.identity for item in history_payloads),
                    ):
                        raise _error(
                            StageCErrorCode.CANDIDATE_VIEW_CONFLICT,
                            "candidate_staging_validation",
                        ) from None
                    if grant is not None:
                        _require_final_grant(grant, request.judge)
                    staging.rename(target)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            finally:
                _release_descriptor_lock(lock_file)
    except StageCError:
        raise
    except (OSError, pa.ArrowException):
        raise _error(
            StageCErrorCode.CANDIDATE_VIEW_CONFLICT,
            "candidate_view_publish",
        ) from None
    return CandidateDataViewReceipt(
        root=target,
        manifest=candidate_manifest,
        manifest_sha256=manifest_digest,
        reused=False,
    )


def _write_staging(
    staging: Path,
    snapshot_root: Path,
    slate: ArtifactReceipt,
    history: tuple[SourcePartitionReceipt, ...],
    history_payloads: tuple[_SourcePayload, ...],
    manifest_bytes: bytes,
    slate_identity: tuple[int, int],
    metadata: PreparedCandidateMetadata | None = None,
) -> None:
    source_slate = snapshot_root.joinpath(*slate.relative_path.split("/"))
    slate_payload = _io_path(source_slate).read_bytes()
    if _safe_regular_file_identity(_io_path(source_slate)) != slate_identity:
        raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "candidate_slate_alias")
    _copy_verified_payload(
        slate_payload,
        staging / "slate.parquet",
        slate,
        source_identity=slate_identity,
    )
    for receipt, source_payload in zip(history, history_payloads, strict=True):
        target = staging / "history" / "action_log" / f"dt={receipt.dt.isoformat()}" / "part-0.parquet"
        _copy_verified_payload(
            source_payload.payload,
            target,
            receipt,
            source_identity=source_payload.identity,
        )
    if metadata is not None:
        for artifact in (metadata.users, metadata.videos):
            _copy_verified_payload(
                artifact.payload, staging / artifact.receipt.relative_path, artifact.receipt,
                source_identity=None,
            )
    (staging / _MANIFEST_NAME).write_bytes(manifest_bytes)


def _load_history_payloads(
    source: ActionLogSource,
    history: tuple[SourcePartitionReceipt, ...],
) -> tuple[_SourcePayload, ...]:
    payloads: list[_SourcePayload] = []
    for receipt in history:
        path_identity = _source_local_identity(source, receipt.dt)
        source_error = False
        handle_identity: tuple[int, int] | None = None
        try:
            with source.open_partition(receipt.dt) as handle:
                handle_valid, handle_identity = _open_local_identity(handle)
                if not handle_valid:
                    source_error = True
                    payload = b""
                else:
                    payload = handle.read()
        except (FileNotFoundError, OSError, TypeError, ValueError, pa.ArrowException):
            source_error = True
            payload = b""
        if source_error:
            raise _error(
                StageCErrorCode.JUDGE_HANDOFF_INVALID,
                "candidate_source_read",
            ) from None
        if sha256(payload).hexdigest() != receipt.sha256 or _payload_rows(payload) != receipt.rows:
            raise _error(
                StageCErrorCode.JUDGE_HANDOFF_INVALID,
                "candidate_source_integrity",
            )
        if (
            path_identity is not None
            and handle_identity is not None
            and path_identity != handle_identity
        ):
            raise _error(
                StageCErrorCode.JUDGE_HANDOFF_INVALID,
                "candidate_source_alias",
            )
        if path_identity is not None and _source_local_identity(
            source, receipt.dt
        ) != path_identity:
            raise _error(
                StageCErrorCode.JUDGE_HANDOFF_INVALID,
                "candidate_source_alias",
            )
        payloads.append(_SourcePayload(payload, path_identity or handle_identity))
    return tuple(payloads)


def _copy_verified_payload(
    payload: bytes,
    target: Path,
    receipt: ArtifactReceipt | SourcePartitionReceipt,
    *,
    source_identity: tuple[int, int] | None,
) -> None:
    expected_digest = receipt.sha256
    expected_rows = receipt.rows
    if sha256(payload).hexdigest() != expected_digest or _payload_rows(payload) != expected_rows:
        raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "candidate_source_integrity")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    target_identity = _safe_regular_file_identity(target)
    if (
        target_identity is None
        or target_identity == source_identity
        or sha256(target.read_bytes()).hexdigest() != expected_digest
        or pq.read_metadata(target).num_rows != expected_rows
    ):
        raise _error(StageCErrorCode.CANDIDATE_VIEW_CONFLICT, "candidate_copy_validation")


def _payload_rows(payload: bytes) -> int:
    try:
        return pq.read_metadata(pa.BufferReader(payload)).num_rows
    except pa.ArrowException:
        raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "candidate_source_integrity") from None


def _open_local_identity(
    handle: pa.NativeFile,
) -> tuple[bool, tuple[int, int] | None]:
    try:
        descriptor = handle.fileno()
    except (AttributeError, OSError, pa.ArrowException):
        return True, None
    try:
        opened = os.fstat(descriptor)
    except OSError:
        return False, None
    reparse = getattr(opened, "st_file_attributes", 0) & 0x400
    valid = not reparse and stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1
    return valid, (opened.st_dev, opened.st_ino) if valid else None


def _source_local_identity(
    source: ActionLogSource,
    partition_date: date,
) -> tuple[int, int] | None:
    local_path = getattr(source, "_physical_partition_path", None)
    if local_path is None:
        return None
    try:
        path = local_path(partition_date)
    except (OSError, TypeError):
        raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "candidate_source_alias") from None
    if path is None:
        return None
    if not isinstance(path, Path):
        raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "candidate_source_alias")
    identity = _safe_regular_file_identity(path)
    if identity is None:
        raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "candidate_source_alias")
    return identity


def _require_source_disjoint(
    destination_root: Path,
    source: ActionLogSource,
    history: tuple[SourcePartitionReceipt, ...],
) -> None:
    local_paths: list[Path] = []
    partition_path = getattr(source, "_physical_partition_path", None)
    if partition_path is not None:
        try:
            for receipt in history:
                path = partition_path(receipt.dt)
                if path is not None:
                    if not isinstance(path, Path):
                        raise TypeError
                    local_paths.append(_io_path(path).resolve(strict=True))
        except (OSError, RuntimeError, TypeError):
            raise _error(
                StageCErrorCode.JUDGE_HANDOFF_INVALID,
                "candidate_source_path",
            ) from None
    source_root_method = getattr(source, "_physical_source_root", None)
    source_roots: list[Path] = []
    if source_root_method is not None:
        try:
            root = source_root_method()
            if root is not None:
                if not isinstance(root, Path):
                    raise TypeError
                source_roots.append(_io_path(root).resolve(strict=True))
        except (OSError, RuntimeError, TypeError):
            raise _error(
                StageCErrorCode.JUDGE_HANDOFF_INVALID,
                "candidate_source_path",
            ) from None
    destination = _io_path(destination_root).resolve(strict=True)
    if any(path.is_relative_to(destination) for path in local_paths) or any(
        destination.is_relative_to(root) or root.is_relative_to(destination)
        for root in source_roots
    ):
        raise _error(StageCErrorCode.FIXTURE_REQUEST_INVALID, "candidate_source_relation")


def _require_disjoint_root(destination_root: Path, protected_root: Path) -> None:
    try:
        destination = _io_path(destination_root).resolve(strict=True)
        protected = _io_path(protected_root).resolve(strict=True)
    except (OSError, RuntimeError):
        raise _error(
            StageCErrorCode.JUDGE_HANDOFF_INVALID,
            "judge_state_root_validation",
        ) from None
    if destination.is_relative_to(protected) or protected.is_relative_to(destination):
        raise _error(
            StageCErrorCode.FIXTURE_REQUEST_INVALID,
            "candidate_judge_state_relation",
        )


def _source_identity_matches(
    source: ActionLogSource,
    expected_root: str,
    partitions: tuple[SourcePartitionReceipt, ...],
) -> bool:
    try:
        return source.opaque_root == expected_root and all(
            source.partition_uri(receipt.dt) == receipt.uri for receipt in partitions
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _view_is_valid(
    root: Path,
    expected: CandidateDataManifest,
    expected_manifest_bytes: bytes,
    slate_source_identity: tuple[int, int],
    history_source_identities: tuple[tuple[int, int] | None, ...],
) -> bool:
    files = {
        _MANIFEST_NAME,
        expected.slate.relative_path,
        *(receipt.relative_path for receipt in expected.history_partitions),
    }
    directories = {
        "history",
        "history/action_log",
        *(f"history/action_log/dt={receipt.dt.isoformat()}" for receipt in expected.history_partitions),
    }
    metadata_receipts = ()
    if isinstance(expected, CandidateDataManifestV2):
        metadata_receipts = (expected.user_metadata, expected.video_metadata)
        files.update(receipt.relative_path for receipt in metadata_receipts)
        directories.add("metadata")
    try:
        if not _safe_tree(root) or not _tree_is_exact(
            root, frozenset(files), frozenset(directories)
        ):
            return False
        manifest_bytes = _io_path(root / _MANIFEST_NAME).read_bytes()
        parsed = type(expected).model_validate_json(manifest_bytes)
        if parsed != expected or manifest_bytes != expected_manifest_bytes:
            return False
        receipts = (expected.slate, *expected.history_partitions, *metadata_receipts)
        source_identities = (slate_source_identity, *history_source_identities, *[None for _ in metadata_receipts])
        return all(
            _candidate_file_is_valid(
                root,
                receipt.relative_path,
                receipt.rows,
                receipt.sha256,
                source_identity,
            )
            for receipt, source_identity in zip(
                receipts, source_identities, strict=True
            )
        )
    except (OSError, ValidationError, StageCError, pa.ArrowException):
        return False


def _candidate_file_is_valid(
    root: Path,
    relative_path: str,
    rows: int,
    digest: str,
    source_identity: tuple[int, int] | None,
) -> bool:
    path = root.joinpath(*relative_path.split("/"))
    candidate_identity = _safe_regular_file_identity(path)
    return (
        candidate_identity is not None
        and candidate_identity != source_identity
        and sha256(path.read_bytes()).hexdigest() == digest
        and pq.read_metadata(path).num_rows == rows
    )


def _require_safe_request(destination_root: Path, snapshot_root: Path) -> None:
    if (
        not destination_root.is_absolute()
        or not destination_root.is_dir()
        or not _resolved_without_link(destination_root)
    ):
        raise _error(StageCErrorCode.FIXTURE_REQUEST_INVALID, "candidate_request_validation")
    try:
        destination = _io_path(destination_root).resolve(strict=True)
        snapshot = _io_path(snapshot_root).resolve(strict=True)
        if (
            snapshot.parent.name != "by-hash"
            or snapshot.parent.parent.name != "evaluation-snapshots"
        ):
            raise ValueError
        fixture_root = snapshot.parents[2]
    except (OSError, RuntimeError, IndexError, ValueError):
        raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "judge_snapshot_layout") from None
    if (
        destination.is_relative_to(snapshot)
        or snapshot.is_relative_to(destination)
        or destination.is_relative_to(fixture_root)
        or fixture_root.is_relative_to(destination)
    ):
        raise _error(StageCErrorCode.FIXTURE_REQUEST_INVALID, "candidate_root_relation")


def _lock_checked(
    lock_path: Path,
    lock_file: BinaryIO,
    expected_identity: tuple[int, int],
) -> None:
    try:
        _acquire_descriptor_lock(lock_file)
    except OSError:
        raise _error(StageCErrorCode.CANDIDATE_VIEW_CONFLICT, "candidate_lock") from None
    if not _open_lock_matches(lock_path, lock_file.fileno(), expected_identity):
        _release_descriptor_lock(lock_file)
        raise _error(StageCErrorCode.CANDIDATE_VIEW_CONFLICT, "candidate_lock_identity")


def _error(code: StageCErrorCode, stage: str) -> StageCError:
    return StageCError(code=code, stage=stage)


def prepare_candidate_metadata(
    judge: JudgeSnapshotHandoff, *, source: ActionLogSource,
) -> PreparedCandidateMetadata:
    """검증된 fixture에서 validation에 허용된 metadata bytes를 한 번 준비한다.

    Args:
        judge: 평가 snapshot과 연결된 검증용 handoff.
        source: 해당 fixture의 FixtureActionLogSource. 원격/임의 경로 입력은 받지 않는다.

    Returns:
        같은 평가의 baseline/candidate workspace에 재사용할 불변 Parquet bundle.

    Raises:
        StageCError: fixture·snapshot·원본 receipt·metadata schema가 잘못된 경우.
    """
    return _prepare_candidate_metadata(judge, source=source, final=False)


def prepare_final_candidate_metadata(
    judge: JudgeSnapshotHandoff, *, source: ActionLogSource,
) -> PreparedCandidateMetadata:
    """Judge 측에서 final용 metadata를 준비하되 소비하거나 candidate에 게시하지 않는다.

    Args:
        judge: 검증할 평가 snapshot의 Harness 소유 handoff.
        source: 같은 fixture의 FixtureActionLogSource.

    Returns:
        final 평가 ID에 결속된 불변 metadata bytes와 receipt.

    Raises:
        StageCError: fixture·snapshot·원본 receipt·metadata schema가 잘못된 경우.
    """
    return _prepare_candidate_metadata(judge, source=source, final=True)


def _prepare_candidate_metadata(
    judge: JudgeSnapshotHandoff, *, source: ActionLogSource, final: bool,
) -> PreparedCandidateMetadata:
    try:
        handoff, snapshot = _validated_judge_snapshot(
            judge.snapshot_root, expected_fingerprint=str(judge.snapshot_fingerprint),
        )
        if handoff != judge or type(source) is not FixtureActionLogSource:
            raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "metadata_fixture_source")
        _require_fixture_source_provenance(source, judge)
        descriptor_bytes = _io_path(source._fixture_root / "fixture.json").read_bytes()
        if sha256(descriptor_bytes).hexdigest() != source._descriptor_digest:
            raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "metadata_descriptor_identity")
        descriptor = FixtureDescriptor.model_validate_json(descriptor_bytes)
        history = _load_history_payloads(source, snapshot.window.candidate_history_partitions)
        split = snapshot.final_holdout if final else snapshot.validation
        slate = split.artifacts.slate
        requests = _metadata_requests(
            _read_verified_local(judge.snapshot_root / slate.relative_path, slate), history,
        )
        users = normalize_user_metadata(pq.read_table(pa.BufferReader(_read_verified_local(
            source._fixture_root / descriptor.virtual_users.relative_path, descriptor.virtual_users,
        ))))
        video_tables = [
            normalize_video_metadata(pq.read_table(pa.BufferReader(_read_verified_local(
                source._fixture_root / receipt.relative_path, receipt,
            ))))
            for receipt in descriptor.youtube_partitions
        ]
        videos = pa.concat_tables(video_tables).sort_by([("video_id", "ascending"), ("available_at", "ascending")])
        # 파티션 간 중복도 필터 전에 검증한다. 미래/미허용 행이라고 손상을 숨기지 않는다.
        select_metadata_as_of(
            videos, requests.select(["video_id", "event_timestamp"]).slice(0, 0), entity_key="video_id",
        )
        bundle = PreparedCandidateMetadata(
            snapshot_fingerprint=judge.snapshot_fingerprint,
            evaluation_id=split.evaluation_id,
            users=_metadata_artifact(_eligible_metadata(users, requests, "user_id"), "metadata/users.parquet"),
            videos=_metadata_artifact(_eligible_metadata(videos, requests, "video_id"), "metadata/videos.parquet"),
        )
        for key, artifact in (("user_id", bundle.users), ("video_id", bundle.videos)):
            _validate_metadata_artifact(artifact, requests, key)
        return bundle
    except StageCError:
        raise
    except (OSError, ValueError, OverflowError, ValidationError, pa.ArrowException):
        raise _error(StageCErrorCode.CANDIDATE_VIEW_CONFLICT, "metadata_prepare") from None


def _read_verified_local(
    path: Path, receipt: ArtifactReceipt | FixtureInputReceipt | FixturePartitionReceipt,
) -> bytes:
    path = _io_path(path)
    identity = _safe_regular_file_identity(path)
    if identity is None:
        raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "metadata_source_alias")
    payload = path.read_bytes()
    if (
        _safe_regular_file_identity(path) != identity
        or sha256(payload).hexdigest() != receipt.sha256
        or _payload_rows(payload) != receipt.rows
    ):
        raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "metadata_source_integrity")
    return payload


def _metadata_requests(slate: bytes, history: tuple[_SourcePayload, ...]) -> pa.Table:
    columns = ["user_id", "video_id", "event_timestamp"]
    table = pq.read_table(pa.BufferReader(slate)).select(columns)
    rows = table.to_pylist()
    for item in history:
        history_table = pq.read_table(pa.BufferReader(item.payload))
        rows.extend(history_table.filter(pc.equal(history_table["event_type"], "impression")).select(columns).to_pylist())
    return pa.Table.from_pylist(rows, schema=table.schema)


def _eligible_metadata(table: pa.Table, requests: pa.Table, key: str) -> pa.Table:
    latest: dict[str, datetime] = {}
    for row in requests.select([key, "event_timestamp"]).to_pylist():
        identifier, timestamp = row[key], row["event_timestamp"]
        latest[identifier] = max(latest.get(identifier, timestamp), timestamp)
    keep = [
        row[key] in latest and row["available_at"] <= latest[row[key]]
        for row in table.select([key, "available_at"]).to_pylist()
    ]
    return table.filter(pa.array(keep, type=pa.bool_()))


def _metadata_artifact(table: pa.Table, relative_path: str) -> PreparedMetadataArtifact:
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink, **asdict(WRITER_OPTIONS))
    payload = sink.getvalue().to_pybytes()
    return PreparedMetadataArtifact(
        ArtifactReceipt(relative_path=relative_path, rows=table.num_rows, sha256=sha256(payload).hexdigest()),
        payload,
    )


def _validate_metadata_artifact(
    artifact: PreparedMetadataArtifact, requests: pa.Table, key: str,
) -> None:
    if sha256(artifact.payload).hexdigest() != artifact.receipt.sha256:
        raise _error(StageCErrorCode.CANDIDATE_VIEW_CONFLICT, "metadata_bundle_digest")
    table = pq.read_table(pa.BufferReader(artifact.payload))
    if table.schema != (_USER_SCHEMA if key == "user_id" else _VIDEO_SCHEMA) or table.num_rows != artifact.receipt.rows:
        raise _error(StageCErrorCode.CANDIDATE_VIEW_CONFLICT, "metadata_bundle_schema")
    # 공개 selector로 값·중복을 검증하되 불필요한 미관측 출력 행은 만들지 않는다.
    select_metadata_as_of(table, requests.select([key, "event_timestamp"]).slice(0, 0), entity_key=key)
    if (
        not _eligible_metadata(table, requests, key).equals(table)
        or not table.sort_by([(key, "ascending"), ("available_at", "ascending")]).equals(table)
    ):
        raise _error(StageCErrorCode.CANDIDATE_VIEW_CONFLICT, "metadata_allowed_observations")
