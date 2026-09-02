"""로컬 재학습 CLI의 설정·배선·안전한 출력 계약을 검증한다."""

import json
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest
from typer.testing import CliRunner

from autoresearch.cli import app
from autoresearch.feature_engineering.model_contract import FeatureContractError


def test_harness_predict_requires_seed() -> None:
    result = CliRunner().invoke(app, ["harness-predict", "--slate", "slate.parquet", "--out", "out.csv"])
    assert result.exit_code == 2
    assert "--seed" in result.output


def test_baseline_defaults_match_existing_training_config() -> None:
    import yaml
    from autoresearch.research_harness.local_training import LocalTrainingConfig

    path = Path(__file__).resolve().parents[2] / "autoresearch" / "model_training" / "config.yaml"
    expected = yaml.safe_load(path.read_text(encoding="utf-8"))["model"]
    actual = LocalTrainingConfig().model_dump()
    assert actual == {name: expected[name] for name in actual}


def test_cli_delegates_default_config_and_explicit_seed(monkeypatch, tmp_path: Path) -> None:
    from autoresearch.research_harness import prediction

    calls = []
    monkeypatch.setattr(prediction, "run_harness_prediction", lambda **kwargs: calls.append(kwargs))
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, [
        "harness-predict", "--slate", "slate.parquet", "--out", "out.csv", "--seed", "17",
    ])
    assert result.exit_code == 0, result.output
    assert calls == [{"slate": Path("slate.parquet"), "out": Path("out.csv"),
                      "seed": 17, "config_path": Path("harness_config.json")}]


def test_cli_failure_does_not_print_traceback(monkeypatch) -> None:
    from autoresearch.research_harness import prediction

    def fail(**kwargs):
        raise FeatureContractError("harness_training_input_invalid")

    monkeypatch.setattr(prediction, "run_harness_prediction", fail)
    result = CliRunner().invoke(app, [
        "harness-predict", "--slate", "slate.parquet", "--out", "out.csv", "--seed", "17",
    ])
    assert result.exit_code == 1
    assert "harness_training_input_invalid" in result.output
    assert "Traceback" not in result.output


@pytest.fixture()
def wired_prediction(monkeypatch, tmp_path: Path):
    from autoresearch.research_harness import prediction

    config = tmp_path / "settings" / "harness_config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"embedding": {
        "model_id": "test/model", "revision": "a" * 40,
        "model_dir": "model", "cache_dir": "cache", "device": "cpu",
    }}), encoding="utf-8")
    slate = tmp_path / "input" / "slate.parquet"
    slate.parent.mkdir()
    slate.write_bytes(b"loader seam")
    out = tmp_path / "output" / "predictions.csv"
    events = []
    loaded = object()
    model_text = "native model text\n"
    predictions = pa.table({
        "evaluation_id": ["eval_" + "b" * 64], "slate_id": ["slate-1"],
        "video_id": ["video-1"], "score": [0.25],
    })

    def loader(value):
        assert value == slate
        events.append("load")
        return loaded

    class Embedding:
        identity = "c" * 64
        manifest = {"model_id": "test/model"}
        stats = {"cache_hits": 0, "cache_misses": 1, "inference_calls": 1}

        def __init__(self, value):
            assert value.model_dir == config.parent / "model"
            assert value.cache_dir == config.parent / "cache"
            events.append("embedding")

    def train(inputs, *, seed, embedding, config):
        assert inputs is loaded and seed == 17 and isinstance(embedding, Embedding)
        events.append("fit")
        return SimpleNamespace(predictions=predictions, model_text=model_text,
                               receipt={"seed": seed, "fit_count": 1})

    monkeypatch.setattr(prediction, "load_local_training_input", loader)
    monkeypatch.setattr(prediction, "LocalSentenceTransformer", Embedding)
    monkeypatch.setattr(prediction, "train_local_candidate", train)
    return prediction, config, slate, out, events


def test_config_input_embedding_training_order_and_outputs(wired_prediction) -> None:
    prediction, config, slate, out, events = wired_prediction
    prediction.run_harness_prediction(slate=slate, out=out, seed=17, config_path=config)
    assert events == ["load", "embedding", "fit"]
    assert out.read_text(encoding="utf-8").splitlines() == [
        "evaluation_id,slate_id,video_id,score", "eval_" + "b" * 64 + ",slate-1,video-1,0.25",
    ]
    assert out.with_suffix(".model.txt").read_text(encoding="utf-8") == "native model text\n"
    receipt = json.loads(out.with_suffix(".training.json").read_text(encoding="utf-8"))
    assert receipt["seed"] == 17
    assert receipt["embedding_identity"] == "c" * 64
    assert str(config.parent) not in json.dumps(receipt)
    assert receipt["duration_seconds"] >= 0


@pytest.mark.parametrize("suffix", [".csv", ".model.txt", ".training.json"])
def test_existing_output_is_never_overwritten(wired_prediction, suffix: str) -> None:
    prediction, config, slate, out, events = wired_prediction
    out.parent.mkdir()
    existing = out.with_suffix(suffix)
    existing.write_bytes(b"preserve")
    with pytest.raises(FeatureContractError, match="harness_prediction_output_invalid"):
        prediction.run_harness_prediction(slate=slate, out=out, seed=17, config_path=config)
    assert existing.read_bytes() == b"preserve"
    assert events == []


