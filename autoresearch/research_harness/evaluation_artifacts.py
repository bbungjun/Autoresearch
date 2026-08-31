"""평가 split의 결정적 Parquet artifact와 manifest를 생성한다.

[파이프라인] user split 뒤와 write-once publisher 앞에서 slate/label artifact와
재현 가능한 evaluation identity 및 snapshot manifest를 조립한다.

[기능] label이 봉인된 네 Parquet 파일, artifact receipt, typed manifest를 생성한다.
[비책임] 원천 검증·귀속·split 계산과 게시·`_SUCCESS` 기록은 인접 모듈이 담당한다.
"""

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.research_harness.evaluation_snapshot_models import (
    ArtifactReceipt,
    AttributionContract,
    AttributedImpression,
    EvaluationId,
    EvaluationSplit,
    EvaluationSnapshotManifest,
    SnapshotArtifactInput,
    SnapshotFingerprint,
    SnapshotSource,
    SplitArtifacts,
    SplitSummary,
    WriterIdentity,
    WriterOptions,
)
from autoresearch.research_harness.evaluation_source_models import SourcePartitionReceipt
from autoresearch.research_harness.evaluation_split import SPLIT_CONTRACT, split_statistics


CONTRACT_VERSION: Final = "evaluation-slate-snapshot-v1"
ATTRIBUTION_CONTRACT: Final = AttributionContract(
    version="click-attribution-v1",
    lookback_seconds=1800,
    tie_break=("event_timestamp_desc", "source_event_id_desc"),
)
WRITER_OPTIONS: Final = WriterOptions(
    version="2.6", coerce_timestamps="us",
    allow_truncated_timestamps=False, use_deprecated_int96_timestamps=False,
    compression="NONE", use_dictionary=False,
    row_group_size=50000, write_statistics=True,
    data_page_version="1.0", store_schema=True,
)


