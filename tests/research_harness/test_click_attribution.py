from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Final, Literal

import pytest

from autoresearch.research_harness.click_attribution import attribute_clicks
from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.evaluation_snapshot_models import EvaluationWindow
from autoresearch.research_harness.evaluation_source_models import (
    LoadedPartition,
    SourceEvent,
    SourcePartitionReceipt,
)


EVALUATION_DATE: Final = date(2026, 9, 1)
NEXT_DATE: Final = date(2026, 9, 2)
BASE_TIME: Final = datetime(2026, 9, 1, tzinfo=UTC)
NEXT_DAY_START: Final = datetime(2026, 9, 1, 15, tzinfo=UTC)
WINDOW: Final = EvaluationWindow(
    history_start_date=date(2026, 8, 31),
    evaluation_start_date=EVALUATION_DATE,
    evaluation_end_date=EVALUATION_DATE,
    label_scan_end_date=NEXT_DATE,
    complete_history_label_end_date=date(2026, 8, 30),
    candidate_history_partitions=(),
)


def _event(
    event_type: Literal["impression", "click"],
    event_timestamp: datetime,
    source_event_id: str,
) -> SourceEvent:
    return SourceEvent(
        partition_date=EVALUATION_DATE,
        source_event_id=source_event_id,
        event_type=event_type,
        user_id="user-1",
        video_id="video-1",
        event_timestamp=event_timestamp,
        slate_id="slate-1",
        rank=1 if event_type == "impression" else None,
        exposure_source="model" if event_type == "impression" else None,
        policy_version="policy-v1" if event_type == "impression" else None,
    )


def _partition(partition_date: date, events: tuple[SourceEvent, ...]) -> LoadedPartition:
    return LoadedPartition(
        receipt=SourcePartitionReceipt(
            dt=partition_date,
            uri=f"memory://action-log/dt={partition_date.isoformat()}/part-0.parquet",
            rows=len(events),
            sha256="0" * 64,
        ),
        events=events,
    )


def test_attribute_clicks_excludes_equal_timestamp_impression() -> None:
    # Given
    partition = _partition(
        EVALUATION_DATE,
        (
            _event("impression", BASE_TIME, "evt_20260901_00000001"),
            _event("click", BASE_TIME, "evt_20260901_00000002"),
        ),
    )

    # When
    result = attribute_clicks((partition,), WINDOW)

    # Then
    assert tuple(row.clicked for row in result) == (False,)


def test_attribute_clicks_includes_impression_exactly_thirty_minutes_before() -> None:
    # Given
    partition = _partition(
        EVALUATION_DATE,
        (
            _event("impression", BASE_TIME, "evt_20260901_00000001"),
            _event(
                "click",
                BASE_TIME + timedelta(minutes=30),
                "evt_20260901_00000002",
            ),
        ),
    )

    # When
    result = attribute_clicks((partition,), WINDOW)

    # Then
    assert tuple(row.clicked for row in result) == (True,)


def test_attribute_clicks_excludes_impression_older_by_thirty_minutes_and_microsecond() -> None:
    # Given
    partition = _partition(
        EVALUATION_DATE,
        (
            _event("impression", BASE_TIME, "evt_20260901_00000001"),
            _event(
                "click",
                BASE_TIME + timedelta(minutes=30, microseconds=1),
                "evt_20260901_00000002",
            ),
        ),
    )

    # When
    result = attribute_clicks((partition,), WINDOW)

    # Then
    assert tuple(row.clicked for row in result) == (False,)


def test_attribute_clicks_lets_newer_next_day_impression_win_without_outputting_it() -> None:
    # Given
    evaluation_partition = _partition(
        EVALUATION_DATE,
        (_event("impression", NEXT_DAY_START - timedelta(minutes=10), "old-impression"),),
    )
    next_partition = _partition(
        NEXT_DATE,
        (
            replace(
                _event(
                    "impression",
                    NEXT_DAY_START + timedelta(minutes=5),
                    "new-impression",
                ),
                partition_date=NEXT_DATE,
            ),
            replace(
                _event(
                    "click",
                    NEXT_DAY_START + timedelta(minutes=10),
                    "next-day-click",
                ),
                partition_date=NEXT_DATE,
            ),
        ),
    )

    # When
    result = attribute_clicks((evaluation_partition, next_partition), WINDOW)

    # Then
    assert tuple((row.source_event_id, row.clicked) for row in result) == (
        ("old-impression", False),
    )


