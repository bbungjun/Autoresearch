"""#111 고정 신규 cohort의 raw 생성과 snapshot 봉인을 실행한다.

[파이프라인] 학습 입력 준비 후 실제 비교 실험 전에 Judge 평가 입력을 준비한다.
[기능] 등록된 bundle/기존 결과 hash를 검증하고 코드·정책을 먼저 기록한 뒤
3개 cohort를 날짜순 생성·봉인하고 validation candidate view를 게시한다.
[비책임] 모델 fit·예측·지표·final 소비·서빙은 호출하지 않는다.
"""

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from time import perf_counter

from autoresearch.research_harness.behavior_evaluation import (
    BehaviorEvaluationRequest, BehaviorEvaluationSource, COHORT_SEEDS, CONTRACT_HASH,
    evaluation_policy, generate_behavior_evaluation, prepare_behavior_metadata, seal_behavior_evaluation,
)
from autoresearch.research_harness.behavior_training import load_behavior_training
from autoresearch.research_harness.candidate_data_view import materialize_candidate_data_view_v2
from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes
from autoresearch.research_harness.fixture_models import CandidateDataViewRequest
from autoresearch.research_harness.local_training import _read_regular


BUNDLE_HASHES = {
    10701: "a4bf85660b7f9aa7992f9f1019b9829104435e1953ccd17c1e0399e5be444eec",
    10702: "aa3e610f2c52187ac2f66adcbc981d5365e5d6e89c6362790df3259aff843e7f",
    10703: "fdf1b35245c2ec8fdc4cf2afa21f8d0825ed8b63c3094b2fb7f737fe2e52ef94",
}
PREVIOUS_HASHES = {
    "e504042bded46fa385b6164c3d45136f041a15cbe6d8b9f896965feefc24d7cc",
    "b70172bbcebe0cd7dfc47d243e0741b8e7f48def3a86f53c765f9d2fd648f5f5",
    "ac12d96ddff7b711e6b83b225118e0435a2299ac41e8656242fd11f766b6cff3",
}


