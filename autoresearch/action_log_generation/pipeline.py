"""Virtual user 후보를 LLM 판정에서 action log 산출물로 변환한다.

[파이프라인] 노출 후보 조립 다음, action log 파티션 publish 이전 구간에서
LLM 판정·클릭 선정·이벤트 확장·로컬 산출물 기록을 담당한다.

[기능] shard/checkpoint가 사용하는 legacy draft/batch 계약, 일일 slate identity
전파·충돌 검증, bounded active-user 스트리밍, Parquet row-group 및 JSONL
기록, completion-time publish 실패 시 기존 산출물 복구를 제공한다.

[비책임] 일일 partition 검증·publish는 autoresearch/action_logs/daily.py,
공개 CLI dispatch는 autoresearch/jobs/action_log.py, KPO resource 설정은
SKYAHO/Autoresearch-airflow가 소유한다.
"""
import json
import logging
import math
import os
import random
import tempfile
from collections import defaultdict, deque
from collections.abc import Mapping, MutableMapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from types import TracebackType
from typing import Callable, Literal, Protocol, Self, TextIO

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from autoresearch.action_log_generation.candidate import build_candidates
from autoresearch.action_log_generation.observability import (
    ActionLogStreamingTelemetryReporter,
    ActionLogTelemetryReporter,
    action_log_work_log_context,
)
from autoresearch.action_log_generation.schema import (
    ACTION_LOG_SCHEMA_VERSION,
    PROMPT_VERSION,
    SOURCE_HISTORICAL,
    EventGenerationRequest,
    EventGenerationResult,
    EventLog,
    EventLogBatch,
    ImpressionDraft,
    QuarantineRecord,
)
from autoresearch.action_log_generation.slate_identity import (
    SlateId,
    SlateIdentity,
    SlateIdentityRegistry,
    SlateMember,
    generate_slate_id,
)
from autoresearch.action_log_generation.video_source import _MAX_DURATION, nominal_duration_sec


logger = logging.getLogger(__name__)

# 클릭 세션(impression 직후 click→view→like)이 impression 시각 뒤로 늘어날 수 있는 최대 초.
# like 지연 상한은 max(2, watch)이고, watch = round(watch_fraction × duration)이며 duration은
# nominal_duration_sec가 _MAX_DURATION으로 캡한다 → 세션 span의 상한이 _MAX_DURATION에 결합된다.
_CLICK_DELAY_MAX_SEC = 30
_VIEW_DELAY_MAX_SEC = 5
_MAX_SESSION_SPAN_SEC = _CLICK_DELAY_MAX_SEC + _VIEW_DELAY_MAX_SEC + max(2, _MAX_DURATION)
# impression을 history_end에서 최소 이만큼(시간, 올림) 이전에 두면 위 세션 span을 항상 흡수해
# 모든 후속 이벤트가 history_end를 넘지 않는다. _MAX_DURATION을 키우면 자동으로 여유가 늘어난다.
_MIN_IMPRESSION_HOURS = max(1, math.ceil(_MAX_SESSION_SPAN_SEC / 3600))

# 이벤트 KST 날짜가 event_id 네임스페이스다(#295 A안: dt 파티션 = KST 당일 슬라이스).
_KST = timezone(timedelta(hours=9))
_STREAMING_ACTIVE_USER_MULTIPLIER = 4


class ActionLogGenerator(Protocol):
    """pipeline이 generator 구현을 동일 방식으로 호출하기 위한 인터페이스."""

    model_name: str

    def generate(self, virtual_user: dict, videos: list[dict]) -> str:
        """유저 1명 × 후보 영상 목록에 대한 raw judgments JSON text를 반환한다."""

        ...


class ActionLogGenerationError(RuntimeError):
    """격리 비율이 임계치를 넘어 전량/대량 실패로 판정될 때 발생한다."""


SlateContractErrorCode = Literal[
    "slate_capacity_exceeded",
    "slate_partition_date_mismatch",
]


@dataclass(frozen=True, slots=True)
class ActionLogSlateContractError(RuntimeError):
    """일일 slate의 용량·partition 계약 위반을 식별하는 오류."""

    code: SlateContractErrorCode
    partition_date: date
    member_count: int | None = None

    def __str__(self) -> str:
        count_text = str(self.member_count) if self.member_count is not None else "unknown"
        return (
            f"daily slate rejected: code={self.code} "
            f"dt={self.partition_date.isoformat()} member_count={count_text}"
        )


@dataclass(frozen=True)
class ActionLogDraftGenerationResult:
    """LLM judgment draft 생성 결과와 quarantine 요약."""

    drafts: list[ImpressionDraft]
    quarantine: list[QuarantineRecord]
    total_work: int

    @property
    def summary(self) -> dict[str, int]:
        counts = {"api_error": 0, "invalid_json": 0, "schema_fail": 0}
        for record in self.quarantine:
            counts[record.error_type] += 1
        return {
            "drafts": len(self.drafts),
            "quarantined_users": len(self.quarantine),
            "total_work": self.total_work,
            **counts,
        }


@dataclass(frozen=True)
class ActionLogSingleResult:
    """bounded active-user 단일 실행의 파일 기록 결과 요약."""

    execution_mode: Literal["streaming", "legacy"]
    total_events: int
    impressions: int
    clicks: int
    quarantined_users: int
    api_error: int
    invalid_json: int
    schema_fail: int

    @property
    def summary(self) -> dict[str, int | float]:
        """legacy batch summary와 같은 집계 필드를 반환한다."""

        return {
            "total_events": self.total_events,
            "impressions": self.impressions,
            "clicks": self.clicks,
            "ctr": round(self.clicks / self.impressions, 4)
            if self.impressions
            else 0.0,
            "quarantined_users": self.quarantined_users,
            "api_error": self.api_error,
            "invalid_json": self.invalid_json,
            "schema_fail": self.schema_fail,
        }


@dataclass(frozen=True)
class ActionLogProgressSnapshot:
    """LLM chunk 생성 진행률을 외부 reporter로 전달하기 위한 스냅샷."""

    status: Literal["running", "success", "failed"]
    completed_chunks: int
    total_chunks: int
    success_chunks: int
    failed_chunks: int
    quarantined_chunks: int


ActionLogProgressCallback = Callable[[ActionLogProgressSnapshot], float | None]
ActionLogWorkIdFactory = Callable[[str, int], str]
ActionLogCheckpointCallback = Callable[[str, int, list[ImpressionDraft]], None]


@dataclass(frozen=True)
class _StreamingRetentionSnapshot:
    """active-user streaming 경로의 현재 보존 payload 수량."""

    phase: Literal["generating", "finalizing"]
    active_users: int
    buffered_drafts: int
    buffered_events: int
    in_flight_work: int
    activated_users: int
    total_users: int
    submitted_work: int
    total_work: int | None
    completed_work: int
    failed_work: int
    pending_work: int | None

# 유저별 노출 후보를 외부에서 결정할 때 쓰는 주입 지점. (virtual_user, user_rng)를
# 받아 video dict 목록을 반환한다. None이면 기존 build_candidates 휴리스틱을 쓴다.
CandidateProvider = Callable[[dict, random.Random], list[dict]]


@dataclass(frozen=True)
class ActionLogCheckpointPart:
    """durable checkpoint part에서 복원한 한 work의 성공 draft."""

    work_id: str
    work_order: int
    drafts: list[ImpressionDraft]


@dataclass(frozen=True)
class _ActionLogWorkItem:
    """결정론적 원본 순서를 가진 유저 후보 chunk 작업."""

    work_id: str
    user_id: str
    virtual_user: dict
    candidates: list[dict]


@dataclass
class _StreamingUserState:
    """drain 전 한 사용자의 chunk 결과만 보관하는 bounded coordinator 상태."""

    user_sequence: int
    user_id: str
    virtual_user: dict
    work: list[_ActionLogWorkItem]
    drafts_by_chunk: dict[int, list[ImpressionDraft]]
    quarantine_by_chunk: dict[int, QuarantineRecord]
    remaining_chunks: int


@dataclass(frozen=True)
class _ActionLogCallResult:
    """worker가 완결한 생성·검증 결과와 서로 겹치지 않는 timing."""

    work_sequence: int
    submitted_at: float
    started_at: float
    request_elapsed_ms: float
    parse_elapsed_ms: float
    raw_text: str = ""
    drafts: list[ImpressionDraft] | None = None
    error_type: Literal["api_error", "invalid_json", "schema_fail"] | None = None
    error: Exception | None = None


EVENT_LOG_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("event_id", pa.string()),
        pa.field("event_timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("user_id", pa.string()),
        pa.field("event_type", pa.string()),
        pa.field("video_id", pa.string()),
        pa.field("watch_time_sec", pa.int64()),
        pa.field("rank", pa.int64()),
        pa.field("source", pa.string()),
        pa.field("policy", pa.string()),
        pa.field("ctr_score", pa.float64()),
        pa.field("is_exploration", pa.bool_()),
        pa.field("policy_version", pa.string()),
        pa.field("exposure_source", pa.string()),
        pa.field("slate_id", pa.string()),
        pa.field("schema_version", pa.string()),
        pa.field("prompt_version", pa.string()),
        pa.field("llm_model", pa.string()),
        pa.field("generated_at", pa.string()),
    ]
)

