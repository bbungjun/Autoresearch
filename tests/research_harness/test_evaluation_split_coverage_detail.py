from datetime import UTC, datetime

import pytest

from autoresearch.research_harness.evaluation_errors import EvaluationSnapshotError
from autoresearch.research_harness.evaluation_snapshot_models import (
    AttributedImpression,
    EvaluationSplit,
    SplitName,
)
from autoresearch.research_harness.evaluation_split import split_statistics


def _row(clicked: bool) -> AttributedImpression:
    return AttributedImpression(
        slate_id="coverage-slate",
        user_id="coverage-user",
        video_id=f"coverage-video-{int(clicked)}",
        event_timestamp=datetime(2026, 9, 1, tzinfo=UTC),
        source_event_id=f"coverage-event-{int(clicked)}",
        clicked=clicked,
        original_rank=1 if clicked else None,
        candidate_source="model" if clicked else None,
    )


@pytest.mark.parametrize("name", ("validation", "final_holdout"))
def test_split_statistics_reports_missing_user_metric_for_each_split(
    name: SplitName,
) -> None:
    split = EvaluationSplit(name=name, rows=(_row(True), _row(False)), user_ids=())

    with pytest.raises(EvaluationSnapshotError) as captured:
        split_statistics(split)

    assert captured.value.stage == f"split_coverage:{name}:user"


@pytest.mark.parametrize("name", ("validation", "final_holdout"))
def test_split_statistics_reports_missing_slate_metric_for_each_split(
    name: SplitName,
) -> None:
    split = EvaluationSplit(name=name, rows=(), user_ids=("coverage-user",))

    with pytest.raises(EvaluationSnapshotError) as captured:
        split_statistics(split)

    assert captured.value.stage == (
        f"split_coverage:{name}:slate,click_positive_slate,clicked_impression,"
        "non_clicked_impression"
    )


@pytest.mark.parametrize("name", ("validation", "final_holdout"))
def test_split_statistics_reports_missing_click_positive_metric_for_each_split(
    name: SplitName,
) -> None:
    split = EvaluationSplit(name=name, rows=(_row(False),), user_ids=("coverage-user",))

    with pytest.raises(EvaluationSnapshotError) as captured:
        split_statistics(split)

    assert captured.value.stage == (
        f"split_coverage:{name}:click_positive_slate,clicked_impression"
    )


@pytest.mark.parametrize("name", ("validation", "final_holdout"))
def test_split_statistics_reports_missing_clicked_metric_for_each_split(
    name: SplitName,
) -> None:
    split = EvaluationSplit(name=name, rows=(_row(False),), user_ids=("coverage-user",))

    with pytest.raises(EvaluationSnapshotError) as captured:
        split_statistics(split)

    assert captured.value.stage == (
        f"split_coverage:{name}:click_positive_slate,clicked_impression"
    )


@pytest.mark.parametrize("name", ("validation", "final_holdout"))
def test_split_statistics_reports_missing_non_clicked_metric_for_each_split(
    name: SplitName,
) -> None:
    split = EvaluationSplit(name=name, rows=(_row(True),), user_ids=("coverage-user",))

    with pytest.raises(EvaluationSnapshotError) as captured:
        split_statistics(split)

    assert captured.value.stage == f"split_coverage:{name}:non_clicked_impression"
