from datetime import UTC, datetime

import pytest

from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.evaluation_snapshot_models import AttributedImpression
from autoresearch.research_harness.evaluation_snapshot_models import EvaluationSplit, SplitName
from autoresearch.research_harness.evaluation_split import split_evaluation_rows, split_statistics


def _row(user_id: str, slate_id: str, clicked: bool) -> AttributedImpression:
    return AttributedImpression(
        slate_id=slate_id,
        user_id=user_id,
        video_id=f"{slate_id}-video-{int(clicked)}",
        event_timestamp=datetime(2026, 9, 1, tzinfo=UTC),
        source_event_id=f"{slate_id}-event-{int(clicked)}",
        clicked=clicked,
        original_rank=1 if clicked else None,
        candidate_source="model" if clicked else None,
    )


def _covered_split(name: SplitName) -> EvaluationSplit:
    return EvaluationSplit(
        name=name,
        rows=(
            _row("coverage-user", "coverage-slate", True),
            _row("coverage-user", "coverage-slate", False),
        ),
        user_ids=("coverage-user",),
    )


def test_split_evaluation_rows_does_not_rebalance_all_validation_users() -> None:
    rows = (
        _row("vector-user-00", "validation-slate", True),
        _row("vector-user-00", "validation-slate", False),
    )

    with pytest.raises(EvaluationSnapshotError) as captured:
        split_evaluation_rows(rows)

    assert captured.value.code is SnapshotErrorCode.SPLIT_COVERAGE_INSUFFICIENT


def test_split_evaluation_rows_rejects_empty_validation_split() -> None:
    rows = (
        _row("fixture-user-04", "final-slate", True),
        _row("fixture-user-04", "final-slate", False),
    )

    with pytest.raises(EvaluationSnapshotError) as captured:
        split_evaluation_rows(rows)

    assert captured.value.code is SnapshotErrorCode.SPLIT_COVERAGE_INSUFFICIENT


def test_split_evaluation_rows_rejects_validation_without_click_positive_slate() -> None:
    rows = (
        _row("vector-user-00", "validation-slate", False),
        _row("fixture-user-04", "final-slate", True),
        _row("fixture-user-04", "final-slate", False),
    )

    with pytest.raises(EvaluationSnapshotError) as captured:
        split_evaluation_rows(rows)

    assert captured.value.code is SnapshotErrorCode.SPLIT_COVERAGE_INSUFFICIENT


@pytest.mark.parametrize("name", ("validation", "final_holdout"))
def test_split_statistics_rejects_each_split_without_users(name: SplitName) -> None:
    covered = _covered_split(name)
    split = EvaluationSplit(name=name, rows=covered.rows, user_ids=())

    with pytest.raises(EvaluationSnapshotError) as captured:
        split_statistics(split)

    assert captured.value.code is SnapshotErrorCode.SPLIT_COVERAGE_INSUFFICIENT


@pytest.mark.parametrize("name", ("validation", "final_holdout"))
def test_split_statistics_rejects_each_split_without_slates(name: SplitName) -> None:
    split = EvaluationSplit(name=name, rows=(), user_ids=("coverage-user",))

    with pytest.raises(EvaluationSnapshotError) as captured:
        split_statistics(split)

    assert captured.value.code is SnapshotErrorCode.SPLIT_COVERAGE_INSUFFICIENT


@pytest.mark.parametrize("name", ("validation", "final_holdout"))
def test_split_statistics_rejects_each_split_without_click_positive_slates(
    name: SplitName,
) -> None:
    split = EvaluationSplit(
        name=name,
        rows=(_row("coverage-user", "coverage-slate", False),),
        user_ids=("coverage-user",),
    )

    with pytest.raises(EvaluationSnapshotError) as captured:
        split_statistics(split)

    assert captured.value.code is SnapshotErrorCode.SPLIT_COVERAGE_INSUFFICIENT


@pytest.mark.parametrize("name", ("validation", "final_holdout"))
def test_split_statistics_rejects_each_split_without_clicked_rows(name: SplitName) -> None:
    split = EvaluationSplit(
        name=name,
        rows=(_row("coverage-user", "coverage-slate", False),),
        user_ids=("coverage-user",),
    )

    with pytest.raises(EvaluationSnapshotError) as captured:
        split_statistics(split)

    assert captured.value.code is SnapshotErrorCode.SPLIT_COVERAGE_INSUFFICIENT


@pytest.mark.parametrize("name", ("validation", "final_holdout"))
def test_split_statistics_rejects_each_split_without_non_clicked_rows(
    name: SplitName,
) -> None:
    split = EvaluationSplit(
        name=name,
        rows=(_row("coverage-user", "coverage-slate", True),),
        user_ids=("coverage-user",),
    )

    with pytest.raises(EvaluationSnapshotError) as captured:
        split_statistics(split)

    assert captured.value.code is SnapshotErrorCode.SPLIT_COVERAGE_INSUFFICIENT
