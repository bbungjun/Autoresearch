from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pytest

from autoresearch.action_log_generation.pipeline import EVENT_LOG_PARQUET_SCHEMA
from autoresearch.action_log_generation.slate_identity import (
    SlateIdentity,
    SlateMember,
    generate_slate_id,
)
from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.evaluation_source import load_required_partitions
from autoresearch.research_harness.slate import build_evaluation_snapshot
from tests.research_harness.test_evaluation_source_fixtures import (
    event_for,
    request_for,
    table_for,
    write_table,
)


_EVALUATION_DATE = date(2026, 9, 1)
_DUPLICATE_EVENT_ID = "evt_20260901_00000001"


def _write_empty_neighbors(root: Path) -> None:
    empty = pa.Table.from_pylist([], schema=EVENT_LOG_PARQUET_SCHEMA)
    write_table(root, date(2026, 8, 31), empty)
    write_table(root, date(2026, 9, 2), empty)


def test_build_rejects_duplicate_impression_id_before_one_click_can_contaminate_labels(
    tmp_path: Path,
) -> None:
    # Given: one click matches only the first impression, but a duplicate ID would mark both clicked.
    root = tmp_path / "action-log"
    output_root = tmp_path / "output"
    _write_empty_neighbors(root)
    matched_impression = event_for(
        _EVALUATION_DATE,
        slate_id=str(
            generate_slate_id(
                SlateIdentity(
                    _EVALUATION_DATE,
                    "user-1",
                    (SlateMember("video-1", 1, "model", "policy-v1"),),
                )
            )
        ),
    )
    unrelated_impression = matched_impression.model_copy(
        update={
            "user_id": "user-2",
            "video_id": "video-2",
            "event_timestamp": matched_impression.event_timestamp + timedelta(minutes=1),
            "slate_id": str(
                generate_slate_id(
                    SlateIdentity(
                        _EVALUATION_DATE,
                        "user-2",
                        (SlateMember("video-2", 1, "model", "policy-v1"),),
                    )
                )
            ),
        }
    )
    matching_click = matched_impression.model_copy(
        update={
            "event_id": "evt_20260901_00000002",
            "event_type": "click",
            "event_timestamp": matched_impression.event_timestamp + timedelta(minutes=2),
        }
    )
    write_table(
        root,
        _EVALUATION_DATE,
        table_for((matched_impression, unrelated_impression, matching_click)),
    )

    # When / Then
    with pytest.raises(EvaluationSnapshotError) as captured:
        build_evaluation_snapshot(request_for(str(root), output_root))

    assert captured.value.code is SnapshotErrorCode.SOURCE_SCHEMA_INVALID
    assert captured.value.stage == "row_validation"
    assert captured.value.dt == _EVALUATION_DATE
    assert captured.value.count == 2
    assert captured.value.identifier_prefix == _DUPLICATE_EVENT_ID[:16]
    assert not (output_root / "evaluation-snapshots").exists()


def test_load_rejects_duplicate_event_id_across_pre_cutover_and_evaluation_partitions(
    tmp_path: Path,
) -> None:
    # Given
    root = tmp_path / "action-log"
    output_root = tmp_path / "output"
    historical_event = event_for(date(2026, 8, 31), event_type="view")
    duplicate_evaluation_event = event_for(
        _EVALUATION_DATE,
        slate_id=str(
            generate_slate_id(
                SlateIdentity(
                    _EVALUATION_DATE,
                    "user-1",
                    (SlateMember("video-1", 1, "model", "policy-v1"),),
                )
            )
        ),
    ).model_copy(update={"event_id": historical_event.event_id})
    write_table(root, date(2026, 8, 31), table_for((historical_event,)))
    write_table(root, _EVALUATION_DATE, table_for((duplicate_evaluation_event,)))
    write_table(root, date(2026, 9, 2), pa.Table.from_pylist([], schema=EVENT_LOG_PARQUET_SCHEMA))

    # When / Then
    with pytest.raises(EvaluationSnapshotError) as captured:
        load_required_partitions(request_for(str(root), output_root))

    assert captured.value.code is SnapshotErrorCode.SOURCE_SCHEMA_INVALID
    assert captured.value.stage == "row_validation"
    assert captured.value.dt == _EVALUATION_DATE
    assert captured.value.count == 2
    assert captured.value.identifier_prefix == historical_event.event_id[:16]
