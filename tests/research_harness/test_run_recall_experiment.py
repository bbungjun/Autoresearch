"""#117 실행기의 예산·봉인 순서·단일 실행·원본 보존을 mock으로 검증한다.

실제 학습·추론·데이터 생성과 기존 final 접근 없이 임시 산출물만 사용한다.
"""

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pyarrow as pa
import pytest

from tools import run_recall_experiment as m


@pytest.fixture
def args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(**{name: tmp_path / name for name in (
        "output", "training", "raw_training", "evaluation", "previous_run", "model_dir", "cache_dir")})


@pytest.fixture
def experiment(args: argparse.Namespace, tmp_path: Path) -> m.Experiment:
    args.output.mkdir()
    return m.Experiment(args, tmp_path / "repository", "a" * 40)


def _git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Mock:
    def response(command: list[str], **kwargs: object) -> SimpleNamespace:
        stdout = "" if command[1] == "status" else (
            str(tmp_path / "git-common") if "--git-common-dir" in command else "a" * 40)
        return SimpleNamespace(stdout=stdout)
    mocked = Mock(side_effect=response)
    monkeypatch.setattr(m.subprocess, "run", mocked)
    return mocked


def _mock_phases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "code_hashes", lambda _: {"code": "a" * 64})
    def develop(self: m.Experiment) -> dict:
        self.selection = {"selected_arm": None, "code": {"code": "a" * 64}}
        m.write_json(self.output / "selection.json", self.selection)
        return {"selection": "sealed"}
    monkeypatch.setattr(m.Experiment, "prepare", lambda _: {})
    monkeypatch.setattr(m.Experiment, "fit", lambda _: {})
    monkeypatch.setattr(m.Experiment, "develop", develop)
    monkeypatch.setattr(m.Experiment, "audit", lambda _: {"unchanged": True})


def test_registered_budget_table_and_fit_cap(tmp_path: Path) -> None:
    assert len(m.ARMS) * len(m.BUNDLE_HASHES) * len(m.TRAINING_SEEDS) * 2 == m.MAX_FITS == 72
    assert sum(m.PHASE_SECONDS.values()) == m.MAX_SECONDS == 7200
    budget = m.Budget(tmp_path)
    for index in range(72):
        budget.before_fit("model" if index % 2 == 0 else "calibration")
    attempts = sorted((tmp_path / "fit-attempts").glob("*.json"))
    assert len(attempts) == 72
    assert all(json.loads(path.read_bytes())["paid_api_calls"] == 0 for path in attempts)
    with pytest.raises(ValueError, match="fit_budget_exceeded"):
        budget.before_fit("model")
    assert not (tmp_path / "fit-attempts/073.json").exists()


def test_total_and_phase_compute_budget_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    current = [0.]
    monkeypatch.setattr(m, "perf_counter", lambda: current[0])
    budget = m.Budget(tmp_path)
    current[0] = 7200
    with pytest.raises(TimeoutError, match="total_compute_budget"):
        budget.check()
    current[0] = 0
    def action() -> dict:
        current[0] = 901
        return {}
    with pytest.raises(TimeoutError, match="phase_budget_exceeded:prepare"):
        budget.phase("prepare", action)
    assert (tmp_path / "stages/prepare-attempt.json").exists()
    assert not (tmp_path / "stages/prepare-complete.json").exists()


def test_exclusive_completed_or_failed_stage_never_reexecutes(tmp_path: Path) -> None:
    budget = m.Budget(tmp_path)
    action = Mock(return_value={"done": True})
    budget.phase("prepare", action)
    with pytest.raises(FileExistsError):
        budget.phase("prepare", action)
    assert action.call_count == 1
    failing = Mock(side_effect=RuntimeError("controlled_failure"))
    with pytest.raises(RuntimeError, match="controlled_failure"):
        budget.phase("models", failing)
    with pytest.raises(FileExistsError):
        m.Budget(tmp_path).phase("models", failing)
    assert failing.call_count == 1


