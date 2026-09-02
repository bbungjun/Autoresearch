"""실제 로컬 adapter의 캐시·오프라인 로딩·실패 경계를 모델 없이 검증한다."""

from contextlib import nullcontext
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import sys

import numpy as np
import pytest
from pydantic import ValidationError

from autoresearch.feature_engineering.model_contract import FeatureContractError


class FakeOOM(RuntimeError):
    pass


class FakeModel:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.max_seq_length = 999
        self.error: Exception | None = None
        self.output: np.ndarray | None = None
        self.evaluated = False
        self.float_called = False

    def float(self) -> "FakeModel":
        self.float_called = True
        return self

    def eval(self) -> "FakeModel":
        self.evaluated = True
        return self

    def get_embedding_dimension(self) -> int:
        return 2

    def encode(self, texts: list[str], **kwargs: object) -> np.ndarray:
        self.calls.append((texts, kwargs))
        if self.error:
            raise self.error
        if self.output is not None:
            return self.output
        return np.array([[3.0, 4.0] if "first" in text else [4.0, 3.0] for text in texts])


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    name = "autoresearch.research_harness.local_embedding"
    assert find_spec(name), "RED: local_embedding 구현이 필요합니다"
    module = import_module(name)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "model.safetensors").write_bytes(b"fake weights")
    model = FakeModel()
    loads: list[tuple[str, dict[str, object]]] = []
    state = SimpleNamespace(available=True, load_error=None)

    def load(path: str, **kwargs: object) -> FakeModel:
        loads.append((path, kwargs))
        if state.load_error:
            raise state.load_error
        return model

    torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: state.available, OutOfMemoryError=FakeOOM),
        inference_mode=nullcontext,
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=load))
    monkeypatch.setattr(module, "version", lambda name: "test-1.0")

    def config(**updates: object) -> object:
        values = dict(model_id="example/model", revision="a" * 40,
                      model_dir=model_dir, cache_dir=tmp_path / "cache")
        return module.LocalEmbeddingConfig(**(values | updates))

    return SimpleNamespace(module=module, config=config, model=model, loads=loads,
                           state=state, model_dir=model_dir, root=tmp_path)


def test_offline_safe_loading_flags_and_manifest(setup: SimpleNamespace) -> None:
    adapter = setup.module.LocalSentenceTransformer(setup.config(max_seq_length=128))
    options = setup.loads[0][1]
    assert options["local_files_only"] is True
    assert options["trust_remote_code"] is False
    assert options["token"] is False
    assert options["model_kwargs"]["use_safetensors"] is True
    assert options["device"] == "cuda"
    assert setup.model.max_seq_length == 128
    assert setup.model.float_called and setup.model.evaluated
    assert adapter.dimension == 2
    assert len(adapter.identity) == 64
    assert str(setup.root) not in str(adapter.manifest)
    manifest = adapter.manifest
    manifest["model_id"] = "changed"
    assert adapter.manifest["model_id"] == "example/model"


def test_duplicate_dedup_order_roles_and_persistent_cache(setup: SimpleNamespace) -> None:
    adapter = setup.module.LocalSentenceTransformer(setup.config())
    result = adapter.encode(["first", "second", "first"], role="query")
    np.testing.assert_allclose(result, [[0.6, 0.8], [0.8, 0.6], [0.6, 0.8]])
    assert setup.model.calls[0][0] == ["query: first", "query: second"]
    assert len(setup.model.calls) == 1
    again = setup.module.LocalSentenceTransformer(setup.config())
    np.testing.assert_array_equal(again.encode(["first"], role="query"), result[:1])
    assert again.stats == {"cache_hits": 1, "cache_misses": 0, "inference_calls": 0}
    again.encode(["first"], role="document")
    assert setup.model.calls[-1][0] == ["passage: first"]
    assert again.stats == {"cache_hits": 1, "cache_misses": 1, "inference_calls": 1}
    assert again.encode([], role="query").shape == (0, 2)


@pytest.mark.parametrize("updates", [
    {"model_id": "another/model"}, {"revision": "b" * 40}, {"query_prefix": "Q: "},
    {"document_prefix": "D: "}, {"max_seq_length": 12}, {"preprocessing": "strip"},
    {"device": "cpu"},
])
def test_identity_change_invalidates_both_roles(setup: SimpleNamespace, updates: dict[str, object]) -> None:
    original = setup.module.LocalSentenceTransformer(setup.config())
    original.encode(["first"], role="query")
    original.encode(["first"], role="document")
    changed = setup.module.LocalSentenceTransformer(setup.config(**updates))
    assert original.identity != changed.identity
    changed.encode(["first"], role="query")
    changed.encode(["first"], role="document")
    assert changed.stats["cache_misses"] == 2


