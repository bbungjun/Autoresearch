"""#117 raw/확률 분리, exact join, coverage와 사전 등록 후보 판정을 검증한다."""

from copy import deepcopy
from hashlib import sha256

import numpy as np
import pyarrow as pa
import pytest
from sklearn.metrics import average_precision_score, log_loss

from tools import recall_analysis as m


def inputs(groups: int = 40, size: int = 16) -> tuple:
    rows = [{"evaluation_id": "eval_" + "a" * 64, "slate_id": f"s{group:03}",
             "video_id": f"v{i:02}", "user_id": f"u{group:03}", "clicked": i == size - 1}
            for group in range(groups) for i in range(size)]
    labels = pa.Table.from_pylist(rows)
    keys = labels.select(["evaluation_id", "slate_id", "video_id", "user_id"])
    raw = np.tile(np.arange(size, dtype=float), groups)
    prob = np.full(len(rows), 1 / size)
    return keys, labels, raw, prob


def observations() -> list[dict]:
    base = m.score_predictions(*inputs())
    rows = []
    for world in m.WORLDS:
        for seed in m.SEEDS:
            for arm in (m.BASELINE, "reference10", *m.CANDIDATES):
                metrics = deepcopy(base)
                metrics["evaluation_id"] = "eval_" + sha256(str(world).encode()).hexdigest()
                metrics.update({name: .5 for name in m.METRICS})
                if arm != m.BASELINE:
                    metrics.update(recall_at_10=.51, ndcg_at_10=.51, log_loss=.49, brier=.49)
                rows.append({"world": world, "seed": seed, "arm": arm, "metrics": metrics,
                             "row_keys_sha256": sha256(str(world).encode()).hexdigest()})
    return rows


def summary(rows: list[dict]) -> dict:
    return m.summarize(rows, [m.BASELINE, "reference10", *m.CANDIDATES])


def test_raw_ranking_probability_separation_and_unordered_label_join() -> None:
    keys, labels, raw, prob = inputs()
    labels = labels.take(pa.array(list(reversed(range(labels.num_rows)))))
    result = m.score_predictions(keys, labels, raw, prob)
    y = [i % 16 == 15 for i in range(len(raw))]
    assert result["valid"]
    assert result["recall_at_10"] == result["ndcg_at_10"] == 1.
    assert result["grouped_roc_auc"] == 1.
    assert result["pr_auc"] == average_precision_score(y, raw)
    assert result["log_loss"] == log_loss(y, prob)
    assert result["brier"] == pytest.approx(np.mean((prob - np.array(y)) ** 2))
    assert result["by_size"]["16"]["total_slates"] == 40
    assert result["by_size"]["8"]["recall_at_10"] is None
    assert m.score_predictions(keys.drop(["user_id"]), labels, raw, prob) == result


@pytest.mark.parametrize("size", [8, 16, 24])
def test_size_diagnostics_and_deterministic_video_tie_break(size: int) -> None:
    keys, labels, raw, prob = inputs(size=size)
    raw[:] = 0
    result = m.score_predictions(keys, labels, raw, prob)
    expected = 1. if size == 8 else 0.
    assert result["recall_at_10"] == expected
    assert result["by_size"][str(size)]["recall_at_10"] == expected
    assert result["by_size"][str(size)]["row_count"] == 40 * size
    assert result["grouped_roc_auc"] == .5


@pytest.mark.parametrize("case", ["duplicate_prediction", "duplicate_label", "missing", "foreign",
                                  "user", "label", "mixed_evaluation", "slate_user"])
def test_join_rejects_invalid_keys_or_labels(case: str) -> None:
    keys, labels, raw, prob = inputs()
    key_rows, label_rows = keys.to_pylist(), labels.to_pylist()
    if case == "duplicate_prediction":
        key_rows[0] = key_rows[1]
    elif case == "duplicate_label":
        label_rows[0] = label_rows[1]
    elif case == "missing":
        key_rows.pop()
    elif case == "foreign":
        key_rows[0]["video_id"] = "unknown"
    elif case == "user":
        key_rows[0]["user_id"] = "other"
    elif case == "label":
        label_rows[0]["clicked"] = None
    elif case == "mixed_evaluation":
        key_rows[0]["evaluation_id"] = label_rows[0]["evaluation_id"] = "eval_" + "b" * 64
    else:
        key_rows[0]["user_id"] = label_rows[0]["user_id"] = "other"
    with pytest.raises(ValueError):
        m.score_predictions(pa.Table.from_pylist(key_rows), pa.Table.from_pylist(label_rows), raw, prob)


@pytest.mark.parametrize("case", ["nan_raw", "inf_probability", "probability_bounds", "shape"])
def test_invalid_prediction_values_fail_closed(case: str) -> None:
    keys, labels, raw, prob = inputs()
    if case == "nan_raw":
        raw[0] = np.nan
    elif case == "inf_probability":
        prob[0] = np.inf
    elif case == "probability_bounds":
        prob[0] = 1.01
    else:
        raw = raw.reshape(40, 16)
    with pytest.raises(ValueError, match="prediction_values"):
        m.score_predictions(keys, labels, raw, prob)


