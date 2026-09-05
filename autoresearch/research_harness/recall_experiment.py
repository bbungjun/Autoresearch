"""Recall 통제 실험의 피처·학습·보정 경계.

[파이프라인] 검증된 학습 bundle과 봉인 평가 사이의 연구 전용 학습을 담당한다.
[기능] 사용자 분할, 사전등록된 단일 요인 arm, 시점 안전 선호 피처와 양의 기울기
확률 보정을 제공하며 학습 시도·산출물 receipt를 독점 생성하고 재로딩을 검증한다.
[비책임] 원본 준비, 전체 예산/실행 claim, 후보 선택과 final 소비는 실험 실행기,
서빙 및 production 모델 승격은 기존 운영 파이프라인의 책임이다.
"""

from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import date, timedelta
from hashlib import sha256
from importlib.metadata import version
from itertools import groupby
import json
from pathlib import Path
from time import perf_counter

import lightgbm as lgb
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
from scipy.optimize import minimize
from scipy.special import expit

from autoresearch.feature_engineering.model_contract import CATEGORICAL_FEATURE_COLUMNS
from autoresearch.research_harness.behavior_data import KST
from autoresearch.research_harness.behavior_training import arm_columns as behavior_arm_columns
from autoresearch.research_harness.candidate_metadata import select_metadata_as_of
from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes
from autoresearch.research_harness.local_training import _read_regular


ARMS = ("baseline15", "reference10", "shallow", "ranker", "preference", "larger")
TRAINING_SEEDS = (401, 402)
PREFERENCE_COLUMNS = ("video_category_click_share_7d", "video_category_click_share_30d")
RECEIPT_VERSION = "recall-controlled-model-v1"


def arm_columns(arm: str) -> tuple[str, ...]:
    """등록 arm의 순서가 고정된 모델 입력 열을 반환한다."""
    if arm not in ARMS:
        raise ValueError("recall_arm_unregistered")
    if arm == "reference10":
        return behavior_arm_columns("without_recent")
    return behavior_arm_columns("with_recent") + (PREFERENCE_COLUMNS if arm == "preference" else ())


def group_split(labels: pa.Table, seed: int) -> dict[str, list[int]]:
    """사용자 hash 순서의 60/20/20 분할을 slate/video 순으로 반환한다.

    각 slate는 한 사용자에게 속해야 하며 같은 slate/video 중복은 허용하지 않는다.
    행 순서를 바꿔도 사용자 배정과 각 분할의 source ID 순서는 유지된다.
    """
    if type(seed) is not int or seed not in TRAINING_SEEDS:
        raise ValueError("recall_seed_unregistered")
    required = ("source_event_id", "user_id", "slate_id", "video_id", "clicked")
    if not set(required).issubset(labels.column_names) or not len(labels):
        raise ValueError("recall_labels_invalid")
    if any(labels[name].null_count for name in required):
        raise ValueError("recall_labels_null")
    rows = labels.select(required).to_pylist()
    owners: dict[str, str] = {}
    identities, pairs = set(), set()
    for row in rows:
        if any(not isinstance(row[name], str) or not row[name].strip() for name in required[:-1]):
            raise ValueError("recall_label_identity_invalid")
        if row["clicked"] not in (0, 1):
            raise ValueError("recall_label_nonbinary")
        slate, user = row["slate_id"], row["user_id"]
        if owners.setdefault(slate, user) != user:
            raise ValueError("recall_slate_multiple_users")
        pair = (slate, row["video_id"])
        if pair in pairs or row["source_event_id"] in identities:
            raise ValueError("recall_label_duplicate")
        pairs.add(pair)
        identities.add(row["source_event_id"])
    users = sorted(set(owners.values()), key=lambda user: (sha256(f"{seed}:{user}".encode()).hexdigest(), user))
    fit_end, calibration_end = len(users) * 3 // 5, len(users) * 4 // 5
    partitions = {"train": set(users[:fit_end]), "calibration": set(users[fit_end:calibration_end]),
                  "reserve": set(users[calibration_end:])}
    result = {}
    for name, members in partitions.items():
        positions = sorted((i for i, row in enumerate(rows) if row["user_id"] in members),
                           key=lambda i: (rows[i]["slate_id"], rows[i]["video_id"]))
        if not positions or {rows[i]["clicked"] for i in positions} != {0, 1}:
            raise ValueError("recall_split_requires_two_classes")
        result[name] = positions
    return result


