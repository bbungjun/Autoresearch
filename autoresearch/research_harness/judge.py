"""validation prediction 계약을 검증하고 Judge metric을 계산한다.

[파이프라인] Stage C의 봉인 evaluation snapshot과 P0-2A ranking metric 뒤에서,
candidate prediction을 validation label과 1:1로 결합해 P0-2C 판정 입력을 만든다.

[기능] 검증된 handoff로 validation 전용 opaque target을 만들고, 공통 parser가 검증한
Judge 소유 CSV 사본을 ranking·probability metric 하나의 불변 결과로 결합한다.

[비책임] candidate 경로에서의 안전한 파일 ingestion·subprocess 자원 제한은
``prediction_ingestion``이, coverage·sigma 판정은 ``judge_decision``이, final holdout 소비
승인은 후속 registry가 담당한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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
from autoresearch.research_harness.judge_errors import JudgeError, JudgeErrorCode
from autoresearch.research_harness.local_evaluation_fixture import (
    _io_path,
    _validated_judge_snapshot,
)
from autoresearch.research_harness.prediction_parser import (
    MAX_IDENTIFIER_BYTES,
    PredictionFormatError,
    PredictionRow,
    is_canonical_ascii,
    parse_prediction_copy as _parse_prediction_copy,
)
from autoresearch.research_harness.prediction_ingestion import (
    SealedPredictionReceipt,
    iter_sealed_prediction_rows,
)
from autoresearch.research_harness.ranking_metrics import (
    RankingMetricError,
    RankingMetricResult,
    ndcg_at_k,
    recall_at_k,
)


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
    target = JudgeEvaluationTarget._from_verified(verified_handoff, manifest)
    _load_verified_target_rows(target)
    return target


def parse_prediction_copy(prediction_copy: Path) -> tuple[PredictionRow, ...]:
    """공통 parser 오류를 안정적인 Judge 오류로 변환한다."""

    try:
        return _parse_prediction_copy(prediction_copy)
    except PredictionFormatError as error:
        raise _invalid_predictions(error.stage, error.row_number) from None


def score_predictions(
    target: JudgeEvaluationTarget,
    sealed_prediction: SealedPredictionReceipt,
) -> JudgeScoringResult:
    """validation target과 exact 1:1 prediction을 결합해 모든 P0-2B 지표를 계산한다."""

    if not isinstance(target, JudgeEvaluationTarget):
        raise JudgeError(JudgeErrorCode.INVALID_TARGET, "target_type")
    target_rows = _load_verified_target_rows(target)
    prediction_rows = iter_sealed_prediction_rows(sealed_prediction)
    expected_id = target._handoff.validation_id

    prediction_by_key: dict[tuple[str, str], PredictionRow] = {}
    for row in prediction_rows:
        key = (row.slate_id, row.video_id)
        if row.evaluation_id != expected_id or key in prediction_by_key:
            raise _invalid_predictions("semantic_validation")
        prediction_by_key[key] = row

    target_by_key = {(row.slate_id, row.video_id): row for row in target_rows}
    if (
        len(prediction_by_key) != len(target_rows)
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
        slate_id = row["slate_id"]
        video_id = row["video_id"]
        user_id = row["user_id"]
        if (
            row["evaluation_id"] != evaluation_id
            or not isinstance(slate_id, str)
            or not isinstance(video_id, str)
            or not isinstance(user_id, str)
            or not user_id
            or user_id != user_id.strip()
        ):
            raise ValueError
        key = (slate_id, video_id)
        if key in slate_by_key or not all(
            _target_identifier_is_encodable(value) for value in key
        ):
            raise ValueError
        slate_by_key[key] = user_id

    target_rows: list[_TargetRow] = []
    label_keys: set[tuple[str, str]] = set()
    for row in label_rows:
        slate_id = row["slate_id"]
        video_id = row["video_id"]
        user_id = row["user_id"]
        if (
            row["evaluation_id"] != evaluation_id
            or not isinstance(slate_id, str)
            or not isinstance(video_id, str)
            or not isinstance(user_id, str)
            or not isinstance(row["clicked"], bool)
        ):
            raise ValueError
        key = (slate_id, video_id)
        if key in label_keys or slate_by_key.get(key) != user_id:
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


def _target_identifier_is_encodable(value: str) -> bool:
    try:
        token = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return (
        0 < len(token) <= MAX_IDENTIFIER_BYTES
        and is_canonical_ascii(token)
        and b"," not in token
    )


def _invalid_predictions(stage: str, row_number: int | None = None) -> JudgeError:
    return JudgeError(
        JudgeErrorCode.INVALID_PREDICTIONS,
        stage,
        row_number=row_number,
    )
