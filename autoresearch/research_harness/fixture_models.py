"""Stage C local fixture와 candidate/Judge handoff의 typed contract.

[파이프라인] 결정적 fixture 입력 생성과 일일 action log·Stage B snapshot 실행 사이,
그리고 완성 snapshot과 candidate/Judge 소비 경계 사이의 불변 값을 정의한다.

[기능] Judge 전용 fixture descriptor·receipt와 candidate-safe data view manifest·receipt를
제공하고 상대 경로 및 seed의 기본 계약을 fail-closed로 검증한다. Metadata v2 manifest는
별도 모델에서 고정 경로·행 수·digest를 검증하며 기존 v1 reader 계약은 유지한다.

[비책임] 실제 일일 producer 실행, snapshot build, handoff 재검증과 candidate view 게시는
후속 Stage C orchestration 모듈이 담당한다.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Annotated, ClassVar, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from autoresearch.research_harness.evaluation_snapshot_models import (
    ArtifactReceipt,
    EvaluationId,
    SnapshotFingerprint,
    WriterIdentity,
)
from autoresearch.research_harness.evaluation_source_models import SourcePartitionReceipt
from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode


_DATACLASS_CONFIG: ConfigDict = ConfigDict(extra="forbid")
_EVALUATION_ID_PATTERN = re.compile(r"eval_[0-9a-f]{64}\Z")
FIXTURE_HISTORY_START_OFFSET_DAYS: Final = 2
FIXTURE_CHANNEL_PUBLISHED_OFFSET_DAYS: Final = 3650
FIXTURE_MAX_EVALUATION_PAST_OFFSET_DAYS: Final = (
    FIXTURE_HISTORY_START_OFFSET_DAYS + FIXTURE_CHANNEL_PUBLISHED_OFFSET_DAYS
)


@dataclass(frozen=True, slots=True)
class LocalEvaluationFixtureRequest:
    __pydantic_config__: ClassVar[ConfigDict] = _DATACLASS_CONFIG

    judge_state_root: Path
    evaluation_start_date: date
    fixture_seed: int

    def __post_init__(self) -> None:
        if isinstance(self.fixture_seed, bool) or not isinstance(self.fixture_seed, int):
            raise StageCError(
                StageCErrorCode.FIXTURE_REQUEST_INVALID,
                "fixture_request_validation",
            )
        if self.fixture_seed < 0:
            raise StageCError(
                StageCErrorCode.FIXTURE_REQUEST_INVALID,
                "fixture_request_validation",
            )
        require_fixture_date_window(self.evaluation_start_date)


@dataclass(frozen=True, slots=True)
class FixtureInputReceipt:
    __pydantic_config__: ClassVar[ConfigDict] = _DATACLASS_CONFIG

    relative_path: str
    rows: int
    sha256: str

    def __post_init__(self) -> None:
        _validate_receipt(self.relative_path, self.rows, self.sha256)


@dataclass(frozen=True, slots=True)
class FixturePartitionReceipt:
    __pydantic_config__: ClassVar[ConfigDict] = _DATACLASS_CONFIG

    dt: date
    relative_path: str
    rows: int
    sha256: str

    def __post_init__(self) -> None:
        _validate_receipt(self.relative_path, self.rows, self.sha256)


class FixtureDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["youtube-ctr-local-fixture-v1"]
    input_generator_version: Literal["youtube-ctr-input-v1"]
    input_writer: WriterIdentity
    fixture_seed: Annotated[int, Field(strict=True, ge=0)]
    generator: Literal["rule_based"]
    generator_model: Literal["fixture-rule-action-log"]
    history_start_date: date
    evaluation_start_date: date
    evaluation_end_date: date
    slate_id_cutover_date: date
    candidates_per_user: Literal[24]
    video_count_per_partition: Literal[48]
    click_threshold: Literal[0.0]
    personalized_ratio: Literal[0.7]
    popular_ratio: Literal[0.2]
    exploration_ratio: Literal[0.1]
    history_days_per_run: Literal[1]
    max_events_per_user_per_day: Literal[24]
    max_concurrency: Literal[1]
    chunk_size: Literal[0]
    max_quarantine_ratio: Literal[0.0]
    overwrite: Literal[False]
    validation_user_count: Literal[160]
    final_holdout_user_count: Literal[40]
    virtual_users: FixtureInputReceipt
    youtube_partitions: tuple[FixturePartitionReceipt, ...]

    @model_validator(mode="after")
    def require_canonical_fixture_semantics(self) -> Self:
        require_fixture_date_window(self.evaluation_start_date)
        history_start_date = self.evaluation_start_date - timedelta(
            days=FIXTURE_HISTORY_START_OFFSET_DAYS
        )
        expected_dates = tuple(
            self.evaluation_start_date + timedelta(days=offset)
            for offset in (-FIXTURE_HISTORY_START_OFFSET_DAYS, -1, 0, 1)
        )
        if (
            self.history_start_date != history_start_date
            or self.slate_id_cutover_date != history_start_date
            or self.evaluation_end_date != self.evaluation_start_date
        ):
            raise StageCError(
                StageCErrorCode.FIXTURE_REQUEST_INVALID,
                "fixture_descriptor_window_validation",
            )
        if (
            self.virtual_users.relative_path != "inputs/virtual_users.parquet"
            or self.virtual_users.rows != 200
        ):
            raise StageCError(
                StageCErrorCode.FIXTURE_REQUEST_INVALID,
                "fixture_descriptor_user_input_validation",
            )
        partition_dates = tuple(receipt.dt for receipt in self.youtube_partitions)
        if partition_dates != expected_dates:
            raise StageCError(
                StageCErrorCode.FIXTURE_REQUEST_INVALID,
                "fixture_descriptor_partition_order_validation",
            )
        for receipt in self.youtube_partitions:
            expected_path = (
                "inputs/youtube_trending_kr/"
                f"dt={receipt.dt.isoformat()}/part-0.parquet"
            )
            if receipt.relative_path != expected_path or receipt.rows != 48:
                raise StageCError(
                    StageCErrorCode.FIXTURE_REQUEST_INVALID,
                    "fixture_descriptor_partition_receipt_validation",
                    dt=receipt.dt,
                )
        return self

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: Literal["allow", "ignore", "forbid"] | None = None,
        context: object | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Parse a descriptor while mapping all schema failures to the Stage C code."""

        try:
            return super().model_validate_json(
                json_data,
                strict=strict,
                extra=extra,
                context=context,
                by_alias=by_alias,
                by_name=by_name,
            )
        except ValidationError:
            raise StageCError(
                StageCErrorCode.FIXTURE_REQUEST_INVALID,
                "fixture_descriptor_schema_validation",
            ) from None