_EVENT_SPOOL_SCHEMA = pa.schema(
    [field for field in EVENT_LOG_PARQUET_SCHEMA if field.name != "generated_at"]
)
_PARQUET_TARGET_ROW_GROUP_ROWS = 50_000

# additive 확장 컬럼 — 이 컬럼이 없는 legacy 파티션 스키마도 event log 계약에서
# 관용한다 (#221). event log 스키마 계약의 단일 출처로 이곳에 둔다.
OPTIONAL_ADDITIVE_COLUMNS = frozenset({"exposure_source", "slate_id"})

ACTION_LOG_DRAFT_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("user_id", pa.string()),
        pa.field("video_id", pa.string()),
        pa.field("click_propensity", pa.float64()),
        pa.field("watch_fraction", pa.float64()),
        pa.field("would_like", pa.bool_()),
        pa.field("duration_sec", pa.int64()),
        pa.field("exposure_source", pa.string()),
        pa.field("exposure_rank", pa.int64()),
        pa.field("exposure_ctr_score", pa.float64()),
        pa.field("policy_version", pa.string()),
    ]
)

ACTION_LOG_CHECKPOINT_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("work_id", pa.string()),
        pa.field("work_order", pa.int64()),
        *ACTION_LOG_DRAFT_PARQUET_SCHEMA,
    ]
)


def _clamp01(value: object) -> float:
    """소프트 신호를 0~1로 클램프(경미한 범위 이탈은 격리 대신 보정)."""

    return max(0.0, min(1.0, float(value)))


WOULD_LIKE_CLICK_THRESHOLD = 0.7
WOULD_LIKE_WATCH_THRESHOLD = 0.6


def derive_would_like(click_propensity: float, watch_fraction: float) -> bool:
    """click/watch 신호로 좋아요(만족) 여부를 결정론적으로 파생한다.

    LLM 출력 토큰 절감을 위해 would_like는 응답에서 제거하고 코드로 판정한다.
    임계값은 like 이벤트 볼륨에 직접 영향을 주므로 캘리브레이션 대상이다.
    """

    return (
        click_propensity >= WOULD_LIKE_CLICK_THRESHOLD
        and watch_fraction >= WOULD_LIKE_WATCH_THRESHOLD
    )


def _build_user_drafts(
    virtual_user: dict,
    candidates: list[dict],
    raw_text: str,
) -> list[ImpressionDraft]:
    """LLM raw judgments를 파싱해 후보별 ImpressionDraft를 만든다.

    응답은 인덱스 포맷({"j": [[index, click_propensity, watch_fraction], ...]})이며,
    index는 후보의 0-base 배열 위치다. 각 판정을 index로 후보에 재결합하므로 LLM이
    순서를 바꿔 반환해도 오정렬되지 않는다. index 집합이 정확히 0..n-1(각 1회)이 아니면
    (개수 불일치·범위 이탈·중복·누락) 라벨 무결성을 보장할 수 없어 ValueError로
    격리(schema_fail)한다. would_like는 click/watch로부터 코드에서 파생한다.

    json.JSONDecodeError -> invalid_json. 구조/타입 오류(ValueError/KeyError/TypeError/
    AttributeError/ValidationError) -> schema_fail.
    """
    data = json.loads(raw_text)  # invalid_json
    judgments = data["j"]  # KeyError/TypeError
    n = len(candidates)
    if not isinstance(judgments, list) or len(judgments) != n:
        got = len(judgments) if isinstance(judgments, list) else "non-list"
        raise ValueError(f"judgment count mismatch: got {got}, expected {n}")

    by_index: dict[int, tuple[object, object]] = {}
    for entry in judgments:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            raise ValueError(f"judgment entry must be [index, cp, wf]: {entry!r}")
        raw_index = entry[0]
        # bool은 int의 subclass라 명시적으로 배제. 정수값 float(3.0)은 허용.
        if isinstance(raw_index, bool) or not isinstance(raw_index, (int, float)):
            raise ValueError(f"judgment index must be an integer: {raw_index!r}")
        if float(raw_index) != int(raw_index):
            raise ValueError(f"judgment index must be an integer: {raw_index!r}")
        index = int(raw_index)
        if not 0 <= index < n:
            raise ValueError(f"judgment index out of range: {index} (n={n})")
        if index in by_index:
            raise ValueError(f"duplicate judgment index: {index}")
        by_index[index] = (entry[1], entry[2])
    # len==n + 범위 [0,n) + 중복 없음 => index 집합은 정확히 0..n-1 (누락도 배제).

    user_id = str(virtual_user.get("user_id", ""))
    drafts: list[ImpressionDraft] = []
    for i, video in enumerate(candidates):
        cp_raw, wf_raw = by_index[i]
        vid = video["video_id"]
        prop = _clamp01(cp_raw)
        frac = _clamp01(wf_raw)
        drafts.append(
            ImpressionDraft(
                user_id=user_id,
                video_id=vid,
                click_propensity=prop,
                watch_fraction=frac,
                would_like=derive_would_like(prop, frac),
                duration_sec=nominal_duration_sec(vid),
            )
        )
    return drafts