def test_write_json_does_not_overwrite_output(tmp_path: Path) -> None:
    target = tmp_path / "nested/selection.json"
    m.write_json(target, {"selected": "first"})
    original = target.read_bytes()
    with pytest.raises(FileExistsError):
        m.write_json(target, {"selected": "second"})
    assert target.read_bytes() == original


@pytest.mark.parametrize("name", ["training", "raw_training", "evaluation", "previous_run", "model_dir", "cache_dir"])
@pytest.mark.parametrize("direction", ["same", "output_inside", "input_inside"])
def test_run_rejects_all_input_output_overlap(args: argparse.Namespace, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch,
                                             name: str, direction: str) -> None:
    _git(monkeypatch, tmp_path)
    if direction == "same":
        args.output = getattr(args, name)
    elif direction == "output_inside":
        args.output = getattr(args, name) / "nested"
    else:
        setattr(args, name, args.output / "nested")
    with pytest.raises(ValueError, match="input_output_overlap"):
        m.run(args)
    assert not (tmp_path / "git-common/research-experiment-claims/issue-117-recall.json").exists()


@pytest.mark.parametrize("case", ["fits", "seconds"])
def test_preflight_budget_mismatch_precedes_any_git_or_compute(args: argparse.Namespace,
                                                               monkeypatch: pytest.MonkeyPatch,
                                                               case: str) -> None:
    if case == "fits":
        monkeypatch.setattr(m, "MAX_FITS", 91)
    else:
        monkeypatch.setattr(m, "PHASE_SECONDS", {"prepare": 7201})
    mocked = Mock(side_effect=AssertionError("git must not run"))
    monkeypatch.setattr(m.subprocess, "run", mocked)
    with pytest.raises(ValueError, match="preflight_budget_grid_invalid"):
        m.run(args)
    mocked.assert_not_called()


