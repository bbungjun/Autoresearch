"""평가 snapshot 요청·manifest·artifact의 typed contract.

[파이프라인] source validation, attribution, split, artifact writer, publisher가
공유하는 불변 snapshot 경계를 정의한다.

[기능] 요청·시간창·split·writer·manifest 및 receipt의 직렬화 가능한 모델을 제공한다.

[비책임] attribution 계산, user split, Parquet 쓰기와 write-once publish는 각 후속
Stage B 모듈이 담당한다.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, ClassVar, Literal, NewType

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.evaluation_source_models import SourcePartitionReceipt


EvaluationId = NewType("EvaluationId", str)
SnapshotFingerprint = NewType("SnapshotFingerprint", str)
SplitName = Literal["validation", "final_holdout"]


@dataclass(frozen=True, slots=True)
class EvaluationSnapshotRequest:
    action_log_root: str
    history_start_date: date
    evaluation_start_date: date
    evaluation_end_date: date
    slate_id_cutover_date: date
    output_root: Path

    def __post_init__(self) -> None:
        if (
            self.history_start_date >= self.evaluation_start_date
            or self.evaluation_start_date > self.evaluation_end_date
            or self.slate_id_cutover_date > self.evaluation_start_date
        ):
            raise EvaluationSnapshotError(
                code=SnapshotErrorCode.INVALID_DATE_RANGE,
                stage="request_validation",
            )

@dataclass(frozen=True, slots=True)
class SnapshotSource:
    __pydantic_config__: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    root: str
    partitions: tuple[SourcePartitionReceipt, ...]
    slate_id_cutover_date: date


@dataclass(frozen=True, slots=True)
class EvaluationWindow:
    history_start_date: date
    evaluation_start_date: date
    evaluation_end_date: date
    label_scan_end_date: date
    complete_history_label_end_date: date
    candidate_history_partitions: tuple[SourcePartitionReceipt, ...]


@dataclass(frozen=True, slots=True)
class AttributedImpression:
    slate_id: str
    user_id: str
    video_id: str
    event_timestamp: datetime
    source_event_id: str
    clicked: bool
    original_rank: int | None
    candidate_source: str | None


@dataclass(frozen=True, slots=True)
class EvaluationSplit:
    name: SplitName
    rows: tuple[AttributedImpression, ...]
    user_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AttributionContract:
    version: Literal["click-attribution-v1"]
    lookback_seconds: Literal[1800]
    tie_break: tuple[str, str]


@dataclass(frozen=True, slots=True)
class SplitContract:
    version: Literal["user-hash-80-20-v1"]
    salt: Literal["research-harness-slate-v1:"]
    validation_buckets: tuple[int, ...]
    final_holdout_buckets: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class WriterOptions:
    version: Literal["2.6"]
    coerce_timestamps: Literal["us"]
    allow_truncated_timestamps: Literal[False]
    use_deprecated_int96_timestamps: Literal[False]
    compression: Literal["NONE"]
    use_dictionary: Literal[False]
    row_group_size: Literal[50000]
    write_statistics: Literal[True]
    data_page_version: Literal["1.0"]
    store_schema: Literal[True]


@dataclass(frozen=True, slots=True)
class WriterIdentity:
    engine: Literal["pyarrow"]
    version: str
    options: WriterOptions


@dataclass(frozen=True, slots=True)
class ArtifactReceipt:
    relative_path: str
    rows: int
    sha256: str


@dataclass(frozen=True, slots=True)
class SplitArtifacts:
    slate: ArtifactReceipt
    labels: ArtifactReceipt


@dataclass(frozen=True, slots=True)
class SplitCounts:
    user_count: int
    slate_count: int
    row_count: int
    clicked_row_count: int
    click_positive_slate_count: int
    click_positive_slate_ratio: float
    mean_slate_size: float


class OptionalNonNullRatio(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_source: Annotated[float, Field(strict=True, ge=0.0, le=1.0)]
    original_rank: Annotated[float, Field(strict=True, ge=0.0, le=1.0)]


@dataclass(frozen=True, slots=True)
class SplitSummary:
    evaluation_id: EvaluationId
    counts: SplitCounts
    optional_non_null_ratio: OptionalNonNullRatio
    artifacts: SplitArtifacts

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "optional_non_null_ratio",
            OptionalNonNullRatio.model_validate(self.optional_non_null_ratio),
        )


class EvaluationSnapshotManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["evaluation-slate-snapshot-v1"]
    snapshot_fingerprint: SnapshotFingerprint
    created_at: datetime
    source: SnapshotSource
    window: EvaluationWindow
    attribution: AttributionContract
    split: SplitContract
    writer: WriterIdentity
    validation: SplitSummary
    final_holdout: SplitSummary

    @field_validator("created_at")
    @classmethod
    def require_timezone_aware_created_at(cls, created_at: datetime) -> datetime:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise EvaluationSnapshotError(
                code=SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
                stage="manifest_validation",
            )
        return created_at.astimezone(UTC)

    @field_serializer("created_at")
    def serialize_created_at(self, created_at: datetime) -> str:
        return created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

@dataclass(frozen=True, slots=True)
class SnapshotArtifactInput:
    request: EvaluationSnapshotRequest
    window: EvaluationWindow
    partitions: tuple[SourcePartitionReceipt, ...]
    validation: EvaluationSplit
    final_holdout: EvaluationSplit
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EvaluationSnapshotReceipt:
    snapshot_fingerprint: SnapshotFingerprint
    target_path: Path
    validation_id: EvaluationId
    final_holdout_id: EvaluationId
    reused: bool
