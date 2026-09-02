"""Judge 소유 local evaluation fixture의 결정적 조립·게시 경계.

[파이프라인] canonical virtual user·YouTube 입력과 Stage B 평가 snapshot 사이에서
production 일일 action log 4개를 생성하고 P0-2가 소비할 검증된 Judge handoff를 게시한다.

[기능] 물리 Judge 경로를 canonical fixture URI로 가리는 source adapter, coverage 검사,
derived path·lock alias 검증, content-addressed write-once fixture 게시와 완성 target
재검증 및 candidate source의 outer fixture provenance·canonical Judge state root 결속을
제공한다. 알려진 하위 오류의 public Stage C 번역에서는 원래 exception context를 숨긴다.
원본 artifact와 별개인 현재 final 소비 marker만 선택적 상태 파일로 허용한다.

[비책임] candidate data view·workspace·argv·환경 구성과 metric/Judge 판정은 후속 Stage C 및
P0-2 모듈이 담당한다. 임의 hostile filesystem actor와의 경쟁 방어도 담당하지 않는다.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, date, datetime
import errno
from hashlib import sha256
import logging
import os
from pathlib import Path
import shutil
import stat
from tempfile import mkdtemp
from time import sleep
from typing import BinaryIO, Final, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, ValidationError

from autoresearch.action_log_generation.daily import run_daily_action_log
from autoresearch.action_log_generation.pipeline import ActionLogGenerationError
from autoresearch.research_harness.evaluation_artifacts import (
    calculate_snapshot_fingerprint,
    canonical_json_bytes,
)
from autoresearch.research_harness.evaluation_errors import EvaluationSnapshotError
from autoresearch.research_harness.evaluation_snapshot_models import (
    ArtifactReceipt,
    EvaluationId,
    EvaluationSnapshotManifest,
    EvaluationSnapshotRequest,
    SnapshotFingerprint,
)
from autoresearch.research_harness.evaluation_source import ActionLogSource
from autoresearch.research_harness.evaluation_source_models import SourcePartitionReceipt
from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode
from autoresearch.research_harness.fixture_inputs import (
    canonical_fixture_dates,
    descriptor_sha256,
    write_canonical_fixture_inputs,
)
from autoresearch.research_harness.fixture_models import (
    FixtureDescriptor,
    JudgeSnapshotHandoff,
    LocalEvaluationFixtureReceipt,
    LocalEvaluationFixtureRequest,
)
from autoresearch.research_harness.slate import _build_evaluation_snapshot
logger = logging.getLogger(__name__)

_FIXTURES_PATH: Final = Path("fixtures") / "by-hash"
_ARTIFACT_COUNT: Final = 4
_SNAPSHOT_FILES: Final = frozenset(
    {
        "_SUCCESS",
        "manifest.json",
        "validation/slate.parquet",
        "validation/labels.parquet",
        "final_holdout/slate.parquet",
        "final_holdout/labels.parquet",
    }
)
_SNAPSHOT_DIRS: Final = frozenset({"validation", "final_holdout"})


class _FixtureIntegrityReceipt(BaseModel):
    """Canonical outer marker anchoring every published fixture output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["local-fixture-integrity-v1"]
    descriptor_sha256: str
    action_log_partitions: tuple[SourcePartitionReceipt, ...]
    snapshot_fingerprint: SnapshotFingerprint
    manifest_sha256: str
    validation_id: EvaluationId
    final_holdout_id: EvaluationId
    snapshot_artifacts: tuple[ArtifactReceipt, ...]


