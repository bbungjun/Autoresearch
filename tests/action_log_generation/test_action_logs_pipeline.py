import gc
import json
import logging
import random
import re
import weakref
from concurrent.futures import Future
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from types import MappingProxyType, TracebackType
from typing import Callable, Never, Self

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

import autoresearch.action_log_generation.pipeline as pipeline_module
from autoresearch.action_log_generation.candidate import build_candidates
from autoresearch.action_log_generation.llm_generator import RuleBasedActionLogGenerator
from autoresearch.action_log_generation.pipeline import (
    ACTION_LOG_CHECKPOINT_PARQUET_SCHEMA,
    ACTION_LOG_DRAFT_PARQUET_SCHEMA,
    ActionLogGenerator,
    ActionLogGenerationError,
    ExposureMetadata,
    _ActionLogCallResult,
    _ActionLogWorkItem,
    _build_user_drafts,
    attach_exposure_tags,
    expand_action_log_drafts,
    generate_action_log_batch,
    generate_action_log_drafts,
    read_action_log_draft_parquet,
    select_clicks_per_slate,
    write_action_log_draft_parquet,
)
from autoresearch.action_log_generation.schema import (
    ACTION_LOG_SCHEMA_VERSION,
    EventGenerationRequest,
    EventLog,
    EventLogBatch,
    ImpressionDraft,
    QuarantineRecord,
    SlateGenerationContext,
)
from autoresearch.action_log_generation.slate_identity import (
    SlateId,
    SlateIdentity,
    SlateIdentityError,
    SlateIdentityErrorCode,
    SlateIdentityRegistry,
    canonical_slate_json,
)
from autoresearch.action_log_generation.video_source import (
    _parse_tags,
    build_fixture_video_records,
    nominal_duration_sec,
)

_FIXED_END = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)

_DRAFT_SCHEMA_FIELD_NAMES = [
    "user_id",
    "video_id",
    "click_propensity",
    "watch_fraction",
    "would_like",
    "duration_sec",
    "exposure_source",
    "exposure_rank",
    "exposure_ctr_score",
    "policy_version",
]


def test_draft_and_checkpoint_schema_field_lists_are_characterized() -> None:
    # Given
    expected_draft = _DRAFT_SCHEMA_FIELD_NAMES
    expected_checkpoint = ["work_id", "work_order", *_DRAFT_SCHEMA_FIELD_NAMES]

    # When
    draft_names = ACTION_LOG_DRAFT_PARQUET_SCHEMA.names
    checkpoint_names = ACTION_LOG_CHECKPOINT_PARQUET_SCHEMA.names

    # Then
    assert draft_names == expected_draft
    assert checkpoint_names == expected_checkpoint


def _fixture_users(n=6):
    cats = [
        ["Gaming", "Music"],
        ["Music", "Entertainment"],
        ["Education", "Science & Technology"],
        ["Food", "Howto & Style"],
        ["Travel & Events", "Sports"],
        ["News & Politics", "People & Blogs"],
    ]
    users = []
    for i in range(n):
        c = cats[i % len(cats)]
        users.append(
            {
                "user_id": f"vu_{i:04d}",
                "age": 20 + i,
                "sex": "male" if i % 2 else "female",
                "persona_summary": "테스트 유저",
                "primary_categories": c,
                "category_affinity": {c[0]: 0.8, c[1]: 0.6},
                "interest_keywords": ["게임" if "Gaming" in c else "음악"],
                "hobby_keywords": [],
                "lifestyle_keywords": [],
                "watch_time_band": "night",
            }
        )
    return users


def _request(tmp_path, **kw):
    base = dict(
        click_threshold=0.55,
        candidates_per_user=20,
        seed=42,
        history_end=_FIXED_END,
        history_days=30,
        output_path=str(tmp_path / "e.parquet"),
        warehouse_output_path=str(tmp_path / "e.jsonl"),
        quarantine_output_path=str(tmp_path / "q.jsonl"),
    )
    base.update(kw)
    return EventGenerationRequest(**base)


def test_end_to_end_long_event_stream(tmp_path):
    users, videos = _fixture_users(6), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, RuleBasedActionLogGenerator())
    events = result.batch.events

    impressions = [e for e in events if e.event_type == "impression"]
    clicks = [e for e in events if e.event_type == "click"]
    views = [e for e in events if e.event_type == "view"]
    likes = [e for e in events if e.event_type == "like"]

    assert len(impressions) == 6 * 20  # 유저당 후보 20 (pool 40)
    assert result.summary["impressions"] == 6 * 20
    # per-slate 커트라인(테스트 고정값 click_threshold=0.55): 유저당 클릭은 최대 1건이며,
    # 슬레이트 최고 click_propensity가 커트라인 이상일 때만 클릭이 발생한다.
    clicks_by_user: dict[str, int] = {}
    for c in clicks:
        clicks_by_user[c.user_id] = clicks_by_user.get(c.user_id, 0) + 1
    assert all(count == 1 for count in clicks_by_user.values())
    assert len(clicks) <= len(users)
    assert result.summary["clicks"] == len(clicks)
    assert len(views) == len(clicks)  # 클릭 선정분마다 view 1행
    assert len(likes) <= len(clicks)  # like는 would_like일 때만
    # view만 watch_time_sec>0, 그 외 event_type은 None
    for e in events:
        if e.event_type == "view":
            assert e.watch_time_sec is not None and e.watch_time_sec > 0
        else:
            assert e.watch_time_sec is None
        assert e.rank is None and e.source == "historical"
    # 클릭 선정 video는 impression·click·view를 모두 가진다
    clicked_keys = {(e.user_id, e.video_id) for e in clicks}
    imp_keys = {(e.user_id, e.video_id) for e in impressions}
    view_keys = {(e.user_id, e.video_id) for e in views}
    assert clicked_keys <= imp_keys and clicked_keys == view_keys
    assert (tmp_path / "e.parquet").exists()
    assert result.summary["quarantined_users"] == 0


def test_click_session_timestamps_are_monotonic(tmp_path):
    users, videos = _fixture_users(6), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, RuleBasedActionLogGenerator())
    # (user, video)별로 event_type 순서대로 timestamp가 단조 증가하는지
    by_key: dict = {}
    for e in result.batch.events:
        by_key.setdefault((e.user_id, e.video_id), []).append(e)
    order = {"impression": 0, "click": 1, "view": 2, "like": 3}
    for group in by_key.values():
        group.sort(key=lambda e: order[e.event_type])
        ts = [e.event_timestamp for e in group]
        assert all(a < b for a, b in zip(ts, ts[1:])), f"non-strict session order: {ts}"


def test_timestamps_within_history_window(tmp_path):
    users, videos = _fixture_users(4), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, RuleBasedActionLogGenerator())
    lo = _FIXED_END - timedelta(days=30)
    for event in result.batch.events:
        assert lo <= event.event_timestamp <= _FIXED_END


def test_impression_headroom_covers_max_session_span():
    # window 불변식(모든 이벤트 <= history_end)이 _MAX_DURATION과 결합돼 있음을 명시적으로 잠근다.
    # _MAX_DURATION을 올리면 _MAX_SESSION_SPAN_SEC가 커지고 _MIN_IMPRESSION_HOURS도 따라
    # 커져야 하며, 그렇지 않으면 클릭 세션 후속 이벤트가 history_end를 넘을 수 있다.
    from autoresearch.action_log_generation.pipeline import (
        _MAX_SESSION_SPAN_SEC,
        _MIN_IMPRESSION_HOURS,
    )

    assert _MIN_IMPRESSION_HOURS >= 1
    assert _MIN_IMPRESSION_HOURS * 3600 >= _MAX_SESSION_SPAN_SEC


def test_per_user_daily_impression_cap_respected(tmp_path):
    users, videos = _fixture_users(1), build_fixture_video_records(40)
    result = generate_action_log_batch(
        _request(tmp_path, candidates_per_user=30, max_events_per_user_per_day=5, history_days=30),
        users, videos, RuleBasedActionLogGenerator(),
    )
    per_day: dict = {}
    for event in result.batch.events:
        if event.event_type != "impression":
            continue  # 상한은 impression 기준
        key = (event.user_id, event.event_timestamp.date())
        per_day[key] = per_day.get(key, 0) + 1
    assert max(per_day.values()) <= 5


def test_parquet_matches_events(tmp_path):
    users, videos = _fixture_users(3), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, RuleBasedActionLogGenerator())
    table = pq.read_table(tmp_path / "e.parquet")
    assert table.num_rows == result.summary["total_events"]
    assert set(table.column_names) >= {"event_id", "event_timestamp", "event_type", "watch_time_sec"}
    assert "clicked" not in table.column_names and "exposure_type" not in table.column_names
    warehouse = [json.loads(line) for line in (tmp_path / "e.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(warehouse) == result.summary["total_events"]
    assert set(warehouse[0]) == {
        "event_id", "event_timestamp", "user_id", "event_type",
        "video_id", "watch_time_sec", "rank", "source",
        "policy", "ctr_score", "is_exploration", "policy_version",
        "exposure_source", "slate_id",
    }
    assert all(row["slate_id"] is None for row in warehouse)


def test_event_and_spool_schemas_add_only_nullable_string_slate_id() -> None:
    # Given
    expected_event_names = [
        "event_id", "event_timestamp", "user_id", "event_type", "video_id",
        "watch_time_sec", "rank", "source", "policy", "ctr_score",
        "is_exploration", "policy_version", "exposure_source", "slate_id",
        "schema_version", "prompt_version", "llm_model", "generated_at",
    ]

    # When
    event_field = pipeline_module.EVENT_LOG_PARQUET_SCHEMA.field("slate_id")
    spool_field = pipeline_module._EVENT_SPOOL_SCHEMA.field("slate_id")

    # Then
    assert pipeline_module.EVENT_LOG_PARQUET_SCHEMA.names == expected_event_names
    assert pipeline_module._EVENT_SPOOL_SCHEMA.names == expected_event_names[:-1]
    assert event_field.type == pa.string() and event_field.nullable
    assert spool_field.type == pa.string() and spool_field.nullable
    assert "slate_id" in pipeline_module.OPTIONAL_ADDITIVE_COLUMNS
    assert ACTION_LOG_SCHEMA_VERSION == "action_log_schema_v1"


def test_batch_parquet_and_jsonl_preserve_explicit_slate_id(tmp_path) -> None:
    # Given
    slate_id = "slt_20260831_0cf0daf7c833035b191942e5"
    event = EventLog(
        event_id="evt_20260831_00000000",
        event_timestamp=datetime(2026, 8, 31, tzinfo=UTC),
        user_id="u1",
        event_type="impression",
        video_id="v1",
        slate_id=slate_id,
    )
    batch = EventLogBatch(
        schema_version=ACTION_LOG_SCHEMA_VERSION,
        prompt_version="action_log_ctr_v4",
        request=_request(tmp_path),
        events=[event],
        generated_at="2026-08-31T00:00:00+00:00",
    )
    parquet_path = tmp_path / "batch.parquet"
    jsonl_path = tmp_path / "batch.jsonl"

    # When
    pipeline_module.write_event_log_parquet(batch, "test-model", parquet_path)
    pipeline_module.write_event_log_warehouse_jsonl(batch, jsonl_path)

    # Then
    assert pq.read_table(parquet_path).column("slate_id").to_pylist() == [slate_id]
    assert json.loads(jsonl_path.read_text(encoding="utf-8"))["slate_id"] == slate_id


def test_streaming_spool_parquet_and_jsonl_preserve_explicit_slate_id(
    tmp_path,
) -> None:
    # Given
    request = _request(tmp_path)
    slate_id = "slt_20260831_0cf0daf7c833035b191942e5"
    event = EventLog(
        event_id="evt_20260831_00000000",
        event_timestamp=datetime(2026, 8, 31, tzinfo=UTC),
        user_id="u1",
        event_type="impression",
        video_id="v1",
        slate_id=slate_id,
    )

    # When
    with pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    ) as writer:
        writer.write_events([event])
        writer.finalize_success("2026-08-31T00:00:00+00:00")

    # Then
    assert pq.read_table(request.output_path).column("slate_id").to_pylist() == [
        slate_id
    ]
    warehouse = json.loads(
        Path(request.warehouse_output_path).read_text(encoding="utf-8")
    )
    assert warehouse["slate_id"] == slate_id


def test_context_free_expand_events_full_projection_is_characterized(tmp_path) -> None:
    # Given
    request = _request(
        tmp_path,
        candidates_per_user=2,
        history_days=1,
        max_events_per_user_per_day=2,
    )
    drafts = [
        ImpressionDraft(
            user_id="u1",
            video_id="v1",
            click_propensity=0.9,
            watch_fraction=0.4,
            would_like=True,
            duration_sec=100,
        ),
        ImpressionDraft(
            user_id="u1",
            video_id="v2",
            click_propensity=0.1,
            watch_fraction=0.2,
            would_like=False,
            duration_sec=100,
        ),
    ]
    common = {
        "user_id": "u1",
        "watch_time_sec": None,
        "rank": None,
        "source": "historical",
        "policy": None,
        "ctr_score": None,
        "is_exploration": None,
        "policy_version": None,
        "exposure_source": None,
    }
    expected = [
        {
            **common,
            "event_id": "evt_20260630_00000000",
            "event_timestamp": datetime(2026, 6, 30, 14, 43, 23, tzinfo=UTC),
            "event_type": "impression",
            "video_id": "v2",
        },
        {
            **common,
            "event_id": "evt_20260701_00000001",
            "event_timestamp": datetime(2026, 7, 1, 9, 53, 52, tzinfo=UTC),
            "event_type": "impression",
            "video_id": "v1",
        },
        {
            **common,
            "event_id": "evt_20260701_00000002",
            "event_timestamp": datetime(2026, 7, 1, 9, 54, 1, tzinfo=UTC),
            "event_type": "click",
            "video_id": "v1",
        },
        {
            **common,
            "event_id": "evt_20260701_00000003",
            "event_timestamp": datetime(2026, 7, 1, 9, 54, 5, tzinfo=UTC),
            "event_type": "view",
            "video_id": "v1",
            "watch_time_sec": 40,
        },
        {
            **common,
            "event_id": "evt_20260701_00000004",
            "event_timestamp": datetime(2026, 7, 1, 9, 54, 15, tzinfo=UTC),
            "event_type": "like",
            "video_id": "v1",
        },
    ]

    # When
    events = pipeline_module._expand_events(drafts, {0}, request)

    # Then
    assert [event.model_dump(exclude={"slate_id"}) for event in events] == expected
    assert all(event.slate_id is None for event in events)


def test_streaming_writer_finalizes_completion_time_and_bounded_row_groups(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    first = EventLog(
        event_id="evt_20260701_00000000",
        event_timestamp=_FIXED_END,
        user_id="u1",
        event_type="impression",
        video_id="v1",
        source="historical",
    )
    second = EventLog(
        event_id="evt_20260701_00000001",
        event_timestamp=_FIXED_END,
        user_id="u2",
        event_type="impression",
        video_id="v2",
        source="historical",
    )
    third = EventLog(
        event_id="evt_20260701_00000002",
        event_timestamp=_FIXED_END,
        user_id="u3",
        event_type="impression",
        video_id="v3",
        source="historical",
    )
    fourth = EventLog(
        event_id="evt_20260701_00000003",
        event_timestamp=_FIXED_END,
        user_id="u4",
        event_type="impression",
        video_id="v4",
        source="historical",
    )
    first_quarantine = QuarantineRecord(
        user_id="u2",
        error_type="invalid_json",
        error_message="broken",
    )
    second_quarantine = QuarantineRecord(
        user_id="u4",
        error_type="schema_fail",
        error_message="invalid row",
    )
    monkeypatch.setattr(pipeline_module, "_PARQUET_TARGET_ROW_GROUP_ROWS", 3)

    with pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    ) as writer:
        writer.write_events([first, second])
        writer.write_events([third, fourth])
        writer.write_quarantine([first_quarantine])
        writer.write_quarantine([second_quarantine])
        writer.finalize_success("2026-07-30T09:00:00+00:00")

    parquet = pq.ParquetFile(request.output_path)
    assert parquet.num_row_groups == 2
    assert parquet.metadata.row_group(0).num_rows == 3
    assert parquet.metadata.row_group(1).num_rows == 1
    assert parquet.read(columns=["event_id"]).column(0).to_pylist() == [
        first.event_id,
        second.event_id,
        third.event_id,
        fourth.event_id,
    ]
    assert set(
        parquet.read(columns=["generated_at"]).column(0).to_pylist()
    ) == {"2026-07-30T09:00:00+00:00"}
    warehouse = [
        json.loads(line)
        for line in Path(request.warehouse_output_path)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["event_id"] for row in warehouse] == [
        first.event_id,
        second.event_id,
        third.event_id,
        fourth.event_id,
    ]
    quarantined = [
        json.loads(line)
        for line in Path(request.quarantine_output_path)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["user_id"] for row in quarantined] == ["u2", "u4"]


def test_streaming_writer_quarantine_failure_commits_only_quarantine(tmp_path) -> None:
    request = _request(tmp_path)
    event = EventLog(
        event_id="evt_20260701_00000000",
        event_timestamp=_FIXED_END,
        user_id="u1",
        event_type="impression",
        video_id="v1",
        source="historical",
    )
    quarantine = QuarantineRecord(
        user_id="u1",
        error_type="invalid_json",
        error_message="broken",
    )

    with pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    ) as writer:
        writer.write_events([event])
        writer.write_quarantine([quarantine])
        writer.finalize_quarantine_failure()

    assert not Path(request.output_path).exists()
    assert not Path(request.warehouse_output_path).exists()
    assert Path(request.quarantine_output_path).read_text(encoding="utf-8").count("\n") == 1