def _try_build_user_drafts(
    virtual_user: dict,
    candidates: list[dict],
    raw_text: str,
) -> tuple[
    list[ImpressionDraft] | None,
    Literal["invalid_json", "schema_fail"] | None,
    Exception | None,
]:
    """raw 응답을 draft로 파싱하고 격리 분류를 값으로 반환한다."""

    try:
        return _build_user_drafts(virtual_user, candidates, raw_text), None, None
    except json.JSONDecodeError as exc:
        return None, "invalid_json", exc
    except (
        ValidationError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        return None, "schema_fail", exc


def _generate_action_log_work(
    generator: ActionLogGenerator,
    item: _ActionLogWorkItem,
    *,
    work_sequence: int,
    submitted_at: float,
    shard_index: int | None,
    detailed_telemetry: bool,
) -> _ActionLogCallResult:
    """한 worker에서 최초 요청부터 선택적 schema 교정과 검증까지 완결한다.

    request 시간은 generator 호출만, parse 시간은 draft 검증만 각각 누적한다.
    schema retry API 오류가 나더라도 최초 응답의 검증 시간과 최종 예외를 함께
    보존해 coordinator가 실제 최종 상태로 격리할 수 있게 한다.
    """

    started_at = monotonic()
    request_elapsed_ms = 0.0
    parse_elapsed_ms = 0.0
    raw_text = ""

    with action_log_work_log_context(
        shard_index=shard_index,
        work_sequence=work_sequence,
        detailed=detailed_telemetry,
    ):
        request_started_at = monotonic()
        try:
            raw_text = generator.generate(item.virtual_user, item.candidates)
        except Exception as exc:  # noqa: BLE001 - worker API boundary
            request_elapsed_ms += (monotonic() - request_started_at) * 1000
            return _ActionLogCallResult(
                work_sequence=work_sequence,
                submitted_at=submitted_at,
                started_at=started_at,
                request_elapsed_ms=request_elapsed_ms,
                parse_elapsed_ms=parse_elapsed_ms,
                error_type="api_error",
                error=exc,
            )
        request_elapsed_ms += (monotonic() - request_started_at) * 1000

        parse_started_at = monotonic()
        drafts, error_type, parse_error = _try_build_user_drafts(
            item.virtual_user,
            item.candidates,
            raw_text,
        )
        parse_elapsed_ms += (monotonic() - parse_started_at) * 1000

        schema_retry = getattr(generator, "generate_schema_retry", None)
        if drafts is None and callable(schema_retry):
            assert error_type is not None
            logger.warning(
                "Retrying action log judgment after response validation failure",
                extra={
                    "user_id": item.user_id,
                    "error_type": error_type,
                    "model_name": getattr(generator, "model_name", "unknown"),
                },
            )
            retry_started_at = monotonic()
            try:
                raw_text = schema_retry(
                    item.virtual_user,
                    item.candidates,
                    error_type=error_type,
                )
            except Exception as exc:  # noqa: BLE001 - schema retry API boundary
                request_elapsed_ms += (monotonic() - retry_started_at) * 1000
                return _ActionLogCallResult(
                    work_sequence=work_sequence,
                    submitted_at=submitted_at,
                    started_at=started_at,
                    request_elapsed_ms=request_elapsed_ms,
                    parse_elapsed_ms=parse_elapsed_ms,
                    raw_text=raw_text,
                    error_type="api_error",
                    error=exc,
                )
            request_elapsed_ms += (monotonic() - retry_started_at) * 1000

            parse_started_at = monotonic()
            drafts, error_type, parse_error = _try_build_user_drafts(
                item.virtual_user,
                item.candidates,
                raw_text,
            )
            parse_elapsed_ms += (monotonic() - parse_started_at) * 1000

    if drafts is None:
        assert error_type is not None and parse_error is not None
    else:
        assert error_type is None and parse_error is None
    return _ActionLogCallResult(
        work_sequence=work_sequence,
        submitted_at=submitted_at,
        started_at=started_at,
        request_elapsed_ms=request_elapsed_ms,
        parse_elapsed_ms=parse_elapsed_ms,
        raw_text=raw_text,
        drafts=drafts,
        error_type=error_type,
        error=parse_error,
    )



def _chunked(seq: list, size: int):
    """size>0이면 seq를 size 단위로 쪼개고, 아니면 통째로 하나만 내보낸다."""

    if size and size > 0:
        for start in range(0, len(seq), size):
            yield seq[start : start + size]
    else:
        yield seq


def _generate_drafts_isolated(
    generator: ActionLogGenerator,
    virtual_users: list[dict],
    videos: list[dict],
    request: EventGenerationRequest,
    progress_callback: ActionLogProgressCallback | None = None,
    work_id_factory: ActionLogWorkIdFactory | None = None,
    completed_work: dict[str, list[ImpressionDraft]] | None = None,
    checkpoint_callback: ActionLogCheckpointCallback | None = None,
    shard_index: int | None = None,
    candidate_provider: CandidateProvider | None = None,
) -> tuple[list[ImpressionDraft], list[QuarantineRecord], int]:
    """LLM 판정을 (유저×후보청크) 단위로 격리·병렬 생성한다.

    후보를 chunk_size로 쪼개 각 청크가 독립 LLM 콜(작은 context)이 되게 하고,
    콜은 max_concurrency로 병렬 실행한다. user_id·조립은 원본(유저,청크) 순서로
    처리하므로 병렬이어도 결정론. 한 청크 실패가 배치를 죽이지 않는다.
    반환: (drafts, quarantine, 총 작업(청크) 수).
    """

    # 1) 결정론적 작업 목록: (work_id, user_id, virtual_user, chunk_candidates)
    work: list[_ActionLogWorkItem] = []
    for index, virtual_user in enumerate(virtual_users):
        user_id = str(virtual_user.get("user_id", f"user_{index}"))
        user_rng = random.Random(f"{request.seed}:{user_id}")
        if candidate_provider is not None:
            candidates = candidate_provider(virtual_user, user_rng)
        else:
            candidates = build_candidates(
                virtual_user,
                videos,
                request.candidates_per_user,
                request.exploration_ratio,
                user_rng,
                personalized_ratio=request.personalized_ratio,
                popular_ratio=request.popular_ratio,
            )
        if not candidates:
            continue
        for chunk_index, chunk in enumerate(_chunked(candidates, request.chunk_size)):
            work_id = (
                work_id_factory(user_id, chunk_index)
                if work_id_factory is not None
                else f"work_{len(work):08d}"
            )
            work.append(
                _ActionLogWorkItem(
                    work_id=work_id,
                    user_id=user_id,
                    virtual_user=virtual_user,
                    candidates=chunk,
                )
            )

    work_ids = [item.work_id for item in work]
    if len(work_ids) != len(set(work_ids)):
        raise ValueError("duplicate action log work_id")

    # 2) 최초 LLM 콜부터 선택적 schema retry와 파싱까지 work 단위로 병렬화한다.
    # 결과는 작업 index별로 보관해 최종 조립 순서는 기존처럼 원본 순서를 유지한다.
    drafts_by_index: dict[int, list[ImpressionDraft]] = {}
    quarantine_by_index: dict[int, QuarantineRecord] = {}
    total_chunks = len(work)
    restored_work = completed_work or {}
    for index, item in enumerate(work):
        restored = restored_work.get(item.work_id)
        if restored is not None:
            drafts_by_index[index] = restored
    completed_chunks = len(drafts_by_index)
    success_chunks = len(drafts_by_index)
    failed_chunks = 0
    quarantined_chunks = 0
    telemetry = ActionLogTelemetryReporter(
        logger=logger,
        shard_index=shard_index,
        total_work=total_chunks,
        initial_completed_work=completed_chunks,
    )

    def _emit_progress(status: Literal["running", "success", "failed"]) -> float:
        if progress_callback is None:
            return 0.0
        snapshot = ActionLogProgressSnapshot(
            status=status,
            completed_chunks=completed_chunks,
            total_chunks=total_chunks,
            success_chunks=success_chunks,
            failed_chunks=failed_chunks,
            quarantined_chunks=quarantined_chunks,
        )
        started_at = monotonic()
        try:
            reported_elapsed_ms = progress_callback(snapshot)
        except Exception:  # noqa: BLE001 - progress reporting must not fail generation
            logger.warning("Action log progress callback failed", exc_info=True)
            return (monotonic() - started_at) * 1000
        if isinstance(reported_elapsed_ms, (int, float)) and not isinstance(
            reported_elapsed_ms,
            bool,
        ):
            return float(reported_elapsed_ms)
        return (monotonic() - started_at) * 1000

    def _call(i: int, submitted_at: float) -> _ActionLogCallResult:
        return _generate_action_log_work(
            generator,
            work[i],
            work_sequence=i,
            submitted_at=submitted_at,
            shard_index=shard_index,
            detailed_telemetry=telemetry.detailed,
        )

    _emit_progress("running")
    telemetry.start(
        completed_work=completed_chunks,
        failed_work=failed_chunks,
        active_workers=0,
        pending_work=total_chunks - completed_chunks,
    )
    pending_indices = iter(i for i in range(total_chunks) if i not in drafts_by_index)
    max_workers = max(1, request.max_concurrency)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures: dict[Future[_ActionLogCallResult], tuple[int, float]] = {}

        def _submit_next() -> bool:
            try:
                index = next(pending_indices)
            except StopIteration:
                return False
            submitted_at = monotonic()
            futures[executor.submit(_call, index, submitted_at)] = (
                index,
                submitted_at,
            )
            return True

        for _ in range(max_workers):
            if not _submit_next():
                break

        while futures:
            done, _pending = wait(futures, return_when=FIRST_COMPLETED)
            completed_batch: list[tuple[_ActionLogCallResult, float, int]] = []
            for future in sorted(done, key=lambda item: futures[item][0]):
                i, _submitted_at = futures.pop(future)
                item = work[i]
                # generator의 외부 API 오류는 worker가 명시적인 결과로 변환한다.
                # 여기까지 전파된 예외는 내부 버그이므로 api_error로 위장하지 않는다.
                call_result = future.result()

                succeeded_drafts = call_result.drafts
                failure: QuarantineRecord | None = None
                if succeeded_drafts is None:
                    assert call_result.error_type is not None
                    assert call_result.error is not None
                    failure = QuarantineRecord(
                        user_id=item.user_id,
                        virtual_user=item.virtual_user,
                        raw_llm_response=call_result.raw_text,
                        error_type=call_result.error_type,
                        error_message=str(call_result.error),
                    )

                checkpoint_write_elapsed_ms = 0.0
                checkpoint_rows = 0
                if succeeded_drafts is not None:
                    if checkpoint_callback is not None:
                        checkpoint_started_at = monotonic()
                        try:
                            checkpoint_callback(item.work_id, i, succeeded_drafts)
                        finally:
                            checkpoint_write_elapsed_ms = (
                                monotonic() - checkpoint_started_at
                            ) * 1000
                    checkpoint_rows = len(succeeded_drafts)
                    drafts_by_index[i] = succeeded_drafts
                    success_chunks += 1
                else:
                    assert failure is not None
                    quarantine_by_index[i] = failure
                    failed_chunks += 1
                    quarantined_chunks += 1
                completed_chunks += 1
                completed_batch.append(
                    (
                        call_result,
                        checkpoint_write_elapsed_ms,
                        checkpoint_rows,
                    )
                )

            progress_write_elapsed_ms = _emit_progress("running")
            submit_elapsed_by_work: list[float] = []
            for _ in completed_batch:
                submit_started_at = monotonic()
                _submit_next()
                submit_elapsed_by_work.append(
                    (monotonic() - submit_started_at) * 1000
                )
            active_workers = len(futures)
            pending_work = max(
                0,
                total_chunks - completed_chunks - active_workers,
            )
            last_batch_index = len(completed_batch) - 1
            for batch_index, (
                call_result,
                checkpoint_write_elapsed_ms,
                checkpoint_rows,
            ) in enumerate(completed_batch):
                telemetry.record(
                    work_sequence=call_result.work_sequence,
                    queue_wait_ms=(
                        call_result.started_at - call_result.submitted_at
                    )
                    * 1000,
                    request_elapsed_ms=call_result.request_elapsed_ms,
                    parse_elapsed_ms=call_result.parse_elapsed_ms,
                    checkpoint_write_elapsed_ms=checkpoint_write_elapsed_ms,
                    checkpoint_rows=checkpoint_rows,
                    progress_write_elapsed_ms=(
                        progress_write_elapsed_ms
                        if batch_index == last_batch_index
                        else 0.0
                    ),
                    submit_elapsed_ms=submit_elapsed_by_work[batch_index],
                    total_elapsed_ms=(
                        monotonic() - call_result.submitted_at
                    )
                    * 1000,
                    completed_work=completed_chunks,
                    failed_work=failed_chunks,
                    active_workers=active_workers,
                    pending_work=pending_work,
                )

    telemetry.finish(
        completed_work=completed_chunks,
        failed_work=failed_chunks,
    )

    # 3) 조립은 원본 순서로 단일 스레드에서(결정론). 실패는 quarantine.
    drafts: list[ImpressionDraft] = []
    quarantine: list[QuarantineRecord] = []
    for i in range(total_chunks):
        if i in quarantine_by_index:
            quarantine.append(quarantine_by_index[i])
        else:
            drafts.extend(drafts_by_index[i])
    return drafts, quarantine, total_chunks


def select_clicks_per_slate(
    drafts: list[ImpressionDraft], click_threshold: float
) -> set[int]:
    """유저(슬레이트)별 click_propensity 최고 1개가 커트라인 이상이면 그 draft
    인덱스를 클릭으로 선정한다. 최고가 커트라인 미만이면 그 유저는 클릭 0개.

    동점은 (-click_propensity, video_id)로 결정적으로 깬다(높은 점수 우선,
    같으면 video_id 작은 쪽). 전역 할당량이 아니라 관련성 커트라인이므로
    CTR은 점수 분포(모델 실력)에 따라 창발한다.
    """
    indices_by_user: dict[str, list[int]] = {}
    for index, draft in enumerate(drafts):
        indices_by_user.setdefault(draft.user_id, []).append(index)

    clicked: set[int] = set()
    for indices in indices_by_user.values():
        top = min(
            indices,
            key=lambda i: (-drafts[i].click_propensity, drafts[i].video_id),
        )
        if drafts[top].click_propensity >= click_threshold:
            clicked.add(top)
    return clicked


@dataclass(frozen=True)
class ExposureMetadata:
    """정책 시뮬레이션 노출 1건의 로그 태깅 메타데이터. 키는 (user_id, video_id)."""

    policy: Literal["baseline", "model"]
    rank: int
    ctr_score: float | None
    is_exploration: bool | None
    policy_version: str | None
    exposure_source: Literal["model", "trending", "random"] | None = None


def _has_duplicate_user_ids(virtual_users: list[dict]) -> bool:
    """streaming이 보존할 수 없는 중복 user_id 입력을 감지한다."""

    user_ids = [str(user.get("user_id", "")) for user in virtual_users]
    return len(user_ids) != len(set(user_ids))


def _consume_user_exposure_metadata(
    metadata: MutableMapping[tuple[str, str], ExposureMetadata],
    user_id: str,
) -> dict[tuple[str, str], ExposureMetadata]:
    """drain 완료 사용자의 노출 metadata를 반환하고 공유 맵에서 제거한다."""

    keys = [key for key in metadata if key[0] == user_id]
    return {key: metadata.pop(key) for key in keys}


def attach_exposure_tags(
    drafts: list[ImpressionDraft],
    metadata: Mapping[tuple[str, str], ExposureMetadata],
) -> list[ImpressionDraft]:
    """provider가 남긴 노출 태그를 draft에 심는다(맵에 없는 draft는 무태그 유지)."""

    tagged: list[ImpressionDraft] = []
    for draft in drafts:
        meta = metadata.get((draft.user_id, draft.video_id))
        if meta is None or meta.exposure_source is None:
            tagged.append(draft)
            continue
        tagged.append(
            draft.model_copy(
                update={
                    "exposure_source": meta.exposure_source,
                    "exposure_rank": meta.rank,
                    "exposure_ctr_score": meta.ctr_score,
                    "policy_version": meta.policy_version,
                }
            )
        )
    return tagged


def _exposure_metadata_from_drafts(
    drafts: list[ImpressionDraft],
) -> dict[tuple[str, str], ExposureMetadata]:
    """draft에 실려 온 태그를 ExposureMetadata 맵으로 복원한다(merge/fallback 경로)."""

    metadata: dict[tuple[str, str], ExposureMetadata] = {}
    for draft in drafts:
        if draft.exposure_source is None:
            continue
        metadata[(draft.user_id, draft.video_id)] = ExposureMetadata(
            policy="model",
            rank=draft.exposure_rank if draft.exposure_rank is not None else 0,
            ctr_score=draft.exposure_ctr_score,
            is_exploration=draft.exposure_source == "random",
            policy_version=draft.policy_version,
            exposure_source=draft.exposure_source,
        )
    return metadata


def _expand_events(
    drafts: list[ImpressionDraft],
    clicked: set[int],
    request: EventGenerationRequest,
    *,
    metadata: Mapping[tuple[str, str], ExposureMetadata] | None = None,
    source: str = SOURCE_HISTORICAL,
    event_id_prefix: str = "evt",
    event_id_sequence_start: int = 0,
    slate_registry: SlateIdentityRegistry | None = None,
) -> list[EventLog]:
    """draft + 클릭 결정 → long EventLog 스트림.

    노출마다 impression 1행. 클릭 선정분엔 같은 세션 흐름으로 click/view(+like)를
    impression 직후(초 단위 단조 증가)에 배치한다. 일일 상한은 impression 기준.
    """
    if metadata is None:
        # draft에 실려 온 태그가 있으면 그것으로 조인한다(merge 경로 무변경 —
        # 외부 metadata 인자(정책 시뮬레이션 라운드)는 그대로 우선).
        embedded = _exposure_metadata_from_drafts(drafts)
        metadata = embedded or None

    end = request.history_end
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)

    by_user: dict[str, list[int]] = defaultdict(list)
    for idx, draft in enumerate(drafts):
        by_user[draft.user_id].append(idx)

    slate_id_by_user: dict[str, SlateId] = {}
    slate_context = request.slate_context
    if slate_context is not None:
        active_registry = (
            slate_registry if slate_registry is not None else SlateIdentityRegistry()
        )
        for user_id, indices in by_user.items():
            if len(indices) > request.max_events_per_user_per_day:
                raise ActionLogSlateContractError(
                    code="slate_capacity_exceeded",
                    partition_date=slate_context.partition_date,
                    member_count=len(indices),
                )
            identity = SlateIdentity(
                partition_date=slate_context.partition_date,
                user_id=user_id,
                members=tuple(
                    SlateMember(
                        video_id=drafts[index].video_id,
                        rank=drafts[index].exposure_rank,
                        exposure_source=drafts[index].exposure_source,
                        policy_version=drafts[index].policy_version,
                    )
                    for index in indices
                ),
                producer=slate_context.producer,
            )
            slate_id_by_user[user_id] = generate_slate_id(
                identity,
                registry=active_registry,
            )

    events: list[EventLog] = []
    seq = event_id_sequence_start

    def _emit(timestamp, user_id, event_type, video_id, watch=None):
        nonlocal seq
        meta = metadata.get((user_id, video_id)) if metadata else None
        events.append(
            EventLog(
                event_id=f"{event_id_prefix}_{timestamp.astimezone(_KST):%Y%m%d}_{seq:08d}",
                event_timestamp=timestamp,
                user_id=user_id,
                event_type=event_type,
                video_id=video_id,
                watch_time_sec=watch,
                rank=meta.rank if meta else None,
                source=source,
                policy=meta.policy if meta else None,
                ctr_score=meta.ctr_score if meta else None,
                is_exploration=meta.is_exploration if meta else None,
                policy_version=meta.policy_version if meta else None,
                exposure_source=meta.exposure_source if meta else None,
                slate_id=slate_id_by_user.get(user_id),
            )
        )
        seq += 1

    for user_id, indices in by_user.items():
        urng = random.Random(f"{request.seed}:ts:{user_id}")
        days = list(range(request.history_days))
        urng.shuffle(days)
        order = list(indices)
        urng.shuffle(order)
        cap = request.max_events_per_user_per_day
        for position, idx in enumerate(order):
            draft = drafts[idx]
            day = days[(position // cap) % len(days)]
            impression_ts = end - timedelta(
                days=day,
                # history_end에서 최소 _MIN_IMPRESSION_HOURS시간 이전 → 후속 click/view/like가
                # 세션 최대 span(_MAX_SESSION_SPAN_SEC)만큼 밀려도 history_end를 넘지 않는다.
                hours=urng.randint(_MIN_IMPRESSION_HOURS, 23),
                minutes=urng.randint(0, 59),
                seconds=urng.randint(0, 59),
            )
            _emit(impression_ts, user_id, "impression", draft.video_id)
            if idx not in clicked:
                continue
            click_ts = impression_ts + timedelta(seconds=urng.randint(1, _CLICK_DELAY_MAX_SEC))
            _emit(click_ts, user_id, "click", draft.video_id)
            watch = max(1, round(draft.watch_fraction * draft.duration_sec))
            view_ts = click_ts + timedelta(seconds=urng.randint(1, _VIEW_DELAY_MAX_SEC))
            _emit(view_ts, user_id, "view", draft.video_id, watch=watch)
            last_ts = view_ts
            if draft.would_like:
                like_ts = view_ts + timedelta(seconds=urng.randint(1, max(2, watch)))
                _emit(like_ts, user_id, "like", draft.video_id)
                last_ts = like_ts
            # window 불변식 가드: 세션 마지막 이벤트도 history_end를 넘지 않는다.
            # _MIN_IMPRESSION_HOURS가 _MAX_DURATION 기반이라 성립하며, 이 결합이 깨지면
            # (예: _MAX_DURATION을 여유보다 크게 올리면) 여기서 조기에 실패한다.
            assert last_ts <= end, (
                f"session event {last_ts} exceeded history_end {end} — "
                "check _MIN_IMPRESSION_HOURS vs _MAX_SESSION_SPAN_SEC"
            )
    if slate_context is not None and any(
        event.event_timestamp.astimezone(_KST).date()
        != slate_context.partition_date
        for event in events
    ):
        raise ActionLogSlateContractError(
            code="slate_partition_date_mismatch",
            partition_date=slate_context.partition_date,
        )
    return events


def _event_rows(batch: EventLogBatch, model_name: str) -> list[dict]:
    """EventLogBatch를 명시적 Parquet schema에 맞는 flat row로 변환한다."""

    rows = []
    for event in batch.events:
        rows.append(
            {
                "event_id": event.event_id,
                "event_timestamp": event.event_timestamp,
                "user_id": event.user_id,
                "event_type": event.event_type,
                "video_id": event.video_id,
                "watch_time_sec": event.watch_time_sec,
                "rank": event.rank,
                "source": event.source,
                "policy": event.policy,
                "ctr_score": event.ctr_score,
                "is_exploration": event.is_exploration,
                "policy_version": event.policy_version,
                "exposure_source": event.exposure_source,
                "slate_id": event.slate_id,
                "schema_version": batch.schema_version,
                "prompt_version": batch.prompt_version,
                "llm_model": model_name,
                "generated_at": batch.generated_at,
            }
        )
    return rows


def _event_spool_rows(
    events: list[EventLog],
    model_name: str,
) -> list[dict[str, object]]:
    """완료 시각을 제외한 event rows를 IPC spool schema에 맞춰 변환한다."""

    return [
        {
            "event_id": event.event_id,
            "event_timestamp": event.event_timestamp,
            "user_id": event.user_id,
            "event_type": event.event_type,
            "video_id": event.video_id,
            "watch_time_sec": event.watch_time_sec,
            "rank": event.rank,
            "source": event.source,
            "policy": event.policy,
            "ctr_score": event.ctr_score,
            "is_exploration": event.is_exploration,
            "policy_version": event.policy_version,
            "exposure_source": event.exposure_source,
            "slate_id": event.slate_id,
            "schema_version": ACTION_LOG_SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "llm_model": model_name,
        }
        for event in events
    ]


class _StreamingActionLogWriter:
    """IPC spool을 completion-time Parquet/JSONL 산출물로 최종화한다."""

    def __init__(
        self,
        *,
        request: EventGenerationRequest,
        model_name: str,
    ) -> None:
        self._request = request
        self._model_name = model_name
        self._warehouse_file: TextIO | None = None
        self._quarantine_file: TextIO | None = None
        self._event_sink: pa.OSFile | None = None
        self._event_stream: pa.ipc.RecordBatchStreamWriter | None = None
        self._exit_stack: ExitStack | None = None
        self._event_spool_path: Path | None = None
        self._parquet_spool_path: Path | None = None
        self._warehouse_spool_path: Path | None = None
        self._quarantine_spool_path: Path | None = None
        self._commit_backup_paths: list[Path] = []
        self._unrestored_backup_paths: list[tuple[Path, Path]] = []
        self._committed = False

    @staticmethod
    def _create_sibling_spool_path(target_path: str) -> Path:
        """최종 target과 같은 filesystem에 임시 spool 파일을 만든다."""

        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".spool",
            dir=str(target.parent),
        )
        os.close(descriptor)
        return Path(raw_path)

    def _spool_paths(self) -> tuple[Path, ...]:
        base_spool_paths = tuple(
            path
            for path in (
                self._event_spool_path,
                self._parquet_spool_path,
                self._warehouse_spool_path,
                self._quarantine_spool_path,
            )
            if path is not None
        )
        return (*base_spool_paths, *self._commit_backup_paths)

    def _write_best_effort_diagnostic(
        self,
        *,
        level: Literal["warning", "error"],
        message: str,
        extra: Mapping[str, str],
    ) -> None:
        """writer의 cleanup/rollback 진단이 복구 흐름을 중단시키지 않게 기록한다."""

        try:
            if level == "warning":
                logger.warning(message, extra=dict(extra), exc_info=True)
            else:
                logger.error(message, extra=dict(extra), exc_info=True)
        except Exception:  # noqa: BLE001 - diagnostic sink must not mask recovery
            pass

    def _remove_spools(self, *, best_effort: bool = False) -> None:
        remaining_backup_paths: list[Path] = []
        for spool_path in self._spool_paths():
            try:
                spool_path.unlink(missing_ok=True)
            except OSError:
                if not best_effort:
                    raise
                if spool_path in self._commit_backup_paths:
                    remaining_backup_paths.append(spool_path)
                    message = "Unable to remove committed action log backup spool"
                else:
                    message = "Unable to remove committed action log spool"
                self._write_best_effort_diagnostic(
                    level="warning",
                    message=message,
                    extra={"spool_path": str(spool_path)},
                )
        if best_effort:
            self._commit_backup_paths = remaining_backup_paths
        else:
            self._commit_backup_paths.clear()

    def _clear_open_resources(self) -> None:
        self._warehouse_file = None
        self._quarantine_file = None
        self._event_sink = None
        self._event_stream = None

    def _restore_unrestored_backups(self) -> None:
        """이전 rollback에서 복원하지 못한 backup을 best-effort로 다시 복원한다."""

        remaining_backups: list[tuple[Path, Path]] = []
        for backup_path, final_path in self._unrestored_backup_paths:
            try:
                if not backup_path.exists():
                    continue
                backup_path.replace(final_path)
            except OSError:
                remaining_backups.append((backup_path, final_path))
                self._write_best_effort_diagnostic(
                    level="error",
                    message="Unable to restore action log backup after failed publish",
                    extra={
                        "backup_spool_path": str(backup_path),
                        "final_path": str(final_path),
                    },
                )
        self._unrestored_backup_paths = remaining_backups

    def _close_generation_resources(self) -> None:
        if self._exit_stack is None:
            return
        stack, self._exit_stack = self._exit_stack, None
        try:
            stack.close()
        finally:
            self._clear_open_resources()

    def _ensure_open_and_uncommitted(self) -> None:
        if self._committed:
            raise RuntimeError("streaming action log writer is already finalized")
        if self._exit_stack is None:
            raise RuntimeError("streaming action log writer is not open")

    def __enter__(self) -> Self:
        stack = ExitStack()
        try:
            self._event_spool_path = self._create_sibling_spool_path(
                self._request.output_path
            )
            self._parquet_spool_path = self._create_sibling_spool_path(
                self._request.output_path
            )
            self._warehouse_spool_path = self._create_sibling_spool_path(
                self._request.warehouse_output_path
            )
            self._quarantine_spool_path = self._create_sibling_spool_path(
                self._request.quarantine_output_path
            )
            self._warehouse_file = stack.enter_context(self._warehouse_spool_path.open(
                "w",
                encoding="utf-8",
            ))
            self._quarantine_file = stack.enter_context(self._quarantine_spool_path.open(
                "w",
                encoding="utf-8",
            ))
            self._event_sink = pa.OSFile(str(self._event_spool_path), "wb")
            stack.callback(self._event_sink.close)
            self._event_stream = pa.ipc.new_stream(
                self._event_sink,
                _EVENT_SPOOL_SCHEMA,
            )
            stack.callback(self._event_stream.close)
        except BaseException:
            try:
                stack.close()
            finally:
                self._clear_open_resources()
                self._remove_spools()
            raise
        self._exit_stack = stack
        return self

    def write_events(self, events: list[EventLog]) -> None:
        if not events:
            return
        self._ensure_open_and_uncommitted()
        assert self._event_stream is not None
        assert self._warehouse_file is not None
        event_batch = pa.RecordBatch.from_pylist(
            _event_spool_rows(events, self._model_name),
            schema=_EVENT_SPOOL_SCHEMA,
        )
        self._event_stream.write_batch(event_batch)
        for event in events:
            self._warehouse_file.write(
                json.dumps(
                    event.to_warehouse_row(),
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )

    def write_quarantine(self, records: list[QuarantineRecord]) -> None:
        self._ensure_open_and_uncommitted()
        assert self._quarantine_file is not None
        for record in records:
            self._quarantine_file.write(
                json.dumps(
                    record.model_dump(),
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )

    def _write_final_parquet(
        self,
        generated_at: str,
        buffered_events_observer: Callable[[int], None] | None,
    ) -> None:
        assert self._event_spool_path is not None
        assert self._parquet_spool_path is not None
        with ExitStack() as stack:
            event_source = pa.OSFile(str(self._event_spool_path), "rb")
            stack.callback(event_source.close)
            event_reader = pa.ipc.open_stream(event_source)
            stack.callback(event_reader.close)
            parquet_writer = pq.ParquetWriter(
                str(self._parquet_spool_path),
                EVENT_LOG_PARQUET_SCHEMA,
            )
            stack.callback(parquet_writer.close)
            buffered_batches: list[pa.RecordBatch] = []
            buffered_rows = 0

            def flush_buffer() -> None:
                nonlocal buffered_rows
                if buffered_events_observer is not None:
                    buffered_events_observer(buffered_rows)
                table = pa.Table.from_batches(
                    buffered_batches,
                    schema=_EVENT_SPOOL_SCHEMA,
                )
                generated_column = pa.array(
                    [generated_at] * table.num_rows,
                    type=pa.string(),
                )
                table = table.append_column("generated_at", generated_column)
                table = table.select(EVENT_LOG_PARQUET_SCHEMA.names).cast(
                    EVENT_LOG_PARQUET_SCHEMA
                )
                parquet_writer.write_table(
                    table,
                    row_group_size=_PARQUET_TARGET_ROW_GROUP_ROWS,
                )
                buffered_batches.clear()
                buffered_rows = 0
                del generated_column
                del table
                if buffered_events_observer is not None:
                    buffered_events_observer(0)

            for event_batch in event_reader:
                start = 0
                while start < event_batch.num_rows:
                    rows_to_buffer = min(
                        _PARQUET_TARGET_ROW_GROUP_ROWS - buffered_rows,
                        event_batch.num_rows - start,
                    )
                    buffered_batches.append(event_batch.slice(start, rows_to_buffer))
                    buffered_rows += rows_to_buffer
                    start += rows_to_buffer
                    if buffered_rows == _PARQUET_TARGET_ROW_GROUP_ROWS:
                        flush_buffer()
            if buffered_batches:
                flush_buffer()

    def _commit_success_outputs(self) -> None:
        """세 final output을 함께 publish하고 중간 실패 시 기존 상태로 되돌린다."""

        assert self._parquet_spool_path is not None
        assert self._warehouse_spool_path is not None
        assert self._quarantine_spool_path is not None
        output_pairs = (
            (self._parquet_spool_path, Path(self._request.output_path)),
            (self._warehouse_spool_path, Path(self._request.warehouse_output_path)),
            (self._quarantine_spool_path, Path(self._request.quarantine_output_path)),
        )
        backups: list[tuple[Path, Path]] = []
        published_paths: list[Path] = []
        try:
            for _spool_path, final_path in output_pairs:
                if final_path.exists():
                    backup_path = self._create_sibling_spool_path(str(final_path))
                    self._commit_backup_paths.append(backup_path)
                    final_path.replace(backup_path)
                    backups.append((backup_path, final_path))
            for spool_path, final_path in output_pairs:
                spool_path.replace(final_path)
                published_paths.append(final_path)
        except BaseException as publish_error:
            rollback_errors: list[Exception] = []
            for published_path in reversed(published_paths):
                try:
                    published_path.unlink(missing_ok=True)
                except OSError as rollback_error:
                    rollback_errors.append(rollback_error)
                    self._write_best_effort_diagnostic(
                        level="error",
                        message="Unable to remove newly published action log output during rollback",
                        extra={"final_path": str(published_path)},
                    )
            for backup_path, final_path in reversed(backups):
                restore_pair = (backup_path, final_path)
                self._commit_backup_paths.remove(backup_path)
                self._unrestored_backup_paths.append(restore_pair)
                try:
                    if backup_path.exists():
                        backup_path.replace(final_path)
                except OSError as rollback_error:
                    rollback_errors.append(rollback_error)
                    self._write_best_effort_diagnostic(
                        level="error",
                        message="Unable to restore action log backup after failed publish",
                        extra={
                            "backup_spool_path": str(backup_path),
                            "final_path": str(final_path),
                        },
                    )
                else:
                    self._unrestored_backup_paths.remove(restore_pair)
            if rollback_errors:
                raise publish_error from ExceptionGroup(
                    "action log output rollback failed",
                    rollback_errors,
                )
            raise

    def finalize_success(
        self,
        generated_at: str,
        buffered_events_observer: Callable[[int], None] | None = None,
    ) -> None:
        """IPC spool을 completion-time Parquet와 최종 JSONL 산출물로 publish한다."""

        self._ensure_open_and_uncommitted()
        self._close_generation_resources()
        self._write_final_parquet(generated_at, buffered_events_observer)
        self._commit_success_outputs()
        self._committed = True
        self._remove_spools(best_effort=True)

    def finalize_quarantine_failure(self) -> None:
        """격리 비율 초과 시 quarantine JSONL만 최종 경로에 남긴다."""

        self._ensure_open_and_uncommitted()
        self._close_generation_resources()
        assert self._quarantine_spool_path is not None
        self._quarantine_spool_path.replace(self._request.quarantine_output_path)
        self._committed = True
        self._remove_spools(best_effort=True)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self._close_generation_resources()
        finally:
            if not self._committed:
                self._restore_unrestored_backups()
            self._remove_spools(best_effort=self._committed or exc is not None)


def _draft_rows(drafts: list[ImpressionDraft]) -> list[dict]:
    """ImpressionDraft 목록을 shard work parquet row로 변환한다."""

    return [draft.model_dump() for draft in drafts]


def write_action_log_draft_parquet(
    drafts: list[ImpressionDraft],
    output_path: str | Path,
    *,
    filesystem=None,
) -> None:
    """Shard work parquet으로 저장할 LLM judgment draft를 쓴다."""

    if filesystem is None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        _draft_rows(drafts),
        schema=ACTION_LOG_DRAFT_PARQUET_SCHEMA,
    )
    pq.write_table(table, output_path, filesystem=filesystem)


def read_action_log_draft_parquet(
    input_path: str | Path,
    *,
    filesystem=None,
) -> list[ImpressionDraft]:
    """Shard work parquet을 ImpressionDraft 목록으로 읽는다."""

    table = pq.read_table(input_path, filesystem=filesystem)
    return [ImpressionDraft.model_validate(row) for row in table.to_pylist()]


def write_action_log_checkpoint_part(
    work_id: str,
    work_order: int,
    drafts: list[ImpressionDraft],
    output_path: str | Path,
    *,
    filesystem=None,
) -> None:
    """성공한 work 하나를 immutable checkpoint parquet part로 쓴다."""

    if not drafts:
        raise ValueError("checkpoint part requires at least one draft")
    if filesystem is None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"work_id": work_id, "work_order": work_order, **draft.model_dump()}
        for draft in drafts
    ]
    table = pa.Table.from_pylist(rows, schema=ACTION_LOG_CHECKPOINT_PARQUET_SCHEMA)
    pq.write_table(table, output_path, filesystem=filesystem)


