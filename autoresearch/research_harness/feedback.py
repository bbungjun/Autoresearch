"""Research Agent에 돌려줄 validation 전용 구조화 피드백을 만든다.

[파이프라인] Controller가 validation trial을 durable ledger에 기록한 뒤 다음 연구
iteration을 계획하기 직전의 memory 조립 구간을 담당한다.

[기능] 사람이 작성한 ExperimentCard의 canonical 표현과 지표·판정·실패·과거 시도를
담은 불변 FeedbackPayload를 제공한다.

[비책임] 행 단위 정답, Judge 구현·경로, final holdout 결과, candidate 실행과 판정 계산은
노출하거나 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re


_CARD_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MAX_TEXT_LENGTH = 4096


@dataclass(frozen=True, slots=True)
class ExperimentCard:
    """사람 또는 후속 planner가 제안하는 검증 가능한 실험 한 건."""

    card_id: str
    hypothesis: str
    change: str
    falsification_condition: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.card_id, str)
            or _CARD_ID_PATTERN.fullmatch(self.card_id) is None
        ):
            raise ValueError("invalid experiment card")
        for value in (self.hypothesis, self.change, self.falsification_condition):
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or len(value) > _MAX_TEXT_LENGTH
                or "\0" in value
            ):
                raise ValueError("invalid experiment card")

    def canonical_summary(self) -> str:
        """Return a stable ledger representation without execution details."""

        return json.dumps(
            {
                "card_id": self.card_id,
                "change": self.change,
                "falsification_condition": self.falsification_condition,
                "hypothesis": self.hypothesis,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_summary(cls, summary: str) -> ExperimentCard:
        """Parse only the exact canonical ExperimentCard representation."""

        try:
            payload = json.loads(summary)
            if not isinstance(payload, dict) or set(payload) != {
                "card_id",
                "hypothesis",
                "change",
                "falsification_condition",
            }:
                raise ValueError
            card = cls(**payload)
            if card.canonical_summary() != summary:
                raise ValueError
            return card
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("invalid experiment card summary") from None


@dataclass(frozen=True, slots=True)
class FeedbackMetric:
    """Candidate validation metric과 개선 방향 정규화 delta."""

    name: str
    value: float | None
    normalized_delta: float | None


@dataclass(frozen=True, slots=True)
class FeedbackFailure:
    """Agent가 다음 수정을 선택할 수 있는 bounded 실행 실패 근거."""

    stage: str
    reason_code: str
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass(frozen=True, slots=True)
class TrialFeedbackSummary:
    """이전 trial 한 건의 재시작 가능한 최소 memory."""

    trial_id: str
    experiment_summary: str
    decision: str
    reason_code: str
    failure: FeedbackFailure | None


@dataclass(frozen=True, slots=True)
class FeedbackPayload:
    """Planner에 전달하는 validation-only 불변 payload."""

    initial_experiment_summary: str
    current_experiment_summary: str
    trial_id: str
    metrics: tuple[FeedbackMetric, ...]
    decision: str
    reason_code: str
    previous_trials: tuple[TrialFeedbackSummary, ...]
    failure: FeedbackFailure | None


def build_feedback(
    *,
    initial_card: ExperimentCard,
    current_card: ExperimentCard,
    trial_id: str,
    metrics: tuple[FeedbackMetric, ...],
    decision: str,
    reason_code: str,
    previous_trials: tuple[TrialFeedbackSummary, ...],
    failure: FeedbackFailure | None,
) -> FeedbackPayload:
    """Build a payload whose type has no final or row-level label fields."""

    return FeedbackPayload(
        initial_experiment_summary=initial_card.canonical_summary(),
        current_experiment_summary=current_card.canonical_summary(),
        trial_id=trial_id,
        metrics=metrics,
        decision=decision,
        reason_code=reason_code,
        previous_trials=previous_trials,
        failure=failure,
    )
