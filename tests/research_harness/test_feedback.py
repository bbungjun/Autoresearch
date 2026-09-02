"""Task 5b validation feedback의 비노출·이력 계약 테스트."""

from __future__ import annotations

from dataclasses import asdict

from autoresearch.research_harness.feedback import (
    ExperimentCard,
    FeedbackFailure,
    FeedbackMetric,
    TrialFeedbackSummary,
    build_feedback,
)


def _card(card_id: str) -> ExperimentCard:
    return ExperimentCard(
        card_id=card_id,
        hypothesis="새 피처가 개인화 순위를 개선한다",
        change="candidate feature를 추가한다",
        falsification_condition="NDCG@10이 개선되지 않는다",
    )


def test_feedback_contains_only_structured_validation_evidence() -> None:
    previous = TrialFeedbackSummary(
        trial_id="trial-0001",
        experiment_summary=_card("card-1").canonical_summary(),
        decision="discard",
        reason_code="primary_not_improved",
        failure=None,
    )

    payload = build_feedback(
        initial_card=_card("initial"),
        current_card=_card("card-2"),
        trial_id="trial-0002",
        metrics=(FeedbackMetric("ndcg_at_10", 0.72, 0.03),),
        decision="revise",
        reason_code="guardrail_regression",
        previous_trials=(previous,),
        failure=FeedbackFailure(
            stage="candidate_run",
            reason_code="predict_crash",
            stdout_tail="bounded stdout",
            stderr_tail="bounded stderr",
        ),
    )

    serialized = repr(asdict(payload)).lower()
    assert payload.previous_trials == (previous,)
    assert payload.metrics[0].normalized_delta == 0.03
    assert "label" not in serialized
    assert "final" not in serialized
    assert "judge_state_root" not in serialized
    assert "predictions.csv" not in serialized


def test_experiment_card_summary_is_canonical_and_round_trips() -> None:
    card = _card("card-1")

    assert ExperimentCard.from_summary(card.canonical_summary()) == card

