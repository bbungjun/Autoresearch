from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from autoresearch.research_harness import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
    build_evaluation_snapshot,
)
from tests.research_harness.test_slate import _snapshot_request


def _evaluation_path(request_root: str) -> Path:
    return Path(request_root) / "dt=2026-09-01" / "part-0.parquet"


def _assert_pre_staging_error(
    captured: pytest.ExceptionInfo[EvaluationSnapshotError],
    expected_code: SnapshotErrorCode,
    output_root: Path,
) -> None:
    assert captured.value.code is expected_code
    assert "user-" not in str(captured.value)
    assert "action-log" not in str(captured.value)
    assert not output_root.exists()


def test_source_error_propagates_sanitized_before_staging(tmp_path: Path) -> None:
    # Given
    request = _snapshot_request(tmp_path)
    (Path(request.action_log_root) / "dt=2026-09-02" / "part-0.parquet").unlink()

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        build_evaluation_snapshot(request)

    # Then
    _assert_pre_staging_error(
        captured,
        SnapshotErrorCode.SOURCE_PARTITION_MISSING,
        request.output_root,
    )


def test_slate_error_propagates_sanitized_before_staging(tmp_path: Path) -> None:
    # Given
    request = _snapshot_request(tmp_path)
    path = _evaluation_path(request.action_log_root)
    table = pq.read_table(path)
    slate_index = table.schema.get_field_index("slate_id")
    slate_ids = pc.if_else(
        pc.equal(table.column("user_id"), "user-0"),
        pa.scalar("slt_20260901_000000000000000000000000"),
        table.column("slate_id"),
    )
    pq.write_table(table.set_column(slate_index, "slate_id", slate_ids), path)

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        build_evaluation_snapshot(request)

    # Then
    _assert_pre_staging_error(
        captured,
        SnapshotErrorCode.SLATE_ID_INVALID,
        request.output_root,
    )


def test_attribution_error_propagates_sanitized_before_staging(
    tmp_path: Path,
) -> None:
    # Given
    request = _snapshot_request(tmp_path)
    path = _evaluation_path(request.action_log_root)
    table = pq.read_table(path)
    slate_index = table.schema.get_field_index("slate_id")
    mismatched = pc.if_else(
        pc.equal(table.column("event_type"), "click"),
        pa.scalar("slt_20260901_000000000000000000000000"),
        table.column("slate_id"),
    )
    pq.write_table(table.set_column(slate_index, "slate_id", mismatched), path)

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        build_evaluation_snapshot(request)

    # Then
    _assert_pre_staging_error(
        captured,
        SnapshotErrorCode.SLATE_ATTRIBUTION_MISMATCH,
        request.output_root,
    )


def test_split_error_propagates_sanitized_before_staging(tmp_path: Path) -> None:
    # Given
    request = _snapshot_request(tmp_path)
    path = _evaluation_path(request.action_log_root)
    table = pq.read_table(path)
    pq.write_table(table.filter(pc.equal(table.column("event_type"), "impression")), path)

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        build_evaluation_snapshot(request)

    # Then
    _assert_pre_staging_error(
        captured,
        SnapshotErrorCode.SPLIT_COVERAGE_INSUFFICIENT,
        request.output_root,
    )


def test_publish_error_preserves_target_and_cleans_owned_staging(tmp_path: Path) -> None:
    # Given
    request = _snapshot_request(tmp_path)
    first = build_evaluation_snapshot(request)
    manifest_path = first.target_path / "manifest.json"
    original = manifest_path.read_bytes()
    manifest_path.write_bytes(original + b"tamper")

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        build_evaluation_snapshot(request)

    # Then
    snapshot_root = request.output_root / "evaluation-snapshots"
    assert captured.value.code is SnapshotErrorCode.SNAPSHOT_WRITE_CONFLICT
    assert manifest_path.read_bytes() == original + b"tamper"
    assert tuple(snapshot_root.glob(".staging-*")) == ()