def test_model_bytes_and_versions_change_identity_but_paths_do_not(setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    original = setup.module.LocalSentenceTransformer(setup.config())
    copied = setup.root / "copy"
    copied.mkdir()
    for path in setup.model_dir.iterdir():
        (copied / path.name).write_bytes(path.read_bytes())
    assert setup.module.LocalSentenceTransformer(setup.config(model_dir=copied)).identity == original.identity
    ignored = copied / ".cache"
    ignored.mkdir()
    (ignored / "download.lock").write_bytes(b"changing bookkeeping")
    assert setup.module.LocalSentenceTransformer(setup.config(model_dir=copied)).identity == original.identity
    (copied / "model.safetensors").write_bytes(b"new weights")
    assert setup.module.LocalSentenceTransformer(setup.config(model_dir=copied)).identity != original.identity
    monkeypatch.setattr(setup.module, "version", lambda name: "test-2.0")
    assert setup.module.LocalSentenceTransformer(setup.config()).identity != original.identity


def test_preprocessing_preserves_original_text_cache_keys(setup: SimpleNamespace) -> None:
    adapter = setup.module.LocalSentenceTransformer(setup.config(preprocessing="strip", query_prefix=""))
    adapter.encode([" first ", "first"], role="query")
    assert setup.model.calls[0][0] == ["first", "first"]
    assert adapter.stats["cache_misses"] == 2


@pytest.mark.parametrize("updates", [
    {"revision": "main"}, {"revision": "A" * 40}, {"batch_size": 0},
    {"batch_size": True}, {"max_seq_length": "128"}, {"device": "auto"},
    {"preprocessing": "custom"}, {"model_id": ""}, {"unexpected": 1},
])
def test_config_rejects_invalid_values(setup: SimpleNamespace, updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        setup.config(**updates)


@pytest.mark.parametrize("texts,role", [("text", "query"), ([""], "query"), ([None], "query"), (["x"], "bad"), ([" "], "query")])
def test_invalid_inputs_fail_before_inference(setup: SimpleNamespace, texts: object, role: str) -> None:
    adapter = setup.module.LocalSentenceTransformer(setup.config())
    with pytest.raises(FeatureContractError, match="embedding_input_invalid"):
        adapter.encode(texts, role=role)
    assert not setup.model.calls


@pytest.mark.parametrize("filename", ["config.json", "model.safetensors"])
def test_incomplete_model_is_not_downloaded(setup: SimpleNamespace, filename: str) -> None:
    (setup.model_dir / filename).unlink()
    with pytest.raises(FeatureContractError, match="local_embedding_model_invalid"):
        setup.module.LocalSentenceTransformer(setup.config())
    assert not setup.loads


@pytest.mark.parametrize("filename", [
    "pytorch_model.bin", "weights.pt", "weights.pth", "weights.pkl", "weights.pickle", "weights.BIN",
])
def test_mixed_legacy_weights_rejected_before_loading(setup: SimpleNamespace, filename: str) -> None:
    dense = setup.model_dir / "2_Dense"
    dense.mkdir()
    legacy = dense / filename
    legacy.write_bytes(b"legacy weights")
    with pytest.raises(FeatureContractError, match="^local_embedding_model_invalid$"):
        setup.module.LocalSentenceTransformer(setup.config())
    assert not setup.loads
    assert legacy.read_bytes() == b"legacy weights"


def test_sentencepiece_tokenizer_model_is_allowed(setup: SimpleNamespace) -> None:
    (setup.model_dir / "sentencepiece.bpe.model").write_bytes(b"tokenizer model")
    setup.module.LocalSentenceTransformer(setup.config())
    assert len(setup.loads) == 1


def test_gpu_unavailable_has_no_cpu_fallback(setup: SimpleNamespace) -> None:
    setup.state.available = False
    with pytest.raises(FeatureContractError, match="local_embedding_cuda_unavailable"):
        setup.module.LocalSentenceTransformer(setup.config())
    assert not setup.loads
    setup.module.LocalSentenceTransformer(setup.config(device="cpu"))


@pytest.mark.parametrize("phase", ["load", "encode"])
@pytest.mark.parametrize("error,code", [(FakeOOM("private path"), "local_embedding_oom"), (RuntimeError("secret"), "local_embedding_execution_failed")])
def test_failures_are_sanitized_without_retry(setup: SimpleNamespace, phase: str, error: Exception, code: str) -> None:
    if phase == "load":
        setup.state.load_error = error
    else:
        setup.model.error = error
    with pytest.raises(FeatureContractError, match=f"^{code}$") as caught:
        adapter = setup.module.LocalSentenceTransformer(setup.config())
        adapter.encode(["private text"], role="query")
    assert caught.value.__suppress_context__
    assert len(setup.loads) == 1
    assert len(setup.model.calls) <= 1


@pytest.mark.parametrize("output", [np.array([[np.nan, 0.0]]), np.zeros((1, 2)), np.ones((1, 3)), np.ones((2, 2)), np.ones((1, 2), dtype=int)])
def test_invalid_inference_vectors_are_not_cached(setup: SimpleNamespace, output: np.ndarray) -> None:
    adapter = setup.module.LocalSentenceTransformer(setup.config())
    setup.model.output = output
    with pytest.raises(FeatureContractError, match="embedding_output_invalid"):
        adapter.encode(["first"], role="query")
    setup.model.output = None
    adapter.encode(["first"], role="query")
    assert len(setup.model.calls) == 2


@pytest.mark.parametrize("blob", [b"broken", np.array([np.nan, 0], dtype="<f4").tobytes(), np.array([2, 0], dtype="<f4").tobytes()])
def test_corrupt_cache_fails_instead_of_recomputing(setup: SimpleNamespace, blob: bytes) -> None:
    adapter = setup.module.LocalSentenceTransformer(setup.config())
    adapter.encode(["first"], role="query")
    with sqlite3.connect(setup.root / "cache" / "embeddings.sqlite3") as conn:
        conn.execute("UPDATE embeddings SET vector = ?", (blob,))
    with pytest.raises(FeatureContractError, match="local_embedding_cache_invalid"):
        adapter.encode(["first"], role="query")
    assert len(setup.model.calls) == 1


def test_cache_metadata_corruption_fails(setup: SimpleNamespace) -> None:
    adapter = setup.module.LocalSentenceTransformer(setup.config())
    adapter.encode(["first"], role="query")
    with sqlite3.connect(setup.root / "cache" / "embeddings.sqlite3") as conn:
        conn.execute("UPDATE embeddings SET dimension = 3")
    with pytest.raises(FeatureContractError, match="local_embedding_cache_invalid"):
        adapter.encode(["first"], role="query")


def test_cache_corrupt_database_and_overlapping_location_fail(setup: SimpleNamespace) -> None:
    with pytest.raises(FeatureContractError, match="local_embedding_cache_overlaps_model"):
        setup.module.LocalSentenceTransformer(setup.config(cache_dir=setup.model_dir / "cache"))
    cache = setup.root / "cache"
    cache.mkdir()
    (cache / "embeddings.sqlite3").write_bytes(b"not a sqlite database")
    with pytest.raises(FeatureContractError, match="local_embedding_cache_invalid"):
        setup.module.LocalSentenceTransformer(setup.config())


def test_missing_dependencies_are_sanitized(setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    with pytest.raises(FeatureContractError, match="local_embedding_dependencies_missing"):
        setup.module.LocalSentenceTransformer(setup.config())


def test_model_modified_during_loading_is_rejected(setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    def altered_loader(*args: object, **kwargs: object) -> FakeModel:
        (setup.model_dir / "model.safetensors").write_bytes(b"changed during load")
        return setup.model

    monkeypatch.setattr(sys.modules["sentence_transformers"], "SentenceTransformer", altered_loader)
    with pytest.raises(FeatureContractError, match="local_embedding_model_changed"):
        setup.module.LocalSentenceTransformer(setup.config())


def test_normalized_cache_payload_is_revalidated_independently(setup: SimpleNamespace) -> None:
    import hashlib

    adapter = setup.module.LocalSentenceTransformer(setup.config())
    adapter.encode(["first"], role="query")
    # checksum만 맞아도 non-unit 벡터는 정상적인 캐시가 아니다.
    blob = np.array([2, 0], dtype="<f4").tobytes()
    with sqlite3.connect(setup.root / "cache" / "embeddings.sqlite3") as conn:
        conn.execute("UPDATE embeddings SET vector = ?, sha256 = ?", (blob, hashlib.sha256(blob).hexdigest()))
    with pytest.raises(FeatureContractError, match="local_embedding_cache_invalid"):
        adapter.encode(["first"], role="query")


def test_batch_settings_prompt_and_float32_contract(setup: SimpleNamespace) -> None:
    adapter = setup.module.LocalSentenceTransformer(setup.config(batch_size=3))
    vectors = adapter.encode(["first"], role="query")
    assert vectors.dtype == np.float32
    assert setup.model.calls[0][1] == {
        "batch_size": 3, "show_progress_bar": False, "convert_to_numpy": True,
        "normalize_embeddings": False, "device": "cuda", "precision": "float32", "prompt": "",
    }
    with pytest.raises(ValidationError):
        setup.config().device = "cpu"
