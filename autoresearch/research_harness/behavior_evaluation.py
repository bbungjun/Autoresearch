"""고정 행동 규칙에서 Judge 소유 신규 평가 snapshot을 준비한다.

[파이프라인] 합성 평가 raw 생성과 학습 모델의 평가 사이에서 원본을 봉인한다.
[기능] 고정 anchor의 날짜순 행동 생성, hash 검증 source, Stage B snapshot 게시와
허용 metadata 준비를 제공한다. 기존 candidate publisher의 정답/시간 경계를 유지한다.
[비책임] 학습은 behavior_execution, 채점은 Judge, final 권한은 consumption_registry가
담당한다. 이 모듈은 모델 fit, 채점, final claim 또는 서빙을 수행하지 않는다.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.action_log_generation import pipeline as event_pipeline, schema as event_schema
from autoresearch.action_log_generation.pipeline import expand_action_log_drafts, write_event_log_parquet
from autoresearch.action_log_generation.schema import EventGenerationRequest, SlateGenerationContext
from autoresearch.research_harness import behavior_data, fixture_inputs
from autoresearch.research_harness.behavior_data import BehaviorDataRequest, KST, VERSION, daily_drafts, user_profile
from autoresearch.research_harness.candidate_data_view import (
    _eligible_metadata, _load_history_payloads, _metadata_artifact, _metadata_requests,
    _read_verified_local, _validate_metadata_artifact,
)
from autoresearch.research_harness.candidate_metadata import normalize_user_metadata, normalize_video_metadata
from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes, _write_table
from autoresearch.research_harness.evaluation_snapshot_models import EvaluationSnapshotRequest
from autoresearch.research_harness.evaluation_split import SPLIT_CONTRACT, user_bucket
from autoresearch.research_harness.fixture_inputs import (
    FIXTURE_VIRTUAL_USER_SCHEMA_V1, FIXTURE_YOUTUBE_SCHEMA_V1, _fixture_video_rows, _virtual_user_rows,
)
from autoresearch.research_harness.fixture_models import JudgeSnapshotHandoff, PreparedCandidateMetadata
from autoresearch.research_harness.local_evaluation_fixture import _validated_judge_snapshot
from autoresearch.research_harness.local_training import _read_regular, _read_table
from autoresearch.research_harness.slate import _build_evaluation_snapshot


EVALUATION_DATE = date(2026, 9, 4)
COHORT_SEEDS = (10901, 10902, 10903)
CONTRACT_HASH = "a1490bca5ebbe8114f6a3619dca6f3684b9eac4cecbcb18eb95af6abd0f624aa"


@dataclass(frozen=True)
class BehaviorEvaluationRequest(BehaviorDataRequest):
    """기존 8/3 anchor와 행동 규칙을 유지하며 평가/scan tail까지 확장한다."""

    validation_users: int = 800
    final_users: int = 200

    def __post_init__(self) -> None:
        super().__post_init__()
        if (self.training_date != date(2026, 9, 2)
                or any(type(n) is not int or n <= 0 for n in (self.validation_users, self.final_users))):
            raise ValueError("invalid_behavior_evaluation_request")

    @property
    def dates(self) -> tuple[date, ...]:
        return tuple(self.start_date + timedelta(days=i) for i in range(34))


def select_evaluation_users(request: BehaviorEvaluationRequest) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """기존 ID 후보식/hash bucket의 목표 수만 변경한다."""
    validation: list[str] = []
    final: list[str] = []
    index = 0
    while len(validation) < request.validation_users or len(final) < request.final_users:
        digest = sha256(f"youtube-ctr-input-v1:{request.seed}:{index}".encode()).hexdigest()
        user = f"fixture-user-{digest[:20]}"
        bucket = user_bucket(user)
        if bucket in SPLIT_CONTRACT.validation_buckets and len(validation) < request.validation_users:
            validation.append(user)
        elif bucket in SPLIT_CONTRACT.final_holdout_buckets and len(final) < request.final_users:
            final.append(user)
        index += 1
    return tuple(validation), tuple(final)


def evaluation_policy(requests: tuple[BehaviorEvaluationRequest, ...] | None = None) -> dict[str, object]:
    """raw 생성 전에 기록할 고정 정책과 실행 소스 hash를 반환한다."""
    modules = (behavior_data, fixture_inputs, event_pipeline, event_schema)
    requests = requests if requests is not None else tuple(BehaviorEvaluationRequest(s) for s in COHORT_SEEDS)
    return {
        "contract_version": "behavior-evaluation-policy-v1", "behavior_version": VERSION,
        "pyarrow_version": pa.__version__,
        "requests": [{"seed": r.seed, "validation_users": r.validation_users, "final_users": r.final_users}
                     for r in requests],
        "anchor_date": "2026-08-03", "evaluation_date": str(EVALUATION_DATE), "scan_tail": "2026-09-05",
        "comparison_contract_sha256": CONTRACT_HASH,
        "sources": {**{m.__name__: sha256(_read_regular(Path(m.__file__))).hexdigest() for m in modules},
                    __name__: sha256(_read_regular(Path(__file__))).hexdigest()},
    }


def _receipt(root: Path, path: Path, rows: int) -> dict[str, object]:
    return {"path": path.relative_to(root).as_posix(), "rows": rows,
            "sha256": sha256(_read_regular(path)).hexdigest()}


def generate_behavior_evaluation(
    root: Path, request: BehaviorEvaluationRequest, *, policy_path: Path, expected_policy_sha256: str,
) -> dict[str, object]:
    """사전 hash와 코드 일치를 검사하고 새 root에만 34일 원본을 생성한다."""
    policy = _read_regular(policy_path)
    parsed = json.loads(policy)
    requests = tuple(BehaviorEvaluationRequest(**r) for r in parsed["requests"])
    if (sha256(policy).hexdigest() != expected_policy_sha256 or parsed != evaluation_policy(requests)
            or request not in requests):
        raise ValueError("evaluation_policy_mismatch")
    root.mkdir(parents=True, exist_ok=False)
    validation, final = select_evaluation_users(request)
    users = sorted(validation + final)
    raw_users = _virtual_user_rows(tuple(users), EVALUATION_DATE, history_days=32)
    for user in raw_users:
        primary = user_profile(request.seed, str(user["user_id"]))["primary"]
        for field in ("hobby_keywords", "interest_keywords", "primary_categories"):
            user[field] = [primary]
        user["persona_summary"] = f"{primary} 영상을 선호하는 합성 사용자"
        user["generated_at"] = datetime.combine(request.start_date, datetime.min.time(), tzinfo=KST).astimezone(UTC).isoformat()
    user_path = root / "inputs/virtual_users.parquet"
    user_path.parent.mkdir(parents=True)
    _write_table(pa.Table.from_pylist(raw_users, schema=FIXTURE_VIRTUAL_USER_SCHEMA_V1), user_path)
    partitions = []
    for day in request.dates:
        start = datetime.combine(day, datetime.min.time(), tzinfo=KST).astimezone(UTC)
        videos = _fixture_video_rows(day)
        for video in videos:
            video["collected_at"] = video["video_trending_date"] = start
        video_path = root / "inputs/youtube_trending_kr" / f"dt={day}/part-0.parquet"
        video_path.parent.mkdir(parents=True)
        _write_table(pa.Table.from_pylist(videos, schema=FIXTURE_YOUTUBE_SCHEMA_V1), video_path)
        drafts = daily_drafts(request, day, users, videos)
        result = expand_action_log_drafts(EventGenerationRequest(
            click_threshold=0.5, history_days=1, history_end=start + timedelta(days=1),
            slate_context=SlateGenerationContext(partition_date=day), max_events_per_user_per_day=24,
            seed=request.seed + day.toordinal(),
        ), drafts, completion_timestamp=start + timedelta(days=1))
        output = root / "action_log" / f"dt={day}/part-0.parquet"
        write_event_log_parquet(result.batch, VERSION, output)
        partitions.append({"date": str(day), "videos": _receipt(root, video_path, len(videos)),
                           "events": _receipt(root, output, len(result.batch.events))})
    manifest = {
        "version": "behavior-evaluation-raw-v1", "behavior_version": VERSION,
        "seed": request.seed, "anchor_date": str(request.start_date), "evaluation_date": str(EVALUATION_DATE),
        "end_date": str(request.dates[-1]), "policy_sha256": expected_policy_sha256,
        "validation_users": list(validation), "reserved_final_users": list(final),
        "users": _receipt(root, user_path, len(users)), "partitions": partitions,
        "latent_profiles": [user_profile(request.seed, user) for user in users],
    }
    (root / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return manifest


class BehaviorEvaluationSource:
    """Judge 전용 원본의 hash를 검증하고 snapshot에 물리 경로를 숨기는 source."""

    def __init__(self, root: Path, *, expected_manifest_sha256: str) -> None:
        self.root = root.absolute()
        payload = _read_regular(self.root / "manifest.json")
        if sha256(payload).hexdigest() != expected_manifest_sha256:
            raise ValueError("evaluation_raw_manifest_mismatch")
        self.digest = expected_manifest_sha256
        self.manifest = json.loads(payload)
        manifest = self.manifest
        request = BehaviorEvaluationRequest(manifest["seed"], validation_users=len(manifest["validation_users"]),
                                            final_users=len(manifest["reserved_final_users"]))
        validation, final = select_evaluation_users(request)
        if (manifest["version"] != "behavior-evaluation-raw-v1" or manifest["behavior_version"] != VERSION
                or manifest["anchor_date"] != str(request.start_date)
                or manifest["evaluation_date"] != str(EVALUATION_DATE) or manifest["end_date"] != str(request.dates[-1])
                or manifest["validation_users"] != list(validation) or manifest["reserved_final_users"] != list(final)
                or [p["date"] for p in manifest["partitions"]] != [str(d) for d in request.dates]):
            raise ValueError("evaluation_raw_contract_invalid")
        self.partitions = {date.fromisoformat(p["date"]): p for p in manifest["partitions"]}

    @property
    def opaque_root(self) -> str:
        return f"behavior://{self.digest}/action-log"

    def partition_uri(self, dt: date) -> str:
        return f"{self.opaque_root}/dt={dt}/part-0.parquet"

    def _physical_source_root(self) -> Path:
        return self.root / "action_log"

    def _physical_partition_path(self, dt: date) -> Path:
        return self.root / "action_log" / f"dt={dt}/part-0.parquet"

    def open_partition(self, dt: date) -> pa.BufferReader:
        receipt = self.partitions[dt]["events"]
        expected = f"action_log/dt={dt}/part-0.parquet"
        if receipt["path"] != expected:
            raise ValueError("evaluation_partition_path_invalid")
        payload = _read_regular(self.root / expected)
        if sha256(payload).hexdigest() != receipt["sha256"]:
            raise ValueError("evaluation_partition_hash_mismatch")
        if pq.ParquetFile(pa.BufferReader(payload)).metadata.num_rows != receipt["rows"]:
            raise ValueError("evaluation_partition_rows_mismatch")
        return pa.BufferReader(payload)

    def read_metadata(self, receipt: dict, expected_path: str) -> pa.Table:
        if receipt["path"] != expected_path:
            raise ValueError("evaluation_metadata_path_invalid")
        return _read_table(self.root, expected_path, receipt["sha256"], receipt["rows"])


def seal_behavior_evaluation(source: BehaviorEvaluationSource, judge_root: Path) -> JudgeSnapshotHandoff:
    """기존 Stage B 날짜·귀속·bucket·write-once publisher로 평가를 봉인한다."""
    if judge_root.resolve().is_relative_to(source.root.resolve()) or source.root.resolve().is_relative_to(judge_root.resolve()):
        raise ValueError("evaluation_source_output_overlap")
    receipt = _build_evaluation_snapshot(EvaluationSnapshotRequest(
        action_log_root=source.opaque_root, history_start_date=date(2026, 8, 3),
        evaluation_start_date=EVALUATION_DATE, evaluation_end_date=EVALUATION_DATE,
        slate_id_cutover_date=date(2026, 8, 3), output_root=judge_root,
    ), source=source, created_at=datetime(2026, 9, 4, tzinfo=UTC))
    handoff, _ = _validated_judge_snapshot(receipt.target_path, expected_fingerprint=str(receipt.snapshot_fingerprint))
    return handoff


def prepare_behavior_metadata(
    source: BehaviorEvaluationSource, judge: JudgeSnapshotHandoff, *, final: bool = False,
) -> PreparedCandidateMetadata:
    """동일 snapshot의 허용 as-of metadata만 Judge 측에서 준비한다."""
    handoff, snapshot = _validated_judge_snapshot(judge.snapshot_root, expected_fingerprint=str(judge.snapshot_fingerprint))
    if handoff != judge or snapshot.source.root != source.opaque_root:
        raise ValueError("evaluation_source_snapshot_mismatch")
    for p in snapshot.source.partitions:
        receipt = source.partitions[p.dt]["events"]
        if (p.uri, p.rows, p.sha256) != (source.partition_uri(p.dt), receipt["rows"], receipt["sha256"]):
            raise ValueError("evaluation_snapshot_receipt_mismatch")
    history = _load_history_payloads(source, snapshot.window.candidate_history_partitions)
    split = snapshot.final_holdout if final else snapshot.validation
    requests = _metadata_requests(_read_verified_local(judge.snapshot_root / split.artifacts.slate.relative_path,
                                                      split.artifacts.slate), history)
    users = normalize_user_metadata(source.read_metadata(source.manifest["users"], "inputs/virtual_users.parquet"))
    videos = pa.concat_tables([
        normalize_video_metadata(source.read_metadata(p["videos"], f"inputs/youtube_trending_kr/dt={day}/part-0.parquet"))
        for day, p in source.partitions.items()
    ]).sort_by([("video_id", "ascending"), ("available_at", "ascending")])
    result = PreparedCandidateMetadata(
        snapshot_fingerprint=judge.snapshot_fingerprint, evaluation_id=split.evaluation_id,
        users=_metadata_artifact(_eligible_metadata(users, requests, "user_id"), "metadata/users.parquet"),
        videos=_metadata_artifact(_eligible_metadata(videos, requests, "video_id"), "metadata/videos.parquet"),
    )
    for key, artifact in (("user_id", result.users), ("video_id", result.videos)):
        _validate_metadata_artifact(artifact, requests, key)
    return result