def read_action_log_checkpoint_part(
    input_path: str | Path,
    *,
    filesystem=None,
) -> ActionLogCheckpointPart:
    """checkpoint parquet part를 work identity와 draft 목록으로 읽는다."""

    rows = pq.read_table(input_path, filesystem=filesystem).to_pylist()
    if not rows:
        raise ValueError(f"empty checkpoint part: {input_path}")
    work_ids = {str(row["work_id"]) for row in rows}
    work_orders = {int(row["work_order"]) for row in rows}
    if len(work_ids) != 1 or len(work_orders) != 1:
        raise ValueError(f"mixed work identity in checkpoint part: {input_path}")
    drafts = [
        ImpressionDraft.model_validate(
            {key: value for key, value in row.items() if key not in {"work_id", "work_order"}}
        )
        for row in rows
    ]
    return ActionLogCheckpointPart(
        work_id=work_ids.pop(),
        work_order=work_orders.pop(),
        drafts=drafts,
    )


def write_event_log_parquet(
    batch: EventLogBatch,
    model_name: str,
    output_path: str | Path,
    *,
    filesystem=None,
) -> None:
    """EventLogBatch를 명시적 Arrow schema의 Parquet 파일로 저장한다."""

    if filesystem is None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(_event_rows(batch, model_name), schema=EVENT_LOG_PARQUET_SCHEMA)
    pq.write_table(table, output_path, filesystem=filesystem)


