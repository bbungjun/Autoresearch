"""Stage C local fixture의 canonical virtual user·YouTube 입력 생성기.

[파이프라인] Judge가 local 평가 fixture를 준비하는 첫 구간에서 일일 action log
producer가 읽을 production-schema Parquet 입력과 그 descriptor를 결정적으로 만든다.

[기능] 평가일 기준 고정 4일 창, Stage B bucket별 160/40 사용자, 일별 48개 영상,
pinned PyArrow writer receipt와 canonical descriptor JSON을 생성한다.

[비책임] 일일 producer 실행, action log 검증, Stage B snapshot build와 fixture root의
write-once 게시는 후속 Stage C orchestration 모듈이 담당한다.
"""

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Final

import pyarrow as pa

from autoresearch.research_harness.evaluation_artifacts import (
    WRITER_OPTIONS,
    _write_table,
    canonical_json_bytes,
)
from autoresearch.research_harness.evaluation_snapshot_models import WriterIdentity
from autoresearch.research_harness.evaluation_split import SPLIT_CONTRACT, user_bucket
from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode
from autoresearch.research_harness.fixture_models import (
    FIXTURE_CHANNEL_PUBLISHED_OFFSET_DAYS,
    FIXTURE_HISTORY_START_OFFSET_DAYS,
    FixtureDescriptor,
    FixtureInputReceipt,
    FixturePartitionReceipt,
    LocalEvaluationFixtureRequest,
    require_fixture_date_window,
)


VALIDATION_USER_COUNT: Final = 160
FINAL_HOLDOUT_USER_COUNT: Final = 40
VIDEO_COUNT_PER_PARTITION: Final = 48
_VIRTUAL_USER_PATH: Final = "inputs/virtual_users.parquet"
_YOUTUBE_ROOT: Final = "inputs/youtube_trending_kr"
_CATEGORIES: Final = ("Gaming", "Music", "Education", "Entertainment")
_WATCH_TIME_BANDS: Final = ("morning", "afternoon", "evening", "night", "mixed")
_TIMESTAMP_US_UTC: Final = pa.timestamp("us", tz="UTC")