def test_streaming_writer_exception_removes_spools(tmp_path) -> None:
    request = _request(tmp_path)
    event = EventLog(
        event_id="evt_20260701_00000000",
        event_timestamp=_FIXED_END,
        user_id="u1",
        event_type="impression",
        video_id="v1",
        source="historical",
    )
    quarantine = QuarantineRecord(
        user_id="u1",
        error_type="invalid_json",
        error_message="broken",
    )

    with pytest.raises(RuntimeError, match="abort run"):
        with pipeline_module._StreamingActionLogWriter(
            request=request,
            model_name="test-model",
        ) as writer:
            writer.write_events([event])
            writer.write_quarantine([quarantine])
            raise RuntimeError("abort run")

    assert not Path(request.output_path).exists()
    assert not Path(request.warehouse_output_path).exists()
    assert not Path(request.quarantine_output_path).exists()
    assert list(tmp_path.iterdir()) == []


def test_streaming_writer_keeps_new_outputs_after_post_commit_stat_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """세 output replace가 끝난 뒤의 stat 실패는 committed publish를 되돌리지 않는다."""

    request = _request(tmp_path)
    final_paths = {
        Path(request.output_path),
        Path(request.warehouse_output_path),
        Path(request.quarantine_output_path),
    }
    for path in final_paths:
        path.write_bytes(b"previous output")
    event = EventLog(
        event_id="evt_20260701_00000000",
        event_timestamp=_FIXED_END,
        user_id="u1",
        event_type="impression",
        video_id="v1",
        source="historical",
    )
    original_replace = Path.replace
    original_exists = Path.exists
    committed_replaces = 0
    post_commit_stat_attempted = False

    def record_final_replace(source: Path, target: str | Path) -> Path:
        nonlocal committed_replaces
        result = original_replace(source, target)
        if Path(target) in final_paths:
            committed_replaces += 1
        return result

    def fail_post_commit_final_stat(path: Path) -> bool:
        nonlocal post_commit_stat_attempted
        if committed_replaces == 3 and path in final_paths:
            post_commit_stat_attempted = True
            raise OSError("post-commit final stat failed")
        return original_exists(path)

    monkeypatch.setattr(Path, "replace", record_final_replace)
    monkeypatch.setattr(Path, "exists", fail_post_commit_final_stat)

    with pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    ) as writer:
        writer.write_events([event])
        writer.finalize_success("2026-07-30T09:00:00+00:00")
        assert writer._committed is True

    assert committed_replaces == 3
    assert post_commit_stat_attempted is False
    assert pq.ParquetFile(request.output_path).read(columns=["event_id"]).column(
        0
    ).to_pylist() == [event.event_id]
    assert [
        json.loads(line)["event_id"]
        for line in Path(request.warehouse_output_path).read_text(encoding="utf-8").splitlines()
    ] == [event.event_id]
    assert Path(request.quarantine_output_path).read_text(encoding="utf-8") == ""


def test_streaming_writer_keeps_quarantine_commit_when_post_commit_cleanup_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """q-only commit 뒤 cleanup과 warning sink가 실패해도 원래 quarantine 오류가 우선이다."""

    request = _request(tmp_path)
    quarantine = QuarantineRecord(
        user_id="u1",
        error_type="invalid_json",
        error_message="broken",
    )
    writer = pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    )
    original_unlink = Path.unlink

    with pytest.raises(ActionLogGenerationError, match="quarantine threshold"):
        with writer:
            cleanup_paths = {
                writer._event_spool_path,
                writer._parquet_spool_path,
                writer._warehouse_spool_path,
            }

            def fail_post_commit_cleanup(
                path: Path,
                *,
                missing_ok: bool = False,
            ) -> None:
                if path in cleanup_paths:
                    raise OSError("post-commit spool cleanup failed")
                original_unlink(path, missing_ok=missing_ok)

            def fail_cleanup_warning(*_args: object, **_kwargs: object) -> None:
                raise RuntimeError("cleanup warning sink failed")

            monkeypatch.setattr(Path, "unlink", fail_post_commit_cleanup)
            monkeypatch.setattr(pipeline_module.logger, "warning", fail_cleanup_warning)
            writer.write_quarantine([quarantine])
            writer.finalize_quarantine_failure()
            assert writer._committed is True
            raise ActionLogGenerationError("quarantine threshold")

    assert Path(request.quarantine_output_path).read_text(encoding="utf-8").count("\n") == 1
    assert not Path(request.output_path).exists()
    assert not Path(request.warehouse_output_path).exists()


def test_streaming_writer_rolls_back_success_outputs_when_commit_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    event = EventLog(
        event_id="evt_20260701_00000000",
        event_timestamp=_FIXED_END,
        user_id="u1",
        event_type="impression",
        video_id="v1",
        source="historical",
    )
    original_replace = Path.replace

    def fail_warehouse_publish(source: Path, target: str | Path) -> Path:
        if Path(target) == Path(request.warehouse_output_path):
            raise OSError("warehouse publish failed")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_warehouse_publish)

    with pytest.raises(OSError, match="warehouse publish failed"):
        with pipeline_module._StreamingActionLogWriter(
            request=request,
            model_name="test-model",
        ) as writer:
            writer.write_events([event])
            writer.finalize_success("2026-07-30T09:00:00+00:00")

    assert not Path(request.output_path).exists()
    assert not Path(request.warehouse_output_path).exists()
    assert not Path(request.quarantine_output_path).exists()
    assert list(tmp_path.iterdir()) == []


def test_streaming_writer_restores_existing_outputs_when_commit_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    previous_outputs = {
        Path(request.output_path): b"previous parquet",
        Path(request.warehouse_output_path): b'{"previous": "warehouse"}\n',
        Path(request.quarantine_output_path): b'{"previous": "quarantine"}\n',
    }
    for path, contents in previous_outputs.items():
        path.write_bytes(contents)

    event = EventLog(
        event_id="evt_20260701_00000000",
        event_timestamp=_FIXED_END,
        user_id="u1",
        event_type="impression",
        video_id="v1",
        source="historical",
    )
    original_replace = Path.replace
    warehouse_publish_failed = False

    def fail_first_warehouse_publish(source: Path, target: str | Path) -> Path:
        nonlocal warehouse_publish_failed
        if (
            Path(target) == Path(request.warehouse_output_path)
            and not warehouse_publish_failed
        ):
            warehouse_publish_failed = True
            raise OSError("warehouse publish failed")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_first_warehouse_publish)

    with pytest.raises(OSError, match="warehouse publish failed"):
        with pipeline_module._StreamingActionLogWriter(
            request=request,
            model_name="test-model",
        ) as writer:
            writer.write_events([event])
            writer.finalize_success("2026-07-30T09:00:00+00:00")

    assert {
        path: path.read_bytes()
        for path in previous_outputs
    } == previous_outputs
    assert {path.name for path in tmp_path.iterdir()} == {
        path.name for path in previous_outputs
    }


def test_streaming_writer_retries_unrestored_backup_after_publish_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    previous_outputs = {
        Path(request.output_path): b"previous parquet",
        Path(request.warehouse_output_path): b'{"previous": "warehouse"}\n',
        Path(request.quarantine_output_path): b'{"previous": "quarantine"}\n',
    }
    for path, contents in previous_outputs.items():
        path.write_bytes(contents)
    event = EventLog(
        event_id="evt_20260701_00000000",
        event_timestamp=_FIXED_END,
        user_id="u1",
        event_type="impression",
        video_id="v1",
        source="historical",
    )
    original_replace = Path.replace
    warehouse_target_attempts = 0
    writer = pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    )

    def fail_publish_then_first_restore(source: Path, target: str | Path) -> Path:
        nonlocal warehouse_target_attempts
        if Path(target) == Path(request.warehouse_output_path):
            warehouse_target_attempts += 1
            if warehouse_target_attempts == 1:
                raise OSError("warehouse publish failed")
            if warehouse_target_attempts == 2:
                raise OSError("warehouse restore failed")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_publish_then_first_restore)

    with pytest.raises(OSError, match="warehouse publish failed") as error:
        with writer:
            writer.write_events([event])
            writer.finalize_success("2026-07-30T09:00:00+00:00")

    assert isinstance(error.value.__cause__, ExceptionGroup)
    assert warehouse_target_attempts == 3
    assert {path: path.read_bytes() for path in previous_outputs} == previous_outputs
    assert writer._unrestored_backup_paths == []
    assert {path.name for path in tmp_path.iterdir()} == {
        path.name for path in previous_outputs
    }


