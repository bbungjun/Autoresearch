"""5회 single-fit calibration의 순서·raw 표준편차·중단 보존 계약."""

from dataclasses import replace
import importlib
import statistics

import pytest

from tests.research_harness.test_controller import _score_for
from autoresearch.research_harness.evaluation_snapshot_models import EvaluationId


def module():
    return importlib.import_module("scripts.research_harness.calibrate_baseline")


def test_sigma_uses_five_raw_values_and_sample_denominator():
    result = module().summarize_metrics([
        {"ndcg_at_10": value, "recall_at_10": 1.0} for value in [0.1, 0.2, 0.3, 0.4, 0.5]
    ])
    assert result["ndcg_at_10"]["sample_stddev"] == statistics.stdev([0.1, 0.2, 0.3, 0.4, 0.5])
    assert result["ndcg_at_10"]["ddof"] == 1
    assert result["recall_at_10"]["sample_stddev"] == 0.0
    assert result["brier"]["valid_count"] == 0 and result["brier"]["mean"] is None


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), True])
def test_incomplete_or_nonfinite_metric_has_null_statistics(value):
    result = module().summarize_metrics([{"ndcg_at_10": item} for item in [0.1, 0.2, 0.3, 0.4, value]])
    assert result["ndcg_at_10"]["valid_count"] == 4
    assert result["ndcg_at_10"]["mean"] is None and result["ndcg_at_10"]["sample_stddev"] is None


def test_invalid_seed_order_and_existing_output_fail_before_execution(tmp_path):
    m = module()
    root = tmp_path / "exists"
    root.mkdir()
    request = m.CalibrationRequest(tmp_path, tmp_path / "workspaces", tmp_path / "snapshot", "a" * 64,
                                   tmp_path / "prediction.json", root)
    with pytest.raises(m.CalibrationError):
        m.run_calibration(request)
    with pytest.raises(m.CalibrationError):
        m.run_calibration(replace(request, out=tmp_path / "fresh", seeds=(1, 2, 3, 4, 5)))


def test_each_seed_is_called_once_and_ledger_contains_checkpoint_evidence(tmp_path, monkeypatch):
    m = module()
    calls = []
    request = m.CalibrationRequest(tmp_path, tmp_path / "workspaces", tmp_path / "snapshot", "a" * 64,
                                   tmp_path / "prediction.json", tmp_path / "out")
    monkeypatch.setattr(m, "_prepare_inputs", lambda req: {"kind": "fake-prepared", "model_files": []})
    def fit(req, prepared, seed, output):
        calls.append(seed)
        return _score_for(0.5, EvaluationId("eval_" + "a" * 64))
    monkeypatch.setattr(m, "_single_fit", fit)
    result = m.run_calibration(request)
    assert calls == [101, 102, 103, 104, 105]
    assert result["status"] == "complete"
    assert result["metrics"]["ndcg_at_10"]["sample_stddev"] == 0.0
    assert result["current_sigma_gate_satisfied"] is False
    from autoresearch.research_harness.ledger import open_trial_ledger
    state = open_trial_ledger(request.out / "experiment-ledger.jsonl").read_state()
    assert len(state.checkpoints) == 12 and len(state.trials) == 0
    assert state.completed("calibration-seed-105-complete")
    with pytest.raises(m.CalibrationError):
        m.run_calibration(request)
    assert len(calls) == 5


def test_interruption_preserves_partial_result_and_never_retries(tmp_path, monkeypatch):
    m = module()
    request = m.CalibrationRequest(tmp_path, tmp_path / "workspaces", tmp_path / "snapshot", "a" * 64,
                                   tmp_path / "prediction.json", tmp_path / "out")
    monkeypatch.setattr(m, "_prepare_inputs", lambda req: {"kind": "fake-prepared", "model_files": []})
    calls = []
    def fit(req, prepared, seed, output):
        calls.append(seed)
        if seed == 102:
            raise KeyboardInterrupt
        return _score_for(0.5, EvaluationId("eval_" + "a" * 64))
    monkeypatch.setattr(m, "_single_fit", fit)
    with pytest.raises(KeyboardInterrupt):
        m.run_calibration(request)
    import json
    result = json.loads((request.out / "calibration.json").read_bytes())
    assert result["status"] == "incomplete" and calls == [101, 102]
    assert result["metrics"]["ndcg_at_10"]["valid_count"] == 1
    assert result["metrics"]["ndcg_at_10"]["sample_stddev"] is None
    assert (request.out / "seed-102/failure.json").exists()


def test_exact_minimum_sigma_is_not_admissible(tmp_path, monkeypatch):
    m = module()
    request = m.CalibrationRequest(tmp_path, tmp_path / "workspaces", tmp_path / "snapshot", "a" * 64,
                                   tmp_path / "prediction.json", tmp_path / "out")
    monkeypatch.setattr(m, "_prepare_inputs", lambda req: {"kind": "fake-prepared"})
    monkeypatch.setattr(m, "_single_fit", lambda *args: _score_for(0.5, EvaluationId("eval_" + "a" * 64)))
    monkeypatch.setattr(m, "summarize_metrics", lambda samples: {name: {"sample_stddev": 1e-6} for name in m.METRICS})
    assert m.run_calibration(request)["current_sigma_gate_satisfied"] is False


def test_single_fit_calls_existing_runner_once_then_scores_after_cleanup(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from autoresearch.research_harness import domain, local_embedding, runner, workspace
    from autoresearch.research_harness import local_trial_runner
    m = module()
    request = m.CalibrationRequest(tmp_path, tmp_path / "workspaces", tmp_path / "snapshot", "a" * 64,
                                   tmp_path / "prediction.json", tmp_path / "out")
    root, output = tmp_path / "checkout", tmp_path / "evidence"
    root.mkdir()
    output.mkdir()
    predictions = root / "predictions.csv"
    events = []
    process = SimpleNamespace(predictions=predictions)
    @contextmanager
    def open_workspace(req, *, source, metadata):
        assert req.base_sha == m.BASELINE_SHA
        events.append("open")
        yield SimpleNamespace(root=root, process=process, candidate_view_sha256="a" * 64)
        events.append("cleanup")
    class FakeRunner:
        def run(self, req):
            assert req.process is process and req.seed == 101
            events.append("fit")
            return runner.LocalRunReceipt(predictions, 0, 12, "", "")
    class FakeDomain:
        def validate_candidate(self, source, destination):
            assert events[-1] == "cleanup"
            events.append("seal")
            return "sealed"
        def evaluate(self, handoff, sealed, *, final_grant):
            assert sealed == "sealed" and final_grant is None
            events.append("score")
            return "score"
    monkeypatch.setattr(workspace, "open_candidate_workspace", open_workspace)
    monkeypatch.setattr(runner, "LocalRunner", FakeRunner)
    monkeypatch.setattr(domain, "YouTubeCTRDomain", FakeDomain)
    monkeypatch.setattr(local_trial_runner, "_copy_outputs", lambda *args, **kwargs: events.append("copy"))
    monkeypatch.setattr(local_embedding, "_model_files", lambda path: [])
    config = SimpleNamespace(model_dump=lambda **kwargs: {}, embedding=SimpleNamespace(model_dir=tmp_path))
    prepared = {"handoff": None, "source": None, "metadata": None, "config": config, "model_files": []}
    assert m._single_fit(request, prepared, 101, output) == "score"
    assert events == ["open", "fit", "copy", "cleanup", "seal", "score"]
