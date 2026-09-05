"""#103 확인 실험의 grid, paired gate와 진단 통계 회귀를 검증한다."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest

from tools import popularity_recall_analysis as m


def observations() -> list[dict]:
    rows = []
    for w in m.WORLD_SEEDS:
        for s in m.SPLITS:
            for t in m.TRAINING_SEEDS:
                for a in m.ARMS:
                    value = .8 if a == m.ARMS[1] else .7
                    ranking = dict(value=value, total_slates=40, scored_slates=40,
                                   skipped_zero_click_slates=0, coverage=1.)
                    metrics = dict(
                        evaluation_id="eval_" + sha256(f"{w}-{s}".encode()).hexdigest(),
                        row_count=80, ndcg_at_10=deepcopy(ranking), recall_at_10=deepcopy(ranking),
                        ndcg_at_24=deepcopy(ranking),
                        probability=dict(row_count=80, positive_count=40, negative_count=40,
                                         roc_auc=value, pr_auc=value, log_loss=1-value, brier=1-value,
                                         grouped_roc_auc=dict(value=value, total_groups=40,
                                                              scored_groups=40, skipped_groups=0,
                                                              null_key_rows=0)))
                    rows.append(dict(world_seed=w, split=s, training_seed=t, arm=a, metrics=metrics))
    return rows


def test_paired_directions_world_means_and_recovery() -> None:
    summary = m.summarize(observations())
    assert summary["verdict"] == "supported"
    for split in summary["splits"].values():
        loss = split["paired_directional_deltas"]["removal_minus_full"]["log_loss"]
        assert loss["mean"] == pytest.approx(.1)
        assert loss["count"] == 9
        assert loss["world_mean_stats"]["count"] == 3
        assert split["recall_recovery"] == "full"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "different_id", "same_split_id"])
def test_rejects_unpaired_grid(mutation: str) -> None:
    rows = observations()
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(rows[0])
    elif mutation == "different_id":
        rows[0]["metrics"]["evaluation_id"] = "eval_" + "b" * 64
    else:
        for row in rows:
            row["metrics"]["evaluation_id"] = "eval_" + "b" * 64
    with pytest.raises(ValueError):
        m.summarize(rows)


@pytest.mark.parametrize("mutation", ["recall", "coverage", "nan", "one_split", "one_world"])
def test_does_not_pass_ndcg_only_or_incomplete_evidence(mutation: str) -> None:
    rows = observations()
    for row in rows:
        if row["arm"] != m.ARMS[1]:
            continue
        metrics = row["metrics"]
        if mutation == "recall":
            metrics["recall_at_10"]["value"] = .6
        elif mutation == "coverage":
            metrics["probability"]["grouped_roc_auc"]["scored_groups"] = 29
        elif mutation == "nan":
            metrics["ndcg_at_10"]["value"] = float("nan")
        elif mutation == "one_split" and row["split"] == "final_holdout":
            metrics["ndcg_at_10"]["value"] = .69
        elif mutation == "one_world":
            metrics["ndcg_at_10"]["value"] = .95 if row["world_seed"] == 10301 else .69
    if mutation == "nan":
        with pytest.raises(ValueError):
            m.summarize(rows)
    else:
        assert m.summarize(rows)["verdict"] == "not_supported"


def test_feature_stats_distinguishes_zero_constant_and_null() -> None:
    columns = m.ABLATION_FEATURE_GROUPS["without_video_popularity"] | m.ABLATION_FEATURE_GROUPS["without_recent_behavior"]
    table = pa.table({name: [0., 0., 2., None] for name in columns})
    result = m.feature_stats(table)["view_count"]
    assert result["null_count"] == 1
    assert result["zero_count"] == 2
    assert result["unique"] == 2
    assert result["mean"] == pytest.approx(2/3)


def test_experiment_claim_rejects_other_output_before_training(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import run_popularity_recall as runner

    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=str(tmp_path)))
    runner._claim_experiment(tmp_path / "first-output", "a" * 40)
    with pytest.raises(FileExistsError):
        runner._claim_experiment(tmp_path / "different-output", "b" * 40)


def test_concurrent_claim_has_exactly_one_winner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import run_popularity_recall as runner

    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=str(tmp_path)))

    def claim(index: int) -> bool:
        try:
            runner._claim_experiment(tmp_path / str(index), "a" * 40)
            return True
        except FileExistsError:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(claim, range(8))) == 1
