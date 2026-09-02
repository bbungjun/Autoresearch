"""P0-2C screening과 confirmation Judge 판정 계약 테스트."""

from __future__ import annotations

from dataclasses import replace

import pytest

from autoresearch.model_evaluation.probability_metrics import (
    GroupedRocAuc,
    ProbabilityMetricResult,
)
from autoresearch.research_harness.evaluation_snapshot_models import EvaluationId
from autoresearch.research_harness.judge import JudgeScoringResult
from autoresearch.research_harness.judge_decision import (
    JudgeDecision,
    JudgeMetric,
    JudgeReasonCode,
    PairedJudgeResult,
    compare_confirmation,
    screen_candidate,
)
from autoresearch.research_harness.ranking_metrics import RankingMetricResult


def _ranking(value: float | None = 0.5) -> RankingMetricResult:
    return RankingMetricResult(
        value=value,
        total_slates=100,
        scored_slates=30,
        skipped_zero_click_slates=70,
        coverage=0.3,
    )


def _probability(
    *,
    grouped_value: float | None = 0.5,
    pr_auc: float | None = 0.5,
    log_loss: float | None = 0.4,
    brier: float | None = 0.2,
) -> ProbabilityMetricResult:
    return ProbabilityMetricResult(
        row_count=100,
        positive_count=50,
        negative_count=50,
        roc_auc=0.5,
        pr_auc=pr_auc,
        log_loss=log_loss,
        brier=brier,
        grouped_roc_auc=GroupedRocAuc(
            value=grouped_value,
            total_groups=100,
            scored_groups=30,
            skipped_groups=70,
            null_key_rows=0,
        ),
    )


def _score(
    *,
    ndcg_at_10: float | None = 0.5,
    recall_at_10: float | None = 0.5,
    ndcg_at_24: float | None = 0.5,
    grouped_roc_auc: float | None = 0.5,
    pr_auc: float | None = 0.5,
    log_loss: float | None = 0.4,
    brier: float | None = 0.2,
    evaluation_id: str = "eval_" + "a" * 64,
) -> JudgeScoringResult:
    return JudgeScoringResult(
        evaluation_id=EvaluationId(evaluation_id),
        row_count=100,
        ndcg_at_10=_ranking(ndcg_at_10),
        recall_at_10=_ranking(recall_at_10),
        ndcg_at_24=_ranking(ndcg_at_24),
        probability=_probability(
            grouped_value=grouped_roc_auc,
            pr_auc=pr_auc,
            log_loss=log_loss,
            brier=brier,
        ),
    )


def _pair(seed: int, *, primary_delta: float = 0.03) -> PairedJudgeResult:
    return PairedJudgeResult(
        seed=seed,
        baseline=_score(),
        candidate=_score(ndcg_at_10=0.5 + primary_delta),
    )


def _sigmas(value: float = 0.01) -> dict[str, float]:
    return {metric.value: value for metric in JudgeMetric}


def test_screening_requires_strict_primary_improvement() -> None:
    tied = screen_candidate(_pair(11, primary_delta=0.0))
    improved = screen_candidate(_pair(11, primary_delta=0.001))

    assert tied.should_confirm is False
    assert tied.reason_code is JudgeReasonCode.PRIMARY_NOT_IMPROVED
    assert improved.should_confirm is True
    assert improved.reason_code is JudgeReasonCode.CONFIRMATION_REQUIRED


def test_confirmation_promotes_at_exact_primary_and_guardrail_boundaries() -> None:
    baseline = _score(log_loss=0.5, brier=0.5)
    candidate = _score(
        ndcg_at_10=0.625,
        recall_at_10=0.4375,
        ndcg_at_24=0.4375,
        grouped_roc_auc=0.4375,
        pr_auc=0.4375,
        log_loss=0.5625,
        brier=0.5625,
    )
    pairs = tuple(
        PairedJudgeResult(seed=seed, baseline=baseline, candidate=candidate)
        for seed in range(5)
    )

    result = compare_confirmation(pairs, baseline_sigmas=_sigmas(0.0625))

    assert result.decision is JudgeDecision.PROMOTE
    assert result.reason_code is JudgeReasonCode.PROMOTION_THRESHOLD_MET
    assert result.delta_for(JudgeMetric.NDCG_AT_10) == 0.125
    assert result.delta_for(JudgeMetric.LOG_LOSS) == -0.0625
    assert result.delta_for(JudgeMetric.BRIER) == -0.0625


def test_confirmation_discards_just_below_primary_threshold() -> None:
    pairs = tuple(_pair(seed, primary_delta=0.019999) for seed in range(5))

    result = compare_confirmation(pairs, baseline_sigmas=_sigmas())

    assert result.decision is JudgeDecision.DISCARD
    assert result.reason_code is JudgeReasonCode.PRIMARY_THRESHOLD_NOT_MET


def test_confirmation_revises_when_guardrail_crosses_negative_sigma() -> None:
    baseline = _score()
    candidate = _score(ndcg_at_10=0.52, recall_at_10=0.489999)
    pairs = tuple(
        PairedJudgeResult(seed=seed, baseline=baseline, candidate=candidate)
        for seed in range(5)
    )

    result = compare_confirmation(pairs, baseline_sigmas=_sigmas())

    assert result.decision is JudgeDecision.REVISE
    assert result.reason_code is JudgeReasonCode.GUARDRAIL_REGRESSION


