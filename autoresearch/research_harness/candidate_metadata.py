"""Candidate 피처 조립에 사용할 안전한 metadata의 순수 변환 경계.

[파이프라인] fixture 원본 수집과 학습·예측 피처 조립 사이에서 공개 열과 관측 시점을 검증한다.
[기능] 허용 열만 정규화하고, 요청 시각까지 관측된 가장 최근 이력을 선택한다.
[비책임] 파일 게시·접근 제어는 candidate_data_view, cold-start 피처 채우기와
임베딩·학습은 후속 Task 6 소비자의 책임이다. 외부 서비스와 파일에 접근하지 않는다.
"""

from bisect import bisect_right
from datetime import UTC, datetime
import re
from typing import Annotated, Self

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from autoresearch.feature_engineering.category_reference import CATEGORY_DESCRIPTIONS
from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode


_TS = pa.timestamp("us", tz="UTC")
_WORDS = pa.list_(pa.field("item", pa.string(), nullable=False))
_COUNT = Annotated[int, Field(ge=0, le=2**63 - 1)]
_IDENTIFIER = Annotated[str, Field(min_length=1, pattern=r"\S")]
_DURATION = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", re.ASCII)
_KEYWORDS = ("hobby_keywords", "interest_keywords", "lifestyle_keywords", "primary_categories")
_COUNTS = (
    "view_count", "like_count", "comment_count", "channel_subscriber_count",
    "channel_view_count", "channel_video_count",
)
_USER_SCHEMA = pa.schema([
    pa.field("user_id", pa.string(), nullable=False),
    pa.field("available_at", _TS, nullable=False),
    pa.field("age", pa.int64(), nullable=False),
    pa.field("occupation", pa.string(), nullable=False),
    pa.field("watch_time_band", pa.string(), nullable=False),
    *[pa.field(name, _WORDS, nullable=False) for name in _KEYWORDS],
])
_VIDEO_SCHEMA = pa.schema([
    pa.field("video_id", pa.string(), nullable=False),
    pa.field("available_at", _TS, nullable=False),
    pa.field("category_id", pa.string(), nullable=False),
    pa.field("duration_sec", pa.int64(), nullable=False),
    pa.field("published_at", _TS, nullable=False),
    *[pa.field(name, pa.int64(), nullable=False) for name in _COUNTS],
])
_RAW_USER_SCHEMA = pa.schema([
    pa.field("user_id", pa.string()), pa.field("generated_at", pa.string()),
    *list(_USER_SCHEMA)[2:],
])
_RAW_VIDEO_SCHEMA = pa.schema([
    pa.field("video_id", pa.string()), pa.field("collected_at", _TS),
    pa.field("video_trending_date", _TS), pa.field("video_category", pa.string()),
    pa.field("video_duration", pa.string()), pa.field("video_published_at", _TS),
    *[pa.field(f"video_{name}" if not name.startswith("channel_") else name, pa.int64())
      for name in _COUNTS],
])