def preference_features(slate: pa.Table, history: pa.Table, videos: pa.Table) -> pa.Table:
    """노출 당일 KST 자정 전 7/30일 click의 후보 카테고리 비율 두 열을 만든다.

    Args:
        slate: user_id/video_id/event_timestamp가 있는 원래 행 순서의 요청.
        history: event_type를 포함하는 관측 action log. 당일과 미래 click은 제외한다.
        videos: normalize_video_metadata로 정규화한 시점별 공개 영상 metadata.

    Returns:
        slate와 길이/순서가 같은 float64 두 열. base 피처에 append할 수 있다.
    """
    candidates = select_metadata_as_of(videos, slate.select(["video_id", "event_timestamp"]), entity_key="video_id")
    if any(candidates["metadata_missing"].to_pylist()):
        raise ValueError("recall_preference_candidate_metadata_missing")
    clicks = history.filter(pc.equal(history["event_type"], "click"))
    categories = select_metadata_as_of(videos, clicks.select(["video_id", "event_timestamp"]), entity_key="video_id")
    daily: dict[tuple[str, date], Counter] = defaultdict(Counter)
    for row, category in zip(clicks.to_pylist(), categories["category_id"].to_pylist(), strict=True):
        daily[row["user_id"], row["event_timestamp"].astimezone(KST).date()][category] += 1
    cache: dict[tuple[str, date, int], Counter] = {}
    output: dict[str, list[float]] = {name: [] for name in PREFERENCE_COLUMNS}
    for request, category in zip(slate.to_pylist(), candidates["category_id"].to_pylist(), strict=True):
        day = request["event_timestamp"].astimezone(KST).date()
        for days, name in zip((7, 30), PREFERENCE_COLUMNS, strict=True):
            key = (request["user_id"], day, days)
            if key not in cache:
                counts: Counter = Counter()
                for offset in range(1, days + 1):
                    counts.update(daily.get((request["user_id"], day - timedelta(days=offset)), {}))
                if counts[None]:
                    raise ValueError("recall_preference_history_metadata_missing")
                cache[key] = counts
            counts = cache[key]
            total = sum(counts.values())
            output[name].append(counts[category] / total if total else 0.0)
    return pa.table({name: pa.array(values, type=pa.float64()) for name, values in output.items()})


def _table_hash(table: pa.Table) -> str:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table.combine_chunks())
    return sha256(sink.getvalue().to_pybytes()).hexdigest()


def _write_once(path: Path, payload: bytes) -> str:
    with path.open("xb") as stream:
        stream.write(payload)
    return sha256(payload).hexdigest()


def _write_json_once(path: Path, value: dict) -> str:
    return _write_once(path, canonical_json_bytes(value))


def _model_parameters(seed: int, arm: str) -> dict:
    params = {"n_estimators": 200, "learning_rate": 0.05, "num_leaves": 7 if arm == "shallow" else 31,
              "min_child_samples": 20, "n_jobs": 1, "random_state": seed,
              "deterministic": True, "force_col_wise": True, "verbosity": -1}
    if arm == "ranker":
        params.update(objective="lambdarank", label_gain=[0, 1], lambdarank_truncation_level=10)
    else:
        params.update(objective="binary", scale_pos_weight=1.0)
    return params


def _frame(features: pa.Table, columns: tuple[str, ...], vocabulary: dict[str, list[str]]) -> pd.DataFrame:
    if not set(columns).issubset(features.column_names):
        raise ValueError("recall_features_missing")
    frame = features.select(columns).to_pandas()
    for name in columns:
        if name in vocabulary:
            # pd.Categorical maps unseen values to NaN with the same integer codes in every arm.
            frame[name] = pd.Categorical(frame[name], categories=vocabulary[name])
        elif not np.isfinite(frame[name].to_numpy(dtype=np.float64)).all():
            raise ValueError("recall_features_nonfinite")
    return frame