def test_streaming_writer_continues_rollback_when_error_logging_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rollback diagnostic sink failure도 원래 publish 오류와 기존 출력 복구를 가리지 않는다."""

    request = _request(tmp_path)
    previous_outputs = {
        Path(request.output_path): b"previous parquet",
        Path(request.warehouse_output_path): b'{"previous": "warehouse"}\n',
        Path(request.quarantine_output_path): b'{"previous": "quarantine"}\n',
    }
    for path, contents in previous_outputs.items():
        path.write_bytes(contents)
    event = EventLog(
        event_id="evt_20260701_00000000",
        event_timestamp=_FIXED_END,
        user_id="u1",
        event_type="impression",
        video_id="v1",
        source="historical",
    )
    original_replace = Path.replace
    original_unlink = Path.unlink
    parquet_final = Path(request.output_path)
    warehouse_final = Path(request.warehouse_output_path)
    final_paths = set(previous_outputs)
    restored_paths: list[Path] = []
    warehouse_publish_failed = False
    parquet_rollback_unlink_failed = False
    writer_error_log_calls = 0
    writer = pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    )

    with pytest.raises(OSError, match="warehouse publish failed") as error:
        with writer:
            initial_spool_paths = set(writer._spool_paths())

            def fail_warehouse_publish(source: Path, target: str | Path) -> Path:
                nonlocal warehouse_publish_failed
                source_path = Path(source)
                target_path = Path(target)
                if (
                    source_path == writer._warehouse_spool_path
                    and target_path == warehouse_final
                    and not warehouse_publish_failed
                ):
                    warehouse_publish_failed = True
                    raise OSError("warehouse publish failed")
                result = original_replace(source, target)
                if source_path not in initial_spool_paths and target_path in final_paths:
                    restored_paths.append(target_path)
                return result

            def fail_published_parquet_rollback_unlink(
                path: Path,
                *,
                missing_ok: bool = False,
            ) -> None:
                nonlocal parquet_rollback_unlink_failed
                if path == parquet_final and not parquet_rollback_unlink_failed:
                    parquet_rollback_unlink_failed = True
                    raise OSError("published parquet rollback unlink failed")
                original_unlink(path, missing_ok=missing_ok)

            def fail_writer_error_log(*_args: object, **_kwargs: object) -> None:
                nonlocal writer_error_log_calls
                writer_error_log_calls += 1
                raise RuntimeError("rollback error sink failed")

            monkeypatch.setattr(Path, "replace", fail_warehouse_publish)
            monkeypatch.setattr(Path, "unlink", fail_published_parquet_rollback_unlink)
            monkeypatch.setattr(pipeline_module.logger, "error", fail_writer_error_log)
            writer.write_events([event])
            writer.finalize_success("2026-07-30T09:00:00+00:00")

    assert isinstance(error.value.__cause__, ExceptionGroup)
    assert any(
        str(rollback_error) == "published parquet rollback unlink failed"
        for rollback_error in error.value.__cause__.exceptions
    )
    assert warehouse_publish_failed is True
    assert parquet_rollback_unlink_failed is True
    assert writer_error_log_calls == 1
    assert restored_paths == [
        Path(request.quarantine_output_path),
        Path(request.warehouse_output_path),
        Path(request.output_path),
    ]
    assert {path: path.read_bytes() for path in previous_outputs} == previous_outputs
    assert writer._commit_backup_paths == []
    assert writer._unrestored_backup_paths == []
    assert {path.name for path in tmp_path.iterdir()} == {
        path.name for path in previous_outputs
    }


def test_streaming_writer_continues_unrestored_backup_retry_when_error_logging_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """한 backup 복구와 error sink가 실패해도 다음 backup 복구를 계속 시도한다."""

    request = _request(tmp_path)
    writer = pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    )
    first_backup = tmp_path / ".e.parquet.first.spool"
    second_backup = tmp_path / ".e.jsonl.second.spool"
    first_final = Path(request.output_path)
    second_final = Path(request.warehouse_output_path)
    first_backup.write_bytes(b"first previous output")
    second_backup.write_bytes(b"second previous output")
    writer._unrestored_backup_paths = [
        (first_backup, first_final),
        (second_backup, second_final),
    ]
    original_replace = Path.replace
    restore_attempts: list[tuple[Path, Path]] = []
    writer_error_log_calls = 0

    def fail_first_restore(source: Path, target: str | Path) -> Path:
        source_path = Path(source)
        target_path = Path(target)
        restore_attempts.append((source_path, target_path))
        if source_path == first_backup:
            raise OSError("first backup restore failed")
        return original_replace(source, target)

    def fail_writer_error_log(*_args: object, **_kwargs: object) -> None:
        nonlocal writer_error_log_calls
        writer_error_log_calls += 1
        raise RuntimeError("restore error sink failed")

    monkeypatch.setattr(Path, "replace", fail_first_restore)
    monkeypatch.setattr(pipeline_module.logger, "error", fail_writer_error_log)

    writer._restore_unrestored_backups()

    assert restore_attempts == [
        (first_backup, first_final),
        (second_backup, second_final),
    ]
    assert writer_error_log_calls == 1
    assert first_backup.read_bytes() == b"first previous output"
    assert not first_final.exists()
    assert second_final.read_bytes() == b"second previous output"
    assert not second_backup.exists()
    assert writer._unrestored_backup_paths == [(first_backup, first_final)]


def test_streaming_writer_retries_backup_after_rollback_stat_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    previous_outputs = {
        Path(request.output_path): b"previous parquet",
        Path(request.warehouse_output_path): b'{"previous": "warehouse"}\n',
        Path(request.quarantine_output_path): b'{"previous": "quarantine"}\n',
    }
    for path, contents in previous_outputs.items():
        path.write_bytes(contents)
    event = EventLog(
        event_id="evt_20260701_00000000",
        event_timestamp=_FIXED_END,
        user_id="u1",
        event_type="impression",
        video_id="v1",
        source="historical",
    )
    original_replace = Path.replace
    original_exists = Path.exists
    warehouse_final = Path(request.warehouse_output_path)
    warehouse_publish_failed = False
    rollback_backup_stat_failed = False
    writer = pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    )

    def fail_first_warehouse_publish(source: Path, target: str | Path) -> Path:
        nonlocal warehouse_publish_failed
        if Path(target) == warehouse_final and not warehouse_publish_failed:
            warehouse_publish_failed = True
            raise OSError("warehouse publish failed")
        return original_replace(source, target)

    def fail_first_warehouse_backup_stat(path: Path) -> bool:
        nonlocal rollback_backup_stat_failed
        if (
            path.name.startswith(f".{warehouse_final.name}.")
            and not rollback_backup_stat_failed
        ):
            rollback_backup_stat_failed = True
            raise OSError("warehouse backup stat failed")
        return original_exists(path)

    monkeypatch.setattr(Path, "replace", fail_first_warehouse_publish)
    monkeypatch.setattr(Path, "exists", fail_first_warehouse_backup_stat)

    with pytest.raises(OSError, match="warehouse publish failed") as error:
        with writer:
            writer.write_events([event])
            writer.finalize_success("2026-07-30T09:00:00+00:00")

    assert isinstance(error.value.__cause__, ExceptionGroup)
    assert any(
        str(rollback_error) == "warehouse backup stat failed"
        for rollback_error in error.value.__cause__.exceptions
    )
    assert rollback_backup_stat_failed is True
    assert {path: path.read_bytes() for path in previous_outputs} == previous_outputs
    assert writer._commit_backup_paths == []
    assert writer._unrestored_backup_paths == []
    assert {path.name for path in tmp_path.iterdir()} == {
        path.name for path in previous_outputs
    }


def test_streaming_writer_keeps_backup_when_exit_retry_stat_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = _request(tmp_path)
    previous_outputs = {
        Path(request.output_path): b"previous parquet",
        Path(request.warehouse_output_path): b'{"previous": "warehouse"}\n',
        Path(request.quarantine_output_path): b'{"previous": "quarantine"}\n',
    }
    for path, contents in previous_outputs.items():
        path.write_bytes(contents)
    event = EventLog(
        event_id="evt_20260701_00000000",
        event_timestamp=_FIXED_END,
        user_id="u1",
        event_type="impression",
        video_id="v1",
        source="historical",
    )
    original_replace = Path.replace
    original_exists = Path.exists
    warehouse_final = Path(request.warehouse_output_path)
    warehouse_target_attempts = 0
    exit_backup_stat_failed = False
    writer = pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    )

    def fail_publish_then_warehouse_restore(
        source: Path,
        target: str | Path,
    ) -> Path:
        nonlocal warehouse_target_attempts
        if Path(target) == warehouse_final:
            warehouse_target_attempts += 1
            if warehouse_target_attempts == 1:
                raise OSError("warehouse publish failed")
            if warehouse_target_attempts == 2:
                raise OSError("warehouse restore failed")
        return original_replace(source, target)

    def fail_exit_warehouse_backup_stat(path: Path) -> bool:
        nonlocal exit_backup_stat_failed
        if (
            path.name.startswith(f".{warehouse_final.name}.")
            and warehouse_target_attempts == 2
            and not exit_backup_stat_failed
        ):
            exit_backup_stat_failed = True
            raise OSError("warehouse exit backup stat failed")
        return original_exists(path)

    monkeypatch.setattr(Path, "replace", fail_publish_then_warehouse_restore)
    monkeypatch.setattr(Path, "exists", fail_exit_warehouse_backup_stat)

    with caplog.at_level(logging.ERROR, logger=pipeline_module.__name__):
        with pytest.raises(OSError, match="warehouse publish failed") as error:
            with writer:
                writer.write_events([event])
                writer.finalize_success("2026-07-30T09:00:00+00:00")

    assert isinstance(error.value.__cause__, ExceptionGroup)
    assert any(
        str(rollback_error) == "warehouse restore failed"
        for rollback_error in error.value.__cause__.exceptions
    )
    assert exit_backup_stat_failed is True
    assert warehouse_target_attempts == 2
    assert Path(request.output_path).read_bytes() == previous_outputs[
        Path(request.output_path)
    ]
    assert Path(request.quarantine_output_path).read_bytes() == previous_outputs[
        Path(request.quarantine_output_path)
    ]
    assert not warehouse_final.exists()
    assert len(writer._unrestored_backup_paths) == 1
    backup_path, final_path = writer._unrestored_backup_paths[0]
    assert final_path == warehouse_final
    assert backup_path.exists()
    assert backup_path.read_bytes() == previous_outputs[warehouse_final]
    assert backup_path not in writer._spool_paths()
    assert writer._warehouse_spool_path is not None
    assert not writer._warehouse_spool_path.exists()
    assert "Unable to restore action log backup after failed publish" in caplog.messages


def test_streaming_writer_preserves_publish_error_and_unrestored_backup_when_cleanup_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = _request(tmp_path)
    previous_outputs = {
        Path(request.output_path): b"previous parquet",
        Path(request.warehouse_output_path): b'{"previous": "warehouse"}\n',
        Path(request.quarantine_output_path): b'{"previous": "quarantine"}\n',
    }
    for path, contents in previous_outputs.items():
        path.write_bytes(contents)
    event = EventLog(
        event_id="evt_20260701_00000000",
        event_timestamp=_FIXED_END,
        user_id="u1",
        event_type="impression",
        video_id="v1",
        source="historical",
    )
    original_replace = Path.replace
    original_unlink = Path.unlink
    warehouse_target_attempts = 0
    writer = pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    )

    def fail_publish_and_all_warehouse_restores(
        source: Path,
        target: str | Path,
    ) -> Path:
        nonlocal warehouse_target_attempts
        if Path(target) == Path(request.warehouse_output_path):
            warehouse_target_attempts += 1
            if warehouse_target_attempts == 1:
                raise OSError("warehouse publish failed")
            raise OSError("warehouse restore failed")
        return original_replace(source, target)

    def fail_warehouse_spool_cleanup(path: Path, *, missing_ok: bool = False) -> None:
        if path == writer._warehouse_spool_path:
            raise OSError("warehouse spool cleanup failed")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "replace", fail_publish_and_all_warehouse_restores)
    monkeypatch.setattr(Path, "unlink", fail_warehouse_spool_cleanup)

    with caplog.at_level(logging.WARNING, logger=pipeline_module.__name__):
        with pytest.raises(OSError, match="warehouse publish failed") as error:
            with writer:
                writer.write_events([event])
                writer.finalize_success("2026-07-30T09:00:00+00:00")

    assert isinstance(error.value.__cause__, ExceptionGroup)
    assert warehouse_target_attempts == 3
    assert Path(request.output_path).read_bytes() == previous_outputs[
        Path(request.output_path)
    ]
    assert Path(request.quarantine_output_path).read_bytes() == previous_outputs[
        Path(request.quarantine_output_path)
    ]
    assert not Path(request.warehouse_output_path).exists()
    assert writer._unrestored_backup_paths
    backup_path, final_path = writer._unrestored_backup_paths[0]
    assert final_path == Path(request.warehouse_output_path)
    assert backup_path.exists()
    assert backup_path.read_bytes() == previous_outputs[final_path]
    assert backup_path not in writer._spool_paths()
    assert writer._warehouse_spool_path is not None
    assert writer._warehouse_spool_path.exists()
    assert "Unable to remove committed action log spool" in caplog.messages


def test_streaming_writer_cleans_tracked_backup_after_unlink_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = _request(tmp_path)
    for path in (
        Path(request.output_path),
        Path(request.warehouse_output_path),
        Path(request.quarantine_output_path),
    ):
        path.write_text("previous output\n", encoding="utf-8")
    event = EventLog(
        event_id="evt_20260701_00000000",
        event_timestamp=_FIXED_END,
        user_id="u1",
        event_type="impression",
        video_id="v1",
        source="historical",
    )
    original_unlink = Path.unlink
    backup_unlink_failed = False

    with pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    ) as writer:
        initial_spool_paths = set(writer._spool_paths())

        def fail_first_backup_unlink(path: Path, *, missing_ok: bool = False) -> None:
            nonlocal backup_unlink_failed
            if path not in initial_spool_paths and not backup_unlink_failed:
                backup_unlink_failed = True
                raise PermissionError("backup cleanup failed")
            original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", fail_first_backup_unlink)
        with caplog.at_level(logging.WARNING, logger=pipeline_module.__name__):
            writer.write_events([event])
            writer.finalize_success("2026-07-30T09:00:00+00:00")

    assert "Unable to remove committed action log backup spool" in caplog.messages
    assert backup_unlink_failed is True
    assert {path.name for path in tmp_path.iterdir()} == {
        "e.parquet",
        "e.jsonl",
        "q.jsonl",
    }


def test_streaming_writer_keeps_success_when_backup_cleanup_persists(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = _request(tmp_path)
    for path in (
        Path(request.output_path),
        Path(request.warehouse_output_path),
        Path(request.quarantine_output_path),
    ):
        path.write_text("previous output\n", encoding="utf-8")
    event = EventLog(
        event_id="evt_20260701_00000000",
        event_timestamp=_FIXED_END,
        user_id="u1",
        event_type="impression",
        video_id="v1",
        source="historical",
    )
    original_unlink = Path.unlink
    writer = pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    )

    with writer:
        initial_spool_paths = set(writer._spool_paths())

        def fail_backup_unlink(path: Path, *, missing_ok: bool = False) -> None:
            if path not in initial_spool_paths:
                raise PermissionError("backup cleanup keeps failing")
            original_unlink(path, missing_ok=missing_ok)

        with monkeypatch.context() as cleanup_monkeypatch:
            cleanup_monkeypatch.setattr(Path, "unlink", fail_backup_unlink)
            with caplog.at_level(logging.WARNING, logger=pipeline_module.__name__):
                writer.write_events([event])
                writer.finalize_success("2026-07-30T09:00:00+00:00")
            assert writer._committed is True
            assert writer._commit_backup_paths

    assert "Unable to remove committed action log backup spool" in caplog.messages
    assert writer._commit_backup_paths == []
    assert {path.name for path in tmp_path.iterdir()} == {
        "e.parquet",
        "e.jsonl",
        "q.jsonl",
    }


def test_streaming_writer_removes_spools_after_partial_open_failure(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_opening_stream(*_args: object, **_kwargs: object) -> Never:
        raise OSError("ipc stream open failed")

    monkeypatch.setattr(pipeline_module.pa.ipc, "new_stream", fail_opening_stream)

    with pytest.raises(OSError, match="ipc stream open failed"):
        pipeline_module._StreamingActionLogWriter(
            request=_request(tmp_path),
            model_name="test-model",
        ).__enter__()

    assert list(tmp_path.iterdir()) == []


def test_streaming_writer_creates_schema_only_parquet_without_events(tmp_path) -> None:
    request = _request(tmp_path)

    with pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    ) as writer:
        writer.finalize_success("2026-07-30T09:00:00+00:00")

    parquet = pq.ParquetFile(request.output_path)
    assert parquet.metadata.num_rows == 0
    assert parquet.num_row_groups == 0
    assert parquet.schema_arrow == pipeline_module.EVENT_LOG_PARQUET_SCHEMA
    assert Path(request.warehouse_output_path).read_text(encoding="utf-8") == ""
    assert Path(request.quarantine_output_path).read_text(encoding="utf-8") == ""


def test_user_isolation_quarantines_bad_row(tmp_path):
    class _OneBadUserGen(RuleBasedActionLogGenerator):
        def generate(self, virtual_user, videos):
            if virtual_user["user_id"] == "vu_0001":
                return "{not valid json"
            return super().generate(virtual_user, videos)

    users, videos = _fixture_users(6), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, _OneBadUserGen())
    assert result.summary["quarantined_users"] == 1
    assert result.summary["invalid_json"] == 1
    q_lines = (tmp_path / "q.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(q_lines[0])["error_type"] == "invalid_json"


@pytest.mark.parametrize(
    ("first_response", "expected_error_type"),
    [
        ("{not valid json", "invalid_json"),
        (json.dumps({"j": [[0, 0.1, 0.2]]}), "schema_fail"),
    ],
)
def test_openrouter_style_generator_repairs_response_validation_once(
    tmp_path,
    first_response,
    expected_error_type,
):
    class _RepairingGenerator:
        model_name = "repairing-generator"

        def __init__(self):
            self.generate_calls = 0
            self.retry_calls = []

        def generate(self, virtual_user, videos):
            self.generate_calls += 1
            return first_response

        def generate_schema_retry(self, virtual_user, videos, *, error_type):
            self.retry_calls.append(error_type)
            return RuleBasedActionLogGenerator().generate(virtual_user, videos)

    generator = _RepairingGenerator()
    result = generate_action_log_batch(
        _request(tmp_path, candidates_per_user=4),
        _fixture_users(1),
        build_fixture_video_records(4),
        generator,
    )

    assert generator.generate_calls == 1
    assert generator.retry_calls == [expected_error_type]
    assert result.summary["quarantined_users"] == 0
    assert result.summary["impressions"] == 4


def test_schema_retry_stays_in_worker_and_does_not_block_next_work_submission(
    tmp_path,
):
    retry_started = Event()
    third_work_started = Event()

    class _CoordinatedGenerator:
        model_name = "coordinated-generator"

        def generate(self, virtual_user, videos):
            user_id = virtual_user["user_id"]
            if user_id == "vu_0000":
                return "{first invalid"
            if user_id == "vu_0001":
                assert retry_started.wait(timeout=2.0)
            if user_id == "vu_0002":
                third_work_started.set()
            return RuleBasedActionLogGenerator().generate(virtual_user, videos)

        def generate_schema_retry(self, virtual_user, videos, *, error_type):
            assert virtual_user["user_id"] == "vu_0000"
            assert error_type == "invalid_json"
            retry_started.set()
            assert third_work_started.wait(timeout=2.0)
            return RuleBasedActionLogGenerator().generate(virtual_user, videos)

    result = generate_action_log_drafts(
        _request(
            tmp_path,
            candidates_per_user=1,
            chunk_size=0,
            max_concurrency=2,
        ),
        _fixture_users(3),
        build_fixture_video_records(3),
        _CoordinatedGenerator(),
    )

    assert third_work_started.is_set()
    assert len(result.drafts) == 3
    assert result.quarantine == []


def test_schema_retry_timings_separate_request_and_parse(monkeypatch):
    class _RepairingGenerator:
        model_name = "timed-repairing-generator"

        def generate(self, virtual_user, videos):
            return "{first invalid"

        def generate_schema_retry(self, virtual_user, videos, *, error_type):
            return RuleBasedActionLogGenerator().generate(virtual_user, videos)

    clock = iter(
        [
            0.000,  # worker start
            0.000,  # initial request start
            0.010,  # initial request end
            0.010,  # initial parse start
            0.012,  # initial parse end
            0.012,  # retry request start
            0.032,  # retry request end
            0.032,  # retry parse start
            0.037,  # retry parse end
        ]
    )
    monkeypatch.setattr(pipeline_module, "monotonic", lambda: next(clock))
    virtual_user = _fixture_users(1)[0]
    item = pipeline_module._ActionLogWorkItem(
        work_id="work_00000000",
        user_id=virtual_user["user_id"],
        virtual_user=virtual_user,
        candidates=build_fixture_video_records(1),
    )

    result = pipeline_module._generate_action_log_work(
        _RepairingGenerator(),
        item,
        work_sequence=0,
        submitted_at=0.0,
        shard_index=None,
        detailed_telemetry=True,
    )

    assert result.drafts is not None
    assert result.error is None
    assert result.request_elapsed_ms == pytest.approx(30.0)
    assert result.parse_elapsed_ms == pytest.approx(7.0)


def test_schema_retry_api_error_preserves_error_and_initial_raw_response(tmp_path):
    class _RetryApiErrorGenerator:
        model_name = "retry-api-error-generator"

        def generate(self, virtual_user, videos):
            return "{first invalid"

        def generate_schema_retry(self, virtual_user, videos, *, error_type):
            raise RuntimeError("retry transport unavailable")

    result = generate_action_log_drafts(
        _request(
            tmp_path,
            candidates_per_user=1,
            max_quarantine_ratio=1.0,
        ),
        _fixture_users(1),
        build_fixture_video_records(1),
        _RetryApiErrorGenerator(),
    )

    assert result.summary["api_error"] == 1
    assert result.summary["invalid_json"] == 0
    assert result.quarantine[0].raw_llm_response == "{first invalid"
    assert result.quarantine[0].error_message == "retry transport unavailable"


def test_unexpected_worker_error_is_not_disguised_as_api_error(
    tmp_path,
    monkeypatch,
):
    def _raise_internal_error(virtual_user, candidates, raw_text):
        raise RuntimeError("unexpected parser bug")

    monkeypatch.setattr(
        pipeline_module,
        "_try_build_user_drafts",
        _raise_internal_error,
    )

    with pytest.raises(RuntimeError, match="unexpected parser bug"):
        generate_action_log_drafts(
            _request(tmp_path, candidates_per_user=1),
            _fixture_users(1),
            build_fixture_video_records(1),
            RuleBasedActionLogGenerator(),
        )



def test_schema_retry_final_failure_is_quarantined(tmp_path):
    class _AlwaysInvalidGenerator:
        model_name = "always-invalid-generator"

        def __init__(self):
            self.retry_calls = 0

        def generate(self, virtual_user, videos):
            return "{first invalid"

        def generate_schema_retry(self, virtual_user, videos, *, error_type):
            self.retry_calls += 1
            return "{retry invalid"

    generator = _AlwaysInvalidGenerator()
    result = generate_action_log_batch(
        _request(
            tmp_path,
            candidates_per_user=4,
            max_quarantine_ratio=1.0,
        ),
        _fixture_users(1),
        build_fixture_video_records(4),
        generator,
    )

    assert generator.retry_calls == 1
    assert result.summary["invalid_json"] == 1
    quarantine = json.loads(
        (tmp_path / "q.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert quarantine["raw_llm_response"] == "{retry invalid"


def test_streaming_single_schema_retry_final_failure_preserves_quarantine(
    tmp_path,
) -> None:
    class _AlwaysInvalidGenerator:
        model_name = "always-invalid-generator"

        def __init__(self) -> None:
            self.retry_calls = 0

        def generate(self, virtual_user: dict, videos: list[dict]) -> str:
            return "{first invalid"

        def generate_schema_retry(
            self,
            virtual_user: dict,
            videos: list[dict],
            *,
            error_type: str,
        ) -> str:
            self.retry_calls += 1
            return "{retry invalid"

    generator = _AlwaysInvalidGenerator()
    videos = build_fixture_video_records(4)

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        return list(videos)

    result = pipeline_module.generate_action_log_single(
        _request(
            tmp_path,
            candidates_per_user=4,
            max_quarantine_ratio=1.0,
        ),
        _fixture_users(1),
        videos,
        generator,
        candidate_provider=provider,
    )

    assert generator.retry_calls == 1
    assert result.summary["quarantined_users"] == 1
    assert result.summary["invalid_json"] == 1
    quarantine = json.loads(
        (tmp_path / "q.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert quarantine["error_type"] == "invalid_json"
    assert quarantine["raw_llm_response"] == "{retry invalid"


def test_streaming_single_propagates_unexpected_parser_exception(
    tmp_path,
    monkeypatch,
) -> None:
    def _raise_internal_error(
        virtual_user: dict[str, object],
        candidates: list[dict[str, object]],
        raw_text: str,
    ) -> Never:
        raise RuntimeError("unexpected parser bug")

    monkeypatch.setattr(
        pipeline_module,
        "_try_build_user_drafts",
        _raise_internal_error,
    )
    videos = build_fixture_video_records(1)

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        return list(videos)

    with pytest.raises(RuntimeError, match="unexpected parser bug"):
        pipeline_module.generate_action_log_single(
            _request(tmp_path, candidates_per_user=1),
            _fixture_users(1),
            videos,
            RuleBasedActionLogGenerator(),
            candidate_provider=provider,
        )


def test_total_failure_raises_and_writes_quarantine(tmp_path):
    class _AllBadGen(RuleBasedActionLogGenerator):
        def generate(self, virtual_user, videos):
            return "{not valid json"

    users, videos = _fixture_users(6), build_fixture_video_records(40)
    with pytest.raises(ActionLogGenerationError):
        generate_action_log_batch(_request(tmp_path), users, videos, _AllBadGen())
    assert len((tmp_path / "q.jsonl").read_text(encoding="utf-8").splitlines()) == 6
    assert not (tmp_path / "e.parquet").exists()


def test_eventlog_watch_time_only_for_view():
    now = datetime(2026, 7, 1, tzinfo=UTC)
    # view는 watch_time_sec 필수(>=0)
    ev = EventLog(event_id="e", event_timestamp=now, user_id="u",
                  event_type="view", video_id="v", watch_time_sec=42)
    assert ev.watch_time_sec == 42 and ev.rank is None and ev.source == "historical"
    # impression/click/like는 watch_time_sec=None (기본값)
    for et in ("impression", "click", "like"):
        assert EventLog(event_id="e", event_timestamp=now, user_id="u",
                        event_type=et, video_id="v").watch_time_sec is None
    # view인데 watch_time_sec 누락 -> 거부
    with pytest.raises(ValidationError):
        EventLog(event_id="e", event_timestamp=now, user_id="u",
                 event_type="view", video_id="v")
    # 비-view인데 watch_time_sec 채움 -> 거부
    with pytest.raises(ValidationError):
        EventLog(event_id="e", event_timestamp=now, user_id="u",
                 event_type="impression", video_id="v", watch_time_sec=5)


def test_batch_summary_ctr_from_impression_and_click_rows():
    from autoresearch.action_log_generation.schema import EventLogBatch
    now = datetime(2026, 7, 1, tzinfo=UTC)

    def _ev(et, wt=None):
        return EventLog(event_id="e", event_timestamp=now, user_id="u",
                        event_type=et, video_id="v", watch_time_sec=wt)

    events = [_ev("impression"), _ev("impression"), _ev("click"), _ev("view", 10), _ev("like")]
    batch = EventLogBatch(
        schema_version="s", prompt_version="p",
        request=EventGenerationRequest(click_threshold=0.55), events=events,
    )
    s = batch.summary
    assert s["impressions"] == 2 and s["clicks"] == 1
    assert s["total_events"] == 5
    assert s["ctr"] == round(1 / 2, 4)


def test_video_source_helpers():
    assert _parse_tags("LCK, 롤, None") == ["LCK", "롤"]
    assert _parse_tags("None") == []
    assert _parse_tags(None) == []
    assert _parse_tags(["a", " b ", ""]) == ["a", "b"]
    assert nominal_duration_sec("abc") == nominal_duration_sec("abc")  # 결정론적
    assert 60 <= nominal_duration_sec("abc") <= 900


def test_build_candidates_returns_video_dicts_no_exposure_label():
    users = _fixture_users(1)
    videos = build_fixture_video_records(40)
    got = build_candidates(users[0], videos, candidates_per_user=20,
                           exploration_ratio=0.2, rng=random.Random(1))
    assert len(got) == 20
    assert all(isinstance(v, dict) and "video_id" in v for v in got)  # tuple 아님
    assert len({v["video_id"] for v in got}) == 20  # dedup
    # pool보다 큰 요청은 pool 크기로 클램프
    assert len(build_candidates(users[0], videos[:5], 20, 0.2, random.Random(1))) == 5
    assert build_candidates(users[0], [], 20, 0.2, random.Random(1)) == []


def test_event_generation_request_defaults_to_70_20_10_candidate_mix():
    req = EventGenerationRequest(click_threshold=0.55)

    assert req.personalized_ratio == 0.7
    assert req.popular_ratio == 0.2
    assert req.exploration_ratio == 0.1


def test_event_generation_request_accepts_candidate_ratio_sum_inside_tolerance():
    request = EventGenerationRequest(
        click_threshold=0.55,
        personalized_ratio=0.7000000005,
        popular_ratio=0.2,
        exploration_ratio=0.1,
    )

    assert request.personalized_ratio == 0.7000000005


@pytest.mark.parametrize(
    ("personalized", "popular", "exploration"),
    [
        (0.700000002, 0.2, 0.1),
        (0.6, 0.2, 0.1),
        (float("nan"), 0.2, 0.1),
        (float("inf"), 0.0, 0.0),
    ],
)
def test_event_generation_request_rejects_invalid_candidate_ratio_mix(
    personalized,
    popular,
    exploration,
):
    with pytest.raises(ValidationError):
        EventGenerationRequest(
            click_threshold=0.55,
            personalized_ratio=personalized,
            popular_ratio=popular,
            exploration_ratio=exploration,
        )


def test_build_candidates_includes_popular_slice_after_personalized_slice():
    user = {
        "user_id": "vu",
        "primary_categories": ["niche"],
        "interest_keywords": ["niche"],
    }
    videos = []
    for i in range(12):
        videos.append(
            {
                "video_id": f"personal_{i}",
                "title": f"niche match {i}",
                "description": "",
                "tags": [],
                "view_count": 100 - i,
            }
        )
    videos.extend(
        [
            {
                "video_id": "popular_a",
                "title": "broad hit",
                "description": "",
                "tags": [],
                "view_count": 10_000,
            },
            {
                "video_id": "popular_b",
                "title": "another broad hit",
                "description": "",
                "tags": [],
                "view_count": 9_000,
            },
            {
                "video_id": "tail",
                "title": "tail video",
                "description": "",
                "tags": [],
                "view_count": 1,
            },
        ]
    )

    got = build_candidates(
        user,
        videos,
        candidates_per_user=10,
        exploration_ratio=0.1,
        rng=random.Random(7),
        personalized_ratio=0.7,
        popular_ratio=0.2,
    )
    ids = {v["video_id"] for v in got}

    assert len(got) == 10
    assert "popular_a" in ids
    assert "popular_b" in ids
    assert len(ids) == 10


def test_build_candidates_fills_popular_slice_when_top_popular_overlap_personalized():
    user = {
        "user_id": "vu",
        "primary_categories": ["niche"],
        "interest_keywords": ["niche"],
    }
    videos = []
    for i in range(7):
        videos.append(
            {
                "video_id": f"popular_personalized_{i}",
                "title": f"niche popular match {i}",
                "description": "",
                "tags": [],
                "view_count": 10_000 - i,
            }
        )
    for i in range(5):
        videos.append(
            {
                "video_id": f"popular_broad_{i}",
                "title": f"broad popular {i}",
                "description": "",
                "tags": [],
                "view_count": 9_000 - i,
            }
        )

    got = build_candidates(
        user,
        videos,
        candidates_per_user=10,
        exploration_ratio=0.1,
        rng=random.Random(7),
        personalized_ratio=0.7,
        popular_ratio=0.2,
    )
    ids = {v["video_id"] for v in got}

    assert len(got) == 10
    assert {"popular_broad_0", "popular_broad_1"} <= ids


def test_rulebased_judgments_are_indexed_triples():
    users = _fixture_users(1)
    videos = build_fixture_video_records(6)
    raw = RuleBasedActionLogGenerator().generate(users[0], videos)
    data = json.loads(raw)
    # 인덱스 포맷: {"j": [[idx, cp, wf], ...]} — would_like·video_id 없음.
    assert set(data) == {"j"}
    assert len(data["j"]) == 6
    assert [entry[0] for entry in data["j"]] == list(range(6))  # 0..n-1
    for entry in data["j"]:
        assert len(entry) == 3
        idx, cp, wf = entry
        assert 0.0 <= cp <= 1.0
        assert 0.0 <= wf <= 1.0


def test_build_user_drafts_realigns_shuffled_indices():
    # LLM이 순서를 바꿔 반환해도 index로 재결합해 올바른 video_id에 매핑된다.
    vu = {"user_id": "vu_x"}
    videos = [{"video_id": f"vid_{i}"} for i in range(4)]
    shuffled = json.dumps({"j": [[2, 0.9, 0.9], [0, 0.1, 0.1], [3, 0.8, 0.7], [1, 0.2, 0.2]]})
    drafts = _build_user_drafts(vu, videos, shuffled)
    got = {d.video_id: (d.click_propensity, d.watch_fraction) for d in drafts}
    assert got["vid_0"] == (0.1, 0.1)
    assert got["vid_2"] == (0.9, 0.9)
    assert got["vid_3"] == (0.8, 0.7)


@pytest.mark.parametrize(
    "payload",
    [
        {"j": [[0, 0.1, 0.1], [1, 0.2, 0.2], [2, 0.3, 0.3]]},  # 개수 부족(n=4)
        {"j": [[0, 0.1, 0.1], [0, 0.2, 0.2], [2, 0.3, 0.3], [3, 0.4, 0.4]]},  # 중복 index
        {"j": [[0, 0.1, 0.1], [1, 0.2, 0.2], [2, 0.3, 0.3], [9, 0.4, 0.4]]},  # 범위 이탈
        {"j": [[0, 0.1], [1, 0.2, 0.2], [2, 0.3, 0.3], [3, 0.4, 0.4]]},  # 원소 길이 오류
    ],
)
def test_build_user_drafts_rejects_broken_index_sets(payload):
    vu = {"user_id": "vu_x"}
    videos = [{"video_id": f"vid_{i}"} for i in range(4)]
    with pytest.raises(ValueError):
        _build_user_drafts(vu, videos, json.dumps(payload))


def test_chunked_parallel_matches_single_call(tmp_path):
    # 청킹+병렬(chunk_size=8, workers=4)이 단일콜과 동일한 impression/click을 내고 결정론적.
    users, videos = _fixture_users(6), build_fixture_video_records(40)
    chunked = generate_action_log_batch(
        _request(tmp_path / "c", chunk_size=8, max_concurrency=4),
        users, videos, RuleBasedActionLogGenerator(),
    )
    single = generate_action_log_batch(
        _request(tmp_path / "s", chunk_size=0, max_concurrency=1),
        users, videos, RuleBasedActionLogGenerator(),
    )
    assert chunked.summary["impressions"] == single.summary["impressions"] == 6 * 20
    assert chunked.summary["clicks"] == single.summary["clicks"]
    imps = [e for e in chunked.batch.events if e.event_type == "impression"]
    assert imps[0].user_id == "vu_0000"  # 병렬이어도 원본 유저 순서 유지
    assert chunked.summary["quarantined_users"] == 0


def test_streaming_single_matches_legacy_chunked_output_order_and_seed(tmp_path) -> None:
    users = _fixture_users(6)
    videos = build_fixture_video_records(40)
    legacy_request = _request(
        tmp_path / "legacy",
        chunk_size=4,
        max_concurrency=3,
    )
    streaming_request = _request(
        tmp_path / "streaming",
        chunk_size=4,
        max_concurrency=3,
    )

    legacy = generate_action_log_batch(
        legacy_request,
        users,
        videos,
        RuleBasedActionLogGenerator(),
    )
    streamed = pipeline_module.generate_action_log_single(
        streaming_request,
        users,
        videos,
        RuleBasedActionLogGenerator(),
    )

    columns = [
        name
        for name in pipeline_module.EVENT_LOG_PARQUET_SCHEMA.names
        if name != "generated_at"
    ]
    legacy_rows = pq.read_table(legacy_request.output_path, columns=columns).to_pylist()
    streamed_rows = pq.read_table(
        streaming_request.output_path,
        columns=columns,
    ).to_pylist()
    assert streamed.execution_mode == "streaming"
    assert streamed.summary == legacy.summary
    assert streamed_rows == legacy_rows
    assert Path(streaming_request.warehouse_output_path).read_text(
        encoding="utf-8"
    ) == Path(legacy_request.warehouse_output_path).read_text(encoding="utf-8")


def test_streaming_single_selects_one_click_after_all_user_chunks_finish(
    tmp_path,
) -> None:
    videos = build_fixture_video_records(4)

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        return list(videos)

    result = pipeline_module.generate_action_log_single(
        _request(
            tmp_path,
            candidates_per_user=4,
            chunk_size=2,
            max_concurrency=2,
            click_threshold=0.0,
        ),
        _fixture_users(1),
        videos,
        RuleBasedActionLogGenerator(),
        candidate_provider=provider,
    )

    result_path = tmp_path / "e.parquet"
    rows = pq.read_table(result_path).to_pylist()
    assert result_path.is_file()
    assert result.summary["clicks"] == 1
    assert sum(row["event_type"] == "click" for row in rows) == 1


def test_streaming_single_bounds_active_users_and_invokes_provider_on_coordinator(
    tmp_path,
) -> None:
    from threading import get_ident

    coordinator_thread = get_ident()
    provider_threads: list[int] = []
    provider_order: list[str] = []
    snapshots: list[pipeline_module._StreamingRetentionSnapshot] = []
    videos = build_fixture_video_records(2)

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        provider_threads.append(get_ident())
        provider_order.append(virtual_user["user_id"])
        return [videos[0]]

    users = _fixture_users(9)
    pipeline_module.generate_action_log_single(
        _request(
            tmp_path,
            candidates_per_user=1,
            max_concurrency=2,
        ),
        users,
        videos,
        RuleBasedActionLogGenerator(),
        candidate_provider=provider,
        _retention_observer=snapshots.append,
    )

    assert provider_order == [user["user_id"] for user in users]
    assert set(provider_threads) == {coordinator_thread}
    assert max(snapshot.active_users for snapshot in snapshots) <= 4 * 2


def test_streaming_single_consumes_mutable_exposure_metadata_per_drained_user(
    tmp_path,
) -> None:
    metadata: dict[tuple[str, str], ExposureMetadata] = {}
    sizes_before_provider: list[int] = []
    videos = build_fixture_video_records(2)

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        sizes_before_provider.append(len(metadata))
        user_id = virtual_user["user_id"]
        for rank, video in enumerate(videos, start=1):
            metadata[(user_id, str(video["video_id"]))] = ExposureMetadata(
                policy="model",
                rank=rank,
                ctr_score=0.5,
                is_exploration=False,
                policy_version="run-a",
                exposure_source="model",
            )
        return list(videos)

    request = _request(
        tmp_path,
        candidates_per_user=2,
        max_concurrency=2,
    )
    result = pipeline_module.generate_action_log_single(
        request,
        _fixture_users(6),
        videos,
        RuleBasedActionLogGenerator(),
        candidate_provider=provider,
        exposure_metadata=metadata,
    )

    assert result.execution_mode == "streaming"
    assert max(sizes_before_provider) <= (
        4 * request.max_concurrency * request.candidates_per_user
    )
    assert metadata == {}
    rows = pq.read_table(request.output_path, columns=["exposure_source"]).to_pylist()
    assert {row["exposure_source"] for row in rows} == {"model"}


def test_streaming_single_consumes_metadata_when_provider_returns_no_candidates(
    tmp_path,
) -> None:
    metadata: dict[tuple[str, str], ExposureMetadata] = {}
    sizes_before_provider: list[int] = []
    videos = build_fixture_video_records(1)

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        user_id = virtual_user["user_id"]
        sizes_before_provider.append(len(metadata))
        metadata[(user_id, str(videos[0]["video_id"]))] = ExposureMetadata(
            policy="model",
            rank=1,
            ctr_score=0.5,
            is_exploration=False,
            policy_version="run-a",
            exposure_source="model",
        )
        if user_id == "vu_0000":
            return []
        return list(videos)

    result = pipeline_module.generate_action_log_single(
        _request(tmp_path, candidates_per_user=1, max_concurrency=2),
        _fixture_users(2),
        videos,
        RuleBasedActionLogGenerator(),
        candidate_provider=provider,
        exposure_metadata=metadata,
    )

    assert result.execution_mode == "streaming"
    assert sizes_before_provider == [0, 0]
    assert metadata == {}


def test_streaming_single_consumes_metadata_for_quarantined_user(tmp_path) -> None:
    class _FirstBadUser(RuleBasedActionLogGenerator):
        def generate(self, virtual_user: dict, videos: list[dict]) -> str:
            if virtual_user["user_id"] == "vu_0000":
                return "{broken"
            return super().generate(virtual_user, videos)

    metadata: dict[tuple[str, str], ExposureMetadata] = {}
    videos = build_fixture_video_records(1)

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        user_id = virtual_user["user_id"]
        metadata[(user_id, str(videos[0]["video_id"]))] = ExposureMetadata(
            policy="model",
            rank=1,
            ctr_score=0.5,
            is_exploration=False,
            policy_version="run-a",
            exposure_source="model",
        )
        return list(videos)

    result = pipeline_module.generate_action_log_single(
        _request(
            tmp_path,
            candidates_per_user=1,
            max_concurrency=1,
            max_quarantine_ratio=1.0,
        ),
        _fixture_users(2),
        videos,
        _FirstBadUser(),
        candidate_provider=provider,
        exposure_metadata=metadata,
    )

    assert result.summary["invalid_json"] == 1
    assert metadata == {}


def test_single_falls_back_to_legacy_for_duplicate_user_id(tmp_path) -> None:
    users = _fixture_users(2)
    users[1]["user_id"] = users[0]["user_id"]
    videos = build_fixture_video_records(1)
    provider_calls: list[str] = []

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        provider_calls.append(virtual_user["user_id"])
        return list(videos)

    result = pipeline_module.generate_action_log_single(
        _request(tmp_path),
        users,
        videos,
        RuleBasedActionLogGenerator(),
        candidate_provider=provider,
    )

    assert result.execution_mode == "legacy"
    assert provider_calls == [users[0]["user_id"], users[1]["user_id"]]


def test_single_falls_back_to_legacy_for_missing_user_id(tmp_path) -> None:
    users = _fixture_users(1)
    users[0].pop("user_id")
    metadata: dict[tuple[str, str], ExposureMetadata] = {}
    provider_calls: list[str] = []
    videos = build_fixture_video_records(1)

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        provider_user_id = str(virtual_user.get("user_id", ""))
        provider_calls.append(provider_user_id)
        metadata[(provider_user_id, str(videos[0]["video_id"]))] = ExposureMetadata(
            policy="model",
            rank=1,
            ctr_score=0.5,
            is_exploration=False,
            policy_version="run-a",
            exposure_source="model",
        )
        return list(videos)

    request = _request(tmp_path, candidates_per_user=1)
    result = pipeline_module.generate_action_log_single(
        request,
        users,
        videos,
        RuleBasedActionLogGenerator(),
        candidate_provider=provider,
        exposure_metadata=metadata,
    )

    assert result.execution_mode == "legacy"
    assert provider_calls == [""]
    rows = pq.read_table(request.output_path, columns=["exposure_source"]).to_pylist()
    assert {row["exposure_source"] for row in rows} == {"model"}


def test_single_falls_back_to_legacy_for_missing_and_explicit_user_0(
    tmp_path,
) -> None:
    users = _fixture_users(2)
    users[0].pop("user_id")
    users[1]["user_id"] = "user_0"
    metadata: dict[tuple[str, str], ExposureMetadata] = {}
    provider_calls: list[str] = []
    videos = build_fixture_video_records(1)

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        provider_user_id = str(virtual_user.get("user_id", ""))
        provider_calls.append(provider_user_id)
        metadata[(provider_user_id, str(videos[0]["video_id"]))] = ExposureMetadata(
            policy="model",
            rank=1,
            ctr_score=0.5,
            is_exploration=False,
            policy_version="run-a",
            exposure_source="model",
        )
        return list(videos)

    request = _request(tmp_path, candidates_per_user=1)
    result = pipeline_module.generate_action_log_single(
        request,
        users,
        videos,
        RuleBasedActionLogGenerator(),
        candidate_provider=provider,
        exposure_metadata=metadata,
    )

    assert result.execution_mode == "legacy"
    assert provider_calls == ["", "user_0"]
    rows = pq.read_table(request.output_path, columns=["exposure_source"]).to_pylist()
    assert {row["exposure_source"] for row in rows} == {"model"}


def test_single_falls_back_to_legacy_for_read_only_exposure_metadata(
    tmp_path,
) -> None:
    backing: dict[tuple[str, str], ExposureMetadata] = {}
    metadata = MappingProxyType(backing)
    videos = build_fixture_video_records(2)

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        user_id = virtual_user["user_id"]
        backing[(user_id, str(videos[0]["video_id"]))] = ExposureMetadata(
            policy="model",
            rank=1,
            ctr_score=0.5,
            is_exploration=False,
            policy_version="run-a",
            exposure_source="model",
        )
        return [videos[0]]

    request = _request(tmp_path, candidates_per_user=1)
    result = pipeline_module.generate_action_log_single(
        request,
        _fixture_users(2),
        videos,
        RuleBasedActionLogGenerator(),
        candidate_provider=provider,
        exposure_metadata=metadata,
    )

    assert result.execution_mode == "legacy"
    assert set(
        pq.read_table(
            request.output_path,
            columns=["exposure_source"],
        ).column("exposure_source").to_pylist()
    ) == {"model"}


def test_streaming_single_preserves_quarantine_order_counts_and_file(
    tmp_path,
) -> None:
    class _TwoBadUsers(RuleBasedActionLogGenerator):
        def generate(self, virtual_user: dict, videos: list[dict]) -> str:
            if virtual_user["user_id"] in {"vu_0001", "vu_0003"}:
                return "{broken"
            return super().generate(virtual_user, videos)

    request = _request(
        tmp_path,
        candidates_per_user=2,
        max_concurrency=3,
        max_quarantine_ratio=0.5,
    )
    snapshots: list[pipeline_module._StreamingRetentionSnapshot] = []
    result = pipeline_module.generate_action_log_single(
        request,
        _fixture_users(5),
        build_fixture_video_records(4),
        _TwoBadUsers(),
        _retention_observer=snapshots.append,
    )

    quarantined = [
        json.loads(line)
        for line in Path(request.quarantine_output_path)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert result.summary["quarantined_users"] == 2
    assert result.summary["invalid_json"] == 2
    assert [row["user_id"] for row in quarantined] == ["vu_0001", "vu_0003"]
    final_snapshot = snapshots[-1]
    assert final_snapshot.completed_work == final_snapshot.total_work == 5
    assert final_snapshot.failed_work == 2
    assert final_snapshot.pending_work == 0


def test_streaming_single_raises_after_writing_quarantine_when_ratio_exceeded(
    tmp_path,
) -> None:
    class _AllBad(RuleBasedActionLogGenerator):
        def generate(self, virtual_user: dict, videos: list[dict]) -> str:
            return "{broken"

    request = _request(
        tmp_path,
        candidates_per_user=2,
        max_quarantine_ratio=0.25,
    )
    with pytest.raises(
        ActionLogGenerationError,
        match="quarantine ratio 1.00 exceeds",
    ):
        pipeline_module.generate_action_log_single(
            request,
            _fixture_users(3),
            build_fixture_video_records(4),
            _AllBad(),
        )

    assert len(
        Path(request.quarantine_output_path)
        .read_text(encoding="utf-8")
        .splitlines()
    ) == 3


def test_streaming_single_keeps_workers_busy_behind_slow_head_user(
    tmp_path,
) -> None:
    """완료된 뒤쪽 work가 느린 선두 user를 기다리지 않고 worker를 이어받는다."""

    users = _fixture_users(3)
    videos = build_fixture_video_records(1)
    first_user = users[0]["user_id"]
    third_user = users[2]["user_id"]
    first_started = Event()
    third_started = Event()
    release_first = Event()
    worker_errors: list[BaseException] = []

    class _SlowHeadGenerator(RuleBasedActionLogGenerator):
        def generate(self, virtual_user: dict, candidates: list[dict]) -> str:
            user_id = virtual_user["user_id"]
            if user_id == first_user:
                first_started.set()
                assert release_first.wait(timeout=5.0)
            elif user_id == third_user:
                third_started.set()
            return super().generate(virtual_user, candidates)

    def run_generation() -> None:
        try:
            pipeline_module.generate_action_log_single(
                _request(
                    tmp_path,
                    candidates_per_user=1,
                    chunk_size=1,
                    max_concurrency=2,
                ),
                users,
                videos,
                _SlowHeadGenerator(),
                candidate_provider=lambda _virtual_user, _user_rng: list(videos),
            )
        except BaseException as error:  # noqa: BLE001 - test thread boundary
            worker_errors.append(error)

    generation_thread = Thread(target=run_generation)
    generation_thread.start()
    try:
        assert first_started.wait(timeout=2.0)
        assert third_started.wait(timeout=2.0)
    finally:
        release_first.set()
        generation_thread.join(timeout=5.0)

    assert not generation_thread.is_alive()
    assert worker_errors == []


def test_streaming_single_emits_operational_retention_telemetry(
    tmp_path,
    caplog,
    monkeypatch,
) -> None:
    """private observer 없이도 single coordinator는 안전한 시작·종료 telemetry를 남긴다."""

    monkeypatch.setenv("ACTION_LOG_TELEMETRY_DETAIL_MAX_WORK", "2")
    monkeypatch.setenv("ACTION_LOG_TELEMETRY_INTERVAL_SEC", "10")
    videos = build_fixture_video_records(2)
    request = _request(
        tmp_path,
        candidates_per_user=2,
        chunk_size=1,
        max_concurrency=2,
    )

    with caplog.at_level(logging.INFO, logger="autoresearch.action_log_generation.pipeline"):
        pipeline_module.generate_action_log_single(
            request,
            _fixture_users(1),
            videos,
            RuleBasedActionLogGenerator(),
            candidate_provider=lambda _virtual_user, _user_rng: list(videos),
        )

    assert any(
        record.message == "Starting action log draft generation"
        for record in caplog.records
    )
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.message.startswith("{")
    ]
    progress = [
        event
        for event in events
        if event["event"] == "action_log_streaming_progress"
    ]
    assert progress[0]["total_work"] is None
    assert progress[0]["pending_work"] is None
    final = progress[-1]
    assert final["phase"] == "finalizing"
    assert final["active_users"] == 0
    assert final["buffered_drafts"] == 0
    assert final["buffered_events"] == 0
    assert final["in_flight_work"] == 0
    assert final["pending_work"] == 0
    assert final["completed_work"] == final["total_work"] == 2

    detail = [
        event
        for event in events
        if event["event"] == "action_log_micro_work_complete"
    ]
    assert [event["work_sequence"] for event in detail] == [0, 1]
    serialized = json.dumps(events, ensure_ascii=False)
    assert "vu_0000" not in serialized
    assert "persona_summary" not in serialized
    assert "raw_text" not in serialized
    assert "prompt" not in serialized


def test_streaming_single_finishes_telemetry_after_writer_finalization(
    tmp_path,
    monkeypatch,
) -> None:
    """성공 telemetry의 강제 종료는 writer가 row buffer를 해제한 뒤에만 일어난다."""

    order: list[str] = []
    original_finalize = pipeline_module._StreamingActionLogWriter.finalize_success

    class _RecordingTelemetry:
        @property
        def detailed_candidate(self) -> bool:
            return False

        def start(self, _snapshot: object) -> None:
            return None

        def note_submission(self, _submitted_work: int) -> None:
            return None

        def record_work(self, **_metrics: object) -> None:
            return None

        def observe(self, _snapshot: object) -> None:
            return None

        def finish(self, _snapshot: object) -> None:
            order.append("finish")

    def _record_finalization(
        writer: pipeline_module._StreamingActionLogWriter,
        *args: object,
        **kwargs: object,
    ) -> None:
        order.append("finalize_success")
        original_finalize(writer, *args, **kwargs)

    monkeypatch.setattr(
        pipeline_module,
        "ActionLogStreamingTelemetryReporter",
        lambda *, logger: _RecordingTelemetry(),
    )
    monkeypatch.setattr(
        pipeline_module._StreamingActionLogWriter,
        "finalize_success",
        _record_finalization,
    )

    pipeline_module.generate_action_log_single(
        _request(tmp_path, candidates_per_user=1, max_concurrency=1),
        _fixture_users(1),
        build_fixture_video_records(1),
        RuleBasedActionLogGenerator(),
    )

    assert order == ["finalize_success", "finish"]


class _NoopStreamingTelemetry:
    """pipeline의 operational reporter 경계만 검사하는 test double."""

    @property
    def detailed_candidate(self) -> bool:
        return False

    def start(self, _snapshot: object) -> None:
        return None

    def note_submission(self, _submitted_work: int) -> None:
        return None

    def record_work(self, **_metrics: object) -> None:
        return None

    def observe(self, _snapshot: object) -> None:
        return None

    def finish(self, _snapshot: object) -> None:
        return None


@pytest.mark.parametrize(
    "failure_stage",
    [
        "constructor",
        "start",
        "observe",
        "note_submission",
        "detailed_candidate",
        "record_work",
    ],
)
def test_streaming_single_isolates_nonfinal_telemetry_failures(
    tmp_path,
    caplog,
    monkeypatch,
    failure_stage: str,
) -> None:
    """운영 reporter의 non-final API 오류는 generation/output을 실패시키지 않는다."""

    class _FailOnceTelemetry(_NoopStreamingTelemetry):
        failed = False

        def _fail_once(self, stage: str) -> None:
            if not self.failed and failure_stage == stage:
                self.failed = True
                raise RuntimeError("private prompt raw response")

        @property
        def detailed_candidate(self) -> bool:
            self._fail_once("detailed_candidate")
            return False

        def start(self, _snapshot: object) -> None:
            self._fail_once("start")

        def note_submission(self, _submitted_work: int) -> None:
            self._fail_once("note_submission")

        def record_work(self, **_metrics: object) -> None:
            self._fail_once("record_work")

        def observe(self, _snapshot: object) -> None:
            self._fail_once("observe")

    reporter = _FailOnceTelemetry()

    def _reporter_factory(*, logger: logging.Logger) -> _NoopStreamingTelemetry:
        if failure_stage == "constructor":
            raise RuntimeError("private prompt raw response")
        return reporter

    monkeypatch.setattr(
        pipeline_module,
        "ActionLogStreamingTelemetryReporter",
        _reporter_factory,
    )
    request = _request(tmp_path, candidates_per_user=1, max_concurrency=1)

    with caplog.at_level(logging.WARNING, logger="autoresearch.action_log_generation.pipeline"):
        result = pipeline_module.generate_action_log_single(
            request,
            _fixture_users(1),
            build_fixture_video_records(1),
            RuleBasedActionLogGenerator(),
        )

    assert result.execution_mode == "streaming"
    assert Path(request.output_path).is_file()
    assert Path(request.warehouse_output_path).is_file()
    assert Path(request.quarantine_output_path).is_file()
    warning_messages = [
        record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]
    assert warning_messages == [
        "Action log streaming telemetry disabled after reporter failure"
    ]
    assert "private prompt" not in "\n".join(warning_messages)
    assert "raw response" not in "\n".join(warning_messages)


def test_streaming_single_keeps_success_when_finish_telemetry_fails(
    tmp_path,
    monkeypatch,
) -> None:
    """세 출력 commit 뒤 finish reporter 오류는 성공 결과를 거짓 실패로 바꾸지 않는다."""

    class _FailingFinishTelemetry(_NoopStreamingTelemetry):
        def finish(self, _snapshot: object) -> None:
            raise RuntimeError("telemetry finish failed")

    monkeypatch.setattr(
        pipeline_module,
        "ActionLogStreamingTelemetryReporter",
        lambda *, logger: _FailingFinishTelemetry(),
    )
    request = _request(tmp_path, candidates_per_user=1, max_concurrency=1)

    result = pipeline_module.generate_action_log_single(
        request,
        _fixture_users(1),
        build_fixture_video_records(1),
        RuleBasedActionLogGenerator(),
    )

    assert result.execution_mode == "streaming"
    assert Path(request.output_path).is_file()
    assert Path(request.warehouse_output_path).is_file()
    assert Path(request.quarantine_output_path).is_file()


def test_streaming_single_keeps_success_when_telemetry_warning_sink_fails(
    tmp_path,
    monkeypatch,
) -> None:
    """reporter constructor와 disable warning 오류가 겹쳐도 성공을 유지한다."""

    def _raise_reporter_error(*, logger: logging.Logger) -> _NoopStreamingTelemetry:
        raise RuntimeError("telemetry constructor failed")

    def _raise_warning_error(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("warning sink failed")

    monkeypatch.setattr(
        pipeline_module,
        "ActionLogStreamingTelemetryReporter",
        _raise_reporter_error,
    )
    monkeypatch.setattr(pipeline_module.logger, "warning", _raise_warning_error)
    request = _request(tmp_path, candidates_per_user=1, max_concurrency=1)

    result = pipeline_module.generate_action_log_single(
        request,
        _fixture_users(1),
        build_fixture_video_records(1),
        RuleBasedActionLogGenerator(),
    )

    assert result.execution_mode == "streaming"
    assert Path(request.output_path).is_file()
    assert Path(request.warehouse_output_path).is_file()
    assert Path(request.quarantine_output_path).is_file()


def test_streaming_single_commits_quarantine_when_final_observe_telemetry_fails(
    tmp_path,
    monkeypatch,
) -> None:
    """quarantine 전 final observe 오류에도 q만 commit하고 원래 오류를 재전파한다."""

    class _FailingFinalObserveTelemetry(_NoopStreamingTelemetry):
        def observe(self, snapshot: object) -> None:
            if getattr(snapshot, "phase") == "finalizing":
                raise RuntimeError("telemetry observe failed")

    class _AllBad(RuleBasedActionLogGenerator):
        def generate(self, virtual_user: dict, videos: list[dict]) -> str:
            return "{broken"

    monkeypatch.setattr(
        pipeline_module,
        "ActionLogStreamingTelemetryReporter",
        lambda *, logger: _FailingFinalObserveTelemetry(),
    )
    request = _request(
        tmp_path,
        candidates_per_user=1,
        max_quarantine_ratio=0.25,
    )

    with pytest.raises(
        ActionLogGenerationError,
        match="quarantine ratio 1.00 exceeds",
    ):
        pipeline_module.generate_action_log_single(
            request,
            _fixture_users(1),
            build_fixture_video_records(1),
            _AllBad(),
        )

    assert not Path(request.output_path).exists()
    assert not Path(request.warehouse_output_path).exists()
    assert Path(request.quarantine_output_path).read_text(encoding="utf-8").count(
        "\n"
    ) == 1


def test_streaming_single_preserves_quarantine_when_telemetry_warning_sink_fails(
    tmp_path,
    monkeypatch,
) -> None:
    """final reporter와 warning 오류가 겹쳐도 q commit 뒤 원래 오류를 전파한다."""

    class _FailingFinalObserveTelemetry(_NoopStreamingTelemetry):
        def observe(self, snapshot: object) -> None:
            if getattr(snapshot, "phase") == "finalizing":
                raise RuntimeError("telemetry observe failed")

    class _AllBad(RuleBasedActionLogGenerator):
        def generate(self, virtual_user: dict, videos: list[dict]) -> str:
            return "{broken"

    def _raise_warning_error(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("warning sink failed")

    monkeypatch.setattr(
        pipeline_module,
        "ActionLogStreamingTelemetryReporter",
        lambda *, logger: _FailingFinalObserveTelemetry(),
    )
    monkeypatch.setattr(pipeline_module.logger, "warning", _raise_warning_error)
    request = _request(
        tmp_path,
        candidates_per_user=1,
        max_quarantine_ratio=0.25,
    )

    with pytest.raises(
        ActionLogGenerationError,
        match="quarantine ratio 1.00 exceeds",
    ):
        pipeline_module.generate_action_log_single(
            request,
            _fixture_users(1),
            build_fixture_video_records(1),
            _AllBad(),
        )

    assert not Path(request.output_path).exists()
    assert not Path(request.warehouse_output_path).exists()
    assert Path(request.quarantine_output_path).read_text(encoding="utf-8").count(
        "\n"
    ) == 1


def test_streaming_single_ignores_finalizer_callback_telemetry_failure(
    tmp_path,
    monkeypatch,
) -> None:
    """row-buffer observer의 reporter 오류는 Parquet 최종화와 commit을 중단하지 않는다."""

    class _FailingFinalizerObserveTelemetry(_NoopStreamingTelemetry):
        finalizing_observations = 0

        def observe(self, snapshot: object) -> None:
            if getattr(snapshot, "phase") != "finalizing":
                return
            self.finalizing_observations += 1
            if self.finalizing_observations == 2:
                raise RuntimeError("telemetry finalizer callback failed")

    monkeypatch.setattr(
        pipeline_module,
        "ActionLogStreamingTelemetryReporter",
        lambda *, logger: _FailingFinalizerObserveTelemetry(),
    )
    request = _request(tmp_path, candidates_per_user=2, max_concurrency=1)

    result = pipeline_module.generate_action_log_single(
        request,
        _fixture_users(1),
        build_fixture_video_records(2),
        RuleBasedActionLogGenerator(),
    )

    assert result.execution_mode == "streaming"
    assert Path(request.output_path).is_file()
    assert Path(request.warehouse_output_path).is_file()
    assert Path(request.quarantine_output_path).is_file()


def test_streaming_single_preserves_quarantine_error_when_finish_telemetry_fails(
    tmp_path,
    monkeypatch,
) -> None:
    """quarantine 최종 telemetry 오류가 원래 generation 오류를 덮어쓰면 안 된다."""

    class _FailingFinishTelemetry:
        @property
        def detailed_candidate(self) -> bool:
            return False

        def start(self, _snapshot: object) -> None:
            return None

        def note_submission(self, _submitted_work: int) -> None:
            return None

        def record_work(self, **_metrics: object) -> None:
            return None

        def observe(self, _snapshot: object) -> None:
            return None

        def finish(self, _snapshot: object) -> None:
            raise RuntimeError("telemetry finish failed")

    class _AllBad(RuleBasedActionLogGenerator):
        def generate(self, virtual_user: dict, videos: list[dict]) -> str:
            return "{broken"

    monkeypatch.setattr(
        pipeline_module,
        "ActionLogStreamingTelemetryReporter",
        lambda *, logger: _FailingFinishTelemetry(),
    )

    with pytest.raises(
        ActionLogGenerationError,
        match="quarantine ratio 1.00 exceeds",
    ):
        pipeline_module.generate_action_log_single(
            _request(
                tmp_path,
                candidates_per_user=1,
                max_quarantine_ratio=0.25,
            ),
            _fixture_users(1),
            build_fixture_video_records(1),
            _AllBad(),
        )


def test_streaming_single_waits_for_known_total_before_worker_detail_context(
    tmp_path,
    caplog,
    monkeypatch,
) -> None:
    """unknown total의 active window에서는 detailed-only worker event를 남기지 않는다."""

    from autoresearch.action_log_generation.observability import emit_action_log_event

    monkeypatch.setenv("ACTION_LOG_TELEMETRY_DETAIL_MAX_WORK", "2")
    videos = build_fixture_video_records(1)
    context_flags: dict[int, bool] = {}
    original_work: Callable[..., _ActionLogCallResult] = (
        pipeline_module._generate_action_log_work
    )

    def _capture_context(
        generator: ActionLogGenerator,
        item: _ActionLogWorkItem,
        *,
        work_sequence: int,
        submitted_at: float,
        shard_index: int | None,
        detailed_telemetry: bool,
    ) -> _ActionLogCallResult:
        context_flags[work_sequence] = detailed_telemetry
        with pipeline_module.action_log_work_log_context(
            shard_index=shard_index,
            work_sequence=work_sequence,
            detailed=detailed_telemetry,
        ):
            emit_action_log_event(
                pipeline_module.logger,
                logging.INFO,
                "test_action_log_detailed_only_probe",
                detailed_only=True,
            )
        return original_work(
            generator,
            item,
            work_sequence=work_sequence,
            submitted_at=submitted_at,
            shard_index=shard_index,
            detailed_telemetry=detailed_telemetry,
        )

    monkeypatch.setattr(
        pipeline_module,
        "_generate_action_log_work",
        _capture_context,
    )
    with caplog.at_level(logging.INFO, logger="autoresearch.action_log_generation.pipeline"):
        pipeline_module.generate_action_log_single(
            _request(
                tmp_path,
                candidates_per_user=1,
                chunk_size=1,
                max_concurrency=2,
            ),
            _fixture_users(9),
            videos,
            RuleBasedActionLogGenerator(),
            candidate_provider=lambda _virtual_user, _user_rng: [videos[0]],
        )

    assert context_flags == {sequence: False for sequence in range(9)}
    assert context_flags[8] is False
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.message.startswith("{")
    ]
    assert not any(
        event["event"] == "test_action_log_detailed_only_probe" for event in events
    )
    assert not any(
        event["event"] == "action_log_micro_work_complete" for event in events
    )


def test_streaming_single_retained_payload_is_bounded_by_active_users(
    tmp_path,
) -> None:
    users = _fixture_users(40)
    candidates_per_user = 7
    max_concurrency = 3
    snapshots: list[pipeline_module._StreamingRetentionSnapshot] = []

    pipeline_module.generate_action_log_single(
        _request(
            tmp_path,
            candidates_per_user=candidates_per_user,
            chunk_size=2,
            max_concurrency=max_concurrency,
            click_threshold=0.0,
        ),
        users,
        build_fixture_video_records(20),
        RuleBasedActionLogGenerator(),
        _retention_observer=snapshots.append,
    )

    assert snapshots
    assert all(
        item.phase in {"generating", "finalizing"} for item in snapshots
    )
    assert all(isinstance(item.active_users, int) for item in snapshots)
    assert all(isinstance(item.buffered_drafts, int) for item in snapshots)
    assert all(isinstance(item.buffered_events, int) for item in snapshots)
    assert all(isinstance(item.in_flight_work, int) for item in snapshots)
    assert all(isinstance(item.activated_users, int) for item in snapshots)
    assert all(isinstance(item.total_users, int) for item in snapshots)
    assert all(isinstance(item.submitted_work, int) for item in snapshots)
    assert all(
        item.total_work is None or isinstance(item.total_work, int)
        for item in snapshots
    )
    assert all(isinstance(item.completed_work, int) for item in snapshots)
    assert all(isinstance(item.failed_work, int) for item in snapshots)
    assert all(
        item.pending_work is None or isinstance(item.pending_work, int)
        for item in snapshots
    )
    assert max(item.active_users for item in snapshots) <= 4 * max_concurrency
    assert max(item.in_flight_work for item in snapshots) <= max_concurrency
    assert max(item.buffered_drafts for item in snapshots) <= (
        4 * max_concurrency * candidates_per_user
    )
    assert max(item.buffered_events for item in snapshots) <= 50_000
    assert [item.activated_users for item in snapshots] == sorted(
        item.activated_users for item in snapshots
    )
    assert any(
        item.total_work is None and item.pending_work is None for item in snapshots
    )
    exhausted_snapshots = [
        item for item in snapshots if item.total_work is not None
    ]
    assert exhausted_snapshots
    assert any(
        item.pending_work is not None
        and item.pending_work > 0
        and item.pending_work
        == item.total_work - item.completed_work - item.in_flight_work
        for item in exhausted_snapshots
    )
    assert all(
        item.pending_work
        == item.total_work - item.completed_work - item.in_flight_work
        for item in exhausted_snapshots
    )
    generating_snapshots = [
        item for item in snapshots if item.phase == "generating"
    ]
    assert generating_snapshots[-1].pending_work == 0
    event_buffer_positions = [
        index
        for index, item in enumerate(generating_snapshots)
        if item.buffered_events > 0
    ]
    assert len(event_buffer_positions) == len(users)
    assert all(
        generating_snapshots[index + 1].buffered_events == 0
        for index in event_buffer_positions
    )
    final_snapshot = snapshots[-1]
    assert final_snapshot.phase == "finalizing"
    assert final_snapshot.active_users == 0
    assert final_snapshot.buffered_drafts == 0
    assert final_snapshot.buffered_events == 0
    assert final_snapshot.in_flight_work == 0
    assert final_snapshot.pending_work == 0
    assert final_snapshot.activated_users == len(users)
    assert final_snapshot.total_users == len(users)
    assert final_snapshot.completed_work == final_snapshot.total_work
    assert any(
        item.phase == "finalizing" and item.buffered_events > 0
        for item in snapshots
    )
    assert any(item.buffered_drafts == 0 for item in snapshots)


def test_streaming_single_releases_completed_future_drafts_before_next_provider(
    tmp_path,
    monkeypatch,
) -> None:
    users = _fixture_users(5)
    videos = build_fixture_video_records(3)
    first_user = users[0]["user_id"]
    next_user = users[4]["user_id"]
    first_draft_refs: list[weakref.ReferenceType[ImpressionDraft]] = []
    provider_order: list[str] = []
    original_work: Callable[..., _ActionLogCallResult] = (
        pipeline_module._generate_action_log_work
    )

    class _InlineExecutor:
        def __init__(self, *, max_workers: int) -> None:
            self.max_workers = max_workers

        def __enter__(self) -> Self:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            return None

        def submit(
            self,
            fn: Callable[..., _ActionLogCallResult],
            generator: ActionLogGenerator,
            item: _ActionLogWorkItem,
            /,
            *,
            work_sequence: int,
            submitted_at: float,
            shard_index: int | None,
            detailed_telemetry: bool,
        ) -> Future[_ActionLogCallResult]:
            future: Future[_ActionLogCallResult] = Future()
            try:
                future.set_result(
                    fn(
                        generator,
                        item,
                        work_sequence=work_sequence,
                        submitted_at=submitted_at,
                        shard_index=shard_index,
                        detailed_telemetry=detailed_telemetry,
                    )
                )
            except BaseException as error:  # noqa: BLE001 - Future worker boundary
                future.set_exception(error)
            return future

    def _capture_first_user_drafts(
        generator: ActionLogGenerator,
        item: _ActionLogWorkItem,
        *,
        work_sequence: int,
        submitted_at: float,
        shard_index: int | None,
        detailed_telemetry: bool,
    ) -> _ActionLogCallResult:
        result = original_work(
            generator,
            item,
            work_sequence=work_sequence,
            submitted_at=submitted_at,
            shard_index=shard_index,
            detailed_telemetry=detailed_telemetry,
        )
        if item.user_id == first_user:
            assert result.drafts is not None
            first_draft_refs.extend(weakref.ref(draft) for draft in result.drafts)
        return result

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        user_id = str(virtual_user["user_id"])
        provider_order.append(user_id)
        if user_id == next_user:
            assert first_draft_refs
            gc.collect()
            assert all(draft_ref() is None for draft_ref in first_draft_refs)
        return list(videos)

    monkeypatch.setattr(
        pipeline_module,
        "_generate_action_log_work",
        _capture_first_user_drafts,
    )
    monkeypatch.setattr(pipeline_module, "ThreadPoolExecutor", _InlineExecutor)
    result = pipeline_module.generate_action_log_single(
        _request(
            tmp_path,
            candidates_per_user=3,
            max_concurrency=1,
        ),
        users,
        videos,
        RuleBasedActionLogGenerator(),
        candidate_provider=provider,
    )

    assert provider_order == [user["user_id"] for user in users]
    assert result.summary["impressions"] == 15


def test_streaming_single_matches_legacy_when_later_work_finishes_first(
    tmp_path,
    monkeypatch,
) -> None:
    users = _fixture_users(2)
    videos = build_fixture_video_records(4)

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        return list(videos)

    legacy_request = _request(
        tmp_path / "legacy",
        candidates_per_user=4,
        chunk_size=2,
        max_concurrency=2,
    )
    streaming_request = _request(
        tmp_path / "streaming",
        candidates_per_user=4,
        chunk_size=2,
        max_concurrency=2,
    )
    legacy = generate_action_log_batch(
        legacy_request,
        users,
        videos,
        RuleBasedActionLogGenerator(),
        candidate_provider=provider,
    )

    first_work_started = Event()
    later_chunk_future_finished = Event()
    later_user_future_finished = Event()
    completed_work: list[tuple[str, str]] = []
    first_user = users[0]["user_id"]
    later_user = users[1]["user_id"]
    first_chunk_video = str(videos[0]["video_id"])
    later_chunk_video = str(videos[2]["video_id"])
    original_work: Callable[..., _ActionLogCallResult] = (
        pipeline_module._generate_action_log_work
    )

    class _CompletionTrackingExecutor(pipeline_module.ThreadPoolExecutor):
        def submit(
            self,
            fn: Callable[..., _ActionLogCallResult],
            generator: ActionLogGenerator,
            item: _ActionLogWorkItem,
            /,
            *,
            work_sequence: int,
            submitted_at: float,
            shard_index: int | None,
            detailed_telemetry: bool,
        ) -> Future[_ActionLogCallResult]:
            future = super().submit(
                fn,
                generator,
                item,
                work_sequence=work_sequence,
                submitted_at=submitted_at,
                shard_index=shard_index,
                detailed_telemetry=detailed_telemetry,
            )
            key = (item.user_id, str(item.candidates[0]["video_id"]))
            if key == (first_user, later_chunk_video):
                future.add_done_callback(_mark_later_chunk_future_finished)
            elif key == (later_user, first_chunk_video):
                future.add_done_callback(_mark_later_user_future_finished)
            return future

    def _mark_later_chunk_future_finished(
        completed_future: Future[_ActionLogCallResult],
    ) -> None:
        later_chunk_future_finished.set()

    def _mark_later_user_future_finished(
        completed_future: Future[_ActionLogCallResult],
    ) -> None:
        later_user_future_finished.set()

    def _complete_out_of_order(
        generator: ActionLogGenerator,
        item: _ActionLogWorkItem,
        *,
        work_sequence: int,
        submitted_at: float,
        shard_index: int | None,
        detailed_telemetry: bool,
    ) -> _ActionLogCallResult:
        result = original_work(
            generator,
            item,
            work_sequence=work_sequence,
            submitted_at=submitted_at,
            shard_index=shard_index,
            detailed_telemetry=detailed_telemetry,
        )
        key = (item.user_id, str(item.candidates[0]["video_id"]))
        if key == (first_user, first_chunk_video):
            first_work_started.set()
            assert later_chunk_future_finished.wait(timeout=2.0)
            assert later_user_future_finished.wait(timeout=2.0)
        elif key in {
            (first_user, later_chunk_video),
            (later_user, first_chunk_video),
        }:
            assert first_work_started.wait(timeout=2.0)
        completed_work.append(key)
        return result

    monkeypatch.setattr(
        pipeline_module,
        "ThreadPoolExecutor",
        _CompletionTrackingExecutor,
    )
    monkeypatch.setattr(
        pipeline_module,
        "_generate_action_log_work",
        _complete_out_of_order,
    )
    streamed = pipeline_module.generate_action_log_single(
        streaming_request,
        users,
        videos,
        RuleBasedActionLogGenerator(),
        candidate_provider=provider,
    )

    assert completed_work.index((first_user, later_chunk_video)) < completed_work.index(
        (first_user, first_chunk_video)
    )
    assert completed_work.index((later_user, first_chunk_video)) < completed_work.index(
        (first_user, first_chunk_video)
    )
    columns = [
        name
        for name in pipeline_module.EVENT_LOG_PARQUET_SCHEMA.names
        if name != "generated_at"
    ]
    assert pq.read_table(streaming_request.output_path, columns=columns).to_pylist() == (
        pq.read_table(legacy_request.output_path, columns=columns).to_pylist()
    )
    assert streamed.summary == legacy.summary
    assert Path(streaming_request.warehouse_output_path).read_text(
        encoding="utf-8"
    ) == Path(legacy_request.warehouse_output_path).read_text(encoding="utf-8")


def test_draft_progress_callback_reports_completed_chunks(tmp_path):
    users, videos = _fixture_users(2), build_fixture_video_records(8)
    snapshots = []

    result = generate_action_log_drafts(
        _request(tmp_path, candidates_per_user=4, chunk_size=2, max_concurrency=2),
        users,
        videos,
        RuleBasedActionLogGenerator(),
        progress_callback=snapshots.append,
    )

    assert result.total_work == 4
    completed = [snapshot.completed_chunks for snapshot in snapshots]
    assert completed[0] == 0
    assert completed[-1] == 4
    assert completed == sorted(set(completed))
    assert {snapshot.total_chunks for snapshot in snapshots} == {4}
    assert snapshots[-1].success_chunks == 4
    assert snapshots[-1].failed_chunks == 0
    assert snapshots[-1].quarantined_chunks == 0


def test_progress_snapshot_is_emitted_after_completed_batch_is_drained(
    tmp_path,
    monkeypatch,
    caplog,
):
    real_wait = pipeline_module.wait

    def _wait_for_current_batch(futures, *, return_when):
        return real_wait(futures)

    monkeypatch.setattr(pipeline_module, "wait", _wait_for_current_batch)
    snapshots = []

    with caplog.at_level(logging.INFO, logger="autoresearch.action_log_generation.pipeline"):
        result = generate_action_log_drafts(
            _request(
                tmp_path,
                candidates_per_user=4,
                chunk_size=2,
                max_concurrency=2,
            ),
            _fixture_users(2),
            build_fixture_video_records(8),
            RuleBasedActionLogGenerator(),
            progress_callback=snapshots.append,
        )

    assert result.total_work == 4
    assert [snapshot.completed_chunks for snapshot in snapshots] == [0, 2, 4]
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.message.startswith("{")
    ]
    micro = [
        event
        for event in events
        if event["event"] == "action_log_micro_work_complete"
    ]
    assert len(micro) == 4
    assert [event["completed_work"] for event in micro] == [2, 2, 4, 4]
    assert all(
        event["completed_work"]
        + event["active_workers"]
        + event["pending_work"]
        == event["total_work"]
        for event in micro
    )


def test_draft_progress_callback_counts_quarantined_chunks(tmp_path):
    class _OneBadUserGen(RuleBasedActionLogGenerator):
        def generate(self, virtual_user, videos):
            if virtual_user["user_id"] == "vu_0000":
                return "{not valid json"
            return super().generate(virtual_user, videos)

    users, videos = _fixture_users(2), build_fixture_video_records(8)
    snapshots = []

    result = generate_action_log_drafts(
        _request(tmp_path, candidates_per_user=4, chunk_size=2, max_concurrency=2),
        users,
        videos,
        _OneBadUserGen(),
        progress_callback=snapshots.append,
    )

    assert result.total_work == 4
    assert len(result.quarantine) == 2
    assert snapshots[-1].completed_chunks == 4
    assert snapshots[-1].success_chunks == 2
    assert snapshots[-1].failed_chunks == 2
    assert snapshots[-1].quarantined_chunks == 2


def test_micro_work_structured_log_separates_pipeline_timings(tmp_path, caplog):
    checkpoint_rows = []

    def _checkpoint(work_id, work_order, drafts):
        checkpoint_rows.append((work_id, work_order, len(drafts)))

    def _progress(snapshot):
        return 3.25

    with caplog.at_level(logging.INFO, logger="autoresearch.action_log_generation.pipeline"):
        result = generate_action_log_drafts(
            _request(
                tmp_path,
                candidates_per_user=4,
                chunk_size=0,
                max_concurrency=1,
            ),
            _fixture_users(1),
            build_fixture_video_records(8),
            RuleBasedActionLogGenerator(),
            progress_callback=_progress,
            checkpoint_callback=_checkpoint,
            shard_index=3,
        )

    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.message.startswith("{")
    ]
    micro = [
        event
        for event in events
        if event["event"] == "action_log_micro_work_complete"
    ]

    assert result.total_work == 1
    assert checkpoint_rows[0][2] == 4
    assert len(micro) == 1
    payload = micro[0]
    assert payload["shard_index"] == 3
    assert payload["work_sequence"] == 0
    assert payload["checkpoint_rows"] == 4
    assert payload["progress_write_elapsed_ms"] == 3.25
    assert payload["completed_work"] == payload["total_work"] == 1
    assert payload["failed_work"] == payload["active_workers"] == 0
    assert payload["pending_work"] == 0
    for field in (
        "queue_wait_ms",
        "request_elapsed_ms",
        "parse_elapsed_ms",
        "checkpoint_write_elapsed_ms",
        "submit_elapsed_ms",
        "total_elapsed_ms",
        "throughput_per_min",
        "latency_p50_ms",
        "latency_p95_ms",
        "eta_seconds",
    ):
        assert payload[field] >= 0
    serialized = json.dumps(events, ensure_ascii=False)
    assert "user_id" not in serialized
    assert "vu_0000" not in serialized


def test_load_video_records_accepts_youtube_collection_schema(tmp_path):
    path = tmp_path / "youtube.parquet"
    table = pa.Table.from_pylist(
        [
            {
                "video_id": "yt1",
                "video_title": "정규화 영상",
                "video_description": "설명",
                "video_tags": ["태그1", "태그2"],
                "video_view_count": 1234,
                "video_like_count": 55,
                "video_comment_count": 6,
                "channel_title": "채널명",
                "video_published_at": datetime(2026, 7, 1, tzinfo=UTC),
            }
        ]
    )
    pq.write_table(table, path)

    from autoresearch.action_log_generation.video_source import load_video_records

    records = load_video_records(path)

    assert records == [
        {
            "video_id": "yt1",
            "title": "정규화 영상",
            "description": "설명",
            "tags": ["태그1", "태그2"],
            "view_count": 1234,
            "like_count": 55,
            "comment_count": 6,
            "channel_name": "채널명",
            "published_at": "2026-07-01 00:00:00+00:00",
        }
    ]


def test_candidate_provider_overrides_default_selection(tmp_path):
    """candidate_provider 주입 시 build_candidates 대신 주입된 후보만 판정한다."""
    users, videos = _fixture_users(2), build_fixture_video_records(10)
    fixed = [videos[0], videos[1]]  # 항상 같은 2개만 노출

    def provider(virtual_user, user_rng):
        return list(fixed)

    result = generate_action_log_drafts(
        _request(tmp_path), users, videos, RuleBasedActionLogGenerator(),
        candidate_provider=provider,
    )
    judged_pairs = {(d.user_id, d.video_id) for d in result.drafts}
    expected_video_ids = {str(v["video_id"]) for v in fixed}
    assert {pair[1] for pair in judged_pairs} <= expected_video_ids
    assert len(result.drafts) == 2 * len(users)


def test_expand_events_tags_exposure_metadata_and_prefix():
    from autoresearch.action_log_generation.pipeline import (
        ExposureMetadata,
        _expand_events,
        select_clicks_per_slate,
    )
    from autoresearch.action_log_generation.schema import SOURCE_ONLINE_SIMULATED, ImpressionDraft

    drafts = [
        ImpressionDraft(
            user_id="u1", video_id="v1", click_propensity=0.9,
            watch_fraction=0.5, would_like=False, duration_sec=100,
        ),
        ImpressionDraft(
            user_id="u1", video_id="v2", click_propensity=0.1,
            watch_fraction=0.5, would_like=False, duration_sec=100,
        ),
    ]
    # per-slate 커트라인 0.5: 슬레이트 최고(v1, 0.9)가 커트라인 이상이라 클릭됨
    clicked = select_clicks_per_slate(drafts, click_threshold=0.5)
    assert clicked == {0}

    metadata = {
        ("u1", "v1"): ExposureMetadata(
            policy="model", rank=1, ctr_score=0.9,
            is_exploration=False, policy_version="run-x",
        ),
        ("u1", "v2"): ExposureMetadata(
            policy="model", rank=2, ctr_score=0.1,
            is_exploration=True, policy_version="run-x",
        ),
    }
    request = EventGenerationRequest(click_threshold=0.55, seed=7)
    events = _expand_events(
        drafts, clicked, request,
        metadata=metadata, source=SOURCE_ONLINE_SIMULATED, event_id_prefix="evt_m",
    )
    impressions = [e for e in events if e.event_type == "impression"]
    assert len(impressions) == 2
    assert all(e.source == "online_simulated" for e in events)
    assert all(e.event_id.startswith("evt_m_") for e in events)
    v1_imp = next(e for e in impressions if e.video_id == "v1")
    assert (v1_imp.policy, v1_imp.rank, v1_imp.ctr_score) == ("model", 1, 0.9)
    v1_click = next(e for e in events if e.event_type == "click")
    assert v1_click.policy == "model"  # 세션 행에도 태깅
    v2_imp = next(e for e in impressions if e.video_id == "v2")
    assert v2_imp.is_exploration is True


def test_expand_events_without_metadata_is_unchanged():
    from autoresearch.action_log_generation.pipeline import _expand_events, select_clicks_per_slate
    from autoresearch.action_log_generation.schema import ImpressionDraft

    drafts = [
        ImpressionDraft(
            user_id="u1", video_id="v1", click_propensity=0.9,
            watch_fraction=0.5, would_like=False, duration_sec=100,
        ),
    ]
    # 커트라인을 최고 propensity보다 높게 잡아 클릭 0건(=impression만)인 경로를 검증한다.
    events = _expand_events(
        drafts, select_clicks_per_slate(drafts, click_threshold=1.0), EventGenerationRequest(click_threshold=0.55, seed=7)
    )
    assert re.fullmatch(r"evt_\d{8}_00000000", events[0].event_id)
    assert events[0].source == "historical"
    assert events[0].policy is None


def test_expand_events_respects_event_id_sequence_start() -> None:
    request = EventGenerationRequest(
        click_threshold=1.0,
        seed=7,
        history_end=_FIXED_END,
    )
    drafts = [
        ImpressionDraft(
            user_id="u1",
            video_id="v1",
            click_propensity=0.1,
            watch_fraction=0.5,
            would_like=False,
            duration_sec=100,
        )
    ]

    events = pipeline_module._expand_events(
        drafts,
        set(),
        request,
        event_id_sequence_start=17,
    )

    assert events[0].event_id.endswith("_00000017")


def test_expand_events_event_ids_are_date_namespaced_and_unique():
    # #295 A안: event_id = {prefix}_{이벤트 KST 날짜}_{seq}. 파티션(dt=KST 날짜)
    # 네임스페이스가 들어가므로 파티션 간 충돌이 구조적으로 불가능해진다.
    import re
    from datetime import datetime, timedelta, timezone

    from autoresearch.action_log_generation.pipeline import _expand_events, select_clicks_per_slate
    from autoresearch.action_log_generation.schema import EventGenerationRequest, ImpressionDraft

    kst = timezone(timedelta(hours=9))
    drafts = [
        ImpressionDraft(
            user_id=f"u{i}", video_id=f"v{i}", click_propensity=0.1,
            watch_fraction=0.5, would_like=False, duration_sec=100,
        )
        for i in range(3)
    ]
    request = EventGenerationRequest(
        click_threshold=0.55, seed=7,
        history_end=datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
    )
    events = _expand_events(drafts, select_clicks_per_slate(drafts, 1.0), request)

    assert len(events) == 3
    ids = [e.event_id for e in events]
    assert len(set(ids)) == len(ids)
    for event in events:
        match = re.fullmatch(r"evt_(\d{8})_(\d{8})", event.event_id)
        assert match, event.event_id
        expected_day = event.event_timestamp.astimezone(kst).strftime("%Y%m%d")
        assert match.group(1) == expected_day


def _tagged_draft(**overrides) -> ImpressionDraft:
    base = dict(
        user_id="u1", video_id="v1", click_propensity=0.9,
        watch_fraction=0.4, would_like=False, duration_sec=100,
        exposure_source="model", exposure_rank=3, exposure_ctr_score=0.7,
        policy_version="run-a",
    )
    base.update(overrides)
    return ImpressionDraft(**base)


def _daily_slate_request(tmp_path, **overrides) -> EventGenerationRequest:
    values = {
        "candidates_per_user": 2,
        "history_days": 1,
        "max_events_per_user_per_day": 2,
        "history_end": datetime(2026, 8, 31, 15, 0, tzinfo=UTC),
        "slate_context": SlateGenerationContext(
            partition_date=date(2026, 8, 31)
        ),
        **overrides,
    }
    return _request(tmp_path, **values)


def test_context_aware_events_share_slate_id_without_changing_projection(
    tmp_path,
) -> None:
    # Given
    drafts = [
        _tagged_draft(video_id="v1", exposure_rank=1, would_like=True),
        _tagged_draft(
            video_id="v2",
            exposure_source="trending",
            exposure_rank=2,
            click_propensity=0.1,
        ),
    ]
    aware_request = _daily_slate_request(tmp_path / "aware")
    legacy_request = aware_request.model_copy(update={"slate_context": None})

    # When
    aware = pipeline_module._expand_events(drafts, {0}, aware_request)
    legacy = pipeline_module._expand_events(drafts, {0}, legacy_request)

    # Then
    assert [event.model_dump(exclude={"slate_id"}) for event in aware] == [
        event.model_dump(exclude={"slate_id"}) for event in legacy
    ]
    assert [event.event_id for event in aware] == [event.event_id for event in legacy]
    assert len({event.slate_id for event in aware}) == 1
    assert aware[0].slate_id is not None
    assert all(
        event.event_timestamp.astimezone(pipeline_module._KST).date()
        == date(2026, 8, 31)
        for event in aware
    )
    assert all(event.slate_id is None for event in legacy)


def test_context_aware_ids_are_stable_and_differ_between_users(tmp_path) -> None:
    # Given
    drafts = [
        _tagged_draft(user_id="u1", video_id="v1", exposure_rank=1),
        _tagged_draft(user_id="u2", video_id="v1", exposure_rank=1),
    ]
    request = _daily_slate_request(tmp_path, candidates_per_user=1)

    # When
    first = pipeline_module._expand_events(drafts, set(), request)
    repeated = pipeline_module._expand_events(drafts, set(), request)

    # Then
    first_ids = {event.user_id: event.slate_id for event in first}
    repeated_ids = {event.user_id: event.slate_id for event in repeated}
    assert first_ids == repeated_ids
    assert first_ids["u1"] != first_ids["u2"]


def test_context_aware_expansion_rejects_actual_user_draft_overflow(
    tmp_path,
) -> None:
    # Given
    drafts = [
        _tagged_draft(video_id=f"v{index}", exposure_rank=index)
        for index in range(1, 4)
    ]
    request = _daily_slate_request(tmp_path)

    # When
    with pytest.raises(pipeline_module.ActionLogSlateContractError) as exc_info:
        pipeline_module._expand_events(drafts, set(), request)

    # Then
    assert exc_info.value.code == "slate_capacity_exceeded"


def test_context_aware_expansion_rejects_partition_date_mismatch(tmp_path) -> None:
    # Given
    request = _daily_slate_request(
        tmp_path,
        slate_context=SlateGenerationContext(partition_date=date(2026, 8, 30)),
    )

    # When
    with pytest.raises(pipeline_module.ActionLogSlateContractError) as exc_info:
        pipeline_module._expand_events([_tagged_draft(exposure_rank=1)], set(), request)

    # Then
    assert exc_info.value.code == "slate_partition_date_mismatch"


@pytest.mark.parametrize("rank", [None, 0, -1])
def test_context_aware_expansion_rejects_invalid_source_rank(
    tmp_path,
    rank: int | None,
) -> None:
    # Given
    draft = _tagged_draft().model_copy(update={"exposure_rank": rank})

    # When
    with pytest.raises(SlateIdentityError) as exc_info:
        pipeline_module._expand_events(
            [draft],
            set(),
            _daily_slate_request(tmp_path, candidates_per_user=1),
        )

    # Then
    assert exc_info.value.code is SlateIdentityErrorCode.INVALID_SLATE_EXPOSURE_RANK


def test_context_aware_expansion_rejects_duplicate_user_video(tmp_path) -> None:
    # Given
    drafts = [
        _tagged_draft(video_id="duplicate", exposure_rank=1),
        _tagged_draft(video_id="duplicate", exposure_rank=2),
    ]

    # When
    with pytest.raises(SlateIdentityError) as exc_info:
        pipeline_module._expand_events(
            drafts,
            set(),
            _daily_slate_request(tmp_path),
        )

    # Then
    assert exc_info.value.code is SlateIdentityErrorCode.DUPLICATE_SLATE_VIDEO


def _forced_collision_id(
    identity: SlateIdentity,
    *,
    registry: SlateIdentityRegistry | None = None,
) -> SlateId:
    assert registry is not None
    forced = SlateId("slt_20260831_000000000000000000000000")
    registry.register(forced, canonical_slate_json(identity))
    return forced


def test_batch_expansion_uses_one_collision_registry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(
        pipeline_module,
        "generate_slate_id",
        _forced_collision_id,
        raising=False,
    )
    drafts = [
        _tagged_draft(user_id="u1", video_id="v1", exposure_rank=1),
        _tagged_draft(user_id="u2", video_id="v1", exposure_rank=1),
    ]

    # When / Then
    with pytest.raises(SlateIdentityError) as exc_info:
        expand_action_log_drafts(
            _daily_slate_request(tmp_path, candidates_per_user=1),
            drafts,
        )
    assert exc_info.value.code is SlateIdentityErrorCode.SLATE_ID_COLLISION


def test_streaming_collision_preserves_existing_final_outputs(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(
        pipeline_module,
        "generate_slate_id",
        _forced_collision_id,
        raising=False,
    )
    request = _daily_slate_request(tmp_path)
    sentinels = {
        Path(request.output_path): b"existing-parquet",
        Path(request.warehouse_output_path): b"existing-warehouse",
        Path(request.quarantine_output_path): b"existing-quarantine",
    }
    for path, content in sentinels.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    # When
    with pytest.raises(SlateIdentityError) as exc_info:
        pipeline_module.generate_action_log_single(
            request,
            _fixture_users(2),
            build_fixture_video_records(4),
            RuleBasedActionLogGenerator(),
        )

    # Then
    assert exc_info.value.code is SlateIdentityErrorCode.SLATE_ID_COLLISION
    assert {path: path.read_bytes() for path in sentinels} == sentinels
    assert {path.name for path in tmp_path.iterdir()} == {
        path.name for path in sentinels
    }


def test_context_aware_batch_and_streaming_publish_same_event_projection(
    tmp_path,
) -> None:
    # Given
    users = _fixture_users(2)
    videos = build_fixture_video_records(4)
    batch_request = _daily_slate_request(tmp_path / "batch")
    streaming_request = _daily_slate_request(tmp_path / "streaming")

    # When
    generate_action_log_batch(
        batch_request,
        users,
        videos,
        RuleBasedActionLogGenerator(),
    )
    pipeline_module.generate_action_log_single(
        streaming_request,
        users,
        videos,
        RuleBasedActionLogGenerator(),
    )
    rows_by_mode = {
        mode: [
            json.loads(line)
            for line in Path(request.warehouse_output_path)
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        for mode, request in (
            ("batch", batch_request),
            ("streaming", streaming_request),
        )
    }

    # Then
    for request in (batch_request, streaming_request):
        parquet_rows = pq.read_table(request.output_path).to_pylist()
        assert all(row["slate_id"] is not None for row in parquet_rows)
    for rows in rows_by_mode.values():
        assert all(row["slate_id"] is not None for row in rows)
        assert all(
            len({row["slate_id"] for row in rows if row["user_id"] == user["user_id"]})
            == 1
            for user in users
        )
    assert [
        {
            key: value
            for key, value in row.items()
            if key not in {"generated_at", "slate_id"}
        }
        for row in rows_by_mode["batch"]
    ] == [
        {
            key: value
            for key, value in row.items()
            if key not in {"generated_at", "slate_id"}
        }
        for row in rows_by_mode["streaming"]
    ]


def test_draft_exposure_tags_roundtrip_parquet(tmp_path):
    drafts = [
        _tagged_draft(),
        _tagged_draft(video_id="v2", exposure_source="random",
                      exposure_rank=9, exposure_ctr_score=None),
    ]
    path = tmp_path / "drafts.parquet"
    write_action_log_draft_parquet(drafts, path)
    restored = read_action_log_draft_parquet(path)
    assert [d.exposure_source for d in restored] == ["model", "random"]
    assert restored[0].exposure_rank == 3 and restored[0].policy_version == "run-a"


def test_legacy_draft_parquet_without_tag_columns_reads_untagged(tmp_path):
    legacy_fields = [
        f for f in ACTION_LOG_DRAFT_PARQUET_SCHEMA
        if f.name not in ("exposure_source", "exposure_rank",
                          "exposure_ctr_score", "policy_version")
    ]
    row = {"user_id": "u1", "video_id": "v1", "click_propensity": 0.9,
           "watch_fraction": 0.4, "would_like": False, "duration_sec": 100}
    path = tmp_path / "legacy.parquet"
    pq.write_table(pa.Table.from_pylist([row], schema=pa.schema(legacy_fields)), path)
    restored = read_action_log_draft_parquet(path)
    assert restored[0].exposure_source is None


def test_attach_exposure_tags_leaves_unmapped_drafts_untagged():
    metadata = {
        ("u1", "v1"): ExposureMetadata(
            policy="model", rank=3, ctr_score=0.7, is_exploration=False,
            policy_version="run-a", exposure_source="model",
        )
    }
    plain = _tagged_draft(exposure_source=None, exposure_rank=None,
                          exposure_ctr_score=None, policy_version=None)
    other = _tagged_draft(video_id="vX", exposure_source=None, exposure_rank=None,
                          exposure_ctr_score=None, policy_version=None)
    tagged = attach_exposure_tags([plain, other], metadata)
    assert tagged[0].exposure_source == "model" and tagged[0].exposure_rank == 3
    assert tagged[1].exposure_source is None


def test_expand_events_joins_tags_from_draft_fallback(tmp_path):
    request = _request(tmp_path)
    drafts = [_tagged_draft(), _tagged_draft(video_id="v2", exposure_source="random",
                                             exposure_rank=2, exposure_ctr_score=None)]
    result = expand_action_log_drafts(request, drafts, [])
    impressions = [e for e in result.batch.events if e.event_type == "impression"]
    by_video = {e.video_id: e for e in impressions}
    assert by_video["v1"].exposure_source == "model"
    assert by_video["v1"].policy == "model" and by_video["v1"].rank == 3
    assert by_video["v1"].ctr_score == 0.7 and by_video["v1"].policy_version == "run-a"
    assert by_video["v2"].is_exploration is True


def test_batch_attaches_provider_exposure_tags(tmp_path):
    users, videos = _fixture_users(2), build_fixture_video_records(10)
    metadata: dict[tuple[str, str], ExposureMetadata] = {}

    def provider(virtual_user: dict, user_rng) -> list[dict]:
        picked = videos[:3]
        for position, video in enumerate(picked, start=1):
            metadata[(virtual_user["user_id"], str(video["video_id"]))] = (
                ExposureMetadata(
                    policy="model", rank=position, ctr_score=0.5,
                    is_exploration=False, policy_version="run-a",
                    exposure_source="model",
                )
            )
        return picked

    result = generate_action_log_batch(
        _request(tmp_path), users, videos, RuleBasedActionLogGenerator(),
        candidate_provider=provider, exposure_metadata=metadata,
    )
    impressions = [e for e in result.batch.events if e.event_type == "impression"]
    assert impressions and all(e.exposure_source == "model" for e in impressions)
    assert all(e.policy_version == "run-a" for e in impressions)


def _draft(user_id: str, video_id: str, cp: float) -> ImpressionDraft:
    return ImpressionDraft(
        user_id=user_id,
        video_id=video_id,
        click_propensity=cp,
        watch_fraction=0.5,
        would_like=False,
        duration_sec=100,
    )


def test_select_clicks_one_top_per_user_above_threshold() -> None:
    drafts = [
        _draft("u1", "a", 0.30),
        _draft("u1", "b", 0.80),  # u1 최고 → 클릭
        _draft("u2", "c", 0.40),  # u2 최고지만 커트라인 미만 → 클릭 없음
        _draft("u2", "d", 0.20),
    ]
    assert select_clicks_per_slate(drafts, 0.55) == {1}


def test_select_clicks_none_when_all_below_threshold() -> None:
    drafts = [_draft("u1", "a", 0.10), _draft("u1", "b", 0.20)]
    assert select_clicks_per_slate(drafts, 0.55) == set()


def test_select_clicks_threshold_is_inclusive() -> None:
    drafts = [_draft("u1", "a", 0.55)]
    assert select_clicks_per_slate(drafts, 0.55) == {0}


def test_select_clicks_tiebreak_is_deterministic_by_video_id() -> None:
    drafts = [_draft("u1", "b", 0.80), _draft("u1", "a", 0.80)]
    # 동점이면 video_id 작은 "a"(index 1)가 선택된다.
    assert select_clicks_per_slate(drafts, 0.55) == {1}


def test_select_clicks_handles_empty() -> None:
    assert select_clicks_per_slate([], 0.55) == set()


def test_expand_uses_per_slate_click_threshold() -> None:
    request = EventGenerationRequest(click_threshold=0.55)
    drafts = [
        _draft("u1", "a", 0.80),  # 클릭
        _draft("u1", "b", 0.30),
        _draft("u2", "c", 0.40),  # 커트라인 미만 → 클릭 없음
    ]
    result = expand_action_log_drafts(request, drafts)
    clicks = [e for e in result.batch.events if e.event_type == "click"]
    assert {c.video_id for c in clicks} == {"a"}


def test_event_generation_request_requires_click_threshold() -> None:
    """click_threshold 미지정 시 0.55로 조용히 채워지지 않고 fail-closed로 거부되어야 한다."""

    with pytest.raises(ValidationError):
        EventGenerationRequest()
