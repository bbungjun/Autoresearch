import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import autoresearch.research_harness as research_harness
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from autoresearch.action_log_generation.pipeline import EVENT_LOG_PARQUET_SCHEMA
from autoresearch.action_log_generation.schema import EventLog
from autoresearch.action_log_generation.slate_identity import (
    SlateIdentity,
    SlateMember,
    generate_slate_id,
)
from autoresearch.research_harness.evaluation_snapshot_models import EvaluationSnapshotRequest
from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)


_HISTORY_DATE = date(2026, 8, 31)
_EVALUATION_DATE = date(2026, 9, 1)
_LABEL_SCAN_DATE = date(2026, 9, 2)
_STALE_SLATE_ID = "stale-pre-cutover-slate-id"


def _event(
    partition_date: date,
    sequence: int,
    *,
    event_type: str,
    user_id: str,
    video_id: str,
    slate_id: str,
    timestamp: datetime,
) -> EventLog:
    return EventLog.model_validate(
        {
            "event_id": f"evt_{partition_date:%Y%m%d}_{sequence:08d}",
            "event_timestamp": timestamp,
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


def _request_with_stale_slate(
    tmp_path: Path,
    *,
    stale_partition_date: date,
) -> EvaluationSnapshotRequest:
    action_log_root = tmp_path / "action-log"
    history_event = _event(
        _HISTORY_DATE,
        1,
        event_type="impression",
        user_id="legacy-user",
        video_id="legacy-video",
        slate_id=(
            _STALE_SLATE_ID
            if stale_partition_date == _HISTORY_DATE
            else "legacy-history-slate-id"
        ),
        timestamp=datetime(2026, 8, 31, tzinfo=UTC),
    )
    evaluation_events: list[EventLog] = []
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
        slate_id = (
            _STALE_SLATE_ID
            if stale_partition_date == _EVALUATION_DATE and user_id == "user-0"
            else str(
                generate_slate_id(
                    SlateIdentity(_EVALUATION_DATE, user_id, members)
                )
            )
        )
        for index in (1, 2):
            evaluation_events.append(
                _event(
                    _EVALUATION_DATE,
                    sequence,
                    event_type="impression",
                    user_id=user_id,
                    video_id=f"{user_id}-video-{index}",
                    slate_id=slate_id,
                    timestamp=datetime(2026, 9, 1, tzinfo=UTC)
                    + timedelta(minutes=index),
                )
            )
            sequence += 1
        evaluation_events.append(
            _event(
                _EVALUATION_DATE,
                sequence,
                event_type="click",
                user_id=user_id,
                video_id=f"{user_id}-video-1",
                slate_id=slate_id,
                timestamp=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=10),
            )
        )
        sequence += 1
    _write_partition(action_log_root, _HISTORY_DATE, (history_event,))
    _write_partition(action_log_root, _EVALUATION_DATE, tuple(evaluation_events))
    _write_partition(action_log_root, _LABEL_SCAN_DATE, ())
    return EvaluationSnapshotRequest(
        action_log_root=str(action_log_root),
        history_start_date=_HISTORY_DATE,
        evaluation_start_date=_EVALUATION_DATE,
        evaluation_end_date=_EVALUATION_DATE,
        slate_id_cutover_date=_EVALUATION_DATE,
        output_root=tmp_path / "output",
    )


def _request_with_pre_cutover_stale_slate(tmp_path: Path) -> EvaluationSnapshotRequest:
    return _request_with_stale_slate(
        tmp_path,
        stale_partition_date=_HISTORY_DATE,
    )


def test_build_evaluation_snapshot_excludes_pre_cutover_malformed_slate_identity(
    tmp_path: Path,
) -> None:
    # Given
    request = _request_with_pre_cutover_stale_slate(tmp_path)

    # When
    receipt = research_harness.build_evaluation_snapshot(request)

    # Then
    assert receipt.reused is False
    assert (receipt.target_path / "manifest.json").is_file()


def test_build_evaluation_snapshot_keeps_pre_cutover_receipt_in_source_and_history(
    tmp_path: Path,
) -> None:
    # Given
    request = _request_with_pre_cutover_stale_slate(tmp_path)

    # When
    receipt = research_harness.build_evaluation_snapshot(request)

    # Then
    manifest = json.loads((receipt.target_path / "manifest.json").read_text(encoding="utf-8"))
    assert [partition["dt"] for partition in manifest["source"]["partitions"]] == [
        "2026-08-31",
        "2026-09-01",
        "2026-09-02",
    ]
    assert [
        partition["dt"]
        for partition in manifest["window"]["candidate_history_partitions"]
    ] == ["2026-08-31"]


def test_build_evaluation_snapshot_omits_pre_cutover_row_from_output(
    tmp_path: Path,
) -> None:
    # Given
    request = _request_with_pre_cutover_stale_slate(tmp_path)

    # When
    receipt = research_harness.build_evaluation_snapshot(request)

    # Then
    published_slate_ids = tuple(
        slate_id
        for split_name in ("validation", "final_holdout")
        for slate_id in pq.read_table(
            receipt.target_path / split_name / "slate.parquet"
        )
        .column("slate_id")
        .to_pylist()
    )
    assert _STALE_SLATE_ID not in published_slate_ids


def test_build_evaluation_snapshot_rejects_malformed_slate_on_or_after_cutover(
    tmp_path: Path,
) -> None:
    # Given
    request = _request_with_stale_slate(
        tmp_path,
        stale_partition_date=_EVALUATION_DATE,
    )

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        research_harness.build_evaluation_snapshot(request)

    # Then
    assert captured.value.code is SnapshotErrorCode.SLATE_ID_INVALID
    assert captured.value.dt == _EVALUATION_DATE
    assert not (request.output_root / "evaluation-snapshots").exists()
