"""#109 실제 E5 학습 bundle 조립 도구.

[파이프라인] #107 raw 데이터와 후속 ablation 실행 사이의 준비 단계다.
[기능] 고정 원본 hash/seed에서 15/10 피처와 split을 생성하고 재로딩을 검증한다.
[비책임] 모델 fit·예측·평가·final claim은 호출하지 않는다.
"""

import argparse
from hashlib import sha256
from pathlib import Path
import subprocess
from time import perf_counter

from autoresearch.research_harness.behavior_training import prepare_behavior_training, load_behavior_training
from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes
from autoresearch.research_harness.local_embedding import LocalEmbeddingConfig, LocalSentenceTransformer


SOURCES = {
    10701: "33f83133f73fc51ee09892d16e40555c2ced926f90ec0931a362fa011236bc79",
    10702: "3ffc3ed5cd51084832aa4f11cc04009354c1c43273e3e6e2ad08482da69f9e14",
    10703: "feb099b82b4a3962cbf36e4af80d3868a13e5e2ffcdd5519642c531e562866fc",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    if subprocess.run(["git", "status", "--porcelain"], cwd=repository, check=True, capture_output=True, text=True).stdout.strip():
        raise ValueError("preparation_requires_clean_checkout")
    source_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()
    contract = (repository / "docs/specs/2026-09-05-diverse-behavior-ablation.md").read_bytes()
    if args.output.resolve().is_relative_to(args.source.resolve()) or args.source.resolve().is_relative_to(args.output.resolve()):
        raise ValueError("source_output_overlap")
    for seed, expected in SOURCES.items():
        if sha256((args.source / f"world-{seed}" / "manifest.json").read_bytes()).hexdigest() != expected:
            raise ValueError("source_manifest_mismatch")
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "comparison-contract.md").write_bytes(contract)
    started = perf_counter()
    embedding = LocalSentenceTransformer(LocalEmbeddingConfig(
        model_id="intfloat/multilingual-e5-small", revision="614241f622f53c4eeff9890bdc4f31cfecc418b3",
        model_dir=args.model_dir, cache_dir=args.cache_dir, device="cuda", batch_size=8,
    ))
    embedding_manifest = {"identity": embedding.identity, "dimension": embedding.dimension, "manifest": embedding.manifest}
    results = []
    for seed, expected in SOURCES.items():
        source, destination = args.source / f"world-{seed}", args.output / f"world-{seed}"
        receipt = prepare_behavior_training(source, destination, expected_source_sha256=expected,
                                             embedding=embedding, embedding_manifest=embedding_manifest)
        digest = sha256((destination / "bundle.json").read_bytes()).hexdigest()
        loaded = load_behavior_training(destination, expected_manifest_sha256=digest)
        results.append({"seed": seed, "bundle_sha256": digest, "rows": len(loaded.labels),
                        "positives": receipt["training_positive_rows"], "reloaded": True})
        print(f"seed={seed} rows={len(loaded.labels)} positives={receipt['training_positive_rows']} prepared", flush=True)
    summary = {"worlds": results, "elapsed_seconds": perf_counter() - started,
               "source_commit": source_commit, "comparison_contract_sha256": sha256(contract).hexdigest(),
               "embedding": embedding_manifest, "embedding_stats": embedding.stats,
               "fit_calls": 0, "evaluation_calls": 0, "final_claims": 0}
    (args.output / "summary.json").write_bytes(canonical_json_bytes(summary))


if __name__ == "__main__":
    main()
