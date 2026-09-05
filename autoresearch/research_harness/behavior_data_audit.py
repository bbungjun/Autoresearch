"""생성된 행동 이력의 데이터 품질 감사 구간.

[파이프라인] 합성 raw 생성 뒤, 학습 데이터 채택 전 검사한다.
[기능] 파일 receipt·날짜·event/slate 계약, 실제 최근 피처 분산과 관심 이동을 검증한다.
[비책임] 상수 임베딩은 시간/행동 피처 감사에만 쓰며 학습·ranking 평가·final 소비를 하지 않는다.
"""

from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean, pstdev
from typing import Literal

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from autoresearch.action_log_generation.pipeline import EVENT_LOG_PARQUET_SCHEMA
from autoresearch.action_log_generation.schema import EventLog
from autoresearch.research_harness.behavior_data import BehaviorDataRequest, KST, VERSION, user_profile
from autoresearch.research_harness.candidate_metadata import normalize_user_metadata, normalize_video_metadata
from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes
from autoresearch.research_harness.fixture_inputs import FIXTURE_VIRTUAL_USER_SCHEMA_V1, FIXTURE_YOUTUBE_SCHEMA_V1, select_fixture_user_ids
from autoresearch.research_harness.local_features import build_local_features


RECENT = ("recent_click_count_7d", "recent_view_count_7d", "recent_watch_time_7d",
          "recent_like_count_7d", "total_event_count_7d")


class AuditEmbedding:
    """시간/행동 피처 검사만을 위한 상수. 생성 데이터나 모델 파일에는 저장하지 않는다."""

    def encode(self, texts: Sequence[str], *, role: Literal["query", "document"]) -> np.ndarray:
        return np.ones((len(texts), 2), dtype=np.float64)


def _read(root: Path, receipt: dict, expected: str, schema: pa.Schema) -> pa.Table:
    if receipt["path"] != expected:
        raise ValueError("unexpected_partition_path")
    path = root / expected
    if sha256(path.read_bytes()).hexdigest() != receipt["sha256"]:
        raise ValueError("partition_hash_mismatch")
    table = pq.ParquetFile(path).read()
    if table.schema != schema or table.num_rows != receipt["rows"]:
        raise ValueError("partition_schema_or_count_mismatch")
    return table


def feature_statistics(features: pa.Table) -> dict[str, object]:
    result = {}
    for name in RECENT:
        values = features[name].to_pylist()
        if not values or any(value is None for value in values):
            raise ValueError("missing_recent_values")
        result[name] = {"min": min(values), "max": max(values), "mean": mean(values),
                        "std": pstdev(values), "unique": len(set(values)),
                        "zero": values.count(0), "rows": len(values)}
    return result


def _interest_audit(rows: list[dict], profiles: list[dict], start: date,
                    categories: dict[str, str]) -> dict[str, object]:
    clicks = defaultdict(list)
    for row in rows:
        if row["event_type"] == "click":
            clicks[row["user_id"]].append(row)
    results = {}
    for changes, name in ((True, "changing"), (False, "stable")):
        deltas = []
        before_total = after_total = before_next = after_next = 0
        users = [p for p in profiles if p["changes_interest"] is changes]
        for profile in users:
            boundary = start + timedelta(days=profile["change_offset"])
            before = [r for r in clicks[profile["user_id"]] if r["event_timestamp"].astimezone(KST).date() < boundary]
            after = [r for r in clicks[profile["user_id"]] if r["event_timestamp"].astimezone(KST).date() >= boundary]
            if not before or not after:
                continue
            left = sum(categories[r["video_id"]] == profile["next_primary"] for r in before)
            right = sum(categories[r["video_id"]] == profile["next_primary"] for r in after)
            deltas.append(right / len(after) - left / len(before))
            before_total += len(before)
            after_total += len(after)
            before_next += left
            after_next += right
        results[name] = {
            "users": len(users), "observed_both_sides": len(deltas),
            "mean_user_next_category_share_change": mean(deltas) if deltas else None,
            "positive_users": sum(value > 0 for value in deltas),
            "before_clicks": before_total, "after_clicks": after_total,
            "before_next_category_clicks": before_next, "after_next_category_clicks": after_next,
        }
    return results


