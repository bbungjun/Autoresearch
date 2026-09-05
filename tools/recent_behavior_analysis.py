"""#105 날짜 분리 실험의 피처 감사와 paired 판정을 제공한다.

[파이프라인] 선택된 과거 학습 입력 및 Judge 평가 출력의 실험 분석 구간이다.
[기능] 현재·미래 학습 이력 제거 전후의 피처 동일성과 학습 가능 조건을 검증하고,
36개 고정 관측으로 최근 행동의 기여를 split/world 단위로 계산한다.
[비책임] 이벤트 생성, label 접근, 학습·final 소비와 production 승격은 하지 않는다.
"""

from datetime import UTC, date, datetime, time, timedelta

import pyarrow as pa
import pyarrow.compute as pc

from autoresearch.research_harness.embedding import TextEmbedder
from autoresearch.research_harness.local_features import build_local_features
from autoresearch.research_harness.local_training import LocalTrainingInput
from autoresearch.research_harness.personalization_ablation import (
    ABLATION_FEATURE_GROUPS, _coverage_is_valid, _nested_metric, _valid_evaluation_id,
    feature_columns_for_arm,
)
from tools.popularity_recall_analysis import METRICS, stats


WORLD_SEEDS = (10501, 10502, 10503)
TRAINING_SEEDS = (301, 302, 303)
SPLITS = ("validation", "final_holdout")
ARMS = ("with_recent", "without_recent")
RECENT = ABLATION_FEATURE_GROUPS["without_recent_behavior"]


def columns(arm: str) -> tuple[str, ...]:
    """15피처 기준선과 최근 행동을 추가 제거한 10피처 열을 반환한다."""
    if arm not in ARMS:
        raise ValueError("recent_behavior_arm_invalid")
    full = feature_columns_for_arm("without_video_popularity")
    return full if arm == "with_recent" else tuple(c for c in full if c not in RECENT)


def audit_training_features(inputs: LocalTrainingInput, embedding: TextEmbedder, day: date) -> dict:
    """실제 학습 날 이후 모든 행동을 제거해도 피처가 동일한지 감사한다."""
    requests = pa.table({"user_id": [r.user_id for r in inputs.training_rows],
                         "video_id": [r.video_id for r in inputs.training_rows],
                         "event_timestamp": [r.event_timestamp for r in inputs.training_rows]})
    cutoff = datetime.combine(day, time(), tzinfo=UTC) - timedelta(hours=9)
    past = inputs.history.filter(pc.less(inputs.history["event_timestamp"], pa.scalar(cutoff)))
    kwargs = dict(users=inputs.users, videos=inputs.videos, embedding=embedding,
                  evaluation_start_date=inputs.manifest.evaluation_start_date,
                  history_start_date=inputs.manifest.history_partitions[0].dt)
    original = build_local_features(requests, history=inputs.history, **kwargs)
    restricted = build_local_features(requests, history=past, **kwargs)
    if not original.features.equals(restricted.features):
        raise ValueError("training_future_history_influences_features")
    return {"training_day": str(day), "cutoff_utc": cutoff.isoformat(),
            "history_rows": len(inputs.history), "strictly_prior_rows": len(past),
            "removed_same_day_or_later_rows": len(inputs.history)-len(past),
            "all_training_features_unchanged": True}


def informative(diagnostics: dict) -> bool:
    """모든 fit subset의 선행 이력과 최소 한 최근 피처의 변동을 요구한다."""
    availability = diagnostics["availability"]["training"]
    return (availability["rows"] > 0
            and availability["history_30d_complete"] == availability["rows"]
            and availability["history_7d_complete"] == availability["rows"]
            and set(diagnostics["fit_by_seed"]) == set(map(str, TRAINING_SEEDS))
            and all(any(fit[n]["unique"] > 1 for n in RECENT)
                    for fit in diagnostics["fit_by_seed"].values()))


def summarize(observations: list[dict]) -> dict:
    """36개 고정 관측의 with_recent−without_recent 방향 보정 변화를 계산한다."""
    expected = {(w, s, t, a) for w in WORLD_SEEDS for s in SPLITS for t in TRAINING_SEEDS for a in ARMS}
    indexed = {}
    for row in observations:
        key = tuple(row.get(k) for k in ("world_seed", "split", "training_seed", "arm"))
        if key not in expected or key in indexed:
            raise ValueError("recent_behavior_grid_invalid")
        indexed[key] = row["metrics"]
    if indexed.keys() != expected:
        raise ValueError("recent_behavior_grid_invalid")
    identities = set()
    for w in WORLD_SEEDS:
        for s in SPLITS:
            group = [indexed[w, s, t, a] for t in TRAINING_SEEDS for a in ARMS]
            ids = {m.get("evaluation_id") for m in group}
            if (len(ids) != 1 or not _valid_evaluation_id(str(next(iter(ids))))
                    or len({m.get("row_count") for m in group}) != 1):
                raise ValueError("recent_behavior_pair_identity_invalid")
            identities.update(ids)
    if len(identities) != 6:
        raise ValueError("recent_behavior_pair_identity_invalid")
    result = {"coverage_valid": all(_coverage_is_valid(m) for m in indexed.values()), "splits": {}}
    for s in SPLITS:
        means = {a: {m: stats([_nested_metric(indexed[w, s, t, a], p)
                               for w in WORLD_SEEDS for t in TRAINING_SEEDS])
                     for m, p in METRICS.items()} for a in ARMS}
        paired = {}
        for m, path in METRICS.items():
            sign = -1 if m in ("log_loss", "brier") else 1
            by_world = {str(w): [sign * (_nested_metric(indexed[w, s, t, ARMS[0]], path)
                                        - _nested_metric(indexed[w, s, t, ARMS[1]], path))
                                 for t in TRAINING_SEEDS] for w in WORLD_SEEDS}
            world_means = {w: sum(v)/len(v) for w, v in by_world.items()}
            paired[m] = {**stats([v for values in by_world.values() for v in values]),
                         "world_means": world_means, "world_mean_stats": stats(list(world_means.values()))}
        ndcg = paired["ndcg_at_10"]
        checks = {"ndcg_mean_positive": ndcg["mean"] > 0,
                  "ndcg_positive_pairs_at_least_six": ndcg["positive_count"] >= 6,
                  "ndcg_positive_worlds_at_least_two": ndcg["world_mean_stats"]["positive_count"] >= 2,
                  "guardrails_nonnegative": all(v["mean"] >= 0 for m, v in paired.items() if m != "ndcg_at_10")}
        result["splits"][s] = {"arm_metrics": means, "paired_directional_deltas": paired, "checks": checks}
    result["verdict"] = "supported" if result["coverage_valid"] and all(
        all(v["checks"].values()) for v in result["splits"].values()) else "not_supported"
    return result
