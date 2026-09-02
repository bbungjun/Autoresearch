"""Task 6 metadata 시점 조인의 손 계산 golden 테스트.

[파이프라인] 허용된 metadata 이력에서 학습·예측 impression 시점의 재료를 선택한다.
[기능] 미래 값 배제, 동시각 포함, 입력 순서·중복 요청 보존과 정상 미관측을 검증한다.
[비책임] cold-start 피처 채우기·LightGBM 학습·metadata 파일 게시는 후속 소비자 책임이다.
"""

from datetime import timedelta

import pyarrow as pa
import pytest

from autoresearch.research_harness.fixture_errors import StageCError
from tests.research_harness.metadata_cases import (
    AT, TS, metadata_module, normalized_users, normalized_videos,
)


def _requests(entity_key: str, rows: list[tuple[str, object]]) -> pa.Table:
    return pa.table({
        entity_key: pa.array([row[0] for row in rows], type=pa.string()),
        "event_timestamp": pa.array([row[1] for row in rows], type=TS),
    })


@pytest.mark.parametrize("entity,key,value", [
    ("user", "user_id", "age"), ("video", "video_id", "view_count"),
])
def test_as_of_golden_preserves_order_and_uses_no_future_row(
    entity: str, key: str, value: str,
) -> None:
    later = AT + timedelta(hours=1)
    initial, updated = (25, 26) if entity == "user" else (100, 120)
    factory = normalized_users if entity == "user" else normalized_videos
    metadata = factory(
        {"available_at": later, value: updated}, {},  # 정렬되지 않은 입력도 시점 기준으로 선택
        {key: f"{entity}-unrequested"},
    )
    requests = _requests(key, [
        (f"{entity}-a", later + timedelta(hours=1)),
        (f"{entity}-a", AT - timedelta(microseconds=1)),
        (f"{entity}-a", later),
        (f"{entity}-a", AT),
        (f"{entity}-absent", AT),
        (f"{entity}-a", AT),
        (f"{entity}-a", later - timedelta(microseconds=1)),
    ])
    result = metadata_module().select_metadata_as_of(metadata, requests, entity_key=key)

    assert result[key].to_pylist() == requests[key].to_pylist()
    assert result["event_timestamp"].to_pylist() == requests["event_timestamp"].to_pylist()
    assert result[value].to_pylist() == [updated, None, updated, initial, None, initial, initial]
    assert result["available_at"].to_pylist() == [later, None, later, AT, None, AT, AT]
    assert result["metadata_missing"].to_pylist() == [False, True, False, False, True, False, False]
    assert result.column_names == [
        *requests.column_names,
        *[name for name in metadata.column_names if name != key],
        "metadata_missing",
    ]
    assert result.schema.field("metadata_missing").type == pa.bool_()
    assert metadata.num_rows == 3


def test_kst_midnight_does_not_see_metadata_from_nine_hours_later() -> None:
    # UTC 00:00 관측은 KST 09:00이다. KST 같은 날짜라는 이유로 00:00에 소급하면 안 된다.
    midnight_kst = AT - timedelta(hours=9)
    requests = _requests("video_id", [("video-a", midnight_kst), ("video-a", AT)])
    result = metadata_module().select_metadata_as_of(
        normalized_videos(), requests, entity_key="video_id",
    )
    assert result["view_count"].to_pylist() == [None, 100]
    assert result["metadata_missing"].to_pylist() == [True, False]


def test_zero_observed_count_is_not_treated_as_missing() -> None:
    result = metadata_module().select_metadata_as_of(
        normalized_videos({"view_count": 0}),
        _requests("video_id", [("video-a", AT)]), entity_key="video_id",
    )
    assert result["view_count"].to_pylist() == [0]
    assert result["metadata_missing"].to_pylist() == [False]


@pytest.mark.parametrize("entity", ["user", "video"])
def test_empty_metadata_is_valid_cold_start_not_an_empty_result(entity: str) -> None:
    key = f"{entity}_id"
    metadata = (normalized_users() if entity == "user" else normalized_videos()).slice(0, 0)
    result = metadata_module().select_metadata_as_of(
        metadata, _requests(key, [(f"{entity}-a", AT)]), entity_key=key,
    )
    assert result.num_rows == 1
    assert result[key].to_pylist() == [f"{entity}-a"]
    assert result["available_at"].to_pylist() == [None]
    assert result["metadata_missing"].to_pylist() == [True]


def test_empty_requests_return_empty_table_with_stable_columns() -> None:
    result = metadata_module().select_metadata_as_of(
        normalized_users(), _requests("user_id", []), entity_key="user_id",
    )
    assert result.num_rows == 0
    assert "metadata_missing" in result.column_names
    assert "primary_categories" in result.column_names


@pytest.mark.parametrize("invalid", ["naive_time", "null_time", "missing_key", "empty_key"])
def test_invalid_request_is_not_cold_start(invalid: str) -> None:
    select = metadata_module().select_metadata_as_of
    valid = _requests("user_id", [("user-a", AT)])
    assert select(normalized_users(), valid, entity_key="user_id").num_rows == 1
    if invalid == "naive_time":
        requests = valid.set_column(1, "event_timestamp", pa.array(
            [AT.replace(tzinfo=None)], type=pa.timestamp("us"),
        ))
    elif invalid == "null_time":
        requests = _requests("user_id", [("user-a", None)])
    elif invalid == "empty_key":
        requests = _requests("user_id", [("", AT)])
    else:
        requests = valid.drop(["user_id"])
    with pytest.raises(StageCError):
        select(normalized_users(), requests, entity_key="user_id")


def test_duplicate_metadata_key_is_not_resolved_by_input_order() -> None:
    select = metadata_module().select_metadata_as_of
    requests = _requests("user_id", [("user-a", AT)])
    assert select(normalized_users(), requests, entity_key="user_id").num_rows == 1
    with pytest.raises(StageCError):
        select(normalized_users({}, {"age": 99}), requests, entity_key="user_id")


@pytest.mark.parametrize("invalid", ["unknown_entity", "extra_column", "null_value", "negative_age"])
def test_selector_validates_metadata_instead_of_trusting_normalizer(invalid: str) -> None:
    select = metadata_module().select_metadata_as_of
    requests = _requests("user_id", [("user-a", AT)])
    metadata = normalized_users()
    key = "user_id"
    assert select(metadata, requests, entity_key=key).num_rows == 1
    if invalid == "unknown_entity":
        key = "channel_id"
    elif invalid == "extra_column":
        metadata = metadata.append_column("private_value", pa.array(["DO_NOT_EXPORT"]))
    else:
        metadata = normalized_users({"age": None if invalid == "null_value" else -1})
    with pytest.raises(StageCError):
        select(metadata, requests, entity_key=key)
