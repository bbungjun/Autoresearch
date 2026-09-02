"""Task 6 metadata 계약 테스트의 독립 입력과 손 계산 기대 schema.

[파이프라인] fixture 원본에서 candidate 피처 조립으로 넘어가는 seam의 테스트 재료다.
[기능] 작은 Arrow 입력과 명시적인 기대 schema를 만들며 제품 변환 함수를 재사용하지 않는다.
[비책임] metadata 검증·변환·시점 조인·파일 게시는 제품 코드가 담당한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib import import_module
from importlib.util import find_spec
from types import ModuleType

import pyarrow as pa


AT = datetime(2026, 8, 30, tzinfo=UTC)
TS = pa.timestamp("us", tz="UTC")
WORDS = pa.list_(pa.field("item", pa.string(), nullable=False))

USER_SCHEMA = pa.schema([
    pa.field("user_id", pa.string(), nullable=False),
    pa.field("available_at", TS, nullable=False),
    pa.field("age", pa.int64(), nullable=False),
    pa.field("occupation", pa.string(), nullable=False),
    pa.field("watch_time_band", pa.string(), nullable=False),
    pa.field("hobby_keywords", WORDS, nullable=False),
    pa.field("interest_keywords", WORDS, nullable=False),
    pa.field("lifestyle_keywords", WORDS, nullable=False),
    pa.field("primary_categories", WORDS, nullable=False),
])
VIDEO_SCHEMA = pa.schema([
    pa.field("video_id", pa.string(), nullable=False),
    pa.field("available_at", TS, nullable=False),
    pa.field("category_id", pa.string(), nullable=False),
    pa.field("duration_sec", pa.int64(), nullable=False),
    pa.field("published_at", TS, nullable=False),
    pa.field("view_count", pa.int64(), nullable=False),
    pa.field("like_count", pa.int64(), nullable=False),
    pa.field("comment_count", pa.int64(), nullable=False),
    pa.field("channel_subscriber_count", pa.int64(), nullable=False),
    pa.field("channel_view_count", pa.int64(), nullable=False),
    pa.field("channel_video_count", pa.int64(), nullable=False),
])

RAW_USER_SCHEMA = pa.schema([
    pa.field("user_id", pa.string()),
    pa.field("generated_at", pa.string()),
    pa.field("age", pa.int64()),
    pa.field("occupation", pa.string()),
    pa.field("watch_time_band", pa.string()),
    *[pa.field(name, pa.list_(pa.string())) for name in (
        "hobby_keywords", "interest_keywords", "lifestyle_keywords", "primary_categories",
    )],
    pa.field("source_persona_json", pa.string()),
    pa.field("source_hash", pa.string()),
])
RAW_VIDEO_SCHEMA = pa.schema([
    pa.field("video_id", pa.string()),
    pa.field("collected_at", TS),
    pa.field("video_trending_date", TS),
    pa.field("video_category", pa.string()),
    pa.field("video_duration", pa.string()),
    pa.field("video_published_at", TS),
    *[pa.field(name, pa.int64()) for name in (
        "video_view_count", "video_like_count", "video_comment_count",
        "channel_subscriber_count", "channel_view_count", "channel_video_count",
    )],
    pa.field("video_title", pa.string()),
])


def metadata_module() -> ModuleType:
    """제품 module 부재를 collection 오류/skip 대신 명시적인 RED assertion으로 표시한다."""
    name = "autoresearch.research_harness.candidate_metadata"
    assert find_spec(name) is not None, "Task 6 RED: candidate_metadata 구현이 필요합니다"
    return import_module(name)


def user_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "user_id": "user-a", "generated_at": AT.isoformat(), "age": 25,
        "occupation": "student", "watch_time_band": "evening",
        "hobby_keywords": ["기타"], "interest_keywords": ["음악"],
        "lifestyle_keywords": [], "primary_categories": ["Music"],
        "source_persona_json": "DO_NOT_EXPORT_PERSONA",
        "source_hash": "DO_NOT_EXPORT_SOURCE_HASH",
    }
    row.update(changes)
    return row


def video_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "video_id": "video-a", "collected_at": AT, "video_trending_date": AT,
        "video_category": "Music", "video_duration": "PT5M",
        "video_published_at": datetime(2026, 8, 1, tzinfo=UTC),
        "video_view_count": 100, "video_like_count": 10, "video_comment_count": 2,
        "channel_subscriber_count": 40, "channel_view_count": 1000,
        "channel_video_count": 8, "video_title": "DO_NOT_EXPORT_TITLE",
    }
    row.update(changes)
    return row


def raw_users(*rows: dict[str, object]) -> pa.Table:
    return pa.Table.from_pylist(list(rows) if rows else [user_row()], schema=RAW_USER_SCHEMA)


def raw_videos(*rows: dict[str, object]) -> pa.Table:
    return pa.Table.from_pylist(list(rows) if rows else [video_row()], schema=RAW_VIDEO_SCHEMA)


def normalized_users(*rows: dict[str, object]) -> pa.Table:
    base: dict[str, object] = {
        "user_id": "user-a", "available_at": AT, "age": 25,
        "occupation": "student", "watch_time_band": "evening",
        "hobby_keywords": ["기타"], "interest_keywords": ["음악"],
        "lifestyle_keywords": [], "primary_categories": ["Music"],
    }
    return pa.Table.from_pylist(
        [{**base, **row} for row in rows] if rows else [base], schema=USER_SCHEMA,
    )


def normalized_videos(*rows: dict[str, object]) -> pa.Table:
    base: dict[str, object] = {
        "video_id": "video-a", "available_at": AT, "category_id": "Music",
        "duration_sec": 300, "published_at": datetime(2026, 8, 1, tzinfo=UTC),
        "view_count": 100, "like_count": 10, "comment_count": 2,
        "channel_subscriber_count": 40, "channel_view_count": 1000,
        "channel_video_count": 8,
    }
    return pa.Table.from_pylist(
        [{**base, **row} for row in rows] if rows else [base], schema=VIDEO_SCHEMA,
    )
