"""실물 소비 없이 고정 ablation의 실행 순서와 중단 경계를 검증한다."""

import json
from types import SimpleNamespace
import sys

import numpy as np
import pyarrow as pa
import pytest

from tools import run_diverse_behavior as runner


def test_run_claim_is_shared_across_output_directories(tmp_path):
    runner.claim_run(tmp_path, tmp_path / "first", "a" * 40)
    with pytest.raises(FileExistsError):
        runner.claim_run(tmp_path, tmp_path / "second", "b" * 40)


def test_bad_summary_and_output_overlap_rejected(tmp_path):
    (tmp_path / "summary.json").write_text("{}")
    with pytest.raises(ValueError, match="summary_hash_mismatch"):
        runner.load_worlds(tmp_path / "training", tmp_path)
    with pytest.raises(ValueError, match="overlap"):
        runner._disjoint(tmp_path / "new-output", tmp_path)


@pytest.mark.parametrize("valid", [True, False])
def test_all_models_then_all_validation_then_final(monkeypatch, tmp_path, valid):
    output = tmp_path / "output"
    output.mkdir()
    experiment = runner.Experiment(output, "b" * 40)
    calls = []
    worlds = []
    for number in runner.COHORT_SEEDS:
        judge = tmp_path / f"judge-{number}"
        judge.mkdir()
        handoff = SimpleNamespace(snapshot_root=judge / "evaluation-snapshots/by-hash/hash",
                                  final_holdout_id=f"eval_{number}")
        worlds.append(runner.World(number, tmp_path / "training", "a" * 64, None, handoff,
                                   tmp_path / "slate.parquet", "c" * 64))

    def train(bundle, root, *, expected_bundle_sha256, seed, arm):
        calls.append(("fit", seed, arm))
        root.mkdir(parents=True)
        (root / "receipt.json").write_text("{}")
        return {"split_receipts": {"seed": seed}}

    def score(world, split, *args):
        assert sum(c[0] == "fit" for c in calls) == 18
        calls.append((split, world.seed))
        experiment.observations.extend([{"split": split}] * 6)

    def gate(rows):
        assert len(rows) == 18 and {r["split"] for r in rows} == {"validation"}
        calls.append(("gate", valid))
        return valid

    def claim(request):
        assert sum(c[0] == "validation" for c in calls) == 3
        assert ("gate", True) in calls
        calls.append(("claim",))
        return SimpleNamespace(evidence=SimpleNamespace(marker_sha256="a" * 64))

    monkeypatch.setattr(runner, "train_behavior_model", train)
    monkeypatch.setattr(experiment, "score_split", score)
    monkeypatch.setattr(runner, "validation_gate", gate)
    monkeypatch.setattr(runner, "claim_final_consumption", claim)
    monkeypatch.setattr(runner, "_second_claim_fails_closed", lambda request: True)
    monkeypatch.setattr(runner, "prepare_behavior_metadata", lambda *a, **k: None)
    monkeypatch.setattr(runner, "materialize_final_candidate_data_view", lambda *a, **k: SimpleNamespace(
        root=tmp_path, manifest_sha256="a" * 64))
    monkeypatch.setattr(runner, "summarize", lambda rows: {"verdict": "not_supported", "observations": len(rows)})
    result = experiment.execute(worlds, None)
    assert experiment.state["fit_completed"] == 18
    assert sum(c[0] == "fit" for c in calls) == 18
    assert experiment.state["final_claims"] == (3 if valid else 0)
    assert sum(c[0] == "final_holdout" for c in calls) == (3 if valid else 0)
    assert result["verdict"] == ("not_supported" if valid else "uninformative")


@pytest.mark.parametrize("fail_prediction", [False, True])
def test_all_six_predictions_sealed_before_scoring(monkeypatch, tmp_path, fail_prediction):
    experiment = runner.Experiment(tmp_path, "a" * 40)
    (tmp_path / "models-sealed.json").write_text("{}")
    keys = pa.table({"evaluation_id": ["eval_" + "a" * 64] * 2,
                     "slate_id": ["slate"] * 2, "video_id": ["one", "two"]})
    features = pa.table({c: [1, 2] for c in runner.arm_columns("with_recent")})
    monkeypatch.setattr(runner, "prediction_features", lambda *a, **k: (keys, features, "b" * 64))
    predictions = []
    scores = []

    def predict(*args, **kwargs):
        predictions.append(1)
        if fail_prediction and len(predictions) == 3:
            raise ValueError("prediction_failure")
        return np.array([0.25, 0.75])

    class Domain:
        def validate_candidate(self, prediction, copy):
            copy.write_bytes(prediction.read_bytes())
            return copy

        def evaluate(self, handoff, receipt, *, final_grant):
            assert len(predictions) == 6
            assert (tmp_path / "world-10901/final_holdout/predictions-sealed.json").exists()
            scores.append(1)
            return {"value": 1}

    monkeypatch.setattr(runner, "predict_behavior_model", predict)
    monkeypatch.setattr(runner, "scoring_result_dict", lambda value: value)
    experiment.domain = Domain()
    experiment.models = {(10901, s, a): "a" * 64 for s in runner.TRAINING_SEEDS for a in runner.ARMS}
    world = runner.World(10901, tmp_path, "a" * 64, None, None, tmp_path, "a" * 64)
    if fail_prediction:
        with pytest.raises(ValueError, match="prediction_failure"):
            experiment.score_split(world, "final_holdout", tmp_path, "a" * 64, SimpleNamespace(identity="e5"), object())
        assert not scores
    else:
        experiment.score_split(world, "final_holdout", tmp_path, "a" * 64, SimpleNamespace(identity="e5"), object())
        assert len(scores) == 6
        assert experiment.state["fit_attempts"] == 0


def test_hard_timeout_records_failure_without_retry(tmp_path):
    code = runner.supervise([sys.executable, "-c", "import time; time.sleep(10)"], tmp_path, timeout=0.15)
    assert code == 124
    assert json.loads((tmp_path / "timeout.json").read_text())["retry_allowed"] is False


def test_step_deadline_rejects_next_work(monkeypatch, tmp_path):
    experiment = runner.Experiment(tmp_path, "a" * 40)
    monkeypatch.setattr(runner, "perf_counter", lambda: experiment.started + 1801)
    with pytest.raises(TimeoutError):
        experiment.progress("fit")
    assert experiment.state["fit_attempts"] == 0
