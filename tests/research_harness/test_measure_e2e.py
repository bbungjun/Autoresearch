"""실제 E2E 수동 측정 wrapper의 비변경 관측·durable 중단·단일 호출 계약."""

from dataclasses import replace
from datetime import UTC, datetime
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoresearch.research_harness.ledger import CheckpointRecord, LedgerAppendReceipt, TrialLedger, open_trial_ledger


def module():
    return importlib.import_module("scripts.research_harness.measure_e2e")


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    m = module()
    fixture = tmp_path / "fixture"
    for name in ("run", "workspaces", "model", "cache", "fixture"):
        (tmp_path / name).mkdir()
    config = SimpleNamespace(
        run_root=tmp_path / "run", workspace_parent=tmp_path / "workspaces",
        handoff=SimpleNamespace(snapshot_root=fixture / "evaluation-snapshots/by-hash" / ("a" * 64),
                                final_holdout_id="eval_" + "b" * 64),
        prediction=SimpleNamespace(embedding=SimpleNamespace(model_dir=tmp_path / "model", cache_dir=tmp_path / "cache")),
    )
    source = tmp_path / "config.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(m, "load_run_config", lambda _: config)
    return m, config, source, tmp_path


def checkpoint() -> CheckpointRecord:
    return CheckpointRecord("trial-0001:validation-recorded", "validation_recorded", "trial-0001",
                            datetime(2026, 9, 3, tzinfo=UTC), (), None)


def test_observation_never_recovers_or_changes_a_partial_ledger(case, monkeypatch: pytest.MonkeyPatch) -> None:
    m, config, _, _ = case
    ledger = open_trial_ledger(config.run_root / "experiment-ledger.jsonl")
    ledger.append(checkpoint())
    with ledger.path.open("ab") as stream:
        stream.write(b'{"partial":')
    original = ledger.path.read_bytes()
    before = {path.name for path in config.run_root.iterdir()}
    monkeypatch.setattr(TrialLedger, "read_state", lambda _: pytest.fail("observer repaired ledger"))
    import autoresearch.research_harness.ledger as ledger_module
    monkeypatch.setattr(ledger_module, "open_trial_ledger", lambda _: pytest.fail("observer opened mutable ledger"))
    observed = m.observe_run(config)
    assert observed["ledger"]["status"] == "partial"
    assert observed["ledger"]["trailing_bytes"] == len(b'{"partial":')
    assert observed["ledger"]["checkpoints"][0]["checkpoint_id"] == checkpoint().checkpoint_id
    assert ledger.path.read_bytes() == original
    assert {path.name for path in config.run_root.iterdir()} == before
    assert not (config.handoff.snapshot_root.parents[2] / "final-holdout-consumed").exists()


def test_interrupt_happens_only_after_new_durable_target_append_and_restores_method(case, monkeypatch: pytest.MonkeyPatch) -> None:
    m, config, source, root = case
    original_append = TrialLedger.append
    called = []
    def run(_: object) -> None:
        ledger = open_trial_ledger(config.run_root / "experiment-ledger.jsonl")
        ledger.append(replace(checkpoint(), checkpoint_id="unrelated"))
        called.append("before-target")
        ledger.append(checkpoint())
        pytest.fail("runtime passed interruption checkpoint")
    monkeypatch.setattr(m, "run_local_research", run)
    result = m.measure_run(source, root / "measurement", interrupt_after_first_validation=True)
    assert result["status"] == "interrupted" and result["interruption_injected"] is True
    assert called == ["before-target"] and TrialLedger.append is original_append
    assert open_trial_ledger(config.run_root / "experiment-ledger.jsonl").read_state().completed(checkpoint().checkpoint_id)
    assert result["runtime_seconds"] >= 0 and result["total_seconds"] >= result["runtime_seconds"]


@pytest.mark.parametrize("condition", ["other-ledger", "wrong-stage", "idempotent"])
def test_append_wrapper_does_not_interrupt_nonmatching_receipts(case, monkeypatch: pytest.MonkeyPatch, condition: str) -> None:
    m, config, _, root = case
    target = config.run_root / "experiment-ledger.jsonl"
    created = condition != "idempotent"
    monkeypatch.setattr(TrialLedger, "append", lambda *_: LedgerAppendReceipt(0, created))
    original_append = TrialLedger.append
    ledger = TrialLedger(root / "other.jsonl" if condition == "other-ledger" else target)
    record = replace(checkpoint(), stage="other") if condition == "wrong-stage" else checkpoint()
    with m.interrupt_after_checkpoint(target):
        assert ledger.append(record).created is created
    assert TrialLedger.append is original_append


