"""평가 snapshot의 구조화된 오류 계약.

[파이프라인] action log 원천을 평가 snapshot으로 검증·변환하는 Stage B 전 구간에서
안전한 오류 문맥을 전달한다.

[기능] Stage B 검증 실패의 reason code와 예외 타입을 제공한다.

[비책임] 오류를 HTTP/CLI 응답으로 변환하거나 재시도 정책을 정하지 않는다.
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum, unique


@unique
class SnapshotErrorCode(StrEnum):
    INVALID_DATE_RANGE = "invalid_date_range"
    SOURCE_PARTITION_MISSING = "source_partition_missing"
    SOURCE_SCHEMA_INVALID = "source_schema_invalid"
    PARTITION_TIMESTAMP_MISMATCH = "partition_timestamp_mismatch"
    SLATE_ID_MISSING_AFTER_CUTOVER = "slate_id_missing_after_cutover"
    SLATE_ID_INVALID = "slate_id_invalid"
    SLATE_ID_COLLISION = "slate_id_collision"
    DUPLICATE_SLATE_VIDEO = "duplicate_slate_video"
    SLATE_ATTRIBUTION_MISMATCH = "slate_attribution_mismatch"
    SPLIT_COVERAGE_INSUFFICIENT = "split_coverage_insufficient"
    SNAPSHOT_WRITE_CONFLICT = "snapshot_write_conflict"


@dataclass(frozen=True, slots=True)
class EvaluationSnapshotError(Exception):
    code: SnapshotErrorCode
    stage: str
    dt: date | None = None
    count: int | None = None
    identifier_prefix: str | None = None

    def __post_init__(self) -> None:
        if self.identifier_prefix is not None:
            object.__setattr__(self, "identifier_prefix", _truncate_identifier_prefix(self.identifier_prefix))

    def __str__(self) -> str:
        return (
            f"{self.code}: stage={self.stage}, dt={self.dt}, count={self.count}, "
            f"identifier_prefix={self.identifier_prefix}"
        )


def _truncate_identifier_prefix(value: str) -> str:
    retained: list[str] = []
    encoded_length = 0
    for character in value:
        character_length = len(character.encode("utf-8"))
        if encoded_length + character_length > 16:
            break
        retained.append(character)
        encoded_length += character_length
    return "".join(retained)
