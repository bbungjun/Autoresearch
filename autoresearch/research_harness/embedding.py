"""로컬 실험 피처와 텍스트 임베딩 실행 사이의 최소 경계.

[파이프라인] 안전한 metadata에서 학습 피처를 조립하는 구간의 벡터 계산 계약이다.
[기능] query/document 역할을 보존하고 출력의 행·차원·유한성을 검사해 L2 정규화한다.
[비책임] 모델 다운로드·로딩·장치·캐시는 구체 adapter, 피처 계산은 local_features가 맡는다.
"""

from collections.abc import Sequence
from typing import Literal, Protocol

import numpy as np

from autoresearch.feature_engineering.model_contract import FeatureContractError


class TextEmbedder(Protocol):
    """같은 설정에서 역할 간 호환되는 벡터를 입력 순서대로 반환한다."""

    def encode(self, texts: Sequence[str], *, role: Literal["query", "document"]) -> np.ndarray:
        ...


def encode_normalized(
    embedding: TextEmbedder, texts: Sequence[str], *, role: Literal["query", "document"],
    expected_dimension: int | None = None,
) -> np.ndarray:
    """비어 있지 않은 배치를 검증하고 overflow 없이 float64 단위 벡터로 바꾼다.

    Args:
        embedding: 텍스트 순서를 보존하는 adapter.
        texts: 개별 keyword 또는 category 설명문.
        role: query 또는 document.
        expected_dimension: 같은 모델의 다른 역할 배치에서 이미 관측한 차원.

    Returns:
        행마다 L2 norm이 1인 2차원 float64 배열. 원본 배열을 수정하지 않는다.

    Raises:
        FeatureContractError: 입력 또는 adapter 출력이 계약에 맞지 않는 경우.
    """
    if (
        isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence) or not texts
        or any(not isinstance(text, str) or not text.strip() for text in texts)
        or role not in ("query", "document")
        or (expected_dimension is not None and (type(expected_dimension) is not int or expected_dimension <= 0))
    ):
        raise FeatureContractError("embedding_input_invalid")
    raw = embedding.encode(texts, role=role)
    if (
        not isinstance(raw, np.ndarray) or raw.ndim != 2 or raw.shape[0] != len(texts)
        or raw.shape[1] == 0 or raw.dtype.kind != "f"
        or (expected_dimension is not None and raw.shape[1] != expected_dimension)
        or not np.isfinite(raw).all()
    ):
        raise FeatureContractError("embedding_output_invalid")
    vectors = raw.astype(np.float64, copy=True)
    scales = np.max(np.abs(vectors), axis=1, keepdims=True)
    if not np.isfinite(vectors).all() or np.any(scales == 0):
        raise FeatureContractError("embedding_vector_invalid")
    vectors /= scales
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors
