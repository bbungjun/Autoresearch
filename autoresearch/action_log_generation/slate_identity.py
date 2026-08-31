"""일일 노출 묶음 확정과 action log event 확장 사이의 slate identity 계약.

[파이프라인] 일일 추천 후보의 노출 메타데이터가 확정된 뒤 impression·click·view·like
action log로 확장되기 전 구간에서 결정적 slate 식별자를 만든다.

[기능] immutable member/identity 값의 canonical JSON과 96-bit SHA-256 기반 ID를
생성하고, 한 실행 안에서 서로 다른 identity가 같은 ID를 쓰는 충돌을 거부한다.

[비책임] event 행 전파와 저장 schema는 ``pipeline.py``·``schema.py``가, 일일/shard
context 배선은 ``daily.py``가 담당하며 이 모듈은 입력 draft나 event 순서를 변경하지 않는다.
"""

from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum, unique
import hashlib
import json
from typing import Final, Literal, NewType


SlateId = NewType("SlateId", str)
ExposureSource = Literal["model", "trending", "random"]
SlateProducer = Literal["daily-action-log-v1"]
SlateIdentityVersion = Literal["action-log-slate-v1"]

DEFAULT_PRODUCER: Final[SlateProducer] = "daily-action-log-v1"
IDENTITY_VERSION: Final[SlateIdentityVersion] = "action-log-slate-v1"
_DIGEST_HEX_LENGTH: Final = 24


@dataclass(frozen=True, slots=True)
class SlateMember:
    """한 slate의 결정적 identity에 참여하는 노출 member 값."""

    video_id: str
    rank: int | None
    exposure_source: ExposureSource | None
    policy_version: str | None


@dataclass(frozen=True, slots=True)
class SlateIdentity:
    """일일 producer가 한 user에게 확정한 후보 묶음 identity."""

    partition_date: date
    user_id: str
    members: tuple[SlateMember, ...]
    producer: SlateProducer = DEFAULT_PRODUCER
    version: SlateIdentityVersion = IDENTITY_VERSION


@unique
class SlateIdentityErrorCode(StrEnum):
    """호출자가 기계적으로 분기할 수 있는 slate identity 실패 코드."""

    DUPLICATE_SLATE_VIDEO = "duplicate_slate_video"
    INVALID_SLATE_EXPOSURE_RANK = "invalid_slate_exposure_rank"
    SLATE_ID_COLLISION = "slate_id_collision"


@dataclass(frozen=True, slots=True)
class SlateIdentityError(Exception):
    """민감한 user 식별자를 포함하지 않는 typed identity 계약 오류."""

    code: SlateIdentityErrorCode
    partition_date: date | None = None
    member_count: int | None = None

    def __str__(self) -> str:
        date_text = self.partition_date.isoformat() if self.partition_date else "unknown"
        count_text = str(self.member_count) if self.member_count is not None else "unknown"
        return (
            f"slate identity rejected: code={self.code.value} "
            f"dt={date_text} member_count={count_text}"
        )


class SlateIdentityRegistry:
    """한 생성 실행 동안 ID와 canonical payload의 일대일 대응을 누적한다."""

    __slots__ = ("_payload_by_id",)

    def __init__(self) -> None:
        self._payload_by_id: dict[SlateId, bytes] = {}

    def register(self, slate_id: SlateId, canonical_payload: bytes) -> None:
        """같은 ID가 다른 canonical payload를 가리키면 생성을 중단한다."""

        existing = self._payload_by_id.get(slate_id)
        if existing is not None and existing != canonical_payload:
            raise SlateIdentityError(code=SlateIdentityErrorCode.SLATE_ID_COLLISION)
        self._payload_by_id[slate_id] = canonical_payload


def _canonical_identity(identity: SlateIdentity) -> SlateIdentity:
    seen_video_ids: set[str] = set()
    for member in identity.members:
        if member.video_id in seen_video_ids:
            raise SlateIdentityError(
                code=SlateIdentityErrorCode.DUPLICATE_SLATE_VIDEO,
                partition_date=identity.partition_date,
                member_count=len(identity.members),
            )
        seen_video_ids.add(member.video_id)
        if member.exposure_source is not None and (
            member.rank is None or member.rank < 1
        ):
            raise SlateIdentityError(
                code=SlateIdentityErrorCode.INVALID_SLATE_EXPOSURE_RANK,
                partition_date=identity.partition_date,
                member_count=len(identity.members),
            )

    members = tuple(
        sorted(
            identity.members,
            key=lambda member: (
                member.rank is None,
                member.rank if member.rank is not None else 0,
                member.video_id,
            ),
        )
    )
    return replace(identity, members=members)


def canonical_slate_json(identity: SlateIdentity) -> bytes:
    """검증·정렬한 identity의 compact canonical UTF-8 JSON을 반환한다."""

    canonical = _canonical_identity(identity)
    payload = {
        "members": [
            {
                "exposure_source": member.exposure_source,
                "policy_version": member.policy_version,
                "rank": member.rank,
                "video_id": member.video_id,
            }
            for member in canonical.members
        ],
        "partition_date": canonical.partition_date.isoformat(),
        "producer": canonical.producer,
        "user_id": canonical.user_id,
        "version": canonical.version,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def generate_slate_id(
    identity: SlateIdentity,
    *,
    registry: SlateIdentityRegistry | None = None,
) -> SlateId:
    """canonical identity에서 결정적 일일 slate ID를 만들고 선택적으로 등록한다."""

    canonical_payload = canonical_slate_json(identity)
    digest = hashlib.sha256(canonical_payload).hexdigest()[:_DIGEST_HEX_LENGTH]
    slate_id = SlateId(f"slt_{identity.partition_date:%Y%m%d}_{digest}")
    if registry is not None:
        registry.register(slate_id, canonical_payload)
    return slate_id
