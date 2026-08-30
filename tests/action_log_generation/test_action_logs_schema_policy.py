"""EventLog 정책 메타데이터 additive 확장·하위 호환 테스트."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from autoresearch.action_log_generation.schema import (
    SOURCE_ONLINE_SIMULATED,
    EventGenerationRequest,
    EventLog,
    SlateGenerationContext,
)


def _base_kwargs() -> dict:
    return {
        "event_id": "evt_00000000",
        "event_timestamp": datetime(2026, 7, 20, tzinfo=UTC),
        "user_id": "u1",
        "event_type": "impression",
        "video_id": "v1",
    }


def _policy_event(**overrides: object) -> EventLog:
    """정책 메타데이터를 덧붙인 impression EventLog를 만드는 로컬 헬퍼."""

    return EventLog(**_base_kwargs(), **overrides)


def test_context_free_event_row_baseline_allows_only_future_null_slate_field() -> None:
    # Given
    event = EventLog(**_base_kwargs())
    expected = {
        "event_id": "evt_00000000",
        "event_timestamp": "2026-07-20T00:00:00+00:00",
        "user_id": "u1",
        "event_type": "impression",
        "video_id": "v1",
        "watch_time_sec": None,
        "rank": None,
        "source": "historical",
        "policy": None,
        "ctr_score": None,
        "is_exploration": None,
        "policy_version": None,
        "exposure_source": None,
    }

    # When
    row = event.to_warehouse_row()
    optional_slate_id = row.pop("slate_id", None)

    # Then
    assert event.event_id == "evt_00000000"
    assert row == expected
    assert optional_slate_id is None


def test_historical_event_without_policy_fields_still_validates():
    event = EventLog(**_base_kwargs())  # 기존 historical 로그 형태 그대로
    assert event.policy is None
    assert event.ctr_score is None
    assert event.is_exploration is None
    assert event.policy_version is None


def test_policy_fields_round_trip_to_warehouse_row():
    event = EventLog(
        **_base_kwargs(),
        rank=3,
        source=SOURCE_ONLINE_SIMULATED,
        policy="model",
        ctr_score=0.87,
        is_exploration=False,
        policy_version="run-abc123",
    )
    row = event.to_warehouse_row()
    assert row["source"] == "online_simulated"
    assert row["policy"] == "model"
    assert row["ctr_score"] == 0.87
    assert row["is_exploration"] is False
    assert row["policy_version"] == "run-abc123"


def test_baseline_policy_allows_null_score():
    event = EventLog(**_base_kwargs(), policy="baseline")
    assert event.ctr_score is None


def test_exposure_source_roundtrip_and_validation():
    event = _policy_event(exposure_source="model")
    assert event.to_warehouse_row()["exposure_source"] == "model"

    legacy = _policy_event()  # 필드 미지정 — 기존 로그 하위 호환
    assert legacy.exposure_source is None
    assert legacy.to_warehouse_row()["exposure_source"] is None

    with pytest.raises(ValidationError):
        _policy_event(exposure_source="heuristic")  # 세 값 외 거부


def test_legacy_event_defaults_slate_id_to_null() -> None:
    # Given / When
    event = EventLog(**_base_kwargs())

    # Then
    assert event.slate_id is None


def test_explicit_slate_id_round_trips_to_warehouse_row() -> None:
    # Given
    slate_id = "slt_20260831_0cf0daf7c833035b191942e5"

    # When
    event = EventLog(**_base_kwargs(), slate_id=slate_id)

    # Then
    assert event.slate_id == slate_id
    assert event.to_warehouse_row()["slate_id"] == slate_id


def test_slate_generation_context_is_frozen_and_typed() -> None:
    # Given
    context = SlateGenerationContext(partition_date=date(2026, 8, 31))

    # When / Then
    assert context.producer == "daily-action-log-v1"
    with pytest.raises(ValidationError):
        context.partition_date = date(2026, 9, 1)


def test_request_accepts_valid_daily_slate_context() -> None:
    # Given
    context = SlateGenerationContext(partition_date=date(2026, 8, 31))

    # When
    request = EventGenerationRequest(
        click_threshold=0.5,
        candidates_per_user=4,
        history_days=1,
        max_events_per_user_per_day=4,
        slate_context=context,
    )

    # Then
    assert request.slate_context == context


@pytest.mark.parametrize(
    ("overrides", "error_fragment"),
    [
        ({"history_days": 2}, "history_days"),
        (
            {"candidates_per_user": 5, "max_events_per_user_per_day": 4},
            "max_events_per_user_per_day",
        ),
    ],
)
def test_request_rejects_invalid_daily_slate_context_boundaries(
    overrides: dict[str, int],
    error_fragment: str,
) -> None:
    # Given
    values = {
        "click_threshold": 0.5,
        "candidates_per_user": 4,
        "history_days": 1,
        "max_events_per_user_per_day": 4,
        "slate_context": SlateGenerationContext(
            partition_date=date(2026, 8, 31)
        ),
        **overrides,
    }

    # When
    with pytest.raises(ValidationError) as exc_info:
        EventGenerationRequest(**values)

    # Then
    assert error_fragment in str(exc_info.value)


def test_slate_generation_context_rejects_unknown_producer() -> None:
    # Given / When
    with pytest.raises(ValidationError) as exc_info:
        SlateGenerationContext(
            partition_date=date(2026, 8, 31),
            producer="other-producer",
        )

    # Then
    assert "daily-action-log-v1" in str(exc_info.value)


def test_cli_videos_help_describes_videos_csv(capsys, monkeypatch):
    import sys

    from autoresearch.recommendation import simulate_policy_round as module

    monkeypatch.setattr(sys, "argv", ["simulate_policy_round", "--help"])
    with pytest.raises(SystemExit):
        module._cli()

    help_text = capsys.readouterr().out
    assert "사전 파싱된 videos.csv 경로" in help_text
    assert "youtube_videos.csv" not in help_text
