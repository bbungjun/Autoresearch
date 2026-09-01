import json
import re
import shutil
import tempfile
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pyarrow.fs import FileInfo, FileType

import autoresearch.action_log_generation.daily as daily_module
import autoresearch.action_log_generation.pipeline as pipeline_module
from autoresearch.action_log_generation.daily import (
    merge_daily_action_log_shards,
    run_daily_action_log,
    run_daily_action_log_shard,
)
from autoresearch.action_log_generation.llm_generator import RuleBasedActionLogGenerator
from autoresearch.action_log_generation.pipeline import ActionLogGenerationError, ExposureMetadata
from autoresearch.action_log_generation.schema import (
    ActionLogShardManifest,
    EventLog,
    SlateGenerationContext,
)


class _SelectorFilesystem:
    """FileSelector 호출만 검증하는 GCS filesystem 대역."""

    def __init__(self, infos=None, error: OSError | None = None):
        self.infos = list(infos or [])
        self.error = error
        self.selector = None

    def get_file_info(self, selector):
        self.selector = selector
        if self.error is not None:
            raise self.error
        return self.infos


def test_list_files_treats_missing_gcs_checkpoint_parts_prefix_as_empty():
    filesystem = _SelectorFilesystem()
    parts_path = "bucket/action_log_checkpoints/dt=2026-07-10/shard=000/parts"

    assert daily_module._list_files(parts_path, filesystem=filesystem) == []
    assert filesystem.selector.base_dir == parts_path
    assert filesystem.selector.recursive is False
    assert filesystem.selector.allow_not_found is True


def test_checkpoint_load_completed_restores_existing_parquet_parts(monkeypatch):
    part_path = "bucket/checkpoints/parts/part-work-a.parquet"
    filesystem = _SelectorFilesystem(
        [
            FileInfo(part_path, type=FileType.File),
            FileInfo("bucket/checkpoints/parts/.keep", type=FileType.File),
        ]
    )
    expected_drafts = [SimpleNamespace(event_id="draft-a")]
    monkeypatch.setattr(
        daily_module,
        "read_action_log_checkpoint_part",
        lambda path, filesystem: SimpleNamespace(
            work_id="work-a",
            drafts=expected_drafts,
        ),
    )
    store = daily_module._ActionLogCheckpointStore(
        partition_date=date(2026, 7, 10),
        shard_index=0,
        shard_count=1,
        checkpoint_base_path="bucket/checkpoints",
        config_fingerprint="fingerprint",
        config={},
        filesystem=filesystem,
    )

    assert store.load_completed() == {"work-a": expected_drafts}
    assert filesystem.selector.allow_not_found is True


def test_list_files_propagates_non_missing_filesystem_errors():
    filesystem = _SelectorFilesystem(error=PermissionError("permission denied"))

    with pytest.raises(PermissionError, match="permission denied"):
        daily_module._list_files("bucket/checkpoints/parts", filesystem=filesystem)


def _write_virtual_users(path, count: int = 3):
    users = []
    for i in range(count):
        users.append(
            {
                "user_id": f"vu_{i:04d}",
                "age": 20 + i,
                "sex": "female" if i % 2 == 0 else "male",
                "persona_summary": "테스트 유저",
                "primary_categories": ["Gaming", "Music"],
                "interest_keywords": ["게임", "음악"],
                "hobby_keywords": [],
                "lifestyle_keywords": [],
                "watch_time_band": "night",
            }
        )
    pq.write_table(pa.Table.from_pylist(users), path)


@pytest.fixture
def short_checkpoint_path(request: pytest.FixtureRequest) -> Path:
    working_directory = Path.cwd()
    uses_drive_root = bool(working_directory.drive)
    directory = tempfile.TemporaryDirectory(
        prefix="t4-", dir=working_directory.anchor if uses_drive_root else None
    )
    request.addfinalizer(directory.cleanup)
    if uses_drive_root:
        return Path(f"\\\\?\\{directory.name}")
    return Path(directory.name)


def _write_youtube_partition(base, partition_date: date, count: int = 12):
    partition_dir = base / f"dt={partition_date:%Y-%m-%d}"
    partition_dir.mkdir(parents=True)
    rows = []
    for i in range(count):
        rows.append(
            {
                "video_id": f"yt_{i:04d}",
                "video_title": f"게임 음악 영상 {i}",
                "video_description": "테스트 설명",
                "video_tags": ["게임", "음악"],
                "video_view_count": 10_000 - i,
                "video_like_count": 100 + i,
                "video_comment_count": 10 + i,
                "channel_title": f"채널 {i}",
                "video_published_at": datetime(2026, 7, 1, tzinfo=UTC),
            }
        )
    pq.write_table(pa.Table.from_pylist(rows), partition_dir / "part-0.parquet")


def test_daily_non_slate_full_row_projection_is_characterized(tmp_path) -> None:
    # Given
    partition_date = date(2026, 7, 1)
    users_path = tmp_path / "users.parquet"
    youtube_base = tmp_path / "youtube"
    output_base = tmp_path / "output"
    _write_virtual_users(users_path, count=1)
    _write_youtube_partition(youtube_base, partition_date, count=4)
    common = {
        "user_id": "vu_0000",
        "rank": None,
        "source": "historical",
        "policy": None,
        "ctr_score": None,
        "is_exploration": None,
        "policy_version": None,
        "exposure_source": None,
        "schema_version": "action_log_schema_v1",
        "prompt_version": "action_log_ctr_v4",
        "llm_model": "fixture-rule-action-log",
        "dt": "2026-07-01",
    }
    expected = [
        {
            **common,
            "event_id": "evt_20260701_00000000",
            "event_timestamp": datetime(2026, 7, 1, 2, 33, 4, tzinfo=UTC),
            "event_type": "impression",
            "video_id": "yt_0001",
            "watch_time_sec": None,
        },
        {
            **common,
            "event_id": "evt_20260701_00000001",
            "event_timestamp": datetime(2026, 7, 1, 2, 33, 11, tzinfo=UTC),
            "event_type": "click",
            "video_id": "yt_0001",
            "watch_time_sec": None,
        },
        {
            **common,
            "event_id": "evt_20260701_00000002",
            "event_timestamp": datetime(2026, 7, 1, 2, 33, 14, tzinfo=UTC),
            "event_type": "view",
            "video_id": "yt_0001",
            "watch_time_sec": 161,
        },
        {
            **common,
            "event_id": "evt_20260701_00000003",
            "event_timestamp": datetime(2026, 6, 30, 15, 44, 19, tzinfo=UTC),
            "event_type": "impression",
            "video_id": "yt_0000",
            "watch_time_sec": None,
        },
    ]

    # When
    run_daily_action_log(
        partition_date=partition_date,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(users_path),
        output_base_path=str(output_base),
        candidates_per_user=2,
        click_threshold=0.2,
        seed=123,
        generator_name="rule_based",
    )
    rows = pq.read_table(
        output_base / "dt=2026-07-01" / "part-0.parquet"
    ).to_pylist()

    # Then
    assert [
        {key: value for key, value in row.items() if key not in {"generated_at", "slate_id"}}
        for row in rows
    ] == expected
    assert all(row["slate_id"] is not None for row in rows)


