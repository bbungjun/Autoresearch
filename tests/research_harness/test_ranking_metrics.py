"""P0-2A 결정적 리랭킹 지표의 손 계산 golden test."""

from __future__ import annotations

import math
from collections.abc import Callable

import pytest

from autoresearch.research_harness.ranking_metrics import (
    RankingMetricError,
    RankingMetricErrorCode,
    RankingMetricResult,
    ndcg_at_k,
    recall_at_k,
)


def test_ndcg_is_one_for_the_ideal_order() -> None:
    result = ndcg_at_k(
        labels=[1, 0, 0],
        scores=[0.9, 0.2, 0.1],
        slate_ids=["slate-1"] * 3,
        video_ids=["video-a", "video-b", "video-c"],
        k=3,
    )

    assert result.value == pytest.approx(1.0)


def test_ndcg_places_a_single_click_at_rank_three() -> None:
    result = ndcg_at_k(
        labels=[1, 0, 0],
        scores=[0.1, 0.3, 0.2],
        slate_ids=["slate-1"] * 3,
        video_ids=["video-a", "video-b", "video-c"],
        k=3,
    )

    assert result.value == pytest.approx(0.5)
    assert result.total_slates == 1
    assert result.scored_slates == 1
    assert result.skipped_zero_click_slates == 0
    assert result.coverage == pytest.approx(1.0)


def test_ndcg_uses_video_id_as_the_equal_score_tie_breaker() -> None:
    result = ndcg_at_k(
        labels=[1, 0],
        scores=[0.5, 0.5],
        slate_ids=["slate-1", "slate-1"],
        video_ids=["video-b", "video-a"],
        k=2,
    )

    assert result.value == pytest.approx(1.0 / math.log2(3.0))


def test_recall_denominator_is_every_click_in_the_slate() -> None:
    result = recall_at_k(
        labels=[1, 1, 1, 0],
        scores=[0.9, 0.8, 0.1, 0.7],
        slate_ids=["slate-1"] * 4,
        video_ids=["video-a", "video-b", "video-c", "video-d"],
        k=2,
    )

    assert result.value == pytest.approx(2.0 / 3.0)


def test_ndcg_accepts_a_slate_shorter_than_k() -> None:
    result = ndcg_at_k(
        labels=[0, 1],
        scores=[0.9, 0.8],
        slate_ids=["slate-1", "slate-1"],
        video_ids=["video-a", "video-b"],
        k=10,
    )

    assert result.value == pytest.approx(1.0 / math.log2(3.0))


@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        (ndcg_at_k, 0.75),
        (recall_at_k, 0.5),
    ],
)
def test_metric_is_a_macro_mean_and_skips_zero_click_slates(
    metric: Callable[..., RankingMetricResult],
    expected: float,
) -> None:
    labels = [1, 0, 0, 1, 0, 0, 0]
    scores = [0.9, 0.2, 0.1, 0.1, 0.3, 0.2, 0.9]
    slate_ids = ["slate-a"] * 3 + ["slate-b"] * 3 + ["slate-c"]
    video_ids = [
        "video-a1",
        "video-a2",
        "video-a3",
        "video-b1",
        "video-b2",
        "video-b3",
        "video-c1",
    ]

    result = metric(
        labels,
        scores,
        slate_ids,
        video_ids,
        k=1 if metric is recall_at_k else 3,
    )

    assert result.value == pytest.approx(expected)
    assert result.total_slates == 3
    assert result.scored_slates == 2
    assert result.skipped_zero_click_slates == 1
    assert result.coverage == pytest.approx(2.0 / 3.0)


def test_metric_reports_no_value_when_every_slate_has_zero_clicks() -> None:
    result = ndcg_at_k(
        labels=[0, 0],
        scores=[0.9, 0.1],
        slate_ids=["slate-1", "slate-1"],
        video_ids=["video-a", "video-b"],
        k=2,
    )

    assert result.value is None
    assert result.total_slates == 1
    assert result.scored_slates == 0
    assert result.skipped_zero_click_slates == 1
    assert result.coverage == pytest.approx(0.0)


