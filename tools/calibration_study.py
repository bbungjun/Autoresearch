"""저장 CTR 모델 이후 확률 보정 표본 비교 구간.

[기능] 기존 모델의 fit과 서로소인 보정 표본 확대, Brier 기여 진단과 고정
4arm 판정을 제공한다. 원래 모델·보정 artifact는 수정하지 않는다.
[비책임] 데이터 생성, 예측 봉인, final 소비와 실행 예산은 run_calibration_study가 맡는다.
"""

from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
import re
from statistics import fmean, stdev

import lightgbm as lgb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes
from autoresearch.research_harness.recall_experiment import (
    _frame, _raw_predict, _table_hash, _verified_receipt, fit_calibration, group_split,
)
from tools.recall_analysis import METRICS, SEEDS, WORLDS, _coverage_valid, summarize

ARMS = ("baseline15", "baseline_expanded", "preference", "preference_expanded")
FAMILIES = ("baseline15", "preference")
RANKING = ("recall_at_10", "ndcg_at_10", "ndcg_at_24", "grouped_roc_auc", "pr_auc")


def expanded_calibration(model_root: Path, receipt_hash: str, labels: pa.Table,
                         features: pa.Table, output: Path, before_fit: Callable[[str], None]) -> dict:
    """원래 fit을 보존하고 calibration+reserve로 고정 sigmoid를 1회 학습한다."""
    receipt, model_bytes, original = _verified_receipt(model_root, receipt_hash)
    if receipt["arm"] not in FAMILIES:
        raise ValueError("calibration_family_invalid")
    if (_table_hash(labels) != receipt["labels_sha256"]
            or _table_hash(features.select(["source_event_id", *receipt["feature_columns"]])) != receipt["features_sha256"]
            or features["source_event_id"].to_pylist() != labels["source_event_id"].to_pylist()):
        raise ValueError("calibration_training_input_changed")
    split = group_split(labels, receipt["seed"])
    positions = split["calibration"] + split["reserve"]
    users = labels["user_id"].to_pylist()
    if ({users[i] for i in split["train"]} & {users[i] for i in positions}
            or sha256(canonical_json_bytes(split)).hexdigest() != receipt["base_split_sha256"]
            or sha256(canonical_json_bytes(split["train"])).hexdigest() != receipt["fit_positions_sha256"]):
        raise ValueError("calibration_fit_overlap_or_changed")
    output.mkdir(parents=True, exist_ok=False)
    model = lgb.Booster(model_str=model_bytes.decode())
    frame = _frame(features, tuple(receipt["feature_columns"]), receipt["vocabulary"])
    raw = _raw_predict(model, frame.iloc[positions])
    y = np.asarray(labels["clicked"].to_pylist(), dtype=np.int8)[positions]
    count = len(split["calibration"])
    if (sha256(raw[:count].tobytes()).hexdigest() != receipt["calibration_input"]["raw_sha256"]
            or sha256(y[:count].tobytes()).hexdigest() != receipt["calibration_input"]["labels_sha256"]):
        raise ValueError("original_calibration_input_not_reproduced")
    inputs = pa.table({"source_event_id": labels["source_event_id"].take(positions),
                       "user_id": labels["user_id"].take(positions), "raw_score": raw, "clicked": y})
    pq.write_table(inputs, output / "input.parquet")
    attempt = {"model_receipt_sha256": receipt_hash,
               "input_sha256": sha256((output / "input.parquet").read_bytes()).hexdigest(),
               "original_rows": count, "expanded_rows": len(positions),
               "original_positive": int(y[:count].sum()), "expanded_positive": int(y.sum()),
               "original_users": len({users[i] for i in split["calibration"]}),
               "expanded_users": len({users[i] for i in positions}), "model_fit_calls": 0}
    (output / "attempt.json").write_bytes(canonical_json_bytes(attempt))
    before_fit("calibration")
    calibration = fit_calibration(raw, y)
    result = {**attempt, "original": original, "expanded": calibration, "calibration_fit_calls": 1}
    (output / "receipt.json").write_bytes(canonical_json_bytes(result))
    return result