class FixtureActionLogSource(ActionLogSource):
    """Read physical fixture action logs while exposing canonical identity URIs."""

    def __init__(
        self,
        fixture_root: Path,
        descriptor_digest: str,
        *,
        _opened_dates: list[date] | None = None,
    ) -> None:
        self._fixture_root = fixture_root
        self._descriptor_digest = descriptor_digest
        self._physical_root = fixture_root / "action_log"
        self._opaque_root = f"fixture://{descriptor_digest}/action-log"
        self._opened_dates = _opened_dates

    @property
    def opaque_root(self) -> str:
        return self._opaque_root

    def partition_uri(self, dt: date) -> str:
        return f"{self._opaque_root}/dt={dt.isoformat()}/part-0.parquet"

    def _physical_partition_path(self, dt: date) -> Path:
        """Return the trusted-process-only local path for alias validation."""

        return self._physical_root / f"dt={dt.isoformat()}" / "part-0.parquet"

    def _physical_source_root(self) -> Path:
        return self._physical_root

    def open_partition(self, dt: date) -> AbstractContextManager[pa.NativeFile]:
        if self._opened_dates is not None:
            self._opened_dates.append(dt)
        return pa.OSFile(
            str(self._physical_partition_path(dt)),
            "rb",
        )


def build_local_evaluation_fixture(
    request: LocalEvaluationFixtureRequest,
) -> LocalEvaluationFixtureReceipt:
    """Build or fully validate one content-addressed Judge-owned local fixture."""

    _require_safe_state_root(request.judge_state_root)
    output_root = _prepare_fixture_output_root(request.judge_state_root)
    try:
        staging = Path(mkdtemp(prefix=".staging-", dir=output_root))
    except OSError:
        raise _fixture_error(
            StageCErrorCode.FIXTURE_REQUEST_INVALID,
            "fixture_root_prepare",
        ) from None

    try:
        descriptor = write_canonical_fixture_inputs(staging, request)
        descriptor_digest = descriptor_sha256(descriptor)
        target = output_root / descriptor_digest
        lock_path = output_root / f".{descriptor_digest}.lock"
        expected_lock_identity = _prepare_descriptor_lock(lock_path)
        with lock_path.open("r+b") as lock_file:
            if not _open_lock_matches(
                lock_path, lock_file.fileno(), expected_lock_identity
            ):
                raise _fixture_error(
                    StageCErrorCode.FIXTURE_STATE_CONFLICT,
                    "fixture_lock_validation",
                ) from None
            _acquire_descriptor_lock(lock_file)
            try:
                if target.exists():
                    if not _fixture_is_valid(target, descriptor, descriptor_digest):
                        raise _fixture_error(
                            StageCErrorCode.FIXTURE_STATE_CONFLICT,
                            "fixture_reuse_validation",
                        ) from None
                    return _receipt_from_complete_fixture(
                        target, descriptor_digest, reused=True
                    )
                _build_staged_fixture(staging, descriptor, descriptor_digest)
                handoff = _validated_judge_handoff(
                    _snapshot_root_from_staging(staging),
                    expected_fingerprint=None,
                )
                action_receipts = _manifest_partitions(handoff.snapshot_root)
                integrity = _integrity_receipt(
                    descriptor_digest, action_receipts, handoff
                )
                (staging / "_SUCCESS").write_bytes(
                    canonical_json_bytes(integrity.model_dump(mode="json"))
                )
                if not _fixture_is_valid(staging, descriptor, descriptor_digest):
                    raise _fixture_error(
                        StageCErrorCode.FIXTURE_STATE_CONFLICT,
                        "fixture_staging_validation",
                    ) from None
                staging.rename(target)
                return LocalEvaluationFixtureReceipt(
                    fixture_root=target,
                    descriptor_path=target / "fixture.json",
                    descriptor_sha256=descriptor_digest,
                    action_log_partitions=action_receipts,
                    judge=JudgeSnapshotHandoff(
                        snapshot_fingerprint=handoff.snapshot_fingerprint,
                        snapshot_root=target
                        / handoff.snapshot_root.relative_to(staging),
                        manifest_sha256=handoff.manifest_sha256,
                        validation_id=handoff.validation_id,
                        final_holdout_id=handoff.final_holdout_id,
                    ),
                    reused=False,
                )
            finally:
                _release_descriptor_lock(lock_file)
    except StageCError:
        raise
    except (
        ActionLogGenerationError,
        EvaluationSnapshotError,
        OSError,
        pa.ArrowException,
        ValidationError,
    ):
        raise _fixture_error(
            StageCErrorCode.FIXTURE_STATE_CONFLICT,
            "fixture_build",
        ) from None
    finally:
        if staging.exists():
            try:
                shutil.rmtree(staging)
            except OSError:
                logger.warning(
                    "fixture_staging_cleanup_failed",
                    extra={"stage": "fixture_staging_cleanup"},
                )


