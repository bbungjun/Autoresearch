"""#117 통제 실험의 예측 채점과 사전 등록된 후보 판정을 담당한다.

[파이프라인] 피처·모델 학습과 예측 봉인 이후 오프라인 평가 구간이다.
[기능] 정답과 예측 키의 정확한 대응, raw 순위와 보정 확률의 분리, coverage와
동일 표본을 검증하며 3world×2seed의 paired 변화로 개발 후보를 고정한다.
[비책임] 학습, 보정, 평가 데이터 생성·봉인·소비와 비용 기록은 실행기가 담당한다.
"""

from __future__ import annotations

from math import isfinite
from numbers import Real
import re
from statistics import fmean, stdev

import numpy as np
import pyarrow as pa
from sklearn.metrics import average_precision_score, log_loss, roc_auc_score

from autoresearch.research_harness.ranking_metrics import ndcg_at_k, recall_at_k


WORLDS = (10701, 10702, 10703)
SEEDS = (401, 402)
BASELINE = "baseline15"
CANDIDATES = ("shallow", "ranker", "preference", "larger")
METRICS = ("recall_at_10", "ndcg_at_10", "ndcg_at_24", "grouped_roc_auc",
           "pr_auc", "log_loss", "brier")
_KEYS = ("evaluation_id", "slate_id", "video_id")


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _aligned_labels(keys: pa.Table, labels: pa.Table) -> list[dict]:
    if (not set(_KEYS).issubset(keys.column_names)
            or not {*_KEYS, "user_id", "clicked"}.issubset(labels.column_names)
            or keys.num_rows == 0 or keys.num_rows != labels.num_rows):
        raise ValueError("recall_prediction_keys_invalid")
    indexed = {}
    for row in labels.select([*_KEYS, "user_id", "clicked"]).to_pylist():
        key = tuple(row[name] for name in _KEYS)
        if (not all(_identifier(v) for v in (*key, row["user_id"]))
                or type(row["clicked"]) is not bool or key in indexed):
            raise ValueError("recall_label_keys_invalid")
        indexed[key] = row
    aligned, seen = [], set()
    columns = [*_KEYS] + (["user_id"] if "user_id" in keys.column_names else [])
    for row in keys.select(columns).to_pylist():
        key = tuple(row[name] for name in _KEYS)
        if (not all(_identifier(v) for v in key) or key in seen or key not in indexed
                or ("user_id" in row and row["user_id"] != indexed[key]["user_id"])):
            raise ValueError("recall_prediction_keys_invalid")
        aligned.append(indexed[key])
        seen.add(key)
    evaluation_ids = {row["evaluation_id"] for row in aligned}
    if (len(evaluation_ids) != 1
            or re.fullmatch(r"eval_[0-9a-f]{64}", next(iter(evaluation_ids))) is None):
        raise ValueError("recall_evaluation_id_invalid")
    users_by_slate = {}
    for row in aligned:
        previous = users_by_slate.setdefault(row["slate_id"], row["user_id"])
        if previous != row["user_id"]:
            raise ValueError("recall_slate_user_mismatch")
    return aligned


def _ranking(y: list[int], raw: list[float], slates: list[str], videos: list[str]) -> dict:
    recall = recall_at_k(y, raw, slates, videos, k=10)
    return {
        "recall_at_10": recall.value,
        "ndcg_at_10": ndcg_at_k(y, raw, slates, videos, k=10).value,
        "ndcg_at_24": ndcg_at_k(y, raw, slates, videos, k=24).value,
        "total_slates": recall.total_slates,
        "ranking_slates": recall.scored_slates,
    }


