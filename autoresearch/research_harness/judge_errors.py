"""Research Harness Judge의 정제된 오류 계약.

[파이프라인] prediction ingestion·parser·scoring 단계에서 발생한 실패를 Controller와 ledger가
분기할 수 있는 안정적인 코드로 바꾸는 공통 구간을 담당한다.

[기능] 원본 prediction 값과 candidate/Judge 경로를 포함하지 않는 오류 코드와 예외를
제공한다.

[비책임] 파일 봉인, CSV 검증, metric 계산과 판정은 각 전용 module이 담당한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique


@unique
class JudgeErrorCode(StrEnum):
    """P0-2 호출자가 안전하게 분기할 수 있는 오류 코드."""

    INVALID_TARGET = "invalid_judge_target"
    INVALID_PREDICTIONS = "invalid_predictions"


@dataclass(frozen=True, slots=True)
class JudgeError(Exception):
    """원본 prediction 값과 Judge path를 포함하지 않는 P0-2 오류."""

    code: JudgeErrorCode
    stage: str
    row_number: int | None = None

    def __str__(self) -> str:
        rendered = f"{self.code.value}: stage={self.stage}"
        if self.row_number is not None:
            rendered += f": row_number={self.row_number}"
        return rendered
