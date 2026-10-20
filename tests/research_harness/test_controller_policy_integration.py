"""Controller의 고정 정책 전달·오류·재개와 synthetic final 단일 소비를 검증한다."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from collections.abc import Sequence

import pytest

from autoresearch.research_harness.controller import (
    ControllerConclusion, ControllerError, ControllerErrorCode, PairedRunReceipt,
    ResearchController, TrialExecutionError, ValidationPairRequest, FinalPairRequest,
)
from autoresearch.research_harness.domain import ResearchDomain, YouTubeCTRDomain
from autoresearch.research_harness.evaluation_snapshot_models import EvaluationId
from autoresearch.research_harness.fixture_models import JudgeSnapshotHandoff
from autoresearch.research_harness.judge_decision import (
    ConfirmationDecision, JudgeMetric, PairedJudgeResult, ScreeningResult,
)
from tests.research_harness.test_consumption_registry import (
    _case, source_handoff as source_handoff,
)
from tests.research_harness.test_controller import (
    FakeDomain, FakeRunner, SequencePlanner, _card, _install_final_claim, _request, _score_for,
)


class SyntheticRunner(FakeRunner):
    """훈련 없이 준비된 합성 점수만 돌려주며 Domain·Judge 판정은 실제로 실행한다."""

    def run_validation(self, request: ValidationPairRequest, domain: ResearchDomain) -> PairedRunReceipt:
        del domain
        self.validation_runs.append(request)
        return self._receipt(request.seed, request.handoff.validation_id)

    def run_final(self, request: FinalPairRequest, domain: ResearchDomain) -> PairedRunReceipt:
        del domain
        self.final_runs.append(request)
        return self._receipt(request.seed, request.handoff.final_holdout_id)

    @staticmethod
    def _receipt(seed: int, evaluation_id: EvaluationId) -> PairedRunReceipt:
        baseline, candidate = _score_for(0.5, evaluation_id), _score_for(0.7, evaluation_id)
        baseline = replace(baseline, recall_at_10=replace(baseline.recall_at_10, value=1.0))
        candidate = replace(candidate, recall_at_10=replace(candidate.recall_at_10, value=1.0))
        return PairedRunReceipt(PairedJudgeResult(seed, baseline, candidate), 1, ())


@pytest.mark.parametrize("policy", ["v1", "v2"])
def test_controller_real_judge_validation_final_and_completed_resume(
    tmp_path: Path, source_handoff: JudgeSnapshotHandoff, policy: str,
) -> None:
    state_root, handoff, _ = _case(source_handoff, tmp_path)
    request = replace(
        _request(tmp_path, max_trials=1), handoff=handoff, judge_state_root=state_root,
        judge_policy_version=policy,
        baseline_sigmas=tuple((metric.value, 0.0 if metric is JudgeMetric.RECALL_AT_10 else 0.01)
                              for metric in JudgeMetric),
    )
    runner = SyntheticRunner()
    cards = (_card("card-2"),)
    result = ResearchController(YouTubeCTRDomain(), SequencePlanner(cards, []), runner).run(request)
    assert len(runner.validation_runs) == 6
    assert len(runner.final_runs) == 5
    assert result.conclusion is (ControllerConclusion.IMPROVED if policy == "v2"
                                 else ControllerConclusion.INCONCLUSIVE)
    records = request.ledger.read_state().trials
    assert [record.split for record in records] == ["validation", "final_holdout"]
    assert all(record.judge_policy_version == policy for record in records)
    if policy == "v2":
        assert all(record.decision == "promote" for record in records)
        assert all(record.zero_variance_guardrails == ("recall_at_10",) for record in records)
    else:
        assert all(record.reason_code == "insufficient_baseline_noise" for record in records)
    marker = result.final_consumption.marker_path
    marker_bytes = marker.read_bytes()
    assert sha256(marker_bytes).hexdigest() == result.final_consumption.marker_sha256
    ledger_bytes = request.ledger.path.read_bytes()
    resumed_runner = SyntheticRunner()
    resumed = ResearchController(YouTubeCTRDomain(), SequencePlanner(cards, []), resumed_runner).run(request)
    assert resumed == result
    assert resumed_runner.preparations == resumed_runner.validation_runs == resumed_runner.final_runs == []
    assert marker.read_bytes() == marker_bytes
    assert request.ledger.path.read_bytes() == ledger_bytes


def test_controller_rejects_mismatched_confirmation_before_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_final_claim(monkeypatch, tmp_path)

    class WrongVersionDomain(FakeDomain):
        def compare(self, results: PairedJudgeResult | Sequence[PairedJudgeResult], **kwargs: object) -> ScreeningResult | ConfirmationDecision:
            compared = super().compare(results, **kwargs)
            return replace(compared, judge_policy_version="v1") if isinstance(results, tuple) else compared

    request = replace(_request(tmp_path, max_trials=1), judge_policy_version="v2")
    runner = FakeRunner()
    with pytest.raises(ControllerError) as failure:
        ResearchController(WrongVersionDomain(), SequencePlanner((_card("card-2"),), []), runner).run(request)
    assert failure.value.code is ControllerErrorCode.INTEGRITY_VIOLATION
    assert failure.value.stage == "judge_policy_result"
    assert request.ledger.read_state().trials == ()
    assert runner.final_runs == []


def test_controller_rejects_mixed_policy_history_before_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_final_claim(monkeypatch, tmp_path)
    request = _request(tmp_path, max_trials=1)
    ResearchController(FakeDomain(), SequencePlanner((_card("card-2"),), []), FakeRunner()).run(request)
    before = request.ledger.path.read_bytes()
    runner = FakeRunner()
    with pytest.raises(ControllerError, match="judge_policy_replay"):
        ResearchController(FakeDomain(), SequencePlanner((_card("card-2"),), []), runner).run(
            replace(request, judge_policy_version="v2"),
        )
    assert request.ledger.path.read_bytes() == before
    assert runner.preparations == runner.validation_runs == runner.final_runs == []


def test_controller_records_v2_execution_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_final_claim(monkeypatch, tmp_path)

    class FailingFinalRunner(FakeRunner):
        def run_final(self, request: FinalPairRequest, domain: ResearchDomain) -> PairedRunReceipt:
            raise TrialExecutionError("final", "synthetic_failure")

    request = replace(_request(tmp_path, max_trials=2), judge_policy_version="v2")
    runner = FailingFinalRunner(fail_first=True)
    ResearchController(FakeDomain(), SequencePlanner((_card("card-1"), _card("card-2")), []), runner).run(request)
    records = request.ledger.read_state().trials
    assert len(records) == 3
    assert all(record.judge_policy_version == "v2" for record in records)
    assert records[0].decision == "failed"
    assert records[-1].failure_reason_code == "synthetic_failure"


def test_controller_records_invalid_v2_result_with_requested_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_final_claim(monkeypatch, tmp_path)
    request = replace(
        _request(tmp_path, max_trials=1), judge_policy_version="v2",
        baseline_sigmas=tuple((metric.value, 0.0) for metric in JudgeMetric),
    )
    ResearchController(YouTubeCTRDomain(), SequencePlanner((_card("card-2"),), []), SyntheticRunner()).run(request)
    records = request.ledger.read_state().trials
    assert len(records) == 2
    assert all(record.judge_policy_version == "v2" for record in records)
    assert all(record.reason_code == "insufficient_baseline_noise" for record in records)
    assert records[0].candidate_sha != records[0].champion_lineage[-1]


def test_controller_rejects_mismatched_final_before_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_final_claim(monkeypatch, tmp_path)

    class WrongFinalVersionDomain(FakeDomain):
        def compare(self, results: PairedJudgeResult | Sequence[PairedJudgeResult], **kwargs: object) -> ScreeningResult | ConfirmationDecision:
            compared = super().compare(results, **kwargs)
            if isinstance(results, tuple) and results[0].baseline.evaluation_id != _request_id:
                return replace(compared, judge_policy_version="v1")
            return compared

    request = replace(_request(tmp_path, max_trials=1), judge_policy_version="v2")
    _request_id = request.handoff.validation_id
    runner = FakeRunner()
    with pytest.raises(ControllerError, match="judge_policy_result"):
        ResearchController(WrongFinalVersionDomain(), SequencePlanner((_card("card-2"),), []), runner).run(request)
    records = request.ledger.read_state().trials
    assert len(records) == 1
    assert records[0].split == "validation"
    assert records[0].judge_policy_version == "v2"
    assert len(runner.final_runs) == 5


def test_v2_interrupted_controller_reuses_validation_and_preserves_single_consumption(
    tmp_path: Path, source_handoff: JudgeSnapshotHandoff, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autoresearch.research_harness.controller as controller

    state_root, handoff, _ = _case(source_handoff, tmp_path)
    request = replace(
        _request(tmp_path, max_trials=1), handoff=handoff, judge_state_root=state_root,
        judge_policy_version="v2",
        baseline_sigmas=tuple((metric.value, 0.0 if metric is JudgeMetric.RECALL_AT_10 else 0.01)
                              for metric in JudgeMetric),
    )
    actual_claim = controller.claim_final_consumption
    claim_calls = []

    def interrupted_claim(consumption_request):
        claim_calls.append(consumption_request)
        if len(claim_calls) == 1:
            raise KeyboardInterrupt
        return actual_claim(consumption_request)

    monkeypatch.setattr(controller, "claim_final_consumption", interrupted_claim)
    cards = (_card("card-2"),)
    initial_runner = SyntheticRunner()
    with pytest.raises(KeyboardInterrupt):
        ResearchController(YouTubeCTRDomain(), SequencePlanner(cards, []), initial_runner).run(request)
    prefix = request.ledger.path.read_bytes()
    assert len(request.ledger.read_state().trials) == 1
    resumed_runner = SyntheticRunner()
    result = ResearchController(YouTubeCTRDomain(), SequencePlanner(cards, []), resumed_runner).run(request)
    assert result.conclusion is ControllerConclusion.IMPROVED
    assert resumed_runner.preparations == resumed_runner.validation_runs == []
    assert len(resumed_runner.final_runs) == 5
    assert request.ledger.path.read_bytes().startswith(prefix)
    marker_bytes = result.final_consumption.marker_path.read_bytes()
    ResearchController(YouTubeCTRDomain(), SequencePlanner(cards, []), SyntheticRunner()).run(request)
    assert len(claim_calls) == 2
    assert result.final_consumption.marker_path.read_bytes() == marker_bytes
