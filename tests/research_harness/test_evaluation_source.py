import hashlib
from datetime import date
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


def _write_required_partitions(root: Path) -> None:
    write_table(root, date(2026, 8, 31), table_for((event_for(date(2026, 8, 31)),)))
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


def test_load_required_partitions_returns_every_required_date(tmp_path: Path) -> None:
    root = tmp_path / "action-log"
    write_table(root, date(2026, 8, 31), table_for((event_for(date(2026, 8, 31)),)))
    for partition_date in (date(2026, 9, 1), date(2026, 9, 2)):
        write_table(
            root,
            partition_date,
            table_for(
                (
                    event_for(
                        partition_date,
                        slate_id=f"slt_{partition_date:%Y%m%d}_0123456789abcdef01234567",
                    ),
                )
            ),
        )

    loaded = load_required_partitions(request_for(str(root), tmp_path / "output"))

    assert tuple(partition.receipt.dt for partition in loaded) == (
        date(2026, 8, 31),
        date(2026, 9, 1),
        date(2026, 9, 2),
    )
    assert tuple(partition.receipt.rows for partition in loaded) == (1, 1, 1)
    assert tuple(partition.events[0].source_event_id for partition in loaded) == (
        "evt_20260831_00000001",
        "evt_20260901_00000001",
        "evt_20260902_00000001",
    )


def test_load_required_partitions_accepts_empty_final_partition(tmp_path: Path) -> None:
    root = tmp_path / "action-log"
    _write_required_partitions(root)

    loaded = load_required_partitions(request_for(str(root), tmp_path / "output"))

    assert tuple(partition.receipt.rows for partition in loaded) == (1, 1, 0)
    assert loaded[-1].events == ()
    assert all(len(partition.receipt.sha256) == 64 for partition in loaded)


def test_load_receipt_sha256_matches_final_partition_bytes(tmp_path: Path) -> None:
    # Given
    root = tmp_path / "action-log"
    _write_required_partitions(root)
    partition_path = root / "dt=2026-09-01" / "part-0.parquet"
    expected_sha256 = hashlib.sha256(partition_path.read_bytes()).hexdigest()

    # When
    loaded = load_required_partitions(request_for(str(root), tmp_path / "output"))

    # Then
    assert loaded[1].receipt.sha256 == expected_sha256


def test_load_required_partitions_requires_label_scan_end_partition(tmp_path: Path) -> None:
    root = tmp_path / "action-log"
    write_table(root, date(2026, 8, 31), table_for((event_for(date(2026, 8, 31)),)))
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

    with pytest.raises(EvaluationSnapshotError) as captured:
        load_required_partitions(request_for(str(root), tmp_path / "output"))

    assert captured.value.code is SnapshotErrorCode.SOURCE_PARTITION_MISSING
    assert captured.value.dt == date(2026, 9, 2)
    assert "action-log" not in str(captured.value)


def test_load_required_partitions_maps_corrupt_parquet_to_typed_error(tmp_path: Path) -> None:
    root = tmp_path / "action-log"
    corrupt = root / "dt=2026-08-31" / "part-0.parquet"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"not parquet")

    with pytest.raises(EvaluationSnapshotError) as captured:
        load_required_partitions(request_for(str(root), tmp_path / "output"))

    assert captured.value.code is SnapshotErrorCode.SOURCE_PARTITION_MISSING
    assert captured.value.dt == date(2026, 8, 31)
    assert "not parquet" not in str(captured.value)


def test_load_required_partitions_does_not_read_shards(tmp_path: Path) -> None:
    root = tmp_path / "action-log"
    for partition_date in (date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2)):
        slate_id = (
            f"slt_{partition_date:%Y%m%d}_0123456789abcdef01234567"
            if partition_date >= date(2026, 9, 1)
            else None
        )
        write_table(root, partition_date, table_for((event_for(partition_date, slate_id=slate_id),)))
    shard = root / "dt=2026-09-01" / "shard=000" / "part-0.parquet"
    shard.parent.mkdir(parents=True)
    shard.write_bytes(b"not parquet")

    loaded = load_required_partitions(request_for(str(root), tmp_path / "output"))

    assert len(loaded) == 3


@pytest.mark.parametrize("event_type", ("impression", "click", "view", "like"))
def test_load_required_partitions_accepts_every_event_domain(
    tmp_path: Path,
    event_type: str,
) -> None:
    root = tmp_path / "action-log"
    write_table(root, date(2026, 8, 31), pa.Table.from_pylist([], schema=EVENT_LOG_PARQUET_SCHEMA))
    write_table(
        root,
        date(2026, 9, 1),
        table_for(
            (
                event_for(
                    date(2026, 9, 1),
                    event_type=event_type,
                    slate_id="slt_20260901_0123456789abcdef01234567",
                ),
            )
        ),
    )
    write_table(root, date(2026, 9, 2), pa.Table.from_pylist([], schema=EVENT_LOG_PARQUET_SCHEMA))

    loaded = load_required_partitions(request_for(str(root), tmp_path / "output"))

    assert loaded[1].events[0].event_type == event_type
