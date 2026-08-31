from dataclasses import replace
from datetime import date
from typing import Final

import pytest

from autoresearch.action_log_generation.slate_identity import (
    SlateId,
    SlateIdentity,
    SlateIdentityError,
    SlateIdentityErrorCode,
    SlateIdentityRegistry,
    SlateMember,
    canonical_slate_json,
    generate_slate_id,
)


PARTITION_DATE: Final = date(2026, 8, 31)
EXPECTED_SPEC_ID: Final = SlateId("slt_20260831_0cf0daf7c833035b191942e5")
EXPECTED_UNICODE_ID: Final = SlateId("slt_20260831_8124c4cf7c70838c98393718")


def _spec_identity() -> SlateIdentity:
    return SlateIdentity(
        partition_date=PARTITION_DATE,
        user_id="user-123",
        members=(
            SlateMember(
                video_id="video-456",
                rank=1,
                exposure_source="trending",
                policy_version=None,
            ),
        ),
    )


def test_generate_slate_id_matches_hard_coded_spec_vector() -> None:
    # Given
    identity = _spec_identity()

    # When
    slate_id = generate_slate_id(identity)

    # Then
    assert slate_id == EXPECTED_SPEC_ID


def test_generate_slate_id_matches_hard_coded_unicode_vector() -> None:
    # Given
    identity = SlateIdentity(
        partition_date=PARTITION_DATE,
        user_id="사용자-홍길동",
        members=(
            SlateMember("영상-二", 2, "model", "정책-β"),
            SlateMember("영상-가", 1, "trending", None),
        ),
    )

    # When
    slate_id = generate_slate_id(identity)

    # Then
    assert slate_id == EXPECTED_UNICODE_ID


def test_canonical_json_sorts_an_identity_copy_without_mutating_members() -> None:
    # Given
    members = (
        SlateMember("video-b", None, None, None),
        SlateMember("video-c", 2, "model", "policy-1"),
        SlateMember("video-a", 1, "trending", None),
    )
    identity = SlateIdentity(PARTITION_DATE, "user-123", members)

    # When
    payload = canonical_slate_json(identity)

    # Then
    assert identity.members == members
    assert payload.index(b'"video_id":"video-a"') < payload.index(
        b'"video_id":"video-c"'
    ) < payload.index(b'"video_id":"video-b"')


def test_generate_slate_id_is_invariant_to_member_input_order() -> None:
    # Given
    identity = SlateIdentity(
        PARTITION_DATE,
        "user-123",
        (
            SlateMember("video-b", 2, "model", "policy-1"),
            SlateMember("video-a", 1, "trending", None),
        ),
    )
    reversed_identity = replace(identity, members=tuple(reversed(identity.members)))

    # When
    generated = generate_slate_id(identity)
    generated_reversed = generate_slate_id(reversed_identity)

    # Then
    assert generated == generated_reversed


@pytest.mark.parametrize(
    "changed",
    [
        replace(_spec_identity(), user_id="user-124"),
        replace(
            _spec_identity(),
            members=(SlateMember("video-457", 1, "trending", None),),
        ),
        replace(
            _spec_identity(),
            members=(SlateMember("video-456", 2, "trending", None),),
        ),
        replace(
            _spec_identity(),
            members=(SlateMember("video-456", 1, "model", None),),
        ),
        replace(
            _spec_identity(),
            members=(SlateMember("video-456", 1, "trending", "policy-2"),),
        ),
        replace(_spec_identity(), partition_date=date(2026, 9, 1)),
    ],
    ids=["user", "video", "rank", "source", "policy", "date"],
)
def test_generate_slate_id_changes_when_identity_field_changes(
    changed: SlateIdentity,
) -> None:
    # Given
    baseline = _spec_identity()

    # When
    changed_id = generate_slate_id(changed)

    # Then
    assert changed_id != generate_slate_id(baseline)


def test_generate_slate_id_rejects_duplicate_video() -> None:
    # Given
    identity = SlateIdentity(
        PARTITION_DATE,
        "sensitive-user",
        (
            SlateMember("video-1", 1, "trending", None),
            SlateMember("video-1", 2, "model", "policy-1"),
        ),
    )

    # When
    with pytest.raises(SlateIdentityError) as exc_info:
        generate_slate_id(identity)

    # Then
    assert exc_info.value.code is SlateIdentityErrorCode.DUPLICATE_SLATE_VIDEO
    assert "sensitive-user" not in str(exc_info.value)


@pytest.mark.parametrize("rank", [None, 0, -1])
def test_generate_slate_id_rejects_invalid_rank_for_non_null_source(
    rank: int | None,
) -> None:
    # Given
    identity = SlateIdentity(
        PARTITION_DATE,
        "sensitive-user",
        (SlateMember("video-1", rank, "model", None),),
    )

    # When
    with pytest.raises(SlateIdentityError) as exc_info:
        generate_slate_id(identity)

    # Then
    assert exc_info.value.code is SlateIdentityErrorCode.INVALID_SLATE_EXPOSURE_RANK
    assert "sensitive-user" not in str(exc_info.value)


def test_registry_rejects_different_payload_for_forced_id_collision() -> None:
    # Given
    registry = SlateIdentityRegistry()
    forced_id = SlateId("slt_20260831_000000000000000000000000")
    registry.register(forced_id, b'{"identity":"first"}')

    # When
    with pytest.raises(SlateIdentityError) as exc_info:
        registry.register(forced_id, b'{"identity":"second"}')

    # Then
    assert exc_info.value.code is SlateIdentityErrorCode.SLATE_ID_COLLISION


def test_registry_allows_same_payload_when_reused_in_run() -> None:
    # Given
    registry = SlateIdentityRegistry()
    identity = _spec_identity()

    # When
    first = generate_slate_id(identity, registry=registry)
    repeated = generate_slate_id(identity, registry=registry)

    # Then
    assert first == repeated == EXPECTED_SPEC_ID