def fit_calibration(raw: np.ndarray, labels: np.ndarray) -> dict:
    """공통 raw margin에 고정된 양의 기울기 sigmoid를 한 번만 fit한다."""
    raw, labels = np.asarray(raw, dtype=np.float64), np.asarray(labels, dtype=np.float64)
    if raw.ndim != 1 or raw.shape != labels.shape or not np.isfinite(raw).all() or set(labels) != {0, 1}:
        raise ValueError("recall_calibration_input_invalid")

    def loss_and_gradient(parameters: np.ndarray) -> tuple[float, np.ndarray]:
        margin = parameters[0] * raw + parameters[1]
        residual = expit(margin) - labels
        return float(np.mean(np.logaddexp(0.0, margin) - labels * margin)), np.array([
            np.mean(residual * raw), np.mean(residual),
        ])

    result = minimize(loss_and_gradient, np.array([1.0, 0.0]), jac=True, method="L-BFGS-B",
                      bounds=[(1e-6, 100.0), (-100.0, 100.0)], options={"maxiter": 500})
    if not result.success or not np.isfinite(result.x).all() or not np.isfinite(result.fun):
        raise ValueError("recall_calibration_fit_failed")
    return {"slope": float(result.x[0]), "intercept": float(result.x[1]), "loss": float(result.fun),
            "iterations": int(result.nit), "method": "L-BFGS-B", "maxiter": 500,
            "slope_bounds": [1e-6, 100.0], "intercept_bounds": [-100.0, 100.0], "initial": [1.0, 0.0],
            "input_kind": "raw_margin", "rows": len(raw)}


def _raw_predict(model: lgb.Booster, frame: pd.DataFrame) -> np.ndarray:
    raw = np.asarray(model.predict(frame, raw_score=True, num_threads=1), dtype=np.float64)
    if raw.shape != (len(frame),) or not np.isfinite(raw).all():
        raise ValueError("recall_raw_prediction_invalid")
    return raw


def _verified_receipt(root: Path, expected_receipt_sha256: str) -> tuple[dict, bytes, dict]:
    payload = _read_regular(root / "receipt.json")
    if sha256(payload).hexdigest() != expected_receipt_sha256:
        raise ValueError("recall_receipt_hash_mismatch")
    receipt = json.loads(payload)
    if receipt["version"] != RECEIPT_VERSION or receipt["arm"] not in ARMS:
        raise ValueError("recall_receipt_contract_invalid")
    if receipt["code_sha256"] != sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError("recall_code_hash_mismatch")
    artifacts = {}
    for name in ("model.txt", "calibration.json", "model_attempt.json", "calibration_attempt.json", "model_complete.json"):
        data = _read_regular(root / name)
        if sha256(data).hexdigest() != receipt["artifacts"][name]:
            raise ValueError("recall_artifact_hash_mismatch")
        artifacts[name] = data
    calibration = json.loads(artifacts["calibration.json"])
    if (calibration["input_kind"] != "raw_margin" or not 1e-6 <= calibration["slope"] <= 100
            or not -100 <= calibration["intercept"] <= 100):
        raise ValueError("recall_calibration_contract_invalid")
    return receipt, artifacts["model.txt"], calibration