# `youtube-ctr-input-v1`의 schema는 production 모듈과 별도로 고정한다. Production에
# additive/change가 생겨도 같은 generator version의 bytes가 조용히 변하면 안 된다.
FIXTURE_VIRTUAL_USER_SCHEMA_V1: Final = pa.schema(
    [
        pa.field("user_id", pa.string()),
        pa.field("source_uuid", pa.string()),
        pa.field("source_dataset", pa.string()),
        pa.field("source_hash", pa.string()),
        pa.field("age", pa.int64()),
        pa.field("sex", pa.string()),
        pa.field("age_bucket", pa.string()),
        pa.field("marital_status", pa.string()),
        pa.field("military_status", pa.string()),
        pa.field("family_type", pa.string()),
        pa.field("housing_type", pa.string()),
        pa.field("education_level", pa.string()),
        pa.field("bachelors_field", pa.string()),
        pa.field("occupation", pa.string()),
        pa.field("province", pa.string()),
        pa.field("district", pa.string()),
        pa.field("country", pa.string()),
        pa.field("locale", pa.string()),
        pa.field("persona_summary", pa.string()),
        pa.field("hobby_keywords", pa.list_(pa.string())),
        pa.field("interest_keywords", pa.list_(pa.string())),
        pa.field("lifestyle_keywords", pa.list_(pa.string())),
        pa.field("food_keywords", pa.list_(pa.string())),
        pa.field("travel_keywords", pa.list_(pa.string())),
        pa.field("career_keywords", pa.list_(pa.string())),
        pa.field("family_context_keywords", pa.list_(pa.string())),
        pa.field("primary_categories", pa.list_(pa.string())),
        pa.field("watch_time_band", pa.string()),
        pa.field("source_persona_json", pa.string()),
        pa.field("schema_version", pa.string()),
        pa.field("prompt_version", pa.string()),
        pa.field("llm_model", pa.string()),
        pa.field("generated_at", pa.string()),
    ]
)
FIXTURE_YOUTUBE_SCHEMA_V1: Final = pa.schema(
    [
        pa.field("video_id", pa.string()),
        pa.field("video_published_at", _TIMESTAMP_US_UTC),
        pa.field("video_trending_date", _TIMESTAMP_US_UTC),
        pa.field("video_trending_country", pa.string()),
        pa.field("video_title", pa.string()),
        pa.field("video_description", pa.string()),
        pa.field("video_default_thumbnail", pa.string()),
        pa.field("video_category", pa.string()),
        pa.field("video_tags", pa.list_(pa.string())),
        pa.field("video_duration", pa.string()),
        pa.field("video_dimension", pa.string()),
        pa.field("video_definition", pa.string()),
        pa.field("video_licensed_content", pa.bool_()),
        pa.field("video_view_count", pa.int64()),
        pa.field("video_like_count", pa.int64()),
        pa.field("video_comment_count", pa.int64()),
        pa.field("channel_id", pa.string()),
        pa.field("channel_title", pa.string()),
        pa.field("channel_description", pa.string()),
        pa.field("channel_custom_url", pa.string()),
        pa.field("channel_published_at", _TIMESTAMP_US_UTC),
        pa.field("channel_country", pa.string()),
        pa.field("channel_view_count", pa.int64()),
        pa.field("channel_subscriber_count", pa.int64()),
        pa.field("channel_have_hidden_subscribers", pa.bool_()),
        pa.field("channel_video_count", pa.int64()),
        pa.field("channel_localized_title", pa.string()),
        pa.field("channel_localized_description", pa.string()),
        pa.field("collected_at", _TIMESTAMP_US_UTC),
    ]
)


def canonical_fixture_dates(evaluation_start_date: date) -> tuple[date, ...]:
    """Return history, evaluation, and scan-tail partition dates in canonical order."""

    require_fixture_date_window(evaluation_start_date)
    try:
        return tuple(
            evaluation_start_date + timedelta(days=offset)
            for offset in (-FIXTURE_HISTORY_START_OFFSET_DAYS, -1, 0, 1)
        )
    except OverflowError:
        raise StageCError(
            StageCErrorCode.FIXTURE_REQUEST_INVALID,
            "fixture_date_window_validation",
        ) from None