def _build_staged_fixture(
    staging: Path,
    descriptor: FixtureDescriptor,
    descriptor_digest: str,
) -> None:
    for partition_date in canonical_fixture_dates(descriptor.evaluation_start_date):
        run_daily_action_log(
            partition_date=partition_date,
            youtube_base_path=str(staging / "inputs" / "youtube_trending_kr"),
            virtual_users_path=str(staging / "inputs" / "virtual_users.parquet"),
            output_base_path=str(staging / "action_log"),
            candidates_per_user=24,
            click_threshold=0.0,
            personalized_ratio=0.7,
            popular_ratio=0.2,
            exploration_ratio=0.1,
            seed=descriptor.fixture_seed,
            max_concurrency=1,
            chunk_size=0,
            max_quarantine_ratio=0.0,
            generator_name="rule_based",
            model_name="fixture-rule-action-log",
            overwrite=False,
            completion_timestamp=datetime.combine(
                partition_date, datetime.min.time(), tzinfo=UTC
            ),
        )
    source = FixtureActionLogSource(staging, descriptor_digest)
    snapshot = _build_evaluation_snapshot(
        EvaluationSnapshotRequest(
            action_log_root=source.opaque_root,
            history_start_date=descriptor.history_start_date,
            evaluation_start_date=descriptor.evaluation_start_date,
            evaluation_end_date=descriptor.evaluation_end_date,
            slate_id_cutover_date=descriptor.slate_id_cutover_date,
            output_root=staging,
        ),
        source=source,
        created_at=datetime.combine(
            descriptor.evaluation_start_date,
            datetime.min.time(),
            tzinfo=UTC,
        ),
    )
    _validate_coverage(snapshot.target_path)
    snapshot_lock = (
        staging
        / "evaluation-snapshots"
        / "by-hash"
        / f".{snapshot.snapshot_fingerprint}.lock"
    )
    snapshot_lock.unlink()


def _validate_coverage(snapshot_root: Path) -> None:
    try:
        manifest = EvaluationSnapshotManifest.model_validate_json(
            _io_path(snapshot_root / "manifest.json").read_bytes()
        )
    except (OSError, ValidationError):
        raise _fixture_error(
            StageCErrorCode.FIXTURE_COVERAGE_INSUFFICIENT,
            "fixture_coverage_validation",
        ) from None
    for split, expected_user_count in (
        (manifest.validation, 160),
        (manifest.final_holdout, 40),
    ):
        counts = split.counts
        try:
            slate = pq.read_table(
                _io_path(snapshot_root / split.artifacts.slate.relative_path)
            )
            labels = pq.read_table(
                _io_path(snapshot_root / split.artifacts.labels.relative_path)
            )
        except (OSError, pa.ArrowException):
            raise _fixture_error(
                StageCErrorCode.FIXTURE_COVERAGE_INSUFFICIENT,
                "fixture_coverage_artifact_read",
            ) from None
        slate_groups: dict[tuple[str, str], int] = {}
        for row in slate.select(["user_id", "slate_id"]).to_pylist():
            key = (row["user_id"], row["slate_id"])
            slate_groups[key] = slate_groups.get(key, 0) + 1
        label_rows = labels.select(["slate_id", "clicked"]).to_pylist()
        positive_slates = {
            row["slate_id"] for row in label_rows if row["clicked"] is True
        }
        clicked_rows = len([row for row in label_rows if row["clicked"] is True])
        slate_count = len(slate_groups)
        positive_ratio = len(positive_slates) / slate_count if slate_count else 0.0
        if (
            slate.num_rows != labels.num_rows
            or any(size != 24 for size in slate_groups.values())
            or counts.row_count != slate.num_rows
            or counts.user_count != expected_user_count
            or len({key[0] for key in slate_groups}) != expected_user_count
            or counts.slate_count != slate_count
            or counts.clicked_row_count != clicked_rows
            or counts.click_positive_slate_count != len(positive_slates)
            or abs(counts.click_positive_slate_ratio - positive_ratio) > 1e-12
            or counts.click_positive_slate_count != slate_count
            or len(positive_slates) != slate_count
            or counts.click_positive_slate_ratio != 1.0
            or counts.clicked_row_count <= 0
            or counts.clicked_row_count >= counts.row_count
            or counts.mean_slate_size != 24.0
        ):
            raise _fixture_error(
                StageCErrorCode.FIXTURE_COVERAGE_INSUFFICIENT,
                "fixture_coverage_validation",
            )


