from dataclasses import FrozenInstanceError, fields
from datetime import date, timedelta
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from autoresearch.research_harness.evaluation_snapshot_models import (
    ArtifactReceipt,
    EvaluationId,
    SnapshotFingerprint,
    WriterIdentity,
    WriterOptions,
)
from autoresearch.research_harness.evaluation_source_models import SourcePartitionReceipt
from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode
from autoresearch.research_harness.fixture_models import (
    CandidateDataManifest,
    CandidateDataViewReceipt,
    CandidateDataViewRequest,
    CandidateHistoryReceipt,
    FixtureDescriptor,
    FixtureInputReceipt,
    FixturePartitionReceipt,
    JudgeSnapshotHandoff,
    LocalEvaluationFixtureReceipt,
    LocalEvaluationFixtureRequest,
)


def _writer() -> WriterIdentity:
    return WriterIdentity(
        "pyarrow",
        "21.0.0",
        WriterOptions("2.6", "us", False, False, "NONE", False, 50000, True, "1.0", True),
    )


def _handoff() -> JudgeSnapshotHandoff:
    return JudgeSnapshotHandoff(
        SnapshotFingerprint("a" * 64),
        Path("snapshot"),
        "b" * 64,
        EvaluationId("eval_" + "c" * 64),
        EvaluationId("eval_" + "d" * 64),
    )


def _descriptor() -> FixtureDescriptor:
    evaluation_start_date = date(2026, 9, 1)
    partitions = tuple(
        FixturePartitionReceipt(
            partition_date,
            (
                "inputs/youtube_trending_kr/"
                f"dt={partition_date.isoformat()}/part-0.parquet"
            ),
            48,
            f"{index + 1:x}" * 64,
        )
        for index, partition_date in enumerate(
            evaluation_start_date + timedelta(days=offset)
            for offset in (-2, -1, 0, 1)
        )
    )
    return FixtureDescriptor(
        contract_version="youtube-ctr-local-fixture-v1",
        input_generator_version="youtube-ctr-input-v1",
        input_writer=_writer(),
        fixture_seed=7,
        generator="rule_based",
        generator_model="fixture-rule-action-log",
        history_start_date=date(2026, 8, 30),
        evaluation_start_date=evaluation_start_date,
        evaluation_end_date=evaluation_start_date,
        slate_id_cutover_date=date(2026, 8, 30),
        candidates_per_user=24,
        video_count_per_partition=48,
        click_threshold=0.0,
        personalized_ratio=0.7,
        popular_ratio=0.2,
        exploration_ratio=0.1,
        history_days_per_run=1,
        max_events_per_user_per_day=24,
        max_concurrency=1,
        chunk_size=0,
        max_quarantine_ratio=0.0,
        overwrite=False,
        validation_user_count=160,
        final_holdout_user_count=40,
        virtual_users=FixtureInputReceipt("inputs/virtual_users.parquet", 200, "e" * 64),
        youtube_partitions=partitions,
    )


def _candidate_manifest() -> CandidateDataManifest:
    return CandidateDataManifest(
        contract_version="candidate-data-view-v1",
        evaluation_id=EvaluationId("eval_" + "e" * 64),
        evaluation_start_date=date(2026, 9, 1),
        complete_history_label_end_date=date(2026, 8, 30),
        slate=ArtifactReceipt("slate.parquet", 24, "f" * 64),
        history_partitions=(
            CandidateHistoryReceipt(
                date(2026, 8, 30),
                "history/action_log/dt=2026-08-30/part-0.parquet",
                100,
                "a" * 64,
            ),
            CandidateHistoryReceipt(
                date(2026, 8, 31),
                "history/action_log/dt=2026-08-31/part-0.parquet",
                100,
                "b" * 64,
            ),
        ),
    )


def test_candidate_manifest_accepts_canonical_t_minus_2_and_t_minus_1_history() -> None:
    manifest = CandidateDataManifest(
        contract_version="candidate-data-view-v1",
        evaluation_id=EvaluationId("eval_" + "e" * 64),
        evaluation_start_date=date(2026, 9, 1),
        complete_history_label_end_date=date(2026, 8, 30),
        slate=ArtifactReceipt("slate.parquet", 24, "f" * 64),
        history_partitions=(
            CandidateHistoryReceipt(
                date(2026, 8, 30),
                "history/action_log/dt=2026-08-30/part-0.parquet",
                100,
                "a" * 64,
            ),
            CandidateHistoryReceipt(
                date(2026, 8, 31),
                "history/action_log/dt=2026-08-31/part-0.parquet",
                100,
                "b" * 64,
            ),
        ),
    )

    assert tuple(receipt.dt for receipt in manifest.history_partitions) == (
        date(2026, 8, 30),
        date(2026, 8, 31),
    )


