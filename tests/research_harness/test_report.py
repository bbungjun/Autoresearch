"""종료 결과 결속과 단일 advisory Judge·REPORT의 재개 계약을 검증한다."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
from hashlib import sha256
import importlib
import html
import json
import os
from pathlib import Path

import pytest

from autoresearch.research_harness.coding_agent import AgentUsage, CodingAgentReceipt, CodingAgentError
from autoresearch.research_harness.consumption_registry import FinalConsumptionEvidence
from autoresearch.research_harness.controller import ControllerConclusion, ControllerRunResult, _feedback_from_record, _feedback_metrics, _ledger_metrics
from autoresearch.research_harness.judge_decision import JudgeDecision
from autoresearch.research_harness.ledger import CheckpointRecord, LedgerArtifactEvidence, LedgerMetric, TrialRecord, open_trial_ledger
from autoresearch.research_harness.local_runtime import bind_input_checkpoint
from tests.research_harness.test_run_inputs import case as case, prepared as prepared, candidate_fixture as candidate_fixture, _freeze
from tests.research_harness.test_controller import _score_for


def module():
    return importlib.import_module("autoresearch.research_harness.report")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, default=str, sort_keys=True), encoding="utf-8")


def evidence(path: Path) -> LedgerArtifactEvidence:
    return LedgerArtifactEvidence(path.name + ":" + sha256(str(path).encode()).hexdigest()[:8],
                                  path.as_uri(), sha256(path.read_bytes()).hexdigest())


@pytest.fixture()
def finished(case):
    _, root, contract, _, _ = case
    frozen = _freeze(case)
    ledger = open_trial_ledger(root / "experiment-ledger.jsonl")
    bind_input_checkpoint(ledger, frozen.artifact)
    result = ControllerRunResult(ControllerConclusion.INCONCLUSIVE, contract.champion_sha,
                                 0, (), None, "already_consumed", None)
    parent = root.parent / "judge-workspaces"
    parent.mkdir()
    return root, contract, result, parent


class FakeJudge:
    def __init__(self, failure: str | None = None):
        self.requests = []
        self.failure = failure

    def run(self, request):
        self.requests.append(request)
        assert request.mode == "read-only" and list(request.cwd.iterdir()) == []
        assert request.candidate_inputs is None
        assert not request.cwd.is_relative_to(request.artifact_root.parent)
        request.artifact_root.mkdir()
        response = {"status": "consistent", "summary": "관측 결과와 일치합니다.",
                    "findings": [], "limitations": ["미측정 비용"]}
        if self.failure == "schema":
            response["new_metric"] = 99
        write_json(request.artifact_root / "response.json", response)
        (request.artifact_root / "prompt.txt").write_bytes(request.prompt.encode("utf-8"))
        write_json(request.artifact_root / "schema.json", request.output_schema)
        for name in ("stdout.log", "stderr.log"):
            (request.artifact_root / name).write_text("")
        usage = AgentUsage(100, 40, 10, 3)
        write_json(request.artifact_root / "receipt.json", {
            "model": "test-model", "reasoning_effort": "medium", "mode": "read-only",
            "approval_policy": "never", "windows_sandbox": "elevated" if os.name == "nt" else None,
            "duration_ms": 25, "exit_code": 0, "usage": asdict(usage), "cost_usd": None,
            "stdout_truncated": False, "stderr_truncated": False,
            "error_code": "agent_cleanup_failed" if self.failure == "cleanup" else None,
        })
        if self.failure == "interrupt":
            raise KeyboardInterrupt
        if self.failure == "cleanup":
            raise CodingAgentError("agent_cleanup_failed", "cleanup")
        return CodingAgentReceipt(response, tuple(evidence(p) for p in request.artifact_root.iterdir()), usage, 25)


def publish(finished, judge=None):
    root, contract, result, parent = finished
    m = module()
    m.seal_terminal_result(root, contract=contract, result=result)
    return m.publish_research_report(root, contract=contract, result=result,
                                     judge=judge or FakeJudge(), judge_workspace_parent=parent)


def test_terminal_binding_replays_and_rejects_deleted_binding(finished):
    root, contract, result, _ = finished
    m = module()
    assert m.load_terminal_result(root, contract=contract) is None
    m.seal_terminal_result(root, contract=contract, result=result)
    assert m.load_terminal_result(root, contract=contract) == result
    (root / "controller-result-binding.json").unlink()
    with pytest.raises(m.ReportError):
        m.load_terminal_result(root, contract=contract)


def test_terminal_binding_recovers_missing_result_without_execution(finished):
    root, contract, result, _ = finished
    m = module()
    m.seal_terminal_result(root, contract=contract, result=result)
    expected = (root / "controller-result.json").read_bytes()
    (root / "controller-result.json").unlink()
    assert m.load_terminal_result(root, contract=contract) == result
    assert (root / "controller-result.json").read_bytes() == expected


@pytest.mark.parametrize("mutation", ["result", "ledger", "input", "hardlink"])
def test_terminal_drift_is_rejected(finished, mutation):
    root, contract, result, _ = finished
    m = module()
    m.seal_terminal_result(root, contract=contract, result=result)
    if mutation == "result":
        write_json(root / "controller-result.json", asdict(replace(result, champion_sha="b" * 40)))
    elif mutation == "ledger":
        open_trial_ledger(root / "experiment-ledger.jsonl").append(
            CheckpointRecord("extra", "extra", "run", datetime.now(UTC), (), None))
    elif mutation == "input":
        contract = replace(contract, screening_seed=7)
    else:
        os.link(root / "controller-result.json", root / "alias.json")
    with pytest.raises(m.ReportError):
        m.load_terminal_result(root, contract=contract)


def test_report_runs_fresh_judge_once_and_keeps_final_unknown(finished):
    root, contract, result, _ = finished
    judge = FakeJudge()
    receipt = publish(finished, judge)
    first = {p.name: p.read_bytes() for p in root.glob("research-*.*")}
    publish(finished, judge)
    assert len(judge.requests) == 1
    assert {p.name: p.read_bytes() for p in root.glob("research-*.*")} == first
    record = json.loads((root / "research-record.json").read_bytes())
    assert record["outcome"]["conclusion"] == "inconclusive"
    assert record["outcome"]["final_mean"] is None
    assert record["outcome"]["validation_champion_sha"] == result.champion_sha
    assert record["outcome"]["baseline_retained"] is True
    assert str(contract.handoff.snapshot_root) not in judge.requests[0].prompt
    assert str(root) not in judge.requests[0].prompt
    assert "already_consumed" in html.unescape((root / "research-report.md").read_text(encoding="utf-8"))
    assert receipt.report_path == root / "research-report.md"


@pytest.mark.parametrize("failure", ["cleanup", "schema"])
def test_judge_failure_is_advisory_and_is_not_retried(finished, failure):
    root, _, _, _ = finished
    judge = FakeJudge(failure)
    publish(finished, judge)
    publish(finished, judge)
    assert len(judge.requests) == 1
    review = json.loads((root / "research-judge.json").read_bytes())
    assert review["availability"] == "unavailable"


def test_successful_attempt_recovers_after_interruption_without_recall(finished):
    judge = FakeJudge("interrupt")
    with pytest.raises(KeyboardInterrupt):
        publish(finished, judge)
    judge.failure = None
    publish(finished, judge)
    assert len(judge.requests) == 1
    assert json.loads((finished[0] / "research-judge.json").read_bytes())["availability"] == "available"


def test_orphan_attempt_costs_use_one_duration_and_one_token_receipt(finished):
    root, _, _, _ = finished
    attempt = root / "attempts" / ("a" * 32)
    write_json(attempt / "attempt.json", {"stage": "prepare", "trial_id": "trial-0001", "seed": None, "started_at_unix_ns": 1})
    write_json(attempt / "failure.json", {"duration_ms": 80, "stage": "prepare", "reason_code": "trial_interrupted"})
    write_json(attempt / "candidate.json", {"duration_ms": 50, "usage": {"input_tokens": 999}})
    write_json(attempt / "agent/receipt.json", {"duration_ms": 40, "usage": {"input_tokens": 100, "cached_input_tokens": 40,
               "output_tokens": 10, "reasoning_output_tokens": None}, "cost_usd": None})
    publish(finished)
    record = json.loads((root / "research-record.json").read_bytes())
    assert record["cost"]["duration_ms"]["observed_sum"] == 80
    assert record["cost"]["tokens"]["input_tokens"]["observed_sum"] == 100
    assert record["cost"]["tokens"]["reasoning_output_tokens"]["observed_sum"] is None
    assert record["attempts"][0]["linked_to_trial"] is False
    assert "repair_candidate_sha" not in record["attempts"][0]["candidate"]
    before = (root / "research-record.json").read_bytes()
    publish(finished)
    assert (root / "research-record.json").read_bytes() == before


@pytest.mark.parametrize("repair_sha", [None, "a" * 40])
def test_report_preserves_optional_repair_provenance(finished, repair_sha) -> None:
    root = finished[0]
    attempt = root / "attempts" / ("a" * 32)
    write_json(attempt / "attempt.json", {"stage": "prepare", "trial_id": "trial-0002", "seed": None, "started_at_unix_ns": 1})
    write_json(attempt / "candidate.json", {"duration_ms": 1, "repair_candidate_sha": repair_sha})
    publish(finished)
    record = json.loads((root / "research-record.json").read_bytes())
    assert record["attempts"][0]["candidate"]["repair_candidate_sha"] == repair_sha


@pytest.mark.parametrize("repair_sha", ["b" * 40, "c" * 40, None])
def test_report_checks_repair_against_previous_failed_ledger_trial(finished, repair_sha) -> None:
    root, contract, result, parent = finished
    ledger = open_trial_ledger(root / "experiment-ledger.jsonl")
    feedback = []
    for number in (1, 2):
        trial_id = f"trial-{number:04d}"
        artifacts = ()
        if number == 2:
            attempt = root / "attempts" / ("a" * 32)
            write_json(attempt / "attempt.json", {"stage": "prepare", "trial_id": trial_id, "seed": None, "started_at_unix_ns": 1})
            write_json(attempt / "candidate.json", {"trial_id": trial_id, "base_sha": contract.baseline_sha,
                       "candidate_sha": "b" * 40, "diff_fingerprint": "sha256:" + "d" * 64,
                       "repair_candidate_sha": repair_sha, "duration_ms": 1})
            artifacts = (evidence(attempt / "candidate.json"),)
        trial = TrialRecord(trial_id, "validation", contract.baseline_sha, "b" * 40,
                            "sha256:" + "d" * 64, str(contract.handoff.validation_id), contract.screening_seed,
                            (), "failed", "candidate_crashed", 1, "candidate_crashed", artifacts,
                            (contract.baseline_sha,), None, contract.initial_card.canonical_summary(),
                            failure_stage="pair")
        ledger.append(trial)
        ledger.append(CheckpointRecord(trial_id + ":validation-recorded", "validation_recorded", trial_id,
                                      datetime.now(UTC), artifacts, None))
        feedback.append(_feedback_from_record(contract.initial_card, contract.initial_card, trial, feedback))
    finished = root, contract, replace(result, validation_trials=2, feedback_history=tuple(feedback)), parent
    if repair_sha == "b" * 40:
        publish(finished)
    else:
        with pytest.raises(module().ReportError, match="candidate_repair_identity"):
            publish(finished)


def final_case(finished, *, count=5, wrong_role=False, champion=None, candidate_value=0.5, feedback=(), deltas=()):
    root, contract, _, parent = finished
    champion = champion or contract.baseline_sha
    lineage = (contract.baseline_sha,) if champion == contract.baseline_sha else (contract.baseline_sha, champion)
    artifacts = []
    for seed in contract.confirmation_seeds[:count]:
        attempt = root / "attempts" / f"{seed:032x}"
        write_json(attempt / "attempt.json", {"stage": "final", "trial_id": "final-holdout", "seed": seed, "started_at_unix_ns": seed})
        write_json(attempt / "pair.json", {"baseline_sha": contract.baseline_sha, "candidate_sha": "b" * 40 if wrong_role else champion,
                   "seed": seed, "duration_ms": 20, "baseline": asdict(_score_for(0.5, contract.handoff.final_holdout_id)),
                   "candidate": asdict(_score_for(candidate_value, contract.handoff.final_holdout_id))})
        artifacts.append(evidence(attempt / "pair.json"))
    marker = FinalConsumptionEvidence(contract.judge_state_root / "final-holdout-consumed" / str(contract.handoff.final_holdout_id), "f" * 64)
    trial = TrialRecord("final-holdout", "final_holdout", contract.baseline_sha,
                        champion if champion != contract.baseline_sha else None,
                        "sha256:" + "d" * 64 if champion != contract.baseline_sha else None,
                        str(contract.handoff.final_holdout_id), 1,
                        _ledger_metrics(_feedback_metrics(_score_for(candidate_value, contract.handoff.final_holdout_id), deltas)) if count == 5 else (),
                        "discard" if count == 5 else "inconclusive", "primary_threshold_not_met" if count == 5 else "prediction_failed",
                        100, None if count == 5 else "prediction_failed", tuple(artifacts), lineage, marker,
                        failure_stage=None if count == 5 else "pair")
    ledger = open_trial_ledger(root / "experiment-ledger.jsonl")
    ledger.append(trial)
    ledger.append(CheckpointRecord("final-holdout:final-recorded", "final_recorded", "final-holdout",
                                  datetime.now(UTC), tuple(artifacts), marker))
    result = ControllerRunResult(ControllerConclusion.NO_IMPROVEMENT if count == 5 else ControllerConclusion.INCONCLUSIVE,
                                 champion, len(feedback), feedback, JudgeDecision.DISCARD if count == 5 else None,
                                 "primary_threshold_not_met" if count == 5 else "prediction_failed", marker)
    return root, contract, result, parent


def test_final_five_pairs_supply_role_specific_mean_even_if_baseline_is_champion(finished):
    finished = final_case(finished)
    root = finished[0]
    publish(finished)
    record = json.loads((root / "research-record.json").read_bytes())
    assert record["outcome"]["final_mean"]["baseline"]["ndcg_at_10"] == 0.5
    assert record["outcome"]["final_mean"]["candidate"]["ndcg_at_10"] == 0.5


def test_partial_final_pairs_are_not_displayed_as_representative_mean(finished):
    finished = final_case(finished, count=2)
    publish(finished)
    outcome = json.loads((finished[0] / "research-record.json").read_bytes())["outcome"]
    assert outcome["final_mean"] is None and outcome["observed_final_pair_count"] == 2


def test_final_pair_wrong_role_sha_fails_even_if_digest_matches_ledger(finished):
    with pytest.raises(module().ReportError, match="pair_code_identity"):
        publish(final_case(finished, wrong_role=True))


def test_validation_promote_lineage_is_not_confused_with_final_adoption(finished):
    root, contract, _, _ = finished
    trial = TrialRecord("trial-0001", "validation", contract.baseline_sha, "b" * 40,
                        "sha256:" + "d" * 64, str(contract.handoff.validation_id), contract.screening_seed,
                        (LedgerMetric("ndcg_at_10", 0.7),), "promote", "promotion_threshold_met", 100, None, (),
                        (contract.baseline_sha, "b" * 40), None, contract.initial_card.canonical_summary())
    ledger = open_trial_ledger(root / "experiment-ledger.jsonl")
    ledger.append(trial)
    ledger.append(CheckpointRecord("trial-0001:validation-recorded", "validation_recorded", trial.trial_id,
                                  datetime.now(UTC), (), None))
    feedback = (_feedback_from_record(contract.initial_card, contract.initial_card, trial, []),)
    finished = final_case(finished, champion="b" * 40, candidate_value=0.4, feedback=feedback)
    publish(finished)
    record = json.loads((root / "research-record.json").read_bytes())
    assert record["outcome"]["validation_champion_sha"] == "b" * 40
    assert record["outcome"]["baseline_retained"] is True
    assert record["outcome"]["final_mean"]["baseline"]["ndcg_at_10"] == 0.5
    assert record["outcome"]["final_mean"]["candidate"]["ndcg_at_10"] == 0.4


@pytest.mark.parametrize("mutation", ["intent_deleted", "intent_changed", "response", "receipt", "review_partial"])
def test_published_report_revalidates_original_judge_evidence(finished, mutation):
    root = finished[0]
    judge = FakeJudge()
    publish(finished, judge)
    if mutation == "intent_deleted":
        (root / "research-judge-intent.json").unlink()
    elif mutation == "intent_changed":
        write_json(root / "research-judge-intent.json", {"different": "record"})
    elif mutation == "review_partial":
        (root / "research-report-manifest.json").unlink()
        review = json.loads((root / "research-judge.json").read_bytes())
        review["response"]["summary"] = "modified review"
        write_json(root / "research-judge.json", review)
    else:
        path = root / "research-judge-attempt" / f"{mutation}.json"
        value = json.loads(path.read_bytes())
        if mutation == "response":
            value["summary"] = "modified response"
        else:
            value["usage"]["input_tokens"] = 999
        write_json(path, value)
    with pytest.raises(module().ReportError):
        publish(finished, judge)
    assert len(judge.requests) == 1


def test_intent_only_is_unavailable_and_never_calls_judge_again(finished):
    class Interrupted(FakeJudge):
        def run(self, request):
            self.requests.append(request)
            raise KeyboardInterrupt
    judge = Interrupted()
    with pytest.raises(KeyboardInterrupt):
        publish(finished, judge)
    publish(finished, judge)
    assert len(judge.requests) == 1
    assert json.loads((finished[0] / "research-judge.json").read_bytes())["availability"] == "unavailable"


def test_failed_judge_preserves_observed_cost_and_evidence(finished):
    publish(finished, FakeJudge("cleanup"))
    review = json.loads((finished[0] / "research-judge.json").read_bytes())
    assert review["availability"] == "unavailable"
    assert review["usage"]["input_tokens"] == 100 and review["duration_ms"] == 25
    assert review["evidence"]["receipt.json"]


def test_record_and_prompt_explain_shared_evaluation_identity(finished):
    judge = FakeJudge()
    publish(finished, judge)
    record = json.loads((finished[0] / "research-record.json").read_bytes())
    assert "MUST share" in record["semantics"]["evaluation_id"]
    assert "not an identity collision" in judge.requests[0].prompt
    assert "validation champion is not final adoption" in judge.requests[0].prompt


def test_report_lock_rejects_concurrent_publication(finished):
    class Concurrent(FakeJudge):
        def run(self, request):
            with pytest.raises(module().ReportError, match="report_already_active"):
                publish(finished, self)
            return super().run(request)
    judge = Concurrent()
    publish(finished, judge)
    assert len(judge.requests) == 1


def test_free_text_is_redacted_and_markdown_is_inert(finished):
    root, contract, _, _ = finished
    attempt = root / "attempts" / ("e" * 32)
    write_json(attempt / "attempt.json", {"stage": "prepare", "trial_id": "trial-0001", "seed": None, "started_at_unix_ns": 1})
    write_json(attempt / "agent-explanation.json", {"experiment_summary": f"<img src=x> [click](https://unsafe.invalid) {contract.judge_state_root}",
               "changes": ["ghp_" + "A" * 36], "claimed_improvement": None})
    judge = FakeJudge()
    publish(finished, judge)
    record = (root / "research-record.json").read_text()
    markdown = (root / "research-report.md").read_text(encoding="utf-8")
    assert "ghp_" not in record and "ghp_" not in judge.requests[0].prompt
    assert str(contract.judge_state_root) not in judge.requests[0].prompt
    assert "<img" not in markdown and "](https://" not in markdown
    assert "&lt;img" in markdown


def test_credentials_are_redacted_from_judge_output_too(finished):
    class CredentialJudge(FakeJudge):
        def run(self, request):
            receipt = super().run(request)
            response = dict(receipt.response, summary="ghp_" + "A" * 36)
            write_json(request.artifact_root / "response.json", response)
            return replace(receipt, response=response,
                           artifacts=tuple(evidence(path) for path in request.artifact_root.iterdir()))
    judge = CredentialJudge()
    publish(finished, judge)
    publish(finished, judge)
    assert "ghp_" not in (finished[0] / "research-judge.json").read_text()


def test_bare_www_email_and_html_are_inert_without_corrupting_entities():
    original = "www.example.com review@example.com <b>'quote'</b> [link](https://example.com)"
    rendered = module()._safe_text(original)
    assert "www." not in rendered and "@" not in rendered and "<b>" not in rendered
    assert html.unescape(rendered) == original


def test_judge_workspace_cleanup_failure_is_durable_unavailable(finished, monkeypatch):
    m = module()
    original = m.TemporaryDirectory
    class BrokenCleanup:
        def __init__(self, **kwargs):
            self.temporary = original(**kwargs)
        def __enter__(self):
            return self.temporary.__enter__()
        def __exit__(self, *args):
            self.temporary.__exit__(*args)
            raise OSError("simulated cleanup error")
    monkeypatch.setattr(m, "TemporaryDirectory", BrokenCleanup)
    judge = FakeJudge()
    publish(finished, judge)
    monkeypatch.setattr(m, "TemporaryDirectory", original)
    publish(finished, judge)
    assert len(judge.requests) == 1
    review = json.loads((finished[0] / "research-judge.json").read_bytes())
    assert review["availability"] == "unavailable"
    assert review["reason_code"] == "judge_workspace_failed"


def test_report_output_drift_never_reuses_or_calls_judge_again(finished):
    judge = FakeJudge()
    publish(finished, judge)
    (finished[0] / "research-report.md").write_text("tampered")
    with pytest.raises(module().ReportError):
        publish(finished, judge)
    assert len(judge.requests) == 1


def test_input_access_sidecar_is_preserved_as_source_evidence(finished):
    root = finished[0]
    attempt = root / "attempts" / ("c" * 32)
    write_json(attempt / "attempt.json", {"stage": "prepare", "trial_id": "trial-0001", "seed": None, "started_at_unix_ns": 1})
    sidecar = attempt / "agent/input-access.json"
    write_json(sidecar, {"version": "candidate-input-access-v1", "status": "failed", "applied_count": 1,
                        "principal": {"name": "CodexSandboxUsers", "sid_sha256": "a" * 64}})
    publish(finished)
    record = json.loads((root / "research-record.json").read_bytes())
    assert record["sources"][sidecar.relative_to(root).as_posix()] == sha256(sidecar.read_bytes()).hexdigest()
