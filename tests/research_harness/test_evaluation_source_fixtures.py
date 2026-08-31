from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.action_log_generation.pipeline import EVENT_LOG_PARQUET_SCHEMA
from autoresearch.action_log_generation.schema import EventLog
from autoresearch.research_harness.evaluation_snapshot_models import EvaluationSnapshotRequest


def event_for(
    partition_date: date,
    *,
    event_type: str = "impression",
    slate_id: str | None = None,
) -> EventLog:
    return EventLog.model_validate(
        {
            "event_id": f"evt_{partition_date:%Y%m%d}_00000001",
            "event_timestamp": datetime.combine(partition_date, datetime.min.time(), tzinfo=UTC),
            "user_id": "user-1",
            "event_type": event_type,
            "video_id": "video-1",
            "watch_time_sec": 1 if event_type == "view" else None,
            "rank": 1,
            "source": "historical",
            "policy": None,
            "ctr_score": None,
            "is_exploration": None,
            "policy_version": "policy-v1",
            "exposure_source": "model",
            "slate_id": slate_id,
        }
    )


def table_for(events: tuple[EventLog, ...]) -> pa.Table:
    rows = []
    for event in events:
        rows.append(
            event.model_dump()
            | {
                "schema_version": "action_log_schema_v1",
                "prompt_version": "action_log_ctr_v4",
                "llm_model": "fixture-model",
                "generated_at": "2026-09-01T00:00:00Z",
            }
        )
    return pa.Table.from_pylist(rows, schema=EVENT_LOG_PARQUET_SCHEMA)


def write_table(root: Path, partition_date: date, table: pa.Table) -> Path:
    target = root / f"dt={partition_date.isoformat()}" / "part-0.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, target)
    return target


def request_for(root: str, output_root: Path) -> EvaluationSnapshotRequest:
    return EvaluationSnapshotRequest(
        action_log_root=root,
        history_start_date=date(2026, 8, 31),
        evaluation_start_date=date(2026, 9, 1),
        evaluation_end_date=date(2026, 9, 1),
        slate_id_cutover_date=date(2026, 9, 1),
        output_root=output_root,
    )
