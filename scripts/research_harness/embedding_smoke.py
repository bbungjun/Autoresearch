"""로컬 사전학습 모델 준비와 GPU 추론·캐시의 수동 smoke 검증.

[파이프라인] 자율 실험 전 모델 준비/장치 점검 구간이다.
[기능] 명시적 --download에서만 공개 모델을 받고 실행 증거 JSON을 보존한다.
[비책임] 모델 학습·품질 판정·실험 loop는 Harness CLI/Controller가 담당한다.
"""

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
from time import perf_counter

import numpy as np

from autoresearch.feature_engineering.model_contract import FeatureContractError


MODEL_ID = "intfloat/multilingual-e5-small"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"


def main() -> int:
    """명시적 준비 옵션과 분리된 local-only smoke를 실행한다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
    if args.download:
        from huggingface_hub import snapshot_download

        snapshot_download(
            MODEL_ID, revision=REVISION, local_dir=args.model_dir, token=False,
            allow_patterns=[
                "config.json", "model.safetensors", "modules.json", "sentence_bert_config.json",
                "config_sentence_transformers.json", "special_tokens_map.json", "tokenizer.json",
                "tokenizer_config.json", "sentencepiece.bpe.model", "1_Pooling/config.json",
                "2_Normalize/config.json", "LICENSE",
            ],
        )

    import torch

    from autoresearch.research_harness.local_embedding import (
        LocalEmbeddingConfig, LocalSentenceTransformer,
    )

    if not torch.cuda.is_available():
        raise FeatureContractError("embedding_smoke_cuda_unavailable")
    tensor_sum = torch.arange(4, device="cuda").sum().item()
    assert tensor_sum == 6
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    started = perf_counter()
    adapter = LocalSentenceTransformer(LocalEmbeddingConfig(
        model_id=MODEL_ID, revision=REVISION, model_dir=args.model_dir,
        cache_dir=args.cache_dir, device="cuda", batch_size=args.batch_size,
    ))
    torch.cuda.synchronize()
    load_seconds = perf_counter() - started
    queries = ["한국 음악 추천", "파이썬 교육", "한국 음악 추천", "music live concert"]
    documents = ["음악 공연과 라이브 콘서트", "프로그래밍 교육과 파이썬 강의"]
    started = perf_counter()
    query_vectors = adapter.encode(queries, role="query")
    document_vectors = adapter.encode(documents, role="document")
    torch.cuda.synchronize()
    first_seconds = perf_counter() - started
    first_stats = dict(adapter.stats)
    started = perf_counter()
    cached_queries = adapter.encode(queries, role="query")
    cached_documents = adapter.encode(documents, role="document")
    torch.cuda.synchronize()
    cached_seconds = perf_counter() - started
    np.testing.assert_allclose(query_vectors, cached_queries, rtol=1e-6, atol=1e-7)
    np.testing.assert_allclose(document_vectors, cached_documents, rtol=1e-6, atol=1e-7)
    np.testing.assert_allclose(np.linalg.norm(query_vectors, axis=1), 1, atol=1e-6)
    assert adapter.stats["inference_calls"] == first_stats["inference_calls"]
    report = {
        "kind": "local-embedding-smoke-v1", "created_at": datetime.now(UTC).isoformat(),
        "model_identity": adapter.identity, "model_manifest": adapter.manifest,
        "device_name": torch.cuda.get_device_name(), "torch_cuda_version": torch.version.cuda,
        "tensor_sum": tensor_sum, "query_shape": list(query_vectors.shape),
        "document_shape": list(document_vectors.shape), "batch_size": args.batch_size,
        "load_seconds": load_seconds, "first_encode_seconds": first_seconds,
        "cached_encode_seconds": cached_seconds, "first_stats": first_stats,
        "final_stats": dict(adapter.stats),
        "cuda_peak_allocated_bytes": torch.cuda.max_memory_allocated(),
        "cuda_peak_reserved_bytes": torch.cuda.max_memory_reserved(),
        "cache_output_verified": True,
        "quality_claim": "not_measured", "oom_validation": "unit_test_injection_only",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FeatureContractError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, RuntimeError, ValueError, ImportError):
        print("embedding_smoke_failed", file=sys.stderr)
        raise SystemExit(1) from None