def canonical_json_bytes(payload: dict[str, "CanonicalValue"]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


CanonicalScalar = str | int | float | bool | None
CanonicalValue = CanonicalScalar | list["CanonicalValue"] | dict[str, "CanonicalValue"]


def calculate_snapshot_fingerprint(manifest: EvaluationSnapshotManifest) -> SnapshotFingerprint:
    payload = manifest.model_dump(mode="json")
    del payload["snapshot_fingerprint"]
    del payload["created_at"]
    return SnapshotFingerprint(sha256(canonical_json_bytes(payload)).hexdigest())


def write_snapshot_artifacts(
    staging_dir: Path, artifact_input: SnapshotArtifactInput
) -> EvaluationSnapshotManifest:
    """Write deterministic split artifacts and return their typed manifest."""
    writer = WriterIdentity(
        engine="pyarrow",
        version=pa.__version__,
        options=WRITER_OPTIONS,
    )
    source = _snapshot_source(artifact_input)
    window = replace(
        artifact_input.window,
        candidate_history_partitions=tuple(
            sorted(
                artifact_input.window.candidate_history_partitions,
                key=lambda receipt: receipt.dt,
            )
        ),
    )
    validation = _write_split(staging_dir, artifact_input.validation, artifact_input)
    final_holdout = _write_split(staging_dir, artifact_input.final_holdout, artifact_input)
    manifest = EvaluationSnapshotManifest(
        contract_version=CONTRACT_VERSION, snapshot_fingerprint=SnapshotFingerprint(""),
        created_at=artifact_input.created_at, source=source, window=window,
        attribution=ATTRIBUTION_CONTRACT, split=SPLIT_CONTRACT, writer=writer,
        validation=validation, final_holdout=final_holdout,
    )
    manifest = manifest.model_copy(
        update={"snapshot_fingerprint": calculate_snapshot_fingerprint(manifest)}
    )
    (staging_dir / "manifest.json").write_bytes(
        canonical_json_bytes(manifest.model_dump(mode="json")))
    return manifest


def _write_split(
    staging_dir: Path, split: EvaluationSplit, artifact_input: SnapshotArtifactInput
) -> SplitSummary:
    rows = tuple(sorted(split.rows, key=_row_sort_key))
    writer = WriterIdentity(engine="pyarrow", version=pa.__version__, options=WRITER_OPTIONS)
    evaluation_id = calculate_evaluation_id(artifact_input, replace(split, rows=rows), writer)
    split_dir = staging_dir / split.name
    split_dir.mkdir(parents=True, exist_ok=True)
    slate_path = split_dir / "slate.parquet"
    labels_path = split_dir / "labels.parquet"
    _write_table(_slate_table(rows, evaluation_id), slate_path)
    _write_table(_labels_table(rows, evaluation_id), labels_path)
    counts, ratios = split_statistics(split)
    return SplitSummary(
        evaluation_id=evaluation_id, counts=counts, optional_non_null_ratio=ratios,
        artifacts=SplitArtifacts(
            slate=_artifact_receipt(staging_dir, slate_path, len(rows)),
            labels=_artifact_receipt(staging_dir, labels_path, len(rows)),
        ),
    )


def calculate_evaluation_id(
    artifact_input: SnapshotArtifactInput, split: EvaluationSplit, writer: WriterIdentity
) -> EvaluationId:
    """Calculate one split ID from its exact canonical payload."""
    rows = tuple(sorted(split.rows, key=_row_sort_key))
    source = _snapshot_source(artifact_input)
    payload: dict[str, CanonicalValue] = {
        "contract_version": CONTRACT_VERSION, "split_name": split.name,
        "source": _source_payload(source), "window": _window_payload(artifact_input),
        "attribution": {
            "version": ATTRIBUTION_CONTRACT.version,
            "lookback_seconds": ATTRIBUTION_CONTRACT.lookback_seconds,
            "tie_break": list(ATTRIBUTION_CONTRACT.tie_break),
        },
        "split": {
            "version": SPLIT_CONTRACT.version, "salt": SPLIT_CONTRACT.salt,
            "validation_buckets": list(SPLIT_CONTRACT.validation_buckets),
            "final_holdout_buckets": list(SPLIT_CONTRACT.final_holdout_buckets),
        },
        "writer": _writer_payload(writer),
        "slate_rows": [_slate_payload(row) for row in rows],
        "label_rows": [_label_payload(row) for row in rows],
    }
    return EvaluationId(f"eval_{sha256(canonical_json_bytes(payload)).hexdigest()}")


def _slate_table(rows: tuple[AttributedImpression, ...], evaluation_id: str) -> pa.Table:
    schema = pa.schema(
        [
            pa.field("evaluation_id", pa.string(), nullable=False), pa.field("slate_id", pa.string(), nullable=False),
            pa.field("user_id", pa.string(), nullable=False), pa.field("video_id", pa.string(), nullable=False),
            pa.field("event_timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("candidate_source", pa.string()),
            pa.field("original_rank", pa.int64()),
        ]
    )
    return pa.Table.from_pylist(
        [
            {
                "evaluation_id": evaluation_id,
                "slate_id": row.slate_id,
                "user_id": row.user_id,
                "video_id": row.video_id,
                "event_timestamp": row.event_timestamp,
                "candidate_source": row.candidate_source,
                "original_rank": row.original_rank,
            }
            for row in rows
        ],
        schema=schema,
    )


def _labels_table(rows: tuple[AttributedImpression, ...], evaluation_id: str) -> pa.Table:
    schema = pa.schema(
        [
            pa.field("evaluation_id", pa.string(), nullable=False), pa.field("slate_id", pa.string(), nullable=False),
            pa.field("user_id", pa.string(), nullable=False), pa.field("video_id", pa.string(), nullable=False),
            pa.field("source_event_id", pa.string(), nullable=False),
            pa.field("clicked", pa.bool_(), nullable=False),
        ]
    )
    return pa.Table.from_pylist(
        [{"evaluation_id": evaluation_id, **_label_payload(row)} for row in rows],
        schema=schema,
    )


def _write_table(table: pa.Table, path: Path) -> None:
    pq.write_table(
        table,
        path,
        version=WRITER_OPTIONS.version,
        coerce_timestamps=WRITER_OPTIONS.coerce_timestamps,
        allow_truncated_timestamps=WRITER_OPTIONS.allow_truncated_timestamps,
        use_deprecated_int96_timestamps=WRITER_OPTIONS.use_deprecated_int96_timestamps,
        compression=WRITER_OPTIONS.compression,
        use_dictionary=WRITER_OPTIONS.use_dictionary,
        row_group_size=WRITER_OPTIONS.row_group_size,
        write_statistics=WRITER_OPTIONS.write_statistics,
        data_page_version=WRITER_OPTIONS.data_page_version,
        store_schema=WRITER_OPTIONS.store_schema,
    )


def _artifact_receipt(root: Path, path: Path, rows: int) -> ArtifactReceipt:
    return ArtifactReceipt(
        relative_path=path.relative_to(root).as_posix(), rows=rows,
        sha256=sha256(path.read_bytes()).hexdigest(),
    )


def _source_payload(source: SnapshotSource) -> dict[str, CanonicalValue]:
    return {
        "root": source.root,
        "partitions": [_receipt_payload(receipt) for receipt in source.partitions],
        "slate_id_cutover_date": source.slate_id_cutover_date.isoformat(),
    }


def _snapshot_source(artifact_input: SnapshotArtifactInput) -> SnapshotSource:
    return SnapshotSource(
        root=artifact_input.request.action_log_root,
        partitions=tuple(sorted(artifact_input.partitions, key=lambda receipt: receipt.dt)),
        slate_id_cutover_date=artifact_input.request.slate_id_cutover_date,
    )


def _window_payload(artifact_input: SnapshotArtifactInput) -> dict[str, CanonicalValue]:
    window = artifact_input.window
    return {
        "history_start_date": window.history_start_date.isoformat(),
        "evaluation_start_date": window.evaluation_start_date.isoformat(),
        "evaluation_end_date": window.evaluation_end_date.isoformat(),
        "label_scan_end_date": window.label_scan_end_date.isoformat(),
        "complete_history_label_end_date": window.complete_history_label_end_date.isoformat(),
        "candidate_history_partitions": [
            _receipt_payload(receipt)
            for receipt in sorted(window.candidate_history_partitions, key=lambda item: item.dt)
        ],
    }


def _receipt_payload(receipt: SourcePartitionReceipt) -> dict[str, CanonicalValue]:
    return {
        "dt": receipt.dt.isoformat(), "uri": receipt.uri,
        "rows": receipt.rows, "sha256": receipt.sha256,
    }


def _writer_payload(writer: WriterIdentity) -> dict[str, CanonicalValue]:
    return {
        "engine": writer.engine, "version": writer.version,
        "options": {
            "version": writer.options.version, "coerce_timestamps": writer.options.coerce_timestamps,
            "allow_truncated_timestamps": writer.options.allow_truncated_timestamps,
            "use_deprecated_int96_timestamps": writer.options.use_deprecated_int96_timestamps,
            "compression": writer.options.compression, "use_dictionary": writer.options.use_dictionary,
            "row_group_size": writer.options.row_group_size, "write_statistics": writer.options.write_statistics,
            "data_page_version": writer.options.data_page_version, "store_schema": writer.options.store_schema,
        },
    }


def _slate_payload(row: AttributedImpression) -> dict[str, CanonicalValue]:
    return {
        "slate_id": row.slate_id, "user_id": row.user_id, "video_id": row.video_id,
        "event_timestamp": _utc_timestamp(row.event_timestamp),
        "original_rank": row.original_rank,
        "candidate_source": row.candidate_source,
    }


def _label_payload(row: AttributedImpression) -> dict[str, CanonicalValue]:
    return {
        "slate_id": row.slate_id, "user_id": row.user_id, "video_id": row.video_id,
        "source_event_id": row.source_event_id,
        "clicked": row.clicked,
    }


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row_sort_key(row: AttributedImpression) -> tuple[str, str, datetime, str]:
    return (row.user_id, row.slate_id, row.event_timestamp, row.video_id)