def score_predictions(keys: pa.Table, labels: pa.Table, raw: np.ndarray,
                      prob: np.ndarray) -> dict:
    """정확히 동일한 키 집합을 join하고 raw 순위와 보정 확률 지표를 반환한다.

    비유한/범위 오류 예측과 키 오류는 ValueError로 거절한다. 표본 부족은
    valid=False로 반환하므로 유효한 부정 결과와 구분할 수 있다.
    """
    aligned = _aligned_labels(keys, labels)
    raw, prob = np.asarray(raw, dtype=float), np.asarray(prob, dtype=float)
    if (raw.shape != (len(aligned),) or prob.shape != raw.shape
            or not np.isfinite(raw).all() or not np.isfinite(prob).all()
            or np.any((prob < 0) | (prob > 1))):
        raise ValueError("recall_prediction_values_invalid")
    y = [int(row["clicked"]) for row in aligned]
    slates = [row["slate_id"] for row in aligned]
    videos = [row["video_id"] for row in aligned]
    groups: dict[str, list[int]] = {}
    for i, slate in enumerate(slates):
        groups.setdefault(slate, []).append(i)
    ranking = _ranking(y, raw.tolist(), slates, videos)
    aucs = [float(roc_auc_score([y[i] for i in indices], raw[indices]))
            for indices in groups.values() if len({y[i] for i in indices}) == 2]
    positives = sum(y)
    both_classes = 0 < positives < len(y)
    coverage = {
        "total_slates": len(groups), "ranking_slates": ranking["ranking_slates"],
        "ranking_ratio": ranking["ranking_slates"] / len(groups),
        "auc_slates": len(aucs), "auc_ratio": len(aucs) / len(groups),
        "positive_rows": positives, "negative_rows": len(y) - positives,
    }
    by_size = {}
    for size in (8, 16, 24):
        indices = [i for group in groups.values() if len(group) == size for i in group]
        by_size[str(size)] = {
            **_ranking([y[i] for i in indices], raw[indices].tolist(),
                       [slates[i] for i in indices], [videos[i] for i in indices]),
            "row_count": len(indices),
        }
    result = {
        **{name: ranking[name] for name in METRICS[:3]},
        "grouped_roc_auc": fmean(aucs) if aucs else None,
        "pr_auc": float(average_precision_score(y, raw)) if both_classes else None,
        "log_loss": float(log_loss(y, prob, labels=[0, 1])),
        "brier": float(np.mean((prob - np.asarray(y)) ** 2)),
        "evaluation_id": aligned[0]["evaluation_id"], "row_count": len(y),
        "coverage": coverage, "by_size": by_size,
    }
    result["valid"] = _coverage_valid(result)
    return result


def _coverage_valid(metrics: dict) -> bool:
    try:
        c = metrics["coverage"]
        total, ranking, auc = (c[k] for k in ("total_slates", "ranking_slates", "auc_slates"))
        counts = (total, ranking, auc, c["positive_rows"], c["negative_rows"], metrics["row_count"])
        return (all(type(v) is int and v > 0 for v in counts)
                and 30 <= ranking <= total and 30 <= auc <= ranking
                and c["ranking_ratio"] == ranking / total >= .2
                and c["auc_ratio"] == auc / total >= .2
                and c["positive_rows"] + c["negative_rows"] == metrics["row_count"]
                and all(isinstance(metrics[k], Real) and not isinstance(metrics[k], bool)
                        and isfinite(metrics[k]) for k in METRICS))
    except (KeyError, TypeError, ZeroDivisionError):
        return False