def test_daily_request_and_manifest_restore_canonical_slate_context(tmp_path) -> None:
    # Given
    partition_date = date(2026, 7, 1)
    request = daily_module._build_request(
        partition_date=partition_date,
        tmp_dir=tmp_path / "direct",
        candidates_per_user=2,
        click_threshold=0.2,
        personalized_ratio=0.7,
        popular_ratio=0.2,
        exploration_ratio=0.1,
        seed=123,
        max_concurrency=1,
        chunk_size=0,
        max_quarantine_ratio=0.5,
        history_end=None,
    )
    manifest = ActionLogShardManifest(
        partition_date=partition_date,
        shard_index=0,
        shard_count=1,
        generator="rule_based",
        model_name="fixture-rule-action-log",
        generator_config={},
        candidates_per_user=2,
        click_threshold=0.2,
        personalized_ratio=0.7,
        popular_ratio=0.2,
        exploration_ratio=0.1,
        seed=123,
        chunk_size=0,
        max_quarantine_ratio=0.5,
        history_end=request.history_end,
        total_work=1,
        completed_work=1,
        quarantine_count=0,
        quarantine_error_counts={},
        schema_version="action_log_schema_v1",
        prompt_version="action_log_ctr_v4",
        input_fingerprint="0" * 64,
        config_fingerprint="1" * 64,
    )

    # When
    restored = daily_module._manifest_request(manifest, tmp_path / "merge")
    payload = daily_module._fingerprint_payload(
        generator_name=manifest.generator,
        model_name=manifest.model_name,
        generator_config=manifest.generator_config,
        input_fingerprint=manifest.input_fingerprint,
        request=request,
    )

    # Then
    expected = SlateGenerationContext(partition_date=partition_date)
    assert request.slate_context == expected
    assert restored.slate_context == expected
    assert payload["slate_context"] == {
        "partition_date": "2026-07-01",
        "producer": "daily-action-log-v1",
    }


def test_run_daily_action_log_writes_dt_partition(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    output_base = tmp_path / "action_log"
    quarantine_base = tmp_path / "action_log_quarantine"

    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)

    summary = run_daily_action_log(
        partition_date=partition_date,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(output_base),
        quarantine_base_path=str(quarantine_base),
        candidates_per_user=5,
        click_threshold=0.2,
        seed=123,
        generator_name="rule_based",
    )

    output_path = output_base / "dt=2026-07-01" / "part-0.parquet"
    quarantine_path = quarantine_base / "dt=2026-07-01" / "quarantine.jsonl"
    table = pq.read_table(output_path)

    assert summary["output_path"] == str(output_path)
    assert summary["impressions"] == 15
    assert summary["clicks"] == 3
    assert table.num_rows == summary["total_events"]
    assert quarantine_path.exists()


def test_run_daily_action_log_coalesces_small_output_into_one_target_row_group(
    tmp_path: Path,
) -> None:
    """작은 daily output은 사용자 수와 무관하게 하나의 target row group으로 coalesce한다."""

    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube"
    output_base = tmp_path / "action_log"
    _write_virtual_users(virtual_users_path, count=4)
    _write_youtube_partition(youtube_base, partition_date)

    summary = run_daily_action_log(
        partition_date=partition_date,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(output_base),
        candidates_per_user=3,
        click_threshold=0.2,
        max_concurrency=2,
        chunk_size=1,
        generator_name="rule_based",
    )

    parquet = pq.ParquetFile(output_base / "dt=2026-07-01" / "part-0.parquet")
    table = parquet.read(columns=["event_id"])
    assert summary["users"] == 4
    assert table.num_rows == summary["total_events"]
    assert table.column("event_id").to_pylist() == sorted(
        table.column("event_id").to_pylist()
    )
    assert parquet.num_row_groups == 1


def test_single_daily_publishes_completion_generated_at_without_full_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """single daily final은 generation 시작이 아닌 완료 시각을 모든 row에 기록한다."""

    generation_started_at = "2026-07-30T08:00:00+00:00"
    completed_at = "2026-07-30T09:00:00+00:00"
    frozen_times = iter(
        [
            datetime.fromisoformat(generation_started_at),
            datetime.fromisoformat(completed_at),
        ]
    )

    class _FrozenPipelineDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return next(frozen_times)

    class _GenerationStartClockGenerator(RuleBasedActionLogGenerator):
        def generate(self, virtual_user: dict, videos: list[dict]) -> str:
            assert (
                pipeline_module.datetime.now(UTC)
                .replace(microsecond=0)
                .isoformat()
                == generation_started_at
            )
            return super().generate(virtual_user, videos)

    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube"
    output_base = tmp_path / "action_log"
    _write_virtual_users(virtual_users_path, count=1)
    _write_youtube_partition(youtube_base, partition_date)
    monkeypatch.setattr(pipeline_module, "datetime", _FrozenPipelineDatetime)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda _generator_name, _model_name=None: _GenerationStartClockGenerator(),
    )

    summary = run_daily_action_log(
        partition_date=partition_date,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(output_base),
        candidates_per_user=2,
        click_threshold=0.2,
        max_concurrency=1,
        generator_name="rule_based",
    )

    final_path = output_base / "dt=2026-07-01" / "part-0.parquet"
    assert summary["status"] == "succeeded"
    assert {
        row["generated_at"]
        for row in pq.read_table(final_path, columns=["generated_at"]).to_pylist()
    } == {completed_at}


def test_daily_explicit_completion_timestamp_is_written_by_production_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube"
    first_output = tmp_path / "first"
    second_output = tmp_path / "second"
    _write_virtual_users(virtual_users_path, count=2)
    _write_youtube_partition(youtube_base, partition_date)
    completion_timestamp = datetime(2026, 7, 1, tzinfo=UTC)

    class _FrozenCompletionDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return completion_timestamp

    monkeypatch.setattr(pipeline_module, "datetime", _FrozenCompletionDatetime)
    common = {
        "partition_date": partition_date,
        "youtube_base_path": str(youtube_base),
        "virtual_users_path": str(virtual_users_path),
        "candidates_per_user": 3,
        "click_threshold": 0.2,
        "seed": 81,
        "max_concurrency": 1,
        "generator_name": "rule_based",
    }

    run_daily_action_log(output_base_path=str(first_output), **common)
    run_daily_action_log(
        output_base_path=str(second_output),
        completion_timestamp=completion_timestamp,
        **common,
    )

    first_path = first_output / "dt=2026-07-01" / "part-0.parquet"
    second_path = second_output / "dt=2026-07-01" / "part-0.parquet"
    assert first_path.read_bytes() == second_path.read_bytes()
    assert {
        row["generated_at"]
        for row in pq.read_table(first_path, columns=["generated_at"]).to_pylist()
    } == {"2026-07-01T00:00:00+00:00"}


def test_validate_staged_event_parquet_reads_row_groups_without_read_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """staging 검증은 전체 파일이 아니라 필요한 컬럼의 row group만 읽어야 한다."""

    partition_date = date(2026, 7, 1)
    request = daily_module._build_request(
        partition_date=partition_date,
        tmp_dir=tmp_path,
        candidates_per_user=1,
        click_threshold=0.2,
        personalized_ratio=0.7,
        popular_ratio=0.2,
        exploration_ratio=0.1,
        seed=1,
        max_concurrency=1,
        chunk_size=0,
        max_quarantine_ratio=0.5,
        history_end=None,
    )
    events = [
        EventLog(
            event_id=f"evt_20260701_{index:08d}",
            event_timestamp=datetime(2026, 7, 1, 3 + index, tzinfo=UTC),
            user_id=f"u{index}",
            event_type="impression",
            video_id=f"v{index}",
            source="historical",
        )
        for index in range(2)
    ]
    with pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    ) as writer:
        writer.write_events([events[0]])
        writer.write_events([events[1]])
        writer.finalize_success("2026-07-30T09:00:00+00:00")

    def _whole_file_read_must_not_run(
        *_args: object,
        **_kwargs: object,
    ) -> NoReturn:
        pytest.fail("whole-file read is forbidden")

    monkeypatch.setattr(daily_module.pq, "read_table", _whole_file_read_must_not_run)
    daily_module._validate_staged_event_parquet(
        request.output_path,
        partition_date,
    )


def test_validate_staged_event_parquet_rejects_unrelated_schema(tmp_path: Path) -> None:
    """staging parquet은 action log의 exact schema여야 한다."""

    path = tmp_path / "bad.parquet"
    pq.write_table(pa.table({"unexpected": [1]}), path)

    with pytest.raises(ValueError, match="schema does not match"):
        daily_module._validate_staged_event_parquet(
            path,
            date(2026, 7, 1),
        )


