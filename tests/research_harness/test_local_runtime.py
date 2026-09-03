"""실제 실행 연결의 입력 고정·validation memory 재생을 검증한다."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
import json
import shutil

import pytest

from autoresearch.research_harness.feedback import ExperimentCard
from autoresearch.research_harness.ledger import (
    CheckpointRecord, LedgerArtifactEvidence, open_trial_ledger,
)
from autoresearch.research_harness.local_runtime import (
    HarnessRunConfig, RevisionPlanner, LocalRuntimeError, _run_lock, _validate_locations,
    bind_input_checkpoint, load_run_config, run_local_research,
)
from autoresearch.research_harness.controller import ControllerConclusion, ControllerRunResult
from autoresearch.research_harness.judge_decision import JudgeMetric
from autoresearch.research_harness.local_evaluation_fixture import _io_path
from tests.research_harness.test_workspace import candidate_fixture as candidate_fixture


def test_planner_replays_one_feedback_revision_and_stops() -> None:
    card = ExperimentCard("experiment", "test hypothesis", "one change", "no gain")
    planner = RevisionPlanner()
    assert planner.next_card(card, ()) == card
    revised = planner.next_card(card, (object(),))
    assert revised.card_id == "experiment-revision-1"
    assert revised.hypothesis == card.hypothesis
    assert planner.next_card(card, (object(),)) == revised
    assert planner.next_card(card, (object(), object())) is None


def test_input_checkpoint_is_idempotent(tmp_path: Path) -> None:
    ledger = open_trial_ledger(tmp_path / "experiment-ledger.jsonl")
    artifact = LedgerArtifactEvidence("run-inputs", (tmp_path / "manifest.json").as_uri(), "a" * 64)
    bind_input_checkpoint(ledger, artifact)
    first = ledger.read_state()
    bind_input_checkpoint(ledger, artifact)
    assert ledger.read_state() == first
    assert first.checkpoint("run-inputs").artifacts == (artifact,)


def test_input_checkpoint_rejects_changed_digest(tmp_path: Path) -> None:
    ledger = open_trial_ledger(tmp_path / "experiment-ledger.jsonl")
    artifact = LedgerArtifactEvidence("run-inputs", (tmp_path / "manifest.json").as_uri(), "a" * 64)
    bind_input_checkpoint(ledger, artifact)
    with pytest.raises(LocalRuntimeError, match="run_inputs_checkpoint"):
        bind_input_checkpoint(ledger, replace(artifact, sha256="b" * 64))


def test_input_checkpoint_rejects_foreign_history_without_binding(tmp_path: Path) -> None:
    ledger = open_trial_ledger(tmp_path / "experiment-ledger.jsonl")
    ledger.append(CheckpointRecord("unbound", "setup", "run", datetime.now(UTC), (), None))
    artifact = LedgerArtifactEvidence("run-inputs", (tmp_path / "manifest.json").as_uri(), "a" * 64)
    with pytest.raises(LocalRuntimeError, match="run_inputs_checkpoint_missing"):
        bind_input_checkpoint(ledger, artifact)


@pytest.fixture()
def run_config(candidate_fixture, tmp_path: Path, tmp_path_factory) -> HarnessRunConfig:
    fixture, _ = candidate_fixture
    fixture_root = tmp_path_factory.mktemp("rt") / "fixtures/by-hash" / fixture.fixture_root.name
    shutil.copytree(_io_path(fixture.fixture_root), _io_path(fixture_root))
    (fixture_root / "final-holdout-consumed").mkdir()
    handoff = replace(fixture.judge, snapshot_root=fixture_root / "evaluation-snapshots/by-hash" / str(fixture.judge.snapshot_fingerprint))
    for name in ("repository", "workspaces", "run", "model", "cache"):
        (tmp_path / name).mkdir()
    from dataclasses import asdict
    payload = {
        "repository_root": str(tmp_path / "repository"), "workspace_parent": str(tmp_path / "workspaces"),
        "run_root": str(tmp_path / "run"), "handoff": asdict(handoff),
        "fixture_descriptor_sha256": fixture.descriptor_sha256,
        "baseline_sha": "a" * 40, "champion_sha": "a" * 40,
        "initial_card": asdict(ExperimentCard("test", "hypothesis", "change", "failure")),
        "screening_seed": 42, "confirmation_seeds": [101, 102, 103, 104, 105],
        "baseline_sigmas": {metric.value: 0.01 for metric in JudgeMetric},
        "prediction": {"embedding": {
            "model_id": "test/model", "revision": "b" * 40,
            "model_dir": str(tmp_path / "model"), "cache_dir": str(tmp_path / "cache"),
        }},
        "agent": {"executable": str(tmp_path / "codex.exe"), "model": "test-model",
                  "reasoning_effort": "medium", "timeout_seconds": 60.0},
    }
    return HarnessRunConfig.model_validate_json(json.dumps(payload, default=str))


def test_config_preserves_only_explicit_training_overrides(run_config: HarnessRunConfig) -> None:
    assert "training" not in run_config.prediction.model_dump(mode="json", exclude_unset=True)


@pytest.mark.parametrize("updates", [
    {"screening_seed": True}, {"screening_seed": 101}, {"confirmation_seeds": [1] * 5},
    {"baseline_sigmas": {"ndcg_at_10": 0.1}}, {"max_trials": 3},
    {"max_duration_seconds": float("inf")}, {"repository_root": "relative"},
])
def test_config_rejects_invalid_execution_contract(run_config, updates) -> None:
    payload = run_config.model_dump(mode="json")
    payload.update(updates)
    with pytest.raises(ValueError):
        HarnessRunConfig.model_validate_json(json.dumps(payload))


def test_missing_configuration_is_safe(tmp_path: Path) -> None:
    with pytest.raises(LocalRuntimeError, match="configuration") as failure:
        load_run_config(tmp_path / "private-name.json")
    assert "private-name" not in str(failure.value)


def test_shallow_snapshot_path_has_safe_failure(run_config) -> None:
    shallow = Path(run_config.run_root.anchor) / "x"
    invalid = run_config.model_copy(update={"handoff": replace(run_config.handoff, snapshot_root=shallow)})
    with pytest.raises(LocalRuntimeError, match="snapshot_location"):
        _validate_locations(invalid)


def test_same_run_rejects_concurrent_controller(tmp_path: Path) -> None:
    with _run_lock(tmp_path):
        with pytest.raises(LocalRuntimeError, match="run_already_active"):
            with _run_lock(tmp_path):
                pytest.fail("two controllers entered")
    with _run_lock(tmp_path):
        pass


def test_runtime_restores_metadata_without_source_preparation(run_config, monkeypatch) -> None:
    import autoresearch.research_harness.local_runtime as runtime
    import autoresearch.research_harness.candidate_data_view as views
    import autoresearch.research_harness.local_trial_runner as trials
    import autoresearch.research_harness.report as report

    monkeypatch.setattr(runtime, "_runtime_identity", lambda _: "{}")
    captured = []
    monkeypatch.setattr(trials, "LocalResearchTrialRunner", lambda **kwargs: captured.append(kwargs))
    result = ControllerRunResult(ControllerConclusion.INCONCLUSIVE, "a" * 40, 0, (), None, "already_consumed", None)
    published = []
    monkeypatch.setattr(report, "publish_research_report", lambda *args, **kwargs: published.append(kwargs))

    class Controller:
        def __init__(self, *args):
            pass

        def run(self, request):
            assert request.ledger.read_state().completed("run-inputs")
            return result

    monkeypatch.setattr(runtime, "ResearchController", Controller)
    assert run_local_research(run_config) == result
    first = captured[0]["validation_metadata"]
    def unexpected(*args, **kwargs):
        pytest.fail("resume queried metadata source")
    monkeypatch.setattr(views, "prepare_candidate_metadata", unexpected)
    monkeypatch.setattr(views, "prepare_final_candidate_metadata", unexpected)
    monkeypatch.setattr(runtime, "ResearchController", unexpected)
    monkeypatch.setattr(trials, "LocalResearchTrialRunner", unexpected)
    assert run_local_research(run_config) == result
    assert captured[0]["validation_metadata"] == first
    assert "training" not in captured[0]["prediction_config"]
    assert len(published) == 2
    assert (run_config.run_root / "controller-result.json").exists()
    drifted = run_config.model_copy(update={"screening_seed": 43})
    with pytest.raises(LocalRuntimeError):
        run_local_research(drifted)
    assert len(captured) == 1


def test_runtime_does_not_initialize_registry(run_config) -> None:
    registry = run_config.handoff.snapshot_root.parents[2] / "final-holdout-consumed"
    registry.rmdir()  # This test owns the empty copied fixture registry.
    with pytest.raises(LocalRuntimeError, match="registry_missing"):
        run_local_research(run_config)
    assert not registry.exists()
    assert not (run_config.run_root / "run-inputs").exists()


def test_cli_requires_configuration() -> None:
    from autoresearch.cli import app
    from typer.testing import CliRunner
    from click import unstyle
    result = CliRunner().invoke(app, ["harness-run"])
    assert result.exit_code == 2
    assert "--config" in unstyle(result.output)


def test_cli_delegates_and_does_not_print_private_paths(monkeypatch, tmp_path) -> None:
    from autoresearch.cli import app
    from typer.testing import CliRunner
    import autoresearch.research_harness.local_runtime as runtime
    sentinel = object()
    seen = []
    monkeypatch.setattr(runtime, "load_run_config", lambda path: seen.append(path) or sentinel)
    result_value = ControllerRunResult(ControllerConclusion.NO_IMPROVEMENT, "a" * 40, 2, (), None, "unchanged", None)
    def run(config):
        assert config is sentinel
        return result_value
    monkeypatch.setattr(runtime, "run_local_research", run)
    result = CliRunner().invoke(app, ["harness-run", "--config", str(tmp_path / "private.json")])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"conclusion": "no_improvement", "validation_trials": 2, "final_reason_code": "unchanged"}
    assert seen == [tmp_path / "private.json"]


@pytest.mark.parametrize("failure", [LocalRuntimeError("configuration"), OSError("secret local path")])
def test_cli_errors_are_nonzero_and_safe(monkeypatch, failure) -> None:
    from autoresearch.cli import app
    from typer.testing import CliRunner
    import autoresearch.research_harness.local_runtime as runtime
    def load(path):
        raise failure
    monkeypatch.setattr(runtime, "load_run_config", load)
    result = CliRunner().invoke(app, ["harness-run", "--config", "private.json"])
    assert result.exit_code == 1
    assert "harness_runtime_failed" in result.output
    assert "secret local path" not in result.output


def test_snapshot_mutation_is_rejected_before_agent(run_config, monkeypatch) -> None:
    import autoresearch.research_harness.local_runtime as runtime
    monkeypatch.setattr(runtime, "_runtime_identity", lambda _: pytest.fail("invalid snapshot reached runtime"))
    path = _io_path(run_config.handoff.snapshot_root / "_SUCCESS")
    path.write_text("corrupt", encoding="utf-8")
    with pytest.raises(LocalRuntimeError, match="judge_handoff_validation"):
        run_local_research(run_config)


def test_invalid_model_does_not_become_contextlib_type_error(run_config) -> None:
    with pytest.raises(LocalRuntimeError, match="local_embedding_model_invalid"):
        run_local_research(run_config)


def test_cli_report_failure_is_translated_without_private_traceback(monkeypatch, tmp_path) -> None:
    from autoresearch.cli import app
    from typer.testing import CliRunner
    import autoresearch.research_harness.local_runtime as runtime
    from autoresearch.research_harness.report import ReportError

    monkeypatch.setattr(runtime, "load_run_config", lambda _: object())
    def fail_report(config):
        with runtime._run_lock(tmp_path):
            raise ReportError("terminal_binding_conflict")
    monkeypatch.setattr(runtime, "run_local_research", fail_report)
    response = CliRunner().invoke(app, ["harness-run", "--config", "private-name.json"])
    assert response.exit_code == 1
    assert "report_terminal_binding_conflict" in response.output
    assert "private-name" not in response.output and "Traceback" not in response.output