def probability_diagnostics(labels: np.ndarray, probabilities: np.ndarray) -> dict:
    """전체평균에 더해지는 양/음성 Brier 기여와 고정 구간 관측률을 반환한다."""
    y, p = np.asarray(labels, dtype=float), np.asarray(probabilities, dtype=float)
    if (y.ndim != 1 or len(y) == 0 or p.shape != y.shape or not np.isin(y, [0, 1]).all()
            or not np.isfinite(p).all() or ((p < 0) | (p > 1)).any()):
        raise ValueError("calibration_diagnostic_input_invalid")
    error = (p - y) ** 2
    classes = {}
    for value, name in ((0, "negative"), (1, "positive")):
        mask = y == value
        classes[name] = {"rows": int(mask.sum()), "contribution": float(error[mask].sum() / len(y)),
                         "conditional_brier": float(error[mask].mean()) if mask.any() else None}
    edges = [0, .01, .025, .05, .1, .2, .5, 1]
    bins = []
    for index, (left, right) in enumerate(zip(edges[:-1], edges[1:], strict=True)):
        mask = (p >= left) & ((p <= right) if index == len(edges)-2 else (p < right))
        bins.append({"lower": left, "upper": right, "rows": int(mask.sum()),
                     "mean_probability": float(p[mask].mean()) if mask.any() else None,
                     "observed_rate": float(y[mask].mean()) if mask.any() else None,
                     "brier_contribution": float(error[mask].sum() / len(y))})
    return {"rows": len(y), "brier": float(error.mean()), "classes": classes, "bins": bins}


def study_summary(observations: list[dict]) -> dict:
    """고정 24관측에서 보정 효과와 원래 baseline 대비 채택 조건을 분리한다."""
    index = {}
    expected = {(w, s, a) for w in WORLDS for s in SEEDS for a in ARMS}
    for row in observations:
        key = (row.get("world"), row.get("seed"), row.get("arm"))
        if (type(key[0]) is not int or type(key[1]) is not int or key not in expected or key in index):
            raise ValueError("calibration_grid_invalid")
        index[key] = row
    if index.keys() != expected:
        raise ValueError("calibration_grid_incomplete")
    identities = set()
    for world in WORLDS:
        group = [index[world, s, a] for s in SEEDS for a in ARMS]
        first = group[0]
        identity = first["metrics"]["evaluation_id"]
        if (identity in identities or re.fullmatch(r"eval_[0-9a-f]{64}", identity) is None
                or re.fullmatch(r"[0-9a-f]{64}", first["row_keys_sha256"]) is None):
            raise ValueError("calibration_pair_identity_invalid")
        identities.add(identity)
        for row in group:
            if (row["row_keys_sha256"] != first["row_keys_sha256"]
                    or any(row["metrics"][k] != first["metrics"][k] for k in ("evaluation_id", "row_count", "coverage"))):
                raise ValueError("calibration_pair_mismatch")
    if not all(r["metrics"].get("valid") is True and _coverage_valid(r["metrics"]) for r in observations):
        return {"coverage_valid": False, "verdict": "uninformative"}
    for w in WORLDS:
        for s in SEEDS:
            for base, expanded in (("baseline15", "baseline_expanded"), ("preference", "preference_expanded")):
                if any(index[w, s, base]["metrics"][k] != index[w, s, expanded]["metrics"][k] for k in RANKING):
                    raise ValueError("calibration_changed_ranking")
    # The existing adoption criterion accepts these original arm names; only a copy is renamed.
    adoption_rows = [{**r, "arm": "preference" if r["arm"] == "preference_expanded" else r["arm"]}
                     for r in observations if r["arm"] in ("baseline15", "preference_expanded")]
    adoption = summarize(adoption_rows, arms=["baseline15", "preference"])
    comparisons = {}
    for base, expanded in (("baseline15", "baseline_expanded"), ("preference", "preference_expanded")):
        delta = {k: [index[w, s, base]["metrics"][k] - index[w, s, expanded]["metrics"][k]
                      for w in WORLDS for s in SEEDS] for k in ("brier", "log_loss")}
        worlds = {str(w): fmean(index[w, s, base]["metrics"]["brier"] - index[w, s, expanded]["metrics"]["brier"]
                                for s in SEEDS) for w in WORLDS}
        checks = {"brier_mean": fmean(delta["brier"]) > 0, "brier_pairs": sum(x > 0 for x in delta["brier"]) >= 4,
                  "brier_worlds": sum(x > 0 for x in worlds.values()) >= 2, "log_loss": fmean(delta["log_loss"]) >= 0}
        comparisons[base] = {"improvement": {k: {"mean": fmean(v), "pairs": v, "sample_stddev": stdev(v)} for k, v in delta.items()},
                             "brier_world_means": worlds, "checks": checks, "passed": all(checks.values())}
    passed = comparisons["preference"]["passed"] and adoption["comparisons"]["preference"]["passed"]
    return {"coverage_valid": True, "verdict": "supported" if passed else "not_supported",
            "calibration_effect": comparisons, "adoption": adoption,
            "arm_means": {a: {k: fmean(index[w, s, a]["metrics"][k] for w in WORLDS for s in SEEDS)
                               for k in METRICS} for a in ARMS}}