def write_event_log_warehouse_jsonl(batch: EventLogBatch, output_path: str | Path) -> None:
    """EventLogBatch를 Data Warehouse 적재용 JSONL row 파일로 저장한다."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for event in batch.events:
            file.write(json.dumps(event.to_warehouse_row(), ensure_ascii=False, default=str) + "\n")
    logger.info("Wrote warehouse event log", extra={"output_path": str(path), "total": len(batch.events)})


def write_quarantine_jsonl(records: list[QuarantineRecord], output_path: str | Path) -> None:
    """생성 실패로 격리된 유저를 후처리용 JSONL 파일로 저장한다."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record.model_dump(), ensure_ascii=False, default=str) + "\n")
    logger.info("Wrote quarantine output", extra={"output_path": str(path), "total": len(records)})


def _raise_if_quarantine_exceeds(
    quarantine: list[QuarantineRecord],
    total_work: int,
    request: EventGenerationRequest,
    user_count: int,
) -> None:
    """전량/대량 실패가 조용히 성공 처리되지 않도록 quarantine 비율을 검증한다."""

    if not total_work:
        return

    quarantine_ratio = len(quarantine) / total_work
    if quarantine_ratio <= request.max_quarantine_ratio:
        return

    write_quarantine_jsonl(quarantine, request.quarantine_output_path)
    _raise_if_quarantine_count_exceeds(
        len(quarantine),
        total_work,
        request,
        user_count,
    )


