from dataclasses import FrozenInstanceError, fields
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import pyarrow.parquet as pq

from autoresearch.research_harness import evaluation_source_models
from tests.research_harness import conftest as fixture_factory


def test_source_event_is_frozen_typed_value() -> None:
    event = evaluation_source_models.SourceEvent(
        partition_date=date(2026, 9, 1),
        source_event_id="evt_20260901_00000001",
        event_type="impression",
        user_id="user-1",
        video_id="video-1",
        event_timestamp=datetime(2026, 9, 1, tzinfo=UTC),
        slate_id="slt_20260901_0123456789abcdef01234567",
        rank=1,
        exposure_source="model",
        policy_version="policy-v1",
    )

    assert event.event_type == "impression"
    with pytest.raises(FrozenInstanceError):
        event.user_id = "changed"


def test_loaded_partition_keeps_receipt_and_immutable_events() -> None:
    receipt = evaluation_source_models.SourcePartitionReceipt(
        dt=date(2026, 9, 1),
        uri="memory://source/dt=2026-09-01/part-0.parquet",
        rows=1,
        sha256="a" * 64,
    )
    event = evaluation_source_models.SourceEvent(
        partition_date=date(2026, 9, 1),
        source_event_id="evt_20260901_00000001",
        event_type="click",
        user_id="user-1",
        video_id="video-1",
        event_timestamp=datetime(2026, 9, 1, tzinfo=UTC),
        slate_id=None,
        rank=None,
        exposure_source=None,
        policy_version=None,
    )

    loaded = evaluation_source_models.LoadedPartition(receipt=receipt, events=(event,))

    assert loaded.receipt.rows == 1
    assert loaded.events == (event,)
    assert [field.name for field in fields(receipt)] == ["dt", "uri", "rows", "sha256"]
    with pytest.raises(FrozenInstanceError):
        receipt.rows = 2


def test_parquet_factory_writes_real_pyarrow_file(tmp_path: Path) -> None:
    event = evaluation_source_models.SourceEvent(
        partition_date=date(2026, 9, 1),
        source_event_id="evt_20260901_00000001",
        event_type="impression",
        user_id="user-1",
        video_id="video-1",
        event_timestamp=datetime(2026, 9, 1, tzinfo=UTC),
        slate_id="slt_20260901_0123456789abcdef01234567",
        rank=1,
        exposure_source="model",
        policy_version="policy-v1",
    )

    path = fixture_factory.write_source_event_parquet(tmp_path / "part-0.parquet", (event,))

    assert path.read_bytes()[:4] == b"PAR1"
    table = pq.read_table(path)
    assert table.num_rows == 1
    assert table.schema.field("event_timestamp").type.tz == "UTC"
