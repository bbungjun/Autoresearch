"""일일 action log를 평가 snapshot용 typed partition으로 읽는 source adapter.

[파이프라인] 최종 일일 action log Parquet와 slate identity·click attribution 사이에서
필수 파티션을 읽고 원천 schema·KST slice·cutover 계약을 검증한다.

[기능] local/GCS 공통 Arrow filesystem seam, 전역 유일성이 검증된 source event와
partition receipt를 제공한다.

[비책임] canonical slate 재계산, click attribution, user split과 artifact 게시는 후속
Stage B 모듈이 담당한다.
"""

from __future__ import annotations

import hashlib
import re
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Final, Protocol, Self
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq
from pydantic import ValidationError

from autoresearch.action_log_generation.pipeline import (
    EVENT_LOG_PARQUET_SCHEMA,
    OPTIONAL_ADDITIVE_COLUMNS,
)
from autoresearch.action_log_generation.schema import EventLog
from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.evaluation_snapshot_models import EvaluationSnapshotRequest
from autoresearch.research_harness.evaluation_source_models import (
    LoadedPartition,
    SourceEvent,
    SourcePartitionReceipt,
)


_KST: Final = ZoneInfo("Asia/Seoul")
_EVENT_ID_PATTERN: Final = re.compile(r"^evt_(?P<date>\d{8})_(?P<sequence>\d{8})$")


class ActionLogSource(Protocol):
    @property
    def opaque_root(self) -> str: ...

    def partition_uri(self, dt: date) -> str: ...

    def open_partition(self, dt: date) -> AbstractContextManager[pa.NativeFile]: ...


@dataclass(frozen=True, slots=True)
class ArrowActionLogSource:
    opaque_root: str
    _filesystem: pafs.FileSystem
    _resolved_root: str

    @classmethod
    def from_root(cls, root: str) -> Self:
        filesystem, resolved_root = pafs.FileSystem.from_uri(root)
        return cls(
            opaque_root=root,
            _filesystem=filesystem,
            _resolved_root=resolved_root.replace("\\", "/"),
        )

    def partition_uri(self, dt: date) -> str:
        root = self.opaque_root.replace("\\", "/")
        return _partition_path(root, dt)

    def open_partition(self, dt: date) -> AbstractContextManager[pa.NativeFile]:
        return self._filesystem.open_input_file(_partition_path(self._resolved_root, dt))

    def _physical_source_root(self) -> Path | None:
        if isinstance(self._filesystem, pafs.LocalFileSystem):
            return Path(self._resolved_root)
        return None

    def _physical_partition_path(self, dt: date) -> Path | None:
        root = self._physical_source_root()
        if root is None:
            return None
        return root / f"dt={dt.isoformat()}" / "part-0.parquet"


def load_required_partitions(
    request: EvaluationSnapshotRequest,
    source: ActionLogSource | None = None,
) -> tuple[LoadedPartition, ...]:
    if source is None:
        try:
            active_source: ActionLogSource = ArrowActionLogSource.from_root(request.action_log_root)
        except (OSError, pa.ArrowException) as error:
            raise EvaluationSnapshotError(
                code=SnapshotErrorCode.SOURCE_PARTITION_MISSING,
                stage="source_resolution",
            ) from error
    else:
        active_source = source
    loaded: list[LoadedPartition] = []
    seen_event_ids: set[str] = set()
    partition_date = request.history_start_date
    final_date = request.evaluation_end_date + timedelta(days=1)
    while partition_date <= final_date:
        try:
            with active_source.open_partition(partition_date) as handle:
                payload = handle.read()
                handle.seek(0)
                table = pq.read_table(handle)
        except (FileNotFoundError, OSError, pa.ArrowException) as error:
            raise EvaluationSnapshotError(
                code=SnapshotErrorCode.SOURCE_PARTITION_MISSING,
                stage="source_read",
                dt=partition_date,
            ) from error
        actual_names = set(table.schema.names)
        type_mismatch = any(
            field.name in actual_names and table.schema.field(field.name).type != field.type
            for field in EVENT_LOG_PARQUET_SCHEMA
        )
        required_names = set(EVENT_LOG_PARQUET_SCHEMA.names) - OPTIONAL_ADDITIVE_COLUMNS
        if type_mismatch or required_names - actual_names:
            raise EvaluationSnapshotError(
                code=SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
                stage="schema_validation",
                dt=partition_date,
            )
        missing_cutover_slate = "slate_id" not in actual_names or (
            "slate_id" in actual_names and table.column("slate_id").null_count > 0
        )
        if partition_date >= request.slate_id_cutover_date and missing_cutover_slate:
            raise EvaluationSnapshotError(
                code=SnapshotErrorCode.SLATE_ID_MISSING_AFTER_CUTOVER,
                stage="cutover_validation",
                dt=partition_date,
            )
        rows = table.to_pylist()
        events: list[SourceEvent] = []
        for row in rows:
            try:
                event = EventLog.model_validate(row)
            except ValidationError as error:
                raise EvaluationSnapshotError(
                    code=SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
                    stage="row_validation",
                    dt=partition_date,
                ) from error
            if event.event_id in seen_event_ids:
                raise EvaluationSnapshotError(
                    code=SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
                    stage="row_validation",
                    dt=partition_date,
                    count=2,
                    identifier_prefix=event.event_id,
                )
            seen_event_ids.add(event.event_id)
            if event.event_timestamp.astimezone(_KST).date() != partition_date:
                raise EvaluationSnapshotError(
                    code=SnapshotErrorCode.PARTITION_TIMESTAMP_MISMATCH,
                    stage="partition_validation",
                    dt=partition_date,
                )
            event_id_match = _EVENT_ID_PATTERN.fullmatch(event.event_id)
            if event_id_match is None:
                raise EvaluationSnapshotError(
                    code=SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
                    stage="row_validation",
                    dt=partition_date,
                )
            if event_id_match.group("date") != partition_date.strftime("%Y%m%d"):
                raise EvaluationSnapshotError(
                    code=SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
                    stage="row_validation",
                    dt=partition_date,
                )
            has_valid_domain = bool(event.user_id and event.video_id)
            has_valid_rank = event.exposure_source is None or (
                event.rank is not None and event.rank >= 1
            )
            if not has_valid_domain or not has_valid_rank:
                raise EvaluationSnapshotError(
                    code=SnapshotErrorCode.SOURCE_SCHEMA_INVALID,
                    stage="row_validation",
                    dt=partition_date,
                )
            events.append(
                SourceEvent(
                    partition_date=partition_date,
                    source_event_id=event.event_id,
                    event_type=event.event_type,
                    user_id=event.user_id,
                    video_id=event.video_id,
                    event_timestamp=event.event_timestamp,
                    slate_id=event.slate_id,
                    rank=event.rank,
                    exposure_source=event.exposure_source,
                    policy_version=event.policy_version,
                )
            )
        loaded.append(
            LoadedPartition(
                receipt=SourcePartitionReceipt(
                    dt=partition_date,
                    uri=active_source.partition_uri(partition_date),
                    rows=table.num_rows,
                    sha256=hashlib.sha256(payload).hexdigest(),
                ),
                events=tuple(events),
            )
        )
        partition_date += timedelta(days=1)
    return tuple(loaded)


def _partition_path(root: str, partition_date: date) -> str:
    normalized_root = root.rstrip("/")
    suffix = f"dt={partition_date.isoformat()}/part-0.parquet"
    return f"{normalized_root}/{suffix}" if normalized_root else f"/{suffix}"
