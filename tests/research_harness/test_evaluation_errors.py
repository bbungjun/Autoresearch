from datetime import date

from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)


def test_error_exposes_typed_context() -> None:
    error = EvaluationSnapshotError(
        code=SnapshotErrorCode.INVALID_DATE_RANGE,
        stage="request_validation",
        dt=date(2026, 9, 1),
        count=3,
        identifier_prefix="event-123",
    )

    assert error.code is SnapshotErrorCode.INVALID_DATE_RANGE
    assert error.stage == "request_validation"
    assert error.dt == date(2026, 9, 1)
    assert error.count == 3
    assert error.identifier_prefix == "event-123"


def test_error_truncates_ascii_identifier_prefix_to_sixteen_utf8_bytes() -> None:
    error = EvaluationSnapshotError(
        code=SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
        stage="source_validation",
        identifier_prefix="0123456789abcdefg",
    )

    assert error.identifier_prefix == "0123456789abcdef"


def test_error_truncates_korean_identifier_prefix_on_utf8_code_point_boundary() -> None:
    error = EvaluationSnapshotError(
        code=SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
        stage="source_validation",
        identifier_prefix="가나다라마바사",
    )

    assert error.identifier_prefix == "가나다라마"


def test_error_truncates_emoji_identifier_prefix_on_utf8_code_point_boundary() -> None:
    error = EvaluationSnapshotError(
        code=SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
        stage="source_validation",
        identifier_prefix="😀😀😀😀😀",
    )

    assert error.identifier_prefix == "😀😀😀😀"


def test_error_codes_exactly_match_snapshot_contract() -> None:
    assert {code.value for code in SnapshotErrorCode} == {
        "invalid_date_range",
        "source_partition_missing",
        "source_schema_invalid",
        "partition_timestamp_mismatch",
        "slate_id_missing_after_cutover",
        "slate_id_invalid",
        "slate_id_collision",
        "duplicate_slate_video",
        "slate_attribution_mismatch",
        "split_coverage_insufficient",
        "snapshot_write_conflict",
    }