def _snapshot_root_from_staging(staging: Path) -> Path:
    roots = tuple(
        path
        for path in (staging / "evaluation-snapshots" / "by-hash").iterdir()
        if path.is_dir()
    )
    if len(roots) != 1:
        raise _fixture_error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "judge_snapshot_lookup")
    return roots[0]


def _validated_judge_handoff(
    snapshot_root: Path,
    *,
    expected_fingerprint: str | None,
) -> JudgeSnapshotHandoff:
    handoff, _ = _validated_judge_snapshot(
        snapshot_root,
        expected_fingerprint=expected_fingerprint,
    )
    return handoff


def _validated_judge_snapshot(
    snapshot_root: Path,
    *,
    expected_fingerprint: str | None,
) -> tuple[JudgeSnapshotHandoff, EvaluationSnapshotManifest]:
    """Return one manifest and handoff after a single sanitized validation pass."""

    try:
        if not _safe_tree(snapshot_root) or not _tree_is_exact(
            snapshot_root, _SNAPSHOT_FILES, _SNAPSHOT_DIRS
        ):
            raise ValueError
        manifest_bytes = _io_path(snapshot_root / "manifest.json").read_bytes()
        manifest = EvaluationSnapshotManifest.model_validate_json(manifest_bytes)
        fingerprint = str(manifest.snapshot_fingerprint)
        if (
            manifest_bytes != canonical_json_bytes(manifest.model_dump(mode="json"))
            or snapshot_root.name != fingerprint
            or expected_fingerprint not in {None, fingerprint}
            or calculate_snapshot_fingerprint(manifest) != manifest.snapshot_fingerprint
            or _io_path(snapshot_root / "_SUCCESS").read_text(encoding="utf-8")
            != f"{fingerprint}\n"
        ):
            raise ValueError
        artifacts = (
            manifest.validation.artifacts.slate,
            manifest.validation.artifacts.labels,
            manifest.final_holdout.artifacts.slate,
            manifest.final_holdout.artifacts.labels,
        )
        if len(artifacts) != _ARTIFACT_COUNT or tuple(
            artifact.relative_path for artifact in artifacts
        ) != (
            "validation/slate.parquet",
            "validation/labels.parquet",
            "final_holdout/slate.parquet",
            "final_holdout/labels.parquet",
        ):
            raise ValueError
        for artifact in artifacts:
            path = snapshot_root.joinpath(*artifact.relative_path.split("/"))
            io_path = _io_path(path)
            if (
                not io_path.is_file()
                or sha256(io_path.read_bytes()).hexdigest() != artifact.sha256
                or pq.read_metadata(io_path).num_rows != artifact.rows
            ):
                raise ValueError
    except (
        OSError,
        UnicodeError,
        EvaluationSnapshotError,
        ValidationError,
        ValueError,
        pa.ArrowException,
    ):
        raise _fixture_error(
            StageCErrorCode.JUDGE_HANDOFF_INVALID,
            "judge_handoff_validation",
        ) from None
    return (
        JudgeSnapshotHandoff(
            snapshot_fingerprint=manifest.snapshot_fingerprint,
            snapshot_root=snapshot_root,
            manifest_sha256=sha256(manifest_bytes).hexdigest(),
            validation_id=manifest.validation.evaluation_id,
            final_holdout_id=manifest.final_holdout.evaluation_id,
        ),
        manifest,
    )


