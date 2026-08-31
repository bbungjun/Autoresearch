from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.research_harness.evaluation_source_models import SourceEvent


def write_source_event_parquet(path: Path, events: tuple[SourceEvent, ...]) -> Path:
    table = pa.table(
        {
            "partition_date": pa.array([event.partition_date for event in events], type=pa.date32()),
            "source_event_id": pa.array([event.source_event_id for event in events], type=pa.string()),
            "event_type": pa.array([event.event_type for event in events], type=pa.string()),
            "user_id": pa.array([event.user_id for event in events], type=pa.string()),
            "video_id": pa.array([event.video_id for event in events], type=pa.string()),
            "event_timestamp": pa.array(
                [event.event_timestamp for event in events], type=pa.timestamp("us", tz="UTC")
            ),
            "slate_id": pa.array([event.slate_id for event in events], type=pa.string()),
            "rank": pa.array([event.rank for event in events], type=pa.int64()),
            "exposure_source": pa.array([event.exposure_source for event in events], type=pa.string()),
            "policy_version": pa.array([event.policy_version for event in events], type=pa.string()),
        }
    )
    pq.write_table(table, path)
    return path
