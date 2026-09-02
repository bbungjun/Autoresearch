import inspect
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import autoresearch.research_harness as research_harness
import pyarrow as pa
import pyarrow.parquet as pq
from autoresearch.action_log_generation.pipeline import EVENT_LOG_PARQUET_SCHEMA
from autoresearch.action_log_generation.schema import EventLog
from autoresearch.action_log_generation.slate_identity import (
    SlateIdentity,
    SlateMember,
    generate_slate_id,
)
from autoresearch.research_harness.evaluation_snapshot_models import (
    EvaluationSnapshotManifest,
    EvaluationSnapshotReceipt,
    EvaluationSnapshotRequest,
)
from autoresearch.research_harness.evaluation_source import ActionLogSource


def _fixture_event(
    partition_date: date,
    sequence: int,
    *,
    event_type: str,
    user_id: str,
    video_id: str,
    event_timestamp: datetime,
    slate_id: str,
) -> EventLog:
    return EventLog.model_validate(
        {
            "event_id": f"evt_{partition_date:%Y%m%d}_{sequence:08d}",
            "event_timestamp": event_timestamp,
            "user_id": user_id,
            "event_type": event_type,
            "video_id": video_id,
            "watch_time_sec": None,
            "rank": None,
            "source": "historical",
            "policy": None,
            "ctr_score": None,
            "is_exploration": None,
            "policy_version": None,
            "exposure_source": None,
            "slate_id": slate_id,
        }
    )


def _write_partition(root: Path, partition_date: date, events: tuple[EventLog, ...]) -> None:
    rows = tuple(
        event.model_dump()
        | {
            "schema_version": "action_log_schema_v1",
            "prompt_version": "action_log_ctr_v4",
            "llm_model": "fixture-model",
            "generated_at": "2026-09-01T00:00:00Z",
        }
        for event in events
    )
    target = root / f"dt={partition_date.isoformat()}" / "part-0.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=EVENT_LOG_PARQUET_SCHEMA), target)


def _snapshot_request(tmp_path: Path) -> EvaluationSnapshotRequest:
    root = tmp_path / "action-log"
    evaluation_date = date(2026, 9, 1)
    base_timestamp = datetime(2026, 9, 1, tzinfo=UTC)
    events: list[EventLog] = []
    sequence = 1
    for user_id in ("user-0", "user-15"):
        members = tuple(
            SlateMember(
                video_id=f"{user_id}-video-{index}",
                rank=None,
                exposure_source=None,
                policy_version=None,
            )
            for index in (1, 2)
        )
        slate_id = generate_slate_id(
            SlateIdentity(
                partition_date=evaluation_date,
                user_id=user_id,
                members=members,
            )
        )
        for index in (1, 2):
            events.append(
                _fixture_event(
                    evaluation_date,
                    sequence,
                    event_type="impression",
                    user_id=user_id,
                    video_id=f"{user_id}-video-{index}",
                    event_timestamp=base_timestamp + timedelta(minutes=index),
                    slate_id=slate_id,
                )
            )
            sequence += 1
        events.append(
            _fixture_event(
                evaluation_date,
                sequence,
                event_type="click",
                user_id=user_id,
                video_id=f"{user_id}-video-1",
                event_timestamp=base_timestamp + timedelta(minutes=10),
                slate_id=slate_id,
            )
        )
        sequence += 1
    _write_partition(root, date(2026, 8, 31), ())
    _write_partition(root, evaluation_date, tuple(events))
    _write_partition(root, date(2026, 9, 2), ())
    return EvaluationSnapshotRequest(
        action_log_root=str(root),
        history_start_date=date(2026, 8, 31),
        evaluation_start_date=evaluation_date,
        evaluation_end_date=evaluation_date,
        slate_id_cutover_date=evaluation_date,
        output_root=tmp_path / "output",
    )


def test_public_surface_exports_only_snapshot_entrypoint_contract() -> None:
    # Given
    expected_exports = {
        "ActionLogSource",
        "CandidateDataManifest",
        "CandidateDataViewReceipt",
        "CandidateDataViewRequest",
        "CandidateHistoryReceipt",
        "ConfirmationDecision",
        "DomainError",
        "DomainErrorCode",
        "EvaluationSnapshotError",
        "EvaluationSnapshotReceipt",
        "EvaluationSnapshotRequest",
        "FixtureDescriptor",
        "FixtureInputReceipt",
        "FixturePartitionReceipt",
        "JudgeSnapshotHandoff",
        "JudgeError",
        "JudgeErrorCode",
        "JudgeDecision",
        "JudgeMetric",
        "JudgeReasonCode",
        "JudgeScoringResult",
        "LocalEvaluationFixtureReceipt",
        "LocalEvaluationFixtureRequest",
        "MetricDelta",
        "PairedJudgeResult",
        "RankingMetricError",
        "RankingMetricErrorCode",
        "RankingMetricResult",
        "ResearchDomain",
        "ScreeningResult",
        "SealedPredictionReceipt",
        "SnapshotErrorCode",
        "StageCError",
        "StageCErrorCode",
        "YouTubeCTRDomain",
        "build_evaluation_snapshot",
        "build_local_evaluation_fixture",
        "build_validation_target",
        "materialize_candidate_data_view",
        "canonical_fixture_dates",
        "compare_confirmation",
        "descriptor_sha256",
        "ndcg_at_k",
        "recall_at_k",
        "screen_candidate",
        "seal_prediction_copy",
        "score_predictions",
        "select_fixture_user_ids",
    }

    # When
    signature = inspect.signature(research_harness.build_evaluation_snapshot)

    # Then
    assert set(research_harness.__all__) == expected_exports
    assert signature == inspect.Signature(
        parameters=(
            inspect.Parameter(
                "request",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=EvaluationSnapshotRequest,
            ),
            inspect.Parameter(
                "source",
                inspect.Parameter.KEYWORD_ONLY,
                default=None,
                annotation=ActionLogSource | None,
            ),
        ),
        return_annotation=EvaluationSnapshotReceipt,
    )


def test_build_evaluation_snapshot_publishes_real_local_parquet(tmp_path: Path) -> None:
    # Given
    request = _snapshot_request(tmp_path)

    # When
    receipt = research_harness.build_evaluation_snapshot(request)

    # Then
    assert receipt.target_path == (
        request.output_root
        / "evaluation-snapshots"
        / "by-hash"
        / receipt.snapshot_fingerprint
    )
    assert receipt.reused is False
    assert (receipt.target_path / "manifest.json").is_file()
    assert (receipt.target_path / "_SUCCESS").read_text(encoding="utf-8") == (
        f"{receipt.snapshot_fingerprint}\n"
    )


def test_public_builder_manifest_uses_actual_utc_call_time(tmp_path: Path) -> None:
    request = _snapshot_request(tmp_path)
    before = datetime.now(UTC)

    receipt = research_harness.build_evaluation_snapshot(request)
    after = datetime.now(UTC)
    manifest_bytes = (receipt.target_path / "manifest.json").read_bytes()
    manifest = EvaluationSnapshotManifest.model_validate_json(manifest_bytes)

    assert manifest.created_at.tzinfo is not None
    assert manifest.created_at.utcoffset() == timedelta(0)
    assert before <= manifest.created_at <= after
    assert f'"created_at":"{manifest.created_at:%Y-%m-%dT%H:%M:%S.%fZ}"' in (
        manifest_bytes.decode("utf-8")
    )
