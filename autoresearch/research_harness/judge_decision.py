"""Sealed Judge metric의 coverage·sigma 기반 결정론적 판정을 계산한다.

[파이프라인] Judge가 baseline/candidate prediction을 채점한 뒤, Controller가 확인 실험과
champion 교체를 결정하기 전에 metric 유효성과 paired delta를 판정하는 구간을 담당한다.

[기능] same-seed screening 비용 gate와 5-seed confirmation의 방향 정규화 delta,
``promote | revise | discard`` 및 안정적인 reason code를 반환한다.

[비책임] prediction 파일 봉인·파싱·metric 계산은 ``prediction_ingestion``과 ``judge``가,
실제 baseline sigma 측정은 Task 7이, 반복 실행은 후속 Controller가 담당한다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum, unique
from math import ceil, fsum, isclose, isfinite

from autoresearch.research_harness.judge import JudgeScoringResult


_MIN_COVERAGE_RATIO = 0.20
_MIN_COVERAGE_COUNT = 30
_MIN_SIGMA = 1e-6
_CONFIRMATION_SEEDS = 5


@unique
class JudgeMetric(StrEnum):
    """P0-2C 판정에 참여하는 primary와 guardrail 지표."""

    NDCG_AT_10 = "ndcg_at_10"
    RECALL_AT_10 = "recall_at_10"
    NDCG_AT_24 = "ndcg_at_24"
    GROUPED_ROC_AUC = "grouped_roc_auc"
    PR_AUC = "pr_auc"
    LOG_LOSS = "log_loss"
    BRIER = "brier"


_PRIMARY = JudgeMetric.NDCG_AT_10
_LOWER_IS_BETTER = frozenset({JudgeMetric.LOG_LOSS, JudgeMetric.BRIER})
_GUARDRAILS = tuple(metric for metric in JudgeMetric if metric is not _PRIMARY)


@unique
class JudgeDecision(StrEnum):
    """유효한 confirmation 비교의 최종 판정."""

    PROMOTE = "promote"
    REVISE = "revise"
    DISCARD = "discard"


@unique
class JudgeReasonCode(StrEnum):
    """Controller와 ledger가 분기할 수 있는 판정 근거 코드."""

    CONFIRMATION_REQUIRED = "confirmation_required"
    PRIMARY_NOT_IMPROVED = "primary_not_improved"
    PROMOTION_THRESHOLD_MET = "promotion_threshold_met"
    GUARDRAIL_REGRESSION = "guardrail_regression"
    PRIMARY_THRESHOLD_NOT_MET = "primary_threshold_not_met"
    INSUFFICIENT_BASELINE_NOISE = "insufficient_baseline_noise"
    METRIC_UNAVAILABLE = "metric_unavailable"
    INSUFFICIENT_METRIC_COVERAGE = "insufficient_metric_coverage"
    INVALID_COMPARISON_INPUT = "invalid_comparison_input"


@dataclass(frozen=True, slots=True)
class PairedJudgeResult:
    """같은 seed에서 얻은 baseline/candidate Judge 결과."""

    seed: int
    baseline: JudgeScoringResult
    candidate: JudgeScoringResult


@dataclass(frozen=True, slots=True)
class MetricDelta:
    """개선이 양수가 되도록 방향을 정규화한 지표 delta."""

    metric: JudgeMetric
    value: float


@dataclass(frozen=True, slots=True)
class ScreeningResult:
    """candidate를 5-seed confirmation으로 보낼지에 대한 비용 gate 결과."""

    should_confirm: bool
    reason_code: JudgeReasonCode
    normalized_deltas: tuple[MetricDelta, ...] = ()


@dataclass(frozen=True, slots=True)
class ConfirmationDecision:
    """유효성 실패를 decision 없음으로 보존하는 confirmation 결과."""

    decision: JudgeDecision | None
    reason_code: JudgeReasonCode
    normalized_deltas: tuple[MetricDelta, ...] = ()

    def delta_for(self, metric: JudgeMetric) -> float:
        """요청 지표의 정규화 delta를 반환한다."""

        for item in self.normalized_deltas:
            if item.metric is metric:
                return item.value
        raise KeyError(metric.value)


def screen_candidate(pair: PairedJudgeResult) -> ScreeningResult:
    """same-seed candidate가 primary를 엄격히 개선했는지 판정한다."""

    invalid = _pair_invalid_reason(pair)
    if invalid is not None:
        return ScreeningResult(False, invalid)
    deltas = _normalized_deltas(pair)
    primary_delta = _delta_value(deltas, _PRIMARY)
    if primary_delta > 0.0:
        return ScreeningResult(
            True,
            JudgeReasonCode.CONFIRMATION_REQUIRED,
            deltas,
        )
    return ScreeningResult(
        False,
        JudgeReasonCode.PRIMARY_NOT_IMPROVED,
        deltas,
    )


def compare_confirmation(
    pairs: Sequence[PairedJudgeResult],
    *,
    baseline_sigmas: Mapping[str, float],
) -> ConfirmationDecision:
    """5개 paired 결과 평균을 지표별 baseline sigma에 비교한다."""

    if (
        len(pairs) != _CONFIRMATION_SEEDS
        or len({pair.seed for pair in pairs}) != _CONFIRMATION_SEEDS
        or len(
            {
                score.evaluation_id
                for pair in pairs
                for score in (pair.baseline, pair.candidate)
            }
        )
        != 1
    ):
        return ConfirmationDecision(
            None,
            JudgeReasonCode.INVALID_COMPARISON_INPUT,
        )
    for pair in pairs:
        invalid = _pair_invalid_reason(pair)
        if invalid is not None:
            return ConfirmationDecision(None, invalid)

    try:
        sigmas = _validated_sigmas(baseline_sigmas)
    except (KeyError, TypeError, ValueError):
        return ConfirmationDecision(
            None,
            JudgeReasonCode.INVALID_COMPARISON_INPUT,
        )
    if sigmas is None:
        return ConfirmationDecision(
            None,
            JudgeReasonCode.INSUFFICIENT_BASELINE_NOISE,
        )

    paired_deltas = tuple(_normalized_deltas(pair) for pair in pairs)
    mean_deltas = tuple(
        MetricDelta(
            metric=metric,
            value=fsum(
                _delta_value(deltas, metric) for deltas in paired_deltas
            )
            / _CONFIRMATION_SEEDS,
        )
        for metric in JudgeMetric
    )
    primary_delta = _delta_value(mean_deltas, _PRIMARY)
    if not _at_least(primary_delta, 2.0 * sigmas[_PRIMARY]):
        return ConfirmationDecision(
            JudgeDecision.DISCARD,
            JudgeReasonCode.PRIMARY_THRESHOLD_NOT_MET,
            mean_deltas,
        )
    if any(
        not _at_least(_delta_value(mean_deltas, metric), -sigmas[metric])
        for metric in _GUARDRAILS
    ):
        return ConfirmationDecision(
            JudgeDecision.REVISE,
            JudgeReasonCode.GUARDRAIL_REGRESSION,
            mean_deltas,
        )
    return ConfirmationDecision(
        JudgeDecision.PROMOTE,
        JudgeReasonCode.PROMOTION_THRESHOLD_MET,
        mean_deltas,
    )


def _pair_invalid_reason(pair: PairedJudgeResult) -> JudgeReasonCode | None:
    if (
        isinstance(pair.seed, bool)
        or not isinstance(pair.seed, int)
        or pair.baseline.evaluation_id != pair.candidate.evaluation_id
        or pair.baseline.row_count != pair.candidate.row_count
        or pair.baseline.row_count <= 0
    ):
        return JudgeReasonCode.INVALID_COMPARISON_INPUT
    for score in (pair.baseline, pair.candidate):
        reason = _score_invalid_reason(score)
        if reason is not None:
            return reason
    return None


def _score_invalid_reason(score: JudgeScoringResult) -> JudgeReasonCode | None:
    values = _metric_values(score)
    if (
        score.probability.roc_auc is None
        or not isfinite(score.probability.roc_auc)
        or any(
            value is None or not isfinite(value) for value in values.values()
        )
    ):
        return JudgeReasonCode.METRIC_UNAVAILABLE

    for ranking in (score.ndcg_at_10, score.recall_at_10, score.ndcg_at_24):
        required = max(
            _MIN_COVERAGE_COUNT,
            ceil(ranking.total_slates * _MIN_COVERAGE_RATIO),
        )
        if ranking.scored_slates < required:
            return JudgeReasonCode.INSUFFICIENT_METRIC_COVERAGE

    probability = score.probability
    grouped = probability.grouped_roc_auc
    if grouped is None:
        return JudgeReasonCode.METRIC_UNAVAILABLE
    required_groups = max(
        _MIN_COVERAGE_COUNT,
        ceil(grouped.total_groups * _MIN_COVERAGE_RATIO),
    )
    if (
        grouped.scored_groups < required_groups
        or grouped.null_key_rows != 0
        or probability.row_count != score.row_count
        or probability.positive_count <= 0
        or probability.negative_count <= 0
        or probability.positive_count + probability.negative_count
        != probability.row_count
    ):
        return JudgeReasonCode.INSUFFICIENT_METRIC_COVERAGE
    return None


def _metric_values(score: JudgeScoringResult) -> dict[JudgeMetric, float | None]:
    grouped = score.probability.grouped_roc_auc
    return {
        JudgeMetric.NDCG_AT_10: score.ndcg_at_10.value,
        JudgeMetric.RECALL_AT_10: score.recall_at_10.value,
        JudgeMetric.NDCG_AT_24: score.ndcg_at_24.value,
        JudgeMetric.GROUPED_ROC_AUC: grouped.value if grouped is not None else None,
        JudgeMetric.PR_AUC: score.probability.pr_auc,
        JudgeMetric.LOG_LOSS: score.probability.log_loss,
        JudgeMetric.BRIER: score.probability.brier,
    }


def _normalized_deltas(pair: PairedJudgeResult) -> tuple[MetricDelta, ...]:
    baseline_values = _metric_values(pair.baseline)
    candidate_values = _metric_values(pair.candidate)
    return tuple(
        MetricDelta(
            metric=metric,
            value=(
                float(baseline_values[metric]) - float(candidate_values[metric])
                if metric in _LOWER_IS_BETTER
                else float(candidate_values[metric]) - float(baseline_values[metric])
            ),
        )
        for metric in JudgeMetric
    )


def _validated_sigmas(
    raw: Mapping[str, float],
) -> dict[JudgeMetric, float] | None:
    if set(raw) != {metric.value for metric in JudgeMetric}:
        raise ValueError("baseline sigma keys do not match required metrics")
    validated: dict[JudgeMetric, float] = {}
    for metric in JudgeMetric:
        value = raw[metric.value]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("baseline sigma must be numeric")
        if not isfinite(value) or value <= _MIN_SIGMA:
            return None
        validated[metric] = float(value)
    return validated


def _delta_value(deltas: Sequence[MetricDelta], metric: JudgeMetric) -> float:
    return next(item.value for item in deltas if item.metric is metric)


def _at_least(value: float, threshold: float) -> bool:
    return value > threshold or isclose(value, threshold, rel_tol=1e-12, abs_tol=1e-15)