def audit_behavior_data(root: Path) -> dict[str, object]:
    """실제 파일을 검증한 후 학습일 피처와 생성 행동 진단을 반환한다."""
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest["version"] != VERSION:
        raise ValueError("unknown_behavior_version")
    request = BehaviorDataRequest(manifest["seed"], date.fromisoformat(manifest["training_date"]))
    dates = request.dates
    validation, final = select_fixture_user_ids(request.seed)
    expected_users = sorted(validation + final)
    if (manifest["start_date"] != str(request.start_date) or manifest["end_date"] != str(dates[-1])
        or manifest["latent_profiles"] != [user_profile(request.seed, user) for user in expected_users]
        or manifest["validation_users"] != list(validation) or manifest["reserved_final_users"] != list(final)
        or manifest["final_evaluations"] != 0):
        raise ValueError("invalid_behavior_manifest")
    if [p["date"] for p in manifest["partitions"]] != [str(day) for day in dates]:
        raise ValueError("noncanonical_partition_dates")
    raw_users = _read(root, manifest["users"], "inputs/virtual_users.parquet", FIXTURE_VIRTUAL_USER_SCHEMA_V1)
    user_ids = raw_users["user_id"].to_pylist()
    if user_ids != expected_users:
        raise ValueError("invalid_user_population")
    user_available = datetime.combine(request.start_date, datetime.min.time(), tzinfo=KST).astimezone(UTC)
    if any(datetime.fromisoformat(at) != user_available for at in raw_users["generated_at"].to_pylist()):
        raise ValueError("invalid_user_availability")
    tables, video_tables, daily = [], [], []
    known_ids: set[str] = set()
    categories = {}
    for day, partition in zip(dates, manifest["partitions"], strict=True):
        videos = _read(root, partition["videos"], f"inputs/youtube_trending_kr/dt={day}/part-0.parquet", FIXTURE_YOUTUBE_SCHEMA_V1)
        day_video_ids = set(videos["video_id"].to_pylist())
        if len(day_video_ids) != 48:
            raise ValueError("invalid_daily_video_population")
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=KST).astimezone(UTC)
        if any(at != day_start for at in videos["collected_at"].to_pylist()):
            raise ValueError("invalid_video_availability")
        categories.update(zip(videos["video_id"].to_pylist(), videos["video_category"].to_pylist(), strict=True))
        table = _read(root, partition["events"], f"action_log/dt={day}/part-0.parquet", EVENT_LOG_PARQUET_SCHEMA)
        rows = table.to_pylist()
        by_slate = defaultdict(list)
        by_pair = defaultdict(list)
        for index, row in enumerate(rows):
            EventLog.model_validate({key: row[key] for key in EventLog.model_fields})
            if (row["event_timestamp"].astimezone(KST).date() != day or row["event_id"] in known_ids
                or row["event_id"] != f"db1s{request.seed}_{day:%Y%m%d}_{index:08d}"
                or row["user_id"] not in user_ids or row["video_id"] not in day_video_ids or not row["slate_id"]):
                raise ValueError("event_identity_or_date_invalid")
            known_ids.add(row["event_id"])
            by_slate[row["slate_id"]].append(row)
            by_pair[(row["slate_id"], row["user_id"], row["video_id"])].append(row)
        exposure_counts = []
        slate_users = set()
        for slate in by_slate.values():
            impressions = [r for r in slate if r["event_type"] == "impression"]
            if (len({r["user_id"] for r in slate}) != 1 or len(impressions) not in (8, 16, 24)
                or len({r["video_id"] for r in impressions}) != len(impressions)
                or sum(r["event_type"] == "click" for r in slate) > 1):
                raise ValueError("invalid_slate_members")
            if slate[0]["user_id"] in slate_users:
                raise ValueError("multiple_slates_per_user_day")
            slate_users.add(slate[0]["user_id"])
            exposure_counts.append(len(impressions))
        for events in by_pair.values():
            ordered = sorted(events, key=lambda r: r["event_timestamp"])
            kinds = [r["event_type"] for r in ordered]
            if kinds not in (["impression"], ["impression", "click", "view"], ["impression", "click", "view", "like"]):
                raise ValueError("invalid_attribution_chain")
            if any(left["event_timestamp"] >= right["event_timestamp"] for left, right in zip(ordered, ordered[1:])):
                raise ValueError("non_increasing_attribution_time")
        daily.append({"date": str(day), "events": len(rows), "active_users": len(by_slate),
                      "exposure_counts": dict(sorted(Counter(exposure_counts).items())),
                      "event_counts": dict(sorted(Counter(r["event_type"] for r in rows).items()))})
        tables.append(table)
        video_tables.append(videos)
    history = pa.concat_tables(tables)
    rows = history.to_pylist()
    users = normalize_user_metadata(raw_users)
    videos = normalize_video_metadata(pa.concat_tables(video_tables))
    training = tables[30].filter(pc.equal(tables[30]["event_type"], "impression"))
    midnight = datetime.combine(request.training_date, datetime.min.time(), tzinfo=KST).astimezone(UTC)
    query_schema = pa.schema([("user_id", pa.string()), ("video_id", pa.string()),
                              ("event_timestamp", pa.timestamp("us", tz="UTC"))])
    all_users = pa.Table.from_pylist([{"user_id": user, "video_id": video_tables[30]["video_id"][0].as_py(),
                                      "event_timestamp": midnight} for user in user_ids], schema=query_schema)
    recent = {}
    for label, query in (("all_users", all_users), ("training_impressions", training)):
        args = dict(users=users, videos=videos, embedding=AuditEmbedding(),
                    evaluation_start_date=request.training_date + timedelta(days=2), history_start_date=request.start_date)
        batch = build_local_features(query, history=history, **args)
        past = history.filter(pc.less(history["event_timestamp"], pa.scalar(midnight, type=pa.timestamp("us", tz="UTC"))))
        clean = build_local_features(query, history=past, **args)
        if not batch.features.equals(clean.features):
            raise ValueError("same_day_or_future_feature_leak")
        diagnostics = batch.diagnostics.to_pydict()
        if (not all(diagnostics["history_7d_complete"]) or not all(diagnostics["history_30d_complete"])
            or any(diagnostics["user_metadata_missing"]) or any(diagnostics["video_metadata_missing"])):
            raise ValueError("incomplete_feature_coverage")
        recent[label] = feature_statistics(batch.features)
    varying = all(value["unique"] >= 2 and value["std"] > 0 for stats in recent.values() for value in stats.values())
    interest = _interest_audit(rows, manifest["latent_profiles"], request.start_date, categories)
    changing = interest["changing"]
    observed_shift = changing["observed_both_sides"] > 0 and changing["mean_user_next_category_share_change"] > 0
    result = {
        "version": "diverse-behavior-audit-v1", "seed": request.seed,
        "manifest_sha256": sha256((root / "manifest.json").read_bytes()).hexdigest(),
        "audit_source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "generated_dates": [str(day) for day in dates], "total_events": len(rows),
        "users": len(user_ids), "training_impressions": len(training),
        "excluded_training_day_and_later_events": sum(len(t) for t in tables[30:]),
        "daily": daily, "recent_features": recent, "interest_shift": interest,
        "checks": {"recent_five_vary": varying, "interest_shift_observed": observed_shift,
                   "date_and_attribution_valid": True, "future_feature_invariant": True,
                   "history_coverage_complete": True},
        "quality_passed": varying and observed_shift, "final_evaluations": 0,
    }
    return result


def write_audit(root: Path) -> dict[str, object]:
    """감사 결과를 신규 파일에 저장한다. 기존 결과는 덮어쓰지 않는다."""
    result = audit_behavior_data(root)
    with (root / "audit.json").open("xb") as stream:
        stream.write(canonical_json_bytes(result))
    return result