def _fixture_is_valid(
    root: Path,
    expected_descriptor: FixtureDescriptor,
    descriptor_digest: str,
) -> bool:
    try:
        if not _safe_tree(root):
            return False
        descriptor_bytes = (root / "fixture.json").read_bytes()
        parsed = FixtureDescriptor.model_validate_json(descriptor_bytes)
        integrity_bytes = (root / "_SUCCESS").read_bytes()
        integrity = _FixtureIntegrityReceipt.model_validate_json(integrity_bytes)
        if (
            parsed != expected_descriptor
            or sha256(descriptor_bytes).hexdigest() != descriptor_digest
            or integrity_bytes
            != canonical_json_bytes(integrity.model_dump(mode="json"))
            or integrity.contract_version != "local-fixture-integrity-v1"
            or integrity.descriptor_sha256 != descriptor_digest
        ):
            return False
        input_receipts = (parsed.virtual_users, *parsed.youtube_partitions)
        for receipt in input_receipts:
            path = root.joinpath(*receipt.relative_path.split("/"))
            if (
                not path.is_file()
                or sha256(path.read_bytes()).hexdigest() != receipt.sha256
                or pq.read_metadata(path).num_rows != receipt.rows
            ):
                return False
        snapshot_root = _single_snapshot_root(root)
        handoff = _validated_judge_handoff(
            snapshot_root,
            expected_fingerprint=str(integrity.snapshot_fingerprint),
        )
        partitions = _manifest_partitions(handoff.snapshot_root)
        expected_dates = canonical_fixture_dates(parsed.evaluation_start_date)
        if (
            tuple(receipt.dt for receipt in partitions) != expected_dates
            or partitions != integrity.action_log_partitions
            or handoff.manifest_sha256 != integrity.manifest_sha256
            or handoff.validation_id != integrity.validation_id
            or handoff.final_holdout_id != integrity.final_holdout_id
            or _snapshot_artifacts(snapshot_root) != integrity.snapshot_artifacts
            or not _fixture_tree_is_exact(root, parsed, handoff)
        ):
            return False
        for receipt in partitions:
            path = root / "action_log" / f"dt={receipt.dt.isoformat()}" / "part-0.parquet"
            if (
                receipt.uri
                != f"fixture://{descriptor_digest}/action-log/dt={receipt.dt.isoformat()}/part-0.parquet"
                or not path.is_file()
                or sha256(path.read_bytes()).hexdigest() != receipt.sha256
                or pq.read_metadata(path).num_rows != receipt.rows
            ):
                return False
        _validate_coverage(snapshot_root)
        return True
    except (OSError, ValidationError, StageCError, pa.ArrowException):
        return False


def _require_fixture_source_provenance(
    source: ActionLogSource,
    handoff: JudgeSnapshotHandoff,
) -> Path | None:
    """Bind fixture:// identity to one fully validated physical fixture root."""

    try:
        opaque_root = source.opaque_root
    except (AttributeError, OSError, TypeError, ValueError):
        raise _fixture_error(
            StageCErrorCode.JUDGE_HANDOFF_INVALID,
            "fixture_source_provenance",
        ) from None
    if not isinstance(opaque_root, str):
        raise _fixture_error(
            StageCErrorCode.JUDGE_HANDOFF_INVALID,
            "fixture_source_provenance",
        )
    if not opaque_root.startswith("fixture://"):
        return None
    if type(source) is not FixtureActionLogSource:
        raise _fixture_error(
            StageCErrorCode.JUDGE_HANDOFF_INVALID,
            "fixture_source_provenance",
        )
    fixture_root = source._fixture_root
    descriptor_digest = source._descriptor_digest
    expected_root = f"fixture://{descriptor_digest}/action-log"
    expected_snapshot = (
        fixture_root
        / "evaluation-snapshots"
        / "by-hash"
        / str(handoff.snapshot_fingerprint)
    )
    try:
        if (
            fixture_root.name != descriptor_digest
            or fixture_root.parent.name != "by-hash"
            or fixture_root.parent.parent.name != "fixtures"
        ):
            raise ValueError
        judge_state_root = fixture_root.parents[2]
        canonical_fixture_root = (
            judge_state_root / "fixtures" / "by-hash" / descriptor_digest
        )
        descriptor_bytes = _io_path(fixture_root / "fixture.json").read_bytes()
        descriptor = FixtureDescriptor.model_validate_json(descriptor_bytes)
        valid = (
            opaque_root == expected_root
            and _resolved_without_link(judge_state_root)
            and fixture_root.resolve(strict=True)
            == canonical_fixture_root.resolve(strict=True)
            and sha256(descriptor_bytes).hexdigest() == descriptor_digest
            and _resolved_without_link(source._physical_root)
            and source._physical_root.resolve(strict=True)
            == (fixture_root / "action_log").resolve(strict=True)
            and handoff.snapshot_root.resolve(strict=True)
            == expected_snapshot.resolve(strict=True)
            and _fixture_is_valid(fixture_root, descriptor, descriptor_digest)
        )
    except (
        IndexError,
        OSError,
        RuntimeError,
        ValidationError,
        StageCError,
        ValueError,
        pa.ArrowException,
    ):
        valid = False
    if not valid:
        raise _fixture_error(
            StageCErrorCode.JUDGE_HANDOFF_INVALID,
            "fixture_source_provenance",
        ) from None
    return judge_state_root


