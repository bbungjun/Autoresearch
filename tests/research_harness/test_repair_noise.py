"""실패 복구 뒤 sigma 부족 판정과 실행 오류의 Controller·REPORT 결속 회귀.

실제 수치 비교·ledger·REPORT를 사용하고 학습·coding agent·기록 Judge·소비 grant는
대역으로 격리한다. 실제 모델 학습 또는 자율 코드 수정의 실측 근거는 아니다.
"""

from dataclasses import asdict, replace
import html
import json
from pathlib import Path

import pytest

from autoresearch.research_harness.controller import (
    ControllerConclusion, ControllerRunRequest, FinalPairRequest, PairedRunReceipt,
    PrepareCandidateRequest, PreparedCandidate, ResearchController, TrialExecutionError,
    ValidationPairRequest,
)
from autoresearch.research_harness.domain import ResearchDomain, YouTubeCTRDomain
from autoresearch.research_harness.judge_decision import JudgeMetric, PairedJudgeResult
from autoresearch.research_harness.ledger import open_trial_ledger
from autoresearch.research_harness.local_runtime import bind_input_checkpoint
from tests.research_harness.test_controller import (
    FakeRunner, SequencePlanner, _install_final_claim, _score_for,
)
from tests.research_harness.test_report import FakeJudge, evidence, publish, write_json
from tests.research_harness.test_run_inputs import (
    _freeze, candidate_fixture as candidate_fixture, case as case, prepared as prepared,
)


@pytest.mark.parametrize("final_execution_fails", [False, True])
def test_repaired_execution_and_zero_recall_noise_remain_distinct_in_report(
    case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, final_execution_fails: bool,
) -> None:
    _install_final_claim(monkeypatch, tmp_path)
    module, root, original, validation, final = case
    contract = replace(original, baseline_sigmas=tuple(
        (name, 0.0 if name == JudgeMetric.RECALL_AT_10.value else value)
        for name, value in original.baseline_sigmas
    ))
    frozen = _freeze((module, root, contract, validation, final))
    ledger = open_trial_ledger(root / "experiment-ledger.jsonl")
    bind_input_checkpoint(ledger, frozen.artifact)

    class RepairedRunner(FakeRunner):
        def prepare_candidate(self, request: PrepareCandidateRequest) -> PreparedCandidate:
            candidate = super().prepare_candidate(request)
            return replace(candidate, candidate_sha="b" * 40 if request.trial_id == "trial-0001" else request.champion_sha)

        def run_validation(self, request: ValidationPairRequest, domain: ResearchDomain) -> PairedRunReceipt:
            self.validation_runs.append(request)
            if request.candidate.trial_id == "trial-0001":
                raise TrialExecutionError("pair", "candidate_crashed")
            score = _score_for(0.5, request.handoff.validation_id)
            return PairedRunReceipt(PairedJudgeResult(request.seed, score, score), 10, ())

        def run_final(self, request: FinalPairRequest, domain: ResearchDomain) -> PairedRunReceipt:
            self.final_runs.append(request)
            if final_execution_fails:
                raise TrialExecutionError("pair", "candidate_crashed")
            score = _score_for(0.5, request.handoff.final_holdout_id)
            pair = PairedJudgeResult(request.seed, score, score)
            attempt = root / "attempts" / f"{request.seed:032x}"
            write_json(attempt / "attempt.json", {"stage": "final", "trial_id": "final-holdout",
                       "seed": request.seed, "started_at_unix_ns": request.seed})
            write_json(attempt / "pair.json", {"baseline_sha": request.baseline_sha,
                       "candidate_sha": request.candidate_sha, "seed": request.seed,
                       "duration_ms": 10, "baseline": asdict(score), "candidate": asdict(score)})
            return PairedRunReceipt(pair, 10, (evidence(attempt / "pair.json"),))

    runner = RepairedRunner()
    controller = ResearchController(YouTubeCTRDomain(), SequencePlanner((contract.initial_card,) * 2, []), runner)
    request = ControllerRunRequest(
        contract.initial_card, contract.budget, contract.baseline_sha, contract.champion_sha,
        contract.handoff, contract.judge_state_root, contract.baseline_sigmas,
        contract.screening_seed, contract.confirmation_seeds, ledger,
    )
    result = controller.run(request)
    expected_reason = "candidate_crashed" if final_execution_fails else "insufficient_baseline_noise"
    assert result.conclusion is ControllerConclusion.INCONCLUSIVE
    assert result.final_decision is None and result.final_reason_code == expected_reason
    assert result.champion_sha == contract.baseline_sha
    assert [item.decision for item in result.feedback_history] == ["failed", "discard"]
    assert runner.preparations[1].repair_candidate_sha == "b" * 40
    assert result.feedback_history[1].failure is None
    assert len(runner.final_runs) == (1 if final_execution_fails else 5)
    terminal = ledger.read_state().trials[-1]
    assert terminal.failure_reason_code == ("candidate_crashed" if final_execution_fails else None)
    assert bool(terminal.metrics) is not final_execution_fails

    judge_parent = tmp_path / "judge-workspaces"
    judge_parent.mkdir()
    judge = FakeJudge()
    publish((root, contract, result, judge_parent), judge)
    record_bytes = (root / "research-record.json").read_bytes()
    record = json.loads(record_bytes)
    assert record["outcome"]["final_reason_code"] == expected_reason
    assert record["outcome"]["baseline_retained"] is True
    assert record["outcome"]["observed_final_pair_count"] == (0 if final_execution_fails else 5)
    assert (record["outcome"]["final_mean"] is None) is final_execution_fails
    assert expected_reason in html.unescape((root / "research-report.md").read_text(encoding="utf-8"))
    assert controller.run(request) == result
    publish((root, contract, result, judge_parent), judge)
    assert (root / "research-record.json").read_bytes() == record_bytes
    assert len(judge.requests) == 1
    assert len(runner.final_runs) == (1 if final_execution_fails else 5)