def _index(observations: list[dict], arms: list[str]) -> dict:
    if (not arms or len(set(arms)) != len(arms) or BASELINE not in arms
            or not set(arms).issubset({BASELINE, "reference10", *CANDIDATES})):
        raise ValueError("recall_arms_invalid")
    expected = {(w, s, a) for w in WORLDS for s in SEEDS for a in arms}
    indexed = {}
    for row in observations:
        key = tuple(row.get(k) for k in ("world", "seed", "arm"))
        if (type(row.get("world")) is not int or type(row.get("seed")) is not int
                or key not in expected or key in indexed or not isinstance(row.get("metrics"), dict)):
            raise ValueError("recall_grid_invalid")
        indexed[key] = row
    if indexed.keys() != expected:
        raise ValueError("recall_grid_invalid")
    identities = set()
    for world in WORLDS:
        group = [indexed[world, seed, arm] for seed in SEEDS for arm in arms]
        first, metrics = group[0], group[0]["metrics"]
        identity = metrics.get("evaluation_id")
        row_hash = first.get("row_keys_sha256")
        if (not isinstance(identity, str) or re.fullmatch(r"eval_[0-9a-f]{64}", identity) is None
                or identity in identities or not isinstance(row_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", row_hash) is None
                or type(metrics.get("row_count")) is not int or metrics["row_count"] <= 0
                or any(row["metrics"].get("evaluation_id") != identity
                       or type(row["metrics"].get("row_count")) is not int
                       or row["metrics"]["row_count"] != metrics["row_count"]
                       or row.get("row_keys_sha256") != row_hash for row in group)):
            raise ValueError("recall_pair_identity_invalid")
        identities.add(identity)
        if any(row["metrics"].get("coverage") != metrics.get("coverage") for row in group):
            raise ValueError("recall_pair_coverage_invalid")
    return indexed


def summarize(observations: list[dict], arms: list[str]) -> dict:
    """고정된 6개 paired 관측의 방향성 변화와 사전 등록 gate를 계산한다.

    world·seed·arm grid와 평가 ID/row hash/coverage 불일치는 오류다. 표본 부족이나
    비유한 지표는 uninformative이며 개발 후보 선택과 신규 final을 차단한다.
    """
    indexed = _index(observations, arms)
    valid = all(row["metrics"].get("valid") is True and _coverage_valid(row["metrics"])
                for row in indexed.values())
    result = {"coverage_valid": valid, "comparisons": {}, "arm_means": {}}
    if not valid:
        return {**result, "verdict": "uninformative"}
    result["arm_means"] = {arm: {name: fmean(indexed[w, s, arm]["metrics"][name]
                                                 for w in WORLDS for s in SEEDS)
                                 for name in METRICS} for arm in arms}
    for arm in arms:
        if arm == BASELINE:
            continue
        deltas = {}
        for name in METRICS:
            sign = -1 if name in ("log_loss", "brier") else 1
            pairs = [{"world": w, "seed": s, "delta": sign * (
                indexed[w, s, arm]["metrics"][name] - indexed[w, s, BASELINE]["metrics"][name])}
                for w in WORLDS for s in SEEDS]
            values = [pair["delta"] for pair in pairs]
            worlds = {str(w): fmean(pair["delta"] for pair in pairs if pair["world"] == w)
                      for w in WORLDS}
            deltas[name] = {"mean": fmean(values), "positive_pairs": sum(v > 0 for v in values),
                            "positive_worlds": sum(v > 0 for v in worlds.values()),
                            "sample_stddev": stdev(values), "world_means": worlds, "pairs": pairs}
        recall = deltas["recall_at_10"]
        checks = {"recall_mean": recall["mean"] >= .005,
                  "recall_pairs": recall["positive_pairs"] >= 4,
                  "recall_worlds": recall["positive_worlds"] >= 2,
                  "guardrails_nonnegative": all(v["mean"] >= 0 for k, v in deltas.items()
                                                if k != "recall_at_10")}
        result["comparisons"][arm] = {"deltas": deltas, "checks": checks, "passed": all(checks.values())}
    supported = any(c["passed"] for arm, c in result["comparisons"].items() if arm in CANDIDATES)
    return {**result, "verdict": "supported" if supported else "not_supported"}


def select_candidate(summary: dict) -> dict:
    """개발 통과 우선·Recall/NDCG 변화·이름 순으로 후보 하나를 고정한다."""
    if not summary["coverage_valid"]:
        return {"selected_arm": None, "development_eligible": False, "verdict": "uninformative"}
    candidates = [arm for arm in CANDIDATES if arm in summary["comparisons"]]
    if not candidates:
        raise ValueError("recall_selection_candidates_missing")
    def order(arm: str) -> tuple:
        comparison = summary["comparisons"][arm]
        return (not comparison["passed"], -comparison["deltas"]["recall_at_10"]["mean"],
                -comparison["deltas"]["ndcg_at_10"]["mean"], arm)
    selected = min(candidates, key=order)
    eligible = summary["comparisons"][selected]["passed"]
    return {"selected_arm": selected, "development_eligible": eligible,
            "verdict": "supported" if eligible else "not_supported"}
