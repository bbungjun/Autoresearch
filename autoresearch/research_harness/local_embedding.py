"""로컬 실험의 텍스트에서 정규화된 dense 피처 벡터를 생성한다.

[파이프라인] 안전한 metadata와 로컬 학습 피처 조립 사이의 실제 임베딩 실행 구간이다.
[기능] 준비된 safetensors 모델을 오프라인으로 로드하고 설정·파일 출처별 캐시를 검증한다.
[비책임] 모델 다운로드와 환경 준비는 호출자, 피처 조립은 local_features,
평가·판정은 Sealed Judge가 담당한다. 원격 API와 자동 장치 fallback은 제공하지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing
from copy import deepcopy
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import sqlite3
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from autoresearch.feature_engineering.model_contract import FeatureContractError


class LocalEmbeddingConfig(BaseModel):
    """모델 실행과 캐시를 결정하는 불변 설정; 다운로드는 하지 않는다."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    model_id: str = Field(min_length=1, pattern=r"\S")
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    model_dir: Path
    cache_dir: Path
    device: Literal["cpu", "cuda"] = "cuda"
    batch_size: int = Field(default=32, gt=0)
    max_seq_length: int = Field(default=512, gt=0)
    query_prefix: str = "query: "
    document_prefix: str = "passage: "
    preprocessing: Literal["identity", "strip"] = "identity"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode("ascii")).hexdigest()


def _model_files(root: Path) -> list[dict[str, object]]:
    """다운로더 bookkeeping을 제외한 모델 파일의 상대 경로와 내용 해시를 묶는다."""
    try:
        if not root.is_dir() or root.is_symlink():
            raise ValueError
        entries: list[dict[str, object]] = []
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if relative.parts[0] == ".cache":
                continue
            if path.is_symlink():
                raise ValueError
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError
            # ST 하위 모듈은 loader 옵션과 별개로 legacy weight를 로드할 수 있다.
            # MVP는 변환하지 않고 거부하며 tokenizer의 SentencePiece .model은 허용한다.
            if path.suffix.lower() in {".bin", ".pt", ".pth", ".pkl", ".pickle"}:
                raise ValueError
            hasher = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(block)
                    size += len(block)
            entries.append({"path": relative.as_posix(), "sha256": hasher.hexdigest(), "size_bytes": size})
        names = {entry["path"] for entry in entries}
        if "config.json" not in names or not any(str(name).endswith(".safetensors") for name in names):
            raise ValueError
        return entries
    except (OSError, ValueError):
        raise FeatureContractError("local_embedding_model_invalid") from None