def _receipt_from_complete_fixture(
    target: Path,
    descriptor_digest: str,
    *,
    reused: bool,
) -> LocalEvaluationFixtureReceipt:
    snapshot_root = _single_snapshot_root(target)
    try:
        integrity = _FixtureIntegrityReceipt.model_validate_json(
            (target / "_SUCCESS").read_bytes()
        )
    except (OSError, ValidationError):
        raise _fixture_error(
            StageCErrorCode.FIXTURE_STATE_CONFLICT,
            "fixture_marker_read",
        ) from None
    handoff = _validated_judge_handoff(
        snapshot_root, expected_fingerprint=str(integrity.snapshot_fingerprint)
    )
    return LocalEvaluationFixtureReceipt(
        fixture_root=target,
        descriptor_path=target / "fixture.json",
        descriptor_sha256=descriptor_digest,
        action_log_partitions=_manifest_partitions(snapshot_root),
        judge=handoff,
        reused=reused,
    )


def _single_snapshot_root(root: Path) -> Path:
    snapshot_parent = root / "evaluation-snapshots" / "by-hash"
    roots = tuple(
        snapshot_parent / path.name
        for path in _io_path(snapshot_parent).iterdir()
        if path.is_dir()
    )
    if len(roots) != 1:
        raise _fixture_error(StageCErrorCode.JUDGE_HANDOFF_INVALID, "judge_snapshot_lookup")
    return roots[0]


def _manifest_partitions(snapshot_root: Path) -> tuple[SourcePartitionReceipt, ...]:
    try:
        manifest = EvaluationSnapshotManifest.model_validate_json(
            _io_path(snapshot_root / "manifest.json").read_bytes()
        )
    except (OSError, ValidationError):
        raise _fixture_error(
            StageCErrorCode.JUDGE_HANDOFF_INVALID,
            "judge_manifest_read",
        ) from None
    return manifest.source.partitions


def _snapshot_artifacts(snapshot_root: Path) -> tuple[ArtifactReceipt, ...]:
    try:
        manifest = EvaluationSnapshotManifest.model_validate_json(
            _io_path(snapshot_root / "manifest.json").read_bytes()
        )
    except (OSError, ValidationError):
        raise _fixture_error(
            StageCErrorCode.JUDGE_HANDOFF_INVALID,
            "judge_manifest_read",
        ) from None
    return (
        manifest.validation.artifacts.slate,
        manifest.validation.artifacts.labels,
        manifest.final_holdout.artifacts.slate,
        manifest.final_holdout.artifacts.labels,
    )


