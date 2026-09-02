"""후보의 안전한 로컬 입력을 재학습 CLI 산출물로 연결한다.

[파이프라인] candidate workspace 준비 뒤, LocalRunner의 학습·예측 실행 구간이다.
[기능] 로컬 설정과 입력 검증 후 임베딩/학습을 호출하고 native 모델·receipt·CSV를 게시한다.
[비책임] 피처/학습 계산은 local_training, 모델 준비는 실행자, 지표와 판정은 Sealed Judge다.
운영 MLflow 등록·원격 데이터 조회·평가 정답 소비를 수행하지 않는다.
"""

from __future__ import annotations

from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
from time import perf_counter

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from autoresearch.feature_engineering.model_contract import FeatureContractError
from autoresearch.research_harness.local_embedding import LocalEmbeddingConfig, LocalSentenceTransformer
from autoresearch.research_harness.local_training import (
    LocalTrainingConfig, load_local_training_input, train_local_candidate,
)
from autoresearch.research_harness.prediction_parser import (
    MAX_IDENTIFIER_BYTES, MAX_PREDICTION_ROWS, PREDICTION_HEADER, is_canonical_ascii,
)


class HarnessPredictConfig(BaseModel):
    """로컬 실행 설정. 경로는 설정 파일 디렉터리를 기준으로 해석한다."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    embedding: LocalEmbeddingConfig
    training: LocalTrainingConfig = Field(default_factory=LocalTrainingConfig)


def _load_config(path: Path) -> HarnessPredictConfig:
    try:
        with path.open("rb") as stream:
            payload = stream.read(64 * 1024 + 1)
        if len(payload) > 64 * 1024:
            raise ValueError
        config = HarnessPredictConfig.model_validate_json(payload)
        model = config.embedding.model_dir
        cache = config.embedding.cache_dir
        embedding = config.embedding.model_copy(update={
            "model_dir": (model if model.is_absolute() else path.parent / model).absolute(),
            "cache_dir": (cache if cache.is_absolute() else path.parent / cache).absolute(),
        })
        return config.model_copy(update={"embedding": embedding})
    except (OSError, ValueError, ValidationError):
        raise FeatureContractError("harness_prediction_config_invalid") from None


def _output_paths(out: Path, input_root: Path) -> tuple[Path, Path, Path]:
    try:
        paths = (out.with_suffix(".model.txt"), out.with_suffix(".training.json"), out)
        if (len(set(paths)) != 3 or any(os.path.lexists(path) for path in paths)
                or out.parent.resolve() != out.parent.absolute()
                or out.resolve().is_relative_to(input_root.resolve())):
            raise ValueError
    except (OSError, ValueError, RuntimeError):
        raise FeatureContractError("harness_prediction_output_invalid") from None
    return paths


def _prediction_bytes(table: pa.Table) -> bytes:
    if (not isinstance(table, pa.Table)
            or table.column_names != ["evaluation_id", "slate_id", "video_id", "score"]
            or not 0 < len(table) <= MAX_PREDICTION_ROWS):
        raise FeatureContractError("harness_prediction_output_invalid")
    rows = [PREDICTION_HEADER + b"\n"]
    try:
        for row in table.to_pylist():
            evaluation = row["evaluation_id"]
            if not isinstance(evaluation, str) or re.fullmatch(r"eval_[0-9a-f]{64}", evaluation) is None:
                raise ValueError
            identifiers = [row["slate_id"], row["video_id"]]
            for value in identifiers:
                if not isinstance(value, str):
                    raise ValueError
                encoded = value.encode("ascii")
                if not 0 < len(encoded) <= MAX_IDENTIFIER_BYTES or b"," in encoded or not is_canonical_ascii(encoded):
                    raise ValueError
            score = float(row["score"])
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError
            rows.append(f"{evaluation},{identifiers[0]},{identifiers[1]},{score:.17g}\n".encode("ascii"))
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError):
        raise FeatureContractError("harness_prediction_output_invalid") from None
    return b"".join(rows)


def run_harness_prediction(*, slate: Path, out: Path, seed: int, config_path: Path) -> None:
    """검증→실제 모델 적재→새 CTR fit→모델/receipt/CSV 순서로 실행한다.

    기존 산출물은 덮어쓰지 않는다. 게시 실패 시 일부 모델/receipt 파일은 남을 수 있으나
    성공으로 반환하지 않으며 CSV를 마지막에 쓴다. 재시도는 새 출력 경로를 사용한다.
    오류에는 로컬 경로·입력 데이터·라이브러리의 원본 예외 메시지를 포함하지 않는다.
    """
    started = perf_counter()
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise FeatureContractError("harness_prediction_config_invalid")
    config = _load_config(config_path.absolute())
    paths = _output_paths(out.absolute(), slate.parent.absolute())
    try:
        cache_in_input = config.embedding.cache_dir.resolve().is_relative_to(slate.parent.resolve())
        output_in_model = out.resolve().is_relative_to(config.embedding.model_dir.resolve())
    except (OSError, ValueError, RuntimeError):
        raise FeatureContractError("harness_prediction_config_invalid") from None
    if cache_in_input:
        raise FeatureContractError("harness_prediction_config_invalid")
    if output_in_model:
        raise FeatureContractError("harness_prediction_output_invalid")
    inputs = load_local_training_input(slate)
    embedding = LocalSentenceTransformer(config.embedding)
    result = train_local_candidate(inputs, seed=seed, embedding=embedding, config=config.training)
    prediction_bytes = _prediction_bytes(result.predictions)
    try:
        model_bytes = result.model_text.encode("utf-8")
        if not model_bytes:
            raise ValueError
        receipt = {
            **result.receipt,
            "model_text_sha256": sha256(model_bytes).hexdigest(),
            "prediction_sha256": sha256(prediction_bytes).hexdigest(),
            "embedding_identity": embedding.identity, "embedding_manifest": embedding.manifest,
            "embedding_stats": embedding.stats,
            "training_duration_seconds": result.receipt.get("duration_seconds"),
            "duration_seconds": perf_counter() - started,
            "timing_scope": "prediction_call_before_publication",
        }
        receipt_bytes = (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2,
                                    allow_nan=False) + "\n").encode("utf-8")
        paths[0].parent.mkdir(parents=True, exist_ok=True)
        for path, payload in zip(paths, (model_bytes, receipt_bytes, prediction_bytes), strict=True):
            with path.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
    except (OSError, TypeError, ValueError, UnicodeError):
        raise FeatureContractError("harness_prediction_output_invalid") from None
