"""실증 측정 구간의 결함 주입·실제 호출 상한·복원 계약을 네트워크 없이 검증한다."""

from contextlib import nullcontext
from hashlib import sha256
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoresearch.research_harness.coding_agent import AgentUsage, CodingAgentReceipt, CodingAgentRequest, CodexCodingAgent


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    m = importlib.import_module("scripts.research_harness.measure_repair")
    for name in ("repository", "run", "workspaces", "model", "cache", "fixture", "fixture/final-holdout-consumed"):
        (tmp_path / name).mkdir()
    config = SimpleNamespace(
        repository_root=tmp_path / "repository", run_root=tmp_path / "run",
        workspace_parent=tmp_path / "workspaces", max_trials=2,
        handoff=SimpleNamespace(snapshot_root=tmp_path / "fixture/evaluation-snapshots/by-hash" / ("a" * 64),
                                final_holdout_id="eval_" + "b" * 64),
        prediction=SimpleNamespace(embedding=SimpleNamespace(model_dir=tmp_path / "model", cache_dir=tmp_path / "cache")),
    )
    source = tmp_path / "config.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(m.e2e, "load_run_config", lambda _: config)
    return m, config, source, tmp_path


def request(root: Path, number: int, *, mode: str = "workspace-write", broken: bool = False,
            feedback: bool = False) -> CodingAgentRequest:
    cwd = root / "workspaces" / str(number)
    target = cwd / "autoresearch/research_harness/local_training.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x_prediction = prediction.featuers.to_pandas()\n" if broken
                       else b"x_prediction = prediction.features.to_pandas()\n")
    payload = {"validation_feedback": ([{"trial_id": "trial-0001", "decision": "failed",
               "failure": {"reason_code": "predict_crash", "stderr_tail": "AttributeError: featuers"}}] if feedback else [])}
    return CodingAgentRequest(cwd, "instructions\n" + json.dumps(payload), {}, root / "run" / str(number) / "agent", mode)


def receipt() -> CodingAgentReceipt:
    return CodingAgentReceipt({"status": "implemented"}, (), AgentUsage(input_tokens=5, output_tokens=2), 3)


def test_seed_is_explicit_fixture_and_second_call_sees_same_broken_bytes(case, monkeypatch: pytest.MonkeyPatch) -> None:
    m, config, _, root = case
    out = root / "measurement"
    out.mkdir()
    calls = []
    def original(agent: object, req: CodingAgentRequest) -> CodingAgentReceipt:
        calls.append(req)
        return receipt()
    monkeypatch.setattr(CodexCodingAgent, "run", original)
    agent = CodexCodingAgent(None)
    first = request(root, 1)
    second = request(root, 2, broken=True, feedback=True)
    before_second = (second.cwd / m.TARGET).read_bytes()
    with m.repair_agent_scope(config, out) as state:
        seeded = agent.run(first)
        assert seeded.response["status"] == "implemented" and seeded.usage.input_tokens == 0
        assert calls == [] and seeded.artifacts
        assert not (first.artifact_root / "receipt.json").exists()
        assert agent.run(second) == receipt()
        agent.run(request(root, 3, mode="read-only"))
    assert CodexCodingAgent.run is original and len(calls) == 2
    assert calls[0] is second and calls[1].mode == "read-only"
    assert (second.cwd / m.TARGET).read_bytes() == before_second
    assert state["external_call_intents"] == {"workspace-write": 1, "read-only": 1}
    fault = json.loads((out / "seed-fault.json").read_text())
    assert fault["external_calls"] == 0 and fault["source"] == "deterministic_fault_fixture"
    assert fault["before_sha256"] != fault["after_sha256"]
    repair = json.loads((out / "repair-start.json").read_text())
    assert repair["restored_sha256"] == sha256(before_second).hexdigest()
    assert repair["feedback_trial_id"] == "trial-0001"