def train_model(
    labels: pa.Table, features: pa.Table, output: Path, seed: int, arm: str,
    input_hashes: dict, before_fit: Callable[[str], None],
) -> dict:
    """고정 모델·보정을 각 한 번 fit하고 receipt를 반환하거나 완료 결과를 검증한다.

    before_fit은 각 독점 attempt 생성 직후 실제 fit 전에 호출하여 외부 예산을 차감한다.
    완료 receipt 없는 기존 attempt는 실패/중단 상태이며 재학습하지 않고 오류를 낸다.
    """
    columns = arm_columns(arm)
    splits = group_split(labels, seed)
    if "source_event_id" not in features.column_names or features["source_event_id"].to_pylist() != labels["source_event_id"].to_pylist():
        raise ValueError("recall_features_alignment_invalid")
    vocabulary = {name: sorted(set(features[name].take(splits["train"]).to_pylist()))
                  for name in CATEGORICAL_FEATURE_COLUMNS if name in columns}
    frame = _frame(features, columns, vocabulary)
    positions = splits["train"]
    if arm == "larger":
        slate_ids, video_ids = labels["slate_id"].to_pylist(), labels["video_id"].to_pylist()
        positions = sorted(positions + splits["reserve"], key=lambda i: (slate_ids[i], video_ids[i]))
    params = _model_parameters(seed, arm)
    contract = {"version": RECEIPT_VERSION, "arm": arm, "seed": seed, "input_hashes": input_hashes,
                "labels_sha256": _table_hash(labels), "features_sha256": _table_hash(features.select(["source_event_id", *columns])),
                "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(), "model_parameters": params,
                "feature_columns": list(columns), "vocabulary": vocabulary,
                "base_split_sha256": sha256(canonical_json_bytes(splits)).hexdigest(),
                "fit_positions_sha256": sha256(canonical_json_bytes(positions)).hexdigest()}
    if (output / "receipt.json").exists():
        expected = sha256(_read_regular(output / "receipt.json")).hexdigest()
        receipt, _, _ = _verified_receipt(output, expected)
        if any(receipt.get(key) != value for key, value in contract.items()):
            raise ValueError("recall_resume_contract_mismatch")
        return receipt
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("recall_incomplete_attempt_no_retry")
    y = np.asarray(labels["clicked"].to_pylist(), dtype=np.int8)
    fit_slates = labels["slate_id"].take(positions).to_pylist()
    groups = [len(list(rows)) for _, rows in groupby(fit_slates)]
    if sum(groups) != len(positions) or len(groups) != len(set(fit_slates)):
        raise ValueError("recall_rank_groups_invalid")
    artifacts = {}
    artifacts["model_attempt.json"] = _write_json_once(output / "model_attempt.json", {"kind": "model", **contract})
    before_fit("model")
    started = perf_counter()
    model = lgb.LGBMRanker(**params) if arm == "ranker" else lgb.LGBMClassifier(**params)
    kwargs = {"group": groups} if arm == "ranker" else {}
    model.fit(frame.iloc[positions], y[positions], categorical_feature=list(vocabulary), **kwargs)
    model_seconds = perf_counter() - started
    model_bytes = model.booster_.model_to_string().encode("utf-8")
    artifacts["model.txt"] = _write_once(output / "model.txt", model_bytes)
    artifacts["model_complete.json"] = _write_json_once(output / "model_complete.json", {
        "model_sha256": artifacts["model.txt"], "duration_seconds": model_seconds, "fit_attempts": 1,
    })
    raw = _raw_predict(model.booster_, frame.iloc[splits["calibration"]])
    calibration_input = {"raw_sha256": sha256(raw.tobytes()).hexdigest(),
                         "labels_sha256": sha256(y[splits["calibration"]].tobytes()).hexdigest()}
    artifacts["calibration_attempt.json"] = _write_json_once(output / "calibration_attempt.json", {
        "kind": "calibration", "model_sha256": artifacts["model.txt"], **calibration_input,
    })
    before_fit("calibration")
    started = perf_counter()
    calibration = fit_calibration(raw, y[splits["calibration"]])
    calibration_seconds = perf_counter() - started
    artifacts["calibration.json"] = _write_json_once(output / "calibration.json", calibration)
    receipt = {**contract, "artifacts": artifacts, "fit_rows": len(positions),
               "calibration_rows": len(splits["calibration"]), "base_fit_rows": len(splits["train"]),
               "fit_groups": len(groups), "group_sizes_sha256": sha256(canonical_json_bytes(groups)).hexdigest(),
               "calibration_input": calibration_input, "model_seconds": model_seconds,
               "calibration_seconds": calibration_seconds, "duration_seconds": model_seconds + calibration_seconds,
               "fit_attempts": 2, "model_fit_attempts": 1, "calibration_fit_attempts": 1, "paid_api_calls": 0,
               "versions": {name: version(name) for name in ("numpy", "pandas", "pyarrow", "lightgbm", "scipy")}}
    _write_json_once(output / "receipt.json", receipt)
    return receipt


def predict_model(root: Path, features: pa.Table, expected_receipt_sha256: str) -> tuple[np.ndarray, np.ndarray]:
    """검증된 모델/보정기로 raw margin과 보정 확률을 원래 요청 순서로 반환한다."""
    receipt, model_bytes, calibration = _verified_receipt(root, expected_receipt_sha256)
    columns = arm_columns(receipt["arm"])
    if receipt["feature_columns"] != list(columns):
        raise ValueError("recall_prediction_columns_mismatch")
    frame = _frame(features, columns, receipt["vocabulary"])
    model = lgb.Booster(model_str=model_bytes.decode("utf-8"))
    raw = _raw_predict(model, frame)
    probability = expit(calibration["slope"] * raw + calibration["intercept"])
    if not np.isfinite(probability).all():
        raise ValueError("recall_calibrated_probability_invalid")
    return raw, probability