def test_local_fixture_request_requires_non_negative_seed_and_is_frozen() -> None:
    with pytest.raises(TypeError):
        LocalEvaluationFixtureRequest(Path("judge"), date(2026, 9, 1))
    with pytest.raises(StageCError) as raised:
        LocalEvaluationFixtureRequest(Path("judge"), date(2026, 9, 1), -1)

    assert raised.value.code is StageCErrorCode.FIXTURE_REQUEST_INVALID
    request = LocalEvaluationFixtureRequest(Path("judge"), date(2026, 9, 1), 0)
    with pytest.raises(FrozenInstanceError):
        request.fixture_seed = 1


def test_fixture_descriptor_has_exact_frozen_json_contract() -> None:
    descriptor = _descriptor()

    assert list(FixtureDescriptor.model_fields) == [
        "contract_version",
        "input_generator_version",
        "input_writer",
        "fixture_seed",
        "generator",
        "generator_model",
        "history_start_date",
        "evaluation_start_date",
        "evaluation_end_date",
        "slate_id_cutover_date",
        "candidates_per_user",
        "video_count_per_partition",
        "click_threshold",
        "personalized_ratio",
        "popular_ratio",
        "exploration_ratio",
        "history_days_per_run",
        "max_events_per_user_per_day",
        "max_concurrency",
        "chunk_size",
        "max_quarantine_ratio",
        "overwrite",
        "validation_user_count",
        "final_holdout_user_count",
        "virtual_users",
        "youtube_partitions",
    ]
    payload = descriptor.model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        FixtureDescriptor.model_validate(payload)
    with pytest.raises(ValidationError):
        descriptor.fixture_seed = 8


@pytest.mark.parametrize(
    "relative_path",
    ("/absolute.parquet", "C:/drive.parquet", "../escape.parquet", "a\\b.parquet"),
)
def test_candidate_safe_receipts_reject_non_posix_relative_paths(relative_path: str) -> None:
    with pytest.raises(StageCError) as raised:
        FixtureInputReceipt(relative_path, 1, "a" * 64)

    assert raised.value.code is StageCErrorCode.FIXTURE_REQUEST_INVALID
    assert relative_path not in str(raised.value)


def test_candidate_contract_has_no_split_or_final_selector() -> None:
    request = CandidateDataViewRequest(_handoff(), Path("candidate"))
    manifest = _candidate_manifest()
    receipt = CandidateDataViewReceipt(Path("candidate/harness_in"), manifest, "b" * 64, False)

    assert [field.name for field in fields(request)] == ["judge", "destination_root"]
    assert [field.name for field in fields(receipt)] == [
        "root",
        "manifest",
        "manifest_sha256",
        "reused",
    ]
    payload = manifest.model_dump(mode="json")
    payload["split_name"] = "final_holdout"
    with pytest.raises(ValidationError):
        CandidateDataManifest.model_validate(payload)


