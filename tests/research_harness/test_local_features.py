"""로컬 21개 피처의 손 계산 기대값과 시점·입력 계약 검증."""

from datetime import UTC, date, datetime, timedelta
from importlib import import_module
from importlib.util import find_spec
from types import ModuleType

import numpy as np
import pyarrow as pa
import pytest

from autoresearch.feature_engineering.category_reference import CATEGORY_DESCRIPTIONS
from autoresearch.feature_engineering.model_contract import FeatureContractError, MODEL_FEATURE_COLUMNS
from tests.research_harness.metadata_cases import normalized_users, normalized_videos


TS = pa.timestamp("us", tz="UTC")
T = date(2026, 9, 1)
START = date(2026, 8, 1)
Q = datetime(2026, 8, 31, 15, tzinfo=UTC)  # KST 9/1 00:00
HISTORY_SCHEMA = pa.schema([
    ("event_id", pa.string()), ("user_id", pa.string()), ("video_id", pa.string()),
    ("event_type", pa.string()), ("event_timestamp", TS), ("watch_time_sec", pa.int64()),
])
REQUEST_SCHEMA = pa.schema([
    ("user_id", pa.string()), ("video_id", pa.string()), ("event_timestamp", TS),
])


class GoldenEmbedding:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    def encode(self, texts: list[str], *, role: str) -> np.ndarray:
        self.calls.append((list(texts), role))
        vectors = {
            "기타": [3.0, 4.0], "음악": [-1.0, 0.0],
            CATEGORY_DESCRIPTIONS["Music"]: [4.0, 3.0],
            CATEGORY_DESCRIPTIONS["Gaming"]: [1.0, 0.0],
        }
        return np.array([vectors[text] for text in texts])


def module() -> ModuleType:
    name = "autoresearch.research_harness.local_features"
    assert find_spec(name), "RED: local_features 구현이 필요합니다"
    return import_module(name)


def requests(*rows: dict[str, object]) -> pa.Table:
    base = {"user_id": "user-a", "video_id": "video-a", "event_timestamp": Q}
    return pa.Table.from_pylist([{**base, **row} for row in rows] or [base], schema=REQUEST_SCHEMA)


def event(at: datetime, kind: str, **changes: object) -> dict[str, object]:
    return {
        "user_id": "user-a", "video_id": "video-a", "event_timestamp": at,
        "event_type": kind, "watch_time_sec": 0 if kind == "view" else None, **changes,
    }


def history(*rows: dict[str, object]) -> pa.Table:
    return pa.Table.from_pylist(
        [{"event_id": f"event-{i}", **row} for i, row in enumerate(rows)], schema=HISTORY_SCHEMA,
    )


def build(query: pa.Table | None = None, **changes: object) -> object:
    args = {
        "history": history(), "users": normalized_users(), "videos": normalized_videos(),
        "embedding": GoldenEmbedding(), "evaluation_start_date": T, "history_start_date": START,
        **changes,
    }
    return module().build_local_features(requests() if query is None else query, **args)


def test_hand_calculated_all_21_features_and_raw_zero_second_view() -> None:
    adapter = GoldenEmbedding()
    result = build(
        history=history(
            event(Q - timedelta(days=7), "click"),
            event(Q - timedelta(days=1), "view", watch_time_sec=0),
            event(Q - timedelta(days=1), "view", watch_time_sec=13),
            event(Q - timedelta(days=1), "like"),
            event(Q - timedelta(days=1), "impression"),
            event(Q - timedelta(days=7, microseconds=1), "click"),
        ),
        users=normalized_users({"watch_time_band": " PM "}),
        videos=normalized_videos({"view_count": 3, "like_count": 1, "comment_count": 2}),
        embedding=adapter,
    )
    assert result.features.column_names == list(MODEL_FEATURE_COLUMNS)
    assert result.features.to_pylist() == [{
        "age_group": "20s", "occupation": "student", "watch_time_band": "evening",
        "recent_click_count_7d": 1, "recent_view_count_7d": 2, "recent_watch_time_7d": 13,
        "recent_like_count_7d": 1, "historical_category_affinity": "Music",
        "total_event_count_7d": 5, "category_id": "Music", "duration_sec": 300,
        "view_count": 3, "like_ratio": 1 / 3, "comment_ratio": 2 / 3,
        "days_since_upload": 29, "channel_subscriber_count": 40,
        "channel_view_count": 1000, "channel_video_count": 8,
        "topic_similarity": 0.96, "preferred_category_match": 1, "historical_category_match": 1,
    }]
    assert result.diagnostics.to_pylist() == [{
        "user_metadata_missing": False, "video_metadata_missing": False,
        "history_7d_complete": True, "history_30d_complete": True,
    }]
    assert adapter.calls == [(["기타", "음악"], "query"), ([CATEGORY_DESCRIPTIONS["Music"]], "document")]