def test_common_registry_blocks_second_output_and_preflight_records_caps(
    args: argparse.Namespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git(monkeypatch, tmp_path)
    _mock_phases(monkeypatch)
    m.run(args)
    preflight = json.loads((args.output / "preflight.json").read_bytes())
    assert preflight["fit_cap"] == 72 and preflight["requested_fit_cap"] == 90
    assert preflight["seconds_cap"] == 7200 and preflight["paid_api_calls"] == 0
    second = tmp_path / "different-output"
    args.output = second
    with pytest.raises(FileExistsError):
        m.run(args)
    assert not second.exists()


def test_existing_output_is_not_reused(args: argparse.Namespace, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    _git(monkeypatch, tmp_path)
    args.output.mkdir()
    (args.output / "sentinel").write_text("unchanged")
    with pytest.raises(FileExistsError):
        m.run(args)
    assert (args.output / "sentinel").read_text() == "unchanged"


def test_uninformative_development_skips_generation_and_final_access(
    experiment: m.Experiment, monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment.selection = {"selected_arm": None}
    generate = Mock(side_effect=AssertionError("generation forbidden"))
    claim = Mock(side_effect=AssertionError("final claim forbidden"))
    monkeypatch.setattr(m, "generate_behavior_evaluation", generate)
    monkeypatch.setattr(m, "claim_final_consumption", claim)
    assert experiment.fresh()["performed"] is False
    assert experiment.evaluate_final() == {"performed": False, "verdict": "uninformative"}
    generate.assert_not_called()
    claim.assert_not_called()


def test_selected_code_change_prevents_new_generation(experiment: m.Experiment,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    experiment.selection = {"selected_arm": "shallow", "code": {"code": "original"}, "models": {}}
    m.write_json(experiment.output / "selection.json", experiment.selection)
    experiment.selection_hash = m.digest(experiment.output / "selection.json")
    monkeypatch.setattr(m, "code_hashes", lambda _: {"code": "changed"})
    generate = Mock(side_effect=AssertionError("generation forbidden"))
    monkeypatch.setattr(m, "generate_behavior_evaluation", generate)
    with pytest.raises(ValueError, match="selection_seal_changed"):
        experiment.fresh()
    generate.assert_not_called()


@pytest.mark.parametrize("case", ["missing", "changed_file", "changed_memory", "changed_model"])
def test_selection_seal_required_before_fresh_generation(
    experiment: m.Experiment, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    experiment.selection = {"selected_arm": "shallow", "code": {}, "models": {}}
    monkeypatch.setattr(m, "code_hashes", lambda _: {})
    if case != "missing":
        path = experiment.output / "selection.json"
        m.write_json(path, experiment.selection)
        experiment.selection_hash = m.digest(path)
        if case == "changed_file":
            path.write_text("{}")
        elif case == "changed_memory":
            experiment.selection["selected_arm"] = "ranker"
        else:
            m.write_json(experiment.output / "models/changed.json", {})
    generate = Mock(side_effect=AssertionError("generation forbidden"))
    monkeypatch.setattr(m, "generate_behavior_evaluation", generate)
    with pytest.raises(ValueError, match="selection_seal_changed"):
        experiment.fresh()
    generate.assert_not_called()


@pytest.mark.parametrize("case", ["prediction", "keys", "models", "missing_arm"])
def test_scoring_rejects_changed_prediction_bundle_before_metrics(
    experiment: m.Experiment, monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    world = next(iter(m.BUNDLE_HASHES))
    keys = pa.table({"evaluation_id": ["eval_" + "b" * 64], "slate_id": ["new"], "video_id": ["video"]})
    features = pa.table({"feature": [1.]})
    experiment.models = {(world, seed, "baseline15"): "a" * 64 for seed in m.TRAINING_SEEDS}
    m.write_json(experiment.output / "models-sealed.json", {})
    monkeypatch.setattr(m, "predict_model", lambda *args, **kwargs: (np.array([0.]), np.array([.5])))
    paths = experiment.predict_bundle(world, "development", keys, features, ("baseline15",))
    if case == "prediction":
        (paths[0][2] / "prediction.parquet").write_bytes(b"changed")
    elif case == "keys":
        keys = keys.set_column(2, "video_id", pa.array(["different"]))
    elif case == "models":
        (experiment.output / "models-sealed.json").write_text('{"changed":true}')
    else:
        paths.pop()
    scorer = Mock(side_effect=AssertionError("scoring forbidden"))
    monkeypatch.setattr(m, "score_predictions", scorer)
    with pytest.raises(ValueError, match="prediction"):
        experiment.score_bundle(world, keys, pa.table({"clicked": [True]}), paths)
    scorer.assert_not_called()


@pytest.mark.parametrize("mutation", ["changed", "added", "deleted"])
def test_audit_detects_any_protected_input_change(experiment: m.Experiment, mutation: str) -> None:
    root = experiment.args.evaluation
    root.mkdir()
    original = root / "opaque-protected-bytes"
    original.write_bytes(b"original")
    experiment.protected = {"evaluation": m.tree_hashes(root)}
    if mutation == "changed":
        original.write_bytes(b"changed")
    elif mutation == "added":
        (root / "extra").write_bytes(b"new")
    else:
        original.unlink()
    with pytest.raises(ValueError, match="protected_old_inputs_changed"):
        experiment.audit()


def test_final_seals_all_six_predictions_before_labels_or_scores(
    experiment: m.Experiment, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from autoresearch.research_harness import local_embedding

    world = next(iter(m.BUNDLE_HASHES))
    keys = pa.table({"evaluation_id": ["eval_" + "b" * 64], "slate_id": ["new-slate"], "video_id": ["video"]})
    features = pa.table({"feature": [1.]})
    events: list[str] = []
    experiment.selection = {"selected_arm": "shallow", "development_eligible": False,
                            "models": {}, "code": {}}
    m.write_json(experiment.output / "selection.json", experiment.selection)
    experiment.selection_hash = m.digest(experiment.output / "selection.json")
    monkeypatch.setattr(m, "code_hashes", lambda _: {})
    m.write_json(experiment.output / "models-sealed.json", {})
    experiment.prepared = {world: {"embedding": {"identity": "mock-identity"}}}
    experiment.models = {(world, seed, arm): "a" * 64 for seed in m.TRAINING_SEEDS
                         for arm in ("baseline15", "reference10", "shallow")}
    snapshot = tmp_path / "new-only/state/a/b/snapshot"
    snapshot.mkdir(parents=True)
    handoff = SimpleNamespace(snapshot_root=snapshot, snapshot_fingerprint="new-fingerprint", final_holdout_id="new-final")
    experiment.new_worlds = {world: (object(), handoff)}
    embedding = SimpleNamespace(identity="mock-identity", stats={})
    monkeypatch.setattr(local_embedding, "LocalSentenceTransformer", lambda _: embedding)
    monkeypatch.setattr(local_embedding, "LocalEmbeddingConfig", lambda **_: object())
    grant = SimpleNamespace(evidence=SimpleNamespace(marker_sha256="b" * 64), _authorizes=lambda _: True)
    monkeypatch.setattr(m, "FinalConsumptionRequest", lambda *args: object())
    monkeypatch.setattr(m, "claim_final_consumption", lambda _: grant)
    monkeypatch.setattr(m, "prepare_behavior_metadata", lambda *args, **kwargs: object())
    monkeypatch.setattr(m, "CandidateDataViewRequest", lambda *args: object())
    monkeypatch.setattr(m, "materialize_final_candidate_data_view", lambda *args, **kwargs: SimpleNamespace(
        root=tmp_path / "mock-candidate", manifest_sha256="c" * 64))
    monkeypatch.setattr(m, "prediction_features", lambda *args: (keys, features, None))
    monkeypatch.setattr(m, "load_local_training_input", lambda _: SimpleNamespace(slate=keys, history=None, videos=None))
    monkeypatch.setattr(m, "preference_features", lambda *args: pa.table({}))
    def predict(*args: object, **kwargs: object) -> tuple:
        events.append("prediction")
        return np.array([0.]), np.array([.5])
    monkeypatch.setattr(m, "predict_model", predict)
    receipt = SimpleNamespace(relative_path="new-labels.parquet", sha256="d" * 64, rows=1)
    manifest = SimpleNamespace(final_holdout=SimpleNamespace(artifacts=SimpleNamespace(labels=receipt)))
    def assert_sealed() -> None:
        seal = json.loads((experiment.output / f"predictions/final/{world}/predictions-sealed.json").read_bytes())
        assert len(seal["predictions"]) == 6
        assert events.count("prediction") == 6
    def snapshot_read(*args: object, **kwargs: object) -> tuple:
        assert_sealed()
        events.append("snapshot")
        return handoff, manifest
    def read_labels(*args: object, **kwargs: object) -> pa.Table:
        assert_sealed()
        events.append("labels")
        return pa.table({"clicked": [True]})
    def score(*args: object, **kwargs: object) -> dict:
        assert_sealed()
        assert "labels" in events
        events.append("score")
        return {"valid": True}
    monkeypatch.setattr(m, "_validated_judge_snapshot", snapshot_read)
    monkeypatch.setattr(m, "read_verified", read_labels)
    monkeypatch.setattr(m, "score_predictions", score)
    monkeypatch.setattr(m, "summarize", lambda *args, **kwargs: {
        "coverage_valid": True, "comparisons": {"shallow": {"passed": True}}})
    result = experiment.evaluate_final()
    assert events == ["prediction"] * 6 + ["snapshot", "labels"] + ["score"] * 6
    assert result["final_claims"] == 1
    assert result["verdict"] == "not_supported"  # Development ineligibility cannot be rescued by final.
