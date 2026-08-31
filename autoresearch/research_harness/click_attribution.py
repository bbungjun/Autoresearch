"""평가 action log의 결정적 click attribution.

[파이프라인] 검증된 multi-day action log partition과 user split 사이에서 평가
impression의 click label을 계산한다.

[기능] 30분 시간 귀속과 결정적 동률 해소를 적용해 평가 기간 impression을 반환한다.

[비책임] 원천 Parquet 검증, slate identity 생성, user split과 artifact 쓰기는 담당하지
않는다.
"""

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.evaluation_snapshot_models import (
    AttributedImpression,
    EvaluationWindow,
)
from autoresearch.research_harness.evaluation_source_models import (
    LoadedPartition,
    SourceEvent,
)


@dataclass(frozen=True, slots=True)
class _ImpressionBucket:
    events: tuple[SourceEvent, ...]
    timestamps: tuple[datetime, ...]


def attribute_clicks(
    partitions: tuple[LoadedPartition, ...],
    window: EvaluationWindow,
) -> tuple[AttributedImpression, ...]:
    kst = ZoneInfo("Asia/Seoul")
    output_start = datetime.combine(window.evaluation_start_date, time.min, tzinfo=kst)
    output_end = datetime.combine(
        window.evaluation_end_date + timedelta(days=1),
        time.min,
        tzinfo=kst,
    )
    candidate_end_date = window.evaluation_end_date + timedelta(days=1)
    candidate_impressions = tuple(
        event
        for partition in partitions
        for event in partition.events
        if event.event_type == "impression"
        and window.evaluation_start_date <= event.partition_date <= candidate_end_date
    )
    output_impressions = tuple(
        event
        for event in candidate_impressions
        if output_start <= event.event_timestamp < output_end
    )
    candidate_groups: dict[tuple[str, str], list[SourceEvent]] = {}
    for impression in candidate_impressions:
        candidate_groups.setdefault(
            (impression.user_id, impression.video_id),
            [],
        ).append(impression)
    candidate_index: dict[tuple[str, str], _ImpressionBucket] = {}
    for candidate_key, grouped_impressions in candidate_groups.items():
        sorted_impressions = tuple(
            sorted(
                grouped_impressions,
                key=lambda impression: (
                    impression.event_timestamp,
                    impression.source_event_id,
                ),
            )
        )
        candidate_index[candidate_key] = _ImpressionBucket(
            events=sorted_impressions,
            timestamps=tuple(
                impression.event_timestamp for impression in sorted_impressions
            ),
        )
    clicked_source_event_ids: set[str] = set()
    for partition in partitions:
        for click in partition.events:
            if (
                click.event_type != "click"
                or click.event_timestamp < output_start
                or click.event_timestamp >= output_end + timedelta(minutes=30)
            ):
                continue
            candidate_bucket = candidate_index.get((click.user_id, click.video_id))
            if candidate_bucket is None:
                continue
            selected_position = (
                bisect_left(candidate_bucket.timestamps, click.event_timestamp) - 1
            )
            if selected_position < 0:
                continue
            selected = candidate_bucket.events[selected_position]
            if selected.event_timestamp < click.event_timestamp - timedelta(minutes=30):
                continue
            if selected.slate_id != click.slate_id:
                raise EvaluationSnapshotError(
                    code=SnapshotErrorCode.SLATE_ATTRIBUTION_MISMATCH,
                    stage="click_attribution",
                    dt=selected.partition_date,
                    identifier_prefix=click.source_event_id,
                )
            clicked_source_event_ids.add(selected.source_event_id)
    return tuple(
        _attributed_impression(
            event,
            clicked=event.source_event_id in clicked_source_event_ids,
        )
        for event in sorted(
            output_impressions,
            key=lambda item: (
                item.user_id,
                item.slate_id or "",
                item.event_timestamp,
                item.video_id,
                item.source_event_id,
            ),
        )
    )


def _attributed_impression(event: SourceEvent, *, clicked: bool) -> AttributedImpression:
    if event.slate_id is None:
        raise EvaluationSnapshotError(
            code=SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
            stage="click_attribution",
            dt=event.partition_date,
        )
    return AttributedImpression(
        slate_id=event.slate_id,
        user_id=event.user_id,
        video_id=event.video_id,
        event_timestamp=event.event_timestamp,
        source_event_id=event.source_event_id,
        clicked=clicked,
        original_rank=event.rank,
        candidate_source=event.exposure_source,
    )
