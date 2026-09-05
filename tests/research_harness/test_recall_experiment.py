"""Recall 통제 실험의 그룹 누출, 단일 개입, 시점·재실행·보정 계약 회귀."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest
from scipy.special import expit

from autoresearch.feature_engineering.model_contract import CATEGORICAL_FEATURE_COLUMNS
from autoresearch.research_harness import recall_experiment as experiment
from autoresearch.research_harness.behavior_training import LABEL_SCHEMA
from autoresearch.research_harness.candidate_metadata import _VIDEO_SCHEMA


@pytest.fixture
def inputs() -> tuple[pa.Table, pa.Table]:
    labels = pa.Table.from_pylist([
        {"source_event_id": f"e{user}_{slate}_{video}", "user_id": f"u{user}",
         "slate_id": f"s{user}_{slate}", "video_id": f"v{video}",
         "event_timestamp": datetime(2026, 9, 2, tzinfo=UTC), "clicked": video}
        for user in range(10) for slate in range(2) for video in range(2)
    ], schema=LABEL_SCHEMA)
    data = {"source_event_id": labels["source_event_id"]}
    for name in experiment.arm_columns("preference"):
        data[name] = pa.array([f"category{i // 4}" for i in range(len(labels))], type=pa.string()) if name in CATEGORICAL_FEATURE_COLUMNS else pa.array([float(i % 2) for i in range(len(labels))])
    return labels, pa.table(data)


@pytest.fixture
def fake_models(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls = []

    class FakeBooster:
        def __init__(self, model_str: str = "mock model") -> None:
            assert model_str == "mock model"

        def model_to_string(self) -> str:
            return "mock model"

        def predict(self, frame: pd.DataFrame, *, raw_score: bool, num_threads: int) -> np.ndarray:
            assert raw_score and num_threads == 1
            return frame["duration_sec"].to_numpy() * 2 - 1

    class FakeModel:
        def __init__(self, **params: object) -> None:
            self.params = params
            self.booster_ = FakeBooster()

        def fit(self, frame: pd.DataFrame, y: np.ndarray, **kwargs: object) -> None:
            calls.append({"params": self.params, "frame": frame.copy(), "y": y.copy(), **kwargs})

    monkeypatch.setattr(experiment.lgb, "LGBMClassifier", FakeModel)
    monkeypatch.setattr(experiment.lgb, "LGBMRanker", FakeModel)
    monkeypatch.setattr(experiment.lgb, "Booster", FakeBooster)
    return calls


def test_user_split_is_disjoint_and_independent_of_row_order(inputs: tuple[pa.Table, pa.Table]) -> None:
    labels, _ = inputs
    split = experiment.group_split(labels, 401)
    sets = [{labels["user_id"][i].as_py() for i in positions} for positions in split.values()]
    assert [len(users) for users in sets] == [6, 2, 2]
    assert not (sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2])
    assert sorted(i for positions in split.values() for i in positions) == list(range(len(labels)))
    reordered = labels.take(list(reversed(range(len(labels)))))
    new_split = experiment.group_split(reordered, 401)
    for key in split:
        assert labels["source_event_id"].take(split[key]).to_pylist() == reordered["source_event_id"].take(new_split[key]).to_pylist()


def test_slate_crossing_users_is_rejected(inputs: tuple[pa.Table, pa.Table]) -> None:
    labels, _ = inputs
    rows = labels.to_pylist()
    rows[1]["user_id"] = "another-user"
    with pytest.raises(ValueError, match="slate_multiple_users"):
        experiment.group_split(pa.Table.from_pylist(rows, schema=labels.schema), 401)


def test_six_arms_share_splits_and_only_change_registered_factor(
    inputs: tuple[pa.Table, pa.Table], fake_models: list[dict], tmp_path: Path,
) -> None:
    labels, features = inputs
    receipts = {}
    attempts = []
    for arm in experiment.ARMS:
        receipts[arm] = experiment.train_model(labels, features, tmp_path / arm, 401, arm, {"bundle": "a" * 64}, attempts.append)
    assert attempts == ["model", "calibration"] * 6
    assert len({receipt["base_split_sha256"] for receipt in receipts.values()}) == 1
    assert all(receipt["vocabulary"] == receipts["baseline15"]["vocabulary"] for receipt in receipts.values())
    assert {arm: len(receipt["feature_columns"]) for arm, receipt in receipts.items()} == {
        "baseline15": 15, "reference10": 10, "shallow": 15, "ranker": 15, "preference": 17, "larger": 15,
    }
    assert receipts["larger"]["fit_rows"] == 32
    assert all(receipt["calibration_rows"] == 8 for receipt in receipts.values())
    base_params = receipts["baseline15"]["model_parameters"]
    assert receipts["shallow"]["model_parameters"] == {**base_params, "num_leaves": 7}
    rank_call = fake_models[3]
    assert rank_call["group"] == [2] * 12
    assert sum(rank_call["group"]) == len(rank_call["frame"])
    assert rank_call["params"]["objective"] == "lambdarank"
    assert rank_call["params"]["label_gain"] == [0, 1]
    assert rank_call["params"]["lambdarank_truncation_level"] == 10
    assert "sample_weight" not in fake_models[0]
    assert base_params["scale_pos_weight"] == 1
    assert fake_models[-1]["frame"]["category_id"].isna().sum() == 8


def test_positive_calibration_uses_margin_and_preserves_order() -> None:
    raw = np.array([-2.0, -1.0, 0.0, 1.0, 2.0, -1.0, 0.0, 1.0])
    labels = np.array([0, 0, 1, 1, 1, 1, 0, 0])
    result = experiment.fit_calibration(raw, labels)
    assert result["input_kind"] == "raw_margin"
    assert 0 < result["slope"] <= 100
    calibrated = expit(result["slope"] * raw + result["intercept"])
    assert np.array_equal(np.argsort(raw), np.argsort(calibrated))
    assert result["loss"] <= np.mean(np.logaddexp(0, raw) - labels * raw) + 1e-10


def test_calibration_failure_is_not_silently_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiment, "minimize", lambda *args, **kwargs: SimpleNamespace(success=False))
    with pytest.raises(ValueError, match="calibration_fit_failed"):
        experiment.fit_calibration(np.array([-1.0, 1.0]), np.array([0, 1]))


def test_completed_receipt_reloads_without_fit_and_prediction_accepts_no_ids(
    inputs: tuple[pa.Table, pa.Table], fake_models: list[dict], tmp_path: Path,
) -> None:
    labels, features = inputs
    receipt = experiment.train_model(labels, features, tmp_path, 401, "baseline15", {"bundle": "a" * 64}, lambda _: None)
    digest = sha256((tmp_path / "receipt.json").read_bytes()).hexdigest()

    def forbidden(_: str) -> None:
        pytest.fail("completed fit must not repeat")

    assert experiment.train_model(labels, features, tmp_path, 401, "baseline15", {"bundle": "a" * 64}, forbidden) == receipt
    assert len(fake_models) == 1
    raw, probability = experiment.predict_model(tmp_path, features.drop(["source_event_id"]), digest)
    assert set(raw) == {-1, 1}
    calibration = json.loads((tmp_path / "calibration.json").read_bytes())
    np.testing.assert_array_equal(probability, expit(calibration["slope"] * raw + calibration["intercept"]))
    with pytest.raises(ValueError, match="resume_contract_mismatch"):
        experiment.train_model(labels, features, tmp_path, 401, "baseline15", {"bundle": "b" * 64}, forbidden)


@pytest.mark.parametrize("artifact", ["model.txt", "calibration.json", "model_attempt.json", "calibration_attempt.json", "model_complete.json"])
def test_prediction_rejects_artifact_tampering(
    inputs: tuple[pa.Table, pa.Table], fake_models: list[dict], tmp_path: Path, artifact: str,
) -> None:
    labels, features = inputs
    experiment.train_model(labels, features, tmp_path, 401, "baseline15", {}, lambda _: None)
    digest = sha256((tmp_path / "receipt.json").read_bytes()).hexdigest()
    with (tmp_path / artifact).open("ab") as stream:
        stream.write(b"corruption")
    with pytest.raises(ValueError, match="artifact_hash_mismatch"):
        experiment.predict_model(tmp_path, features, digest)


def test_incomplete_model_or_calibration_never_retried(
    inputs: tuple[pa.Table, pa.Table], fake_models: list[dict], tmp_path: Path,
) -> None:
    labels, features = inputs

    def stop_calibration(kind: str) -> None:
        if kind == "calibration":
            raise RuntimeError("budget stop")

    with pytest.raises(RuntimeError, match="budget stop"):
        experiment.train_model(labels, features, tmp_path, 401, "baseline15", {}, stop_calibration)
    assert (tmp_path / "model_complete.json").exists()
    assert (tmp_path / "calibration_attempt.json").exists()
    with pytest.raises(ValueError, match="incomplete_attempt_no_retry"):
        experiment.train_model(labels, features, tmp_path, 401, "baseline15", {}, lambda _: None)
    assert len(fake_models) == 1


def test_feature_misalignment_rejected_before_attempt(
    inputs: tuple[pa.Table, pa.Table], fake_models: list[dict], tmp_path: Path,
) -> None:
    labels, features = inputs
    with pytest.raises(ValueError, match="alignment_invalid"):
        experiment.train_model(labels, features.take(list(reversed(range(len(features))))), tmp_path, 401, "ranker", {}, lambda _: None)
    assert not list(tmp_path.iterdir())
    assert not fake_models


def test_preference_uses_kst_past_windows_and_as_of_categories() -> None:
    midnight = datetime(2026, 9, 1, 15, tzinfo=UTC)  # KST September 2 midnight.
    metadata = []
    for identifier, category, at in (
        ("v1", "Music", midnight - timedelta(days=40)),
        ("v1", "Gaming", midnight - timedelta(days=2)),
        ("v2", "Gaming", midnight - timedelta(days=40)),
        ("v2", "Music", midnight + timedelta(days=1)),
    ):
        metadata.append({"video_id": identifier, "available_at": at, "category_id": category,
                         "duration_sec": 60, "published_at": midnight - timedelta(days=50),
                         **{name: 1 for name in ("view_count", "like_count", "comment_count", "channel_subscriber_count", "channel_view_count", "channel_video_count")}})
    videos = pa.Table.from_pylist(metadata, schema=_VIDEO_SCHEMA)
    slate = pa.table({"user_id": ["u", "u", "cold"], "video_id": ["v1", "v2", "v2"],
                      "event_timestamp": pa.array([midnight + timedelta(hours=10)] * 3, type=pa.timestamp("us", tz="UTC"))})
    rows = [
        {"event_type": kind, "user_id": "u", "video_id": video, "event_timestamp": at}
        for kind, video, at in (
            ("click", "v1", midnight - timedelta(days=30)),  # Music, inside 30d.
            ("click", "v1", midnight - timedelta(days=7)),   # Music, inside 7d.
            ("click", "v2", midnight - timedelta(days=1)),   # Gaming.
            ("click", "v2", midnight - timedelta(days=30, microseconds=1)),  # Outside.
            ("impression", "v2", midnight - timedelta(days=1)),
            ("click", "v2", midnight),                     # Same day excluded.
            ("click", "v2", midnight + timedelta(days=1)),  # Future excluded.
        )
    ]
    history = pa.Table.from_pylist(rows)
    result = experiment.preference_features(slate, history, videos)
    assert result.column_names == list(experiment.PREFERENCE_COLUMNS)
    assert result[experiment.PREFERENCE_COLUMNS[0]].to_pylist() == [0.5, 0.5, 0]
    assert result[experiment.PREFERENCE_COLUMNS[1]].to_pylist() == [1 / 3, 1 / 3, 0]
    assert result.equals(experiment.preference_features(slate, pa.Table.from_pylist(rows[:-2]), videos))
    missing_history = pa.Table.from_pylist([{
        "event_type": "click", "user_id": "u", "video_id": "not-observed",
        "event_timestamp": midnight - timedelta(days=1),
    }])
    with pytest.raises(ValueError, match="preference_history_metadata_missing"):
        experiment.preference_features(slate, missing_history, videos)
