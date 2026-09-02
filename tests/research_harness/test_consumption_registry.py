"""Task 4 final holdout 전역 소비 registry와 grant 계약 테스트."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, date, datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest

import autoresearch.research_harness as research_harness
import autoresearch.research_harness.consumption_registry as registry_module
from autoresearch.research_harness import (
    ConsumptionRegistryError,
    ConsumptionRegistryErrorCode,
    FinalConsumptionRequest,
    FinalConsumptionEvidence,
    LocalEvaluationFixtureRequest,
    build_final_target,
    build_local_evaluation_fixture,
    claim_final_consumption,
)
from autoresearch.research_harness.consumption_registry import FinalConsumptionGrant
from autoresearch.research_harness.judge import JudgeError, JudgeErrorCode
from autoresearch.research_harness.local_evaluation_fixture import _io_path


_STARTED_AT = datetime(2026, 9, 2, 3, 4, 5, tzinfo=UTC)
_BASE_SHA = "a" * 40
_CANDIDATE_SHA = "b" * 40


@pytest.fixture(scope="module")
def source_handoff(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("registry-source")
    receipt = build_local_evaluation_fixture(
        LocalEvaluationFixtureRequest(root, date(2026, 9, 1), 1937)
    )
    return receipt.judge


def _case(source_handoff, tmp_path: Path):
    state_root = tmp_path / "judge-state"
    snapshot_root = (
        state_root
        / "evaluation-snapshots"
        / "by-hash"
        / str(source_handoff.snapshot_fingerprint)
    )
    snapshot_root.parent.mkdir(parents=True)
    shutil.copytree(_io_path(source_handoff.snapshot_root), _io_path(snapshot_root))
    (state_root / "final-holdout-consumed").mkdir()
    handoff = replace(source_handoff, snapshot_root=snapshot_root)
    request = FinalConsumptionRequest(
        judge_state_root=state_root,
        handoff=handoff,
        baseline_sha=_BASE_SHA,
        candidate_sha=_CANDIDATE_SHA,
        started_at=_STARTED_AT,
    )
    return state_root, handoff, request


def test_public_registry_interface_and_opaque_grant() -> None:
    expected = {
        "ConsumptionRegistryError",
        "ConsumptionRegistryErrorCode",
        "FinalConsumptionEvidence",
        "FinalConsumptionGrant",
        "FinalConsumptionRequest",
        "build_final_target",
        "claim_final_consumption",
    }

    assert expected <= set(research_harness.__all__)
    with pytest.raises(ConsumptionRegistryError) as captured:
        FinalConsumptionGrant()

    assert captured.value.code is ConsumptionRegistryErrorCode.INVALID_REQUEST
    assert repr(captured.value) == (
        "ConsumptionRegistryError(code=<ConsumptionRegistryErrorCode.INVALID_REQUEST: "
        "'invalid_request'>, stage='grant_construction')"
    )


def test_claim_writes_canonical_durable_marker_and_builds_final_target(
    source_handoff,
    tmp_path: Path,
) -> None:
    state_root, handoff, request = _case(source_handoff, tmp_path)

    grant = claim_final_consumption(request)

    expected_payload = {
        "baseline_sha": _BASE_SHA,
        "candidate_sha": _CANDIDATE_SHA,
        "contract_version": "final-consumption-v1",
        "evaluation_id": str(handoff.final_holdout_id),
        "started_at": "2026-09-02T03:04:05.000000Z",
    }
    expected_bytes = (
        json.dumps(expected_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")
    evidence = grant.evidence

    assert evidence.marker_path == (
        state_root.resolve()
        / "final-holdout-consumed"
        / str(handoff.final_holdout_id)
    )
    assert evidence.marker_path.read_bytes() == expected_bytes
    assert evidence.marker_sha256 == sha256(expected_bytes).hexdigest()
    assert "opaque" in repr(grant)
    assert build_final_target(handoff, grant) is not None


def test_existing_marker_is_consumed_without_parsing(
    source_handoff,
    tmp_path: Path,
) -> None:
    state_root, handoff, request = _case(source_handoff, tmp_path)
    marker = (
        state_root / "final-holdout-consumed" / str(handoff.final_holdout_id)
    )
    marker.write_bytes(b"{broken")

    with pytest.raises(ConsumptionRegistryError) as captured:
        claim_final_consumption(request)

    assert captured.value.code is ConsumptionRegistryErrorCode.ALREADY_CONSUMED
    assert marker.read_bytes() == b"{broken"


def test_concurrent_claim_has_exactly_one_winner(
    source_handoff,
    tmp_path: Path,
) -> None:
    _, _, request = _case(source_handoff, tmp_path)

    def claim() -> str:
        try:
            claim_final_consumption(request)
        except ConsumptionRegistryError as error:
            return error.code.value
        return "granted"

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = tuple(executor.map(lambda _: claim(), range(8)))

    assert outcomes.count("granted") == 1
    assert outcomes.count(ConsumptionRegistryErrorCode.ALREADY_CONSUMED.value) == 7


def test_directory_sync_failure_keeps_consumed_marker(
    source_handoff,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root, handoff, request = _case(source_handoff, tmp_path)

    def fail_sync(_: Path) -> None:
        raise OSError("sensitive local detail")

    monkeypatch.setattr(registry_module, "sync_directory", fail_sync)
    with pytest.raises(ConsumptionRegistryError) as captured:
        claim_final_consumption(request)

    marker = state_root / "final-holdout-consumed" / str(handoff.final_holdout_id)
    assert captured.value.code is ConsumptionRegistryErrorCode.STATE_UNAVAILABLE
    assert "sensitive" not in str(captured.value)
    assert marker.exists()
    with pytest.raises(ConsumptionRegistryError) as second:
        claim_final_consumption(request)
    assert second.value.code is ConsumptionRegistryErrorCode.ALREADY_CONSUMED


def test_prior_evidence_missing_is_integrity_violation_without_recreation(
    source_handoff,
    tmp_path: Path,
) -> None:
    _, _, request = _case(source_handoff, tmp_path)
    grant = claim_final_consumption(request)
    grant.evidence.marker_path.unlink()

    with pytest.raises(ConsumptionRegistryError) as captured:
        claim_final_consumption(request, prior_evidence=grant.evidence)

    assert captured.value.code is ConsumptionRegistryErrorCode.INTEGRITY_VIOLATION
    assert not grant.evidence.marker_path.exists()


def test_prior_evidence_verification_does_not_depend_on_new_request_time(
    source_handoff,
    tmp_path: Path,
) -> None:
    _, _, request = _case(source_handoff, tmp_path)
    grant = claim_final_consumption(request)
    resumed = replace(request, started_at=datetime(2026, 9, 3, tzinfo=UTC))

    with pytest.raises(ConsumptionRegistryError) as captured:
        claim_final_consumption(resumed, prior_evidence=grant.evidence)

    assert captured.value.code is ConsumptionRegistryErrorCode.ALREADY_CONSUMED


@pytest.mark.parametrize("invalid", ["relative", "missing_registry", "foreign_snapshot"])
def test_invalid_state_root_fails_before_marker(
    source_handoff,
    tmp_path: Path,
    invalid: str,
) -> None:
    state_root, handoff, request = _case(source_handoff, tmp_path)
    if invalid == "relative":
        request = replace(request, judge_state_root=Path("relative"))
    elif invalid == "missing_registry":
        (state_root / "final-holdout-consumed").rmdir()
    else:
        foreign = tmp_path / "foreign"
        (foreign / "final-holdout-consumed").mkdir(parents=True)
        request = replace(request, judge_state_root=foreign)

    with pytest.raises(ConsumptionRegistryError) as captured:
        claim_final_consumption(request)

    assert captured.value.code in {
        ConsumptionRegistryErrorCode.INVALID_REQUEST,
        ConsumptionRegistryErrorCode.STATE_UNAVAILABLE,
    }
    assert not (
        request.judge_state_root
        / "final-holdout-consumed"
        / str(handoff.final_holdout_id)
    ).exists()


def test_state_root_ancestor_cannot_create_a_second_registry(
    source_handoff,
    tmp_path: Path,
) -> None:
    state_root, handoff, request = _case(source_handoff, tmp_path)
    claim_final_consumption(request)
    ancestor_registry = tmp_path / "final-holdout-consumed"
    ancestor_registry.mkdir()
    second_request = replace(request, judge_state_root=tmp_path)

    with pytest.raises(ConsumptionRegistryError) as captured:
        claim_final_consumption(second_request)

    assert captured.value.code is ConsumptionRegistryErrorCode.STATE_UNAVAILABLE
    assert not (ancestor_registry / str(handoff.final_holdout_id)).exists()
    assert (
        state_root / "final-holdout-consumed" / str(handoff.final_holdout_id)
    ).exists()


def test_grant_has_no_externally_callable_issuance_factory() -> None:
    assert not hasattr(FinalConsumptionGrant, "_from_claim")


def test_internal_grant_without_a_real_canonical_marker_is_rejected(
    source_handoff,
    tmp_path: Path,
) -> None:
    _, handoff, _ = _case(source_handoff, tmp_path)
    fake = FinalConsumptionEvidence(
        marker_path=(
            tmp_path
            / "final-holdout-consumed"
            / str(handoff.final_holdout_id)
        ).resolve(),
        marker_sha256="f" * 64,
    )
    grant = registry_module._issue_grant(handoff, fake)

    with pytest.raises(JudgeError) as captured:
        build_final_target(handoff, grant)

    assert captured.value.code is JudgeErrorCode.INVALID_TARGET


def test_forged_handoff_does_not_consume_marker(
    source_handoff,
    tmp_path: Path,
) -> None:
    state_root, handoff, request = _case(source_handoff, tmp_path)
    request = replace(
        request,
        handoff=replace(handoff, manifest_sha256="0" * 64),
    )

    with pytest.raises(ConsumptionRegistryError) as captured:
        claim_final_consumption(request)

    assert captured.value.code is ConsumptionRegistryErrorCode.INVALID_REQUEST
    assert not (
        state_root / "final-holdout-consumed" / str(handoff.final_holdout_id)
    ).exists()


def test_final_target_rejects_grant_reused_with_another_handoff(
    source_handoff,
    tmp_path: Path,
) -> None:
    _, handoff, request = _case(source_handoff, tmp_path)
    grant = claim_final_consumption(request)
    other = replace(handoff, manifest_sha256="0" * 64)

    with pytest.raises(JudgeError) as captured:
        build_final_target(other, grant)

    assert captured.value.code is JudgeErrorCode.INVALID_TARGET
