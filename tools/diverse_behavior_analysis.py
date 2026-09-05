"""#113 다양한 행동 ablation의 고정 관측을 판정한다.

[파이프라인] 학습·예측 이후 Sealed Judge 지표의 실험 분석 구간이다.
[기능] 평가 ID와 동일 표본·coverage를 검증하고, validation 유효성 gate 및
36개 관측의 paired 변화와 world 평균으로 사전 등록 판정을 계산한다.
[비책임] 데이터·정답 접근, 학습과 final 소비는 실행기가 담당하며 여기서는 하지 않는다.
"""

from collections.abc import Sequence
import re
from statistics import fmean

from autoresearch.research_harness.personalization_ablation import (
    _coverage_is_valid, _nested_metric, _valid_evaluation_id,
)
from tools.popularity_recall_analysis import METRICS, stats


WORLD_SEEDS = (10901, 10902, 10903)
TRAINING_SEEDS = (401, 402, 403)
SPLITS = ("validation", "final_holdout")
ARMS = ("with_recent", "without_recent")


def _coverage_signature(metrics: dict) -> tuple:
    """예측값에 의존하지 않는 표본 수와 coverage를 비교 가능한 순서로 반환한다."""
    values = []
    for name in ("ndcg_at_10", "recall_at_10", "ndcg_at_24"):
        ranking = metrics.get(name, {})
        if not isinstance(ranking, dict):
            ranking = {}
        values.extend(ranking.get(k) for k in (
            "total_slates", "scored_slates", "skipped_zero_click_slates", "coverage"))
    probability = metrics.get("probability", {})
    if not isinstance(probability, dict):
        probability = {}
    values.extend(probability.get(k) for k in ("row_count", "positive_count", "negative_count"))
    grouped = probability.get("grouped_roc_auc", {})
    if not isinstance(grouped, dict):
        grouped = {}
    values.extend(grouped.get(k) for k in (
        "total_groups", "scored_groups", "skipped_groups", "null_key_rows"))
    return tuple(values)


def _index(observations: Sequence[dict], splits: tuple[str, ...]) -> dict:
    expected = {(w, s, t, a) for w in WORLD_SEEDS for s in splits
                for t in TRAINING_SEEDS for a in ARMS}
    indexed = {}
    for row in observations:
        key = tuple(row.get(k) for k in ("world_seed", "split", "training_seed", "arm"))
        if key not in expected or key in indexed or not isinstance(row.get("metrics"), dict):
            raise ValueError("diverse_behavior_grid_invalid")
        indexed[key] = row
    if indexed.keys() != expected:
        raise ValueError("diverse_behavior_grid_invalid")
    identities = set()
    for w in WORLD_SEEDS:
        for s in splits:
            group = [indexed[w, s, t, a] for t in TRAINING_SEEDS for a in ARMS]
            first = group[0]
            metrics = first["metrics"]
            evaluation_id = metrics.get("evaluation_id")
            row_count = metrics.get("row_count")
            row_hash = first.get("row_keys_sha256")
            if (not isinstance(evaluation_id, str) or not _valid_evaluation_id(evaluation_id)
                    or evaluation_id in identities
                    or type(row_count) is not int or row_count <= 0
                    or not isinstance(row_hash, str) or re.fullmatch(r"[0-9a-f]{64}", row_hash) is None
                    or any(row["metrics"].get("evaluation_id") != evaluation_id
                           or type(row["metrics"].get("row_count")) is not int
                           or row["metrics"].get("row_count") != row_count
                           or row.get("row_keys_sha256") != row_hash for row in group)):
                raise ValueError("diverse_behavior_pair_identity_invalid")
            identities.add(evaluation_id)
            signature = _coverage_signature(metrics)
            if any(_coverage_signature(row["metrics"]) != signature for row in group):
                raise ValueError("diverse_behavior_pair_coverage_invalid")
    return indexed


def validation_gate(observations: Sequence[dict]) -> bool:
    """18개 validation 관측의 유효성만 검증하고 개선 여부와 무관하게 gate를 반환한다.

    그리드·동일 표본 불일치는 ValueError, 표본 부족·비유한 지표는 False다.
    """
    indexed = _index(observations, ("validation",))
    return all(_coverage_is_valid(row["metrics"]) for row in indexed.values())


def summarize(observations: Sequence[dict]) -> dict:
    """고정 36개 관측을 supported/not_supported/uninformative로 판정한다.

    품질 미달에서는 통계 계산을 생략한다. 유효한 관측의 loss 변화는 부호를
    반전해 모든 paired directional delta에서 양수가 개선을 뜻한다.
    """
    indexed = _index(observations, SPLITS)
    coverage_valid = all(_coverage_is_valid(row["metrics"]) for row in indexed.values())
    result = {"coverage_valid": coverage_valid, "splits": {}}
    if not coverage_valid:
        return {**result, "verdict": "uninformative"}
    for split in SPLITS:
        means = {arm: {name: stats([
            _nested_metric(indexed[world, split, seed, arm]["metrics"], path)
            for world in WORLD_SEEDS for seed in TRAINING_SEEDS])
            for name, path in METRICS.items()} for arm in ARMS}
        paired = {}
        for name, path in METRICS.items():
            sign = -1 if name in ("log_loss", "brier") else 1
            by_world = {str(world): [sign * (
                _nested_metric(indexed[world, split, seed, ARMS[0]]["metrics"], path)
                - _nested_metric(indexed[world, split, seed, ARMS[1]]["metrics"], path))
                for seed in TRAINING_SEEDS] for world in WORLD_SEEDS}
            world_means = {world: fmean(values) for world, values in by_world.items()}
            paired[name] = {
                **stats([value for values in by_world.values() for value in values]),
                "world_means": world_means,
                "world_mean_stats": stats(list(world_means.values())),
            }
        ndcg = paired["ndcg_at_10"]
        checks = {
            "ndcg_mean_positive": ndcg["mean"] > 0,
            "ndcg_positive_pairs_at_least_six": ndcg["positive_count"] >= 6,
            "ndcg_positive_worlds_at_least_two": ndcg["world_mean_stats"]["positive_count"] >= 2,
            "guardrails_nonnegative": all(value["mean"] >= 0 for name, value in paired.items()
                                          if name != "ndcg_at_10"),
        }
        result["splits"][split] = {"arm_metrics": means, "paired_directional_deltas": paired,
                                   "checks": checks}
    result["verdict"] = "supported" if all(
        all(split["checks"].values()) for split in result["splits"].values()) else "not_supported"
    return result
