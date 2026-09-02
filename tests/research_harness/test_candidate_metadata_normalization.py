"""Task 6 원본 metadata → candidate-safe Arrow 변환의 계약 테스트.

[파이프라인] Judge가 허용된 원본에서 candidate 입력을 준비하는 구간을 검증한다.
[기능] 손 계산 golden, 선호값 보존, schema·시간·중복·duration 오류를 검증한다.
[비책임] 학습, 모델 다운로드, 파일 게시·final 소비는 이 테스트의 대상이 아니다.
"""

from datetime import timedelta

import pyarrow as pa
import pytest

from autoresearch.research_harness.fixture_errors import StageCError
from tests.research_harness.metadata_cases import (
    AT,
    USER_SCHEMA,
    VIDEO_SCHEMA,
    metadata_module,
    normalized_users,
    normalized_videos,
    raw_users,
    raw_videos,
    user_row,
    video_row,
)


def test_user_projection_matches_independent_golden_and_keeps_primary_categories() -> None:
    result = metadata_module().normalize_user_metadata(raw_users())
    assert result.schema == USER_SCHEMA
    assert result.to_pylist() == normalized_users().to_pylist()
    assert "DO_NOT_EXPORT" not in str(result.to_pylist())


def test_video_projection_matches_independent_golden() -> None:
    result = metadata_module().normalize_video_metadata(raw_videos())
    assert result.schema == VIDEO_SCHEMA
    assert result.to_pylist() == normalized_videos().to_pylist()


def test_explicit_preference_is_not_reinferred_from_conflicting_keywords() -> None:
    raw = raw_users(user_row(primary_categories=["Education"], hobby_keywords=["gaming"]))
    result = metadata_module().normalize_user_metadata(raw)
    assert result["primary_categories"].to_pylist() == [["Education"]]
    assert result["hobby_keywords"].to_pylist() == [["gaming"]]


def test_empty_keyword_lists_are_valid_not_missing_columns() -> None:
    empty = {name: [] for name in (
        "hobby_keywords", "interest_keywords", "lifestyle_keywords", "primary_categories",
    )}
    result = metadata_module().normalize_user_metadata(raw_users(user_row(**empty)))
    assert all(result[name].to_pylist() == [[]] for name in empty)


def test_user_timestamp_offset_is_normalized_to_utc() -> None:
    raw = raw_users(user_row(generated_at="2026-08-30T09:00:00+09:00"))
    assert metadata_module().normalize_user_metadata(raw)["available_at"].to_pylist() == [AT]


@pytest.mark.parametrize("late_field", ["collected_at", "video_trending_date"])
def test_video_availability_uses_later_observation(late_field: str) -> None:
    later = AT + timedelta(hours=1)
    raw = raw_videos(video_row(**{late_field: later}))
    assert metadata_module().normalize_video_metadata(raw)["available_at"].to_pylist() == [later]


@pytest.mark.parametrize("duration,seconds", [("PT5M", 300), ("PT1H2M3S", 3723), ("PT1S", 1)])
def test_duration_seconds_are_hand_calculated(duration: str, seconds: int) -> None:
    result = metadata_module().normalize_video_metadata(raw_videos(video_row(video_duration=duration)))
    assert result["duration_sec"].to_pylist() == [seconds]


@pytest.mark.parametrize("entity", ["user", "video"])
def test_normalization_sorts_entity_and_time_without_mutating_input(entity: str) -> None:
    module = metadata_module()
    later = AT + timedelta(hours=1)
    if entity == "user":
        raw = raw_users(user_row(user_id="user-b"), user_row(generated_at=later.isoformat()), user_row())
        transform = module.normalize_user_metadata
        key = "user_id"
    else:
        raw = raw_videos(video_row(video_id="video-b"), video_row(collected_at=later), video_row())
        transform = module.normalize_video_metadata
        key = "video_id"
    before = raw.to_pylist()
    output = transform(raw)
    assert output[key].to_pylist() == [f"{entity}-a", f"{entity}-a", f"{entity}-b"]
    assert output["available_at"].to_pylist() == [AT, later, AT]
    assert raw.to_pylist() == before


@pytest.mark.parametrize("entity", ["user", "video"])
def test_empty_raw_tables_preserve_exact_output_schema(entity: str) -> None:
    module = metadata_module()
    output = (
        module.normalize_user_metadata(raw_users().slice(0, 0)) if entity == "user"
        else module.normalize_video_metadata(raw_videos().slice(0, 0))
    )
    assert output.num_rows == 0
    assert output.schema == (USER_SCHEMA if entity == "user" else VIDEO_SCHEMA)


