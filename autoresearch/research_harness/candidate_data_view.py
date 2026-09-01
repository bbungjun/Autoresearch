"""검증 전용 candidate data view의 local materializer.

[파이프라인] Stage B 평가 snapshot 게시 뒤와 격리된 candidate 학습·실행 앞에서
Judge handoff를 재검증하고 validation slate와 과거 action log만 별도 root에 게시한다.

[기능] source provenance·Judge state/source와 destination의 격리·filesystem identity·
Parquet receipt를 fail-closed로 확인하고, candidate-safe canonical manifest와 독립 byte
copy를 write-once atomic directory로 materialize한다.

[비책임] git worktree·subprocess argv·환경·credential 구성, final holdout 소비 권한과
소비 registry, metric/Judge 판정은 후속 Task 3/P0-2가 담당한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import os
from pathlib import Path
import shutil
import stat
from tempfile import mkdtemp
from typing import BinaryIO, Final

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes
from autoresearch.research_harness.evaluation_snapshot_models import (
    ArtifactReceipt,
)
from autoresearch.research_harness.evaluation_source import ActionLogSource
from autoresearch.research_harness.evaluation_source_models import SourcePartitionReceipt
from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode
from autoresearch.research_harness.fixture_models import (
    CandidateDataManifest,
    CandidateDataViewReceipt,
    CandidateDataViewRequest,
    CandidateHistoryReceipt,
)
from autoresearch.research_harness.local_evaluation_fixture import (
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

    destination_root = request.destination_root
    _require_safe_request(destination_root, request.judge.snapshot_root)
    handoff, manifest = _validated_judge_snapshot(
        request.judge.snapshot_root,
        expected_fingerprint=str(request.judge.snapshot_fingerprint),
    )
    if handoff != request.judge:
        raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "judge_handoff_identity")
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
        *manifest.validation.artifacts.slate.relative_path.split("/")
    )
    slate_identity = _safe_regular_file_identity(_io_path(slate_source))
    if slate_identity is None:
        raise _error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "candidate_slate_alias")

    candidate_manifest = CandidateDataManifest(
        contract_version="candidate-data-view-v1",
        evaluation_id=manifest.validation.evaluation_id,
        evaluation_start_date=manifest.window.evaluation_start_date,
        complete_history_label_end_date=(
            manifest.window.complete_history_label_end_date
        ),
        slate=ArtifactReceipt(
            relative_path="slate.parquet",
            rows=manifest.validation.artifacts.slate.rows,
            sha256=manifest.validation.artifacts.slate.sha256,
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
                        manifest.validation.artifacts.slate,
                        history,
                        history_payloads,
                        manifest_bytes,
                        slate_identity,
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
                    local_paths.append(path.resolve(strict=True))
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
                source_roots.append(root.resolve(strict=True))
        except (OSError, RuntimeError, TypeError):
            raise _error(
                StageCErrorCode.JUDGE_HANDOFF_INVALID,
                "candidate_source_path",
            ) from None
    destination = destination_root.resolve(strict=True)
    if any(path.is_relative_to(destination) for path in local_paths) or any(
        destination.is_relative_to(root) or root.is_relative_to(destination)
        for root in source_roots
    ):
        raise _error(StageCErrorCode.FIXTURE_REQUEST_INVALID, "candidate_source_relation")


def _require_disjoint_root(destination_root: Path, protected_root: Path) -> None:
    try:
        destination = destination_root.resolve(strict=True)
        protected = protected_root.resolve(strict=True)
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
    try:
        if not _safe_tree(root) or not _tree_is_exact(
            root, frozenset(files), frozenset(directories)
        ):
            return False
        manifest_bytes = _io_path(root / _MANIFEST_NAME).read_bytes()
        parsed = CandidateDataManifest.model_validate_json(manifest_bytes)
        if parsed != expected or manifest_bytes != expected_manifest_bytes:
            return False
        receipts = (expected.slate, *expected.history_partitions)
        source_identities = (slate_source_identity, *history_source_identities)
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
        destination = destination_root.resolve(strict=True)
        snapshot = snapshot_root.resolve(strict=True)
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
