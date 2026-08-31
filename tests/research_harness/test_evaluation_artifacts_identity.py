from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa

from autoresearch.research_harness.evaluation_artifacts import (
    WRITER_OPTIONS,
    calculate_evaluation_id,
)
from autoresearch.research_harness.evaluation_snapshot_models import (
    AttributedImpression,
    EvaluationSnapshotRequest,
    EvaluationSplit,
    EvaluationWindow,
    SnapshotArtifactInput,
    WriterIdentity,
)
from autoresearch.research_harness.evaluation_source_models import SourcePartitionReceipt


def _vector_receipt(day: date, rows: int, digest: str) -> SourcePartitionReceipt:
    return SourcePartitionReceipt(
        dt=day,
        uri=f"memory://research-harness-vector/dt={day.isoformat()}/part-0.parquet",
        rows=rows,
        sha256=digest,
    )


def _vector_input() -> SnapshotArtifactInput:
    receipts = (
        _vector_receipt(date(2026, 8, 30), 2, "f42aeca04305f5654582dd541ef0d56d832bd3cea2e1da0b8274fb88fcf34bf8"),
        _vector_receipt(date(2026, 8, 31), 3, "3f7b574ee4fde5dfd56a206317e6959311f8577667dfe07614d94d80e4ce7573"),
        _vector_receipt(date(2026, 9, 1), 1, "e930108a7a48791a5891486b4419eb2186b84a45b01a1cde3081514ec99e3420"),
        _vector_receipt(date(2026, 9, 2), 0, "663fc92e06df292c05a44a4bf5c86d2b4429b03edbe2f11cef21da9cfe6ea5d0"),
        _vector_receipt(date(2026, 9, 3), 1, "aa40602ef43f323ba14e1b52265d8f2bc9ea3d624ce0a0a650045c5006de282b"),
    )
    request = EvaluationSnapshotRequest(
        action_log_root="memory://research-harness-vector",
        history_start_date=date(2026, 8, 30),
        evaluation_start_date=date(2026, 9, 1),
        evaluation_end_date=date(2026, 9, 2),
        slate_id_cutover_date=date(2026, 8, 30),
        output_root=Path("unused"),
    )
    row = AttributedImpression(
        slate_id="slt_20260901_0123456789abcdef01234567",
        user_id="user-01",
        video_id="video-A",
        event_timestamp=datetime(2026, 9, 1, tzinfo=UTC),
        source_event_id="evt_20260901_00000001",
        clicked=False,
        original_rank=1,
        candidate_source="model",
    )
    split = EvaluationSplit(name="validation", rows=(row,), user_ids=("user-01",))
    return SnapshotArtifactInput(
        request=request,
        window=EvaluationWindow(
            history_start_date=date(2026, 8, 30),
            evaluation_start_date=date(2026, 9, 1),
            evaluation_end_date=date(2026, 9, 2),
            label_scan_end_date=date(2026, 9, 3),
            complete_history_label_end_date=date(2026, 8, 30),
            candidate_history_partitions=receipts[:2],
        ),
        partitions=receipts,
        validation=split,
        final_holdout=split,
        created_at=datetime(2026, 9, 3, tzinfo=UTC),
    )


def test_task_zero_literal_evaluation_id_vector() -> None:
    # Given
    writer = WriterIdentity(engine="pyarrow", version="21.0.0", options=WRITER_OPTIONS)

    # When
    evaluation_id = calculate_evaluation_id(
        _vector_input(), _vector_input().validation, writer
    )

    # Then
    assert evaluation_id == "eval_dafb0e95e3595ada1da4ddbe0b75a076b173ffffc5b9e0c53021fc659df49d8d"


def test_writer_version_changes_evaluation_identity() -> None:
    # Given
    artifact_input = _vector_input()
    locked = WriterIdentity(engine="pyarrow", version=pa.__version__, options=WRITER_OPTIONS)
    changed = WriterIdentity(engine="pyarrow", version=f"{pa.__version__}-changed", options=WRITER_OPTIONS)

    # When
    locked_id = calculate_evaluation_id(artifact_input, artifact_input.validation, locked)
    changed_id = calculate_evaluation_id(artifact_input, artifact_input.validation, changed)

    # Then
    assert locked_id != changed_id


def test_writer_options_change_evaluation_identity() -> None:
    # Given
    artifact_input = _vector_input()
    locked = WriterIdentity(engine="pyarrow", version=pa.__version__, options=WRITER_OPTIONS)
    changed = WriterIdentity(
        engine="pyarrow",
        version=pa.__version__,
        options=replace(WRITER_OPTIONS, row_group_size=1),
    )

    # When
    locked_id = calculate_evaluation_id(artifact_input, artifact_input.validation, locked)
    changed_id = calculate_evaluation_id(artifact_input, artifact_input.validation, changed)

    # Then
    assert locked_id != changed_id