class _MetadataRow(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    available_at: datetime

    @field_validator("available_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("timezone required")
        return value.astimezone(UTC)


class _UserRow(_MetadataRow):
    user_id: _IDENTIFIER
    age: _COUNT
    occupation: str
    watch_time_band: str
    hobby_keywords: list[str]
    interest_keywords: list[str]
    lifestyle_keywords: list[str]
    primary_categories: list[str]

    @field_validator("primary_categories")
    @classmethod
    def require_known_categories(cls, value: list[str]) -> list[str]:
        if any(category not in CATEGORY_DESCRIPTIONS for category in value):
            raise ValueError("unknown category")
        return value


class _VideoRow(_MetadataRow):
    video_id: _IDENTIFIER
    category_id: str
    duration_sec: Annotated[int, Field(gt=0, le=2**63 - 1)]
    published_at: datetime
    view_count: _COUNT
    like_count: _COUNT
    comment_count: _COUNT
    channel_subscriber_count: _COUNT
    channel_view_count: _COUNT
    channel_video_count: _COUNT

    @model_validator(mode="after")
    def require_video_semantics(self) -> Self:
        if (
            self.category_id not in CATEGORY_DESCRIPTIONS
            or self.published_at.utcoffset() is None
            or self.published_at > self.available_at
        ):
            raise ValueError("invalid video observation")
        return self


def _conflict(stage: str) -> StageCError:
    return StageCError(StageCErrorCode.CANDIDATE_VIEW_CONFLICT, stage)


def _project_rows(
    table: pa.Table, schema: pa.Schema, *, exact: bool = False,
) -> list[dict[str, object]]:
    """Arrow 타입은 강제 변환하지 않으며, 원본의 nullable 선언은 값 검증으로 보완한다."""
    if not isinstance(table, pa.Table) or len(set(table.column_names)) != len(table.column_names):
        raise _conflict("metadata_schema_validation")
    if exact and table.column_names != schema.names:
        raise _conflict("metadata_schema_validation")
    for field in schema:
        if field.name not in table.column_names:
            raise _conflict("metadata_schema_validation")
        actual = table.schema.field(field.name).type
        compatible = actual == field.type
        if pa.types.is_list(field.type):
            compatible = pa.types.is_list(actual) and actual.value_type == field.type.value_type
        if not compatible or table[field.name].null_count:
            raise _conflict("metadata_schema_validation")
    try:
        return table.select(schema.names).to_pylist()
    except (ValueError, OverflowError, pa.ArrowException):
        raise _conflict("metadata_value_validation") from None


def _checked_rows(
    rows: list[dict[str, object]], model: type[_UserRow] | type[_VideoRow], entity_key: str,
) -> list[dict[str, object]]:
    try:
        checked = [model.model_validate(row).model_dump() for row in rows]
    except (ValidationError, OverflowError):
        raise _conflict("metadata_value_validation") from None
    checked.sort(key=lambda row: (row[entity_key], row["available_at"]))
    keys = [(row[entity_key], row["available_at"]) for row in checked]
    if len(set(keys)) != len(keys):
        raise _conflict("metadata_duplicate_key_validation")
    return checked


def normalize_user_metadata(raw: pa.Table) -> pa.Table:
    """원본 사용자 열을 검증하고 UTC 관측 이력으로 정규화한다.

    Args:
        raw: generated_at과 명시적인 선호 category를 포함한 원본 Arrow table.

    Returns:
        원본 부가 열을 제외하고 (user_id, available_at)으로 정렬한 table.

    Raises:
        StageCError: 필수 열·타입·값이 잘못되거나 관측 키가 중복인 경우.
    """
    rows = _project_rows(raw, _RAW_USER_SCHEMA)
    try:
        for row in rows:
            row["available_at"] = datetime.fromisoformat(row.pop("generated_at"))
    except (ValueError, OverflowError):
        raise _conflict("metadata_timestamp_validation") from None
    return pa.Table.from_pylist(_checked_rows(rows, _UserRow, "user_id"), schema=_USER_SCHEMA)


def normalize_video_metadata(raw: pa.Table) -> pa.Table:
    """원본 영상 열을 검증하고 두 관측 시각 중 늦은 시각을 available_at으로 쓴다.

    Args:
        raw: 수집·trending 시각, ISO 8601 PT duration과 비음수 count를 가진 table.

    Returns:
        허용 열만 포함한 (video_id, available_at) 정렬 table.

    Raises:
        StageCError: 필수 열·타입·값이 잘못되거나 관측 키가 중복인 경우.
    """
    rows: list[dict[str, object]] = []
    for raw_row in _project_rows(raw, _RAW_VIDEO_SCHEMA):
        match = _DURATION.fullmatch(raw_row["video_duration"])
        if match is None:
            raise _conflict("metadata_duration_validation")
        try:
            hours, minutes, seconds = (int(value or 0) for value in match.groups())
        except ValueError:
            raise _conflict("metadata_duration_validation") from None
        rows.append({
            "video_id": raw_row["video_id"],
            "available_at": max(raw_row["collected_at"], raw_row["video_trending_date"]),
            "category_id": raw_row["video_category"],
            "duration_sec": hours * 3600 + minutes * 60 + seconds,
            "published_at": raw_row["video_published_at"],
            **{name: raw_row[f"video_{name}" if not name.startswith("channel_") else name]
               for name in _COUNTS},
        })
    return pa.Table.from_pylist(_checked_rows(rows, _VideoRow, "video_id"), schema=_VIDEO_SCHEMA)


def select_metadata_as_of(
    metadata: pa.Table, requests: pa.Table, *, entity_key: str,
) -> pa.Table:
    """요청별로 available_at <= event_timestamp인 가장 최근 관측을 선택한다.

    Args:
        metadata: 정규화된 사용자 또는 영상 이력. 입력 정렬은 요구하지 않는다.
        requests: entity_key, event_timestamp 두 열의 요청 table.
        entity_key: user_id 또는 video_id.

    Returns:
        요청 순서·중복을 보존한 table. 미관측은 metadata_missing=True와 null로 표시한다.

    Raises:
        StageCError: 잘못된 요청, metadata 값·schema 또는 중복 관측 키인 경우.
    """
    if entity_key not in ("user_id", "video_id"):
        raise _conflict("metadata_entity_validation")
    schema, model = (_USER_SCHEMA, _UserRow) if entity_key == "user_id" else (_VIDEO_SCHEMA, _VideoRow)
    rows = _checked_rows(_project_rows(metadata, schema, exact=True), model, entity_key)
    request_schema = pa.schema([
        pa.field(entity_key, pa.string(), nullable=False),
        pa.field("event_timestamp", _TS, nullable=False),
    ])
    request_rows = _project_rows(requests, request_schema, exact=True)
    by_entity: dict[str, list[dict[str, object]]] = {}
    times: dict[str, list[datetime]] = {}
    for row in rows:
        identifier = row[entity_key]
        by_entity.setdefault(identifier, []).append(row)
        times.setdefault(identifier, []).append(row["available_at"])
    selected: list[dict[str, object]] = []
    for request in request_rows:
        identifier = request[entity_key]
        if not identifier.strip():
            raise _conflict("metadata_request_validation")
        index = bisect_right(times.get(identifier, []), request["event_timestamp"]) - 1
        observation = by_entity[identifier][index] if index >= 0 else {}
        selected.append({
            **request,
            **{field.name: observation.get(field.name) for field in schema if field.name != entity_key},
            "metadata_missing": index < 0,
        })
    output_schema = pa.schema([
        *request_schema,
        *[field.with_nullable(True) for field in schema if field.name != entity_key],
        pa.field("metadata_missing", pa.bool_(), nullable=False),
    ])
    return pa.Table.from_pylist(selected, schema=output_schema)
