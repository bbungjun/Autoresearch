"""다양한 합성 행동 이력의 날짜순 생성 구간.

[파이프라인] raw 사용자·영상 입력에서 학습 전 행동 이력을 생성한다.
[기능] 버전·seed 고정 활동/관심 상태로 draft를 만들고 production event 확장과
writer를 재사용한다. 신규 경로에만 게시하며 날짜별 입력/출력 hash를 기록한다.
[비책임] 품질 감사는 behavior_data_audit, snapshot 봉인·학습·평가는 후속 실험이 맡는다.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pyarrow as pa

from autoresearch.action_log_generation import pipeline as event_pipeline, schema as event_schema
from autoresearch.action_log_generation.pipeline import (
    derive_would_like, expand_action_log_drafts, write_event_log_parquet,
)
from autoresearch.action_log_generation.schema import (
    EventGenerationRequest, ImpressionDraft, SlateGenerationContext,
)
from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes, _write_table
from autoresearch.research_harness.fixture_inputs import (
    FIXTURE_VIRTUAL_USER_SCHEMA_V1, FIXTURE_YOUTUBE_SCHEMA_V1,
    _fixture_video_rows, _virtual_user_rows, select_fixture_user_ids,
)
from autoresearch.research_harness.fixture_models import require_fixture_date_window
from autoresearch.research_harness import fixture_inputs


VERSION = "diverse-behavior-v1"
CATEGORIES = ("Gaming", "Music", "Education", "Entertainment")
KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class BehaviorDataRequest:
    """30일 warm-up, 학습일, 귀속 확인일의 고정 32일 생성 요청."""

    seed: int
    training_date: date = date(2026, 9, 2)

    def __post_init__(self) -> None:
        if type(self.seed) is not int or self.seed < 0 or type(self.training_date) is not date:
            raise ValueError("invalid_behavior_data_request")
        # E=training+2의 기존 32일 fixture와 동일한 날짜 안전 범위.
        if self.training_date > date.max - timedelta(days=2):
            raise ValueError("invalid_behavior_date_window")
        require_fixture_date_window(self.training_date + timedelta(days=2), history_days=32)

    @property
    def start_date(self) -> date:
        return self.training_date - timedelta(days=30)

    @property
    def dates(self) -> tuple[date, ...]:
        return tuple(self.start_date + timedelta(days=i) for i in range(32))


def draw(seed: int, *keys: object) -> float:
    """호출 순서와 무관한 고정 hash 균등값 [0,1)을 반환한다."""
    payload = canonical_json_bytes([VERSION, seed, *[str(key) for key in keys]])
    return (int.from_bytes(sha256(payload).digest()[:8], "big") >> 11) / 2**53


def user_profile(seed: int, user_id: str) -> dict[str, object]:
    """미래 파일을 읽지 않는 고정 잠재 속성. Candidate metadata에는 공개하지 않는다."""
    primary = int(draw(seed, user_id, "primary") * 4)
    return {
        "user_id": user_id,
        "activity_probability": (0.25, 0.5, 0.8)[int(draw(seed, user_id, "activity") * 3)],
        "click_probability": 0.25 + 0.6 * draw(seed, user_id, "engagement"),
        "primary": CATEGORIES[primary],
        "next_primary": CATEGORIES[(primary + 1) % 4],
        "changes_interest": draw(seed, user_id, "changes") < 0.5,
        "change_offset": 14 + int(draw(seed, user_id, "change_day") * 8),
    }


def daily_drafts(request: BehaviorDataRequest, day: date, users: list[str],
                 videos: list[dict[str, object]]) -> list[ImpressionDraft]:
    """날짜와 사용자 상태로 노출·반응을 생성하며 과거/미래 파일을 읽지 않는다."""
    if day not in request.dates:
        raise ValueError("day_outside_behavior_window")
    drafts: list[ImpressionDraft] = []
    for user in sorted(users):
        profile = user_profile(request.seed, user)
        if draw(request.seed, user, day, "active") >= profile["activity_probability"]:
            continue
        count = (8, 16, 24)[int(draw(request.seed, user, day, "exposures") * 3)]
        candidates = sorted(videos, key=lambda v: (draw(request.seed, user, day, v["video_id"], "expose"), v["video_id"]))[:count]
        changed = profile["changes_interest"] and (day - request.start_date).days >= profile["change_offset"]
        primary = profile["next_primary"] if changed else profile["primary"]
        intends_click = draw(request.seed, user, day, "click") < profile["click_probability"]
        for video in candidates:
            affinity = float(video["video_category"] == primary)
            noise = draw(request.seed, user, day, video["video_id"], "utility")
            # 클릭 여부와 후보 내 카테고리/독립 noise 기반 선택을 분리한다.
            utility = 0.6 * affinity + 0.4 * noise
            propensity = (0.55 + 0.44 * utility) if intends_click else (0.05 + 0.4 * utility)
            watch = min(0.99, 0.1 + 0.25 * affinity + 0.64 * draw(request.seed, user, day, video["video_id"], "watch"))
            drafts.append(ImpressionDraft(
                user_id=user, video_id=str(video["video_id"]), click_propensity=propensity,
                watch_fraction=watch, would_like=derive_would_like(propensity, watch),
                duration_sec=int(str(video["video_duration"])[2:-1]) * 60,
            ))
    return drafts


def _receipt(root: Path, path: Path, rows: int) -> dict[str, object]:
    return {"path": path.relative_to(root).as_posix(), "rows": rows,
            "sha256": sha256(path.read_bytes()).hexdigest()}


def generate_behavior_data(root: Path, request: BehaviorDataRequest) -> dict[str, object]:
    """새 디렉터리에 32일 raw 이력과 manifest를 생성한다. 재사용/덮어쓰기는 거절한다.

    실패 시 부분 산출물은 진단용으로 보존하고 manifest가 없으므로 미완료로 취급한다.
    같은 seed의 새 경로 재생성은 입력 재현성 확인이며 평가/final 소비와 무관하다.
    """
    root.mkdir(parents=True, exist_ok=False)
    validation, final = select_fixture_user_ids(request.seed)
    users = sorted(validation + final)
    raw_users = _virtual_user_rows(tuple(users), request.training_date + timedelta(days=2), history_days=32)
    for user in raw_users:
        primary = user_profile(request.seed, str(user["user_id"]))["primary"]
        for field in ("hobby_keywords", "interest_keywords", "primary_categories"):
            user[field] = [primary]
        user["persona_summary"] = f"{primary} 영상을 선호하는 합성 사용자"
        user["generated_at"] = datetime.combine(request.start_date, datetime.min.time(), tzinfo=KST).astimezone(UTC).isoformat()
    user_path = root / "inputs" / "virtual_users.parquet"
    user_path.parent.mkdir(parents=True)
    _write_table(pa.Table.from_pylist(raw_users, schema=FIXTURE_VIRTUAL_USER_SCHEMA_V1), user_path)
    partitions = []
    for day in request.dates:
        start = datetime.combine(day, datetime.min.time(), tzinfo=KST).astimezone(UTC)
        videos = _fixture_video_rows(day)
        for video in videos:
            video["collected_at"] = start
            video["video_trending_date"] = start
        video_path = root / "inputs" / "youtube_trending_kr" / f"dt={day}" / "part-0.parquet"
        video_path.parent.mkdir(parents=True)
        _write_table(pa.Table.from_pylist(videos, schema=FIXTURE_YOUTUBE_SCHEMA_V1), video_path)
        drafts = daily_drafts(request, day, users, videos)
        generation_request = EventGenerationRequest(
            click_threshold=0.5, history_days=1, history_end=start + timedelta(days=1),
            slate_context=SlateGenerationContext(partition_date=day),
            max_events_per_user_per_day=24, seed=request.seed + day.toordinal(),
        )
        result = expand_action_log_drafts(generation_request, drafts, completion_timestamp=start + timedelta(days=1))
        # 서로 다른 world에서도 ID가 겹치지 않도록 기존 날짜/seq를 보존해 prefix만 구분한다.
        for event in result.batch.events:
            event.event_id = f"db1s{request.seed}_{event.event_id.split('_', 1)[1]}"
        output_path = root / "action_log" / f"dt={day}" / "part-0.parquet"
        write_event_log_parquet(result.batch, VERSION, output_path)
        partitions.append({"date": day.isoformat(),
                           "videos": _receipt(root, video_path, len(videos)),
                           "events": _receipt(root, output_path, len(result.batch.events))})
    # 모든 입력 완료 후에만 manifest를 쓴다. 잠재 상태는 생성기 감사 전용이다.
    manifest = {
        "version": VERSION, "seed": request.seed, "training_date": request.training_date.isoformat(),
        "start_date": request.start_date.isoformat(), "end_date": request.dates[-1].isoformat(),
        "pyarrow_version": pa.__version__, "generator_source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "dependency_sources": {module.__name__: sha256(Path(module.__file__).read_bytes()).hexdigest()
                               for module in (event_pipeline, event_schema, fixture_inputs)},
        "users": _receipt(root, user_path, len(users)), "partitions": partitions,
        "validation_users": list(validation), "reserved_final_users": list(final),
        "latent_profiles": [user_profile(request.seed, user) for user in users],
        "final_evaluations": 0,
    }
    (root / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return manifest