def test_validate_staged_event_parquet_accepts_empty_event_file(tmp_path: Path) -> None:
    """모든 사용자가 quarantine된 빈 final parquet은 유효한 staging 산출물이다."""

    partition_date = date(2026, 7, 1)
    request = daily_module._build_request(
        partition_date=partition_date,
        tmp_dir=tmp_path,
        candidates_per_user=1,
        click_threshold=0.2,
        personalized_ratio=0.7,
        popular_ratio=0.2,
        exploration_ratio=0.1,
        seed=1,
        max_concurrency=1,
        chunk_size=0,
        max_quarantine_ratio=1.0,
        history_end=None,
    )
    with pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    ) as writer:
        writer.finalize_success("2026-07-30T09:00:00+00:00")

    daily_module._validate_staged_event_parquet(request.output_path, partition_date)


def test_validate_staged_event_parquet_rejects_null_timestamp(tmp_path: Path) -> None:
    """nullable Arrow schema라도 final event timestamp null은 publish 전에 거부해야 한다."""

    path = tmp_path / "null-timestamp.parquet"
    table = pa.Table.from_pylist(
        [{"event_id": "evt_20260701_00000000", "event_timestamp": None}],
        schema=pipeline_module.EVENT_LOG_PARQUET_SCHEMA,
    )
    pq.write_table(table, path)

    with pytest.raises(ValueError, match="invalid event_timestamp"):
        daily_module._validate_staged_event_parquet(path, date(2026, 7, 1))


def test_daily_context_rejects_duplicate_user_before_publish(
    tmp_path: Path,
) -> None:
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube"
    output_base = tmp_path / "action_log"
    _write_virtual_users(virtual_users_path, count=2)
    users = pq.read_table(virtual_users_path).to_pylist()
    users[1]["user_id"] = users[0]["user_id"]
    pq.write_table(pa.Table.from_pylist(users), virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)

    with pytest.raises(pipeline_module.ActionLogSlateContractError) as exc_info:
        run_daily_action_log(
            partition_date=partition_date,
            youtube_base_path=str(youtube_base),
            virtual_users_path=str(virtual_users_path),
            output_base_path=str(output_base),
            candidates_per_user=3,
            click_threshold=0.2,
            generator_name="rule_based",
        )

    final_path = output_base / "dt=2026-07-01" / "part-0.parquet"
    assert exc_info.value.code == "slate_capacity_exceeded"
    assert not final_path.exists()


