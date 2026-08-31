from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.research_harness.evaluation_artifacts import write_snapshot_artifacts
from autoresearch.research_harness.evaluation_snapshot_models import (
    AttributedImpression,
    EvaluationSnapshotRequest,
    EvaluationSnapshotManifest,
    EvaluationSplit,
    EvaluationWindow,
    SnapshotArtifactInput,
)
from autoresearch.research_harness.evaluation_source_models import SourcePartitionReceipt
from autoresearch.research_harness.evaluation_split import SPLIT_CONTRACT


def _receipt(day: date, rows: int) -> SourcePartitionReceipt:
    return SourcePartitionReceipt(
        dt=day,
        uri=f"memory://fixture/dt={day.isoformat()}/part-0.parquet",
        rows=rows,
        sha256=f"{day.day:064x}",
    )


def _row(
    *, user_id: str, slate_id: str, video_id: str, clicked: bool
) -> AttributedImpression:
    return AttributedImpression(
        slate_id=slate_id,
        user_id=user_id,
        video_id=video_id,
        event_timestamp=datetime(2026, 9, 1, 0, int(video_id[-1]), tzinfo=UTC),
        source_event_id=f"evt_20260901_0000000{video_id[-1]}",
        clicked=clicked,
        original_rank=int(video_id[-1]),
        candidate_source="model",
    )


def _artifact_input(created_at: datetime | None = None) -> SnapshotArtifactInput:
    partitions = tuple(
        _receipt(day, index)
        for index, day in enumerate(
            (date(2026, 8, 30), date(2026, 8, 31), date(2026, 9, 1)),
            start=1,
        )
    )
    request = EvaluationSnapshotRequest(
        action_log_root="memory://fixture",
        history_start_date=date(2026, 8, 30),
        evaluation_start_date=date(2026, 9, 1),
        evaluation_end_date=date(2026, 9, 1),
        slate_id_cutover_date=date(2026, 8, 30),
        output_root=Path("unused"),
    )
    window = EvaluationWindow(
        history_start_date=request.history_start_date,
        evaluation_start_date=request.evaluation_start_date,
        evaluation_end_date=request.evaluation_end_date,
        label_scan_end_date=date(2026, 9, 2),
        complete_history_label_end_date=date(2026, 8, 30),
        candidate_history_partitions=partitions[:2],
    )
    validation_rows = (
        _row(user_id="validation-user", slate_id="validation-slate", video_id="v1", clicked=True),
        _row(user_id="validation-user", slate_id="validation-slate", video_id="v2", clicked=False),
    )
    final_rows = (
        _row(user_id="final-user", slate_id="final-slate", video_id="v1", clicked=True),
        _row(user_id="final-user", slate_id="final-slate", video_id="v2", clicked=False),
    )
    return SnapshotArtifactInput(
        request=request,
        window=window,
        partitions=partitions,
        validation=EvaluationSplit(
            name="validation", rows=validation_rows, user_ids=("validation-user",)
        ),
        final_holdout=EvaluationSplit(
            name="final_holdout", rows=final_rows, user_ids=("final-user",)
        ),
        created_at=created_at or datetime(2026, 9, 2, tzinfo=UTC),
    )