def test_metric_reports_zero_coverage_for_empty_input() -> None:
    result = recall_at_k([], [], [], [], k=10)

    assert result.value is None
    assert result.total_slates == 0
    assert result.scored_slates == 0
    assert result.skipped_zero_click_slates == 0
    assert result.coverage == pytest.approx(0.0)


def test_metric_is_invariant_to_input_row_order_for_unique_keys() -> None:
    original = ndcg_at_k(
        labels=[1, 0, 1, 0],
        scores=[0.8, 0.2, 0.4, 0.9],
        slate_ids=["slate-b", "slate-a", "slate-a", "slate-b"],
        video_ids=["video-b1", "video-a1", "video-a2", "video-b2"],
        k=2,
    )
    permuted = ndcg_at_k(
        labels=[0, 0, 1, 1],
        scores=[0.9, 0.2, 0.4, 0.8],
        slate_ids=["slate-b", "slate-a", "slate-a", "slate-b"],
        video_ids=["video-b2", "video-a1", "video-a2", "video-b1"],
        k=2,
    )

    assert permuted == original


def test_metric_leaves_probability_range_validation_to_p0_2b() -> None:
    result = ndcg_at_k(
        labels=[1, 0],
        scores=[2.0, -1.0],
        slate_ids=["slate-1", "slate-1"],
        video_ids=["video-a", "video-b"],
        k=2,
    )

    assert result.value == pytest.approx(1.0)


def test_error_codes_exactly_match_the_p0_2a_contract() -> None:
    assert {code.value for code in RankingMetricErrorCode} == {
        "length_mismatch",
        "invalid_k",
        "invalid_label",
        "invalid_identifier",
        "non_finite_score",
    }


@pytest.mark.parametrize(
    ("kwargs", "expected_code"),
    [
        (
            {
                "labels": [1],
                "scores": [],
                "slate_ids": ["slate-1"],
                "video_ids": ["video-a"],
                "k": 1,
            },
            RankingMetricErrorCode.LENGTH_MISMATCH,
        ),
        (
            {
                "labels": [1],
                "scores": [0.5],
                "slate_ids": ["slate-1"],
                "video_ids": ["video-a"],
                "k": 0,
            },
            RankingMetricErrorCode.INVALID_K,
        ),
        (
            {
                "labels": [1],
                "scores": [0.5],
                "slate_ids": ["slate-1"],
                "video_ids": ["video-a"],
                "k": False,
            },
            RankingMetricErrorCode.INVALID_K,
        ),
        (
            {
                "labels": [2],
                "scores": [0.5],
                "slate_ids": ["slate-1"],
                "video_ids": ["video-a"],
                "k": 1,
            },
            RankingMetricErrorCode.INVALID_LABEL,
        ),
        (
            {
                "labels": [1],
                "scores": [0.5],
                "slate_ids": [" slate-1"],
                "video_ids": ["video-a"],
                "k": 1,
            },
            RankingMetricErrorCode.INVALID_IDENTIFIER,
        ),
        (
            {
                "labels": [1],
                "scores": [0.5],
                "slate_ids": ["slate-1"],
                "video_ids": [""],
                "k": 1,
            },
            RankingMetricErrorCode.INVALID_IDENTIFIER,
        ),
        (
            {
                "labels": [1],
                "scores": [float("nan")],
                "slate_ids": ["slate-1"],
                "video_ids": ["video-a"],
                "k": 1,
            },
            RankingMetricErrorCode.NON_FINITE_SCORE,
        ),
        (
            {
                "labels": [1],
                "scores": [float("inf")],
                "slate_ids": ["slate-1"],
                "video_ids": ["video-a"],
                "k": 1,
            },
            RankingMetricErrorCode.NON_FINITE_SCORE,
        ),
    ],
)
def test_metric_rejects_invalid_contract_input(
    kwargs: dict[str, object],
    expected_code: RankingMetricErrorCode,
) -> None:
    with pytest.raises(RankingMetricError) as error:
        ndcg_at_k(**kwargs)  # type: ignore[arg-type]

    assert error.value.code is expected_code
