"""Stage C local fixture와 candidate/Judge handoff의 typed contract.

[파이프라인] 결정적 fixture 입력 생성과 일일 action log·Stage B snapshot 실행 사이,
그리고 완성 snapshot과 candidate/Judge 소비 경계 사이의 불변 값을 정의한다.

[기능] Judge 전용 fixture descriptor·receipt와 candidate-safe data view manifest·receipt를
제공하고 상대 경로 및 seed의 기본 계약을 fail-closed로 검증한다.

[비책임] 실제 일일 producer 실행, snapshot build, handoff 재검증과 candidate view 게시는
후속 Stage C orchestration 모듈이 담당한다.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from autoresearch.research_harness.evaluation_snapshot_models import (
    ArtifactReceipt,
    EvaluationId,
    SnapshotFingerprint,
    WriterIdentity,
)
from autoresearch.research_harness.evaluation_source_models import SourcePartitionReceipt
from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode


_DATACLASS_CONFIG: ConfigDict = ConfigDict(extra="forbid")


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


class CandidateDataManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["candidate-data-view-v1"]
    evaluation_id: EvaluationId
    evaluation_start_date: date
    complete_history_label_end_date: date
    slate: ArtifactReceipt
    history_partitions: tuple[CandidateHistoryReceipt, ...]

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
