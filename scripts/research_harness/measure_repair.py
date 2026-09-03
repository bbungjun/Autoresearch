"""사전 주입 candidate 결함 한 건과 실제 agent 수정 한 번의 수동 측정 경계.

[파이프라인] 준비된 fixture/calibration 뒤 기존 Controller 학습·평가·보고 호출 구간이다.
[기능] 첫 coding 요청만 deterministic fault fixture로 대체하고, 다음 요청의 복원된
코드·실패 feedback을 확인한 뒤 원래 agent에 위임한다. 호출 의도/반환/실패를 따로 보존한다.
[비책임] 결함은 자연 발생 agent 오류가 아니다. 실패 코드 복원·학습·판정·REPORT는
runtime 소유이며, 이 모듈은 정답 patch·자동 재시도·fixture/registry 준비·성공 판정을 하지 않는다.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from time import perf_counter

from autoresearch.research_harness.coding_agent import (
    AgentUsage, CodingAgentReceipt, CodingAgentRequest, CodexCodingAgent,
)
from autoresearch.research_harness.ledger import LedgerArtifactEvidence
from autoresearch.research_harness.local_runtime import HarnessRunConfig, _validate_locations
from scripts.research_harness import measure_e2e as e2e


TARGET = Path("autoresearch/research_harness/local_training.py")
_HEALTHY = b"x_prediction = prediction.features.to_pandas()"
_BROKEN = b"x_prediction = prediction.featuers.to_pandas()"


def _target_bytes(request: CodingAgentRequest, config: HarnessRunConfig) -> tuple[Path, bytes]:
    target = request.cwd / TARGET
    if (not request.cwd.is_relative_to(config.workspace_parent) or request.cwd == config.workspace_parent
            or not request.artifact_root.is_relative_to(config.run_root)
            or not e2e._resolved_without_link(target) or not target.is_file()):
        raise e2e.MeasurementError("unexpected_coding_workspace")
    return target, e2e.read_file(target)


def _seed_fault(request: CodingAgentRequest, config: HarnessRunConfig, out: Path) -> tuple[CodingAgentReceipt, str]:
    started = perf_counter()
    target, before = _target_bytes(request, config)
    if before.count(_HEALTHY) != 1 or _BROKEN in before:
        raise e2e.MeasurementError("fault_anchor_not_unique")
    after = before.replace(_HEALTHY, _BROKEN, 1)
    before_sha, after_sha = sha256(before).hexdigest(), sha256(after).hexdigest()
    # This is the sole fault-fixture write, not an agent repair or a scoring change.
    intent = {"source": "deterministic_fault_fixture", "external_calls": 0, "path": TARGET.as_posix(),
              "before_sha256": before_sha, "after_sha256": after_sha}
    e2e._write_json(out / "seed-fault-intent.json", intent)
    target.write_bytes(after)
    if e2e.file_digest(target) != after_sha:
        raise e2e.MeasurementError("fault_write_changed")
    e2e._write_json(out / "seed-fault.json", intent)
    response = {"status": "implemented", "experiment_summary":
                "Measurement fixture injected one deterministic attribute typo; no external coding agent was called.",
                "changes": ["Seeded an intentional local_training prediction attribute defect for recovery observation."],
                "tests": ["No training or tests executed by the fault fixture."], "claimed_improvement": None}
    request.artifact_root.mkdir(parents=True, exist_ok=False)
    e2e._write_json(request.artifact_root / "response.json", response)
    e2e._write_json(request.artifact_root / "seed-fault.json", intent)
    artifacts = tuple(LedgerArtifactEvidence("measurement-" + name, (request.artifact_root / name).as_uri(),
                                           e2e.file_digest(request.artifact_root / name))
                      for name in ("response.json", "seed-fault.json"))
    return CodingAgentReceipt(response, artifacts, AgentUsage(0, 0, 0, 0),
                              round((perf_counter() - started) * 1000)), after_sha


def _verify_repair_start(request: CodingAgentRequest, config: HarnessRunConfig, out: Path, broken_sha: str) -> None:
    _, content = _target_bytes(request, config)
    if sha256(content).hexdigest() != broken_sha:
        raise e2e.MeasurementError("failed_candidate_not_restored")
    try:
        payload = json.loads(request.prompt[request.prompt.index("\n{") + 1:])
        feedback = payload["validation_feedback"][-1]
        failure = feedback["failure"]
        if (feedback["trial_id"] != "trial-0001" or feedback["decision"] != "failed"
                or failure["reason_code"] != "predict_crash" or not isinstance(failure["stderr_tail"], str)
                or "featuers" not in failure["stderr_tail"]):
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError):
        raise e2e.MeasurementError("failed_feedback_not_connected") from None
    e2e._write_json(out / "repair-start.json", {
        "restored_sha256": broken_sha, "feedback_trial_id": feedback["trial_id"],
        "feedback_reason_code": failure["reason_code"],
        "failure_log_sha256": sha256(failure["stderr_tail"].encode()).hexdigest(),
        "prompt_sha256": sha256(request.prompt.encode()).hexdigest(),
        "scope": "checks restored bytes and supplied failure feedback; independent receipt audit still required",
    })


@contextmanager
def repair_agent_scope(config: HarnessRunConfig, out: Path) -> Iterator[dict]:
    """한 run 구간에 fixture 1회, 실제 write/read 각 최대 1회만 허용하고 seam을 복원한다.

    원래 agent에 동일 request를 전달하며 반환 값을 변조하지 않는다. 카운트는 외부 호출
    경계 진입 의도이지 성공한 모델 호출 수가 아니다. 오류 시 예외 원문은 복제하지 않는다.
    """
    original = CodexCodingAgent.run
    state = {"seed_requests": 0, "external_call_intents": {"workspace-write": 0, "read-only": 0},
             "external_completed": {"workspace-write": 0, "read-only": 0}}
    broken_sha: str | None = None

    def run(agent: CodexCodingAgent, request: CodingAgentRequest) -> CodingAgentReceipt:
        nonlocal broken_sha
        if request.mode not in state["external_call_intents"]:
            raise e2e.MeasurementError("unexpected_agent_mode")
        if request.mode == "workspace-write" and state["seed_requests"] == 0:
            state["seed_requests"] += 1
            receipt, broken_sha = _seed_fault(request, config, out)
            return receipt
        if state["external_call_intents"][request.mode] >= 1:
            raise e2e.MeasurementError("external_call_limit")
        if request.mode == "workspace-write":
            if broken_sha is None:
                raise e2e.MeasurementError("fault_not_completed")
            _verify_repair_start(request, config, out, broken_sha)
        prefix = "external-" + request.mode
        e2e._write_json(out / (prefix + "-intent.json"), {"mode": request.mode,
            "prompt_sha256": sha256(request.prompt.encode()).hexdigest(),
            "artifact_root": str(request.artifact_root), "source": "original_CodexCodingAgent.run"})
        state["external_call_intents"][request.mode] += 1
        started = perf_counter()
        try:
            receipt = original(agent, request)
        except BaseException as error:
            e2e._write_json(out / (prefix + "-result.json"), {
                "status": "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed",
                "error_type": type(error).__name__, "error_code": e2e._error_identifier(getattr(error, "code", None)),
                "duration_seconds": perf_counter() - started})
            raise
        state["external_completed"][request.mode] += 1
        e2e._write_json(out / (prefix + "-result.json"), {"status": "returned", "usage": asdict(receipt.usage),
            "agent_duration_ms": receipt.duration_ms, "duration_seconds": perf_counter() - started,
            "scope": "adapter returned a receipt; not a recovery or quality verdict"})
        return receipt

    CodexCodingAgent.run = run
    try:
        yield state
    finally:
        CodexCodingAgent.run = original


def measure_repair(config_path: Path, out: Path) -> dict:
    """사전 준비된 빈 run만 한 번 실행한다. 전후 증거와 별도 복구 측정 sidecar를 남긴다.

    Args:
        config_path: 사전 준비된 고정 HarnessRunConfig JSON.
        out: 입력 및 run/workspace와 겹치지 않는 새 절대 측정 경로.

    Returns:
        Runtime 상태와 호출 관측. recovery_success/human_interventions/cost는 추정하지 않는다.
    """
    config_sha = e2e.file_digest(config_path)
    config = e2e.load_run_config(config_path)
    e2e._validate_output(out, config)
    # Runtime requires pre-existing roots and registry. Do not create/reset them here.
    fixture_root = _validate_locations(config)
    if not e2e._resolved_without_link(config.run_root) or any(config.run_root.iterdir()):
        raise e2e.MeasurementError("empty_run_required")
    if os.path.lexists(fixture_root / "final-holdout-consumed" / str(config.handoff.final_holdout_id)):
        raise e2e.MeasurementError("final_already_consumed")
    if config.max_trials != 2:
        raise e2e.MeasurementError("two_trial_budget_required")
    if e2e.file_digest(config_path) != config_sha:
        raise e2e.MeasurementError("config_changed")
    with repair_agent_scope(config, out) as state:
        measured = e2e.measure_run(config_path, out)
    result = {"version": "agent-repair-measurement-v1", "runtime_status": measured["status"],
              "runtime_result": measured.get("result"), "calls": state, "config_sha256": config_sha,
              "script_sha256": e2e.file_digest(Path(__file__).absolute()),
              "recovery_success": None, "human_interventions": None, "cost_usd": None,
              "autonomy_scope": "single operator invocation, excluding pre-run setup; no human intervention observer installed",
              "interpretation": "seed is an operator fixture, not a natural agent error; receipts require independent audit"}
    e2e._write_json(out / "repair-measurement.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = measure_repair(args.config.absolute(), args.out.absolute())
    except Exception:
        print("repair_measurement_failed; inspect local evidence", file=sys.stderr)
        return 1
    print(json.dumps({"status": result["runtime_status"], "recovery_success": None}))
    return 0 if result["runtime_status"] == "completed" else 130 if result["runtime_status"] == "interrupted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
