"""Task 5b ResearchController 예산·피드백·재개·final 계약 테스트."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Never, cast

import pytest

import autoresearch.research_harness.controller as controller_module
from autoresearch.model_evaluation.probability_metrics import (
    GroupedRocAuc,
    ProbabilityMetricResult,
)
from autoresearch.research_harness import (
    ConfirmationDecision,
    ConsumptionRegistryError,
    ConsumptionRegistryErrorCode,
    FinalConsumptionEvidence,
    FinalConsumptionGrant,
    JudgeDecision,
    JudgeMetric,
    JudgeReasonCode,
    JudgeScoringResult,
    JudgeSnapshotHandoff,
    MetricDelta,
    PairedJudgeResult,
    RankingMetricResult,
    ResearchDomain,
    ScreeningResult,
    open_trial_ledger,
)
from autoresearch.research_harness.evaluation_snapshot_models import (
    EvaluationId,
    SnapshotFingerprint,
)
from autoresearch.research_harness.controller import (
    ControllerConclusion,
    ControllerRunRequest,
    ExperimentCard,
    FinalPairRequest,
    PairedRunReceipt,
    PrepareCandidateRequest,
    PreparedCandidate,
    ResearchBudget,
    ResearchController,
    TrialExecutionError,
    ValidationPairRequest,
)


_BASE_SHA = "a" * 40
_CANDIDATE_ONE = "b" * 40
_CANDIDATE_TWO = "c" * 40
_DIFF = "sha256:" + "d" * 64
_EVALUATION_ID = EvaluationId("eval_" + "e" * 64)


def _card(card_id: str) -> ExperimentCard:
    return ExperimentCard(
        card_id=card_id,
        hypothesis=f"{card_id} hypothesis",
        change=f"{card_id} change",
        falsification_condition=f"{card_id} falsification",
    )


def _score(value: float) -> JudgeScoringResult:
    ranking = RankingMetricResult(value, 40, 40, 0, 1.0)
    return JudgeScoringResult(
        evaluation_id=_EVALUATION_ID,
        row_count=80,
        ndcg_at_10=ranking,
        recall_at_10=ranking,
        ndcg_at_24=ranking,
        probability=ProbabilityMetricResult(
            row_count=80,
            positive_count=40,
            negative_count=40,
            roc_auc=value,
            pr_auc=value,
            log_loss=1.0 - value,
            brier=1.0 - value,
            grouped_roc_auc=GroupedRocAuc(value, 40, 40, 0, 0),
        ),
    )


def _score_for(value: float, evaluation_id: EvaluationId) -> JudgeScoringResult:
    score = _score(value)
    return JudgeScoringResult(
        evaluation_id=evaluation_id,
        row_count=score.row_count,
        ndcg_at_10=score.ndcg_at_10,
        recall_at_10=score.recall_at_10,
        ndcg_at_24=score.ndcg_at_24,
        probability=score.probability,
    )


class FakeDomain(ResearchDomain):
    def describe_capabilities(self) -> Never:
        raise AssertionError("not used")

    def build_evaluation_snapshot(self, request: object, *, source: object = None) -> Never:
        del request, source
        raise AssertionError("not used")

    def validate_candidate(self, candidate_prediction: Path, judge_copy: Path) -> Never:
        del candidate_prediction, judge_copy
        raise AssertionError("runner adapter owns this call")

    def evaluate(self, handoff: object, sealed_prediction: object, *, final_grant: object = None) -> Never:
        del handoff, sealed_prediction, final_grant
        raise AssertionError("runner adapter owns this call")

    def compare(
        self,
        results: PairedJudgeResult | tuple[PairedJudgeResult, ...],
        *,
        baseline_sigmas: object = None,
    ) -> ScreeningResult | ConfirmationDecision:
        del baseline_sigmas
        if isinstance(results, PairedJudgeResult):
            delta = results.candidate.ndcg_at_10.value - results.baseline.ndcg_at_10.value
            assert delta is not None
            if delta > 0:
                return ScreeningResult(
                    True,
                    JudgeReasonCode.CONFIRMATION_REQUIRED,
                    (MetricDelta(JudgeMetric.NDCG_AT_10, delta),),
                )
            return ScreeningResult(
                False,
                JudgeReasonCode.PRIMARY_NOT_IMPROVED,
                (MetricDelta(JudgeMetric.NDCG_AT_10, delta),),
            )
        return ConfirmationDecision(
            JudgeDecision.PROMOTE,
            JudgeReasonCode.PROMOTION_THRESHOLD_MET,
            (MetricDelta(JudgeMetric.NDCG_AT_10, 0.2),),
        )


@dataclass
class SequencePlanner:
    cards: tuple[ExperimentCard, ...]
    calls: list[tuple[object, ...]]

    def next_card(
        self,
        initial_card: ExperimentCard,
        feedback_history: tuple[object, ...],
    ) -> ExperimentCard | None:
        del initial_card
        self.calls.append(feedback_history)
        index = len(feedback_history)
        return self.cards[index] if index < len(self.cards) else None


class FakeRunner:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.preparations: list[PrepareCandidateRequest] = []
        self.validation_runs: list[ValidationPairRequest] = []
        self.final_runs: list[FinalPairRequest] = []

    def prepare_candidate(self, request: PrepareCandidateRequest) -> PreparedCandidate:
        self.preparations.append(request)
        if self.fail_first and len(self.preparations) == 1:
            raise TrialExecutionError(
                "agent_edit",
                "candidate_generation_failed",
                stderr_tail="compiler error",
            )
        candidate_sha = _CANDIDATE_ONE if request.card.card_id == "card-1" else _CANDIDATE_TWO
        return PreparedCandidate(
            trial_id=request.trial_id,
            card=request.card,
            base_sha=request.champion_sha,
            candidate_sha=candidate_sha,
            diff_fingerprint=_DIFF,
            artifacts=(),
        )

    def run_validation(
        self,
        request: ValidationPairRequest,
        domain: ResearchDomain,
    ) -> PairedRunReceipt:
        del domain
        self.validation_runs.append(request)
        candidate_value = 0.4 if request.candidate.card.card_id == "card-1" else 0.7
        return PairedRunReceipt(
            pair=PairedJudgeResult(request.seed, _score(0.5), _score(candidate_value)),
            duration_ms=10,
            artifacts=(),
        )

    def run_final(
        self,
        request: FinalPairRequest,
        domain: ResearchDomain,
    ) -> PairedRunReceipt:
        del domain
        self.final_runs.append(request)
        evaluation_id = request.handoff.final_holdout_id
        return PairedRunReceipt(
            pair=PairedJudgeResult(
                request.seed,
                _score_for(0.5, evaluation_id),
                _score_for(0.7, evaluation_id),
            ),
            duration_ms=20,
            artifacts=(),
        )


class ConfirmationFailureRunner(FakeRunner):
    def run_validation(
        self,
        request: ValidationPairRequest,
        domain: ResearchDomain,
    ) -> PairedRunReceipt:
        if self.validation_runs:
            self.validation_runs.append(request)
            raise TrialExecutionError(
                "confirmation_run",
                "predict_timeout",
                duration_ms=7,
            )
        return super().run_validation(request, domain)


def _handoff(tmp_path: Path) -> JudgeSnapshotHandoff:
    return JudgeSnapshotHandoff(
        snapshot_fingerprint=SnapshotFingerprint("f" * 64),
        snapshot_root=(tmp_path / "judge" / "snapshots" / ("f" * 64)).resolve(),
        manifest_sha256="1" * 64,
        validation_id=_EVALUATION_ID,
        final_holdout_id=EvaluationId("eval_" + "2" * 64),
    )


def _request(tmp_path: Path, *, max_trials: int = 2) -> ControllerRunRequest:
    run_root = tmp_path / "run"
    run_root.mkdir(exist_ok=True)
    state_root = tmp_path / "judge"
    state_root.mkdir(exist_ok=True)
    return ControllerRunRequest(
        initial_card=_card("initial"),
        budget=ResearchBudget(max_trials=max_trials, max_duration_seconds=60.0),
        baseline_sha=_BASE_SHA,
        champion_sha=_BASE_SHA,
        handoff=_handoff(tmp_path),
        judge_state_root=state_root.resolve(),
        baseline_sigmas=tuple((metric.value, 0.01) for metric in JudgeMetric),
        screening_seed=7,
        confirmation_seeds=(11, 12, 13, 14, 15),
        ledger=open_trial_ledger((run_root / "experiment-ledger.jsonl").resolve()),
    )


def _install_final_claim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    evidence = FinalConsumptionEvidence(
        marker_path=(tmp_path / "judge" / "final-holdout-consumed" / ("eval_" + "2" * 64)).resolve(),
        marker_sha256="3" * 64,
    )

    class FakeGrant:
        @property
        def evidence(self) -> FinalConsumptionEvidence:
            return evidence

    monkeypatch.setattr(
        controller_module,
        "claim_final_consumption",
        lambda request, **kwargs: cast(FinalConsumptionGrant, FakeGrant()),
    )
    monkeypatch.setattr(
        controller_module,
        "_utcnow",
        lambda: datetime(2026, 9, 2, tzinfo=UTC),
    )


def test_controller_runs_feedback_loop_promotes_and_hides_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_final_claim(monkeypatch, tmp_path)
    planner = SequencePlanner((_card("card-1"), _card("card-2")), [])
    runner = FakeRunner()
    request = _request(tmp_path)

    result = ResearchController(FakeDomain(), planner, runner).run(request)

    assert result.conclusion is ControllerConclusion.IMPROVED
    assert result.champion_sha == _CANDIDATE_TWO
    assert len(result.feedback_history) == 2
    assert result.feedback_history[0].decision == "discard"
    assert result.feedback_history[1].decision == "promote"
    assert [item.champion_sha for item in runner.preparations] == [_BASE_SHA, _BASE_SHA]
    assert len(runner.validation_runs) == 7
    assert len(runner.final_runs) == 5
    assert {item.candidate_sha for item in runner.final_runs} == {_CANDIDATE_TWO}
    assert all("final" not in repr(item).lower() for item in result.feedback_history)
    state = request.ledger.read_state()
    assert [trial.split for trial in state.trials] == [
        "validation",
        "validation",
        "final_holdout",
    ]
    assert len(state.checkpoints) == 3


def test_controller_records_failure_feedback_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_final_claim(monkeypatch, tmp_path)
    planner = SequencePlanner((_card("card-1"), _card("card-2")), [])
    runner = FakeRunner(fail_first=True)

    result = ResearchController(FakeDomain(), planner, runner).run(_request(tmp_path))

    assert len(result.feedback_history) == 2
    failure = result.feedback_history[0].failure
    assert failure is not None
    assert failure.stage == "agent_edit"
    assert failure.reason_code == "candidate_generation_failed"
    assert failure.stderr_tail == "compiler error"
    assert runner.preparations[1].feedback_history[0].failure == failure


def test_controller_preserves_completed_work_when_confirmation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_final_claim(monkeypatch, tmp_path)
    planner = SequencePlanner((_card("card-2"),), [])
    runner = ConfirmationFailureRunner()
    request = _request(tmp_path, max_trials=1)

    result = ResearchController(FakeDomain(), planner, runner).run(request)

    assert result.feedback_history[0].decision == "failed"
    assert result.feedback_history[0].failure is not None
    assert result.feedback_history[0].failure.stage == "confirmation_run"
    validation_record = request.ledger.read_state().trials[0]
    assert validation_record.duration_ms == 17


def test_controller_respects_trial_budget_but_still_runs_terminal_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_final_claim(monkeypatch, tmp_path)
    planner = SequencePlanner((_card("card-1"), _card("card-2")), [])
    runner = FakeRunner()

    result = ResearchController(FakeDomain(), planner, runner).run(
        _request(tmp_path, max_trials=1)
    )

    assert len(result.feedback_history) == 1
    assert len(runner.preparations) == 1
    assert len(runner.final_runs) == 5


def test_controller_resume_replays_feedback_without_repeating_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_final_claim(monkeypatch, tmp_path)
    request = _request(tmp_path, max_trials=1)
    first_planner = SequencePlanner((_card("card-1"),), [])
    first_runner = FakeRunner()
    expected = ResearchController(FakeDomain(), first_planner, first_runner).run(request)
    resumed_planner = SequencePlanner((_card("card-1"),), [])
    resumed_runner = FakeRunner()

    resumed = ResearchController(FakeDomain(), resumed_planner, resumed_runner).run(request)

    assert resumed == expected
    assert resumed_runner.preparations == []
    assert resumed_runner.validation_runs == []
    assert resumed_runner.final_runs == []


def test_controller_time_budget_stops_validation_but_keeps_terminal_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_final_claim(monkeypatch, tmp_path)
    ticks = iter((0.0, 61.0))
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: next(ticks))
    planner = SequencePlanner((_card("card-1"),), [])
    runner = FakeRunner()

    result = ResearchController(FakeDomain(), planner, runner).run(_request(tmp_path))

    assert result.validation_trials == 0
    assert planner.calls == []
    assert runner.preparations == []
    assert len(runner.final_runs) == 5


def test_controller_does_not_run_final_when_registry_rejects_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_claim(*args: object, **kwargs: object) -> Never:
        del args, kwargs
        raise ConsumptionRegistryError(
            ConsumptionRegistryErrorCode.ALREADY_CONSUMED,
            "marker_exists",
        )

    monkeypatch.setattr(controller_module, "claim_final_consumption", reject_claim)
    planner = SequencePlanner((_card("card-1"),), [])
    runner = FakeRunner()

    result = ResearchController(FakeDomain(), planner, runner).run(
        _request(tmp_path, max_trials=1)
    )

    assert result.conclusion is ControllerConclusion.INCONCLUSIVE
    assert result.final_reason_code == "already_consumed"
    assert result.final_consumption is None
    assert runner.final_runs == []