def _integrity_receipt(
    descriptor_digest: str,
    action_receipts: tuple[SourcePartitionReceipt, ...],
    handoff: JudgeSnapshotHandoff,
) -> _FixtureIntegrityReceipt:
    return _FixtureIntegrityReceipt(
        contract_version="local-fixture-integrity-v1",
        descriptor_sha256=descriptor_digest,
        action_log_partitions=action_receipts,
        snapshot_fingerprint=handoff.snapshot_fingerprint,
        manifest_sha256=handoff.manifest_sha256,
        validation_id=handoff.validation_id,
        final_holdout_id=handoff.final_holdout_id,
        snapshot_artifacts=_snapshot_artifacts(handoff.snapshot_root),
    )


def _fixture_tree_is_exact(
    root: Path,
    descriptor: FixtureDescriptor,
    handoff: JudgeSnapshotHandoff,
) -> bool:
    files = {
        "fixture.json",
        "_SUCCESS",
        descriptor.virtual_users.relative_path,
        *(receipt.relative_path for receipt in descriptor.youtube_partitions),
        *(
            f"action_log/dt={partition_date.isoformat()}/part-0.parquet"
            for partition_date in canonical_fixture_dates(descriptor.evaluation_start_date)
        ),
        *(
            f"evaluation-snapshots/by-hash/{handoff.snapshot_fingerprint}/{path}"
            for path in _SNAPSHOT_FILES
        ),
    }
    directories = {
        "inputs",
        "inputs/youtube_trending_kr",
        "action_log",
        "evaluation-snapshots",
        "evaluation-snapshots/by-hash",
        *(f"inputs/youtube_trending_kr/dt={receipt.dt.isoformat()}" for receipt in descriptor.youtube_partitions),
        *(f"action_log/dt={partition_date.isoformat()}" for partition_date in canonical_fixture_dates(descriptor.evaluation_start_date)),
        f"evaluation-snapshots/by-hash/{handoff.snapshot_fingerprint}",
        *(
            f"evaluation-snapshots/by-hash/{handoff.snapshot_fingerprint}/{path}"
            for path in _SNAPSHOT_DIRS
        ),
    }
    # Registry의 고정 root는 이 fixture의 snapshot root 상위다. 소비 상태는 원본
    # content identity에 포함하지 않지만, 허용 이름과 파일 종류는 계속 제한한다.
    registry_name = "final-holdout-consumed"
    registry = _io_path(root / registry_name)
    if os.path.lexists(registry):
        directories.add(registry_name)
        marker_name = str(handoff.final_holdout_id)
        if os.path.lexists(registry / marker_name):
            files.add(f"{registry_name}/{marker_name}")
    return _tree_is_exact(root, frozenset(files), frozenset(directories))


def _tree_is_exact(
    root: Path,
    expected_files: frozenset[str],
    expected_directories: frozenset[str],
) -> bool:
    try:
        io_root = _io_path(root)
        actual_files: set[str] = set()
        actual_directories: set[str] = set()
        for path in io_root.rglob("*"):
            entry_stat = path.lstat()
            relative_path = path.relative_to(io_root).as_posix()
            if stat.S_ISREG(entry_stat.st_mode):
                actual_files.add(relative_path)
            elif stat.S_ISDIR(entry_stat.st_mode):
                actual_directories.add(relative_path)
            else:
                return False
    except OSError:
        return False
    return actual_files == expected_files and actual_directories == expected_directories


def _require_safe_state_root(root: Path) -> None:
    if not root.is_absolute() or not _resolved_without_link(root) or not root.is_dir():
        raise _fixture_error(
            StageCErrorCode.FIXTURE_REQUEST_INVALID,
            "fixture_request_validation",
        )


def _prepare_fixture_output_root(state_root: Path) -> Path:
    root_resolved = state_root.resolve(strict=True)
    current = state_root
    for component in _FIXTURES_PATH.parts:
        current = current / component
        try:
            current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir()
            except FileExistsError:
                pass
            except OSError:
                raise _fixture_error(
                    StageCErrorCode.FIXTURE_REQUEST_INVALID,
                    "fixture_root_prepare",
                ) from None
        except OSError:
            raise _fixture_error(
                StageCErrorCode.FIXTURE_REQUEST_INVALID,
                "fixture_root_prepare",
            ) from None
        if not _safe_derived_directory(current, root_resolved):
            raise _fixture_error(
                StageCErrorCode.FIXTURE_REQUEST_INVALID,
                "fixture_root_validation",
            )
    return current


