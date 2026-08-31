"""평가 snapshot 원천 event와 파티션의 내부 값 객체.

[파이프라인] action log Parquet 원천을 읽은 직후와 attribution 이전 사이에서 typed
event·partition receipt를 보존한다.

[기능] 검증된 source event, partition receipt 및 loaded partition 형태를 제공한다.

[비책임] Parquet I/O, row schema 검증, cutover 판정은 후속 source adapter가 담당한다.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class SourceEvent:
    partition_date: date
    source_event_id: str
    event_type: Literal["impression", "click", "view", "like"]
    user_id: str
    video_id: str
    event_timestamp: datetime
    slate_id: str | None
    rank: int | None
    exposure_source: str | None
    policy_version: str | None


@dataclass(frozen=True, slots=True)
class SourcePartitionReceipt:
    dt: date
    uri: str
    rows: int
    sha256: str


@dataclass(frozen=True, slots=True)
class LoadedPartition:
    receipt: SourcePartitionReceipt
    events: tuple[SourceEvent, ...]