@pytest.mark.parametrize("kind", ["existing-checkpoint", "partial", "corrupt"])
def test_interruption_preflight_rejects_existing_or_unreadable_checkpoint(case, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    m, config, source, root = case
    ledger = open_trial_ledger(config.run_root / "experiment-ledger.jsonl")
    if kind == "existing-checkpoint":
        ledger.append(checkpoint())
    else:
        ledger.path.write_bytes(b"broken\n" if kind == "corrupt" else b"partial")
    original = ledger.path.read_bytes()
    monkeypatch.setattr(m, "run_local_research", lambda _: pytest.fail("preflight must stop"))
    result = m.measure_run(source, root / "measurement", interrupt_after_first_validation=True)
    assert result["status"] == "failed" and result["runtime_seconds"] is None
    assert result["interruption_injected"] is False
    assert ledger.path.read_bytes() == original


def test_runtime_failure_is_recorded_once_without_raw_exception_text(case, monkeypatch: pytest.MonkeyPatch) -> None:
    m, _, source, root = case
    original_append = TrialLedger.append
    calls = []
    def run(_: object) -> None:
        calls.append(1)
        raise RuntimeError("secret local data path")
    monkeypatch.setattr(m, "run_local_research", run)
    result = m.measure_run(source, root / "measurement", interrupt_after_first_validation=True)
    assert result["status"] == "failed" and len(calls) == 1
    assert result["interruption_injected"] is False and TrialLedger.append is original_append
    assert "secret local data path" not in (root / "measurement/measurement.json").read_text()


@pytest.mark.parametrize(("stage", "expected"), [("registry_missing", "registry_missing"), ("C:/private/input.json", None)])
def test_runtime_failure_preserves_only_safe_stage(case, monkeypatch: pytest.MonkeyPatch, stage: str, expected: str | None) -> None:
    from autoresearch.research_harness.local_runtime import LocalRuntimeError
    m, _, source, root = case
    def run(_: object) -> None:
        raise LocalRuntimeError(stage)
    monkeypatch.setattr(m, "run_local_research", run)
    result = m.measure_run(source, root / "measurement")
    assert result["status"] == "failed" and result["error_stage"] == expected
    assert "C:/private" not in (root / "measurement/measurement.json").read_text()


def test_requested_interruption_not_reached_is_not_completed(case, monkeypatch: pytest.MonkeyPatch) -> None:
    m, _, source, root = case
    monkeypatch.setattr(m, "run_local_research", lambda _: SimpleNamespace(
        conclusion=SimpleNamespace(value="inconclusive"), validation_trials=0, final_reason_code="already_consumed"))
    result = m.measure_run(source, root / "measurement", interrupt_after_first_validation=True)
    assert result["status"] == "failed" and result["interruption_injected"] is False
    assert result["error_code"] == "interruption_not_reached"


def test_malformed_attempt_metadata_is_unavailable_without_repair(case) -> None:
    m, config, _, _ = case
    directory = config.run_root / "attempts" / ("a" * 32)
    directory.mkdir(parents=True)
    path = directory / "attempt.json"
    path.write_text('{"stage": {}, "trial_id": "trial-0001"}')
    original = path.read_bytes()
    result = m.observe_run(config)
    assert result["inventory_status"] == "unavailable"
    assert result["files"]["attempts/" + "a" * 32 + "/attempt.json"]["sha256"]
    assert path.read_bytes() == original


def test_existing_and_overlapping_output_are_rejected_before_runtime(case, monkeypatch: pytest.MonkeyPatch) -> None:
    m, config, source, root = case
    monkeypatch.setattr(m, "run_local_research", lambda _: pytest.fail("invalid output executed runtime"))
    for output in (config.run_root, config.run_root / "measurement", config.prediction.embedding.model_dir / "measurement"):
        with pytest.raises(m.MeasurementError):
            m.measure_run(source, output)
    assert not (config.run_root / "measurement").exists()


def test_observation_separates_trial_two_from_trial_one_and_hashes_judge_and_marker(case) -> None:
    m, config, _, _ = case
    def attempt(identifier: str, trial: str) -> None:
        directory = config.run_root / "attempts" / identifier
        (directory / "agent").mkdir(parents=True)
        (directory / "attempt.json").write_text(json.dumps({"stage": "prepare", "trial_id": trial, "seed": None}))
        (directory / "candidate.json").write_text("{}")
        (directory / "agent/receipt.json").write_text("{}")
    attempt("a" * 32, "trial-0001")
    before = m.observe_run(config)
    attempt("b" * 32, "trial-0002")
    judge = config.run_root / "research-judge-attempt"
    judge.mkdir()
    (judge / "prompt.txt").write_text("private prompt")
    registry = config.handoff.snapshot_root.parents[2] / "final-holdout-consumed"
    registry.mkdir()
    (registry / config.handoff.final_holdout_id).write_text("{}")
    after = m.observe_run(config)
    changes = m.compare_observations(before, after)
    assert after["trials"]["trial-0001"] == before["trials"]["trial-0001"]
    assert after["trials"]["trial-0002"]["prepare_attempts"] == 1
    assert "research-judge-attempt/prompt.txt" in changes["added"]
    assert after["files"]["final-marker"]["sha256"]
    assert not changes["changed"]
    assert "attempts/" + "a" * 32 + "/candidate.json" in changes["unchanged"]
    assert "private prompt" not in json.dumps(after)


def test_terminal_recall_observation_shows_no_new_evidence(case, monkeypatch: pytest.MonkeyPatch) -> None:
    m, config, source, root = case
    for name in ("controller-result.json", "controller-result-binding.json", "research-report.md"):
        (config.run_root / name).write_text("{}")
    calls = []
    def run(_: object) -> SimpleNamespace:
        calls.append(1)
        return SimpleNamespace(conclusion=SimpleNamespace(value="discard"), validation_trials=2, final_reason_code="no_improvement")
    monkeypatch.setattr(m, "run_local_research", run)
    result = m.measure_run(source, root / "measurement")
    assert result["status"] == "completed" and len(calls) == 1
    assert result["changes"]["added"] == result["changes"]["changed"] == result["changes"]["removed"] == []
    assert result["config_sha256"] and result["script_sha256"]
    assert result["cost_usd"] is None
