"""고정 행동 bundle에서 모델을 학습하고 저장 모델을 재사용하는 오프라인 구간.

[파이프라인] behavior_training의 피처 조립 이후, Judge 예측 제출 이전을 담당한다.
[기능] 등록된 arm/seed와 기본 설정으로 train subset만 fit하고 native 모델 및
재현 receipt를 보존한다. 별도 예측 호출은 hash로 봉인한 모델을 재로딩한다.
[비책임] 온라인 serving, 평가 cohort 생성, 지표 채점과 final 소비는 수행하지 않는다.
"""

from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
from time import perf_counter

import lightgbm as lgb
import numpy as np
import pandas as pd
import pyarrow as pa

from autoresearch.feature_engineering.model_contract import CATEGORICAL_FEATURE_COLUMNS
from autoresearch.model_training.lgbm_model import LGBMModel
from autoresearch.research_harness.behavior_training import TRAINING_SEEDS, arm_columns, load_behavior_training
from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes
from autoresearch.research_harness.local_training import LocalTrainingConfig, _read_regular


E5_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"


def _registered(seed: int, arm: str) -> tuple[str, ...]:
    if type(seed) is not int or seed not in TRAINING_SEEDS:
        raise ValueError("unregistered_training_seed")
    return arm_columns(arm)


def _embedding_identity(embedding: dict) -> str:
    manifest = embedding["manifest"]
    # Same serialization as local_embedding's execution identity (ASCII escaped).
    identity = sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=True,
                                 separators=(",", ":")).encode("ascii")).hexdigest()
    if (embedding["identity"] != identity or embedding["dimension"] != 384
        or manifest["model_id"] != "intfloat/multilingual-e5-small"
        or manifest["revision"] != E5_REVISION or manifest["dimension"] != 384):
        raise ValueError("behavior_embedding_contract_invalid")
    return identity


def _feature_frame(features: pa.Table, columns: tuple[str, ...]) -> pd.DataFrame:
    if features.column_names != list(columns) or not len(features):
        raise ValueError("behavior_prediction_columns_invalid")
    for name in columns:
        values = features[name]
        expected = pa.string() if name in CATEGORICAL_FEATURE_COLUMNS else pa.float64() if name == "topic_similarity" else pa.int64()
        if values.type != expected or values.null_count:
            raise ValueError("behavior_prediction_schema_invalid")
        if name not in CATEGORICAL_FEATURE_COLUMNS and not np.isfinite(values.to_numpy()).all():
            raise ValueError("behavior_prediction_nonfinite")
    return features.to_pandas()


def train_behavior_model(
    bundle_root: Path, output_root: Path, *, expected_bundle_sha256: str, seed: int, arm: str,
) -> dict[str, object]:
    """검증된 bundle의 train 행만 한 번 fit하고 새 디렉터리에 모델을 저장한다.

    Args:
        bundle_root: 고정된 labels/features/splits의 입력 디렉터리.
        output_root: 아직 존재하지 않는 모델 출력 디렉터리.
        expected_bundle_sha256: 호출자가 사전에 고정한 bundle.json hash.
        seed: 사전 등록한 401/402/403 중 하나.
        arm: with_recent 또는 without_recent.

    Returns:
        model.txt hash, 피처 순서, vocabulary, split 및 모델 설정을 기록한 receipt.
    """
    columns = _registered(seed, arm)
    if (output_root.resolve().is_relative_to(bundle_root.resolve())
        or bundle_root.resolve().is_relative_to(output_root.resolve())):
        raise ValueError("behavior_model_output_overlap")
    bundle = load_behavior_training(bundle_root, expected_manifest_sha256=expected_bundle_sha256)
    config = LocalTrainingConfig()
    if bundle.manifest["model_config"] != config.model_dump():
        raise ValueError("behavior_model_config_not_registered")
    _embedding_identity(bundle.manifest["embedding"])
    frame = _feature_frame(bundle.features[arm].select(columns), columns)
    labels = pd.Series(bundle.labels["clicked"].to_pylist(), name="clicked")
    split = bundle.splits[str(seed)]
    x_train, y_train = frame.iloc[split["train"]].copy(), labels.iloc[split["train"]]
    x_validation = frame.iloc[split["validation"]]
    categories: dict[str, list[str]] = {}
    for name in (column for column in CATEGORICAL_FEATURE_COLUMNS if column in columns):
        vocabulary = pd.api.types.union_categoricals([
            x_train[name].astype("category"), x_validation[name].astype("category"),
        ]).categories
        categories[name] = vocabulary.tolist()
        x_train[name] = pd.Categorical(x_train[name], categories=vocabulary)
    weight = float((y_train == 0).sum() / (y_train == 1).sum())
    model = LGBMModel(scale_pos_weight=weight, n_estimators=config.n_estimators,
                      learning_rate=config.learning_rate, num_leaves=config.num_leaves, random_state=seed)
    # Existing output also records an interrupted attempt: never silently fit again.
    output_root.mkdir(parents=True, exist_ok=False)
    started = perf_counter()
    (output_root / "attempt.json").write_bytes(canonical_json_bytes({
        "input_bundle_sha256": expected_bundle_sha256, "seed": seed, "arm": arm,
        "model_config": config.model_dump(),
    }))
    model.fit(x_train, y_train, categorical_features=list(categories))
    model_payload = model.model.booster_.model_to_string().encode("utf-8")
    (output_root / "model.txt").write_bytes(model_payload)
    receipt: dict[str, object] = {
        "version": "behavior-model-v1", "input_bundle_sha256": expected_bundle_sha256,
        "source_seed": bundle.manifest["source_seed"], "seed": seed, "arm": arm,
        "model": {"path": "model.txt", "sha256": sha256(model_payload).hexdigest()},
        "feature_columns": list(columns), "categorical_categories": categories,
        "split_receipts": bundle.manifest["split_receipts"][str(seed)],
        "model_config": config.model_dump(), "scale_pos_weight": weight,
        "embedding": bundle.manifest["embedding"],
        "fit_calls": 1, "evaluation_calls": 0, "final_claims": 0,
        "versions": {name: version(name) for name in ("lightgbm", "numpy", "pandas", "pyarrow")},
        "executor_source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "duration_seconds": perf_counter() - started,
    }
    # Receipt is the completion marker and is written only after model bytes.
    (output_root / "receipt.json").write_bytes(canonical_json_bytes(receipt))
    return receipt


