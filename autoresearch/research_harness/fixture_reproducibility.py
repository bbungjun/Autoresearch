"""Stage C 독립 local fixture 재생성의 내부 검증 경계.

[파이프라인] Judge 소유 fixture 두 개가 production 일일 action log와 Stage B snapshot을
각각 게시한 뒤, candidate data view를 만들기 전에 재현성 증거를 한 번에 대조한다.

[기능] descriptor·입력·canonical source·slate projection·evaluation identity·snapshot
artifact를 typed evidence로 재검증하고 불일치를 sanitized Stage C 오류로 정규화한다.

[비책임] fixture 생성, 동일 target 게시 재사용, candidate view materialization과 P0-2
metric/final consumption은 각각 기존 Stage C 외부 seam과 후속 모듈이 담당한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes
from autoresearch.research_harness.evaluation_snapshot_models import (
    EvaluationSnapshotManifest,
)
from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode
from autoresearch.research_harness.fixture_models import (
    FixtureDescriptor,
    LocalEvaluationFixtureReceipt,
)
from autoresearch.research_harness.local_evaluation_fixture import _io_path


@dataclass(frozen=True, slots=True)
class _FixtureReproductionEvidence:
    descriptor_sha256: str
    input_receipts: tuple[tuple[str, int, str], ...]
    source_root: str
    source_partitions: tuple[tuple[str, int, str, str], ...]
    validation_slate_projection_sha256: str
    final_slate_projection_sha256: str
    evaluation_ids: tuple[str, str]
    snapshot_artifacts: tuple[tuple[str, int, str], ...]
    manifest_sha256: str
    snapshot_fingerprint: str


def _verify_independent_fixture_reproduction(
    first: LocalEvaluationFixtureReceipt,
    second: LocalEvaluationFixtureReceipt,
) -> None:
    """Require two first-build receipts to contain identical canonical evidence."""

    try:
        if first.reused or second.reused:
            raise ValueError
        first_root = first.fixture_root.resolve(strict=True)
        second_root = second.fixture_root.resolve(strict=True)
        if (
            not first_root.is_absolute()
            or not second_root.is_absolute()
            or first_root == second_root
        ):
            raise ValueError
        if _collect_evidence(first) != _collect_evidence(second):
            raise ValueError
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        ValidationError,
        StageCError,
        pa.ArrowException,
    ):
        raise StageCError(
            StageCErrorCode.FIXTURE_REPRODUCIBILITY_MISMATCH,
            "fixture_reproducibility_validation",
        ) from None


def _collect_evidence(
    receipt: LocalEvaluationFixtureReceipt,
) -> _FixtureReproductionEvidence:
    descriptor_path = _io_path(receipt.descriptor_path)
    descriptor_bytes = descriptor_path.read_bytes()
    descriptor = FixtureDescriptor.model_validate_json(descriptor_bytes)
    if (
        receipt.descriptor_path != receipt.fixture_root / "fixture.json"
        or sha256(descriptor_bytes).hexdigest() != receipt.descriptor_sha256
    ):
        raise ValueError

    input_receipts = (descriptor.virtual_users, *descriptor.youtube_partitions)
    for item in input_receipts:
        path = _io_path(receipt.fixture_root.joinpath(*item.relative_path.split("/")))
        if (
            sha256(path.read_bytes()).hexdigest() != item.sha256
            or pq.read_metadata(path).num_rows != item.rows
        ):
            raise ValueError

    action_evidence: list[tuple[str, int, str, str]] = []
    for item in receipt.action_log_partitions:
        expected_uri = (
            f"fixture://{receipt.descriptor_sha256}/action-log/"
            f"dt={item.dt.isoformat()}/part-0.parquet"
        )
        path = _io_path(
            receipt.fixture_root
            / "action_log"
            / f"dt={item.dt.isoformat()}"
            / "part-0.parquet"
        )
        if (
            item.uri != expected_uri
            or sha256(path.read_bytes()).hexdigest() != item.sha256
            or pq.read_metadata(path).num_rows != item.rows
        ):
            raise ValueError
        action_evidence.append((item.dt.isoformat(), item.rows, item.sha256, item.uri))

    manifest_path = _io_path(receipt.judge.snapshot_root / "manifest.json")
    manifest_bytes = manifest_path.read_bytes()
    manifest = EvaluationSnapshotManifest.model_validate_json(manifest_bytes)
    expected_source_root = f"fixture://{receipt.descriptor_sha256}/action-log"
    if (
        sha256(manifest_bytes).hexdigest() != receipt.judge.manifest_sha256
        or manifest.snapshot_fingerprint != receipt.judge.snapshot_fingerprint
        or manifest.validation.evaluation_id != receipt.judge.validation_id
        or manifest.final_holdout.evaluation_id != receipt.judge.final_holdout_id
        or manifest.source.root != expected_source_root
        or manifest.source.partitions != receipt.action_log_partitions
    ):
        raise ValueError

    artifacts = (
        manifest.validation.artifacts.slate,
        manifest.validation.artifacts.labels,
        manifest.final_holdout.artifacts.slate,
        manifest.final_holdout.artifacts.labels,
    )
    for artifact in artifacts:
        path = _io_path(
            receipt.judge.snapshot_root.joinpath(*artifact.relative_path.split("/"))
        )
        if (
            sha256(path.read_bytes()).hexdigest() != artifact.sha256
            or pq.read_metadata(path).num_rows != artifact.rows
        ):
            raise ValueError

    return _FixtureReproductionEvidence(
        descriptor_sha256=receipt.descriptor_sha256,
        input_receipts=tuple(
            (item.relative_path, item.rows, item.sha256) for item in input_receipts
        ),
        source_root=manifest.source.root,
        source_partitions=tuple(action_evidence),
        validation_slate_projection_sha256=_slate_projection_sha256(
            receipt.judge.snapshot_root,
            manifest.validation.artifacts.slate.relative_path,
        ),
        final_slate_projection_sha256=_slate_projection_sha256(
            receipt.judge.snapshot_root,
            manifest.final_holdout.artifacts.slate.relative_path,
        ),
        evaluation_ids=(
            str(manifest.validation.evaluation_id),
            str(manifest.final_holdout.evaluation_id),
        ),
        snapshot_artifacts=tuple(
            (artifact.relative_path, artifact.rows, artifact.sha256)
            for artifact in artifacts
        ),
        manifest_sha256=receipt.judge.manifest_sha256,
        snapshot_fingerprint=str(receipt.judge.snapshot_fingerprint),
    )


def _slate_projection_sha256(snapshot_root: Path, relative_path: str) -> str:
    slate_ids = pq.read_table(
        _io_path(snapshot_root.joinpath(*relative_path.split("/"))),
        columns=["slate_id"],
    ).column("slate_id").to_pylist()
    if any(not isinstance(value, str) or not value for value in slate_ids):
        raise ValueError
    projection = tuple(sorted(set(slate_ids)))
    return sha256(canonical_json_bytes(projection)).hexdigest()
