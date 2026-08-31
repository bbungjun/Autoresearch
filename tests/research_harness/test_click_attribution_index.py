from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import ClassVar, Literal

from autoresearch.research_harness.click_attribution import attribute_clicks
from autoresearch.research_harness.evaluation_snapshot_models import EvaluationWindow
from autoresearch.research_harness.evaluation_source_models import (
    LoadedPartition,
    SourceEvent,
    SourcePartitionReceipt,
)


class _ComparisonCountingUserId(str):
    comparisons: ClassVar[int] = 0

    def __eq__(self, value: str, /) -> bool:
        type(self).comparisons += 1
        return super().__eq__(value)

    __hash__ = str.__hash__


def _event(
    event_type: Literal["impression", "click"],
    event_id: str,
    user_id: str,
) -> SourceEvent:
    base_time = datetime(2026, 9, 1, tzinfo=UTC)
    return SourceEvent(
        partition_date=date(2026, 9, 1),
        source_event_id=event_id,
        event_type=event_type,
        user_id=user_id,
        video_id="video-1",
        event_timestamp=(
            base_time if event_type == "impression" else base_time + timedelta(minutes=1)
        ),
        slate_id="slate-1",
        rank=1 if event_type == "impression" else None,
        exposure_source="model" if event_type == "impression" else None,
        policy_version=None,
    )


def test_attribute_clicks_does_not_rescan_unrelated_impressions_per_click() -> None:
    # Given
    next_date = date(2026, 9, 2)
    next_day_start = datetime(2026, 9, 1, 15, tzinfo=UTC)
    unrelated = tuple(
        replace(
            _event(
                "impression",
                f"unrelated-{index:08d}",
                _ComparisonCountingUserId(f"unrelated-user-{index:08d}"),
            ),
            partition_date=next_date,
            event_timestamp=next_day_start + timedelta(minutes=5),
        )
        for index in range(64)
    )
    matching = replace(
        _event("impression", "matching-impression", "matching-user"),
        event_timestamp=next_day_start - timedelta(minutes=10),
    )
    click = replace(
        _event("click", "matching-click", "matching-user"),
        partition_date=next_date,
        event_timestamp=next_day_start + timedelta(minutes=10),
    )
    events = (*unrelated, matching, click)
    partition = LoadedPartition(
        receipt=SourcePartitionReceipt(
            dt=date(2026, 9, 1),
            uri="memory://action-log/dt=2026-09-01/part-0.parquet",
            rows=len(events),
            sha256="0" * 64,
        ),
        events=events,
    )
    window = EvaluationWindow(
        history_start_date=date(2026, 8, 31),
        evaluation_start_date=date(2026, 9, 1),
        evaluation_end_date=date(2026, 9, 1),
        label_scan_end_date=date(2026, 9, 2),
        complete_history_label_end_date=date(2026, 8, 30),
        candidate_history_partitions=(),
    )
    _ComparisonCountingUserId.comparisons = 0

    # When
    result = attribute_clicks((partition,), window)

    # Then
    assert _ComparisonCountingUserId.comparisons <= 4
    assert next(row for row in result if row.source_event_id == "matching-impression").clicked
