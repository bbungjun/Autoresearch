"""Research Harness의 validation 반복·판정·final 종료 정책을 실행한다.

[파이프라인] 사람이 준 ExperimentCard와 이미 준비된 Judge snapshot을 받아, candidate
실행 adapter와 ResearchDomain 판정을 반복한 뒤 final holdout 결론을 ledger에 남긴다.

[기능] trial/time budget, ledger-first feedback, promote-only champion 전이, typed 실패 후
자동 계속, checkpoint 재생과 유효 validation 후보가 있을 때의 final 단일 소비를 하나의
``run`` interface로 제공한다.
직전 실패 trial의 candidate만 수정 출발점으로 전달하며 비교 champion은 유지한다.

[비책임] candidate 코드 작성·workspace 생성·LocalRunner 조립·prediction 봉인과 metric
구현은 ResearchTrialRunner adapter와 ResearchDomain이 담당하며 REPORT·CLI는 후속 Task다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum, unique
from math import isfinite
from pathlib import Path
import time
from typing import Protocol

from autoresearch.research_harness.consumption_registry import (
    ConsumptionRegistryError,
    FinalConsumptionEvidence,
    FinalConsumptionGrant,
    FinalConsumptionRequest,
    claim_final_consumption,
)
from autoresearch.research_harness.domain import ResearchDomain
from autoresearch.research_harness.feedback import (
    ExperimentCard,
    FeedbackFailure,
    FeedbackMetric,
    FeedbackPayload,
    TrialFeedbackSummary,
    build_feedback,
)
from autoresearch.research_harness.fixture_models import JudgeSnapshotHandoff
from autoresearch.research_harness.judge import JudgeScoringResult
from autoresearch.research_harness.judge_decision import (
    ConfirmationDecision,
    JudgeDecision,
    JudgeMetric,
    MetricDelta,
    PairedJudgeResult,
    ScreeningResult,
)
from autoresearch.research_harness.ledger import (
    CheckpointRecord,
    LedgerArtifactEvidence,
    LedgerError,
    LedgerMetric,
    TrialLedger,
    TrialLedgerState,
    TrialRecord,
)


_SHA_LENGTH = 40
_MAX_SEED = 2**32 - 1
_CONFIRMATION_SEED_COUNT = 5
_FINAL_TRIAL_ID = "final-holdout"
_DELTA_PREFIX = "delta__"


@unique
class ControllerConclusion(StrEnum):
    """final holdout이 결정하는 사용자용 MVP 결론."""

    IMPROVED = "improved"
    NO_IMPROVEMENT = "no_improvement"
    INCONCLUSIVE = "inconclusive"


@unique
class ControllerTerminalReasonCode(StrEnum):
    """final을 시작하지 않는 Controller 정책의 안정적 종료 사유."""

    NO_VALID_VALIDATION_CANDIDATE = "no_valid_validation_candidate"


@unique
class ControllerErrorCode(StrEnum):
    """호출자가 run 재시도 여부를 판단할 수 있는 안정적 오류 코드."""

    INVALID_REQUEST = "controller_invalid_request"
    INTEGRITY_VIOLATION = "controller_integrity_violation"
    LEDGER_FAILED = "controller_ledger_failed"


@dataclass(slots=True)
class ControllerError(Exception):
    """원본 card·로그·경로를 문자열에 노출하지 않는 Controller 오류."""

    code: ControllerErrorCode
    stage: str

    def __str__(self) -> str:
        return f"{self.code.value}: stage={self.stage}"


@dataclass(frozen=True, slots=True)
class ResearchBudget:
    """새 validation trial 시작을 제한하는 실행 예산."""

    max_trials: int
    max_duration_seconds: float


@dataclass(frozen=True, slots=True)
class PrepareCandidateRequest:
    """Planner card를 현재 champion에서 구현하도록 runner에 전달하는 입력."""

    trial_id: str
    card: ExperimentCard
    champion_sha: str
    feedback_history: tuple[FeedbackPayload, ...]
    repair_candidate_sha: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedCandidate:
    """여러 seed 실행에서 동일성을 유지할 candidate 증거."""

    trial_id: str
    card: ExperimentCard
    base_sha: str
    candidate_sha: str
    diff_fingerprint: str
    artifacts: tuple[LedgerArtifactEvidence, ...]


@dataclass(frozen=True, slots=True)
class ValidationPairRequest:
    """동일 seed baseline/candidate validation 실행 요청."""

    candidate: PreparedCandidate
    handoff: JudgeSnapshotHandoff
    seed: int


@dataclass(frozen=True, slots=True)
class FinalPairRequest:
    """소비 grant가 승인한 baseline/champion final 실행 요청."""

    baseline_sha: str
    candidate_sha: str
    handoff: JudgeSnapshotHandoff
    grant: FinalConsumptionGrant = field(repr=False)
    seed: int


@dataclass(frozen=True, slots=True)
class PairedRunReceipt:
    """runner adapter가 Controller에 돌려주는 정제된 paired 결과."""

    pair: PairedJudgeResult
    duration_ms: int
    artifacts: tuple[LedgerArtifactEvidence, ...]
    stdout_tail: str = field(default="", repr=False)
    stderr_tail: str = field(default="", repr=False)


@dataclass(slots=True, repr=False)
class TrialExecutionError(Exception):
    """실패 stage와 bounded log만 보존하는 runner adapter 오류."""

    stage: str
    reason_code: str
    duration_ms: int = 0
    stdout_tail: str = field(default="", repr=False)
    stderr_tail: str = field(default="", repr=False)

    def __str__(self) -> str:
        return f"{self.reason_code}: stage={self.stage}"


class ResearchPlanner(Protocol):
    """이전 validation feedback으로 다음 card를 선택하는 seam."""

    def next_card(
        self,
        initial_card: ExperimentCard,
        feedback_history: tuple[FeedbackPayload, ...],
    ) -> ExperimentCard | None: ...


class ResearchTrialRunner(Protocol):
    """candidate/workspace/LocalRunner/Judge 실행을 감추는 adapter interface."""

    def prepare_candidate(self, request: PrepareCandidateRequest) -> PreparedCandidate: ...

    def run_validation(
        self,
        request: ValidationPairRequest,
        domain: ResearchDomain,
    ) -> PairedRunReceipt: ...

    def run_final(
        self,
        request: FinalPairRequest,
        domain: ResearchDomain,
    ) -> PairedRunReceipt: ...


@dataclass(frozen=True, slots=True)
class ControllerRunRequest:
    """한 research run의 불변 입력과 durable ledger."""

    initial_card: ExperimentCard
    budget: ResearchBudget
    baseline_sha: str
    champion_sha: str
    handoff: JudgeSnapshotHandoff
    judge_state_root: Path
    baseline_sigmas: tuple[tuple[str, float], ...]
    screening_seed: int
    confirmation_seeds: tuple[int, ...]
    ledger: TrialLedger = field(repr=False)


@dataclass(frozen=True, slots=True)
class ControllerRunResult:
    """validation feedback와 비노출 final 결론을 분리한 실행 결과."""

    conclusion: ControllerConclusion
    champion_sha: str
    validation_trials: int
    feedback_history: tuple[FeedbackPayload, ...]
    final_decision: JudgeDecision | None
    final_reason_code: str
    final_consumption: FinalConsumptionEvidence | None


def _repair_candidate_sha(record: TrialRecord | None, champion_sha: str) -> str | None:
    """Select only the immediate failed validation candidate, without promoting it."""
    if (record is not None and record.split == "validation" and record.decision == "failed"
            and record.candidate_sha is not None and record.base_sha == champion_sha):
        return record.candidate_sha
    return None


def _is_valid_validation_candidate(record: TrialRecord) -> bool:
    """공식 screening 결과가 기록된 candidate인지 판별한다."""
    return (
        record.split == "validation"
        and record.candidate_sha is not None
        and record.failure_reason_code is None
        and bool(record.metrics)
    )


class ResearchController:
    """예산·판정·복구 정책을 하나의 run interface 뒤에 숨기는 deep module."""

    def __init__(
        self,
        domain: ResearchDomain,
        planner: ResearchPlanner,
        runner: ResearchTrialRunner,
    ) -> None:
        self._domain = domain
        self._planner = planner
        self._runner = runner

    def run(self, request: ControllerRunRequest) -> ControllerRunResult:
        """Resume validation history, spend budget, then consume final once."""

        sigmas = _validate_request(request)
        try:
            state = request.ledger.read_state()
        except LedgerError:
            raise ControllerError(
                ControllerErrorCode.LEDGER_FAILED,
                "ledger_read",
            ) from None

        feedback: list[FeedbackPayload] = []
        lineage = _initial_lineage(request)
        champion_sha = request.champion_sha
        validation_records = [item for item in state.trials if item.split == "validation"]
        for record in validation_records:
            card = self._planner.next_card(request.initial_card, tuple(feedback))
            if (
                card is None
                or record.experiment_summary is None
                or card.canonical_summary() != record.experiment_summary
            ):
                raise ControllerError(
                    ControllerErrorCode.INTEGRITY_VIOLATION,
                    "planner_replay",
                )
            if record.base_sha != champion_sha:
                raise ControllerError(
                    ControllerErrorCode.INTEGRITY_VIOLATION,
                    "champion_replay",
                )
            _ensure_checkpoint(request.ledger, state, record)
            feedback.append(_feedback_from_record(request.initial_card, card, record, feedback))
            if record.decision == JudgeDecision.PROMOTE.value:
                if record.candidate_sha is None:
                    raise ControllerError(
                        ControllerErrorCode.INTEGRITY_VIOLATION,
                        "champion_replay",
                    )
                champion_sha = record.candidate_sha
                lineage = record.champion_lineage

        final_records = [item for item in state.trials if item.split == "final_holdout"]
        if final_records:
            if len(final_records) != 1:
                raise ControllerError(
                    ControllerErrorCode.INTEGRITY_VIOLATION,
                    "final_replay",
                )
            _ensure_checkpoint(request.ledger, state, final_records[0])
            return _result_from_final(final_records[0], champion_sha, feedback)

        started = time.monotonic()
        recorded_seconds = sum(item.duration_ms for item in validation_records) / 1000.0
        while len(feedback) < request.budget.max_trials and (
            recorded_seconds + time.monotonic() - started
            < request.budget.max_duration_seconds
        ):
            card = self._planner.next_card(request.initial_card, tuple(feedback))
            if card is None:
                break
            trial_id = f"trial-{len(feedback) + 1:04d}"
            record, payload = self._run_validation_trial(
                request,
                card,
                trial_id,
                champion_sha,
                lineage,
                feedback,
                sigmas,
                validation_records[-1] if validation_records else None,
            )
            _append_with_checkpoint(request.ledger, record)
            validation_records.append(record)
            feedback.append(payload)
            recorded_seconds += record.duration_ms / 1000.0
            if record.decision == JudgeDecision.PROMOTE.value:
                if record.candidate_sha is None:
                    raise ControllerError(
                        ControllerErrorCode.INTEGRITY_VIOLATION,
                        "champion_transition",
                    )
                champion_sha = record.candidate_sha
                lineage = record.champion_lineage

        if not any(_is_valid_validation_candidate(record) for record in validation_records):
            return ControllerRunResult(
                ControllerConclusion.INCONCLUSIVE,
                champion_sha,
                len(feedback),
                tuple(feedback),
                None,
                ControllerTerminalReasonCode.NO_VALID_VALIDATION_CANDIDATE.value,
                None,
            )
        return self._run_final(request, champion_sha, lineage, feedback, sigmas)

    def _run_validation_trial(
        self,
        request: ControllerRunRequest,
        card: ExperimentCard,
        trial_id: str,
        champion_sha: str,
        lineage: tuple[str, ...],
        feedback: list[FeedbackPayload],
        sigmas: Mapping[str, float],
        previous_record: TrialRecord | None,
    ) -> tuple[TrialRecord, FeedbackPayload]:
        candidate: PreparedCandidate | None = None
        receipts: list[PairedRunReceipt] = []
        try:
            candidate = self._runner.prepare_candidate(
                PrepareCandidateRequest(trial_id, card, champion_sha, tuple(feedback),
                                        _repair_candidate_sha(previous_record, champion_sha))
            )
            _validate_candidate(candidate, trial_id, card, champion_sha)
            screening_receipt = self._runner.run_validation(
                ValidationPairRequest(candidate, request.handoff, request.screening_seed),
                self._domain,
            )
            _validate_receipt(
                screening_receipt,
                request.screening_seed,
                str(request.handoff.validation_id),
            )
            receipts.append(screening_receipt)
            screening = self._domain.compare(screening_receipt.pair)
            if not isinstance(screening, ScreeningResult):
                raise ControllerError(
                    ControllerErrorCode.INTEGRITY_VIOLATION,
                    "screening_result",
                )
            decision, reason, deltas = _screening_outcome(screening)
            if screening.should_confirm:
                confirmation = []
                for seed in request.confirmation_seeds:
                    receipt = self._runner.run_validation(
                        ValidationPairRequest(candidate, request.handoff, seed),
                        self._domain,
                    )
                    _validate_receipt(
                        receipt,
                        seed,
                        str(request.handoff.validation_id),
                    )
                    receipts.append(receipt)
                    confirmation.append(receipt.pair)
                compared = self._domain.compare(
                    tuple(confirmation),
                    baseline_sigmas=sigmas,
                )
                if not isinstance(compared, ConfirmationDecision):
                    raise ControllerError(
                        ControllerErrorCode.INTEGRITY_VIOLATION,
                        "confirmation_result",
                    )
                decision = (
                    compared.decision.value if compared.decision is not None else "invalid"
                )
                reason = compared.reason_code.value
                deltas = compared.normalized_deltas
            duration = sum(item.duration_ms for item in receipts)
            metrics = _feedback_metrics(screening_receipt.pair.candidate, deltas)
            next_lineage = (
                (*lineage, candidate.candidate_sha)
                if decision == JudgeDecision.PROMOTE.value
                else lineage
            )
            record = TrialRecord(
                trial_id=trial_id,
                split="validation",
                base_sha=champion_sha,
                candidate_sha=candidate.candidate_sha,
                diff_fingerprint=candidate.diff_fingerprint,
                evaluation_id=str(request.handoff.validation_id),
                seed=request.screening_seed,
                metrics=_ledger_metrics(metrics),
                decision=decision,
                reason_code=reason,
                duration_ms=duration,
                failure_reason_code=None,
                artifacts=_artifacts(candidate, receipts),
                champion_lineage=next_lineage,
                final_consumption=None,
                experiment_summary=card.canonical_summary(),
            )
            return record, build_feedback(
                initial_card=request.initial_card,
                current_card=card,
                trial_id=trial_id,
                metrics=metrics,
                decision=decision,
                reason_code=reason,
                previous_trials=_history(feedback),
                failure=None,
            )
        except TrialExecutionError as error:
            failure = FeedbackFailure(
                error.stage,
                error.reason_code,
                error.stdout_tail,
                error.stderr_tail,
            )
            record = TrialRecord(
                trial_id=trial_id,
                split="validation",
                base_sha=champion_sha,
                candidate_sha=candidate.candidate_sha if candidate is not None else None,
                diff_fingerprint=(candidate.diff_fingerprint if candidate is not None else None),
                evaluation_id=str(request.handoff.validation_id),
                seed=request.screening_seed,
                metrics=(),
                decision="failed",
                reason_code=error.reason_code,
                duration_ms=(
                    sum(item.duration_ms for item in receipts) + error.duration_ms
                ),
                failure_reason_code=error.reason_code,
                artifacts=(
                    _artifacts(candidate, receipts)
                    if candidate is not None
                    else _receipt_artifacts(receipts)
                ),
                champion_lineage=lineage,
                final_consumption=None,
                experiment_summary=card.canonical_summary(),
                failure_stage=error.stage,
                failure_stdout_tail=error.stdout_tail,
                failure_stderr_tail=error.stderr_tail,
            )
            return record, build_feedback(
                initial_card=request.initial_card,
                current_card=card,
                trial_id=trial_id,
                metrics=(),
                decision="failed",
                reason_code=error.reason_code,
                previous_trials=_history(feedback),
                failure=failure,
            )

    def _run_final(
        self,
        request: ControllerRunRequest,
        champion_sha: str,
        lineage: tuple[str, ...],
        feedback: list[FeedbackPayload],
        sigmas: Mapping[str, float],
    ) -> ControllerRunResult:
        try:
            grant = claim_final_consumption(
                FinalConsumptionRequest(
                    judge_state_root=request.judge_state_root,
                    handoff=request.handoff,
                    baseline_sha=request.baseline_sha,
                    candidate_sha=champion_sha,
                    started_at=_utcnow(),
                )
            )
        except ConsumptionRegistryError as error:
            return ControllerRunResult(
                ControllerConclusion.INCONCLUSIVE,
                champion_sha,
                len(feedback),
                tuple(feedback),
                None,
                error.code.value,
                None,
            )

        receipts: list[PairedRunReceipt] = []
        try:
            for seed in request.confirmation_seeds:
                receipt = self._runner.run_final(
                    FinalPairRequest(
                        request.baseline_sha,
                        champion_sha,
                        request.handoff,
                        grant,
                        seed,
                    ),
                    self._domain,
                )
                _validate_receipt(
                    receipt,
                    seed,
                    str(request.handoff.final_holdout_id),
                )
                receipts.append(receipt)
            compared = self._domain.compare(
                tuple(item.pair for item in receipts),
                baseline_sigmas=sigmas,
            )
            if not isinstance(compared, ConfirmationDecision):
                raise ControllerError(
                    ControllerErrorCode.INTEGRITY_VIOLATION,
                    "final_result",
                )
            decision = compared.decision
            reason = compared.reason_code.value
            conclusion = _conclusion(decision)
            metrics = _average_ledger_metrics(receipts, compared.normalized_deltas)
            failure_reason = None
            failure_stage = None
            failure_stdout_tail = ""
            failure_stderr_tail = ""
        except TrialExecutionError as error:
            decision = None
            reason = error.reason_code
            conclusion = ControllerConclusion.INCONCLUSIVE
            metrics = ()
            failure_reason = error.reason_code
            failure_stage = error.stage
            failure_stdout_tail = error.stdout_tail
            failure_stderr_tail = error.stderr_tail
            failure_duration_ms = error.duration_ms
        else:
            failure_duration_ms = 0

        champion_diff = _champion_diff(request.ledger, champion_sha, request.baseline_sha)
        final_record = TrialRecord(
            trial_id=_FINAL_TRIAL_ID,
            split="final_holdout",
            base_sha=request.baseline_sha,
            candidate_sha=champion_sha if champion_diff is not None else None,
            diff_fingerprint=champion_diff,
            evaluation_id=str(request.handoff.final_holdout_id),
            seed=request.confirmation_seeds[0],
            metrics=metrics,
            decision=decision.value if decision is not None else "inconclusive",
            reason_code=reason,
            duration_ms=(
                sum(item.duration_ms for item in receipts) + failure_duration_ms
            ),
            failure_reason_code=failure_reason,
            artifacts=_receipt_artifacts(receipts),
            champion_lineage=lineage,
            final_consumption=grant.evidence,
            failure_stage=failure_stage,
            failure_stdout_tail=failure_stdout_tail,
            failure_stderr_tail=failure_stderr_tail,
        )
        _append_with_checkpoint(request.ledger, final_record)
        return ControllerRunResult(
            conclusion,
            champion_sha,
            len(feedback),
            tuple(feedback),
            decision,
            reason,
            grant.evidence,
        )


def _validate_request(request: ControllerRunRequest) -> dict[str, float]:
    try:
        budget = request.budget
        if (
            not isinstance(request.initial_card, ExperimentCard)
            or isinstance(budget.max_trials, bool)
            or not isinstance(budget.max_trials, int)
            or budget.max_trials <= 0
            or isinstance(budget.max_duration_seconds, bool)
            or not isfinite(budget.max_duration_seconds)
            or budget.max_duration_seconds <= 0
            or not _is_sha(request.baseline_sha)
            or not _is_sha(request.champion_sha)
            or not request.judge_state_root.is_absolute()
            or not isinstance(request.handoff, JudgeSnapshotHandoff)
            or not isinstance(request.ledger, TrialLedger)
            or isinstance(request.screening_seed, bool)
            or not isinstance(request.screening_seed, int)
            or not 0 <= request.screening_seed <= _MAX_SEED
            or len(request.confirmation_seeds) != _CONFIRMATION_SEED_COUNT
            or len(set(request.confirmation_seeds)) != _CONFIRMATION_SEED_COUNT
            or any(
                isinstance(seed, bool)
                or not isinstance(seed, int)
                or not 0 <= seed <= _MAX_SEED
                for seed in request.confirmation_seeds
            )
            or request.screening_seed in request.confirmation_seeds
        ):
            raise ValueError
        sigmas = dict(request.baseline_sigmas)
        if set(sigmas) != {metric.value for metric in JudgeMetric} or any(
            not isinstance(name, str)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value < 0
            for name, value in request.baseline_sigmas
        ):
            raise ValueError
        return {name: float(value) for name, value in sigmas.items()}
    except (AttributeError, TypeError, ValueError):
        raise ControllerError(
            ControllerErrorCode.INVALID_REQUEST,
            "request_validation",
        ) from None


def _is_sha(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _initial_lineage(request: ControllerRunRequest) -> tuple[str, ...]:
    if request.baseline_sha == request.champion_sha:
        return (request.baseline_sha,)
    return (request.baseline_sha, request.champion_sha)


def _validate_candidate(
    candidate: PreparedCandidate,
    trial_id: str,
    card: ExperimentCard,
    champion_sha: str,
) -> None:
    if (
        candidate.trial_id != trial_id
        or candidate.card != card
        or candidate.base_sha != champion_sha
        or not _is_sha(candidate.candidate_sha)
        or not candidate.diff_fingerprint.startswith("sha256:")
        or len(candidate.diff_fingerprint) != 71
        or any(
            character not in "0123456789abcdef"
            for character in candidate.diff_fingerprint.removeprefix("sha256:")
        )
    ):
        raise ControllerError(
            ControllerErrorCode.INTEGRITY_VIOLATION,
            "candidate_identity",
        )


def _validate_receipt(
    receipt: PairedRunReceipt,
    seed: int,
    evaluation_id: str,
) -> None:
    if (
        not isinstance(receipt, PairedRunReceipt)
        or receipt.pair.seed != seed
        or str(receipt.pair.baseline.evaluation_id) != evaluation_id
        or str(receipt.pair.candidate.evaluation_id) != evaluation_id
        or isinstance(receipt.duration_ms, bool)
        or not isinstance(receipt.duration_ms, int)
        or receipt.duration_ms < 0
    ):
        raise ControllerError(
            ControllerErrorCode.INTEGRITY_VIOLATION,
            "runner_receipt",
        )


def _screening_outcome(
    screening: ScreeningResult,
) -> tuple[str, str, tuple[MetricDelta, ...]]:
    if screening.should_confirm:
        return "confirmation_required", screening.reason_code.value, screening.normalized_deltas
    decision = (
        JudgeDecision.DISCARD.value
        if screening.reason_code.value == "primary_not_improved"
        else "invalid"
    )
    return decision, screening.reason_code.value, screening.normalized_deltas


def _score_values(score: JudgeScoringResult) -> dict[str, float | None]:
    grouped = score.probability.grouped_roc_auc
    return {
        JudgeMetric.NDCG_AT_10.value: score.ndcg_at_10.value,
        JudgeMetric.RECALL_AT_10.value: score.recall_at_10.value,
        JudgeMetric.NDCG_AT_24.value: score.ndcg_at_24.value,
        JudgeMetric.GROUPED_ROC_AUC.value: grouped.value if grouped is not None else None,
        JudgeMetric.PR_AUC.value: score.probability.pr_auc,
        JudgeMetric.LOG_LOSS.value: score.probability.log_loss,
        JudgeMetric.BRIER.value: score.probability.brier,
    }


def _feedback_metrics(
    score: JudgeScoringResult,
    deltas: tuple[MetricDelta, ...],
) -> tuple[FeedbackMetric, ...]:
    delta_by_name = {item.metric.value: item.value for item in deltas}
    return tuple(
        FeedbackMetric(name, value, delta_by_name.get(name))
        for name, value in _score_values(score).items()
    )


def _ledger_metrics(metrics: tuple[FeedbackMetric, ...]) -> tuple[LedgerMetric, ...]:
    values = [LedgerMetric(item.name, item.value) for item in metrics]
    values.extend(
        LedgerMetric(f"{_DELTA_PREFIX}{item.name}", item.normalized_delta)
        for item in metrics
        if item.normalized_delta is not None
    )
    return tuple(values)


def _artifacts(
    candidate: PreparedCandidate,
    receipts: list[PairedRunReceipt],
) -> tuple[LedgerArtifactEvidence, ...]:
    return (*candidate.artifacts, *_receipt_artifacts(receipts))


def _receipt_artifacts(
    receipts: list[PairedRunReceipt],
) -> tuple[LedgerArtifactEvidence, ...]:
    return tuple(artifact for receipt in receipts for artifact in receipt.artifacts)


def _history(feedback: list[FeedbackPayload]) -> tuple[TrialFeedbackSummary, ...]:
    return tuple(
        TrialFeedbackSummary(
            item.trial_id,
            item.current_experiment_summary,
            item.decision,
            item.reason_code,
            item.failure,
        )
        for item in feedback
    )


def _feedback_from_record(
    initial_card: ExperimentCard,
    card: ExperimentCard,
    record: TrialRecord,
    previous: list[FeedbackPayload],
) -> FeedbackPayload:
    metrics = {item.name: item.value for item in record.metrics}
    feedback_metrics = tuple(
        FeedbackMetric(
            name,
            value,
            metrics.get(f"{_DELTA_PREFIX}{name}"),
        )
        for name, value in metrics.items()
        if not name.startswith(_DELTA_PREFIX)
    )
    failure = (
        FeedbackFailure(
            record.failure_stage or "unknown",
            record.failure_reason_code,
            record.failure_stdout_tail,
            record.failure_stderr_tail,
        )
        if record.failure_reason_code is not None
        else None
    )
    return build_feedback(
        initial_card=initial_card,
        current_card=card,
        trial_id=record.trial_id,
        metrics=feedback_metrics,
        decision=record.decision,
        reason_code=record.reason_code,
        previous_trials=_history(previous),
        failure=failure,
    )


def _ensure_checkpoint(
    ledger: TrialLedger,
    state: TrialLedgerState,
    record: TrialRecord,
) -> None:
    checkpoint_id = _checkpoint_id(record)
    if not state.completed(checkpoint_id):
        try:
            _append_checkpoint(ledger, record)
        except LedgerError:
            raise ControllerError(
                ControllerErrorCode.LEDGER_FAILED,
                "checkpoint_recovery",
            ) from None


def _append_with_checkpoint(ledger: TrialLedger, record: TrialRecord) -> None:
    try:
        ledger.append(record)
        _append_checkpoint(ledger, record)
    except LedgerError:
        raise ControllerError(
            ControllerErrorCode.LEDGER_FAILED,
            "ledger_append",
        ) from None


def _append_checkpoint(ledger: TrialLedger, record: TrialRecord) -> None:
    ledger.append(
        CheckpointRecord(
            checkpoint_id=_checkpoint_id(record),
            stage=(
                "validation_recorded"
                if record.split == "validation"
                else "final_recorded"
            ),
            trial_id=record.trial_id,
            completed_at=_utcnow(),
            artifacts=record.artifacts,
            final_consumption=record.final_consumption,
        )
    )


def _checkpoint_id(record: TrialRecord) -> str:
    suffix = "validation-recorded" if record.split == "validation" else "final-recorded"
    return f"{record.trial_id}:{suffix}"


def _champion_diff(
    ledger: TrialLedger,
    champion_sha: str,
    baseline_sha: str,
) -> str | None:
    if champion_sha == baseline_sha:
        return None
    try:
        for record in reversed(ledger.read_state().trials):
            if record.candidate_sha == champion_sha and record.diff_fingerprint is not None:
                return record.diff_fingerprint
    except LedgerError:
        raise ControllerError(ControllerErrorCode.LEDGER_FAILED, "ledger_read") from None
    raise ControllerError(
        ControllerErrorCode.INTEGRITY_VIOLATION,
        "champion_evidence",
    )


def _average_ledger_metrics(
    receipts: list[PairedRunReceipt],
    deltas: tuple[MetricDelta, ...],
) -> tuple[LedgerMetric, ...]:
    names = tuple(_score_values(receipts[0].pair.candidate))
    metrics = tuple(
        FeedbackMetric(
            name,
            sum(float(_score_values(item.pair.candidate)[name]) for item in receipts)
            / len(receipts),
            next((delta.value for delta in deltas if delta.metric.value == name), None),
        )
        for name in names
    )
    return _ledger_metrics(metrics)


def _conclusion(decision: JudgeDecision | None) -> ControllerConclusion:
    if decision is JudgeDecision.PROMOTE:
        return ControllerConclusion.IMPROVED
    if decision in {JudgeDecision.REVISE, JudgeDecision.DISCARD}:
        return ControllerConclusion.NO_IMPROVEMENT
    return ControllerConclusion.INCONCLUSIVE


def _result_from_final(
    record: TrialRecord,
    champion_sha: str,
    feedback: list[FeedbackPayload],
) -> ControllerRunResult:
    if record.candidate_sha is not None and record.candidate_sha != champion_sha:
        raise ControllerError(
            ControllerErrorCode.INTEGRITY_VIOLATION,
            "final_candidate_replay",
        )
    try:
        decision = (
            JudgeDecision(record.decision)
            if record.decision != "inconclusive"
            else None
        )
    except ValueError:
        raise ControllerError(
            ControllerErrorCode.INTEGRITY_VIOLATION,
            "final_decision_replay",
        ) from None
    return ControllerRunResult(
        _conclusion(decision),
        champion_sha,
        len(feedback),
        tuple(feedback),
        decision,
        record.reason_code,
        record.final_consumption,
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)