class LocalSentenceTransformer:
    """실제 SentenceTransformer 추론과 텍스트별 검증된 SQLite 캐시.

    Args:
        config: 고정 revision, 준비된 모델 파일, 장치와 처리 설정.

    Raises:
        FeatureContractError: 모델/의존성 부재, GPU 불가, OOM, 추론 또는 캐시 오류.
            외부 라이브러리의 경로·텍스트를 포함할 수 있는 메시지는 전파하지 않는다.

    Notes:
        constructor에서 모델을 로드한다. stats의 hit/miss는 호출 내 중복을 제거한
        원문별 횟수이고 inference_calls는 adapter의 모델 encode 호출 횟수다.
        인스턴스는 단일 호출 흐름용이며 병렬 추론 scheduling을 제공하지 않는다.
    """

    def __init__(self, config: LocalEmbeddingConfig) -> None:
        if not isinstance(config, LocalEmbeddingConfig):
            raise FeatureContractError("local_embedding_config_invalid")
        self._config = config
        try:
            if config.cache_dir.resolve().is_relative_to(config.model_dir.resolve()):
                raise FeatureContractError("local_embedding_cache_overlaps_model")
        except OSError:
            raise FeatureContractError("local_embedding_config_invalid") from None
        files = _model_files(config.model_dir)
        try:
            import torch
            from sentence_transformers import SentenceTransformer

            libraries = {name: version(name) for name in (
                "numpy", "torch", "sentence-transformers", "transformers", "tokenizers", "safetensors",
            )}
        except (ImportError, PackageNotFoundError):
            raise FeatureContractError("local_embedding_dependencies_missing") from None
        self._torch = torch
        if config.device == "cuda" and not torch.cuda.is_available():
            raise FeatureContractError("local_embedding_cuda_unavailable")
        try:
            self._model = SentenceTransformer(
                str(config.model_dir), device=config.device, local_files_only=True,
                trust_remote_code=False, token=False, model_kwargs={"use_safetensors": True},
            )
            self._model.float()
            self._model.eval()
            self._model.max_seq_length = config.max_seq_length
            dimension = self._model.get_embedding_dimension()
        except torch.cuda.OutOfMemoryError:
            raise FeatureContractError("local_embedding_oom") from None
        except Exception:
            # 외부 모델 loader 경계: 원본 예외는 로컬 경로/자격 증명을 담을 수 있다.
            raise FeatureContractError("local_embedding_execution_failed") from None
        if type(dimension) is not int or dimension <= 0:
            raise FeatureContractError("embedding_output_invalid")
        self._dimension = dimension
        if files != _model_files(config.model_dir):
            raise FeatureContractError("local_embedding_model_changed")
        self._manifest: dict[str, object] = {
            "schema_version": "local-embedding-v1", "model_id": config.model_id,
            "revision": config.revision, "model_files": files, "libraries": libraries,
            "query_prefix": config.query_prefix, "document_prefix": config.document_prefix,
            "preprocessing": config.preprocessing, "max_seq_length": config.max_seq_length,
            "normalization": "l2-float64-rescale-to-float32-v1", "dtype": "float32",
            "device": config.device, "batch_size": config.batch_size, "dimension": dimension,
        }
        self._identity = _digest(self._manifest)
        self._stats = {"cache_hits": 0, "cache_misses": 0, "inference_calls": 0}
        self._cache_path = config.cache_dir / "embeddings.sqlite3"
        try:
            config.cache_dir.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self._cache_path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS embeddings "
                    "(key TEXT PRIMARY KEY, dimension INTEGER NOT NULL, "
                    "vector BLOB NOT NULL, sha256 TEXT NOT NULL)"
                )
        except (OSError, sqlite3.Error):
            raise FeatureContractError("local_embedding_cache_invalid") from None

    @property
    def identity(self) -> str:
        """로컬 절대 경로를 제외한 모델 실행 identity."""
        return self._identity

    @property
    def dimension(self) -> int:
        """query/document가 공유하는 출력 차원."""
        return self._dimension

    @property
    def manifest(self) -> dict[str, object]:
        """모델/실행 출처의 독립 복사본; 반환값 변경은 adapter에 영향을 주지 않는다."""
        return deepcopy(self._manifest)

    @property
    def stats(self) -> dict[str, int]:
        """이 인스턴스의 캐시와 추론 누적 계수 복사본."""
        return self._stats.copy()

    def _read_vector(self, row: tuple[object, ...]) -> np.ndarray:
        dimension, blob, checksum = row
        if (dimension != self.dimension or not isinstance(blob, bytes)
                or len(blob) != self.dimension * 4 or hashlib.sha256(blob).hexdigest() != checksum):
            raise FeatureContractError("local_embedding_cache_invalid")
        vector = np.frombuffer(blob, dtype="<f4").copy()
        if not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1.0, atol=1e-5, rtol=0):
            raise FeatureContractError("local_embedding_cache_invalid")
        return vector

    def _infer(self, texts: list[str], role: Literal["query", "document"]) -> np.ndarray:
        prefix = self._config.query_prefix if role == "query" else self._config.document_prefix
        prepared = [prefix + (text.strip() if self._config.preprocessing == "strip" else text) for text in texts]
        self._stats["inference_calls"] += 1
        try:
            with self._torch.inference_mode():
                raw = self._model.encode(
                    prepared, batch_size=self._config.batch_size, show_progress_bar=False,
                    convert_to_numpy=True, normalize_embeddings=False, device=self._config.device,
                    precision="float32", prompt="",
                )
        except self._torch.cuda.OutOfMemoryError:
            raise FeatureContractError("local_embedding_oom") from None
        except Exception:
            # 외부 추론 경계: 원문 입력이나 모델 파일 경로를 오류로 공개하지 않는다.
            raise FeatureContractError("local_embedding_execution_failed") from None
        if (not isinstance(raw, np.ndarray) or raw.shape != (len(texts), self.dimension)
                or raw.dtype.kind != "f" or not np.isfinite(raw).all()):
            raise FeatureContractError("embedding_output_invalid")
        vectors = raw.astype(np.float64, copy=True)
        scales = np.max(np.abs(vectors), axis=1, keepdims=True)
        if np.any(scales == 0) or not np.isfinite(vectors).all():
            raise FeatureContractError("embedding_output_invalid")
        vectors /= scales
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors.astype("<f4")

    def encode(self, texts: Sequence[str], *, role: Literal["query", "document"]) -> np.ndarray:
        """원문·역할별 캐시를 사용하여 입력 순서의 float32 단위 벡터를 반환한다.

        빈 입력은 모델 차원을 유지한 (0, dimension) 배열이다. 손상된 캐시는 실패하며
        자동 재계산하지 않는다. 실제 추론 배치는 설정 batch_size를 사용한다.
        """
        if (isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence)
                or any(not isinstance(text, str) or not text.strip() for text in texts)
                or role not in ("query", "document")):
            raise FeatureContractError("embedding_input_invalid")
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        unique = list(dict.fromkeys(texts))
        keys = {text: _digest([self.identity, role, text]) for text in unique}
        results: dict[str, np.ndarray] = {}
        missing: list[str] = []
        try:
            with closing(sqlite3.connect(self._cache_path)) as connection, connection:
                for text in unique:
                    row = connection.execute(
                        "SELECT dimension, vector, sha256 FROM embeddings WHERE key = ?", (keys[text],),
                    ).fetchone()
                    if row is None:
                        missing.append(text)
                    else:
                        results[text] = self._read_vector(row)
                self._stats["cache_hits"] += len(results)
                self._stats["cache_misses"] += len(missing)
                if missing:
                    vectors = self._infer(missing, role)
                    for text, vector in zip(missing, vectors, strict=True):
                        blob = vector.tobytes()
                        connection.execute(
                            "INSERT OR IGNORE INTO embeddings VALUES (?, ?, ?, ?)",
                            (keys[text], self.dimension, blob, hashlib.sha256(blob).hexdigest()),
                        )
                        # 동시 작성자가 먼저 게시한 경우에도 저장된 벡터를 검증하고 반환한다.
                        row = connection.execute(
                            "SELECT dimension, vector, sha256 FROM embeddings WHERE key = ?", (keys[text],),
                        ).fetchone()
                        results[text] = self._read_vector(row)
        except (OSError, sqlite3.Error):
            raise FeatureContractError("local_embedding_cache_invalid") from None
        return np.stack([results[text] for text in texts])
