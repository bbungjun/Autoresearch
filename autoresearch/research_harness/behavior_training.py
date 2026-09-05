"""다양한 행동 이력에서 학습 전용 bundle을 조립하는 구간.

[파이프라인] 검증된 raw 행동과 모델 fit 사이에서 라벨·피처·분할을 준비한다.
[기능] 학습일 click 귀속, 과거 피처의 15/10열 projection, 동일 seed 분할과
파일 receipt를 보존하고 재로딩 때 계약을 검증한다.
[비책임] 평가 slate/snapshot 생성, 모델 학습·예측, Judge/final 소비는 수행하지 않는다.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import re

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
from sklearn.model_selection import train_test_split

from autoresearch.feature_engineering.model_contract import CATEGORICAL_FEATURE_COLUMNS
from autoresearch.research_harness.behavior_data import BehaviorDataRequest, KST
from autoresearch.research_harness.behavior_data_audit import audit_behavior_data, feature_statistics
from autoresearch.research_harness.candidate_metadata import normalize_user_metadata, normalize_video_metadata
from autoresearch.research_harness.click_attribution import attribute_clicks
from autoresearch.research_harness.embedding import TextEmbedder
from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes, _write_table
from autoresearch.research_harness.evaluation_snapshot_models import EvaluationWindow
from autoresearch.research_harness.evaluation_source_models import LoadedPartition, SourceEvent, SourcePartitionReceipt
from autoresearch.research_harness.local_features import build_local_features
from autoresearch.research_harness.local_training import LocalTrainingConfig, _read_regular, _read_table
from autoresearch.research_harness.personalization_ablation import ABLATION_FEATURE_GROUPS, feature_columns_for_arm


TRAINING_SEEDS = (401, 402, 403)
ARMS = ("with_recent", "without_recent")
LABEL_SCHEMA = pa.schema([
    pa.field("source_event_id", pa.string(), nullable=False),
    pa.field("slate_id", pa.string(), nullable=False),
    pa.field("user_id", pa.string(), nullable=False),
    pa.field("video_id", pa.string(), nullable=False),
    pa.field("event_timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("clicked", pa.int8(), nullable=False),
])


def arm_columns(arm: str) -> tuple[str, ...]:
    """Canonical 15/10열 projection. 평가용 추가 피처는 포함하지 않는다."""
    if arm not in ARMS:
        raise ValueError("unknown_behavior_arm")
    columns = feature_columns_for_arm("without_video_popularity")
    removed = ABLATION_FEATURE_GROUPS["without_recent_behavior"]
    return columns if arm == "with_recent" else tuple(column for column in columns if column not in removed)


def split_labels(labels: pa.Table, seed: int) -> dict[str, list[int]]:
    """기존 local_training과 같은 stratified 60/20/20 위치를 고정한다."""
    if type(seed) is not int or seed not in TRAINING_SEEDS:
        raise ValueError("unregistered_training_seed")
    y = np.asarray(labels["clicked"].to_pylist(), dtype=np.int8)
    if set(y) != {0, 1}:
        raise ValueError("training_requires_two_classes")
    train_val, test = train_test_split(np.arange(len(y)), test_size=0.2, random_state=seed, stratify=y)
    train, validation = train_test_split(train_val, test_size=0.25, random_state=seed, stratify=y[train_val])
    result = {"train": train.tolist(), "validation": validation.tolist(), "test": test.tolist()}
    if any(set(y[indexes]) != {0, 1} for indexes in result.values()):
        raise ValueError("split_requires_two_classes")
    return result


def attribute_training_day(partitions: tuple[LoadedPartition, ...], day: date) -> pa.Table:
    """당일 노출의 label만 산출한다. 다음날 30분 scan을 기존 귀속기로 처리한다."""
    window = EvaluationWindow(day - timedelta(days=30), day, day, day + timedelta(days=1), day, ())
    attributed = attribute_clicks(partitions, window)
    return pa.Table.from_pylist([
        {"source_event_id": row.source_event_id, "slate_id": row.slate_id, "user_id": row.user_id,
         "video_id": row.video_id, "event_timestamp": row.event_timestamp, "clicked": int(row.clicked)}
        for row in attributed
    ], schema=LABEL_SCHEMA)


def split_receipts(labels: pa.Table, splits: dict[str, dict[str, list[int]]]) -> dict:
    """분할별 source ID 순서와 양성 수, 자동 class weight의 검증 근거."""
    ids, y = labels["source_event_id"].to_pylist(), labels["clicked"].to_pylist()
    result = {}
    for seed, subsets in splits.items():
        result[seed] = {}
        for name, positions in subsets.items():
            positives = sum(y[i] for i in positions)
            result[seed][name] = {"rows": len(positions), "positive_rows": positives,
                                  "source_event_ids_sha256": sha256(canonical_json_bytes([ids[i] for i in positions])).hexdigest()}
        train = result[seed]["train"]
        result[seed]["scale_pos_weight"] = (train["rows"] - train["positive_rows"]) / train["positive_rows"]
    return result


@dataclass(frozen=True)
class BehaviorTrainingBundle:
    labels: pa.Table
    features: dict[str, pa.Table]
    splits: dict[str, dict[str, list[int]]]
    manifest: dict


def _write_receipt(root: Path, name: str, table: pa.Table) -> dict[str, object]:
    path = root / name
    _write_table(table, path)
    return {"path": name, "rows": len(table), "sha256": sha256(path.read_bytes()).hexdigest()}


def prepare_behavior_training(
    source: Path, destination: Path, *, expected_source_sha256: str,
    embedding: TextEmbedder, embedding_manifest: dict,
) -> dict[str, object]:
    """원본을 검증한 뒤 새 경로에 실제 피처·정답·분할을 준비한다. fit은 하지 않는다."""
    if destination.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(destination.resolve()):
        raise ValueError("source_destination_overlap")
    source_payload = _read_regular(source / "manifest.json")
    if sha256(source_payload).hexdigest() != expected_source_sha256:
        raise ValueError("source_manifest_mismatch")
    manifest = json.loads(source_payload)
    audit = audit_behavior_data(source)
    if audit["manifest_sha256"] != expected_source_sha256 or not audit["quality_passed"]:
        raise ValueError("source_quality_failed")
    request = BehaviorDataRequest(manifest["seed"], date.fromisoformat(manifest["training_date"]))

    def read(receipt: dict) -> pa.Table:
        return _read_table(source, receipt["path"], receipt["sha256"], receipt["rows"])

    users = normalize_user_metadata(read(manifest["users"]))
    histories, videos, partitions = [], [], []
    for item in manifest["partitions"]:
        day = date.fromisoformat(item["date"])
        history = read(item["events"])
        histories.append(history)
        videos.append(read(item["videos"]))
        events = tuple(SourceEvent(
            day, row["event_id"], row["event_type"], row["user_id"], row["video_id"],
            row["event_timestamp"], row["slate_id"], row["rank"], row["exposure_source"], row["policy_version"],
        ) for row in history.to_pylist())
        partitions.append(LoadedPartition(SourcePartitionReceipt(day, item["events"]["path"], len(history), item["events"]["sha256"]), events))
    labels = attribute_training_day(tuple(partitions), request.training_date)
    if len(labels) != audit["training_impressions"]:
        raise ValueError("attributed_row_count_mismatch")
    history = pa.concat_tables(histories)
    cutoff = datetime.combine(request.training_date, datetime.min.time(), tzinfo=KST).astimezone(UTC)
    past = history.filter(pc.less(history["event_timestamp"], pa.scalar(cutoff, type=pa.timestamp("us", tz="UTC"))))
    kwargs = dict(users=users, videos=normalize_video_metadata(pa.concat_tables(videos)), embedding=embedding,
                  evaluation_start_date=request.training_date + timedelta(days=2), history_start_date=request.start_date)
    batch = build_local_features(labels, history=past, **kwargs)
    full = build_local_features(labels, history=history, **kwargs)
    if not batch.features.equals(full.features):
        raise ValueError("future_feature_leak")
    diagnostics = batch.diagnostics.to_pydict()
    if (not all(diagnostics["history_7d_complete"]) or not all(diagnostics["history_30d_complete"])
        or any(diagnostics["user_metadata_missing"]) or any(diagnostics["video_metadata_missing"])):
        raise ValueError("training_metadata_or_history_incomplete")
    splits = {str(seed): split_labels(labels, seed) for seed in TRAINING_SEEDS}
    fit_stats = {seed: feature_statistics(batch.features.take(split["train"])) for seed, split in splits.items()}
    if not all(stats["unique"] > 1 for fit in fit_stats.values() for stats in fit.values()):
        raise ValueError("uninformative_fit_features")
    # 검증이 끝난 뒤만 신규 출력 root를 만든다. manifest 이전 실패는 부분 산출물이다.
    destination.mkdir(parents=True, exist_ok=False)
    label_receipt = _write_receipt(destination, "labels.parquet", labels)
    feature_receipts = {}
    for arm in ARMS:
        frame = batch.features.select(arm_columns(arm)).add_column(0, LABEL_SCHEMA.field("source_event_id"), labels["source_event_id"])
        feature_receipts[arm] = _write_receipt(destination, f"{arm}.parquet", frame)
    split_payload = canonical_json_bytes(splits)
    (destination / "splits.json").write_bytes(split_payload)
    receipt = {
        "version": "behavior-training-bundle-v1", "source_manifest_sha256": expected_source_sha256,
        "source_seed": request.seed, "training_date": str(request.training_date),
        "history_start_date": str(request.start_date), "feature_history_end_date": str(request.training_date - timedelta(days=1)),
        "label_scan_end_date": str(request.dates[-1]), "labels": label_receipt, "features": feature_receipts,
        "splits": {"path": "splits.json", "sha256": sha256(split_payload).hexdigest()},
        "split_receipts": split_receipts(labels, splits),
        "feature_columns": {arm: list(arm_columns(arm)) for arm in ARMS},
        "embedding": embedding_manifest, "model_config": LocalTrainingConfig().model_dump(),
        "fit_feature_statistics": fit_stats, "same_day_and_future_features_unchanged": True,
        "training_positive_rows": int(pc.sum(labels["clicked"]).as_py()),
        "excluded_warmup_impressions": sum(int(pc.sum(pc.equal(t["event_type"], "impression")).as_py()) for t in histories[:30]),
        "feature_history_rows": len(past), "label_scan_history_rows": len(history),
        "versions": {name: version(name) for name in ("pyarrow", "numpy", "scikit-learn")},
        "assembler_source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "fit_calls": 0, "evaluation_calls": 0,
    }
    (destination / "bundle.json").write_bytes(canonical_json_bytes(receipt))
    return receipt


def load_behavior_training(root: Path, *, expected_manifest_sha256: str) -> BehaviorTrainingBundle:
    """핀한 bundle을 재로딩하며 row 정렬·projection·분할을 재계산해 검증한다."""
    payload = _read_regular(root / "bundle.json")
    if sha256(payload).hexdigest() != expected_manifest_sha256:
        raise ValueError("bundle_manifest_mismatch")
    manifest = json.loads(payload)
    if manifest["version"] != "behavior-training-bundle-v1":
        raise ValueError("unknown_bundle_version")

    def read(receipt: dict, name: str) -> pa.Table:
        if receipt["path"] != name:
            raise ValueError("bundle_path_invalid")
        return _read_table(root, name, receipt["sha256"], receipt["rows"])

    labels = read(manifest["labels"], "labels.parquet")
    if labels.schema != LABEL_SCHEMA or any(labels[c].null_count for c in labels.column_names):
        raise ValueError("bundle_label_schema_invalid")
    ids = labels["source_event_id"].to_pylist()
    if len(set(ids)) != len(ids) or not ids:
        raise ValueError("bundle_label_ids_invalid")
    day = date.fromisoformat(manifest["training_date"])
    if (any(at.astimezone(KST).date() != day for at in labels["event_timestamp"].to_pylist())
        or set(labels["clicked"].to_pylist()) != {0, 1}
        or manifest["history_start_date"] != str(day - timedelta(days=30))
        or manifest["feature_history_end_date"] != str(day - timedelta(days=1))
        or manifest["label_scan_end_date"] != str(day + timedelta(days=1))):
        raise ValueError("bundle_training_window_invalid")
    if (any(not re.fullmatch(f"db1s{manifest['source_seed']}_{day:%Y%m%d}_" + r"\d{8}", identifier) for identifier in ids)
        or any(not value.strip() for name in ("source_event_id", "slate_id", "user_id", "video_id")
               for value in labels[name].to_pylist())):
        raise ValueError("bundle_label_identity_invalid")
    features = {arm: read(manifest["features"][arm], f"{arm}.parquet") for arm in ARMS}
    for arm, frame in features.items():
        if (frame.column_names != ["source_event_id", *arm_columns(arm)]
            or frame["source_event_id"].to_pylist() != ids
            or manifest["feature_columns"][arm] != list(arm_columns(arm))
            or any(frame[c].null_count for c in frame.column_names)):
            raise ValueError("bundle_feature_alignment_invalid")
        for name in arm_columns(arm):
            expected_type = pa.string() if name in CATEGORICAL_FEATURE_COLUMNS else pa.float64() if name == "topic_similarity" else pa.int64()
            if frame.schema.field(name).type != expected_type:
                raise ValueError("bundle_feature_type_invalid")
            if name not in CATEGORICAL_FEATURE_COLUMNS and not np.isfinite(frame[name].to_numpy()).all():
                raise ValueError("bundle_feature_nonfinite")
    if not features["without_recent"].equals(features["with_recent"].select(features["without_recent"].column_names)):
        raise ValueError("bundle_projection_mismatch")
    if manifest["splits"]["path"] != "splits.json":
        raise ValueError("bundle_split_path_invalid")
    split_payload = _read_regular(root / "splits.json")
    if sha256(split_payload).hexdigest() != manifest["splits"]["sha256"]:
        raise ValueError("bundle_split_hash_mismatch")
    splits = json.loads(split_payload)
    expected = {str(seed): split_labels(labels, seed) for seed in TRAINING_SEEDS}
    if (splits != expected or any(type(index) is not int for split in splits.values()
                                 for indexes in split.values() for index in indexes)):
        raise ValueError("bundle_split_mismatch")
    if manifest["split_receipts"] != split_receipts(labels, splits):
        raise ValueError("bundle_split_receipt_mismatch")
    return BehaviorTrainingBundle(labels, features, splits, manifest)