def prepare(output: Path, training: Path, previous: list[Path]) -> dict[str, object]:
    """고정 원본과 과거 identity를 확인한 뒤 새 출력만 준비한다."""
    repository = Path(__file__).resolve().parents[1]
    if subprocess.run(["git", "status", "--porcelain"], cwd=repository, check=True,
                      capture_output=True, text=True).stdout.strip():
        raise ValueError("preparation_requires_clean_checkout")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True,
                            capture_output=True, text=True).stdout.strip()
    for path in [training, *[p.parent for p in previous]]:
        if output.resolve().is_relative_to(path.resolve()) or path.resolve().is_relative_to(output.resolve()):
            raise ValueError("source_output_overlap")
    contract = _read_regular(training / "comparison-contract.md")
    if sha256(contract).hexdigest() != CONTRACT_HASH:
        raise ValueError("comparison_contract_mismatch")
    old_bytes = [_read_regular(p) for p in previous]
    if len(old_bytes) != 3 or {sha256(b).hexdigest() for b in old_bytes} != PREVIOUS_HASHES:
        raise ValueError("previous_results_mismatch")
    forbidden = {str(w[k]) for payload in old_bytes for w in json.loads(payload)["worlds"]
                 for k in ("snapshot_fingerprint", "validation_evaluation_id", "final_evaluation_id")}
    training_users = set()
    for seed, digest in BUNDLE_HASHES.items():
        bundle = load_behavior_training(training / f"world-{seed}", expected_manifest_sha256=digest)
        training_users.update(bundle.labels["user_id"].to_pylist())
    # 전체 개발 cohort도 비교한다. 학습일 미접속 사용자를 새 사용자로 세지 않는다.
    from autoresearch.research_harness.fixture_inputs import select_fixture_user_ids
    for seed in BUNDLE_HASHES:
        left, right = select_fixture_user_ids(seed)
        training_users.update(left + right)
    output.mkdir(parents=True, exist_ok=False)
    policy = canonical_json_bytes(evaluation_policy())
    policy_path = output / "policy.json"
    policy_path.write_bytes(policy)
    policy_hash = sha256(policy).hexdigest()
    (output / "comparison-contract.md").write_bytes(contract)
    preflight = {"source_commit": commit, "policy_sha256": policy_hash,
                 "tool_sha256": sha256(_read_regular(Path(__file__))).hexdigest(),
                 "comparison_contract_sha256": CONTRACT_HASH, "training_bundles": BUNDLE_HASHES,
                 "previous_results_sha256": sorted(PREVIOUS_HASHES),
                 "forbidden_evaluation_identities": sorted(forbidden)}
    (output / "preflight.json").write_bytes(canonical_json_bytes(preflight))
    started = perf_counter()
    worlds = []
    for seed in COHORT_SEEDS:
        if perf_counter() - started >= 1800:
            raise TimeoutError("evaluation_preparation_budget_exceeded")
        root = output / f"world-{seed}"
        raw = root / "judge-owned/raw"
        print(f"GENERATE cohort={seed} dates=2026-08-03..2026-09-05", flush=True)
        generated = generate_behavior_evaluation(raw, BehaviorEvaluationRequest(seed),
                        policy_path=policy_path, expected_policy_sha256=policy_hash)
        cohort_users = set(generated["validation_users"] + generated["reserved_final_users"])
        if cohort_users & training_users:
            raise ValueError("evaluation_training_users_overlap")
        training_users.update(cohort_users)
        digest = sha256(_read_regular(raw / "manifest.json")).hexdigest()
        source = BehaviorEvaluationSource(raw, expected_manifest_sha256=digest)
        handoff = seal_behavior_evaluation(source, root / "judge-owned/state")
        ids = {str(handoff.snapshot_fingerprint), str(handoff.validation_id), str(handoff.final_holdout_id)}
        if ids & forbidden:
            raise ValueError("evaluation_identity_reused")
        forbidden.update(ids)
        metadata = prepare_behavior_metadata(source, handoff)
        destination = root / "candidate-validation"
        destination.mkdir()
        view = materialize_candidate_data_view_v2(CandidateDataViewRequest(handoff, destination),
                                                  source=source, metadata=metadata)
        receipt = {"cohort_seed": seed, "training_seed_world": seed - 200, "raw_manifest_sha256": digest,
                   "policy_sha256": policy_hash, "snapshot_fingerprint": str(handoff.snapshot_fingerprint),
                   "snapshot_manifest_sha256": handoff.manifest_sha256,
                   "validation_evaluation_id": str(handoff.validation_id),
                   "final_evaluation_id": str(handoff.final_holdout_id),
                   "snapshot_relative_path": handoff.snapshot_root.relative_to(root).as_posix(),
                   "candidate_manifest_sha256": view.manifest_sha256,
                   "validation_slate_rows": view.manifest.slate.rows,
                   "history_partitions": len(view.manifest.history_partitions),
                   "raw_event_rows": sum(p["events"]["rows"] for p in generated["partitions"]),
                   "metadata": {"users": asdict(metadata.users.receipt), "videos": asdict(metadata.videos.receipt)},
                   "fit_calls": 0, "evaluation_calls": 0, "final_claims": 0}
        (root / "receipt.json").write_bytes(canonical_json_bytes(receipt))
        worlds.append(receipt)
        print(f"SEALED cohort={seed} validation_rows={view.manifest.slate.rows}", flush=True)
    if [_read_regular(p) for p in previous] != old_bytes:
        raise ValueError("previous_results_changed")
    for seed, digest in BUNDLE_HASHES.items():
        load_behavior_training(training / f"world-{seed}", expected_manifest_sha256=digest)
    summary = {**preflight, "worlds": worlds, "elapsed_seconds": perf_counter() - started,
               "fit_calls": 0, "evaluation_calls": 0, "final_claims": 0}
    (output / "summary.json").write_bytes(canonical_json_bytes(summary))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--training", type=Path, required=True)
    parser.add_argument("--previous-result", type=Path, action="append", required=True)
    args = parser.parse_args()
    prepare(args.output.absolute(), args.training.absolute(), [p.absolute() for p in args.previous_result])


if __name__ == "__main__":
    main()