def test_run_daily_action_log_applies_deterministic_max_users(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    output_base = tmp_path / "action_log"

    _write_virtual_users(virtual_users_path, count=3)
    _write_youtube_partition(youtube_base, partition_date)

    summary = run_daily_action_log(
        partition_date=partition_date,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(output_base),
        max_users=2,
        candidates_per_user=5,
        click_threshold=0.2,
        seed=123,
        generator_name="rule_based",
    )

    assert summary["users"] == 2
    assert summary["impressions"] == 10


def test_run_daily_action_log_keeps_event_timestamps_inside_partition_date(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    output_base = tmp_path / "action_log"

    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)

    run_daily_action_log(
        partition_date=partition_date,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(output_base),
        candidates_per_user=5,
        click_threshold=0.2,
        seed=123,
        generator_name="rule_based",
    )

    table = pq.read_table(output_base / "dt=2026-07-01" / "part-0.parquet")
    kst = ZoneInfo("Asia/Seoul")
    assert {
        row["event_timestamp"].astimezone(kst).date()
        for row in table.select(["event_timestamp"]).to_pylist()
    } == {partition_date}


def test_run_daily_action_log_rejects_timestamp_outside_partition_date(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    output_base = tmp_path / "action_log"

    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)

    with pytest.raises(pipeline_module.ActionLogSlateContractError) as exc_info:
        run_daily_action_log(
            partition_date=partition_date,
            youtube_base_path=str(youtube_base),
            virtual_users_path=str(virtual_users_path),
            output_base_path=str(output_base),
            candidates_per_user=5,
            click_threshold=0.2,
            seed=123,
            generator_name="rule_based",
            history_end=datetime(2026, 7, 3, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        )
    assert exc_info.value.code == "slate_partition_date_mismatch"


def test_sharded_daily_action_log_merges_global_partition(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"
    work_quarantine_base = tmp_path / "action_log_quarantine_work"
    output_base = tmp_path / "action_log"

    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)

    for shard_index in range(2):
        run_daily_action_log_shard(
            partition_date=partition_date,
            shard_index=shard_index,
            shard_count=2,
            youtube_base_path=str(youtube_base),
            virtual_users_path=str(virtual_users_path),
            output_base_path=str(work_base),
            quarantine_base_path=str(work_quarantine_base),
            candidates_per_user=5,
            click_threshold=0.2,
            seed=123,
            generator_name="rule_based",
        )

    shard_path = work_base / "dt=2026-07-01" / "shard=000" / "part-0.parquet"
    shard_table = pq.read_table(shard_path)
    manifest_path = work_base / "dt=2026-07-01" / "shard=000" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "event_id" not in shard_table.column_names
    assert shard_table.num_rows == 5
    assert manifest == {
        "manifest_version": "action_log_shard_manifest_v1",
        "partition_date": "2026-07-01",
        "shard_index": 0,
        "shard_count": 2,
        "generator": "rule_based",
        "model_name": "fixture-rule-action-log",
        "generator_config": {},
        "candidates_per_user": 5,
        "click_threshold": 0.2,
        "personalized_ratio": 0.7,
        "popular_ratio": 0.2,
        "exploration_ratio": 0.1,
        "seed": 123,
        "chunk_size": 0,
        "max_quarantine_ratio": 0.5,
        "history_end": "2026-07-01T15:00:00Z",
        "total_work": 1,
        "completed_work": 1,
        "quarantine_count": 0,
        "quarantine_error_counts": {},
        "schema_version": "action_log_schema_v1",
        "prompt_version": "action_log_ctr_v4",
        "input_fingerprint": manifest["input_fingerprint"],
        "config_fingerprint": manifest["config_fingerprint"],
    }
    assert len(manifest["input_fingerprint"]) == 64
    assert len(manifest["config_fingerprint"]) == 64

    summary = merge_daily_action_log_shards(
        partition_date=partition_date,
        shard_count=2,
        shard_output_base_path=str(work_base),
        output_base_path=str(output_base),
    )

    output_path = output_base / "dt=2026-07-01" / "part-0.parquet"
    table = pq.read_table(output_path)
    rows = table.to_pylist()
    event_ids = [row["event_id"] for row in rows]

    assert summary["impressions"] == 15
    assert summary["clicks"] == 3
    assert table.num_rows == summary["total_events"]
    # #295 A안: event_id에 이벤트 KST 날짜 네임스페이스가 들어가 evt_00000000 형식이 아니다.
    assert len(event_ids) == len(rows)
    for index, event_id in enumerate(event_ids):
        assert re.fullmatch(rf"evt_\d{{8}}_{index:08d}", event_id), event_id
    assert set(table["llm_model"].to_pylist()) == {"fixture-rule-action-log"}


def test_manifest_requires_click_threshold_fail_closed() -> None:
    """구버전(=click_threshold 부재) manifest는 역직렬화 시 fail-closed 해야 한다."""

    from pydantic import ValidationError

    from autoresearch.action_log_generation.schema import ActionLogShardManifest

    complete = {
        "manifest_version": "action_log_shard_manifest_v1",
        "partition_date": "2026-07-01",
        "shard_index": 0,
        "shard_count": 2,
        "generator": "rule_based",
        "model_name": "fixture-rule-action-log",
        "generator_config": {},
        "candidates_per_user": 5,
        "click_threshold": 0.2,
        "personalized_ratio": 0.7,
        "popular_ratio": 0.2,
        "exploration_ratio": 0.1,
        "seed": 123,
        "chunk_size": 0,
        "max_quarantine_ratio": 0.5,
        "history_end": "2026-07-01T15:00:00Z",
        "total_work": 1,
        "completed_work": 1,
        "quarantine_count": 0,
        "quarantine_error_counts": {},
        "schema_version": "action_log_schema_v1",
        "prompt_version": "action_log_ctr_v4",
        "input_fingerprint": "0" * 64,
        "config_fingerprint": "0" * 64,
    }

    # sanity: click_threshold를 포함하면 정상적으로 검증된다.
    ActionLogShardManifest.model_validate(complete)

    legacy = {key: value for key, value in complete.items() if key != "click_threshold"}
    with pytest.raises(ValidationError):
        ActionLogShardManifest.model_validate(legacy)


@pytest.mark.parametrize("shard_count", [1, 3])
def test_daily_single_and_shards_have_full_slate_parity(
    tmp_path,
    shard_count: int,
    short_checkpoint_path: Path,
) -> None:
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    single_base = tmp_path / "single"
    repeated_base = tmp_path / "repeated"
    work_base = tmp_path / "work"
    merged_base = tmp_path / "merged"

    _write_virtual_users(virtual_users_path, count=6)
    _write_youtube_partition(youtube_base, partition_date)
    common = {
        "partition_date": partition_date,
        "youtube_base_path": str(youtube_base),
        "virtual_users_path": str(virtual_users_path),
        "candidates_per_user": 5,
        "click_threshold": 0.2,
        "seed": 123,
        "generator_name": "rule_based",
    }
    single_summary = run_daily_action_log(
        **common,
        output_base_path=str(single_base),
    )
    run_daily_action_log(
        **common,
        output_base_path=str(repeated_base),
    )
    for shard_index in reversed(range(shard_count)):
        run_daily_action_log_shard(
            **common,
            shard_index=shard_index,
            shard_count=shard_count,
            output_base_path=str(work_base),
            checkpoint_base_path=str(short_checkpoint_path),
        )
    merged_summary = merge_daily_action_log_shards(
        partition_date=partition_date,
        shard_count=shard_count,
        shard_output_base_path=str(work_base),
        output_base_path=str(merged_base),
    )

    single = pq.read_table(single_base / "dt=2026-07-01" / "part-0.parquet").to_pylist()
    repeated = pq.read_table(
        repeated_base / "dt=2026-07-01" / "part-0.parquet"
    ).to_pylist()
    merged = pq.read_table(merged_base / "dt=2026-07-01" / "part-0.parquet").to_pylist()
    single_members = {
        (row["user_id"], row["slate_id"], row["video_id"])
        for row in single
        if row["event_type"] == "impression"
    }
    merged_members = {
        (row["user_id"], row["slate_id"], row["video_id"])
        for row in merged
        if row["event_type"] == "impression"
    }
    projections = [
        [
            {key: value for key, value in row.items() if key != "generated_at"}
            for row in rows
        ]
        for rows in (single, repeated, merged)
    ]

    assert all(row["slate_id"] is not None for row in single)
    assert projections[0] == projections[1] == projections[2]
    assert merged_members == single_members
    assert merged_summary["ctr"] == single_summary["ctr"]
    assert {row["llm_model"] for row in merged} == {
        row["llm_model"] for row in single
    } == {"fixture-rule-action-log"}


def test_merge_rejects_missing_or_tampered_shard_manifest(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "work"

    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)
    for shard_index in range(2):
        run_daily_action_log_shard(
            partition_date=partition_date,
            shard_index=shard_index,
            shard_count=2,
            youtube_base_path=str(youtube_base),
            virtual_users_path=str(virtual_users_path),
            output_base_path=str(work_base),
            candidates_per_user=5,
            click_threshold=0.2,
            seed=123,
        )

    manifest_path = work_base / "dt=2026-07-01" / "shard=001" / "manifest.json"
    original = manifest_path.read_text(encoding="utf-8")
    manifest_path.unlink()
    with pytest.raises(ValueError, match="missing shard manifest"):
        merge_daily_action_log_shards(
            partition_date=partition_date,
            shard_count=2,
            shard_output_base_path=str(work_base),
            output_base_path=str(tmp_path / "merged"),
        )

    manifest_path.write_text(original, encoding="utf-8")
    payload = json.loads(original)
    payload["model_name"] = ""
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="model_name"):
        merge_daily_action_log_shards(
            partition_date=partition_date,
            shard_count=2,
            shard_output_base_path=str(work_base),
            output_base_path=str(tmp_path / "merged"),
        )


def test_pre_context_fingerprint_fails_closed(
    tmp_path,
    short_checkpoint_path: Path,
) -> None:
    # Given
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube"
    work_base = tmp_path / "work"
    output_base = tmp_path / "output"
    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)
    run_daily_action_log_shard(
        partition_date=partition_date,
        shard_index=0,
        shard_count=1,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(work_base),
        checkpoint_base_path=str(short_checkpoint_path),
        candidates_per_user=3,
        click_threshold=0.2,
        seed=123,
    )
    merge_daily_action_log_shards(
        partition_date=partition_date,
        shard_count=1,
        shard_output_base_path=str(work_base),
        output_base_path=str(output_base),
    )
    final_path = output_base / "dt=2026-07-01" / "part-0.parquet"
    previous_final = final_path.read_bytes()
    manifest_path = work_base / "dt=2026-07-01" / "shard=000" / "manifest.json"
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = ActionLogShardManifest.model_validate(manifest_payload)
    request = daily_module._manifest_request(manifest, tmp_path / "request")
    fingerprint_payload = daily_module._fingerprint_payload(
        generator_name=manifest.generator,
        model_name=manifest.model_name,
        generator_config=manifest.generator_config,
        input_fingerprint=manifest.input_fingerprint,
        request=request,
    )
    legacy_payload = dict(fingerprint_payload)
    legacy_payload.pop("slate_context", None)
    legacy_fingerprint = daily_module.hashlib.sha256(
        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert legacy_fingerprint != manifest.config_fingerprint
    manifest_payload["config_fingerprint"] = legacy_fingerprint
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

    # When
    with pytest.raises(ValueError, match="config_fingerprint"):
        merge_daily_action_log_shards(
            partition_date=partition_date,
            shard_count=1,
            shard_output_base_path=str(work_base),
            output_base_path=str(output_base),
            overwrite=True,
        )

    # Then
    assert final_path.read_bytes() == previous_final


class _OneUserFailureGenerator(RuleBasedActionLogGenerator):
    def generate(self, virtual_user, videos):
        if virtual_user["user_id"] == "vu_0000":
            return "{not valid json"
        return super().generate(virtual_user, videos)


class _CountingGenerator(RuleBasedActionLogGenerator):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def generate(self, virtual_user, videos):
        self.calls += 1
        return super().generate(virtual_user, videos)


@pytest.mark.parametrize(
    ("max_quarantine_ratio", "should_raise"),
    [(0.4, False), (0.2, True)],
)
def test_quarantine_guard_is_global_at_merge(
    tmp_path,
    monkeypatch,
    max_quarantine_ratio,
    should_raise,
):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "work"
    work_quarantine_base = tmp_path / "work_quarantine"

    _write_virtual_users(virtual_users_path, count=4)
    _write_youtube_partition(youtube_base, partition_date)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda generator_name, model_name=None: _OneUserFailureGenerator(),
    )

    summaries = []
    for shard_index in range(2):
        summaries.append(
            run_daily_action_log_shard(
                partition_date=partition_date,
                shard_index=shard_index,
                shard_count=2,
                youtube_base_path=str(youtube_base),
                virtual_users_path=str(virtual_users_path),
                output_base_path=str(work_base),
                quarantine_base_path=str(work_quarantine_base),
                candidates_per_user=5,
                click_threshold=0.2,
                max_quarantine_ratio=max_quarantine_ratio,
            )
        )

    assert summaries[0]["drafts"] == 5
    assert summaries[0]["quarantined_users"] == 1
    def merge():
        return merge_daily_action_log_shards(
            partition_date=partition_date,
            shard_count=2,
            shard_output_base_path=str(work_base),
            output_base_path=str(tmp_path / "merged"),
        )
    if should_raise:
        with pytest.raises(ActionLogGenerationError, match="global quarantine ratio"):
            merge()
    else:
        summary = merge()
        assert summary["quarantine_count"] == 1
        assert summary["total_work"] == 4
        assert summary["invalid_json"] == 1