def predict_behavior_model(
    model_root: Path, features: pa.Table, *, expected_receipt_sha256: str, embedding_identity: str,
) -> np.ndarray:
    """봉인한 native 모델로 확률을 예측한다. fit/채점/final claim은 호출하지 않는다.

    Args:
        model_root: train_behavior_model의 완료 모델 디렉터리.
        features: ID/정답을 제외한 arm의 정확한 열 순서 및 dtype인 피처 테이블.
        expected_receipt_sha256: 학습 완료 때 고정한 receipt.json hash.
        embedding_identity: 평가 피처를 생성한 실제 E5 adapter의 identity.

    Returns:
        입력 행 순서를 보존한 1차원 클릭 확률 배열.
    """
    payload = _read_regular(model_root / "receipt.json")
    if sha256(payload).hexdigest() != expected_receipt_sha256:
        raise ValueError("behavior_model_receipt_mismatch")
    receipt = json.loads(payload)
    columns = _registered(receipt["seed"], receipt["arm"])
    if (receipt["version"] != "behavior-model-v1" or receipt["feature_columns"] != list(columns)
        or receipt["model_config"] != LocalTrainingConfig().model_dump()
        or receipt["model"]["path"] != "model.txt" or receipt["fit_calls"] != 1):
        raise ValueError("behavior_model_receipt_invalid")
    if _embedding_identity(receipt["embedding"]) != embedding_identity:
        raise ValueError("behavior_prediction_embedding_mismatch")
    frame = _feature_frame(features, columns)
    categories = receipt["categorical_categories"]
    if set(categories) != set(columns) & set(CATEGORICAL_FEATURE_COLUMNS):
        raise ValueError("behavior_model_categories_invalid")
    for name, vocabulary in categories.items():
        if (not isinstance(vocabulary, list) or not vocabulary
            or any(type(value) is not str for value in vocabulary) or len(set(vocabulary)) != len(vocabulary)):
            raise ValueError("behavior_model_categories_invalid")
        frame[name] = pd.Categorical(frame[name], categories=vocabulary)
    model_payload = _read_regular(model_root / "model.txt")
    if sha256(model_payload).hexdigest() != receipt["model"]["sha256"]:
        raise ValueError("behavior_model_bytes_mismatch")
    model = lgb.Booster(model_str=model_payload.decode("utf-8"))
    if model.feature_name() != list(columns):
        raise ValueError("behavior_model_columns_mismatch")
    scores = np.asarray(model.predict(frame), dtype=np.float64)
    if scores.shape != (len(features),) or not np.isfinite(scores).all() or ((scores < 0) | (scores > 1)).any():
        raise ValueError("behavior_prediction_probabilities_invalid")
    return scores
