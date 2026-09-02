"""모델 평가와 Research Harness가 공유하는 확률 지표를 계산한다.

[파이프라인] 모델 또는 candidate가 click 확률을 만든 뒤, 승격·Judge 판정이 그 결과를
소비하기 전에 전역·그룹 확률 품질 지표를 계산하는 구간을 담당한다.

[기능] ROC-AUC, PR-AUC, Log Loss, Brier와 그룹별 macro ROC-AUC·coverage를 입력 배열에서
순수 계산해 불변 결과로 반환한다.

[비책임] 모델 예측, downsampling 보정, ranking metric, coverage gate와 승격 판정은 각각
호출자, ``research_harness.ranking_metrics``와 P0-2C가 담당한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    log_loss,
    roc_auc_score,
)


@dataclass(frozen=True, slots=True)
class GroupedRocAuc:
    """그룹별 macro ROC-AUC와 계산 가능성 근거."""

    value: float | None
    total_groups: int
    scored_groups: int
    skipped_groups: int
    null_key_rows: int


@dataclass(frozen=True, slots=True)
class ProbabilityMetricResult:
    """한 score 배열에서 함께 계산한 probability metric 결과."""

    row_count: int
    positive_count: int
    negative_count: int
    roc_auc: float | None
    pr_auc: float | None
    log_loss: float | None
    brier: float | None
    grouped_roc_auc: GroupedRocAuc | None


def probability_metrics(
    labels: Sequence[int],
    scores: Sequence[float],
    groups: Sequence[object] | None,
) -> ProbabilityMetricResult:
    """동일한 labels·scores에서 전역 및 선택적 그룹 확률 지표를 계산한다."""

    if len(labels) != len(scores) or (
        groups is not None and len(labels) != len(groups)
    ):
        raise ValueError("probability metric inputs must have equal lengths")
    if any(label not in (0, 1) for label in labels):
        raise ValueError("probability metric labels must be binary")
    row_count = len(labels)
    positive_count = sum(label == 1 for label in labels)
    negative_count = row_count - positive_count
    has_both_classes = positive_count > 0 and negative_count > 0
    return ProbabilityMetricResult(
        row_count=row_count,
        positive_count=positive_count,
        negative_count=negative_count,
        roc_auc=float(roc_auc_score(labels, scores)) if has_both_classes else None,
        pr_auc=(
            float(average_precision_score(labels, scores))
            if has_both_classes
            else None
        ),
        log_loss=float(log_loss(labels, scores)) if has_both_classes else None,
        brier=float(brier_score_loss(labels, scores)) if has_both_classes else None,
        grouped_roc_auc=(
            grouped_roc_auc(labels, scores, groups) if groups is not None else None
        ),
    )


def grouped_roc_auc(
    labels: Sequence[int],
    scores: Sequence[float],
    groups: Sequence[object],
) -> GroupedRocAuc:
    """그룹 안의 ROC-AUC를 계산해 그룹 동등 가중 macro 평균을 반환한다."""

    frame = pd.DataFrame({"label": labels, "score": scores, "group": groups})
    null_key_rows = int(frame["group"].isna().sum())

    per_group: list[float] = []
    total_groups = 0
    for _, rows in frame.groupby("group", sort=False):
        total_groups += 1
        if rows["label"].nunique() < 2:
            continue
        per_group.append(float(roc_auc_score(rows["label"], rows["score"])))

    scored_groups = len(per_group)
    return GroupedRocAuc(
        value=(sum(per_group) / scored_groups) if scored_groups else None,
        total_groups=total_groups,
        scored_groups=scored_groups,
        skipped_groups=total_groups - scored_groups,
        null_key_rows=null_key_rows,
    )
