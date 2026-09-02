"""결정적 리랭킹 지표를 계산한다.

[파이프라인] Stage C가 만든 평가 fixture와 향후 P0-2B Sealed Judge 사이에서,
이미 정렬된 label·candidate score 입력의 오프라인 랭킹 품질을 계산한다.

[기능] score 내림차순과 video_id 오름차순 tie-break로 slate를 재정렬하고,
binary relevance 기반 NDCG@K·Recall@K 및 zero-click 제외 coverage를 반환한다.

[비책임] prediction CSV 파싱, ``(slate_id, video_id)`` 고유성·score 범위 검증,
coverage gate, sigma 기반 승격 판정과 Judge 파일 I/O는 담당하지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum, unique
from math import fsum, isfinite, log2
from numbers import Real


@unique
class RankingMetricErrorCode(StrEnum):
    """호출자가 분기할 수 있는 안정적인 지표 입력 오류 코드."""

    LENGTH_MISMATCH = "length_mismatch"
    INVALID_K = "invalid_k"
    INVALID_LABEL = "invalid_label"
    INVALID_IDENTIFIER = "invalid_identifier"
    NON_FINITE_SCORE = "non_finite_score"


@dataclass(frozen=True, slots=True)
class RankingMetricError(Exception):
    """원본 평가 값을 노출하지 않는 지표 입력 계약 오류."""

    code: RankingMetricErrorCode
    field: str | None = None
    position: int | None = None

    def __str__(self) -> str:
        details = [self.code.value]
        if self.field is not None:
            details.append(f"field={self.field}")
        if self.position is not None:
            details.append(f"position={self.position}")
        return ": ".join(details)


@dataclass(frozen=True, slots=True)
class RankingMetricResult:
    """zero-click 제외 여부를 함께 보존하는 macro ranking metric 결과."""

    value: float | None
    total_slates: int
    scored_slates: int
    skipped_zero_click_slates: int
    coverage: float


_SlateRow = tuple[float, str, int]
_SlateMetric = Callable[[Sequence[_SlateRow], int, int], float]


def ndcg_at_k(
    labels: Sequence[int],
    scores: Sequence[float],
    slate_ids: Sequence[str],
    video_ids: Sequence[str],
    *,
    k: int,
) -> RankingMetricResult:
    """binary relevance 기준 macro NDCG@K를 계산한다."""

    return _evaluate_ranking_metric(
        labels,
        scores,
        slate_ids,
        video_ids,
        k=k,
        slate_metric=_ndcg_for_slate,
    )


def recall_at_k(
    labels: Sequence[int],
    scores: Sequence[float],
    slate_ids: Sequence[str],
    video_ids: Sequence[str],
    *,
    k: int,
) -> RankingMetricResult:
    """slate의 전체 click 수를 분모로 하는 macro Recall@K를 계산한다."""

    return _evaluate_ranking_metric(
        labels,
        scores,
        slate_ids,
        video_ids,
        k=k,
        slate_metric=_recall_for_slate,
    )


def _evaluate_ranking_metric(
    labels: Sequence[int],
    scores: Sequence[float],
    slate_ids: Sequence[str],
    video_ids: Sequence[str],
    *,
    k: int,
    slate_metric: _SlateMetric,
) -> RankingMetricResult:
    _validate_lengths(labels, scores, slate_ids, video_ids)
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise RankingMetricError(RankingMetricErrorCode.INVALID_K, field="k")

    slates: dict[str, list[_SlateRow]] = {}
    for position, (label, score, slate_id, video_id) in enumerate(
        zip(labels, scores, slate_ids, video_ids, strict=True)
    ):
        if label not in (0, 1):
            raise RankingMetricError(
                RankingMetricErrorCode.INVALID_LABEL,
                field="label",
                position=position,
            )
        _validate_identifier(slate_id, field="slate_id", position=position)
        _validate_identifier(video_id, field="video_id", position=position)
        if isinstance(score, bool) or not isinstance(score, Real) or not isfinite(score):
            raise RankingMetricError(
                RankingMetricErrorCode.NON_FINITE_SCORE,
                field="score",
                position=position,
            )
        slates.setdefault(slate_id, []).append((float(score), video_id, int(label)))

    values: list[float] = []
    skipped_zero_click_slates = 0
    for slate_id in sorted(slates):
        rows = sorted(slates[slate_id], key=lambda row: (-row[0], row[1]))
        total_clicks = sum(row[2] for row in rows)
        if total_clicks == 0:
            skipped_zero_click_slates += 1
            continue
        values.append(slate_metric(rows, k, total_clicks))

    total_slates = len(slates)
    scored_slates = len(values)
    return RankingMetricResult(
        value=fsum(values) / scored_slates if scored_slates else None,
        total_slates=total_slates,
        scored_slates=scored_slates,
        skipped_zero_click_slates=skipped_zero_click_slates,
        coverage=scored_slates / total_slates if total_slates else 0.0,
    )


def _ndcg_for_slate(rows: Sequence[_SlateRow], k: int, total_clicks: int) -> float:
    dcg = fsum(
        row[2] / log2(rank + 1)
        for rank, row in enumerate(rows[:k], start=1)
    )
    ideal_clicks = min(total_clicks, k)
    idcg = fsum(1.0 / log2(rank + 1) for rank in range(1, ideal_clicks + 1))
    return dcg / idcg


def _recall_for_slate(rows: Sequence[_SlateRow], k: int, total_clicks: int) -> float:
    return sum(row[2] for row in rows[:k]) / total_clicks


def _validate_lengths(*values: Sequence[object]) -> None:
    if len({len(value) for value in values}) != 1:
        raise RankingMetricError(RankingMetricErrorCode.LENGTH_MISMATCH)


def _validate_identifier(value: object, *, field: str, position: int) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RankingMetricError(
            RankingMetricErrorCode.INVALID_IDENTIFIER,
            field=field,
            position=position,
        )