def test_run_daily_action_log_shard_writes_progress_json_dt_shard_path(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"

    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)

    summary = run_daily_action_log_shard(
        partition_date=partition_date,
        shard_index=1,
        shard_count=2,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(work_base),
        candidates_per_user=5,
        click_threshold=0.2,
        seed=123,
        generator_name="rule_based",
        progress_flush_chunks=1,
    )

    progress_path = (
        tmp_path
        / "action_log_progress"
        / "dt=2026-07-01"
        / "shard=001"
        / "progress.json"
    )
    payload = json.loads(progress_path.read_text(encoding="utf-8"))

    assert summary["progress_path"] == str(progress_path)
    assert payload["partition_date"] == "2026-07-01"
    assert payload["shard_index"] == 1
    assert payload["shard_count"] == 2
    assert payload["status"] == "success"
    assert payload["completed_chunks"] == payload["total_chunks"] == 2
    assert payload["success_chunks"] == 2
    assert payload["failed_chunks"] == 0
    assert payload["quarantined_chunks"] == 0
    assert payload["updated_at"].endswith("Z")


def test_run_daily_action_log_shard_ignores_progress_writer_failure(
    tmp_path,
    monkeypatch,
):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"

    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)

    def _raise_progress_write(payload, path, *, filesystem=None):
        raise OSError("progress write failed")

    monkeypatch.setattr(
        daily_module,
        "_write_progress_json_file",
        _raise_progress_write,
    )

    summary = run_daily_action_log_shard(
        partition_date=partition_date,
        shard_index=0,
        shard_count=2,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(work_base),
        candidates_per_user=5,
        click_threshold=0.2,
        seed=123,
        generator_name="rule_based",
        progress_flush_chunks=1,
    )

    output_path = work_base / "dt=2026-07-01" / "shard=000" / "part-0.parquet"
    assert summary["output_path"] == str(output_path)
    assert output_path.exists()


def test_shard_resume_calls_only_unfinished_work_after_interruption(
    tmp_path,
    monkeypatch,
):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"
    generator = _CountingGenerator()

    _write_virtual_users(virtual_users_path, count=3)
    _write_youtube_partition(youtube_base, partition_date)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda generator_name, model_name=None: generator,
    )
    original_write_part = daily_module._ActionLogCheckpointStore.write_part
    interrupted = False

    def _write_then_interrupt(self, work_id, work_order, drafts):
        nonlocal interrupted
        original_write_part(self, work_id, work_order, drafts)
        if not interrupted:
            interrupted = True
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(
        daily_module._ActionLogCheckpointStore,
        "write_part",
        _write_then_interrupt,
    )
    kwargs = {
        "partition_date": partition_date,
        "shard_index": 0,
        "shard_count": 1,
        "youtube_base_path": str(youtube_base),
        "virtual_users_path": str(virtual_users_path),
        "output_base_path": str(work_base),
        "candidates_per_user": 4,
        "click_threshold": 0.2,
        "chunk_size": 2,
        "max_concurrency": 1,
    }

    with pytest.raises(RuntimeError, match="simulated interruption"):
        run_daily_action_log_shard(**kwargs)
    assert generator.calls == 1

    monkeypatch.setattr(
        daily_module._ActionLogCheckpointStore,
        "write_part",
        original_write_part,
    )
    calls_before_resume = generator.calls
    summary = run_daily_action_log_shard(**kwargs)

    assert summary["total_work"] == 6
    assert generator.calls - calls_before_resume == 5
    assert Path(summary["manifest_path"]).exists()
    assert Path(summary["output_path"]).exists()


def test_checkpoint_fingerprint_isolates_changed_config(tmp_path, monkeypatch):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"
    generator = _CountingGenerator()

    _write_virtual_users(virtual_users_path, count=2)
    _write_youtube_partition(youtube_base, partition_date)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda generator_name, model_name=None: generator,
    )
    common = {
        "partition_date": partition_date,
        "shard_index": 0,
        "shard_count": 1,
        "youtube_base_path": str(youtube_base),
        "virtual_users_path": str(virtual_users_path),
        "output_base_path": str(work_base),
        "candidates_per_user": 4,
        "click_threshold": 0.2,
        "chunk_size": 2,
    }

    first = run_daily_action_log_shard(**common, seed=1)
    calls_after_first = generator.calls
    second = run_daily_action_log_shard(**common, seed=2)

    assert first["config_fingerprint"] != second["config_fingerprint"]
    assert first["checkpoint_path"] != second["checkpoint_path"]
    assert generator.calls - calls_after_first == second["total_work"]


def test_prompt_version_change_isolates_checkpoint_and_supports_rollback(
    tmp_path,
    monkeypatch,
):
    """프롬프트 버전 rollout은 namespace를 분리하고 rollback을 보존한다."""
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"
    generator = _CountingGenerator()

    _write_virtual_users(virtual_users_path, count=2)
    _write_youtube_partition(youtube_base, partition_date)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda generator_name, model_name=None: generator,
    )
    common = {
        "partition_date": partition_date,
        "shard_index": 0,
        "shard_count": 1,
        "youtube_base_path": str(youtube_base),
        "virtual_users_path": str(virtual_users_path),
        "output_base_path": str(work_base),
        "candidates_per_user": 4,
        "click_threshold": 0.2,
        "chunk_size": 2,
    }

    monkeypatch.setattr(daily_module, "PROMPT_VERSION", "action_log_ctr_v2")
    v2_first = run_daily_action_log_shard(**common)
    calls_after_v2 = generator.calls

    monkeypatch.setattr(daily_module, "PROMPT_VERSION", "action_log_ctr_v3")
    v3 = run_daily_action_log_shard(**common)
    calls_after_v3 = generator.calls

    assert v2_first["config_fingerprint"] != v3["config_fingerprint"]
    assert v2_first["checkpoint_path"] != v3["checkpoint_path"]
    assert calls_after_v3 - calls_after_v2 == v3["total_work"]

    monkeypatch.setattr(daily_module, "PROMPT_VERSION", "action_log_ctr_v2")
    v2_rollback = run_daily_action_log_shard(**common)

    assert v2_rollback["config_fingerprint"] == v2_first["config_fingerprint"]
    assert v2_rollback["checkpoint_path"] == v2_first["checkpoint_path"]
    assert generator.calls == calls_after_v3


def test_checkpoint_fingerprint_isolates_changed_input_content(tmp_path, monkeypatch):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"
    generator = _CountingGenerator()

    _write_virtual_users(virtual_users_path, count=2)
    _write_youtube_partition(youtube_base, partition_date)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda generator_name, model_name=None: generator,
    )
    kwargs = {
        "partition_date": partition_date,
        "shard_index": 0,
        "shard_count": 1,
        "youtube_base_path": str(youtube_base),
        "virtual_users_path": str(virtual_users_path),
        "output_base_path": str(work_base),
        "candidates_per_user": 4,
        "click_threshold": 0.2,
        "chunk_size": 2,
    }

    first = run_daily_action_log_shard(**kwargs)
    users = pq.read_table(virtual_users_path).to_pylist()
    users[0]["persona_summary"] = "변경된 입력"
    pq.write_table(pa.Table.from_pylist(users), virtual_users_path)
    calls_after_first = generator.calls
    second = run_daily_action_log_shard(**kwargs, overwrite=True)

    assert first["config_fingerprint"] != second["config_fingerprint"]
    assert first["checkpoint_path"] != second["checkpoint_path"]
    assert generator.calls - calls_after_first == second["total_work"]


