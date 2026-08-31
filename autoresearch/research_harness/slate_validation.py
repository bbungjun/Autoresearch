"""평가 대상 일일 action log의 canonical slate identity 검증.

[파이프라인] 최종 일일 action log Parquet을 typed partition으로 읽은 직후와 click
attribution 이전 사이에서 producer가 저장한 slate identity를 검증한다.

[기능] 저장된 impression group의 canonical identity와 ``slate_id`` 일치 여부를
검증하고 안전한 Stage B 오류로 변환한다.

[비책임] legacy slate 추론, click attribution, user split과 artifact 쓰기는 담당하지
않는다.
"""

from collections import defaultdict
from datetime import date
from typing import Final

from autoresearch.action_log_generation.slate_identity import (
    ExposureSource,
    SlateId,
    SlateIdentity,
    SlateIdentityError,
    SlateIdentityErrorCode,
    SlateIdentityRegistry,
    SlateMember,
    canonical_slate_json,
    generate_slate_id,
)
from autoresearch.research_harness.evaluation_source_models import (
    LoadedPartition,
    SourceEvent,
)
from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)


_EXPOSURE_SOURCES: Final[dict[str, ExposureSource]] = {
    "model": "model",
    "trending": "trending",
    "random": "random",
}
_SNAPSHOT_ERROR_BY_IDENTITY_ERROR: Final[
    dict[SlateIdentityErrorCode, SnapshotErrorCode]
] = {
    SlateIdentityErrorCode.DUPLICATE_SLATE_VIDEO: SnapshotErrorCode.DUPLICATE_SLATE_VIDEO,
    SlateIdentityErrorCode.INVALID_SLATE_EXPOSURE_RANK: SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
    SlateIdentityErrorCode.SLATE_ID_COLLISION: SnapshotErrorCode.SLATE_ID_COLLISION,
}


def validate_slate_identities(partitions: tuple[LoadedPartition, ...]) -> None:
    """일일 producer가 저장한 slate identity를 검증한다."""

    groups: dict[tuple[date, str, str], list[SourceEvent]] = defaultdict(list)
    for partition in partitions:
        for event in partition.events:
            if event.event_type == "impression" and event.slate_id is not None:
                groups[(event.partition_date, event.slate_id, event.user_id)].append(event)

    registry = SlateIdentityRegistry()
    identities: list[tuple[date, str, SlateIdentity]] = []
    for group_key in sorted(groups, key=lambda key: (str(key[0]), key[1], key[2])):
        partition_date, stored_slate_id, user_id = group_key
        events = groups[group_key]
        identity = SlateIdentity(
            partition_date=partition_date,
            user_id=user_id,
            members=tuple(_member_for_event(event) for event in events),
        )
        try:
            canonical_payload = canonical_slate_json(identity)
        except SlateIdentityError as error:
            raise EvaluationSnapshotError(
                code=_SNAPSHOT_ERROR_BY_IDENTITY_ERROR[error.code],
                stage="slate_identity_validation",
                dt=partition_date,
                count=error.member_count,
            ) from error
        try:
            registry.register(SlateId(stored_slate_id), canonical_payload)
        except SlateIdentityError as error:
            raise EvaluationSnapshotError(
                code=_SNAPSHOT_ERROR_BY_IDENTITY_ERROR[error.code],
                stage="slate_identity_validation",
                dt=partition_date,
                identifier_prefix=stored_slate_id,
            ) from error
        identities.append((partition_date, stored_slate_id, identity))

    for partition_date, stored_slate_id, identity in identities:
        generated_slate_id = generate_slate_id(identity)
        if generated_slate_id != stored_slate_id:
            raise EvaluationSnapshotError(
                code=SnapshotErrorCode.SLATE_ID_INVALID,
                stage="slate_identity_validation",
                dt=partition_date,
                identifier_prefix=stored_slate_id,
            )


def _member_for_event(event: SourceEvent) -> SlateMember:
    if event.exposure_source is None:
        if event.rank is not None or event.policy_version is not None:
            raise EvaluationSnapshotError(
                code=SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
                stage="slate_identity_validation",
                dt=event.partition_date,
            )
        return SlateMember(
            video_id=event.video_id,
            rank=None,
            exposure_source=None,
            policy_version=None,
        )
    exposure_source = _EXPOSURE_SOURCES.get(event.exposure_source)
    if exposure_source is None or event.rank is None or event.rank < 1:
        raise EvaluationSnapshotError(
            code=SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
            stage="slate_identity_validation",
            dt=event.partition_date,
        )
    return SlateMember(
        video_id=event.video_id,
        rank=event.rank,
        exposure_source=exposure_source,
        policy_version=event.policy_version,
    )
