from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa
import pytest

from autoresearch.action_log_generation.pipeline import EVENT_LOG_PARQUET_SCHEMA
from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.evaluation_source import load_required_partitions
from tests.research_harness.test_evaluation_source_fixtures import (
    event_for,
    request_for,
    table_for,
    write_table,
)


def _write_empty_neighbors(root: Path) -> None:
    empty = pa.Table.from_pylist([], schema=EVENT_LOG_PARQUET_SCHEMA)
    write_table(root, date(2026, 8, 31), empty)
    write_table(root, date(2026, 9, 2), empty)


def _assert_failure(tmp_path: Path, table: pa.Table, code: SnapshotErrorCode) -> None:
    root = tmp_path / "action-log"
    _write_empty_neighbors(root)
    write_table(root, date(2026, 9, 1), table)

    with pytest.raises(EvaluationSnapshotError) as captured:
        load_required_partitions(request_for(str(root), tmp_path / "output"))

    assert captured.value.code is code
    assert captured.value.dt == date(2026, 9, 1)
    assert "user-1" not in str(captured.value)


def test_load_rejects_required_column_absence(tmp_path: Path) -> None:
    table = table_for(
        (
            event_for(
                date(2026, 9, 1),
                slate_id="slt_20260901_0123456789abcdef01234567",
            ),
        )
    ).drop_columns(["user_id"])

    _assert_failure(tmp_path, table, SnapshotErrorCode.SOURCE_SCHEMA_INVALID)


def test_load_rejects_required_column_type_mismatch(tmp_path: Path) -> None:
    index = EVENT_LOG_PARQUET_SCHEMA.get_field_index("user_id")
    schema = EVENT_LOG_PARQUET_SCHEMA.set(index, pa.field("user_id", pa.int64()))
    arrays = [
        pa.array([1], type=field.type)
        if field.name == "user_id"
        else table_for(
            (
                event_for(
                    date(2026, 9, 1),
                    slate_id="slt_20260901_0123456789abcdef01234567",
                ),
            )
        ).column(field.name)
        for field in schema
    ]

    _assert_failure(tmp_path, pa.Table.from_arrays(arrays, schema=schema), SnapshotErrorCode.SOURCE_SCHEMA_INVALID)


def test_load_rejects_unknown_event_type(tmp_path: Path) -> None:
    table = table_for(
        (
            event_for(
                date(2026, 9, 1),
                slate_id="slt_20260901_0123456789abcdef01234567",
            ),
        )
    ).set_column(
        EVENT_LOG_PARQUET_SCHEMA.get_field_index("event_type"),
        "event_type",
        pa.array(["purchase"], type=pa.string()),
    )

    _assert_failure(tmp_path, table, SnapshotErrorCode.SOURCE_SCHEMA_INVALID)


@pytest.mark.parametrize(
    ("column", "value"),
    (("user_id", ""), ("video_id", ""), ("rank", 0), ("source", "untrusted")),
)
def test_load_rejects_event_field_domain(
    tmp_path: Path,
    column: str,
    value: str | int,
) -> None:
    field = EVENT_LOG_PARQUET_SCHEMA.field(column)
    table = table_for(
        (
            event_for(
                date(2026, 9, 1),
                slate_id="slt_20260901_0123456789abcdef01234567",
            ),
        )
    ).set_column(
        EVENT_LOG_PARQUET_SCHEMA.get_field_index(column),
        column,
        pa.array([value], type=field.type),
    )

    _assert_failure(tmp_path, table, SnapshotErrorCode.SOURCE_SCHEMA_INVALID)


def test_load_rejects_event_timestamp_outside_kst_partition(tmp_path: Path) -> None:
    table = table_for(
        (
            event_for(
                date(2026, 9, 1),
                slate_id="slt_20260901_0123456789abcdef01234567",
            ),
        )
    ).set_column(
        EVENT_LOG_PARQUET_SCHEMA.get_field_index("event_timestamp"),
        "event_timestamp",
        pa.array([datetime(2026, 8, 31, 14, 59, tzinfo=UTC)], type=pa.timestamp("us", tz="UTC")),
    )

    _assert_failure(tmp_path, table, SnapshotErrorCode.PARTITION_TIMESTAMP_MISMATCH)


def _table_with_event_id(event_id: str) -> pa.Table:
    return table_for(
        (
            event_for(
                date(2026, 9, 1),
                slate_id="slt_20260901_0123456789abcdef01234567",
            ),
        )
    ).set_column(
        EVENT_LOG_PARQUET_SCHEMA.get_field_index("event_id"),
        "event_id",
        pa.array([event_id], type=pa.string()),
    )


def test_load_rejects_wrong_event_id_prefix(tmp_path: Path) -> None:
    _assert_failure(
        tmp_path,
        _table_with_event_id("daily_20260901_00000001"),
        SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
    )


def test_load_rejects_wrong_event_id_format(tmp_path: Path) -> None:
    _assert_failure(
        tmp_path,
        _table_with_event_id("evt_20260901_1"),
        SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
    )


def test_load_rejects_event_id_partition_date_mismatch(tmp_path: Path) -> None:
    _assert_failure(
        tmp_path,
        _table_with_event_id("evt_20260902_00000001"),
        SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
    )


def test_load_rejects_missing_slate_column_after_cutover(tmp_path: Path) -> None:
    table = table_for(
        (
            event_for(
                date(2026, 9, 1),
            ),
        )
    ).drop_columns(["slate_id"])

    _assert_failure(tmp_path, table, SnapshotErrorCode.SLATE_ID_MISSING_AFTER_CUTOVER)


def test_load_allows_missing_slate_column_before_cutover(tmp_path: Path) -> None:
    root = tmp_path / "action-log"
    legacy = table_for((event_for(date(2026, 8, 31)),)).drop_columns(["slate_id"])
    write_table(root, date(2026, 8, 31), legacy)
    write_table(
        root,
        date(2026, 9, 1),
        table_for(
            (
                event_for(
                    date(2026, 9, 1),
                    slate_id="slt_20260901_0123456789abcdef01234567",
                ),
            )
        ),
    )
    write_table(root, date(2026, 9, 2), pa.Table.from_pylist([], schema=EVENT_LOG_PARQUET_SCHEMA))

    loaded = load_required_partitions(request_for(str(root), tmp_path / "output"))

    assert loaded[0].events[0].slate_id is None


def test_load_rejects_null_slate_after_cutover(tmp_path: Path) -> None:
    table = table_for((event_for(date(2026, 9, 1)),))

    _assert_failure(tmp_path, table, SnapshotErrorCode.SLATE_ID_MISSING_AFTER_CUTOVER)
