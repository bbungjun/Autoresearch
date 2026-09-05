"""#103 실험의 paired 비교와 candidate-safe 피처 진단을 제공한다.

[파이프라인] 로컬 학습 입력과 Sealed Judge 출력 사이의 실험 분석 구간이다.
[기능] 고정 54개 관측의 split/world 통계, Recall 회복과 사전 등록 판정을 계산한다.
학습/예측 피처 분포와 LightGBM importance를 기록하며 진단의 fit seed를 명시할 수 있다.
[비책임] fixture 생성, 정답 접근, 모델 학습, final 소비와 production 승격은 하지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import fmean, pstdev, stdev

import lightgbm as lgb
import numpy as np
import pyarrow as pa
from sklearn.model_selection import train_test_split

from autoresearch.research_harness.local_features import build_local_features
from autoresearch.research_harness.local_training import LocalTrainingInput
from autoresearch.research_harness.personalization_ablation import (
    ABLATION_FEATURE_GROUPS,
    _coverage_is_valid,
    _nested_metric,
    _valid_evaluation_id,
)


WORLD_SEEDS = (10301, 10302, 10303)
TRAINING_SEEDS = (201, 202, 203)
SPLITS = ("validation", "final_holdout")
ARMS = ("personalized_lgbm", "without_video_popularity", "video_only_lgbm")
METRICS = {
    "ndcg_at_10": ("ndcg_at_10", "value"),
    "recall_at_10": ("recall_at_10", "value"),
    "ndcg_at_24": ("ndcg_at_24", "value"),
    "grouped_roc_auc": ("probability", "grouped_roc_auc", "value"),
    "pr_auc": ("probability", "pr_auc"),
    "log_loss": ("probability", "log_loss"),
    "brier": ("probability", "brier"),
}


def stats(values: Sequence[float]) -> dict[str, float | int]:
    """반복 관측의 기술 통계를 반환한다. 독립 표본 추론은 하지 않는다."""
    return {"mean": fmean(values), "sample_stddev": stdev(values),
            "positive_count": sum(x > 0 for x in values), "count": len(values)}


def summarize(observations: Sequence[dict]) -> dict:
    """누락·중복·다른 평가 입력을 거절하고 split별 고정 판정을 반환한다."""
    indexed = {}
    expected = {(w, s, t, a) for w in WORLD_SEEDS for s in SPLITS
                for t in TRAINING_SEEDS for a in ARMS}
    for row in observations:
        key = tuple(row.get(k) for k in ("world_seed", "split", "training_seed", "arm"))
        if key not in expected or key in indexed:
            raise ValueError("confirmation_grid_invalid")
        indexed[key] = row["metrics"]
    if indexed.keys() != expected:
        raise ValueError("confirmation_grid_invalid")
    identities = set()
    for w in WORLD_SEEDS:
        for s in SPLITS:
            group = [indexed[w, s, t, a] for t in TRAINING_SEEDS for a in ARMS]
            ids = {m.get("evaluation_id") for m in group}
            if (len(ids) != 1 or not _valid_evaluation_id(str(next(iter(ids))))
                    or len({m.get("row_count") for m in group}) != 1):
                raise ValueError("confirmation_pair_identity_invalid")
            identities.update(ids)
    if len(identities) != len(WORLD_SEEDS) * len(SPLITS):
        raise ValueError("confirmation_pair_identity_invalid")
    coverage = all(_coverage_is_valid(m) for m in indexed.values())
    result = {"coverage_valid": coverage, "splits": {}}
    for s in SPLITS:
        means = {a: {m: stats([_nested_metric(indexed[w, s, t, a], p)
                               for w in WORLD_SEEDS for t in TRAINING_SEEDS])
                     for m, p in METRICS.items()} for a in ARMS}
        paired = {}
        for name, left, right in (
            ("removal_minus_full", ARMS[1], ARMS[0]),
            ("removal_minus_video_only", ARMS[1], ARMS[2]),
            ("full_minus_video_only", ARMS[0], ARMS[2]),
        ):
            paired[name] = {}
            for m, path in METRICS.items():
                sign = -1 if m in ("log_loss", "brier") else 1
                by_world = {w: [sign * (_nested_metric(indexed[w, s, t, left], path)
                                      - _nested_metric(indexed[w, s, t, right], path))
                                for t in TRAINING_SEEDS] for w in WORLD_SEEDS}
                world_means = [fmean(v) for v in by_world.values()]
                paired[name][m] = {
                    **stats([v for values in by_world.values() for v in values]),
                    "world_means": dict(zip(map(str, WORLD_SEEDS), world_means)),
                    "world_mean_stats": stats(world_means),
                }
        removal = paired["removal_minus_full"]
        ndcg = removal["ndcg_at_10"]
        checks = {"ndcg_mean_positive": ndcg["mean"] > 0,
                  "ndcg_positive_pairs_at_least_six": ndcg["positive_count"] >= 6,
                  "ndcg_positive_worlds_at_least_two": ndcg["world_mean_stats"]["positive_count"] >= 2,
                  "guardrails_nonnegative": all(v["mean"] >= 0 for m, v in removal.items()
                                                if m != "ndcg_at_10")}
        recall_gap = paired["removal_minus_video_only"]["recall_at_10"]["mean"]
        recovery = ("full" if recall_gap >= 0 else
                    "partial" if removal["recall_at_10"]["mean"] > 0 else "none")
        result["splits"][s] = {"arm_metrics": means, "paired_directional_deltas": paired,
                               "checks": checks, "recall_recovery": recovery}
    result["verdict"] = "supported" if coverage and all(
        all(r["checks"].values()) for r in result["splits"].values()) else "not_supported"
    return result


def feature_stats(table: pa.Table) -> dict:
    """수치 피처의 결측·0·분산을 분리해 기록한다."""
    names = sorted(ABLATION_FEATURE_GROUPS["without_video_popularity"]
                   | ABLATION_FEATURE_GROUPS["without_recent_behavior"])
    output = {}
    for name in names:
        raw = table[name].to_pylist()
        values = [float(v) for v in raw if v is not None]
        output[name] = {"rows": len(raw), "null_count": len(raw) - len(values),
                        "zero_count": sum(v == 0 for v in values), "unique": len(set(values)),
                        "min": min(values), "max": max(values), "mean": fmean(values),
                        "population_stddev": pstdev(values)}
    return output


def input_diagnostics(
    inputs: LocalTrainingInput, embedding: object, *, training_seeds: Sequence[int] = TRAINING_SEEDS,
) -> dict:
    """학습 계약과 동일한 fit 행 선택 및 예측 batch를 진단한다."""
    requests = pa.table({"user_id": [r.user_id for r in inputs.training_rows],
                         "video_id": [r.video_id for r in inputs.training_rows],
                         "event_timestamp": [r.event_timestamp for r in inputs.training_rows]})
    kwargs = dict(history=inputs.history, users=inputs.users, videos=inputs.videos,
                  embedding=embedding, evaluation_start_date=inputs.manifest.evaluation_start_date,
                  history_start_date=inputs.manifest.history_partitions[0].dt)
    training = build_local_features(requests, **kwargs)
    prediction = build_local_features(inputs.slate, **kwargs)
    labels = np.asarray([int(r.clicked) for r in inputs.training_rows])
    fit = {}
    for seed in training_seeds:
        train_val, _ = train_test_split(np.arange(len(labels)), test_size=.2,
                                        random_state=seed, stratify=labels)
        train, _ = train_test_split(train_val, test_size=.25, random_state=seed,
                                    stratify=labels[train_val])
        fit[str(seed)] = feature_stats(training.features.take(pa.array(train)))
    return {"training_all": feature_stats(training.features), "fit_by_seed": fit,
            "prediction": feature_stats(prediction.features),
            "availability": {name: {"rows": len(batch.diagnostics), **{
                col: sum(batch.diagnostics[col].to_pylist()) for col in batch.diagnostics.column_names}}
                for name, batch in (("training", training), ("prediction", prediction))}}


def model_importance(model_text: str) -> dict:
    """저장된 실제 Booster의 native gain/split importance를 읽는다."""
    model = lgb.Booster(model_str=model_text)
    return {name: {"gain": float(gain), "split": int(split)}
            for name, gain, split in zip(model.feature_name(), model.feature_importance("gain"),
                                          model.feature_importance("split"), strict=True)}
