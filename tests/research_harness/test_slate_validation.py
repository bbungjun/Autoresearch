from datetime import UTC, date, datetime
from typing import Final

import pytest

from autoresearch.action_log_generation.slate_identity import (
    ExposureSource,
    SlateIdentity,
    SlateMember,
    generate_slate_id,
)
from autoresearch.research_harness.evaluation_source_models import (
    LoadedPartition,
    SourceEvent,
    SourcePartitionReceipt,
)
from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.slate_validation import validate_slate_identities


PARTITION_DATE: Final = date(2026, 9, 1)


def _partition(events: tuple[SourceEvent, ...]) -> LoadedPartition:
    return LoadedPartition(
        receipt=SourcePartitionReceipt(
            dt=PARTITION_DATE,
            uri="memory://daily/dt=2026-09-01/part-0.parquet",
            rows=len(events),
            sha256="0" * 64,
        ),
        events=events,
    )


def _impression(
    *,
    slate_id: str,
    video_id: str = "video-1",
    rank: int | None = 1,
    exposure_source: str | None = "model",
    policy_version: str | None = "policy-v1",
    user_id: str = "user-1",
    source_event_id: str = "evt_20260901_00000001",
) -> SourceEvent:
    return SourceEvent(
        partition_date=PARTITION_DATE,
        source_event_id=source_event_id,
        event_type="impression",
        user_id=user_id,
        video_id=video_id,
        event_timestamp=datetime(2026, 9, 1, tzinfo=UTC),
        slate_id=slate_id,
        rank=rank,
        exposure_source=exposure_source,
        policy_version=policy_version,
    )


def test_validate_slate_identities_accepts_valid_stored_producer_id() -> None:
    # Given
    identity = SlateIdentity(
        partition_date=PARTITION_DATE,
        user_id="user-1",
        members=(SlateMember("video-1", 1, "model", "policy-v1"),),
    )
    partition = _partition((_impression(slate_id=str(generate_slate_id(identity))),))

    # When
    result = validate_slate_identities((partition,))

    # Then
    assert result is None


def test_validate_slate_identities_accepts_permuted_member_input() -> None:
    # Given
    members = (
        SlateMember("video-1", 1, "model", "policy-v1"),
        SlateMember("video-2", 2, "trending", None),
    )
    slate_id = str(
        generate_slate_id(
            SlateIdentity(PARTITION_DATE, "user-1", members),
        )
    )
    partition = _partition(
        (
            _impression(
                slate_id=slate_id,
                video_id="video-2",
                rank=2,
                exposure_source="trending",
                policy_version=None,
                source_event_id="evt_20260901_00000002",
            ),
            _impression(slate_id=slate_id),
        )
    )

    # When
    result = validate_slate_identities((partition,))

    # Then
    assert result is None


@pytest.mark.parametrize(
    "stored_slate_id",
    (
        "not-a-slate-id",
        "slt_20260902_b3aea7dbc26846d9e62bb38f",
        "slt_20260901_000000000000000000000000",
    ),
    ids=("format", "date-prefix", "hash"),
)
def test_validate_slate_identities_rejects_invalid_stored_id(
    stored_slate_id: str,
) -> None:
    # Given
    partition = _partition((_impression(slate_id=stored_slate_id),))

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        validate_slate_identities((partition,))

    # Then
    assert captured.value.code is SnapshotErrorCode.SLATE_ID_INVALID
    assert captured.value.dt == PARTITION_DATE


def test_validate_slate_identities_rejects_duplicate_slate_video() -> None:
    # Given
    stored_slate_id = "slt_20260901_000000000000000000000000"
    partition = _partition(
        (
            _impression(slate_id=stored_slate_id),
            _impression(
                slate_id=stored_slate_id,
                source_event_id="evt_20260901_00000002",
            ),
        )
    )

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        validate_slate_identities((partition,))

    # Then
    assert captured.value.code is SnapshotErrorCode.DUPLICATE_SLATE_VIDEO
    assert captured.value.count == 2


@pytest.mark.parametrize(
    ("rank", "exposure_source", "policy_version"),
    (
        (None, "model", None),
        (0, "trending", None),
        (1, None, None),
        (None, None, "policy-v1"),
        (1, "unsupported", None),
    ),
    ids=("missing-rank", "zero-rank", "rank-only", "policy-only", "source-domain"),
)
def test_validate_slate_identities_rejects_incomplete_exposure_metadata(
    rank: int | None,
    exposure_source: str | None,
    policy_version: str | None,
) -> None:
    # Given
    partition = _partition(
        (
            _impression(
                slate_id="slt_20260901_000000000000000000000000",
                rank=rank,
                exposure_source=exposure_source,
                policy_version=policy_version,
            ),
        )
    )

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        validate_slate_identities((partition,))

    # Then
    assert captured.value.code is SnapshotErrorCode.SOURCE_SCHEMA_INVALID
    assert captured.value.dt == PARTITION_DATE


def test_validate_slate_identities_rejects_stored_id_collision() -> None:
    # Given
    stored_slate_id = "slt_20260901_000000000000000000000000"
    partition = _partition(
        (
            _impression(slate_id=stored_slate_id),
            _impression(
                slate_id=stored_slate_id,
                user_id="user-2",
                video_id="video-2",
                source_event_id="evt_20260901_00000002",
            ),
        )
    )

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        validate_slate_identities((partition,))

    # Then
    assert captured.value.code is SnapshotErrorCode.SLATE_ID_COLLISION


@pytest.mark.parametrize(
    ("exposure_source", "policy_version"),
    (("model", "policy-v1"), ("trending", None), ("random", "policy-v2")),
)
def test_validate_slate_identities_accepts_every_tagged_daily_source(
    exposure_source: ExposureSource,
    policy_version: str | None,
) -> None:
    # Given
    identity = SlateIdentity(
        PARTITION_DATE,
        "user-1",
        (SlateMember("video-1", 1, exposure_source, policy_version),),
    )
    partition = _partition(
        (
            _impression(
                slate_id=str(generate_slate_id(identity)),
                exposure_source=exposure_source,
                policy_version=policy_version,
            ),
        )
    )

    # When
    result = validate_slate_identities((partition,))

    # Then
    assert result is None
