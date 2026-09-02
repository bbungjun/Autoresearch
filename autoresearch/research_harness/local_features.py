"""안전한 candidate metadata와 raw 행동 로그의 로컬 baseline 피처 조립.

[파이프라인] candidate view 게시 뒤, CTR 모델 재학습·예측 앞의 순수 피처 계산 구간이다.
[기능] 시점별 metadata와 KST 일별 raw 이벤트 집계를 기존 21개 피처 순서로 조립하고,
metadata 누락과 제공 이력 구간 coverage를 행별 진단으로 분리한다.
[비책임] 파일·partition 검증은 candidate_data_view/호출 loader, 라벨·학습·예측은
후속 학습 CLI, 모델 로딩·GPU 실행은 TextEmbedder adapter가 담당한다.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal, Self

import numpy as np
import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from autoresearch.feature_engineering.category_reference import CATEGORY_DESCRIPTIONS
from autoresearch.feature_engineering.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS, COLD_START_CATEGORICAL_DEFAULT, FeatureContractError,
    MODEL_FEATURE_COLUMNS,
)
from autoresearch.research_harness.candidate_metadata import select_metadata_as_of
from autoresearch.research_harness.embedding import TextEmbedder, encode_normalized


_KST = timezone(timedelta(hours=9))
_TS = pa.timestamp("us", tz="UTC")
_ID = Annotated[str, Field(min_length=1, pattern=r"\S")]
_REQUEST_SCHEMA = pa.schema([
    pa.field("user_id", pa.string(), nullable=False),
    pa.field("video_id", pa.string(), nullable=False),
    pa.field("event_timestamp", _TS, nullable=False),
])
_HISTORY_SCHEMA = pa.schema([
    *_REQUEST_SCHEMA, pa.field("event_id", pa.string(), nullable=False),
    pa.field("event_type", pa.string(), nullable=False), pa.field("watch_time_sec", pa.int64()),
])
_FLOATS = {"like_ratio", "comment_ratio", "topic_similarity"}
_FEATURE_SCHEMA = pa.schema([
    pa.field(name, pa.string() if name in CATEGORICAL_FEATURE_COLUMNS else
             pa.float64() if name in _FLOATS else pa.int64(), nullable=False)
    for name in MODEL_FEATURE_COLUMNS
])
_DIAGNOSTIC_SCHEMA = pa.schema([
    pa.field(name, pa.bool_(), nullable=False) for name in (
        "user_metadata_missing", "video_metadata_missing", "history_7d_complete", "history_30d_complete",
    )
])


class _Request(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    user_id: _ID
    video_id: _ID
    event_timestamp: datetime


class _Event(_Request):
    event_id: _ID
    event_type: Literal["impression", "click", "view", "like"]
    watch_time_sec: Annotated[int, Field(ge=0, le=2**63 - 1)] | None

    @model_validator(mode="after")
    def validate_watch_time(self) -> Self:
        if (self.event_type == "view") != (self.watch_time_sec is not None):
            raise ValueError("watch_time_event_type_mismatch")
        return self


@dataclass(frozen=True)
class LocalFeatureBatch:
    """동일 입력 행 순서의 모델 피처와 모델에 넣지 않는 진단."""

    features: pa.Table
    diagnostics: pa.Table


@dataclass
class _Daily:
    clicks: int = 0
    views: int = 0
    watch: int = 0
    likes: int = 0
    total: int = 0
    categories: Counter[str] = field(default_factory=Counter)


def _rows(table: pa.Table, schema: pa.Schema) -> list[dict[str, object]]:
    if not isinstance(table, pa.Table) or len(set(table.column_names)) != len(table.column_names):
        raise FeatureContractError("local_feature_schema_invalid")
    for column in schema:
        if (
            column.name not in table.column_names or table.schema.field(column.name).type != column.type
            or (not column.nullable and table[column.name].null_count)
        ):
            raise FeatureContractError("local_feature_schema_invalid")
    try:
        return table.select(schema.names).to_pylist()
    except (ValueError, OverflowError, pa.ArrowException):
        raise FeatureContractError("local_feature_values_invalid") from None


def _watch_band(value: str) -> str:
    value = value.strip().lower()
    for normalized, aliases in (
        ("morning", ("morning", "am", "오전", "아침")),
        ("evening", ("evening", "pm", "저녁", "오후")),
        ("night", ("night", "late_night", "밤", "심야")),
    ):
        if value in aliases:
            return normalized
    return COLD_START_CATEGORICAL_DEFAULT


def _daily_history(events: list[_Event], history: pa.Table, videos: pa.Table) -> dict[tuple[str, date], _Daily]:
    categories = select_metadata_as_of(
        videos, history.select(["video_id", "event_timestamp"]), entity_key="video_id",
    )["category_id"].to_pylist()
    daily: dict[tuple[str, date], _Daily] = {}
    for event, category in zip(events, categories, strict=True):
        key = (event.user_id, event.event_timestamp.astimezone(_KST).date())
        aggregate = daily.setdefault(key, _Daily())
        aggregate.total += 1
        aggregate.clicks += event.event_type == "click"
        aggregate.views += event.event_type == "view"
        aggregate.likes += event.event_type == "like"
        aggregate.watch += event.watch_time_sec or 0
        if event.event_type != "impression" and category is not None:
            aggregate.categories[category] += 1
    return daily


def _window(daily: dict[tuple[str, date], _Daily], user: str, day: date) -> dict[str, object]:
    recent = _Daily()
    affinity: Counter[str] = Counter()
    for offset in range(1, 31):
        aggregate = daily.get((user, day - timedelta(days=offset)))
        if aggregate is None:
            continue
        affinity.update(aggregate.categories)
        if offset <= 7:
            recent.clicks += aggregate.clicks
            recent.views += aggregate.views
            recent.watch += aggregate.watch
            recent.likes += aggregate.likes
            recent.total += aggregate.total
    return {
        "recent_click_count_7d": recent.clicks, "recent_view_count_7d": recent.views,
        "recent_watch_time_7d": recent.watch, "recent_like_count_7d": recent.likes,
        "total_event_count_7d": recent.total,
        "historical_category_affinity": min(affinity, key=lambda cat: (-affinity[cat], cat)) if affinity else COLD_START_CATEGORICAL_DEFAULT,
    }


def _similarities(
    users: list[dict[str, object]], videos: list[dict[str, object]], embedding: TextEmbedder,
) -> list[float]:
    # Unique texts are encoded once per call; persistence/model identity belongs to the adapter.
    keywords_by_row = [
        [word for name in ("hobby_keywords", "interest_keywords", "lifestyle_keywords")
         for word in (user[name] or []) if word.strip()] for user in users
    ]
    pairs = [(words, video["category_id"]) for words, video in zip(keywords_by_row, videos, strict=True)]
    keywords = list(dict.fromkeys(word for words, category in pairs if category is not None for word in words))
    categories = list(dict.fromkeys(category for words, category in pairs if words and category is not None))
    if not keywords or not categories:
        return [0.0] * len(users)
    queries = encode_normalized(embedding, keywords, role="query")
    documents = encode_normalized(
        embedding, [CATEGORY_DESCRIPTIONS[category] for category in categories], role="document",
        expected_dimension=queries.shape[1],
    )
    by_keyword = dict(zip(keywords, queries, strict=True))
    by_category = dict(zip(categories, documents, strict=True))
    return [
        round(float(np.clip(max(np.dot(by_keyword[word], by_category[category]) for word in words), -1.0, 1.0)), 4)
        if words and category is not None else 0.0 for words, category in pairs
    ]


def build_local_features(
    requests: pa.Table, *, history: pa.Table, users: pa.Table, videos: pa.Table,
    embedding: TextEmbedder, evaluation_start_date: date, history_start_date: date,
) -> LocalFeatureBatch:
    """요청별 as-of metadata와 KST 당일 제외 행동을 모델 피처로 변환한다.

    Args:
        requests: user_id/video_id/event_timestamp[us, UTC]. 추가 열은 모델 입력에서 제외한다.
        history: [history_start_date, evaluation_start_date) KST의 raw long-format 이벤트.
        users: 정규화한 사용자 metadata 이력.
        videos: 정규화한 영상 metadata 이력.
        embedding: keyword query와 category document를 임베딩하는 adapter.
        evaluation_start_date: 허용 행동 로그 구간의 배타적 상한 T.
        history_start_date: loader가 검증한 연속 partition 구간의 시작일.

    Returns:
        요청 순서·중복을 유지한 21열 features와 행별 진단. 이력 coverage는 제공 구간의
        길이에 관한 값이며 이 함수가 파일의 존재·완전성을 검사했다는 뜻은 아니다.

    Raises:
        FeatureContractError: 입력 schema/행/구간 또는 임베딩이 계약을 위반한 경우.
        StageCError: metadata 자체가 기존 as-of 계약을 위반한 경우.
    """
    if type(evaluation_start_date) is not date or type(history_start_date) is not date or history_start_date >= evaluation_start_date:
        raise FeatureContractError("local_feature_history_bounds_invalid")
    try:
        query_rows = [_Request.model_validate(row) for row in _rows(requests, _REQUEST_SCHEMA)]
        event_rows = [_Event.model_validate(row) for row in _rows(history, _HISTORY_SCHEMA)]
    except ValidationError:
        raise FeatureContractError("local_feature_values_invalid") from None
    if len({event.event_id for event in event_rows}) != len(event_rows):
        raise FeatureContractError("local_feature_duplicate_event")
    try:
        outside_bounds = any(
            not history_start_date <= event.event_timestamp.astimezone(_KST).date() < evaluation_start_date
            for event in event_rows
        )
    except (ValueError, OverflowError):
        raise FeatureContractError("local_feature_history_bounds_invalid") from None
    if outside_bounds:
        raise FeatureContractError("local_feature_history_bounds_invalid")
    user_rows = select_metadata_as_of(users, requests.select(["user_id", "event_timestamp"]), entity_key="user_id").to_pylist()
    video_rows = select_metadata_as_of(videos, requests.select(["video_id", "event_timestamp"]), entity_key="video_id").to_pylist()
    daily = _daily_history(event_rows, history, videos)
    similarities = _similarities(user_rows, video_rows, embedding)
    windows: dict[tuple[str, date], dict[str, object]] = {}
    output: list[dict[str, object]] = []
    diagnostics: list[dict[str, bool]] = []
    try:
        for query, user, video, similarity in zip(query_rows, user_rows, video_rows, similarities, strict=True):
            day = query.event_timestamp.astimezone(_KST).date()
            key = (query.user_id, day)
            if key not in windows:
                windows[key] = _window(daily, query.user_id, day)
            row = {name: COLD_START_CATEGORICAL_DEFAULT if name in CATEGORICAL_FEATURE_COLUMNS else 0 for name in MODEL_FEATURE_COLUMNS}
            row.update(windows[key])
            if not user["metadata_missing"]:
                age = user["age"]
                row.update(age_group=f"{max(1, age // 10)}0s" if age < 50 else "50s+",
                           occupation=user["occupation"], watch_time_band=_watch_band(user["watch_time_band"]))
            if not video["metadata_missing"]:
                row.update({name: video[name] for name in (
                    "category_id", "duration_sec", "view_count", "channel_subscriber_count",
                    "channel_view_count", "channel_video_count",
                )})
                row.update(
                    like_ratio=video["like_count"] / video["view_count"] if video["view_count"] else 0.0,
                    comment_ratio=video["comment_count"] / video["view_count"] if video["view_count"] else 0.0,
                    days_since_upload=(video["available_at"].astimezone(_KST).date() - video["published_at"].astimezone(_KST).date()).days,
                )
            row.update(
                topic_similarity=similarity,
                preferred_category_match=int(row["category_id"] in (user["primary_categories"] or [])),
                historical_category_match=int(row["historical_category_affinity"] != COLD_START_CATEGORICAL_DEFAULT and row["historical_category_affinity"] == row["category_id"]),
            )
            output.append(row)
            diagnostics.append({
                "user_metadata_missing": user["metadata_missing"], "video_metadata_missing": video["metadata_missing"],
                "history_7d_complete": history_start_date <= day - timedelta(days=7) and day <= evaluation_start_date,
                "history_30d_complete": history_start_date <= day - timedelta(days=30) and day <= evaluation_start_date,
            })
        return LocalFeatureBatch(pa.Table.from_pylist(output, schema=_FEATURE_SCHEMA), pa.Table.from_pylist(diagnostics, schema=_DIAGNOSTIC_SCHEMA))
    except (OverflowError, pa.ArrowException):
        raise FeatureContractError("local_feature_output_invalid") from None