def _raise_if_quarantine_count_exceeds(
    quarantine_count: int,
    total_work: int,
    request: EventGenerationRequest,
    user_count: int,
) -> None:
    """quarantine 목록을 보관하지 않고 실패 비율을 검증한다."""

    if not total_work:
        return

    quarantine_ratio = quarantine_count / total_work
    if quarantine_ratio <= request.max_quarantine_ratio:
        return

    raise ActionLogGenerationError(
        f"quarantine ratio {quarantine_ratio:.2f} exceeds max_quarantine_ratio "
        f"{request.max_quarantine_ratio:.2f} "
        f"(quarantined={quarantine_count}, total_chunks={total_work}, "
        f"users={user_count})"
    )


def generate_action_log_drafts(
    request: EventGenerationRequest,
    virtual_users: list[dict],
    videos: list[dict],
    generator: ActionLogGenerator,
    progress_callback: ActionLogProgressCallback | None = None,
    *,
    enforce_quarantine_limit: bool = True,
    work_id_factory: ActionLogWorkIdFactory | None = None,
    completed_work: dict[str, list[ImpressionDraft]] | None = None,
    checkpoint_callback: ActionLogCheckpointCallback | None = None,
    shard_index: int | None = None,
    candidate_provider: CandidateProvider | None = None,
) -> ActionLogDraftGenerationResult:
    """유저 단위 LLM 판단을 실행하고 per-slate 클릭 선정 전 draft를 반환한다.

    단일 실행은 quarantine 비율을 즉시 검증한다. shard 실행은 성공 draft를
    보존하기 위해 이 검증을 merge 단계의 전역 합산 뒤로 미룰 수 있다.
    """

    logger.info(
        "Starting action log draft generation",
        extra={
            "users": len(virtual_users),
            "videos": len(videos),
            "click_threshold": request.click_threshold,
            "candidates_per_user": request.candidates_per_user,
            "seed": request.seed,
        },
    )

    drafts, quarantine, total_work = _generate_drafts_isolated(
        generator,
        virtual_users,
        videos,
        request,
        progress_callback,
        work_id_factory,
        completed_work,
        checkpoint_callback,
        shard_index,
        candidate_provider,
    )
    if enforce_quarantine_limit:
        _raise_if_quarantine_exceeds(quarantine, total_work, request, len(virtual_users))
    result = ActionLogDraftGenerationResult(
        drafts=drafts,
        quarantine=quarantine,
        total_work=total_work,
    )
    logger.info("Generated action log drafts", extra=result.summary)
    return result


