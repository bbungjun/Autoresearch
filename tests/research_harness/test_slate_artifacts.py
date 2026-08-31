from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.research_harness import build_evaluation_snapshot
from tests.research_harness.test_slate import _snapshot_request


_SLATE_SCHEMA = pa.schema(
    [
        pa.field("evaluation_id", pa.string(), nullable=False),
        pa.field("slate_id", pa.string(), nullable=False),
        pa.field("user_id", pa.string(), nullable=False),
        pa.field("video_id", pa.string(), nullable=False),
        pa.field("event_timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("candidate_source", pa.string()),
        pa.field("original_rank", pa.int64()),
    ]
)
_LABEL_SCHEMA = pa.schema(
    [
        pa.field("evaluation_id", pa.string(), nullable=False),
        pa.field("slate_id", pa.string(), nullable=False),
        pa.field("user_id", pa.string(), nullable=False),
        pa.field("video_id", pa.string(), nullable=False),
        pa.field("source_event_id", pa.string(), nullable=False),
        pa.field("clicked", pa.bool_(), nullable=False),
    ]
)


def test_four_artifacts_seal_labels_with_exact_one_to_one_schema(
    tmp_path: Path,
) -> None:
    # Given
    request = _snapshot_request(tmp_path)

    # When
    receipt = build_evaluation_snapshot(request)
    pairs = tuple(
        (
            pq.read_table(receipt.target_path / split_name / "slate.parquet"),
            pq.read_table(receipt.target_path / split_name / "labels.parquet"),
        )
        for split_name in ("validation", "final_holdout")
    )

    # Then
    assert all(slate.schema == _SLATE_SCHEMA for slate, _ in pairs)
    assert all(labels.schema == _LABEL_SCHEMA for _, labels in pairs)
    assert all(
        slate.select(("evaluation_id", "slate_id", "user_id", "video_id")).to_pylist()
        == labels.select(("evaluation_id", "slate_id", "user_id", "video_id")).to_pylist()
        for slate, labels in pairs
    )
