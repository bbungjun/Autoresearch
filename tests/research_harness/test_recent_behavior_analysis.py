"""최근 행동 ablation 판정과 실행 claim의 회귀를 검증한다."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.research_harness.test_popularity_recall import observations as prior_observations
from tools import recent_behavior_analysis as m
from tools import run_recent_behavior as runner


def observations() -> list[dict]:
    rows = []
    for row in prior_observations():
        if row["arm"] == "video_only_lgbm":
            continue
        row.update(world_seed=row["world_seed"]+200, training_seed=row["training_seed"]+100,
                   arm="with_recent" if row["arm"] == "without_video_popularity" else "without_recent")
        rows.append(row)
    return rows


def test_fixed_15_vs_10_and_loss_direction() -> None:
    assert len(m.columns("with_recent")) == 15
    assert len(m.columns("without_recent")) == 10
    assert set(m.columns("with_recent"))-set(m.columns("without_recent")) == m.RECENT
    summary = m.summarize(observations())
    assert summary["verdict"] == "supported"
    loss = summary["splits"]["final_holdout"]["paired_directional_deltas"]["log_loss"]
    assert loss["mean"] == pytest.approx(.1)
    assert loss["count"] == 9 and loss["world_mean_stats"]["count"] == 3


@pytest.mark.parametrize("case", ["missing", "duplicate", "identity", "recall", "coverage", "nan", "final_only", "one_world"])
def test_invalid_or_worse_evidence_cannot_pass(case: str) -> None:
    rows = observations()
    if case == "missing":
        rows.pop()
    elif case == "duplicate":
        rows.append(rows[0])
    elif case == "identity":
        rows[0]["metrics"]["evaluation_id"] = "eval_"+"f"*64
    else:
        for row in rows:
            if row["arm"] != "with_recent":
                continue
            metrics = row["metrics"]
            if case == "recall":
                metrics["recall_at_10"]["value"] = .6
            elif case == "coverage":
                metrics["probability"]["grouped_roc_auc"]["scored_groups"] = 29
            elif case == "nan":
                metrics["ndcg_at_10"]["value"] = float("nan")
            elif case == "final_only" and row["split"] == "validation":
                metrics["ndcg_at_10"]["value"] = .69
            elif case == "one_world":
                metrics["ndcg_at_10"]["value"] = .99 if row["world_seed"] == 10501 else .69
    if case in ("missing", "duplicate", "identity", "nan"):
        with pytest.raises(ValueError):
            m.summarize(rows)
    else:
        assert m.summarize(rows)["verdict"] == "not_supported"


def test_recent_variation_and_history_gate() -> None:
    d = {"availability": {"training": {"rows": 100, "history_7d_complete": 100, "history_30d_complete": 100}},
         "fit_by_seed": {str(s): {n: {"unique": 1} for n in m.RECENT} for s in m.TRAINING_SEEDS}}
    assert not m.informative(d)
    for fit in d["fit_by_seed"].values():
        fit["recent_watch_time_7d"]["unique"] = 2
    assert m.informative(d)
    d["availability"]["training"]["history_30d_complete"] = 99
    assert not m.informative(d)


def test_concurrent_claim_blocks_changed_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=str(tmp_path)))

    def claim(i: int) -> bool:
        try:
            runner.claim_run(tmp_path / str(i), "a"*40)
            return True
        except FileExistsError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(claim, range(8))) == 1
