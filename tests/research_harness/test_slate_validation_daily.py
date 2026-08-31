from datetime import UTC, date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.action_log_generation.daily import run_daily_action_log
from autoresearch.action_log_generation.pipeline import (
    EVENT_LOG_PARQUET_SCHEMA,
)
from autoresearch.research_harness.evaluation_snapshot_models import EvaluationSnapshotRequest
from autoresearch.research_harness.evaluation_source import load_required_partitions
from autoresearch.research_harness.slate_validation import validate_slate_identities
from tests.action_log_generation.test_action_logs_daily import _closed_loop_factory


def _write_virtual_user(path: Path) -> None:
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "user_id": "user-1",
                    "age": 30,
                    "sex": "female",
                    "persona_summary": "fixture user",
                    "primary_categories": ["Gaming"],
                    "interest_keywords": ["game"],
                    "hobby_keywords": [],
                    "lifestyle_keywords": [],
                    "watch_time_band": "night",
                }
            ]
        ),
        path,
    )


def _write_videos(root: Path, partition_date: date) -> None:
    target = root / f"dt={partition_date.isoformat()}" / "part-0.parquet"
    target.parent.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "video_id": f"video-{index}",
                    "video_title": f"fixture video {index}",
                    "video_description": "fixture",
                    "video_tags": ["game"],
                    "video_view_count": 100 - index,
                    "video_like_count": 10,
                    "video_comment_count": 1,
                    "channel_title": "fixture channel",
                    "video_published_at": datetime(2026, 8, 1, tzinfo=UTC),
                }
                for index in range(3)
            ]
        ),
        target,
    )


def _write_empty_partition(root: Path, partition_date: date) -> None:
    target = root / f"dt={partition_date.isoformat()}" / "part-0.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist([], schema=EVENT_LOG_PARQUET_SCHEMA), target)


def _request(root: Path, output_root: Path) -> EvaluationSnapshotRequest:
    return EvaluationSnapshotRequest(
        action_log_root=str(root),
        history_start_date=date(2026, 8, 31),
        evaluation_start_date=date(2026, 9, 1),
        evaluation_end_date=date(2026, 9, 1),
        slate_id_cutover_date=date(2026, 9, 1),
        output_root=output_root,
    )


def test_daily_all_null_metadata_round_trips_through_loader_and_validator(
    tmp_path: Path,
) -> None:
    # Given
    partition_date = date(2026, 9, 1)
    users_path = tmp_path / "users.parquet"
    youtube_root = tmp_path / "youtube"
    action_log_root = tmp_path / "action-log"
    _write_virtual_user(users_path)
    _write_videos(youtube_root, partition_date)
    _ = run_daily_action_log(
        partition_date=partition_date,
        youtube_base_path=str(youtube_root),
        virtual_users_path=str(users_path),
        output_base_path=str(action_log_root),
        candidates_per_user=3,
        click_threshold=0.2,
        seed=123,
        generator_name="rule_based",
    )
    _write_empty_partition(action_log_root, date(2026, 8, 31))
    _write_empty_partition(action_log_root, date(2026, 9, 2))
    partitions = load_required_partitions(_request(action_log_root, tmp_path / "output"))

    # When
    result = validate_slate_identities(partitions)

    # Then
    assert result is None
    assert partitions[1].events
    assert all(
        (event.exposure_source, event.rank, event.policy_version) == (None, None, None)
        for event in partitions[1].events
    )


def test_daily_tagged_metadata_round_trips_through_loader_and_validator(
    tmp_path: Path,
) -> None:
    # Given
    partition_date = date(2026, 9, 1)
    users_path = tmp_path / "users.parquet"
    youtube_root = tmp_path / "youtube"
    action_log_root = tmp_path / "action-log"
    _write_virtual_user(users_path)
    _write_videos(youtube_root, partition_date)
    _ = run_daily_action_log(
        partition_date=partition_date,
        youtube_base_path=str(youtube_root),
        virtual_users_path=str(users_path),
        output_base_path=str(action_log_root),
        candidates_per_user=3,
        click_threshold=0.2,
        seed=123,
        generator_name="rule_based",
        candidate_provider_factory=_closed_loop_factory,
    )
    _write_empty_partition(action_log_root, date(2026, 8, 31))
    _write_empty_partition(action_log_root, date(2026, 9, 2))
    partitions = load_required_partitions(_request(action_log_root, tmp_path / "output"))

    # When
    result = validate_slate_identities(partitions)

    # Then
    assert result is None
    assert all(
        event.exposure_source == "model"
        and event.rank is not None
        and event.rank >= 1
        and event.policy_version == "run-a"
        for event in partitions[1].events
    )