def test_checkpoint_duplicate_parts_are_deduplicated_by_work_id(
    tmp_path,
    monkeypatch,
):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"
    first_generator = _CountingGenerator()

    _write_virtual_users(virtual_users_path, count=2)
    _write_youtube_partition(youtube_base, partition_date)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda generator_name, model_name=None: first_generator,
    )
    kwargs = {
        "partition_date": partition_date,
        "shard_index": 0,
        "shard_count": 1,
        "youtube_base_path": str(youtube_base),
        "virtual_users_path": str(virtual_users_path),
        "output_base_path": str(work_base),
        "candidates_per_user": 4,
        "click_threshold": 0.2,
        "chunk_size": 2,
    }
    first = run_daily_action_log_shard(**kwargs)
    parts_dir = Path(first["checkpoint_path"]) / "parts"
    source = sorted(parts_dir.glob("*.parquet"))[0]
    shutil.copyfile(source, parts_dir / "duplicate.parquet")

    resumed_generator = _CountingGenerator()
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda generator_name, model_name=None: resumed_generator,
    )
    second = run_daily_action_log_shard(**kwargs)

    assert resumed_generator.calls == 0
    assert second["drafts"] == first["drafts"]


def test_single_quarantine_publish_failure_warns_and_keeps_final_success(
    tmp_path,
    monkeypatch,
):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube"
    output_base = tmp_path / "action_log"
    quarantine_base = tmp_path / "quarantine"
    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)
    original_copy = daily_module._copy_local_file

    def fail_quarantine_copy(source, destination, *, filesystem=None):
        if str(destination).endswith("quarantine.jsonl"):
            raise OSError("quarantine storage unavailable")
        return original_copy(source, destination, filesystem=filesystem)

    monkeypatch.setattr(daily_module, "_copy_local_file", fail_quarantine_copy)

    summary = run_daily_action_log(
        partition_date=partition_date,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(output_base),
        quarantine_base_path=str(quarantine_base),
        candidates_per_user=5,
        click_threshold=0.2,
    )

    assert summary["status"] == "succeeded"
    assert summary["warnings"] == [
        {
            "event": "warning",
            "warning_type": "quarantine_publish_failed",
            "artifact": "quarantine",
        }
    ]
    assert (output_base / "dt=2026-07-01" / "part-0.parquet").exists()
    assert not (quarantine_base / "dt=2026-07-01" / "quarantine.jsonl").exists()


def test_single_default_preserves_legacy_overwrite_behavior(tmp_path, monkeypatch):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube"
    output_base = tmp_path / "action_log"
    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)
    generator = _CountingGenerator()
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda *args, **kwargs: generator,
    )
    common = {
        "partition_date": partition_date,
        "youtube_base_path": str(youtube_base),
        "virtual_users_path": str(virtual_users_path),
        "output_base_path": str(output_base),
        "candidates_per_user": 5,
        "click_threshold": 0.2,
    }

    first = run_daily_action_log(**common)
    first_call_count = generator.calls
    second = run_daily_action_log(**common)

    assert first["status"] == "succeeded"
    assert second["status"] == "succeeded"
    assert generator.calls == first_call_count * 2


def test_single_skips_existing_final_before_generator_creation(tmp_path, monkeypatch):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube"
    output_path = tmp_path / "action_log" / "dt=2026-07-01" / "part-0.parquet"
    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)
    common = {
        "partition_date": partition_date,
        "youtube_base_path": str(youtube_base),
        "virtual_users_path": str(virtual_users_path),
        "output_base_path": str(tmp_path / "action_log"),
        "candidates_per_user": 5,
        "click_threshold": 0.2,
    }
    run_daily_action_log(**common)
    previous = output_path.read_bytes()
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda *args, **kwargs: pytest.fail("generator must not be created"),
    )

    summary = run_daily_action_log(**common, overwrite=False)

    assert summary["status"] == "skipped"
    assert output_path.read_bytes() == previous


def test_single_failed_overwrite_preserves_previous_final(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube"
    output_base = tmp_path / "action_log"
    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)
    common = {
        "partition_date": partition_date,
        "youtube_base_path": str(youtube_base),
        "virtual_users_path": str(virtual_users_path),
        "output_base_path": str(output_base),
        "candidates_per_user": 5,
        "click_threshold": 0.2,
    }
    run_daily_action_log(**common)
    output_path = output_base / "dt=2026-07-01" / "part-0.parquet"
    previous = output_path.read_bytes()

    with pytest.raises(pipeline_module.ActionLogSlateContractError) as exc_info:
        run_daily_action_log(
            **common,
            overwrite=True,
            history_end=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
        )

    assert exc_info.value.code == "slate_partition_date_mismatch"
    assert output_path.read_bytes() == previous


def _write_final_parquet(path: Path, schema: pa.Schema, partition_date: date) -> None:
    """주어진 schema로 파티션 날짜 내 timestamp 한 건을 담은 final parquet을 기록한다."""

    timestamp = datetime(
        partition_date.year, partition_date.month, partition_date.day, 3, 0, tzinfo=UTC
    )
    row = {}
    for field in schema:
        if field.name == "event_timestamp":
            row[field.name] = timestamp
        elif pa.types.is_integer(field.type):
            row[field.name] = 0
        elif pa.types.is_floating(field.type):
            row[field.name] = 0.0
        elif pa.types.is_boolean(field.type):
            row[field.name] = False
        else:
            row[field.name] = "x"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist([row], schema=schema), path)


def test_validate_existing_final_tolerates_schema_without_additive_columns(
    tmp_path,
):
    from autoresearch.action_log_generation.pipeline import EVENT_LOG_PARQUET_SCHEMA

    partition_date = date(2026, 7, 1)
    legacy_schema = pa.schema(
        [
            field
            for field in EVENT_LOG_PARQUET_SCHEMA
            if field.name not in pipeline_module.OPTIONAL_ADDITIVE_COLUMNS
        ]
    )
    final_path = tmp_path / "dt=2026-07-01" / "part-0.parquet"
    _write_final_parquet(final_path, legacy_schema, partition_date)

    daily_module._validate_existing_final(str(final_path), partition_date)


@pytest.mark.parametrize(
    ("schema", "is_compatible"),
    (
        (pipeline_module.EVENT_LOG_PARQUET_SCHEMA, True),
        (
            pa.schema(
                [
                    field
                    for field in pipeline_module.EVENT_LOG_PARQUET_SCHEMA
                    if field.name != "slate_id"
                ]
            ),
            True,
        ),
        (
            pa.schema(
                [
                    field
                    for field in pipeline_module.EVENT_LOG_PARQUET_SCHEMA
                    if field.name not in {"exposure_source", "slate_id"}
                ]
            ),
            True,
        ),
        (
            pa.schema(
                [
                    field
                    for field in pipeline_module.EVENT_LOG_PARQUET_SCHEMA
                    if field.name != "event_id"
                ]
            ),
            False,
        ),
        (
            pipeline_module.EVENT_LOG_PARQUET_SCHEMA.set(
                pipeline_module.EVENT_LOG_PARQUET_SCHEMA.get_field_index("event_id"),
                pa.field("event_id", pa.int64()),
            ),
            False,
        ),
        (
            pipeline_module.EVENT_LOG_PARQUET_SCHEMA.set(
                pipeline_module.EVENT_LOG_PARQUET_SCHEMA.get_field_index("event_id"),
                pa.field("event_id", pa.string(), nullable=False),
            ),
            False,
        ),
        (
            pa.schema(
                [
                    pipeline_module.EVENT_LOG_PARQUET_SCHEMA.field(1),
                    pipeline_module.EVENT_LOG_PARQUET_SCHEMA.field(0),
                    *[
                        pipeline_module.EVENT_LOG_PARQUET_SCHEMA.field(index)
                        for index in range(2, len(pipeline_module.EVENT_LOG_PARQUET_SCHEMA))
                    ],
                ]
            ),
            False,
        ),
        (
            pipeline_module.EVENT_LOG_PARQUET_SCHEMA.append(
                pa.field("unexpected_column", pa.string())
            ),
            False,
        ),
    ),
    ids=(
        "current",
        "without-slate-id",
        "without-additive-columns",
        "missing-required-field",
        "wrong-required-field-type",
        "wrong-required-field-nullability",
        "wrong-field-order",
        "unexpected-field",
    ),
)
def test_schema_compatibility_allows_only_known_optional_column_subsets(
    schema,
    is_compatible,
):
    assert (
        daily_module._schema_is_compatible(
            schema,
            pipeline_module.EVENT_LOG_PARQUET_SCHEMA,
            pipeline_module.OPTIONAL_ADDITIVE_COLUMNS,
        )
        is is_compatible
    )