def test_exact_arrow_schema_and_nullability_when_artifacts_are_written(
    tmp_path: Path,
) -> None:
    # Given
    expected_slate = pa.schema(
        [
            pa.field("evaluation_id", pa.string(), nullable=False),
            pa.field("slate_id", pa.string(), nullable=False),
            pa.field("user_id", pa.string(), nullable=False),
            pa.field("video_id", pa.string(), nullable=False),
            pa.field("event_timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
            pa.field("candidate_source", pa.string(), nullable=True),
            pa.field("original_rank", pa.int64(), nullable=True),
        ]
    )
    expected_labels = pa.schema(
        [
            pa.field("evaluation_id", pa.string(), nullable=False),
            pa.field("slate_id", pa.string(), nullable=False),
            pa.field("user_id", pa.string(), nullable=False),
            pa.field("video_id", pa.string(), nullable=False),
            pa.field("source_event_id", pa.string(), nullable=False),
            pa.field("clicked", pa.bool_(), nullable=False),
        ]
    )

    # When
    write_snapshot_artifacts(tmp_path, _artifact_input())

    # Then
    assert (
        pq.read_schema(tmp_path / "validation" / "slate.parquet"),
        pq.read_schema(tmp_path / "validation" / "labels.parquet"),
        pq.read_schema(tmp_path / "final_holdout" / "slate.parquet"),
        pq.read_schema(tmp_path / "final_holdout" / "labels.parquet"),
    ) == (expected_slate, expected_labels, expected_slate, expected_labels)


def test_labels_are_sealed_out_of_slate_when_artifacts_are_written(tmp_path: Path) -> None:
    # Given
    expected_columns = [
        "evaluation_id", "slate_id", "user_id", "video_id", "event_timestamp",
        "candidate_source", "original_rank",
    ]

    # When
    write_snapshot_artifacts(tmp_path, _artifact_input())

    # Then
    assert pq.read_table(tmp_path / "validation" / "slate.parquet").column_names == expected_columns


def test_labels_join_one_to_one_in_slate_order_when_input_is_unsorted(tmp_path: Path) -> None:
    # Given
    artifact_input = _artifact_input()
    reversed_validation = replace(
        artifact_input.validation, rows=tuple(reversed(artifact_input.validation.rows))
    )

    # When
    write_snapshot_artifacts(tmp_path, replace(artifact_input, validation=reversed_validation))

    # Then
    slate = pq.read_table(tmp_path / "validation" / "slate.parquet").to_pylist()
    labels = pq.read_table(tmp_path / "validation" / "labels.parquet").to_pylist()
    keys = ("evaluation_id", "slate_id", "user_id", "video_id")
    slate_keys = [tuple(row[key] for key in keys) for row in slate]
    label_keys = [tuple(row[key] for key in keys) for row in labels]
    assert (slate_keys, label_keys, [row["video_id"] for row in slate]) == (
        slate_keys, slate_keys, ["v1", "v2"]
    )


def test_validation_and_final_ids_differ_when_split_identity_differs(tmp_path: Path) -> None:
    # Given
    artifact_input = _artifact_input()
    same_rows_final = replace(
        artifact_input.final_holdout,
        rows=artifact_input.validation.rows,
        user_ids=artifact_input.validation.user_ids,
    )

    # When
    write_snapshot_artifacts(tmp_path, replace(artifact_input, final_holdout=same_rows_final))
    manifest = EvaluationSnapshotManifest.model_validate_json(
        (tmp_path / "manifest.json").read_bytes()
    )

    # Then
    assert manifest.validation.evaluation_id != manifest.final_holdout.evaluation_id


def test_permutation_preserves_ids_bytes_and_fingerprint(tmp_path: Path) -> None:
    # Given
    artifact_input = _artifact_input()
    permuted = replace(
        artifact_input,
        partitions=tuple(reversed(artifact_input.partitions)),
        window=replace(
            artifact_input.window,
            candidate_history_partitions=tuple(
                reversed(artifact_input.window.candidate_history_partitions)
            ),
        ),
        validation=replace(
            artifact_input.validation, rows=tuple(reversed(artifact_input.validation.rows))
        ),
    )

    # When
    first = write_snapshot_artifacts(tmp_path / "first", artifact_input)
    second = write_snapshot_artifacts(tmp_path / "second", permuted)

    # Then
    paths = ("validation/slate.parquet", "validation/labels.parquet",
             "final_holdout/slate.parquet", "final_holdout/labels.parquet")
    assert (
        first.validation.evaluation_id,
        first.final_holdout.evaluation_id,
        first.snapshot_fingerprint,
        tuple((tmp_path / "first" / path).read_bytes() for path in paths),
    ) == (
        second.validation.evaluation_id,
        second.final_holdout.evaluation_id,
        second.snapshot_fingerprint,
        tuple((tmp_path / "second" / path).read_bytes() for path in paths),
    )


def test_created_at_is_excluded_from_ids_fingerprint_and_artifact_bytes(tmp_path: Path) -> None:
    # Given
    first_input = _artifact_input(datetime(2026, 9, 2, tzinfo=UTC))
    second_input = replace(first_input, created_at=datetime(2026, 9, 3, tzinfo=UTC))

    # When
    first = write_snapshot_artifacts(tmp_path / "first", first_input)
    second = write_snapshot_artifacts(tmp_path / "second", second_input)

    # Then
    assert (
        first.validation.evaluation_id,
        first.final_holdout.evaluation_id,
        first.snapshot_fingerprint,
        first.validation.artifacts,
        first.final_holdout.artifacts,
    ) == (
        second.validation.evaluation_id,
        second.final_holdout.evaluation_id,
        second.snapshot_fingerprint,
        second.validation.artifacts,
        second.final_holdout.artifacts,
    )


def test_manifest_contains_exact_nested_source_window_and_summary(tmp_path: Path) -> None:
    # Given
    artifact_input = _artifact_input()

    # When
    manifest = write_snapshot_artifacts(tmp_path, artifact_input)

    # Then
    assert (
        manifest.source.root,
        manifest.source.partitions,
        manifest.window,
        manifest.split,
        manifest.validation.counts.row_count,
        manifest.validation.optional_non_null_ratio.model_dump(),
    ) == (
        artifact_input.request.action_log_root,
        artifact_input.partitions,
        artifact_input.window,
        SPLIT_CONTRACT,
        2,
        {"candidate_source": 1.0, "original_rank": 1.0},
    )


def test_artifact_receipts_use_raw_file_sha256_and_row_counts(tmp_path: Path) -> None:
    # Given
    artifact_input = _artifact_input()

    # When
    manifest = write_snapshot_artifacts(tmp_path, artifact_input)

    # Then
    receipt = manifest.validation.artifacts.slate
    assert (receipt.rows, receipt.sha256) == (
        2,
        sha256((tmp_path / receipt.relative_path).read_bytes()).hexdigest(),
    )


def test_candidate_history_stops_at_t_minus_two_and_retains_receipts(tmp_path: Path) -> None:
    # Given
    artifact_input = _artifact_input()

    # When
    manifest = write_snapshot_artifacts(tmp_path, artifact_input)

    # Then
    assert (
        manifest.window.complete_history_label_end_date,
        manifest.window.candidate_history_partitions,
    ) == (date(2026, 8, 30), artifact_input.partitions[:2])


def test_writer_runtime_and_locked_options_are_manifest_identity(tmp_path: Path) -> None:
    # Given
    expected_options = {
        "version": "2.6", "coerce_timestamps": "us",
        "allow_truncated_timestamps": False,
        "use_deprecated_int96_timestamps": False, "compression": "NONE",
        "use_dictionary": False, "row_group_size": 50000,
        "write_statistics": True, "data_page_version": "1.0", "store_schema": True,
    }

    # When
    manifest = write_snapshot_artifacts(tmp_path, _artifact_input())

    # Then
    assert (manifest.writer.engine, manifest.writer.version, asdict(manifest.writer.options)) == (
        "pyarrow", pa.__version__, expected_options,
    )