def test_coverage_floor_and_zero_click_probability_rows() -> None:
    assert not m.score_predictions(*inputs(groups=29))["valid"]
    keys, labels, raw, prob = inputs(groups=151)
    label_rows = labels.to_pylist()
    for i, row in enumerate(label_rows):
        row["clicked"] = row["clicked"] and i < 30 * 16
    result = m.score_predictions(keys, pa.Table.from_pylist(label_rows), raw, prob)
    assert result["coverage"]["ranking_slates"] == result["coverage"]["auc_slates"] == 30
    assert result["row_count"] == 151 * 16
    assert result["coverage"]["negative_rows"] == 151 * 16 - 30
    assert not result["valid"]  # 30/151 < 20% despite the count floor.
    for row in label_rows:
        row["clicked"] = False
    result = m.score_predictions(keys, pa.Table.from_pylist(label_rows), raw, prob)
    assert not result["valid"] and result["pr_auc"] is None
    assert result["recall_at_10"] is None and result["grouped_roc_auc"] is None
    assert np.isfinite(result["log_loss"])


def test_positive_deltas_loss_direction_world_statistics_and_name_tie_break() -> None:
    result = summary(observations())
    assert result["verdict"] == "supported"
    comparison = result["comparisons"]["shallow"]
    assert comparison["passed"]
    for name in ("recall_at_10", "log_loss", "brier"):
        stat = comparison["deltas"][name]
        assert stat["mean"] == pytest.approx(.01)
        assert stat["positive_pairs"] == 6 and stat["positive_worlds"] == 3
        assert len(stat["pairs"]) == 6 and len(stat["world_means"]) == 3
    assert m.select_candidate(result) == {
        "selected_arm": "larger", "development_eligible": True, "verdict": "supported"}


@pytest.mark.parametrize("case", ["missing", "duplicate", "extra", "row_hash", "identity",
                                  "world_identity", "count", "coverage"])
def test_fixed_grid_and_paired_identity_are_required(case: str) -> None:
    rows = observations()
    if case == "missing":
        rows.pop()
    elif case == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif case == "extra":
        rows[0]["seed"] = 403
    elif case == "row_hash":
        rows[0]["row_keys_sha256"] = "b" * 64
    elif case == "identity":
        rows[0]["metrics"]["evaluation_id"] = "eval_" + "b" * 64
    elif case == "world_identity":
        for row in rows:
            row["metrics"]["evaluation_id"] = "eval_" + "a" * 64
    elif case == "count":
        rows[0]["metrics"]["row_count"] += 1
    else:
        rows[0]["metrics"]["coverage"]["positive_rows"] += 1
    with pytest.raises(ValueError):
        summary(rows)


@pytest.mark.parametrize("case", ["nan", "coverage", "false_valid"])
def test_uninformative_prohibits_selection(case: str) -> None:
    rows = observations()
    for row in rows:
        metrics = row["metrics"]
        if case == "nan":
            metrics["ndcg_at_10"] = float("nan")
        elif case == "coverage":
            metrics["coverage"].update(ranking_slates=29, ranking_ratio=29 / 40,
                                       auc_slates=29, auc_ratio=29 / 40)
        else:
            metrics["valid"] = False
    result = summary(rows)
    assert result["verdict"] == "uninformative"
    assert m.select_candidate(result) == {
        "selected_arm": None, "development_eligible": False, "verdict": "uninformative"}


@pytest.mark.parametrize("case", ["effect", "pairs", "worlds", "guardrail"])
def test_valid_negative_result_keeps_only_diagnostic_candidate(case: str) -> None:
    rows = observations()
    for row in rows:
        if row["arm"] == m.BASELINE:
            continue
        metrics = row["metrics"]
        if case == "effect":
            metrics["recall_at_10"] = .504
        elif case == "pairs":
            metrics["recall_at_10"] = .53 if row["seed"] == 401 else .499
        elif case == "worlds":
            metrics["recall_at_10"] = .99 if row["world"] == 10701 else .499
        else:
            metrics["pr_auc"] = .499
    result = summary(rows)
    assert result["coverage_valid"] and result["verdict"] == "not_supported"
    selection = m.select_candidate(result)
    assert selection["selected_arm"] is not None
    assert not selection["development_eligible"]


def test_selection_prefers_passed_then_recall_then_ndcg_and_excludes_reference() -> None:
    rows = observations()
    for row in rows:
        if row["arm"] in ("reference10", "larger"):
            row["metrics"]["recall_at_10"] = .9
            row["metrics"]["log_loss"] = .6  # larger cannot outrank a passed arm.
        elif row["arm"] in ("preference", "ranker"):
            row["metrics"]["recall_at_10"] = .52
            row["metrics"]["ndcg_at_10"] = .6 if row["arm"] == "ranker" else .55
    assert m.select_candidate(summary(rows))["selected_arm"] == "ranker"


def test_final_subset_uses_same_pair_grid_without_reselection() -> None:
    arms = [m.BASELINE, "reference10", "shallow"]
    rows = [row for row in observations() if row["arm"] in arms]
    assert m.summarize(rows, arms)["comparisons"]["shallow"]["passed"]