@pytest.mark.parametrize(
    ("mutation", "stage"),
    (
        (
            lambda payload: payload.update(
                evaluation_id="eval_C:/judge/fixture_seed=917"
            ),
            "candidate_evaluation_id_validation",
        ),
        (
            lambda payload: payload.update(
                complete_history_label_end_date="2026-08-31"
            ),
            "candidate_window_validation",
        ),
        (
            lambda payload: payload["history_partitions"].reverse(),
            "candidate_history_order_validation",
        ),
        (
            lambda payload: payload["history_partitions"].append(
                payload["history_partitions"][0]
            ),
            "candidate_history_order_validation",
        ),
        (
            lambda payload: payload["history_partitions"][0].update(
                dt="2026-09-01",
                relative_path="history/action_log/dt=2026-09-01/part-0.parquet",
            ),
            "candidate_history_order_validation",
        ),
        (
            lambda payload: payload["history_partitions"][0].update(
                relative_path="history/action_log/dt=2026-08-31/part-0.parquet"
            ),
            "candidate_history_path_validation",
        ),
        (
            lambda payload: payload["slate"].update(
                relative_path="C:/judge/fixture_seed=917"
            ),
            "candidate_manifest_validation",
        ),
    ),
)
def test_candidate_manifest_rejects_noncanonical_or_judge_owned_values(
    mutation,
    stage: str,
) -> None:
    payload = _candidate_manifest().model_dump(mode="json")
    mutation(payload)

    with pytest.raises(StageCError) as raised:
        CandidateDataManifest.model_validate(payload)

    assert raised.value.code is StageCErrorCode.CANDIDATE_VIEW_CONFLICT
    assert raised.value.stage == stage
    assert "judge" not in str(raised.value).lower()
    assert "917" not in str(raised.value)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload.update(history_start_date="2026-08-29"),
        lambda payload: payload.update(slate_id_cutover_date="2026-08-31"),
        lambda payload: payload.update(evaluation_end_date="2026-09-02"),
        lambda payload: payload["virtual_users"].update(relative_path="inputs/users.parquet"),
        lambda payload: payload["virtual_users"].update(rows=199),
        lambda payload: payload["youtube_partitions"].reverse(),
        lambda payload: payload["youtube_partitions"].append(
            payload["youtube_partitions"][0]
        ),
        lambda payload: payload["youtube_partitions"][0].update(rows=47),
        lambda payload: payload["youtube_partitions"][0].update(
            relative_path="inputs/youtube_trending_kr/dt=2026-08-31/part-0.parquet"
        ),
        lambda payload: payload.update(unexpected=True),
    ),
)
def test_fixture_descriptor_json_rejects_semantic_drift_with_typed_error(
    mutation,
) -> None:
    payload = _descriptor().model_dump(mode="json")
    mutation(payload)

    with pytest.raises(StageCError) as raised:
        FixtureDescriptor.model_validate_json(json.dumps(payload))

    assert raised.value.code is StageCErrorCode.FIXTURE_REQUEST_INVALID


def test_all_stage_c_value_models_are_frozen_slotted_dataclasses() -> None:
    model_types = (
        LocalEvaluationFixtureRequest,
        FixtureInputReceipt,
        FixturePartitionReceipt,
        JudgeSnapshotHandoff,
        LocalEvaluationFixtureReceipt,
        CandidateHistoryReceipt,
        CandidateDataViewRequest,
        CandidateDataViewReceipt,
    )

    assert all(model_type.__dataclass_params__.frozen for model_type in model_types)
    assert all(hasattr(model_type, "__slots__") for model_type in model_types)

    fixture_receipt = LocalEvaluationFixtureReceipt(
        Path("fixture"),
        Path("fixture/fixture.json"),
        "a" * 64,
        (SourcePartitionReceipt(date(2026, 8, 30), "fixture://source", 1, "b" * 64),),
        _handoff(),
        False,
    )
    with pytest.raises(FrozenInstanceError):
        fixture_receipt.reused = True


def test_stage_c_error_codes_are_exact_and_message_is_sanitized() -> None:
    assert {code.value for code in StageCErrorCode} == {
        "fixture_request_invalid",
        "fixture_coverage_insufficient",
        "fixture_state_conflict",
        "candidate_view_conflict",
        "judge_handoff_invalid",
        "fixture_reproducibility_mismatch",
    }
    error = StageCError(
        code=StageCErrorCode.FIXTURE_STATE_CONFLICT,
        stage="fixture_validation",
        dt=date(2026, 9, 1),
        count=1,
        identifier_prefix="fixture-user-secret",
    )

    assert "fixture-user-sec" not in str(error)
    assert "fixture-user-secret" not in str(error)


def test_stage_c_contract_and_pure_input_helpers_are_available_from_facade() -> None:
    from autoresearch import research_harness

    assert research_harness.LocalEvaluationFixtureRequest is LocalEvaluationFixtureRequest
    assert research_harness.FixtureDescriptor is FixtureDescriptor
    assert research_harness.CandidateDataManifest is CandidateDataManifest
    assert research_harness.StageCError is StageCError
    assert callable(research_harness.canonical_fixture_dates)
    assert callable(research_harness.select_fixture_user_ids)
    assert callable(research_harness.descriptor_sha256)
