"""Stage C local fixture와 candidate handoff의 구조화된 오류 계약.

[파이프라인] Stage B 평가 snapshot 뒤와 candidate/Judge 소비 경계 앞에서 local
fixture 입력·상태와 handoff 검증 실패를 안전한 reason code로 전달한다.

[기능] Stage C 실패 code와 원문 식별자·경로를 메시지에 포함하지 않는 typed
exception을 제공한다.

[비책임] 일일 producer 실행, snapshot 생성, candidate view 게시와 재시도 정책은
후속 Stage C orchestration 모듈이 담당한다.
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum, unique


@unique
class StageCErrorCode(StrEnum):
    FIXTURE_REQUEST_INVALID = "fixture_request_invalid"
    FIXTURE_COVERAGE_INSUFFICIENT = "fixture_coverage_insufficient"
    FIXTURE_STATE_CONFLICT = "fixture_state_conflict"
    CANDIDATE_VIEW_CONFLICT = "candidate_view_conflict"
    JUDGE_HANDOFF_INVALID = "judge_handoff_invalid"
    FIXTURE_REPRODUCIBILITY_MISMATCH = "fixture_reproducibility_mismatch"


@dataclass(frozen=True, slots=True)
class StageCError(Exception):
    code: StageCErrorCode
    stage: str
    dt: date | None = None
    count: int | None = None
    identifier_prefix: str | None = None

    def __post_init__(self) -> None:
        if self.identifier_prefix is not None:
            object.__setattr__(
                self,
                "identifier_prefix",
                _truncate_identifier_prefix(self.identifier_prefix),
            )

    def __str__(self) -> str:
        return (
            f"{self.code}: stage={self.stage}, dt={self.dt}, count={self.count}, "
            f"identifier_present={self.identifier_prefix is not None}"
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
