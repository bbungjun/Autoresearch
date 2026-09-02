"""Research Controller가 사용하는 도메인별 평가 seam을 제공한다.

[파이프라인] 평가 snapshot 준비와 candidate prediction 산출 사이부터 Sealed Judge의 최종
confirmation 판정까지, 후속 Controller가 호출할 도메인 interface 구간을 담당한다.

[기능] 다섯 동작으로 제한된 ``ResearchDomain`` ABC와 기존 YouTube CTR snapshot·봉인·
scoring·판정 module에 위임하는 ``YouTubeCTRDomain`` adapter를 제공한다.

[비책임] candidate workspace·실행, screening fidelity 선택, baseline sigma 측정, final holdout
소비 registry와 Paper Discovery capability 모델은 후속 Task가 담당한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum, unique
from pathlib import Path
from typing import Never, overload

from autoresearch.research_harness.evaluation_snapshot_models import (
    EvaluationSnapshotReceipt,
    EvaluationSnapshotRequest,
)
from autoresearch.research_harness.evaluation_source import ActionLogSource
from autoresearch.research_harness.fixture_models import JudgeSnapshotHandoff
from autoresearch.research_harness.judge import (
    JudgeScoringResult,
    build_validation_target,
    score_predictions,
)
from autoresearch.research_harness.judge_decision import (
    ConfirmationDecision,
    JudgeReasonCode,
    PairedJudgeResult,
    ScreeningResult,
    compare_confirmation,
    screen_candidate,
)
from autoresearch.research_harness.prediction_ingestion import (
    SealedPredictionReceipt,
    seal_prediction_copy,
)
from autoresearch.research_harness.slate import build_evaluation_snapshot


@unique
class DomainErrorCode(StrEnum):
    """Controller가 안정적으로 분기할 수 있는 domain 오류 코드."""

    CAPABILITIES_UNAVAILABLE = "domain_capabilities_unavailable"


@dataclass(frozen=True, slots=True)
class DomainError(Exception):
    """아직 지원하지 않는 domain 동작의 정제된 오류."""

    code: DomainErrorCode

    def __str__(self) -> str:
        return self.code.value


class ResearchDomain(ABC):
    """Controller에서 도메인 구현을 교체하는 다섯 동작 interface."""

    @abstractmethod
    def describe_capabilities(self) -> Never:
        """Paper Discovery가 사용할 domain capability를 반환한다."""

        raise NotImplementedError

    @abstractmethod
    def build_evaluation_snapshot(
        self,
        request: EvaluationSnapshotRequest,
        *,
        source: ActionLogSource | None = None,
    ) -> EvaluationSnapshotReceipt:
        """평가 snapshot을 조립해 immutable receipt를 반환한다."""

        raise NotImplementedError

    @abstractmethod
    def validate_candidate(
        self,
        candidate_prediction: Path,
        judge_copy: Path,
    ) -> SealedPredictionReceipt:
        """candidate prediction을 Judge 소유 receipt로 봉인한다."""

        raise NotImplementedError

    @abstractmethod
    def evaluate(
        self,
        handoff: JudgeSnapshotHandoff,
        sealed_prediction: SealedPredictionReceipt,
    ) -> JudgeScoringResult:
        """validation handoff와 봉인 prediction을 결합해 지표를 계산한다."""

        raise NotImplementedError

    @overload
    def compare(
        self,
        results: PairedJudgeResult,
        *,
        baseline_sigmas: None = None,
    ) -> ScreeningResult: ...

    @overload
    def compare(
        self,
        results: Sequence[PairedJudgeResult],
        *,
        baseline_sigmas: Mapping[str, float],
    ) -> ConfirmationDecision: ...

    @abstractmethod
    def compare(
        self,
        results: PairedJudgeResult | Sequence[PairedJudgeResult],
        *,
        baseline_sigmas: Mapping[str, float] | None = None,
    ) -> ScreeningResult | ConfirmationDecision:
        """단일 screening 또는 paired confirmation 결과를 판정한다."""

        raise NotImplementedError


class YouTubeCTRDomain(ResearchDomain):
    """기존 YouTube CTR 평가 module을 조립하는 stateless adapter."""

    def describe_capabilities(self) -> Never:
        raise DomainError(DomainErrorCode.CAPABILITIES_UNAVAILABLE)

    def build_evaluation_snapshot(
        self,
        request: EvaluationSnapshotRequest,
        *,
        source: ActionLogSource | None = None,
    ) -> EvaluationSnapshotReceipt:
        return build_evaluation_snapshot(request, source=source)

    def validate_candidate(
        self,
        candidate_prediction: Path,
        judge_copy: Path,
    ) -> SealedPredictionReceipt:
        return seal_prediction_copy(candidate_prediction, judge_copy)

    def evaluate(
        self,
        handoff: JudgeSnapshotHandoff,
        sealed_prediction: SealedPredictionReceipt,
    ) -> JudgeScoringResult:
        target = build_validation_target(handoff)
        return score_predictions(target, sealed_prediction)

    @overload
    def compare(
        self,
        results: PairedJudgeResult,
        *,
        baseline_sigmas: None = None,
    ) -> ScreeningResult: ...

    @overload
    def compare(
        self,
        results: Sequence[PairedJudgeResult],
        *,
        baseline_sigmas: Mapping[str, float],
    ) -> ConfirmationDecision: ...

    def compare(
        self,
        results: PairedJudgeResult | Sequence[PairedJudgeResult],
        *,
        baseline_sigmas: Mapping[str, float] | None = None,
    ) -> ScreeningResult | ConfirmationDecision:
        if isinstance(results, PairedJudgeResult):
            if baseline_sigmas is not None:
                return _invalid_confirmation()
            return screen_candidate(results)
        if (
            isinstance(results, (str, bytes))
            or not isinstance(results, Sequence)
            or any(not isinstance(pair, PairedJudgeResult) for pair in results)
            or baseline_sigmas is None
        ):
            return _invalid_confirmation()
        return compare_confirmation(
            results,
            baseline_sigmas=baseline_sigmas,
        )


def _invalid_confirmation() -> ConfirmationDecision:
    return ConfirmationDecision(None, JudgeReasonCode.INVALID_COMPARISON_INPUT)