def test_schema_compatibility_rejects_reversed_legacy_generation():
    reversed_legacy_schema = pa.schema(
        [
            field
            for field in pipeline_module.EVENT_LOG_PARQUET_SCHEMA
            if field.name != "exposure_source"
        ]
    )

    assert not daily_module._schema_is_compatible(
        reversed_legacy_schema,
        pipeline_module.EVENT_LOG_PARQUET_SCHEMA,
        pipeline_module.OPTIONAL_ADDITIVE_COLUMNS,
    )


def test_validate_existing_final_rejects_unrelated_schema(tmp_path):
    partition_date = date(2026, 7, 1)
    unrelated_schema = pa.schema(
        [
            pa.field("event_timestamp", pa.timestamp("us", tz="UTC")),
            pa.field("unexpected_column", pa.string()),
        ]
    )
    final_path = tmp_path / "dt=2026-07-01" / "part-0.parquet"
    _write_final_parquet(final_path, unrelated_schema, partition_date)

    with pytest.raises(ValueError, match="schema does not match"):
        daily_module._validate_existing_final(str(final_path), partition_date)


def test_final_publish_staging_copy_failure_preserves_previous_file(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "new.parquet"
    destination = tmp_path / "dt=2026-07-01" / "part-0.parquet"
    source.write_bytes(b"new-result")
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"last-known-good")

    def fail_copy(source_path, destination_path):
        raise OSError("staging copy failed")

    monkeypatch.setattr(daily_module.shutil, "copyfile", fail_copy)

    with pytest.raises(OSError, match="staging copy failed"):
        daily_module._publish_final_file(source, str(destination))

    assert destination.read_bytes() == b"last-known-good"
    assert list(destination.parent.glob("*.staging")) == []


# 구 테스트 `test_remote_final_copy_failure_preserves_previous_object`는 staging→copy
# 메커니즘(copy_file 실패)을 단언했으므로 #515에서 제거했다. 그것이 지키던 요구
# ("publish 실패 시 last-known-good 보존")는
# `test_remote_publish_failure_preserves_last_known_good`가 새 메커니즘 기준으로 승계한다.


def test_merge_quality_failure_uses_manifest_counts_and_preserves_final(
    tmp_path,
    monkeypatch,
):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube"
    work_base = tmp_path / "work"
    output_base = tmp_path / "action_log"
    _write_virtual_users(virtual_users_path, count=2)
    _write_youtube_partition(youtube_base, partition_date)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda generator_name, model_name=None: _OneUserFailureGenerator(),
    )
    for shard_index in range(2):
        run_daily_action_log_shard(
            partition_date=partition_date,
            shard_index=shard_index,
            shard_count=2,
            youtube_base_path=str(youtube_base),
            virtual_users_path=str(virtual_users_path),
            output_base_path=str(work_base),
            candidates_per_user=5,
            click_threshold=0.2,
            max_quarantine_ratio=0.2,
        )

    first_manifest = json.loads(
        (work_base / "dt=2026-07-01" / "shard=000" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert first_manifest["quarantine_error_counts"] == {"invalid_json": 1}
    output_path = output_base / "dt=2026-07-01" / "part-0.parquet"
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"last-known-good")

    with pytest.raises(ActionLogGenerationError, match="global quarantine ratio"):
        merge_daily_action_log_shards(
            partition_date=partition_date,
            shard_count=2,
            shard_output_base_path=str(work_base),
            output_base_path=str(output_base),
            overwrite=True,
        )

    assert output_path.read_bytes() == b"last-known-good"


def test_merge_reports_unclassified_count_for_legacy_manifest(tmp_path, monkeypatch):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube"
    work_base = tmp_path / "work"
    _write_virtual_users(virtual_users_path, count=2)
    _write_youtube_partition(youtube_base, partition_date)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda generator_name, model_name=None: _OneUserFailureGenerator(),
    )
    for shard_index in range(2):
        run_daily_action_log_shard(
            partition_date=partition_date,
            shard_index=shard_index,
            shard_count=2,
            youtube_base_path=str(youtube_base),
            virtual_users_path=str(virtual_users_path),
            output_base_path=str(work_base),
            candidates_per_user=5,
            click_threshold=0.2,
            max_quarantine_ratio=0.5,
        )

    manifest_path = work_base / "dt=2026-07-01" / "shard=000" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.pop("quarantine_error_counts") == {"invalid_json": 1}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    summary = merge_daily_action_log_shards(
        partition_date=partition_date,
        shard_count=2,
        shard_output_base_path=str(work_base),
        output_base_path=str(tmp_path / "action_log"),
    )

    assert summary["quarantine_count"] == 1
    assert summary["unclassified_quarantine_count"] == 1
    assert sum(summary[key] for key in ("api_error", "invalid_json", "schema_fail")) == 0
    assert summary["warnings"] == [
        {
            "event": "warning",
            "warning_type": "quarantine_error_counts_unavailable",
            "artifact": "shard_manifest",
        }
    ]