def _safe_derived_directory(path: Path, root_resolved: Path) -> bool:
    if not _resolved_without_link(path):
        return False
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return path.is_dir() and resolved.is_relative_to(root_resolved)


def _prepare_descriptor_lock(lock_path: Path) -> tuple[int, int]:
    created = False
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        if _safe_regular_file_identity(lock_path) is None:
            raise _fixture_error(
                StageCErrorCode.FIXTURE_STATE_CONFLICT,
                "fixture_lock_validation",
            ) from None
    except OSError:
        raise _fixture_error(
            StageCErrorCode.FIXTURE_STATE_CONFLICT,
            "fixture_lock_prepare",
        ) from None
    else:
        created = True
        try:
            os.write(descriptor, b"\0")
        except OSError:
            raise _fixture_error(
                StageCErrorCode.FIXTURE_STATE_CONFLICT,
                "fixture_lock_prepare",
            ) from None
        finally:
            os.close(descriptor)
    try:
        if not created and lock_path.stat().st_size == 0:
            with lock_path.open("r+b") as lock_file:
                lock_file.write(b"\0")
                lock_file.flush()
    except OSError:
        raise _fixture_error(
            StageCErrorCode.FIXTURE_STATE_CONFLICT,
            "fixture_lock_prepare",
        ) from None
    identity = _safe_regular_file_identity(lock_path)
    if identity is None:
        raise _fixture_error(
            StageCErrorCode.FIXTURE_STATE_CONFLICT,
            "fixture_lock_validation",
        )
    return identity


def _acquire_descriptor_lock(lock_file: BinaryIO) -> None:
    """Acquire the descriptor lock without truncating its locked byte range."""

    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        while True:
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if error.winerror not in {33, 36} and error.errno not in {
                    errno.EACCES,
                    errno.EDEADLK,
                }:
                    raise
                sleep(0)
            else:
                return
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _release_descriptor_lock(lock_file: BinaryIO) -> None:
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _open_lock_matches(
    lock_path: Path,
    descriptor: int,
    expected_identity: tuple[int, int],
) -> bool:
    try:
        opened = os.fstat(descriptor)
    except OSError:
        return False
    current_identity = _safe_regular_file_identity(lock_path)
    return (
        current_identity == expected_identity
        and (opened.st_dev, opened.st_ino) == expected_identity
        and stat.S_ISREG(opened.st_mode)
        and opened.st_nlink == 1
    )


def _safe_regular_file_identity(path: Path) -> tuple[int, int] | None:
    try:
        file_stat = path.lstat()
    except OSError:
        return None
    reparse = getattr(file_stat, "st_file_attributes", 0) & 0x400
    if (
        reparse
        or not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
        or not _resolved_without_link(path)
    ):
        return None
    return (file_stat.st_dev, file_stat.st_ino)


def _safe_tree(root: Path) -> bool:
    io_root = _io_path(root)
    if not _resolved_without_link(io_root) or not io_root.is_dir():
        return False
    try:
        for path in io_root.rglob("*"):
            if not _resolved_without_link(path):
                return False
            entry_stat = path.lstat()
            if stat.S_ISREG(entry_stat.st_mode):
                if entry_stat.st_nlink != 1:
                    return False
            elif not stat.S_ISDIR(entry_stat.st_mode):
                return False
    except OSError:
        return False
    return True


def _resolved_without_link(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        absolute = path.absolute()
        stat_result = path.lstat()
    except (OSError, RuntimeError):
        return False
    reparse = getattr(stat_result, "st_file_attributes", 0) & 0x400
    return not reparse and os.path.normcase(resolved) == os.path.normcase(absolute)


def _fixture_error(code: StageCErrorCode, stage: str) -> StageCError:
    return StageCError(code=code, stage=stage)


def _io_path(path: Path) -> Path:
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        return Path(f"\\\\?\\{path.absolute()}")
    return path
