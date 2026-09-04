"""#16 개인화 비교군과 피처군 ablation의 고정 실험 계약을 제공한다.

[파이프라인] candidate-safe feature batch와 Sealed Judge 사이에서 비교할 학습 입력과
heuristic 점수 정규화를 정의한다.

[기능] 비교군/ablation arm과 각 학습 arm의 열 projection을 고정하고, slate별 원시
점수를 확률 범위로 결정론적으로 정규화한다.

[비책임] fixture 생성, 모델 학습, final holdout 소비, metric 계산과 결과 보고는 각각
실행기, ``local_training``, 소비 registry와 Sealed Judge가 담당한다.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import asdict
from statistics import fmean, stdev
from typing import Final

import pyarrow as pa

from autoresearch.feature_engineering.model_contract import MODEL_FEATURE_COLUMNS
from autoresearch.research_harness.candidate_metadata import select_metadata_as_of
from autoresearch.research_harness.judge import JudgeScoringResult
from autoresearch.research_harness.local_training import LocalTrainingInput


COMPARISON_ARMS: Final[tuple[str, ...]] = (
    "trending",
    "popularity",
    "video_only_lgbm",
    "personalized_lgbm",
    "oracle_upper_bound",
)

VIDEO_ONLY_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "category_id",
    "duration_sec",
    "view_count",
    "like_ratio",
    "comment_ratio",
    "days_since_upload",
    "channel_subscriber_count",
    "channel_view_count",
    "channel_video_count",
)

ABLATION_FEATURE_GROUPS: Final[dict[str, frozenset[str]]] = {
    "without_user_static": frozenset({"age_group", "occupation", "watch_time_band"}),
    "without_recent_behavior": frozenset(
        {
            "recent_click_count_7d",
            "recent_view_count_7d",
            "recent_watch_time_7d",
            "recent_like_count_7d",
            "total_event_count_7d",
        }
    ),
    "without_category_match": frozenset(
        {
            "historical_category_affinity",
            "preferred_category_match",
            "historical_category_match",
        }
    ),
    "without_topic_similarity": frozenset({"topic_similarity"}),
    "without_video_popularity": frozenset(
        {
            "view_count",
            "like_ratio",
            "comment_ratio",
            "channel_subscriber_count",
            "channel_view_count",
            "channel_video_count",
        }
    ),
}


def feature_columns_for_arm(arm: str) -> tuple[str, ...]:
    """학습 arm이 사용할 canonical-order feature projection을 반환한다."""

    if arm == "video_only_lgbm":
        return VIDEO_ONLY_FEATURE_COLUMNS
    if arm == "personalized_lgbm":
        return MODEL_FEATURE_COLUMNS
    removed = ABLATION_FEATURE_GROUPS.get(arm)
    if removed is None:
        raise ValueError("personalization_ablation_arm_invalid")
    return tuple(name for name in MODEL_FEATURE_COLUMNS if name not in removed)


def normalize_scores_by_slate(
    slate_ids: Sequence[str],
    values: Sequence[float | int],
    *,
    higher_is_better: bool,
) -> tuple[float, ...]:
    """각 slate 안에서 원시 점수를 [0, 1]로 min-max 정규화한다."""

    if len(slate_ids) != len(values) or not slate_ids:
        raise ValueError("personalization_ablation_scores_invalid")
    numeric = tuple(float(value) for value in values)
    if any(not slate_id or slate_id != slate_id.strip() for slate_id in slate_ids):
        raise ValueError("personalization_ablation_scores_invalid")
    if any(not math.isfinite(value) for value in numeric):
        raise ValueError("personalization_ablation_scores_invalid")

    positions: dict[str, list[int]] = {}
    for index, slate_id in enumerate(slate_ids):
        positions.setdefault(slate_id, []).append(index)

    normalized = [0.0] * len(numeric)
    for indexes in positions.values():
        slate_values = [numeric[index] for index in indexes]
        low = min(slate_values)
        high = max(slate_values)
        if low == high:
            for index in indexes:
                normalized[index] = 0.5
            continue
        width = high - low
        for index in indexes:
            score = (numeric[index] - low) / width
            normalized[index] = score if higher_is_better else 1.0 - score
    return tuple(normalized)


def heuristic_predictions(inputs: LocalTrainingInput, arm: str) -> pa.Table:
    """candidate-safe slate에서 Trending 또는 popularity prediction을 만든다."""

    slate_ids = tuple(inputs.slate["slate_id"].to_pylist())
    if arm == "trending":
        raw = inputs.slate["original_rank"].to_pylist()
        if all(value is None for value in raw):
            raw = synthetic_fixture_trending_ranks(inputs.slate["video_id"].to_pylist())
        elif any(value is None for value in raw):
            raise ValueError("personalization_ablation_scores_invalid")
        scores = normalize_scores_by_slate(slate_ids, raw, higher_is_better=False)
    elif arm == "popularity":
        requests = inputs.slate.select(["video_id", "event_timestamp"])
        metadata = select_metadata_as_of(inputs.videos, requests, entity_key="video_id")
        raw = metadata["view_count"].to_pylist()
        raw = [0 if value is None else value for value in raw]
        scores = normalize_scores_by_slate(slate_ids, raw, higher_is_better=True)
    else:
        raise ValueError("personalization_ablation_arm_invalid")
    return inputs.slate.select(["evaluation_id", "slate_id", "video_id"]).append_column(
        "score", pa.array(scores, type=pa.float64())
    )


def synthetic_fixture_trending_ranks(video_ids: Sequence[object]) -> tuple[int, ...]:
    """합성 fixture video ID에 보존된 원천 Trending 행 번호를 복원한다."""

    ranks: list[int] = []
    for video_id in video_ids:
        if not isinstance(video_id, str):
            raise ValueError("personalization_ablation_scores_invalid")
        match = re.fullmatch(r"fixture-video-\d{8}-(\d{4})", video_id)
        if match is None:
            raise ValueError("personalization_ablation_scores_invalid")
        ranks.append(int(match.group(1)) + 1)
    if not ranks:
        raise ValueError("personalization_ablation_scores_invalid")
    return tuple(ranks)


def scoring_result_dict(result: JudgeScoringResult) -> dict[str, object]:
    """Judge 결과를 경로·원시 행 없이 JSON 호환 dict로 변환한다."""

    return asdict(result)


def summarize_observations(observations: Sequence[dict[str, object]]) -> dict[str, object]:
    """고정 paired observation으로 delta, 분산과 사전 등록 판정을 계산한다."""

    indexed: dict[tuple[int, str, int, str], dict[str, object]] = {}
    for observation in observations:
        key = (
            int(observation["world_seed"]),
            str(observation["split"]),
            int(observation["training_seed"]),
            str(observation["arm"]),
        )
        if key in indexed or not isinstance(observation.get("metrics"), dict):
            raise ValueError("personalization_ablation_observations_invalid")
        indexed[key] = observation

    paired: dict[str, dict[str, object]] = {}
    comparisons = {
        "personalized_minus_trending": ("personalized_lgbm", "trending"),
        "personalized_minus_video_only": ("personalized_lgbm", "video_only_lgbm"),
        **{
            f"personalized_minus_{arm}": ("personalized_lgbm", arm)
            for arm in ABLATION_FEATURE_GROUPS
        },
    }
    metric_paths = {
        "ndcg_at_10": ("ndcg_at_10", "value", 1.0),
        "recall_at_10": ("recall_at_10", "value", 1.0),
        "ndcg_at_24": ("ndcg_at_24", "value", 1.0),
        "grouped_roc_auc": ("probability", "grouped_roc_auc", "value", 1.0),
        "pr_auc": ("probability", "pr_auc", 1.0),
        "log_loss": ("probability", "log_loss", -1.0),
        "brier": ("probability", "brier", -1.0),
    }
    base_keys = sorted({key[:3] for key in indexed})
    for comparison, (left_arm, right_arm) in comparisons.items():
        metric_summary: dict[str, object] = {}
        for metric, path in metric_paths.items():
            direction = float(path[-1])
            keys = tuple(str(item) for item in path[:-1])
            deltas: list[float] = []
            for base_key in base_keys:
                left = indexed.get((*base_key, left_arm))
                right = indexed.get((*base_key, right_arm))
                if left is None or right is None:
                    raise ValueError("personalization_ablation_observations_invalid")
                left_value = _nested_metric(left["metrics"], keys)
                right_value = _nested_metric(right["metrics"], keys)
                deltas.append(direction * (left_value - right_value))
            metric_summary[metric] = {
                "mean_directional_delta": fmean(deltas),
                "sample_stddev": stdev(deltas) if len(deltas) > 1 else 0.0,
                "positive_count": sum(delta > 0 for delta in deltas),
                "pair_count": len(deltas),
            }
        paired[comparison] = metric_summary

    coverage_valid = all(_coverage_is_valid(observation["metrics"]) for observation in observations)
    required = paired["personalized_minus_video_only"]
    primary_comparisons = (
        paired["personalized_minus_trending"]["ndcg_at_10"],
        required["ndcg_at_10"],
    )
    split_primary = all(
        _split_delta_positive(indexed, split, left, right)
        for split in ("validation", "final_holdout")
        for left, right in (
            ("personalized_lgbm", "trending"),
            ("personalized_lgbm", "video_only_lgbm"),
        )
    )
    direction_repeated = all(
        int(item["positive_count"]) * 3 >= int(item["pair_count"]) * 2
        for item in primary_comparisons
    )
    guardrails = all(
        float(required[name]["mean_directional_delta"]) >= 0
        for name in metric_paths
        if name != "ndcg_at_10"
    )
    ablation_contribution = any(
        float(paired[f"personalized_minus_{arm}"]["ndcg_at_10"]["mean_directional_delta"]) > 0
        and int(paired[f"personalized_minus_{arm}"]["ndcg_at_10"]["positive_count"]) * 3
        >= int(paired[f"personalized_minus_{arm}"]["ndcg_at_10"]["pair_count"]) * 2
        for arm in ABLATION_FEATURE_GROUPS
    )
    checks = {
        "split_primary_means_positive": split_primary,
        "primary_positive_direction_at_least_two_thirds": direction_repeated,
        "video_only_guardrails_nonnegative": guardrails,
        "at_least_one_repeated_ablation_contribution": ablation_contribution,
        "all_coverage_valid": coverage_valid,
    }
    return {"paired_deltas": paired, "checks": checks, "verdict": "supported" if all(checks.values()) else "not_supported"}


def _nested_metric(value: object, path: tuple[str, ...]) -> float:
    current = value
    for key in path:
        if not isinstance(current, dict):
            raise ValueError("personalization_ablation_observations_invalid")
        current = current.get(key)
    if isinstance(current, bool) or not isinstance(current, (float, int)) or not math.isfinite(float(current)):
        raise ValueError("personalization_ablation_observations_invalid")
    return float(current)


def _coverage_is_valid(metrics: object) -> bool:
    try:
        assert isinstance(metrics, dict)
        probability = metrics["probability"]
        assert isinstance(probability, dict)
        grouped = probability["grouped_roc_auc"]
        assert isinstance(grouped, dict)
        return (
            _nested_metric(metrics, ("ndcg_at_10", "coverage")) > 0
            and _nested_metric(metrics, ("recall_at_10", "coverage")) > 0
            and _nested_metric(metrics, ("ndcg_at_24", "coverage")) > 0
            and int(probability["positive_count"]) > 0
            and int(probability["negative_count"]) > 0
            and _nested_metric(metrics, ("probability", "grouped_roc_auc", "value")) >= 0
            and int(grouped["scored_groups"]) > 0
            and int(grouped["null_key_rows"]) == 0
        )
    except (AssertionError, KeyError, TypeError, ValueError):
        return False


def _split_delta_positive(
    indexed: dict[tuple[int, str, int, str], dict[str, object]],
    split: str,
    left_arm: str,
    right_arm: str,
) -> bool:
    deltas = []
    bases = sorted({key[:3] for key in indexed if key[1] == split})
    for base in bases:
        left = indexed.get((*base, left_arm))
        right = indexed.get((*base, right_arm))
        if left is None or right is None:
            return False
        deltas.append(
            _nested_metric(left["metrics"], ("ndcg_at_10", "value"))
            - _nested_metric(right["metrics"], ("ndcg_at_10", "value"))
        )
    return bool(deltas) and fmean(deltas) > 0


_MODEL_FEATURE_SET = frozenset(MODEL_FEATURE_COLUMNS)
if (
    not VIDEO_ONLY_FEATURE_COLUMNS
    or len(VIDEO_ONLY_FEATURE_COLUMNS) != len(set(VIDEO_ONLY_FEATURE_COLUMNS))
    or not set(VIDEO_ONLY_FEATURE_COLUMNS).issubset(_MODEL_FEATURE_SET)
    or any(not group or not group.issubset(_MODEL_FEATURE_SET) for group in ABLATION_FEATURE_GROUPS.values())
):
    raise RuntimeError("personalization_ablation_contract_invalid")