def test_request_order_duplicates_and_historical_same_day_exclusion() -> None:
    previous = Q - timedelta(days=1)
    result = build(
        requests({}, {"event_timestamp": previous}, {}),
        history=history(event(previous, "click")),
    )
    assert result.features["recent_click_count_7d"].to_pylist() == [1, 0, 1]
    assert result.features.to_pylist()[0] == result.features.to_pylist()[2]


def test_event_category_uses_event_time_not_query_time_and_ties_sort_strings() -> None:
    old = Q - timedelta(days=20)
    result = build(
        videos=normalized_videos(
            {"available_at": old, "category_id": "Gaming"},
            {"available_at": Q - timedelta(days=1), "category_id": "Music"},
        ),
        history=history(event(old, "click"), event(Q - timedelta(days=1), "view")),
    )
    row = result.features.to_pylist()[0]
    assert row["historical_category_affinity"] == "Gaming"
    assert row["historical_category_match"] == 0


def test_missing_observations_coldstart_and_incomplete_history_are_separate() -> None:
    result = build(
        requests({"user_id": "new", "video_id": "new"}),
        history_start_date=date(2026, 8, 30),
    )
    row = result.features.to_pylist()[0]
    categorical = {"age_group", "occupation", "watch_time_band", "historical_category_affinity", "category_id"}
    assert all(value == ("unknown" if key in categorical else 0) for key, value in row.items())
    assert result.diagnostics.to_pylist() == [{
        "user_metadata_missing": True, "video_metadata_missing": True,
        "history_7d_complete": False, "history_30d_complete": False,
    }]


@pytest.mark.parametrize("changes", [
    {"event_type": "bad"}, {"user_id": " "}, {"event_id": ""},
    {"watch_time_sec": 1}, {"event_timestamp": Q},
    {"event_timestamp": datetime(2026, 7, 31, 14, tzinfo=UTC)},
])
def test_invalid_history_fails_closed(changes: dict[str, object]) -> None:
    with pytest.raises(FeatureContractError):
        build(history=history(event(Q - timedelta(days=1), "click", **changes)))


def test_duplicate_event_ids_fail_instead_of_inflating_counts() -> None:
    row = event(Q - timedelta(days=1), "click", event_id="duplicate")
    with pytest.raises(FeatureContractError):
        build(history=history(row, row))


def test_30_day_lower_bound_is_inclusive_and_prior_microsecond_excluded() -> None:
    earliest = Q - timedelta(days=30)
    result = build(
        history=history(
            event(earliest, "click"),
            event(earliest - timedelta(microseconds=1), "click", video_id="old"),
            event(earliest - timedelta(microseconds=1), "view", video_id="old"),
        ),
        videos=normalized_videos(
            {"available_at": Q - timedelta(days=31), "published_at": Q - timedelta(days=32)},
            {"video_id": "old", "available_at": Q - timedelta(days=31), "published_at": Q - timedelta(days=32), "category_id": "Gaming"},
        ),
    )
    assert result.features["historical_category_affinity"].to_pylist() == ["Music"]
    assert result.features["recent_click_count_7d"].to_pylist() == [0]


def test_metadata_asof_before_at_after_boundary_and_no_future_backfill() -> None:
    at = datetime(2026, 8, 30, tzinfo=UTC)
    result = build(requests(
        {"event_timestamp": at - timedelta(microseconds=1)},
        {"event_timestamp": at},
        {"event_timestamp": at + timedelta(microseconds=1)},
    ))
    assert result.features["age_group"].to_pylist() == ["unknown", "20s", "20s"]
    assert result.features["view_count"].to_pylist() == [0, 100, 100]
    assert result.diagnostics["user_metadata_missing"].to_pylist() == [True, False, False]
    assert result.diagnostics["video_metadata_missing"].to_pylist() == [True, False, False]


@pytest.mark.parametrize(("age", "expected"), [
    (0, "10s"), (19, "10s"), (20, "20s"), (29, "20s"), (30, "30s"),
    (39, "30s"), (40, "40s"), (49, "40s"), (50, "50s+"), (120, "50s+"),
])
def test_age_boundaries(age: int, expected: str) -> None:
    assert build(users=normalized_users({"age": age})).features["age_group"].to_pylist() == [expected]


