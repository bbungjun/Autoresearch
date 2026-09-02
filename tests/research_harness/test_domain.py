"""P0-2D ResearchDomain interface와 YouTubeCTRDomain 위임 계약 테스트."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Never, cast, get_type_hints

import pytest

import autoresearch.research_harness as research_harness
import autoresearch.research_harness.domain as domain_module
from autoresearch.research_harness.domain import (
    DomainError,
    DomainErrorCode,
    ResearchDomain,
    YouTubeCTRDomain,
)
from autoresearch.research_harness.judge_decision import (
    JudgeReasonCode,
    PairedJudgeResult,
)


def test_research_domain_requires_exact_five_methods() -> None:
    assert ResearchDomain.__abstractmethods__ == {
        "describe_capabilities",
        "build_evaluation_snapshot",
        "validate_candidate",
        "evaluate",
        "compare",
    }

    with pytest.raises(TypeError):
        ResearchDomain()


def test_youtube_domain_exports_public_types() -> None:
    assert research_harness.ResearchDomain is ResearchDomain
    assert research_harness.YouTubeCTRDomain is YouTubeCTRDomain
    assert research_harness.DomainError is DomainError
    assert research_harness.DomainErrorCode is DomainErrorCode


def test_youtube_domain_rejects_unimplemented_capability_description() -> None:
    with pytest.raises(DomainError) as error:
        YouTubeCTRDomain().describe_capabilities()

    assert error.value.code is DomainErrorCode.CAPABILITIES_UNAVAILABLE
    assert str(error.value) == "domain_capabilities_unavailable"


def test_capability_description_is_typed_as_never() -> None:
    assert get_type_hints(ResearchDomain.describe_capabilities)["return"] is Never
    assert get_type_hints(YouTubeCTRDomain.describe_capabilities)["return"] is Never


def test_youtube_domain_delegates_snapshot_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = cast(Any, object())
    source = cast(Any, object())
    expected = cast(Any, object())
    calls: list[tuple[object, object]] = []

    def fake_build(value: object, *, source: object) -> object:
        calls.append((value, source))
        return expected

    monkeypatch.setattr(domain_module, "build_evaluation_snapshot", fake_build)

    result = YouTubeCTRDomain().build_evaluation_snapshot(request, source=source)

    assert result is expected
    assert calls == [(request, source)]


def test_youtube_domain_delegates_candidate_sealing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = Path("candidate/predictions.csv")
    judge_copy = Path("judge/predictions.csv")
    expected = cast(Any, object())
    calls: list[tuple[Path, Path]] = []

    def fake_seal(source: Path, destination: Path) -> object:
        calls.append((source, destination))
        return expected

    monkeypatch.setattr(domain_module, "seal_prediction_copy", fake_seal)

    result = YouTubeCTRDomain().validate_candidate(candidate, judge_copy)

    assert result is expected
    assert calls == [(candidate, judge_copy)]


def test_youtube_domain_hides_target_construction_during_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff = cast(Any, object())
    sealed = cast(Any, object())
    target = cast(Any, object())
    expected = cast(Any, object())
    calls: list[tuple[str, object, object | None]] = []

    def fake_target(value: object) -> object:
        calls.append(("target", value, None))
        return target

    def fake_score(value: object, prediction: object) -> object:
        calls.append(("score", value, prediction))
        return expected

    monkeypatch.setattr(domain_module, "build_validation_target", fake_target)
    monkeypatch.setattr(domain_module, "score_predictions", fake_score)

    result = YouTubeCTRDomain().evaluate(handoff, sealed)

    assert result is expected
    assert calls == [
        ("target", handoff, None),
        ("score", target, sealed),
    ]


def test_youtube_domain_delegates_confirmation_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pairs = tuple(object.__new__(PairedJudgeResult) for _ in range(5))
    sigmas = {"ndcg_at_10": 0.01}
    expected = cast(Any, object())
    calls: list[tuple[object, object]] = []

    def fake_compare(
        values: object,
        *,
        baseline_sigmas: object,
    ) -> object:
        calls.append((values, baseline_sigmas))
        return expected

    monkeypatch.setattr(domain_module, "compare_confirmation", fake_compare)

    result = YouTubeCTRDomain().compare(pairs, baseline_sigmas=sigmas)

    assert result is expected
    assert calls == [(pairs, sigmas)]


def test_youtube_domain_delegates_single_pair_screening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = object.__new__(PairedJudgeResult)
    expected = cast(Any, object())
    calls: list[PairedJudgeResult] = []

    def fake_screen(value: PairedJudgeResult) -> object:
        calls.append(value)
        return expected

    monkeypatch.setattr(domain_module, "screen_candidate", fake_screen)

    result = YouTubeCTRDomain().compare(pair)

    assert result is expected
    assert calls == [pair]


def test_youtube_domain_confirmation_without_sigmas_fails_closed() -> None:
    result = YouTubeCTRDomain().compare(())

    assert result.decision is None
    assert result.reason_code is JudgeReasonCode.INVALID_COMPARISON_INPUT


def test_youtube_domain_rejects_sigmas_for_single_screening_pair() -> None:
    pair = object.__new__(PairedJudgeResult)

    result = YouTubeCTRDomain().compare(  # type: ignore[call-overload]
        pair,
        baseline_sigmas={"ndcg_at_10": 0.01},
    )

    assert result.decision is None
    assert result.reason_code is JudgeReasonCode.INVALID_COMPARISON_INPUT


@pytest.mark.parametrize("malformed", ["abcde", [object()] * 5])
def test_youtube_domain_rejects_malformed_confirmation_sequence(
    malformed: object,
) -> None:
    result = YouTubeCTRDomain().compare(  # type: ignore[call-overload]
        malformed,
        baseline_sigmas={"ndcg_at_10": 0.01},
    )

    assert result.decision is None
    assert result.reason_code is JudgeReasonCode.INVALID_COMPARISON_INPUT