@pytest.mark.parametrize("field,value", [
    ("user_id", ""), ("age", -1), ("occupation", None),
    ("generated_at", "2026-08-30T00:00:00"), ("generated_at", "not-a-time"),
    ("primary_categories", [None]), ("primary_categories", ["not-a-category"]),
    ("hobby_keywords", None),
    ("hobby_keywords", [None]), ("interest_keywords", [None]),
    ("lifestyle_keywords", [None]), ("user_id", "   "),
    ("generated_at", "0001-01-01T00:00:00+14:00"),
])
def test_invalid_user_value_is_rejected(field: str, value: object) -> None:
    transform = metadata_module().normalize_user_metadata
    assert transform(raw_users()).num_rows == 1  # 모든 입력을 거부하는 구현의 거짓 성공 방지
    with pytest.raises(StageCError):
        transform(raw_users(user_row(**{field: value})))


@pytest.mark.parametrize("field,value", [
    ("video_id", ""), ("video_category", "not-a-category"),
    ("video_duration", "garbage"), ("video_duration", "PT0S"),
    ("video_duration", "PT0.5S"), ("video_duration", "-PT5M"),
    ("video_view_count", -1), ("video_like_count", -1), ("video_comment_count", -1),
    ("channel_subscriber_count", -1), ("channel_view_count", -1), ("channel_video_count", -1),
    ("video_published_at", AT + timedelta(days=1)), ("collected_at", None),
    ("video_duration", "PT"), ("video_duration", "PT9223372036854775808S"),
])
def test_invalid_video_value_is_rejected(field: str, value: object) -> None:
    transform = metadata_module().normalize_video_metadata
    assert transform(raw_videos()).num_rows == 1
    with pytest.raises(StageCError):
        transform(raw_videos(video_row(**{field: value})))


@pytest.mark.parametrize("entity,field", [
    ("user", "primary_categories"), ("user", "generated_at"),
    ("video", "video_duration"), ("video", "collected_at"),
])
def test_missing_required_column_is_not_cold_start(entity: str, field: str) -> None:
    module = metadata_module()
    transform = module.normalize_user_metadata if entity == "user" else module.normalize_video_metadata
    raw = raw_users() if entity == "user" else raw_videos()
    assert transform(raw).num_rows == 1
    with pytest.raises(StageCError):
        transform(raw.drop([field]))


@pytest.mark.parametrize("entity", ["user", "video"])
def test_duplicate_key_is_rejected_even_when_payload_is_identical(entity: str) -> None:
    module = metadata_module()
    transform = module.normalize_user_metadata if entity == "user" else module.normalize_video_metadata
    raw = raw_users() if entity == "user" else raw_videos()
    assert transform(raw).num_rows == 1
    with pytest.raises(StageCError):
        transform(pa.concat_tables([raw, raw]))


def test_duplicate_user_key_after_timezone_normalization_is_rejected() -> None:
    transform = metadata_module().normalize_user_metadata
    assert transform(raw_users()).num_rows == 1
    with pytest.raises(StageCError):
        transform(raw_users(user_row(), user_row(generated_at="2026-08-30T09:00:00+09:00")))


@pytest.mark.parametrize("entity,field,value", [
    ("user", "age", "25"), ("video", "video_view_count", "100"),
])
def test_wrong_arrow_type_is_not_silently_coerced(entity: str, field: str, value: str) -> None:
    module = metadata_module()
    transform = module.normalize_user_metadata if entity == "user" else module.normalize_video_metadata
    raw = raw_users() if entity == "user" else raw_videos()
    assert transform(raw).num_rows == 1
    wrong = raw.set_column(raw.schema.get_field_index(field), field, pa.array([value]))
    with pytest.raises(StageCError):
        transform(wrong)


@pytest.mark.parametrize("entity", ["user", "video"])
def test_duplicate_column_names_are_rejected(entity: str) -> None:
    module = metadata_module()
    transform = module.normalize_user_metadata if entity == "user" else module.normalize_video_metadata
    raw = raw_users() if entity == "user" else raw_videos()
    assert transform(raw).num_rows == 1
    with pytest.raises(StageCError):
        transform(raw.append_column(raw.column_names[0], raw.column(0)))


def test_invalid_value_is_not_in_public_error_message() -> None:
    transform = metadata_module().normalize_user_metadata
    assert transform(raw_users()).num_rows == 1
    with pytest.raises(StageCError) as error:
        transform(raw_users(user_row(primary_categories=["DO_NOT_EXPORT_CATEGORY"])))
    assert "DO_NOT_EXPORT" not in str(error.value)


def test_arrow_timestamp_outside_python_datetime_range_is_rejected() -> None:
    transform = metadata_module().normalize_video_metadata
    raw = raw_videos()
    assert transform(raw).num_rows == 1
    raw = raw.set_column(raw.schema.get_field_index("collected_at"), "collected_at",
                         pa.array([2**63 - 1], type=pa.timestamp("us", tz="UTC")))
    with pytest.raises(StageCError):
        transform(raw)
