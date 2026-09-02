"""교체 가능한 임베딩 경계의 수치·차원 검증."""

from importlib import import_module
from importlib.util import find_spec

import numpy as np
import pytest

from autoresearch.feature_engineering.model_contract import FeatureContractError


class FixedEmbedding:
    def __init__(self, output: object) -> None:
        self.output = output

    def encode(self, texts: list[str], *, role: str) -> np.ndarray:
        return self.output


def encode(output: object, **kwargs: object) -> np.ndarray:
    name = "autoresearch.research_harness.embedding"
    assert find_spec(name), "RED: embedding 구현이 필요합니다"
    return import_module(name).encode_normalized(FixedEmbedding(output), ["text"], role="query", **kwargs)


def test_normalization_is_dimension_agnostic() -> None:
    np.testing.assert_allclose(encode(np.array([[3.0, 4.0]])), [[0.6, 0.8]])
    np.testing.assert_allclose(encode(np.array([[1.0, 2.0, 2.0]])), [[1 / 3, 2 / 3, 2 / 3]])


@pytest.mark.parametrize("output", [
    [[1.0, 0.0]], np.array([1.0, 0.0]), np.empty((1, 0)), np.ones((2, 2)),
    np.array([[0.0, 0.0]]), np.array([[np.nan, 1.0]]), np.array([[np.inf, 1.0]]),
    np.array([["1", "2"]]), np.array([[1 + 1j, 0]]),
])
def test_invalid_embedding_outputs_are_rejected(output: object) -> None:
    with pytest.raises(FeatureContractError):
        encode(output)


def test_cross_role_dimension_mismatch_fails() -> None:
    with pytest.raises(FeatureContractError):
        encode(np.array([[1.0, 2.0]]), expected_dimension=3)


@pytest.mark.parametrize("scale", [1e-300, 1e300])
def test_normalization_handles_large_and_small_finite_vectors(scale: float) -> None:
    raw = np.array([[3 * scale, 4 * scale]])
    before = raw.copy()
    np.testing.assert_allclose(encode(raw), [[0.6, 0.8]])
    np.testing.assert_array_equal(raw, before)


@pytest.mark.parametrize("texts", [[], "text", [""], [" "], [None]])
def test_invalid_text_input_fails_before_adapter(texts: object) -> None:
    name = "autoresearch.research_harness.embedding"
    with pytest.raises(FeatureContractError, match="embedding_input_invalid"):
        import_module(name).encode_normalized(FixedEmbedding(None), texts, role="query")
