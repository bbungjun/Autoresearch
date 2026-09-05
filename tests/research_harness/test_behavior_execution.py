"""작은 합성 bundle의 fit 행 경계 및 저장 모델 재사용 계약을 검증한다."""

from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from autoresearch.feature_engineering.model_contract import CATEGORICAL_FEATURE_COLUMNS
from autoresearch.model_training.lgbm_model import LGBMModel
from autoresearch.research_harness.behavior_execution import E5_REVISION, predict_behavior_model, train_behavior_model
from autoresearch.research_harness.behavior_training import (
    ARMS, LABEL_SCHEMA, TRAINING_SEEDS, arm_columns, load_behavior_training, split_labels, split_receipts,
)
from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes
from autoresearch.research_harness.local_training import LocalTrainingConfig


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """Actual-data-free 120-row input; synthetic E5 identity tests only serialization."""
    root = tmp_path / "bundle"
    root.mkdir()
    count = 120
    ids = [f"db1s10701_20260902_{i:08d}" for i in range(count)]
    labels = pa.Table.from_pylist([
        {"source_event_id": ids[i], "slate_id": f"s{i}", "user_id": f"u{i}", "video_id": f"v{i}",
         "event_timestamp": datetime(2026, 9, 2, tzinfo=UTC), "clicked": int(i % 3 == 0)}
        for i in range(count)
    ], schema=LABEL_SCHEMA)
    splits = {str(seed): split_labels(labels, seed) for seed in TRAINING_SEEDS}
    partition = {position: name for name, positions in splits["401"].items() for position in positions}
    data = {"source_event_id": pa.array(ids, type=pa.string())}
    for name in arm_columns("with_recent"):
        if name in CATEGORICAL_FEATURE_COLUMNS:
            values = [partition[i] if name == "age_group" else f"c{i % 3}" for i in range(count)]
            data[name] = pa.array(values, type=pa.string())
        else:
            data[name] = pa.array(np.arange(count), type=pa.float64() if name == "topic_similarity" else pa.int64())
    features = pa.table(data)

    def write(name: str, table: pa.Table) -> dict[str, object]:
        pq.write_table(table, root / name)
        return {"path": name, "sha256": digest(root / name), "rows": len(table)}

    embedding_manifest = {"model_id": "intfloat/multilingual-e5-small", "revision": E5_REVISION,
                          "dimension": 384, "test_fixture": True}
    manifest = {
        "version": "behavior-training-bundle-v1", "source_seed": 10701,
        "training_date": "2026-09-02", "history_start_date": "2026-08-03",
        "feature_history_end_date": "2026-09-01", "label_scan_end_date": "2026-09-03",
        "labels": write("labels.parquet", labels),
        "features": {arm: write(f"{arm}.parquet", features.select(["source_event_id", *arm_columns(arm)])) for arm in ARMS},
        "feature_columns": {arm: list(arm_columns(arm)) for arm in ARMS},
        "split_receipts": split_receipts(labels, splits), "model_config": LocalTrainingConfig().model_dump(),
        "embedding": {"dimension": 384, "manifest": embedding_manifest,
                      "identity": sha256(canonical_json_bytes(embedding_manifest)).hexdigest()},
    }
    (root / "splits.json").write_bytes(canonical_json_bytes(splits))
    manifest["splits"] = {"path": "splits.json", "sha256": digest(root / "splits.json")}
    (root / "bundle.json").write_bytes(canonical_json_bytes(manifest))
    return root