@pytest.mark.parametrize(("band", "expected"), [
    *[(band, "morning") for band in ("morning", "AM", "오전", " 아침 ")],
    *[(band, "evening") for band in ("evening", "pm", "저녁", "오후")],
    *[(band, "night") for band in ("night", "late_night", "밤", "심야")],
    *[(band, "unknown") for band in ("afternoon", "mixed", "", "not-known")],
])
def test_watch_time_aliases(band: str, expected: str) -> None:
    result = build(users=normalized_users({"watch_time_band": band}))
    assert result.features["watch_time_band"].to_pylist() == [expected]


def test_empty_requests_keep_typed_schema_and_do_not_embed() -> None:
    adapter = GoldenEmbedding()
    result = build(requests().slice(0, 0), embedding=adapter)
    assert result.features.num_rows == result.diagnostics.num_rows == 0
    assert result.features.column_names == list(MODEL_FEATURE_COLUMNS)
    assert result.features.schema.field("topic_similarity").type == pa.float64()
    assert adapter.calls == []


def test_zero_denominator_empty_keywords_and_explicit_preference() -> None:
    adapter = GoldenEmbedding()
    row = build(
        users=normalized_users({"hobby_keywords": [], "interest_keywords": [], "primary_categories": ["Music"]}),
        videos=normalized_videos({"view_count": 0}), embedding=adapter,
    ).features.to_pylist()[0]
    assert row["like_ratio"] == row["comment_ratio"] == row["topic_similarity"] == 0
    assert row["preferred_category_match"] == 1
    assert adapter.calls == []


def test_negative_cosine_is_not_clamped_to_zero() -> None:
    row = build(users=normalized_users({"hobby_keywords": [], "interest_keywords": ["음악"]})).features.to_pylist()[0]
    assert row["topic_similarity"] == -0.8


def test_observed_video_age_uses_kst_not_utc_or_query_date() -> None:
    # UTC 날짜는 같지만 KST 날짜가 하루 다름. q(9/1)가 아닌 관측일(8/31) 기준.
    result = build(videos=normalized_videos({
        "published_at": datetime(2026, 8, 30, 14, 59, tzinfo=UTC),
        "available_at": datetime(2026, 8, 30, 15, 0, tzinfo=UTC),
    }))
    assert result.features["days_since_upload"].to_pylist() == [1]


def test_evaluation_second_day_cannot_claim_complete_history() -> None:
    result = build(requests({"event_timestamp": Q + timedelta(days=1)}))
    assert result.diagnostics["history_7d_complete"].to_pylist() == [False]
    assert result.diagnostics["history_30d_complete"].to_pylist() == [False]


@pytest.mark.parametrize("table_name", ["requests", "history"])
def test_naive_timestamp_and_wrong_numeric_schema_are_rejected(table_name: str) -> None:
    table = requests() if table_name == "requests" else history(event(Q - timedelta(days=1), "click"))
    table = table.set_column(
        table.column_names.index("event_timestamp"), "event_timestamp",
        table["event_timestamp"].cast(pa.timestamp("us")),
    )
    with pytest.raises(FeatureContractError):
        build(table) if table_name == "requests" else build(history=table)


@pytest.mark.parametrize("seconds", [None, -1])
def test_view_requires_nonnegative_watch_time(seconds: int | None) -> None:
    with pytest.raises(FeatureContractError):
        build(history=history(event(Q - timedelta(days=1), "view", watch_time_sec=seconds)))


def test_history_sum_overflow_is_sanitized() -> None:
    with pytest.raises(FeatureContractError, match="local_feature_output_invalid"):
        build(history=history(
            event(Q - timedelta(days=1), "view", watch_time_sec=2**63 - 1),
            event(Q - timedelta(days=1), "view", watch_time_sec=1),
        ))


def test_kst_history_date_overflow_is_sanitized() -> None:
    with pytest.raises(FeatureContractError, match="local_feature_history_bounds_invalid"):
        build(history=history(event(datetime.max.replace(tzinfo=UTC), "click")))


def test_extra_request_fields_are_not_features_and_inputs_are_unchanged() -> None:
    table = requests().append_column("label", pa.array([1]))
    before = table.to_pylist()
    result = build(table)
    assert table.to_pylist() == before
    assert "label" not in result.features.column_names


def test_missing_required_metadata_field_is_not_coldstart() -> None:
    from autoresearch.research_harness.fixture_errors import StageCError

    with pytest.raises(StageCError):
        build(users=normalized_users().drop(["age"]))