@dataclass(frozen=True, slots=True)
class JudgeSnapshotHandoff:
    __pydantic_config__: ClassVar[ConfigDict] = _DATACLASS_CONFIG

    snapshot_fingerprint: SnapshotFingerprint
    snapshot_root: Path
    manifest_sha256: str
    validation_id: EvaluationId
    final_holdout_id: EvaluationId


@dataclass(frozen=True, slots=True)
class LocalEvaluationFixtureReceipt:
    __pydantic_config__: ClassVar[ConfigDict] = _DATACLASS_CONFIG

    fixture_root: Path
    descriptor_path: Path
    descriptor_sha256: str
    action_log_partitions: tuple[SourcePartitionReceipt, ...]
    judge: JudgeSnapshotHandoff
    reused: bool


@dataclass(frozen=True, slots=True)
class CandidateHistoryReceipt:
    __pydantic_config__: ClassVar[ConfigDict] = _DATACLASS_CONFIG

    dt: date
    relative_path: str
    rows: int
    sha256: str

    def __post_init__(self) -> None:
        _validate_receipt(
            self.relative_path,
            self.rows,
            self.sha256,
            code=StageCErrorCode.CANDIDATE_VIEW_CONFLICT,
        )
        expected_path = (
            f"history/action_log/dt={self.dt.isoformat()}/part-0.parquet"
        )
        if self.relative_path != expected_path:
            raise StageCError(
                StageCErrorCode.CANDIDATE_VIEW_CONFLICT,
                "candidate_history_path_validation",
                dt=self.dt,
            )


class CandidateDataManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["candidate-data-view-v1"]
    evaluation_id: EvaluationId
    evaluation_start_date: date
    complete_history_label_end_date: date
    slate: ArtifactReceipt
    history_partitions: tuple[CandidateHistoryReceipt, ...]

    @field_validator("evaluation_id")
    @classmethod
    def require_canonical_evaluation_id(
        cls,
        evaluation_id: EvaluationId,
    ) -> EvaluationId:
        if _EVALUATION_ID_PATTERN.fullmatch(str(evaluation_id)) is None:
            raise StageCError(
                StageCErrorCode.CANDIDATE_VIEW_CONFLICT,
                "candidate_evaluation_id_validation",
            )
        return evaluation_id

    @field_validator("slate")
    @classmethod
    def require_candidate_slate_path(cls, slate: ArtifactReceipt) -> ArtifactReceipt:
        if slate.relative_path != "slate.parquet":
            raise StageCError(
                StageCErrorCode.CANDIDATE_VIEW_CONFLICT,
                "candidate_manifest_validation",
            )
        _validate_receipt(
            slate.relative_path,
            slate.rows,
            slate.sha256,
            code=StageCErrorCode.CANDIDATE_VIEW_CONFLICT,
        )
        return slate

    @model_validator(mode="after")
    def require_candidate_history_window(self) -> Self:
        try:
            expected_complete_end = self.evaluation_start_date - timedelta(days=2)
        except OverflowError:
            raise StageCError(
                StageCErrorCode.CANDIDATE_VIEW_CONFLICT,
                "candidate_window_validation",
            ) from None
        if self.complete_history_label_end_date != expected_complete_end:
            raise StageCError(
                StageCErrorCode.CANDIDATE_VIEW_CONFLICT,
                "candidate_window_validation",
            )
        partition_dates = tuple(receipt.dt for receipt in self.history_partitions)
        if (
            partition_dates != tuple(sorted(set(partition_dates)))
            or any(
                partition_date >= self.evaluation_start_date
                for partition_date in partition_dates
            )
        ):
            raise StageCError(
                StageCErrorCode.CANDIDATE_VIEW_CONFLICT,
                "candidate_history_order_validation",
            )
        return self


class _MetadataArtifactReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    relative_path: str
    rows: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CandidateDataManifestV2(CandidateDataManifest):
    """v1 공통 시점 계약에 안전한 metadata 파일 receipt를 추가한 opt-in manifest."""

    contract_version: Literal["candidate-data-view-v2"]
    metadata_contract: Literal["candidate-metadata-v1"]
    user_metadata: _MetadataArtifactReceipt
    video_metadata: _MetadataArtifactReceipt

    @model_validator(mode="after")
    def require_metadata_paths(self) -> Self:
        if (
            self.user_metadata.relative_path != "metadata/users.parquet"
            or self.video_metadata.relative_path != "metadata/videos.parquet"
        ):
            raise StageCError(
                StageCErrorCode.CANDIDATE_VIEW_CONFLICT,
                "candidate_metadata_path_validation",
            )
        return self


@dataclass(frozen=True, slots=True)
class CandidateDataViewRequest:
    __pydantic_config__: ClassVar[ConfigDict] = _DATACLASS_CONFIG

    judge: JudgeSnapshotHandoff
    destination_root: Path


@dataclass(frozen=True, slots=True)
class CandidateDataViewReceipt:
    __pydantic_config__: ClassVar[ConfigDict] = _DATACLASS_CONFIG

    root: Path
    manifest: CandidateDataManifest
    manifest_sha256: str
    reused: bool


def _validate_receipt(
    relative_path: str,
    rows: int,
    digest: str,
    *,
    code: StageCErrorCode = StageCErrorCode.FIXTURE_REQUEST_INVALID,
) -> None:
    if not _is_posix_relative_path(relative_path):
        raise StageCError(code, "relative_path_validation")
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
        raise StageCError(code, "receipt_row_validation")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise StageCError(code, "receipt_digest_validation")


def _is_posix_relative_path(value: str) -> bool:
    if not value or "\\" in value:
        return False
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    return (
        not posix_path.is_absolute()
        and not windows_path.is_absolute()
        and windows_path.drive == ""
        and ".." not in posix_path.parts
        and str(posix_path) == value
    )


def require_fixture_date_window(evaluation_start_date: date) -> None:
    ordinal = evaluation_start_date.toordinal()
    if (
        ordinal
        < date.min.toordinal() + FIXTURE_MAX_EVALUATION_PAST_OFFSET_DAYS
        or ordinal > date.max.toordinal() - 1
    ):
        raise StageCError(
            StageCErrorCode.FIXTURE_REQUEST_INVALID,
            "fixture_date_window_validation",
        )