def test_bad_input_does_not_load_gpu(wired_prediction, monkeypatch) -> None:
    prediction, config, slate, out, events = wired_prediction

    def fail(value):
        raise FeatureContractError("harness_training_input_invalid")

    monkeypatch.setattr(prediction, "load_local_training_input", fail)
    with pytest.raises(FeatureContractError, match="harness_training_input_invalid"):
        prediction.run_harness_prediction(slate=slate, out=out, seed=17, config_path=config)
    assert not events and not out.exists()


@pytest.mark.parametrize("seed", [-1, 2**32, True])
def test_invalid_seed_fails_before_loading(wired_prediction, seed) -> None:
    prediction, config, slate, out, events = wired_prediction
    with pytest.raises(FeatureContractError, match="harness_prediction_config_invalid"):
        prediction.run_harness_prediction(slate=slate, out=out, seed=seed, config_path=config)
    assert not events


def test_invalid_config_error_hides_values(wired_prediction) -> None:
    prediction, config, slate, out, events = wired_prediction
    config.write_text('{"private_value": "not-for-logs"}', encoding="utf-8")
    with pytest.raises(FeatureContractError) as error:
        prediction.run_harness_prediction(slate=slate, out=out, seed=17, config_path=config)
    assert str(error.value) == "harness_prediction_config_invalid"
    assert not events


def test_output_inside_input_is_rejected(wired_prediction) -> None:
    prediction, config, slate, out, events = wired_prediction
    with pytest.raises(FeatureContractError, match="harness_prediction_output_invalid"):
        prediction.run_harness_prediction(slate=slate, out=slate.parent / "predictions.csv",
                                          seed=17, config_path=config)
    assert not events


def test_cache_inside_input_is_rejected(wired_prediction) -> None:
    prediction, config, slate, out, events = wired_prediction
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["embedding"]["cache_dir"] = str(slate.parent / "cache")
    config.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FeatureContractError, match="harness_prediction_config_invalid"):
        prediction.run_harness_prediction(slate=slate, out=out, seed=17, config_path=config)
    assert not events


def test_output_inside_model_is_rejected(wired_prediction) -> None:
    prediction, config, slate, out, events = wired_prediction
    with pytest.raises(FeatureContractError, match="harness_prediction_output_invalid"):
        prediction.run_harness_prediction(slate=slate, out=config.parent / "model" / "predictions.csv",
                                          seed=17, config_path=config)
    assert not events


def test_output_without_filename_has_safe_error(wired_prediction) -> None:
    prediction, config, slate, out, events = wired_prediction
    with pytest.raises(FeatureContractError, match="harness_prediction_output_invalid"):
        prediction.run_harness_prediction(slate=slate, out=Path(out.anchor), seed=17, config_path=config)
    assert not events


@pytest.mark.parametrize("field", ["model_dir", "cache_dir"])
def test_malformed_config_path_cli_has_safe_error(wired_prediction, field: str) -> None:
    prediction, config, slate, out, events = wired_prediction
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["embedding"][field] = "\x00synthetic-private-path"
    config.write_text(json.dumps(payload), encoding="utf-8")
    result = CliRunner().invoke(app, [
        "harness-predict", "--slate", str(slate), "--out", str(out), "--seed", "17", "--config", str(config),
    ])
    assert result.exit_code == 1
    assert result.output.strip() == "harness_prediction_config_invalid"
    assert "Traceback" not in result.output and not events


def test_receipt_publication_failure_does_not_publish_csv(wired_prediction, monkeypatch) -> None:
    prediction, config, slate, out, events = wired_prediction
    original = Path.open

    def fail_receipt(self, mode="r", *args, **kwargs):
        if self == out.with_suffix(".training.json") and mode == "xb":
            raise OSError("private filesystem details")
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_receipt)
    with pytest.raises(FeatureContractError) as error:
        prediction.run_harness_prediction(slate=slate, out=out, seed=17, config_path=config)
    assert str(error.value) == "harness_prediction_output_invalid"
    assert out.with_suffix(".model.txt").exists()
    assert not out.exists()


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -0.1, 1.1])
def test_csv_writer_rejects_bad_probabilities(score: float) -> None:
    from autoresearch.research_harness.prediction import _prediction_bytes

    table = pa.table({"evaluation_id": ["eval_" + "a" * 64], "slate_id": ["s"],
                      "video_id": ["v"], "score": [score]})
    with pytest.raises(FeatureContractError, match="harness_prediction_output_invalid"):
        _prediction_bytes(table)


def test_csv_writer_matches_sealed_parser(tmp_path: Path) -> None:
    from autoresearch.research_harness.prediction import _prediction_bytes
    from autoresearch.research_harness.prediction_parser import parse_prediction_copy

    table = pa.table({"evaluation_id": ["eval_" + "a" * 64] * 4, "slate_id": ["s"] * 4,
                      "video_id": ["v0", "v1", "v2", "v3"], "score": [0.0, 1.0, 1e-300, 0.12345678901234567]})
    path = tmp_path / "predictions.csv"
    path.write_bytes(_prediction_bytes(table))
    rows = parse_prediction_copy(path)
    assert [row.score for row in rows] == table["score"].to_pylist()
