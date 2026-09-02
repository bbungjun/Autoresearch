"""validation prediction 계약을 검증하고 Judge metric을 계산한다.

[파이프라인] Stage C의 봉인 evaluation snapshot과 P0-2A ranking metric 뒤에서,
candidate prediction을 validation label과 1:1로 결합해 P0-2C 판정 입력을 만든다.

[기능] 검증된 handoff로 validation 전용 opaque target을 만들고, Judge 소유 CSV 사본을
streaming parse·검증한 뒤 ranking·probability metric을 하나의 불변 결과로 반환한다.

[비책임] candidate 경로에서의 안전한 파일 ingestion·subprocess 자원 제한·coverage gate·
sigma 판정과 final holdout 소비 승인은 P0-2C 및 후속 final registry가 담당한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique
from math import isfinite
from pathlib import Path
import re

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.model_evaluation.probability_metrics import (
    ProbabilityMetricResult,
    probability_metrics,
)
from autoresearch.research_harness.evaluation_snapshot_models import (
    ArtifactReceipt,
    EvaluationId,
    EvaluationSnapshotManifest,
)
from autoresearch.research_harness.fixture_errors import StageCError
from autoresearch.research_harness.fixture_models import JudgeSnapshotHandoff
from autoresearch.research_harness.local_evaluation_fixture import (
    _io_path,
    _validated_judge_snapshot,
)
from autoresearch.research_harness.ranking_metrics import (
    RankingMetricError,
    RankingMetricResult,
    ndcg_at_k,
    recall_at_k,
)


_PREDICTION_HEADER = b"evaluation_id,slate_id,video_id,score"
_EVALUATION_ID_PATTERN = re.compile(rb"eval_[0-9a-f]{64}\Z")
_MAX_IDENTIFIER_BYTES = 64
_MAX_SCORE_BYTES = 24
_MAX_PREDICTION_ROWS = 300_000
_MAX_ROW_BYTES = 226

_SLATE_SCHEMA = pa.schema(
    [
        pa.field("evaluation_id", pa.string(), nullable=False),
        pa.field("slate_id", pa.string(), nullable=False),
        pa.field("user_id", pa.string(), nullable=False),
        pa.field("video_id", pa.string(), nullable=False),
        pa.field("event_timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("candidate_source", pa.string()),
        pa.field("original_rank", pa.int64()),
    ]
)
_LABEL_SCHEMA = pa.schema(
    [
        pa.field("evaluation_id", pa.string(), nullable=False),
        pa.field("slate_id", pa.string(), nullable=False),
        pa.field("user_id", pa.string(), nullable=False),
        pa.field("video_id", pa.string(), nullable=False),
        pa.field("source_event_id", pa.string(), nullable=False),
        pa.field("clicked", pa.bool_(), nullable=False),
    ]
)


@unique
class JudgeErrorCode(StrEnum):
    """P0-2B 호출자가 안전하게 분기할 수 있는 오류 코드."""

    INVALID_TARGET = "invalid_judge_target"
    INVALID_PREDICTIONS = "invalid_predictions"


@dataclass(frozen=True, slots=True)
class JudgeError(Exception):
    """원본 prediction 값과 Judge path를 포함하지 않는 P0-2B 오류."""

    code: JudgeErrorCode
    stage: str
    row_number: int | None = None

    def __str__(self) -> str:
        rendered = f"{self.code.value}: stage={self.stage}"
        if self.row_number is not None:
            rendered += f": row_number={self.row_number}"
        return rendered


@dataclass(frozen=True, slots=True)
class PredictionRow:
    """동일 parser를 P0-2C에서도 재사용하기 위한 module 내부 typed row."""

    evaluation_id: EvaluationId
    slate_id: str
    video_id: str
    score: float


@dataclass(frozen=True, slots=True, init=False, repr=False)
class JudgeEvaluationTarget:
    """검증된 validation artifact에만 연결되는 opaque scoring target."""

    _handoff: JudgeSnapshotHandoff
    _slate: ArtifactReceipt
    _labels: ArtifactReceipt

    def __init__(self, *_: object, **__: object) -> None:
        raise JudgeError(JudgeErrorCode.INVALID_TARGET, "target_construction")

    @classmethod
    def _from_verified(
        cls,
        handoff: JudgeSnapshotHandoff,
        manifest: EvaluationSnapshotManifest,
    ) -> JudgeEvaluationTarget:
        target = object.__new__(cls)
        object.__setattr__(target, "_handoff", handoff)
        object.__setattr__(target, "_slate", manifest.validation.artifacts.slate)
        object.__setattr__(target, "_labels", manifest.validation.artifacts.labels)
        return target

    def __repr__(self) -> str:
        return "JudgeEvaluationTarget(<opaque>)"


@dataclass(frozen=True, slots=True)
class JudgeScoringResult:
    """P0-2C coverage gate와 판정이 소비할 validation metric 묶음."""

    evaluation_id: EvaluationId
    row_count: int
    ndcg_at_10: RankingMetricResult
    recall_at_10: RankingMetricResult
    ndcg_at_24: RankingMetricResult
    probability: ProbabilityMetricResult


@dataclass(frozen=True, slots=True)
class _TargetRow:
    slate_id: str
    video_id: str
    user_id: str
    label: int


def build_validation_target(
    handoff: JudgeSnapshotHandoff,
) -> JudgeEvaluationTarget:
    """Stage C handoff를 재검증해 validation 전용 opaque target을 만든다."""

    try:
        verified_handoff, manifest = _validated_judge_snapshot(
            handoff.snapshot_root,
            expected_fingerprint=str(handoff.snapshot_fingerprint),
        )
    except (AttributeError, StageCError):
        raise JudgeError(
            JudgeErrorCode.INVALID_TARGET,
            "target_validation",
        ) from None
    if verified_handoff != handoff:
        raise JudgeError(JudgeErrorCode.INVALID_TARGET, "target_identity")
    return JudgeEvaluationTarget._from_verified(verified_handoff, manifest)


def parse_prediction_copy(prediction_copy: Path) -> tuple[PredictionRow, ...]:
    """Judge 소유 CSV 사본을 한 번의 streaming parser 계약으로 검증한다."""

    rows: list[PredictionRow] = []
    try:
        with prediction_copy.open("rb") as stream:
            header = stream.readline(len(_PREDICTION_HEADER) + 3)
            if _without_line_ending(header) != _PREDICTION_HEADER:
                raise _invalid_predictions("header")
            row_number = 1
            while True:
                raw_line = stream.readline(_MAX_ROW_BYTES + 2)
                if not raw_line:
                    break
                row_number += 1
                if len(raw_line) > _MAX_ROW_BYTES or (
                    not raw_line.endswith(b"\n")
                    and len(raw_line) == _MAX_ROW_BYTES + 2
                ):
                    raise _invalid_predictions("field_bytes", row_number)
                if len(rows) >= _MAX_PREDICTION_ROWS:
                    raise _invalid_predictions("row_limit", row_number)
                rows.append(_parse_prediction_line(raw_line, row_number))
    except JudgeError:
        raise
    except (OSError, TypeError, ValueError):
        raise _invalid_predictions("read") from None
    return tuple(rows)


def score_predictions(
    target: JudgeEvaluationTarget,
    prediction_copy: Path,
) -> JudgeScoringResult:
    """validation target과 exact 1:1 prediction을 결합해 모든 P0-2B 지표를 계산한다."""

    if not isinstance(target, JudgeEvaluationTarget):
        raise JudgeError(JudgeErrorCode.INVALID_TARGET, "target_type")
    target_rows = _load_verified_target_rows(target)
    prediction_rows = parse_prediction_copy(prediction_copy)
    expected_id = target._handoff.validation_id

    prediction_by_key: dict[tuple[str, str], PredictionRow] = {}
    for row in prediction_rows:
        key = (row.slate_id, row.video_id)
        if row.evaluation_id != expected_id or key in prediction_by_key:
            raise _invalid_predictions("semantic_validation")
        prediction_by_key[key] = row

    target_by_key = {(row.slate_id, row.video_id): row for row in target_rows}
    if (
        len(prediction_rows) != len(target_rows)
        or len(target_by_key) != len(target_rows)
        or prediction_by_key.keys() != target_by_key.keys()
    ):
        raise _invalid_predictions("semantic_validation")

    ordered_keys = sorted(target_by_key)
    labels = [target_by_key[key].label for key in ordered_keys]
    scores = [prediction_by_key[key].score for key in ordered_keys]
    slate_ids = [key[0] for key in ordered_keys]
    video_ids = [key[1] for key in ordered_keys]
    groups = [target_by_key[key].user_id for key in ordered_keys]
    try:
        return JudgeScoringResult(
            evaluation_id=expected_id,
            row_count=len(target_rows),
            ndcg_at_10=ndcg_at_k(labels, scores, slate_ids, video_ids, k=10),
            recall_at_10=recall_at_k(labels, scores, slate_ids, video_ids, k=10),
            ndcg_at_24=ndcg_at_k(labels, scores, slate_ids, video_ids, k=24),
            probability=probability_metrics(labels, scores, groups),
        )
    except (RankingMetricError, ValueError):
        raise JudgeError(JudgeErrorCode.INVALID_TARGET, "metric_input") from None


def _load_verified_target_rows(
    target: JudgeEvaluationTarget,
) -> tuple[_TargetRow, ...]:
    try:
        verified_handoff, manifest = _validated_judge_snapshot(
            target._handoff.snapshot_root,
            expected_fingerprint=str(target._handoff.snapshot_fingerprint),
        )
        if (
            verified_handoff != target._handoff
            or manifest.validation.artifacts.slate != target._slate
            or manifest.validation.artifacts.labels != target._labels
        ):
            raise ValueError
        slate_table = pq.read_table(
            _io_path(target._handoff.snapshot_root / "validation" / "slate.parquet")
        )
        label_table = pq.read_table(
            _io_path(target._handoff.snapshot_root / "validation" / "labels.parquet")
        )
        if (
            slate_table.schema != _SLATE_SCHEMA
            or label_table.schema != _LABEL_SCHEMA
            or slate_table.num_rows != target._slate.rows
            or label_table.num_rows != target._labels.rows
        ):
            raise ValueError
        return _join_target_rows(
            slate_table.to_pylist(),
            label_table.to_pylist(),
            target._handoff.validation_id,
        )
    except (OSError, StageCError, ValueError, pa.ArrowException):
        raise JudgeError(JudgeErrorCode.INVALID_TARGET, "artifact_validation") from None


def _join_target_rows(
    slate_rows: list[dict[str, object]],
    label_rows: list[dict[str, object]],
    evaluation_id: EvaluationId,
) -> tuple[_TargetRow, ...]:
    slate_by_key: dict[tuple[str, str], str] = {}
    for row in slate_rows:
        key = (str(row["slate_id"]), str(row["video_id"]))
        if row["evaluation_id"] != evaluation_id or key in slate_by_key:
            raise ValueError
        slate_by_key[key] = str(row["user_id"])

    target_rows: list[_TargetRow] = []
    label_keys: set[tuple[str, str]] = set()
    for row in label_rows:
        key = (str(row["slate_id"]), str(row["video_id"]))
        user_id = str(row["user_id"])
        if (
            row["evaluation_id"] != evaluation_id
            or key in label_keys
            or slate_by_key.get(key) != user_id
            or not isinstance(row["clicked"], bool)
        ):
            raise ValueError
        label_keys.add(key)
        target_rows.append(
            _TargetRow(
                slate_id=key[0],
                video_id=key[1],
                user_id=user_id,
                label=int(row["clicked"]),
            )
        )
    if label_keys != slate_by_key.keys():
        raise ValueError
    return tuple(sorted(target_rows, key=lambda row: (row.slate_id, row.video_id)))


def _parse_prediction_line(raw_line: bytes, row_number: int) -> PredictionRow:
    fields = _without_line_ending(raw_line).split(b",")
    if len(fields) != 4:
        raise _invalid_predictions("schema", row_number)
    evaluation_token, slate_token, video_token, score_token = fields
    if _EVALUATION_ID_PATTERN.fullmatch(evaluation_token) is None:
        raise _invalid_predictions("evaluation_id", row_number)
    slate_id = _parse_identifier(slate_token, row_number)
    video_id = _parse_identifier(video_token, row_number)
    if (
        not score_token
        or len(score_token) > _MAX_SCORE_BYTES
        or not _is_canonical_ascii(score_token)
    ):
        raise _invalid_predictions("score", row_number)
    try:
        score = float(score_token.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise _invalid_predictions("score", row_number) from None
    if not isfinite(score) or not 0.0 <= score <= 1.0:
        raise _invalid_predictions("score", row_number)
    return PredictionRow(
        evaluation_id=EvaluationId(evaluation_token.decode("ascii")),
        slate_id=slate_id,
        video_id=video_id,
        score=score,
    )


def _parse_identifier(token: bytes, row_number: int) -> str:
    if (
        not token
        or len(token) > _MAX_IDENTIFIER_BYTES
        or not _is_canonical_ascii(token)
    ):
        raise _invalid_predictions("identifier", row_number)
    return token.decode("ascii")


def _is_canonical_ascii(token: bytes) -> bool:
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


def _invalid_predictions(stage: str, row_number: int | None = None) -> JudgeError:
    return JudgeError(
        JudgeErrorCode.INVALID_PREDICTIONS,
        stage,
        row_number=row_number,
    )