def test_all_shards_share_input_fingerprint_and_do_not_touch_final(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube"
    work_base = tmp_path / "work"
    final_path = tmp_path / "action_log" / "dt=2026-07-01" / "part-0.parquet"
    _write_virtual_users(virtual_users_path, count=4)
    _write_youtube_partition(youtube_base, partition_date)
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"last-known-good")

    summaries = [
        run_daily_action_log_shard(
            partition_date=partition_date,
            shard_index=shard_index,
            shard_count=2,
            youtube_base_path=str(youtube_base),
            virtual_users_path=str(virtual_users_path),
            output_base_path=str(work_base),
            candidates_per_user=5,
            click_threshold=0.2,
            max_users=3,
        )
        for shard_index in range(2)
    ]
    manifests = [
        json.loads(
            (work_base / "dt=2026-07-01" / f"shard={index:03d}" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        for index in range(2)
    ]
    shard_users = [
        set(
            pq.read_table(
                work_base / "dt=2026-07-01" / f"shard={index:03d}" / "part-0.parquet",
                columns=["user_id"],
            )["user_id"].to_pylist()
        )
        for index in range(2)
    ]

    assert {summary["status"] for summary in summaries} == {"succeeded"}
    assert len({manifest["input_fingerprint"] for manifest in manifests}) == 1
    assert shard_users[0].isdisjoint(shard_users[1])
    assert shard_users[0] | shard_users[1] == {"vu_0000", "vu_0001", "vu_0002"}
    assert final_path.read_bytes() == b"last-known-good"


def _closed_loop_factory(videos):
    """(user_id, video_id) 태그를 채우며 상위 3개를 노출하는 폐루프 provider factory."""

    metadata: dict[tuple[str, str], ExposureMetadata] = {}

    def provider(virtual_user, user_rng):
        picked = videos[:3]
        user_id = str(virtual_user.get("user_id", ""))
        for position, video in enumerate(picked, start=1):
            metadata[(user_id, str(video["video_id"]))] = ExposureMetadata(
                policy="model",
                rank=position,
                ctr_score=0.5,
                is_exploration=False,
                policy_version="run-a",
                exposure_source="model",
            )
        return picked

    return provider, metadata


def test_daily_single_joins_exposure_tags_into_final_parquet(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    output_base = tmp_path / "action_log"

    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)

    summary = run_daily_action_log(
        partition_date=partition_date,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(output_base),
        candidates_per_user=3,
        click_threshold=0.2,
        seed=123,
        generator_name="rule_based",
        candidate_provider_factory=_closed_loop_factory,
        overwrite=True,
    )

    assert summary["status"] == "succeeded"
    table = pq.read_table(output_base / "dt=2026-07-01" / "part-0.parquet")
    assert "model" in set(table.column("exposure_source").to_pylist())


def test_daily_shard_then_merge_carries_exposure_tags(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"
    output_base = tmp_path / "action_log"

    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)

    run_daily_action_log_shard(
        partition_date=partition_date,
        shard_index=0,
        shard_count=1,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(work_base),
        candidates_per_user=3,
        click_threshold=0.2,
        seed=123,
        generator_name="rule_based",
        candidate_provider_factory=_closed_loop_factory,
    )
    merge_summary = merge_daily_action_log_shards(
        partition_date=partition_date,
        shard_count=1,
        shard_output_base_path=str(work_base),
        output_base_path=str(output_base),
    )

    assert merge_summary["status"] == "succeeded"
    table = pq.read_table(output_base / "dt=2026-07-01" / "part-0.parquet")
    assert "model" in set(table.column("exposure_source").to_pylist())


def test_cli_parses_click_threshold() -> None:
    from autoresearch.jobs.action_log import _build_parser

    # 브리프 원문은 --mode/--partition-date 없이 parse_args를 호출하지만, 두 인자는
    # argparse에 required=True로 선언되어 있어 그 상태로는 항상 BatchArgumentError가
    # 난다(구현과 무관). 파싱 대상 자체는 그대로 두고 필수 인자만 채워 의도(CLI가
    # --click-threshold를 인식·보관하는지)를 보존한다.
    args = _build_parser().parse_args(
        [
            "--mode",
            "single",
            "--partition-date",
            "2026-07-01",
            "--click-threshold",
            "0.6",
        ]
    )
    assert args.click_threshold == 0.6


def test_cli_requires_click_threshold() -> None:
    """--click-threshold 없이는 CLI가 조용히 0.55로 채우지 않고 실패해야 한다.

    #260 후속 수정으로 --click-threshold는 파서 레벨 required가 아니라
    single/shard 모드에서만 `_validate_args`가 강제하는 모드-스코프 필수
    인자다(merge는 click_threshold를 사용하지 않으므로 대상에서 제외).
    따라서 parse_args 자체는 성공하고, `_validate_args` 호출 시
    `BatchArgumentError`가 발생해야 한다.
    """

    from autoresearch.jobs.action_log import BatchArgumentError, _build_parser, _validate_args

    args = _build_parser().parse_args(
        ["--mode", "single", "--partition-date", "2026-07-22"]
    )
    assert args.click_threshold is None
    with pytest.raises(BatchArgumentError):
        _validate_args(args)



class _RecordingFilesystem:
    """publish 경로를 관찰하는 객체 저장소 대역(#515).

    `written`은 `open_output_stream`으로 실제 업로드가 일어난 경로를 순서대로 담는다.
    임시 객체가 파티션 프리픽스 **밖**에 만들어지는지 검증하는 데 쓴다.
    """

    def __init__(self, initial: dict[str, bytes] | None = None) -> None:
        self.objects: dict[str, bytes] = dict(initial or {})
        self.written: list[str] = []
        self.deleted: list[str] = []
        self.delete_error: Exception | None = None

    def open_output_stream(self, path: str):
        import io

        filesystem = self
        filesystem.written.append(path)

        class _Stream(io.BytesIO):
            def close(self) -> None:
                if not self.closed:
                    filesystem.objects[path] = self.getvalue()
                super().close()

        return _Stream()

    def copy_file(self, source: str, destination: str) -> None:
        self.objects[destination] = self.objects[source]

    def delete_file(self, path: str) -> None:
        self.deleted.append(path)
        if self.delete_error is not None:
            raise self.delete_error
        self.objects.pop(path, None)


_PARTITION = "bucket/data_lake/action_log/dt=2026-07-01/"
_DESTINATION = _PARTITION + "part-0.parquet"


def _under(filesystem: _RecordingFilesystem, prefix: str) -> list[str]:
    return sorted(key for key in filesystem.objects if key.startswith(prefix))


def test_remote_publish_stages_outside_partition_prefix(tmp_path):
    """임시 객체는 파티션 프리픽스 **밖**에 만든다(#515).

    적재가 ``dt=<날짜>/``를 와일드카드로 읽으므로, 임시 객체가 그 안에 있으면 정리가
    실패했을 때 같은 내용이 두 번 적재된다(event_id 전역 고유 계약 위반).
    """
    filesystem = _RecordingFilesystem({_DESTINATION: b"last-known-good"})
    source = tmp_path / "new.parquet"
    source.write_bytes(b"new-result")

    daily_module._publish_final_file(source, _DESTINATION, filesystem=filesystem)

    assert _under(filesystem, _PARTITION) == [_DESTINATION]
    assert filesystem.objects[_DESTINATION] == b"new-result"
    # 실제 업로드는 파티션 밖에서 일어났다.
    assert filesystem.written and all(
        not path.startswith(_PARTITION) for path in filesystem.written
    )


def test_remote_publish_upload_failure_preserves_last_known_good(tmp_path, monkeypatch):
    """업로드가 도중에 끊겨도 최종 경로는 건드리지 않는다(#515).

    pyarrow ``NativeFile``에는 abort가 없어 ``with`` 종료 시 ``close()``가 finalize한다.
    따라서 최종 경로에 직접 쓰면 **잘린 객체가 게시되어** last-known-good이 파괴된다.
    임시 경로로 먼저 올리고 서버측 ``copy_file``로 바꾸면 그 창이 사라진다.
    """
    filesystem = _RecordingFilesystem({_DESTINATION: b"last-known-good"})
    source = tmp_path / "new.parquet"
    source.write_bytes(b"new-result")

    def partial_then_fail(source_path, destination_path, *, filesystem=None):
        filesystem.objects[destination_path] = b"new-res"  # 잘린 상태로 finalize
        raise OSError("network dropped mid-upload")

    monkeypatch.setattr(daily_module, "_copy_local_file", partial_then_fail)

    with pytest.raises(OSError, match="network dropped mid-upload"):
        daily_module._publish_final_file(source, _DESTINATION, filesystem=filesystem)

    assert filesystem.objects[_DESTINATION] == b"last-known-good"
    # 잘린 객체가 파티션 안에 남지 않는다.
    assert _under(filesystem, _PARTITION) == [_DESTINATION]


def test_remote_publish_cleanup_failure_keeps_partition_clean(tmp_path):
    """정리가 실패해도 파티션은 오염되지 않고 게시는 성공한다(#515 회귀).

    이번 사고의 조건(삭제 권한 없음)을 그대로 재현한다. 임시 객체가 파티션 밖이므로
    남더라도 적재가 읽지 않는다.
    """
    filesystem = _RecordingFilesystem({_DESTINATION: b"last-known-good"})
    filesystem.delete_error = PermissionError("storage.objects.delete denied")
    source = tmp_path / "new.parquet"
    source.write_bytes(b"new-result")

    daily_module._publish_final_file(source, _DESTINATION, filesystem=filesystem)

    assert filesystem.objects[_DESTINATION] == b"new-result"
    assert _under(filesystem, _PARTITION) == [_DESTINATION]
    # 고아 객체는 남지만 파티션 밖이다.
    orphans = [key for key in filesystem.objects if key not in {_DESTINATION}]
    assert orphans and all(not key.startswith(_PARTITION) for key in orphans)


def test_remote_publish_stages_outside_shard_partition_prefix(tmp_path):
    """shard 경로(``dt=.../shard=NNN/``)에서도 같은 보장이 성립한다(#515)."""
    shard_prefix = "bucket/data_lake/action_log/dt=2026-07-01/shard=000/"
    destination = shard_prefix + "part-0.parquet"
    filesystem = _RecordingFilesystem({destination: b"last-known-good"})
    filesystem.delete_error = PermissionError("storage.objects.delete denied")
    source = tmp_path / "new.parquet"
    source.write_bytes(b"new-result")

    daily_module._publish_final_file(source, destination, filesystem=filesystem)

    assert _under(filesystem, shard_prefix) == [destination]
    assert all(not path.startswith(shard_prefix) for path in filesystem.written)