@pytest.mark.parametrize("sigma", [0.0, 1e-6, -0.01, float("nan")])
def test_confirmation_rejects_unusable_baseline_sigma(sigma: float) -> None:
    sigmas = _sigmas()
    sigmas[JudgeMetric.NDCG_AT_10.value] = sigma

    result = compare_confirmation(
        tuple(_pair(seed) for seed in range(5)),
        baseline_sigmas=sigmas,
    )

    assert result.decision is None
    assert result.reason_code is JudgeReasonCode.INSUFFICIENT_BASELINE_NOISE


def test_confirmation_accepts_sigma_just_above_resolution() -> None:
    result = compare_confirmation(
        tuple(_pair(seed, primary_delta=0.000003) for seed in range(5)),
        baseline_sigmas=_sigmas(0.0000011),
    )

    assert result.decision is JudgeDecision.PROMOTE


@pytest.mark.parametrize(
    "sigmas",
    [
        {},
        {**_sigmas(), "unexpected": 0.01},
        {**_sigmas(), JudgeMetric.NDCG_AT_10.value: "not-a-number"},
    ],
)
def test_confirmation_rejects_malformed_sigma_map(
    sigmas: dict[str, object],
) -> None:
    result = compare_confirmation(  # type: ignore[arg-type]
        tuple(_pair(seed) for seed in range(5)),
        baseline_sigmas=sigmas,
    )

    assert result.decision is None
    assert result.reason_code is JudgeReasonCode.INVALID_COMPARISON_INPUT


@pytest.mark.parametrize(
    "score",
    [
        _score(ndcg_at_10=None),
        _score(ndcg_at_24=None),
        _score(grouped_roc_auc=None),
        _score(pr_auc=None),
        _score(log_loss=None),
        _score(brier=None),
    ],
)
def test_screening_fails_closed_when_required_metric_is_unavailable(
    score: JudgeScoringResult,
) -> None:
    pair = PairedJudgeResult(seed=7, baseline=_score(), candidate=score)

    result = screen_candidate(pair)

    assert result.should_confirm is False
    assert result.reason_code is JudgeReasonCode.METRIC_UNAVAILABLE


def test_screening_rejects_ranking_coverage_below_count_floor() -> None:
    candidate = replace(
        _score(ndcg_at_10=0.6),
        ndcg_at_10=RankingMetricResult(
            value=0.6,
            total_slates=100,
            scored_slates=29,
            skipped_zero_click_slates=71,
            coverage=0.29,
        ),
    )

    result = screen_candidate(
        PairedJudgeResult(seed=7, baseline=_score(), candidate=candidate)
    )

    assert result.should_confirm is False
    assert result.reason_code is JudgeReasonCode.INSUFFICIENT_METRIC_COVERAGE


def test_screening_rejects_grouped_coverage_below_ratio_floor() -> None:
    probability = replace(
        _probability(),
        grouped_roc_auc=GroupedRocAuc(
            value=0.5,
            total_groups=200,
            scored_groups=39,
            skipped_groups=161,
            null_key_rows=0,
        ),
    )
    candidate = replace(_score(ndcg_at_10=0.6), probability=probability)

    result = screen_candidate(
        PairedJudgeResult(seed=7, baseline=_score(), candidate=candidate)
    )

    assert result.should_confirm is False
    assert result.reason_code is JudgeReasonCode.INSUFFICIENT_METRIC_COVERAGE


@pytest.mark.parametrize(
    "probability",
    [
        replace(_probability(), row_count=99),
        replace(_probability(), positive_count=0, negative_count=100),
        replace(_probability(), negative_count=0, positive_count=100),
    ],
)
def test_screening_requires_full_item_and_binary_label_coverage(
    probability: ProbabilityMetricResult,
) -> None:
    candidate = replace(_score(ndcg_at_10=0.6), probability=probability)

    result = screen_candidate(
        PairedJudgeResult(seed=7, baseline=_score(), candidate=candidate)
    )

    assert result.should_confirm is False
    assert result.reason_code is JudgeReasonCode.INSUFFICIENT_METRIC_COVERAGE


def test_confirmation_requires_five_unique_paired_seeds() -> None:
    four = tuple(_pair(seed) for seed in range(4))
    duplicate = tuple(_pair(seed) for seed in (0, 1, 2, 3, 3))

    for pairs in (four, duplicate):
        result = compare_confirmation(pairs, baseline_sigmas=_sigmas())
        assert result.decision is None
        assert result.reason_code is JudgeReasonCode.INVALID_COMPARISON_INPUT


def test_screening_rejects_different_evaluation_targets() -> None:
    pair = PairedJudgeResult(
        seed=7,
        baseline=_score(),
        candidate=_score(
            ndcg_at_10=0.6,
            evaluation_id="eval_" + "b" * 64,
        ),
    )

    result = screen_candidate(pair)

    assert result.should_confirm is False
    assert result.reason_code is JudgeReasonCode.INVALID_COMPARISON_INPUT