def expand_action_log_drafts(
    request: EventGenerationRequest,
    drafts: list[ImpressionDraft],
    quarantine: list[QuarantineRecord] | None = None,
) -> EventGenerationResult:
    """전체 draft에 유저별 커트라인 클릭 선정과 long event 확장을 적용한다."""

    clicked = select_clicks_per_slate(drafts, request.click_threshold)
    slate_registry = (
        SlateIdentityRegistry() if request.slate_context is not None else None
    )
    events = _expand_events(
        drafts,
        clicked,
        request,
        slate_registry=slate_registry,
    )

    batch = EventLogBatch(
        schema_version=ACTION_LOG_SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        request=request,
        events=events,
    )
    result = EventGenerationResult(batch=batch, quarantine=quarantine or [])
    logger.info("Generated action log batch", extra=result.summary)
    return result


def generate_action_log_batch(
    request: EventGenerationRequest,
    virtual_users: list[dict],
    videos: list[dict],
    generator: ActionLogGenerator,
    progress_callback: ActionLogProgressCallback | None = None,
    *,
    candidate_provider: CandidateProvider | None = None,
    exposure_metadata: Mapping[tuple[str, str], ExposureMetadata] | None = None,
) -> EventGenerationResult:
    """유저 단위 격리 생성 → per-slate click_threshold 클릭 선정 → 조립 →
    파일 저장을 실행한다.

    exposure_metadata는 candidate_provider 호출이 진행되며 채워지는 공유 맵일 수
    있으므로(#221 ModelExposureRound), draft 생성이 끝난 뒤에 참조한다.
    """

    draft_result = generate_action_log_drafts(
        request,
        virtual_users,
        videos,
        generator,
        progress_callback,
        candidate_provider=candidate_provider,
    )
    drafts = draft_result.drafts
    if exposure_metadata is not None:
        drafts = attach_exposure_tags(drafts, exposure_metadata)
    result = expand_action_log_drafts(
        request,
        drafts,
        draft_result.quarantine,
    )

    output_path = Path(request.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_event_log_parquet(result.batch, generator.model_name, output_path)
    write_event_log_warehouse_jsonl(result.batch, request.warehouse_output_path)
    write_quarantine_jsonl(draft_result.quarantine, request.quarantine_output_path)
    logger.info(
        "Wrote action log outputs",
        extra={"output_path": str(output_path), **result.summary},
    )
    return result


def _single_result_from_legacy(
    result: EventGenerationResult,
) -> ActionLogSingleResult:
    """legacy batch 결과를 단일 실행 결과 계약으로 변환한다."""

    summary = result.summary
    return ActionLogSingleResult(
        execution_mode="legacy",
        total_events=int(summary["total_events"]),
        impressions=int(summary["impressions"]),
        clicks=int(summary["clicks"]),
        quarantined_users=int(summary["quarantined_users"]),
        api_error=int(summary["api_error"]),
        invalid_json=int(summary["invalid_json"]),
        schema_fail=int(summary["schema_fail"]),
    )


def generate_action_log_single(
    request: EventGenerationRequest,
    virtual_users: list[dict],
    videos: list[dict],
    generator: ActionLogGenerator,
    *,
    candidate_provider: CandidateProvider | None = None,
    exposure_metadata: (
        MutableMapping[tuple[str, str], ExposureMetadata] | None
    ) = None,
    _retention_observer: Callable[[_StreamingRetentionSnapshot], None] | None = None,
) -> ActionLogSingleResult:
    """active user 수를 제한해 action log를 사용자 순서대로 증분 기록한다.

    후보 provider는 coordinator에서 입력 순서대로 호출한다. 각 사용자의 모든
    chunk 결과가 모인 뒤에만 click을 한 번 선정하고, user-local event를 writer에
    기록한 즉시 참조를 해제한다. 기존 draft/shard 경로의 progress와 checkpoint
    계약은 이 단일 실행 경로에서 사용하지 않는다. 변경 가능한 exposure metadata는
    사용자가 drain될 때 해당 entry를 제거하므로 정상 streaming 종료 후 비어 있다.
    read-only mapping은 변경하지 않고 legacy batch 경로로 위임한다.
    `_retention_observer`는 회귀 검증용 private hook이며, 동일 snapshot은 DAG
    structured telemetry에도 기록된다.
    """

    if (
        any("user_id" not in virtual_user for virtual_user in virtual_users)
        or _has_duplicate_user_ids(virtual_users)
        or (
            exposure_metadata is not None
            and not isinstance(exposure_metadata, MutableMapping)
        )
    ):
        legacy = generate_action_log_batch(
            request,
            virtual_users,
            videos,
            generator,
            candidate_provider=candidate_provider,
            exposure_metadata=exposure_metadata,
        )
        return _single_result_from_legacy(legacy)

    mutable_exposure_metadata: (
        MutableMapping[tuple[str, str], ExposureMetadata] | None
    )
    if exposure_metadata is None:
        mutable_exposure_metadata = None
    else:
        assert isinstance(exposure_metadata, MutableMapping)
        mutable_exposure_metadata = exposure_metadata

    logger.info(
        "Starting action log draft generation",
        extra={
            "users": len(virtual_users),
            "videos": len(videos),
            "click_threshold": request.click_threshold,
            "candidates_per_user": request.candidates_per_user,
            "seed": request.seed,
        },
    )

    max_workers = max(1, request.max_concurrency)
    max_active_users = _STREAMING_ACTIVE_USER_MULTIPLIER * max_workers
    telemetry: ActionLogStreamingTelemetryReporter | None = None
    telemetry_enabled = True

    def _disable_telemetry() -> None:
        nonlocal telemetry_enabled
        if not telemetry_enabled:
            return
        telemetry_enabled = False
        try:
            logger.warning(
                "Action log streaming telemetry disabled after reporter failure"
            )
        except Exception:  # noqa: BLE001 - warning sink is also best effort
            pass

    try:
        telemetry = ActionLogStreamingTelemetryReporter(logger=logger)
    except Exception:  # noqa: BLE001 - operational telemetry is best effort
        _disable_telemetry()

    def _report_telemetry(
        operation: Callable[[ActionLogStreamingTelemetryReporter], None],
    ) -> None:
        if not telemetry_enabled or telemetry is None:
            return
        try:
            operation(telemetry)
        except Exception:  # noqa: BLE001 - operational telemetry is best effort
            _disable_telemetry()

    def _telemetry_detailed_candidate() -> bool:
        if not telemetry_enabled or telemetry is None:
            return False
        try:
            return telemetry.detailed_candidate
        except Exception:  # noqa: BLE001 - operational telemetry is best effort
            _disable_telemetry()
            return False

    active_users: deque[_StreamingUserState] = deque()
    user_iterator = iter(enumerate(virtual_users))
    total_users = len(virtual_users)
    provider_exhausted = False
    next_work_sequence = 0
    next_event_sequence = 0
    slate_registry = (
        SlateIdentityRegistry() if request.slate_context is not None else None
    )
    activated_users = 0
    submitted_work = 0
    completed_work = 0
    failed_work = 0
    total_events = 0
    impressions = 0
    clicks = 0
    quarantined_users = 0
    error_counts = {"api_error": 0, "invalid_json": 0, "schema_fail": 0}
    futures: dict[
        Future[_ActionLogCallResult],
        tuple[_StreamingUserState, int, int],
    ] = {}
    unsent_work: deque[tuple[_StreamingUserState, int, int]] = deque()

    def _observe(
        buffered_events: int = 0,
        *,
        phase: Literal["generating", "finalizing"] = "generating",
        start: bool = False,
        finish: bool = False,
    ) -> None:
        if provider_exhausted:
            total_work: int | None = next_work_sequence
            pending_work: int | None = total_work - completed_work - len(futures)
        else:
            total_work = None
            pending_work = None
        snapshot = _StreamingRetentionSnapshot(
            phase=phase,
            active_users=len(active_users),
            buffered_drafts=sum(
                len(drafts)
                for state in active_users
                for drafts in state.drafts_by_chunk.values()
            ),
            buffered_events=buffered_events,
            in_flight_work=len(futures),
            activated_users=activated_users,
            total_users=total_users,
            submitted_work=submitted_work,
            total_work=total_work,
            completed_work=completed_work,
            failed_work=failed_work,
            pending_work=pending_work,
        )
        if _retention_observer is not None:
            _retention_observer(snapshot)
        if start:
            _report_telemetry(lambda reporter: reporter.start(snapshot))
        elif finish:
            _report_telemetry(lambda reporter: reporter.finish(snapshot))
        else:
            _report_telemetry(lambda reporter: reporter.observe(snapshot))

    with _StreamingActionLogWriter(
        request=request,
        model_name=generator.model_name,
    ) as writer, ThreadPoolExecutor(max_workers=max_workers) as executor:

        def _activate_next_user() -> bool:
            nonlocal activated_users, next_work_sequence, provider_exhausted
            if activated_users == total_users:
                provider_exhausted = True
                return False
            try:
                user_sequence, virtual_user = next(user_iterator)
            except StopIteration:
                provider_exhausted = True
                return False

            activated_users += 1
            user_id = str(virtual_user.get("user_id", f"user_{user_sequence}"))
            user_rng = random.Random(f"{request.seed}:{user_id}")
            if candidate_provider is not None:
                candidates = candidate_provider(virtual_user, user_rng)
            else:
                candidates = build_candidates(
                    virtual_user,
                    videos,
                    request.candidates_per_user,
                    request.exploration_ratio,
                    user_rng,
                    personalized_ratio=request.personalized_ratio,
                    popular_ratio=request.popular_ratio,
                )
            if not candidates:
                if mutable_exposure_metadata is not None:
                    _consume_user_exposure_metadata(
                        mutable_exposure_metadata,
                        user_id,
                    ).clear()
                if activated_users == total_users:
                    provider_exhausted = True
                _observe()
                return True

            work: list[_ActionLogWorkItem] = []
            work_sequences: list[int] = []
            for chunk_index, chunk in enumerate(
                _chunked(candidates, request.chunk_size)
            ):
                work.append(
                    _ActionLogWorkItem(
                        work_id=f"work_{next_work_sequence:08d}",
                        user_id=user_id,
                        virtual_user=virtual_user,
                        candidates=chunk,
                    )
                )
                work_sequences.append(next_work_sequence)
                next_work_sequence += 1

            state = _StreamingUserState(
                user_sequence=user_sequence,
                user_id=user_id,
                virtual_user=virtual_user,
                work=work,
                drafts_by_chunk={},
                quarantine_by_chunk={},
                remaining_chunks=len(work),
            )
            active_users.append(state)
            for chunk_index, (item, work_sequence) in enumerate(
                zip(work, work_sequences, strict=True)
            ):
                unsent_work.append((state, chunk_index, work_sequence))
            if activated_users == total_users:
                provider_exhausted = True
            _observe()
            return True

        def _fill_active_users() -> None:
            while len(active_users) < max_active_users and not provider_exhausted:
                if not _activate_next_user():
                    return

        def _submit_available_work() -> None:
            nonlocal submitted_work
            while unsent_work and len(futures) < max_workers:
                state, chunk_index, work_sequence = unsent_work.popleft()
                submitted_at = monotonic()
                submitted_work += 1
                _report_telemetry(
                    lambda reporter: reporter.note_submission(submitted_work)
                )
                futures[
                    executor.submit(
                        _generate_action_log_work,
                        generator,
                        state.work[chunk_index],
                        work_sequence=work_sequence,
                        submitted_at=submitted_at,
                        shard_index=None,
                        detailed_telemetry=_telemetry_detailed_candidate(),
                    )
                ] = (state, chunk_index, work_sequence)
                _observe()

        def _store_work_result(
            state: _StreamingUserState,
            chunk_index: int,
            call_result: _ActionLogCallResult,
        ) -> None:
            nonlocal completed_work, failed_work
            if call_result.drafts is not None:
                state.drafts_by_chunk[chunk_index] = call_result.drafts
            else:
                assert call_result.error_type is not None
                assert call_result.error is not None
                failed_work += 1
                state.quarantine_by_chunk[chunk_index] = QuarantineRecord(
                    user_id=state.user_id,
                    virtual_user=state.virtual_user,
                    raw_llm_response=call_result.raw_text,
                    error_type=call_result.error_type,
                    error_message=str(call_result.error),
                )
            state.remaining_chunks -= 1
            completed_work += 1
            _report_telemetry(
                lambda reporter: reporter.record_work(
                    work_sequence=call_result.work_sequence,
                    queue_wait_ms=max(
                        0.0,
                        (call_result.started_at - call_result.submitted_at) * 1000,
                    ),
                    request_elapsed_ms=call_result.request_elapsed_ms,
                    parse_elapsed_ms=call_result.parse_elapsed_ms,
                    total_elapsed_ms=max(
                        0.0,
                        (monotonic() - call_result.submitted_at) * 1000,
                    ),
                )
            )
            _observe()

        def _collect_completed_futures() -> None:
            completed, _pending = wait(futures, return_when=FIRST_COMPLETED)
            for completed_future in sorted(
                completed,
                key=lambda item: futures[item][2],
            ):
                state, chunk_index, _work_sequence = futures.pop(completed_future)
                _store_work_result(state, chunk_index, completed_future.result())

        _observe(start=True)
        _fill_active_users()
        _submit_available_work()
        while active_users or futures:
            if futures:
                _collect_completed_futures()
                _submit_available_work()

            while active_users and active_users[0].remaining_chunks == 0:
                state = active_users[0]
                drafts: list[ImpressionDraft] = []
                quarantine: list[QuarantineRecord] = []
                for chunk_index in range(len(state.work)):
                    if chunk_index in state.quarantine_by_chunk:
                        quarantine.append(state.quarantine_by_chunk[chunk_index])
                    else:
                        drafts.extend(state.drafts_by_chunk[chunk_index])
                if mutable_exposure_metadata is not None:
                    user_exposure_metadata = _consume_user_exposure_metadata(
                        mutable_exposure_metadata,
                        state.user_id,
                    )
                    drafts = attach_exposure_tags(drafts, user_exposure_metadata)
                    user_exposure_metadata.clear()

                clicked_drafts = select_clicks_per_slate(
                    drafts,
                    request.click_threshold,
                )
                events = _expand_events(
                    drafts,
                    clicked_drafts,
                    request,
                    event_id_sequence_start=next_event_sequence,
                    slate_registry=slate_registry,
                )
                event_count = len(events)
                total_events += event_count
                impressions += sum(
                    event.event_type == "impression" for event in events
                )
                clicks += sum(event.event_type == "click" for event in events)
                quarantined_users += len(quarantine)
                for record in quarantine:
                    error_counts[record.error_type] += 1

                _observe(buffered_events=event_count)
                writer.write_events(events)
                next_event_sequence += event_count
                events.clear()
                _observe()
                writer.write_quarantine(quarantine)

                drafts.clear()
                quarantine.clear()
                state.work.clear()
                state.drafts_by_chunk.clear()
                state.quarantine_by_chunk.clear()
                active_users.popleft()
                _observe()
                _fill_active_users()
                _submit_available_work()

        try:
            _raise_if_quarantine_count_exceeds(
                quarantined_users,
                next_work_sequence,
                request,
                len(virtual_users),
            )
        except ActionLogGenerationError:
            _observe(phase="finalizing")
            writer.finalize_quarantine_failure()
            _observe(phase="finalizing", finish=True)
            raise
        generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        _observe(phase="finalizing")
        writer.finalize_success(
            generated_at,
            buffered_events_observer=lambda buffered_events: _observe(
                buffered_events,
                phase="finalizing",
            ),
        )
        _observe(phase="finalizing", finish=True)

    result = ActionLogSingleResult(
        execution_mode="streaming",
        total_events=total_events,
        impressions=impressions,
        clicks=clicks,
        quarantined_users=quarantined_users,
        api_error=error_counts["api_error"],
        invalid_json=error_counts["invalid_json"],
        schema_fail=error_counts["schema_fail"],
    )
    logger.info("Wrote streaming action log outputs", extra=result.summary)
    return result
