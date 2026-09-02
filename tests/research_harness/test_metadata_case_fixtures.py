"""Task 6 테스트 입력이 기존 fixture 원본 타입과 맞는지 확인한다.

[파이프라인] metadata 정규화 전 원본 테스트 재료의 계약을 검증한다.
[기능] 독립 작성한 작은 입력의 필드·Arrow 타입·실제 값을 기존 schema와 대조한다.
[비책임] 제품 metadata 변환의 성공 증거가 아니며 RED 테스트를 대체하지 않는다.
"""

from autoresearch.research_harness.fixture_inputs import (
    FIXTURE_VIRTUAL_USER_SCHEMA_V1, FIXTURE_YOUTUBE_SCHEMA_V1,
)
from tests.research_harness.metadata_cases import (
    AT, normalized_users, normalized_videos, raw_users, raw_videos,
)


def test_small_user_fixture_matches_real_source_field_types() -> None:
    raw = raw_users()
    raw.validate(full=True)
    for field in raw.schema:
        assert FIXTURE_VIRTUAL_USER_SCHEMA_V1.field(field.name).type == field.type
    expected = normalized_users()
    expected.validate(full=True)
    assert expected["available_at"].to_pylist() == [AT]
    assert expected["primary_categories"].to_pylist() == [["Music"]]


def test_small_video_fixture_matches_real_source_field_types() -> None:
    raw = raw_videos()
    raw.validate(full=True)
    for field in raw.schema:
        assert FIXTURE_YOUTUBE_SCHEMA_V1.field(field.name).type == field.type
    expected = normalized_videos()
    expected.validate(full=True)
    assert raw["video_duration"].to_pylist() == ["PT5M"]
    assert expected["duration_sec"].to_pylist() == [5 * 60]