def test_attribute_clicks_uses_descending_source_event_id_for_timestamp_tie() -> None:
    # Given
    partition = _partition(
        EVALUATION_DATE,
        (
            replace(_event("impression", BASE_TIME, "impression-a"), slate_id="slate-a"),
            replace(_event("impression", BASE_TIME, "impression-b"), slate_id="slate-b"),
            replace(
                _event("click", BASE_TIME + timedelta(minutes=1), "click-1"),
                slate_id="slate-b",
            ),
        ),
    )

    # When
    result = attribute_clicks((partition,), WINDOW)

    # Then
    assert tuple((row.source_event_id, row.clicked) for row in result) == (
        ("impression-a", False),
        ("impression-b", True),
    )


def test_attribute_clicks_outputs_only_half_open_evaluation_window() -> None:
    # Given
    previous_partition = _partition(
        date(2026, 8, 31),
        (
            replace(
                _event(
                    "impression",
                    datetime(2026, 8, 31, 14, 59, 59, 999999, tzinfo=UTC),
                    "before-window",
                ),
                partition_date=date(2026, 8, 31),
            ),
        ),
    )
    evaluation_partition = _partition(
        EVALUATION_DATE,
        (
            _event(
                "impression",
                datetime(2026, 8, 31, 15, tzinfo=UTC),
                "window-start",
            ),
        ),
    )
    next_partition = _partition(
        NEXT_DATE,
        (
            replace(
                _event("impression", NEXT_DAY_START, "window-end"),
                partition_date=NEXT_DATE,
            ),
        ),
    )

    # When
    result = attribute_clicks(
        (next_partition, previous_partition, evaluation_partition),
        WINDOW,
    )

    # Then
    assert tuple(row.source_event_id for row in result) == ("window-start",)


def test_attribute_clicks_raises_typed_error_for_selected_slate_mismatch() -> None:
    # Given
    partition = _partition(
        EVALUATION_DATE,
        (
            replace(_event("impression", BASE_TIME, "impression-1"), slate_id="slate-a"),
            replace(
                _event("click", BASE_TIME + timedelta(minutes=1), "click-1"),
                slate_id="slate-b",
            ),
        ),
    )

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        attribute_clicks((partition,), WINDOW)

    # Then
    assert captured.value.code is SnapshotErrorCode.SLATE_ATTRIBUTION_MISMATCH
    assert captured.value.stage == "click_attribution"


def test_attribute_clicks_is_deterministic_when_input_order_is_reversed() -> None:
    # Given
    events = (
        replace(_event("impression", BASE_TIME, "impression-a"), slate_id="slate-a"),
        replace(_event("impression", BASE_TIME, "impression-b"), slate_id="slate-b"),
        replace(
            _event("click", BASE_TIME + timedelta(minutes=1), "click-1"),
            slate_id="slate-b",
        ),
    )
    forward = _partition(EVALUATION_DATE, events)
    reversed_partition = _partition(EVALUATION_DATE, tuple(reversed(events)))

    # When
    forward_result = attribute_clicks((forward,), WINDOW)
    reversed_result = attribute_clicks((reversed_partition,), WINDOW)

    # Then
    assert reversed_result == forward_result


def test_attribute_clicks_marks_one_impression_for_multiple_eligible_clicks() -> None:
    # Given
    partition = _partition(
        EVALUATION_DATE,
        (
            _event("impression", BASE_TIME, "impression-1"),
            _event(
                "click",
                BASE_TIME + timedelta(minutes=1),
                "click-1",
            ),
            _event(
                "click",
                BASE_TIME + timedelta(minutes=2),
                "click-2",
            ),
        ),
    )

    # When
    result = attribute_clicks((partition,), WINDOW)

    # Then
    assert tuple(row.clicked for row in result) == (True,)