@pytest.mark.parametrize("arm", ARMS)
def test_only_train_is_fit_and_saved_model_predicts_without_refit(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str,
) -> None:
    inputs = load_behavior_training(bundle, expected_manifest_sha256=digest(bundle / "bundle.json"))
    original = LGBMModel.fit
    captured: dict = {}

    def spy(self: LGBMModel, X_train: pd.DataFrame, y_train: pd.Series, categorical_features: list) -> None:
        captured.update(x=X_train.copy(), y=y_train.copy(), calls=captured.get("calls", 0) + 1)
        original(self, X_train, y_train, categorical_features)
        frame = inputs.features[arm].select(arm_columns(arm)).to_pandas()
        for column in categorical_features:
            frame[column] = pd.Categorical(frame[column], categories=X_train[column].cat.categories)
        captured["probabilities"] = self.predict_proba(frame)[:, 1]

    monkeypatch.setattr(LGBMModel, "fit", spy)
    output = tmp_path / "model"
    receipt = train_behavior_model(bundle, output, expected_bundle_sha256=digest(bundle / "bundle.json"), seed=401, arm=arm)
    split = inputs.splits["401"]
    assert captured["x"].index.tolist() == captured["y"].index.tolist() == split["train"]
    assert not set(captured["x"].index) & set(split["validation"] + split["test"])
    assert set(captured["x"]["age_group"].cat.categories) == {"train", "validation"}
    assert receipt["split_receipts"] == inputs.manifest["split_receipts"]["401"]
    assert receipt["scale_pos_weight"] == 2.0
    assert receipt["feature_columns"] == list(arm_columns(arm))
    assert receipt["model_config"] == LocalTrainingConfig().model_dump()
    assert receipt["fit_calls"] == 1 and receipt["evaluation_calls"] == receipt["final_claims"] == 0
    features = inputs.features[arm].select(arm_columns(arm))
    prediction_args = {"expected_receipt_sha256": digest(output / "receipt.json"),
                       "embedding_identity": inputs.manifest["embedding"]["identity"]}

    def reject_fit(*args: object, **kwargs: object) -> None:
        pytest.fail("saved-model prediction must never fit")

    monkeypatch.setattr(LGBMModel, "fit", reject_fit)
    first = predict_behavior_model(output, features, **prediction_args)
    second = predict_behavior_model(output, features, **prediction_args)
    np.testing.assert_allclose(first, captured["probabilities"], rtol=0, atol=1e-14)
    np.testing.assert_array_equal(first, second)
    assert captured["calls"] == 1
    with pytest.raises(FileExistsError):
        train_behavior_model(bundle, output, expected_bundle_sha256=digest(bundle / "bundle.json"), seed=401, arm=arm)
    with pytest.raises(ValueError, match="embedding_mismatch"):
        predict_behavior_model(output, features, **{**prediction_args, "embedding_identity": "0" * 64})
    with pytest.raises(ValueError, match="columns_invalid"):
        predict_behavior_model(output, features.select(list(reversed(features.column_names))), **prediction_args)
    with pytest.raises(ValueError, match="schema_invalid"):
        predict_behavior_model(output, features.set_column(features.column_names.index("topic_similarity"),
                                                          "topic_similarity", pa.array(range(len(features)))), **prediction_args)
    with pytest.raises(ValueError, match="receipt_mismatch"):
        predict_behavior_model(output, features, **{**prediction_args, "expected_receipt_sha256": "0" * 64})
    with (output / "model.txt").open("ab") as handle:
        handle.write(b"corrupted")
    with pytest.raises(ValueError, match="bytes_mismatch"):
        predict_behavior_model(output, features, **prediction_args)


@pytest.mark.parametrize("mutation", ["hash", "seed", "arm", "projection", "config", "embedding"])
def test_invalid_training_contract_rejected_before_fit(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    def reject_fit(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid input must not reach fit")

    monkeypatch.setattr(LGBMModel, "fit", reject_fit)
    manifest = json.loads((bundle / "bundle.json").read_text())
    if mutation == "projection":
        path = bundle / "without_recent.parquet"
        table = pq.read_table(path)
        table = table.set_column(table.column_names.index("topic_similarity"), "topic_similarity",
                                 pa.array(np.ones(len(table)), type=pa.float64()))
        pq.write_table(table, path)
        manifest["features"]["without_recent"]["sha256"] = digest(path)
    elif mutation == "config":
        manifest["model_config"]["n_estimators"] = 5
    elif mutation == "embedding":
        manifest["embedding"]["manifest"]["revision"] = "not-registered"
    (bundle / "bundle.json").write_bytes(canonical_json_bytes(manifest))
    output = tmp_path / "invalid-model"
    with pytest.raises(ValueError):
        train_behavior_model(bundle, output, expected_bundle_sha256="0" * 64 if mutation == "hash" else digest(bundle / "bundle.json"),
                             seed=999 if mutation == "seed" else 401, arm="other" if mutation == "arm" else "with_recent")
    assert not output.exists()


def test_failed_attempt_preserved_and_not_refit(bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fail_fit(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("injected-fit-failure")

    monkeypatch.setattr(LGBMModel, "fit", fail_fit)
    output = tmp_path / "failed-model"
    args = dict(expected_bundle_sha256=digest(bundle / "bundle.json"), seed=401, arm="with_recent")
    with pytest.raises(RuntimeError, match="injected-fit-failure"):
        train_behavior_model(bundle, output, **args)
    assert (output / "attempt.json").exists() and not (output / "receipt.json").exists()
    with pytest.raises(FileExistsError):
        train_behavior_model(bundle, output, **args)
    assert calls == 1