def select_fixture_user_ids(
    fixture_seed: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Select exactly 160 validation and 40 final-holdout IDs by Stage B buckets."""

    if isinstance(fixture_seed, bool) or not isinstance(fixture_seed, int) or fixture_seed < 0:
        raise StageCError(
            StageCErrorCode.FIXTURE_REQUEST_INVALID,
            "fixture_user_selection",
        )
    validation: list[str] = []
    final_holdout: list[str] = []
    candidate_index = 0
    while (
        len(validation) < VALIDATION_USER_COUNT
        or len(final_holdout) < FINAL_HOLDOUT_USER_COUNT
    ):
        candidate_digest = sha256(
            f"youtube-ctr-input-v1:{fixture_seed}:{candidate_index}".encode("utf-8")
        ).hexdigest()
        candidate_id = f"fixture-user-{candidate_digest[:20]}"
        bucket = user_bucket(candidate_id)
        if (
            bucket in SPLIT_CONTRACT.validation_buckets
            and len(validation) < VALIDATION_USER_COUNT
        ):
            validation.append(candidate_id)
        elif (
            bucket in SPLIT_CONTRACT.final_holdout_buckets
            and len(final_holdout) < FINAL_HOLDOUT_USER_COUNT
        ):
            final_holdout.append(candidate_id)
        candidate_index += 1
    return tuple(validation), tuple(final_holdout)


def descriptor_sha256(descriptor: FixtureDescriptor) -> str:
    """Return the digest of the descriptor's exact canonical JSON representation."""

    return sha256(
        canonical_json_bytes(descriptor.model_dump(mode="json"))
    ).hexdigest()


def write_canonical_fixture_inputs(
    fixture_root: Path,
    request: LocalEvaluationFixtureRequest,
) -> FixtureDescriptor:
    """Write deterministic production-schema inputs and their Judge-only descriptor.

    Args:
        fixture_root: 아직 게시되지 않은 fixture staging root.
        request: 평가 기준일과 필수 seed를 포함한 Stage C 요청.

    Returns:
        기록된 입력의 exact receipt를 포함한 frozen descriptor.
    """

    fixture_dates = canonical_fixture_dates(request.evaluation_start_date)
    virtual_users_path = fixture_root / _VIRTUAL_USER_PATH
    virtual_users_path.parent.mkdir(parents=True, exist_ok=True)
    validation_ids, final_holdout_ids = select_fixture_user_ids(request.fixture_seed)
    virtual_user_table = pa.Table.from_pylist(
        _virtual_user_rows(validation_ids + final_holdout_ids, request.evaluation_start_date),
        schema=FIXTURE_VIRTUAL_USER_SCHEMA_V1,
    )
    _write_table(virtual_user_table, virtual_users_path)
    virtual_users_receipt = FixtureInputReceipt(
        relative_path=_VIRTUAL_USER_PATH,
        rows=virtual_user_table.num_rows,
        sha256=_file_sha256(virtual_users_path),
    )

    youtube_receipts: list[FixturePartitionReceipt] = []
    for partition_date in fixture_dates:
        relative_path = (
            f"{_YOUTUBE_ROOT}/dt={partition_date.isoformat()}/part-0.parquet"
        )
        partition_path = fixture_root / relative_path
        partition_path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(
            _fixture_video_rows(partition_date),
            schema=FIXTURE_YOUTUBE_SCHEMA_V1,
        )
        _write_table(table, partition_path)
        youtube_receipts.append(
            FixturePartitionReceipt(
                dt=partition_date,
                relative_path=relative_path,
                rows=table.num_rows,
                sha256=_file_sha256(partition_path),
            )
        )

    descriptor = FixtureDescriptor(
        contract_version="youtube-ctr-local-fixture-v1",
        input_generator_version="youtube-ctr-input-v1",
        input_writer=WriterIdentity(
            engine="pyarrow",
            version=pa.__version__,
            options=WRITER_OPTIONS,
        ),
        fixture_seed=request.fixture_seed,
        generator="rule_based",
        generator_model="fixture-rule-action-log",
        history_start_date=fixture_dates[0],
        evaluation_start_date=request.evaluation_start_date,
        evaluation_end_date=request.evaluation_start_date,
        slate_id_cutover_date=fixture_dates[0],
        candidates_per_user=24,
        video_count_per_partition=VIDEO_COUNT_PER_PARTITION,
        click_threshold=0.0,
        personalized_ratio=0.7,
        popular_ratio=0.2,
        exploration_ratio=0.1,
        history_days_per_run=1,
        max_events_per_user_per_day=24,
        max_concurrency=1,
        chunk_size=0,
        max_quarantine_ratio=0.0,
        overwrite=False,
        validation_user_count=VALIDATION_USER_COUNT,
        final_holdout_user_count=FINAL_HOLDOUT_USER_COUNT,
        virtual_users=virtual_users_receipt,
        youtube_partitions=tuple(youtube_receipts),
    )
    (fixture_root / "fixture.json").write_bytes(
        canonical_json_bytes(descriptor.model_dump(mode="json"))
    )
    return descriptor


def _virtual_user_rows(
    user_ids: tuple[str, ...],
    evaluation_start_date: date,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    generated_at = datetime.combine(
        evaluation_start_date - timedelta(days=2),
        datetime.min.time(),
        tzinfo=UTC,
    ).isoformat().replace("+00:00", "Z")
    for index, user_id in enumerate(user_ids):
        primary = _CATEGORIES[index % len(_CATEGORIES)]
        secondary = _CATEGORIES[(index + 1) % len(_CATEGORIES)]
        source_hash = sha256(f"{user_id}:source".encode("utf-8")).hexdigest()
        rows.append(
            {
                "user_id": user_id,
                "source_uuid": source_hash[:32],
                "source_dataset": "fixture/youtube-ctr-input-v1",
                "source_hash": source_hash,
                "age": 20 + index % 40,
                "sex": "female" if index % 2 == 0 else "male",
                "age_bucket": f"{(20 + index % 40) // 10 * 10}s",
                "marital_status": "fixture",
                "military_status": "fixture",
                "family_type": "fixture",
                "housing_type": "fixture",
                "education_level": "fixture",
                "bachelors_field": "fixture",
                "occupation": "fixture-researcher",
                "province": "Seoul",
                "district": "fixture",
                "country": "KR",
                "locale": "ko-KR",
                "persona_summary": f"{primary}와 {secondary} 영상을 선호하는 사용자",
                "hobby_keywords": [primary],
                "interest_keywords": [primary, secondary],
                "lifestyle_keywords": ["영상"],
                "food_keywords": [],
                "travel_keywords": [],
                "career_keywords": [],
                "family_context_keywords": [],
                "primary_categories": [primary, secondary],
                "watch_time_band": _WATCH_TIME_BANDS[index % len(_WATCH_TIME_BANDS)],
                "source_persona_json": "{}",
                "schema_version": "virtual_user_schema_v1",
                "prompt_version": "virtual_user_youtube_v1",
                "llm_model": "fixture-rule-virtual-user",
                "generated_at": generated_at,
            }
        )
    return rows


def _fixture_video_rows(partition_date: date) -> list[dict[str, object]]:
    collected_at = datetime.combine(
        partition_date,
        datetime.min.time(),
        tzinfo=UTC,
    )
    videos: list[dict[str, object]] = []
    for index in range(VIDEO_COUNT_PER_PARTITION):
        category = _CATEGORIES[index % len(_CATEGORIES)]
        video_id = f"fixture-video-{partition_date:%Y%m%d}-{index:04d}"
        channel_index = index % 12
        videos.append(
            {
                "video_id": video_id,
                "video_published_at": collected_at - timedelta(days=30 + index),
                "video_trending_date": collected_at,
                "video_trending_country": "KR",
                "video_title": f"{category} fixture 영상 {index:02d}",
                "video_description": f"{category} 연구용 결정적 영상 설명 {index:02d}",
                "video_default_thumbnail": f"https://fixture.invalid/{video_id}.jpg",
                "video_category": category,
                "video_tags": [category, "fixture", f"topic-{index % 6}"],
                "video_duration": f"PT{5 + index % 20}M",
                "video_dimension": "2d",
                "video_definition": "hd",
                "video_licensed_content": False,
                "video_view_count": 1_000_000 - index * 1_000,
                "video_like_count": 50_000 - index * 100,
                "video_comment_count": 5_000 - index * 10,
                "channel_id": f"fixture-channel-{channel_index:02d}",
                "channel_title": f"Fixture Channel {channel_index:02d}",
                "channel_description": "결정적 local fixture 채널",
                "channel_custom_url": f"@fixture-channel-{channel_index:02d}",
                "channel_published_at": collected_at
                - timedelta(days=FIXTURE_CHANNEL_PUBLISHED_OFFSET_DAYS),
                "channel_country": "KR",
                "channel_view_count": 10_000_000 + channel_index,
                "channel_subscriber_count": 100_000 + channel_index,
                "channel_have_hidden_subscribers": False,
                "channel_video_count": 100 + channel_index,
                "channel_localized_title": f"Fixture Channel {channel_index:02d}",
                "channel_localized_description": "결정적 local fixture 채널",
                "collected_at": collected_at,
            }
        )
    return videos


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()
