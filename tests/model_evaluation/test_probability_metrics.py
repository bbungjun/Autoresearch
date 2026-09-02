"""CLI와 P0-2B Judge가 공유하는 probability metric 계약 테스트."""

import pytest

from autoresearch.model_evaluation import evaluate
from autoresearch.model_evaluation.probability_metrics import (
    GroupedRocAuc,
    ProbabilityMetricResult,
    grouped_roc_auc,
    probability_metrics,
)


def test_probability_metrics_matches_hand_calculated_fixture() -> None:
    result = probability_metrics(
        labels=[1, 0, 1, 0],
        scores=[0.9, 0.8, 0.2, 0.1],
        groups=["user-a", "user-a", "user-b", "user-b"],
    )

    assert result == ProbabilityMetricResult(
        row_count=4,
        positive_count=2,
        negative_count=2,
        roc_auc=pytest.approx(0.75),
        pr_auc=pytest.approx(5.0 / 6.0),
        log_loss=pytest.approx(0.8573992140459633),
        brier=pytest.approx(0.325),
        grouped_roc_auc=GroupedRocAuc(
            value=pytest.approx(1.0),
            total_groups=2,
            scored_groups=2,
            skipped_groups=0,
            null_key_rows=0,
        ),
    )


def test_probability_metrics_can_omit_grouped_observation() -> None:
    result = probability_metrics(
        labels=[1, 0],
        scores=[0.8, 0.2],
        groups=None,
    )

    assert result.grouped_roc_auc is None


def test_probability_metrics_structures_single_class_as_unavailable() -> None:
    result = probability_metrics(
        labels=[1, 1],
        scores=[0.9, 0.8],
        groups=["user-a", "user-b"],
    )

    assert result.row_count == 2
    assert result.positive_count == 2
    assert result.negative_count == 0
    assert result.roc_auc is None
    assert result.pr_auc is None
    assert result.log_loss is None
    assert result.brier is None
    assert result.grouped_roc_auc is not None
    assert result.grouped_roc_auc.value is None


def test_evaluate_reexports_the_extracted_probability_contract() -> None:
    assert evaluate.GroupedRocAuc is GroupedRocAuc
    assert evaluate.grouped_roc_auc is grouped_roc_auc
