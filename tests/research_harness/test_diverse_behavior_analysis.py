"""#113 고정 관측의 동일 표본, 유효성 gate와 paired 판정을 검증한다."""

from hashlib import sha256

import pytest

from tests.research_harness.test_recent_behavior_analysis import observations as prior_observations
from tools import diverse_behavior_analysis as m


def observations() -> list[dict]:
    rows = prior_observations()
    for row in rows:
        row["world_seed"] += 400
        row["training_seed"] += 100
        row["row_keys_sha256"] = sha256(f'{row["world_seed"]}-{row["split"]}'.encode()).hexdigest()
    return rows


def validation(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row["split"] == "validation"]


def test_supported_loss_direction_and_separate_world_statistics() -> None:
    rows = observations()
    assert m.validation_gate(validation(rows))
    summary = m.summarize(rows)
    assert summary["verdict"] == "supported"
    for split in summary["splits"].values():
        loss = split["paired_directional_deltas"]["log_loss"]
        assert loss["mean"] == pytest.approx(.1)
        assert loss["count"] == 9
        assert loss["world_mean_stats"]["count"] == 3


@pytest.mark.parametrize("case", ["recall", "loss", "negative_validation", "one_world", "five_pairs"])
def test_valid_evidence_without_registered_effect_is_not_supported(case: str) -> None:
    rows = observations()
    for row in rows:
        if row["arm"] != "with_recent":
            continue
        metrics = row["metrics"]
        if case == "recall":
            metrics["recall_at_10"]["value"] = .6
        elif case == "loss":
            metrics["probability"]["log_loss"] = .4
        elif case == "negative_validation" and row["split"] == "validation":
            metrics["ndcg_at_10"]["value"] = .6
        elif case == "one_world":
            metrics["ndcg_at_10"]["value"] = .99 if row["world_seed"] == 10901 else .69
        elif case == "five_pairs":
            positive = row["world_seed"] == 10901 or (row["world_seed"] == 10902 and row["training_seed"] != 403)
            metrics["ndcg_at_10"]["value"] = .9 if positive else .69
    assert m.validation_gate(validation(rows))
    assert m.summarize(rows)["verdict"] == "not_supported"


@pytest.mark.parametrize("case", ["missing", "duplicate", "unknown_seed", "identity", "same_id", "row_count", "row_key", "missing_key", "malformed_key"])
def test_invalid_grid_or_sample_identity_raises(case: str) -> None:
    rows = observations()
    if case == "missing":
        rows.pop()
    elif case == "duplicate":
        rows.append(rows[0])
    elif case == "unknown_seed":
        rows[0]["training_seed"] = 404
    elif case == "identity":
        rows[0]["metrics"]["evaluation_id"] = "eval_" + "f" * 64
    elif case == "same_id":
        for row in rows:
            row["metrics"]["evaluation_id"] = "eval_" + "f" * 64
    elif case == "row_count":
        rows[0]["metrics"]["row_count"] += 1
    elif case == "row_key":
        rows[0]["row_keys_sha256"] = "f" * 64
    elif case == "missing_key":
        del rows[0]["row_keys_sha256"]
    else:
        for row in rows:
            row["row_keys_sha256"] = "not-a-hash"
    with pytest.raises(ValueError):
        m.summarize(rows)


@pytest.mark.parametrize("case", ["ranking_count", "ranking_coverage", "grouped_count", "label_count"])
def test_paired_coverage_mismatch_raises(case: str) -> None:
    rows = observations()
    metrics = rows[0]["metrics"]
    if case == "ranking_count":
        metrics["ndcg_at_10"].update(scored_slates=39, skipped_zero_click_slates=1, coverage=39/40)
    elif case == "ranking_coverage":
        metrics["ndcg_at_10"]["coverage"] = .99
    elif case == "grouped_count":
        metrics["probability"]["grouped_roc_auc"].update(scored_groups=39, skipped_groups=1)
    else:
        metrics["probability"].update(positive_count=39, negative_count=41)
    with pytest.raises(ValueError, match="pair_coverage"):
        m.validation_gate(validation(rows))
    with pytest.raises(ValueError, match="pair_coverage"):
        m.summarize(rows)


@pytest.mark.parametrize("case", ["ranking_below_30", "grouped_below_30", "nan", "inf", "single_class"])
def test_insufficient_coverage_or_nonfinite_is_uninformative(case: str) -> None:
    rows = observations()
    for row in rows:
        metrics = row["metrics"]
        if case == "ranking_below_30":
            for name in ("ndcg_at_10", "recall_at_10", "ndcg_at_24"):
                metrics[name].update(scored_slates=29, skipped_zero_click_slates=11, coverage=29/40)
        elif case == "grouped_below_30":
            metrics["probability"]["grouped_roc_auc"].update(scored_groups=29, skipped_groups=11)
        elif case == "single_class":
            metrics["probability"].update(positive_count=0, negative_count=80)
        elif row["arm"] == "with_recent":
            metrics["ndcg_at_10"]["value"] = float(case)
    assert not m.validation_gate(validation(rows))
    summary = m.summarize(rows)
    assert summary["verdict"] == "uninformative"
    assert summary["coverage_valid"] is False
    assert summary["splits"] == {}


def test_validation_gate_rejects_final_rows_and_incomplete_grid() -> None:
    with pytest.raises(ValueError, match="grid_invalid"):
        m.validation_gate(observations())
    with pytest.raises(ValueError, match="grid_invalid"):
        m.validation_gate(validation(observations())[:-1])
