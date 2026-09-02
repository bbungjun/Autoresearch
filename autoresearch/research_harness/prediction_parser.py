"""Judge 소유 prediction CSV의 결정적 streaming parser.

[파이프라인] candidate prediction 봉인 사본 생성 뒤, metric 결합 전에 CSV의 물리·값
계약을 검증하는 구간을 담당한다.

[기능] 격리 parser subprocess가 Judge scoring용 정규화 행을 만들 때 사용하는 단일 parser
구현과 typed row를 제공한다.

[비책임] candidate 경로 열기·사본 생성·자원 제한은 ``prediction_ingestion``이, target과
지표 결합은 ``judge``가, coverage·sigma 판정은 ``judge_decision``이 담당한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from collections.abc import Iterator
from pathlib import Path
import re

PREDICTION_HEADER = b"evaluation_id,slate_id,video_id,score"
MAX_IDENTIFIER_BYTES = 64
MAX_SCORE_BYTES = 24
MAX_PREDICTION_ROWS = 300_000
MAX_ROW_BYTES = 226

_EVALUATION_ID_PATTERN = re.compile(rb"eval_[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class PredictionFormatError(Exception):
    """원본 token을 노출하지 않는 parser 내부 계약 오류."""

    stage: str
    row_number: int | None = None


@dataclass(frozen=True, slots=True)
class PredictionRow:
    """검증을 마친 prediction 한 행."""

    evaluation_id: str
    slate_id: str
    video_id: str
    score: float


def parse_prediction_copy(prediction_copy: Path) -> tuple[PredictionRow, ...]:
    """Judge 소유 CSV 사본을 streaming parse해 typed row로 반환한다."""

    return tuple(iter_prediction_copy(prediction_copy))


def iter_prediction_copy(prediction_copy: Path) -> Iterator[PredictionRow]:
    """검증된 행을 보존하지 않고 순서대로 내보낸다."""

    try:
        with prediction_copy.open("rb") as stream:
            header = stream.readline(len(PREDICTION_HEADER) + 3)
            if _without_line_ending(header) != PREDICTION_HEADER:
                raise PredictionFormatError("header")
            row_number = 1
            while True:
                raw_line = stream.readline(MAX_ROW_BYTES + 2)
                if not raw_line:
                    break
                row_number += 1
                if len(raw_line) > MAX_ROW_BYTES or (
                    not raw_line.endswith(b"\n")
                    and len(raw_line) == MAX_ROW_BYTES + 2
                ):
                    raise PredictionFormatError("field_bytes", row_number)
                if row_number > MAX_PREDICTION_ROWS + 1:
                    raise PredictionFormatError("row_limit", row_number)
                yield _parse_prediction_line(raw_line, row_number)
    except PredictionFormatError:
        raise
    except (OSError, TypeError, ValueError):
        raise PredictionFormatError("read") from None


def _parse_prediction_line(raw_line: bytes, row_number: int) -> PredictionRow:
    fields = _without_line_ending(raw_line).split(b",")
    if len(fields) != 4:
        raise PredictionFormatError("schema", row_number)
    evaluation_token, slate_token, video_token, score_token = fields
    if _EVALUATION_ID_PATTERN.fullmatch(evaluation_token) is None:
        raise PredictionFormatError("evaluation_id", row_number)
    slate_id = _parse_identifier(slate_token, row_number)
    video_id = _parse_identifier(video_token, row_number)
    if (
        not score_token
        or len(score_token) > MAX_SCORE_BYTES
        or not is_canonical_ascii(score_token)
    ):
        raise PredictionFormatError("score", row_number)
    try:
        score = float(score_token.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise PredictionFormatError("score", row_number) from None
    if not isfinite(score) or not 0.0 <= score <= 1.0:
        raise PredictionFormatError("score", row_number)
    return PredictionRow(
        evaluation_id=evaluation_token.decode("ascii"),
        slate_id=slate_id,
        video_id=video_id,
        score=score,
    )


def _parse_identifier(token: bytes, row_number: int) -> str:
    if (
        not token
        or len(token) > MAX_IDENTIFIER_BYTES
        or not is_canonical_ascii(token)
    ):
        raise PredictionFormatError("identifier", row_number)
    return token.decode("ascii")


def is_canonical_ascii(token: bytes) -> bool:
    """CSV quoting 없이 표현 가능한 canonical printable ASCII인지 판정한다."""

    return (
        all(0x20 <= byte <= 0x7E for byte in token)
        and b'"' not in token
        and token == token.strip()
    )


def _without_line_ending(raw_line: bytes) -> bytes:
    if raw_line.endswith(b"\r\n"):
        return raw_line[:-2]
    if raw_line.endswith(b"\n"):
        return raw_line[:-1]
    return raw_line