@pytest.mark.parametrize("bad", ["healthy", "missing-feedback", "wrong-trial", "wrong-error", "no-error-log"])
def test_repair_preflight_never_calls_real_agent_or_patches_bad_start(case, monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    m, config, _, root = case
    out = root / "measurement"
    out.mkdir()
    monkeypatch.setattr(CodexCodingAgent, "run", lambda *_: pytest.fail("bad preflight delegated"))
    agent = CodexCodingAgent(None)
    second = request(root, 2, broken=bad != "healthy", feedback=bad != "missing-feedback")
    if bad in {"wrong-trial", "wrong-error", "no-error-log"}:
        from dataclasses import replace
        prompt = second.prompt.replace("trial-0001", "trial-0000") if bad == "wrong-trial" else (
            second.prompt.replace("predict_crash", "predict_timeout") if bad == "wrong-error" else
            second.prompt.replace("AttributeError: featuers", ""))
        second = replace(second, prompt=prompt)
    before = (second.cwd / m.TARGET).read_bytes()
    with m.repair_agent_scope(config, out):
        agent.run(request(root, 1))
        with pytest.raises(m.e2e.MeasurementError):
            agent.run(second)
    assert (second.cwd / m.TARGET).read_bytes() == before


@pytest.mark.parametrize("mode", ["workspace-write", "read-only"])
def test_more_than_one_real_call_per_mode_is_blocked(case, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    m, config, _, root = case
    out = root / "measurement"
    out.mkdir()
    calls = []
    monkeypatch.setattr(CodexCodingAgent, "run", lambda *args: calls.append(args) or receipt())
    agent = CodexCodingAgent(None)
    with m.repair_agent_scope(config, out):
        agent.run(request(root, 1))
        agent.run(request(root, 2, broken=True, feedback=True))
        if mode == "read-only":
            agent.run(request(root, 3, mode=mode))
        with pytest.raises(m.e2e.MeasurementError):
            agent.run(request(root, 4, mode=mode, broken=True, feedback=True))
    assert len(calls) == (2 if mode == "read-only" else 1)


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_exception_restores_method_and_preserves_external_intent_error(case, monkeypatch: pytest.MonkeyPatch, error_type: type) -> None:
    m, config, _, root = case
    out = root / "measurement"
    out.mkdir()
    def original(*_: object) -> CodingAgentReceipt:
        raise error_type("private text")
    monkeypatch.setattr(CodexCodingAgent, "run", original)
    agent = CodexCodingAgent(None)
    with pytest.raises(error_type), m.repair_agent_scope(config, out) as state:
        agent.run(request(root, 1))
        agent.run(request(root, 2, broken=True, feedback=True))
    assert CodexCodingAgent.run is original
    assert state["external_call_intents"]["workspace-write"] == 1
    result = (out / "external-workspace-write-result.json").read_text()
    assert "private text" not in result and error_type.__name__ in result


def test_duplicate_fault_anchor_is_rejected_without_edit(case) -> None:
    m, config, _, root = case
    out = root / "measurement"
    out.mkdir()
    req = request(root, 1)
    target = req.cwd / m.TARGET
    target.write_bytes(target.read_bytes() * 2)
    before = target.read_bytes()
    with m.repair_agent_scope(config, out), pytest.raises(m.e2e.MeasurementError):
        CodexCodingAgent(None).run(req)
    assert target.read_bytes() == before


@pytest.mark.parametrize("invalid", ["existing-run", "existing-output", "overlap", "wrong-budget"])
def test_invalid_measurement_is_rejected_before_runtime(case, monkeypatch: pytest.MonkeyPatch, invalid: str) -> None:
    m, config, source, root = case
    out = root / "measurement"
    if invalid == "existing-run":
        (config.run_root / "experiment-ledger.jsonl").write_text("existing")
    elif invalid == "existing-output":
        out.mkdir()
    elif invalid == "overlap":
        out = config.prediction.embedding.model_dir / "measurement"
    else:
        config.max_trials = 1
    monkeypatch.setattr(m.e2e, "measure_run", lambda *_: pytest.fail("invalid request executed"))
    with pytest.raises(m.e2e.MeasurementError):
        m.measure_repair(source, out)


def test_wrapper_calls_existing_measurement_once_and_does_not_infer_success(case, monkeypatch: pytest.MonkeyPatch) -> None:
    from autoresearch.research_harness.local_runtime import _validate_locations
    m, config, source, root = case
    calls = []
    def measure(config_path: Path, output: Path) -> dict:
        # Exercise the actual runtime filesystem preconditions, not just the fake call.
        assert _validate_locations(config) == root / "fixture"
        calls.append(config_path)
        output.mkdir()
        return {"status": "completed", "result": {"conclusion": "no_improvement"}}
    monkeypatch.setattr(m.e2e, "measure_run", measure)
    monkeypatch.setattr(m, "repair_agent_scope", lambda *_: nullcontext({"external_call_intents": {}}))
    result = m.measure_repair(source, root / "measurement")
    assert calls == [source] and result["runtime_status"] == "completed"
    assert result["recovery_success"] is None and result["human_interventions"] is None
    assert result["cost_usd"] is None


def test_consumed_final_is_rejected_before_measurement_and_preserved(case, monkeypatch: pytest.MonkeyPatch) -> None:
    m, config, source, root = case
    marker = root / "fixture/final-holdout-consumed" / config.handoff.final_holdout_id
    marker.write_bytes(b"existing final consumption")
    monkeypatch.setattr(m.e2e, "measure_run", lambda *_: pytest.fail("consumed final executed"))
    with pytest.raises(m.e2e.MeasurementError, match="final_already_consumed"):
        m.measure_repair(source, root / "measurement")
    assert marker.read_bytes() == b"existing final consumption"


def test_actual_measurement_wrapper_preserves_cancel_and_never_retries(case, monkeypatch: pytest.MonkeyPatch) -> None:
    m, _, source, root = case
    calls = []
    def original(*_: object) -> CodingAgentReceipt:
        calls.append("external")
        raise KeyboardInterrupt
    monkeypatch.setattr(CodexCodingAgent, "run", original)
    def run(_: object) -> None:
        calls.append("runtime")
        agent = CodexCodingAgent(None)
        agent.run(request(root, 1))
        agent.run(request(root, 2, broken=True, feedback=True))
    monkeypatch.setattr(m.e2e, "run_local_research", run)
    monkeypatch.setattr(m.e2e, "observe_run", lambda _: {"ledger": {}, "files": {}, "trials": {}})
    result = m.measure_repair(source, root / "measurement")
    assert result["runtime_status"] == "interrupted"
    assert calls == ["runtime", "external"] and CodexCodingAgent.run is original
    assert result["calls"]["external_completed"]["workspace-write"] == 0
    assert (root / "measurement/after.json").exists()
    assert (root / "measurement/repair-measurement.json").exists()


def test_fault_receipt_is_write_once_and_existing_evidence_is_preserved(case) -> None:
    m, config, _, root = case
    out = root / "measurement"
    out.mkdir()
    (out / "seed-fault-intent.json").write_bytes(b"existing evidence")
    req = request(root, 1)
    before = (req.cwd / m.TARGET).read_bytes()
    with m.repair_agent_scope(config, out), pytest.raises(FileExistsError):
        CodexCodingAgent(None).run(req)
    assert (req.cwd / m.TARGET).read_bytes() == before
    assert (out / "seed-fault-intent.json").read_bytes() == b"existing evidence"
