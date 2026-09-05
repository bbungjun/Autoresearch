"""#113 고정 행동 bundle과 봉인 snapshot의 실제 ablation 실행기.

[파이프라인] #109 학습 입력 및 #111 평가 준비 이후 fit·예측·Judge 채점을 연결한다.
[기능] 18모델 선행 봉인, 전체 validation 유효성 gate, 3개 final 단일 소비,
6개 예측 묶음 선행 봉인과 저장 모델 재사용, 실행 중단/실측 receipt를 제공한다.
[비책임] 데이터 생성·기준 변경·재시도·production 승격·serving은 수행하지 않는다.
OS sandbox가 없는 신뢰 코드 실행이며 감독 프로세스가 30분 상한을 적용한다.
"""

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from time import perf_counter

import pyarrow as pa

from autoresearch.research_harness.behavior_evaluation import (
    BehaviorEvaluationSource, COHORT_SEEDS, CONTRACT_HASH, prepare_behavior_metadata,
)
from autoresearch.research_harness.behavior_execution import predict_behavior_model, train_behavior_model
from autoresearch.research_harness.behavior_training import ARMS, TRAINING_SEEDS, arm_columns, load_behavior_training
from autoresearch.research_harness.candidate_data_view import materialize_final_candidate_data_view
from autoresearch.research_harness.consumption_registry import (
    FinalConsumptionGrant, FinalConsumptionRequest, claim_final_consumption,
)
from autoresearch.research_harness.domain import YouTubeCTRDomain
from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes, _write_table
from autoresearch.research_harness.fixture_models import CandidateDataViewRequest, JudgeSnapshotHandoff
from autoresearch.research_harness.local_embedding import LocalEmbeddingConfig, LocalSentenceTransformer
from autoresearch.research_harness.local_evaluation_fixture import _validated_judge_snapshot
from autoresearch.research_harness.local_features import build_local_features
from autoresearch.research_harness.local_training import _read_regular, load_local_training_input
from autoresearch.research_harness.personalization_ablation import scoring_result_dict
from autoresearch.research_harness.prediction import _prediction_bytes
from tools.diverse_behavior_analysis import summarize, validation_gate
from tools.prepare_behavior_evaluation import BUNDLE_HASHES
from tools.run_popularity_recall import _atomic_write, _second_claim_fails_closed, _write_json


EVALUATION_SUMMARY_HASH = "459c938eaf3a0d00b56fe46327a023745acddaae06e6afb904358f103b2c7d89"
BASELINE_SHA = "91ae50a7b014a4bd3b7973ace56fa32d20482902"
MAX_SECONDS = 1800


@dataclass(frozen=True)
class World:
    seed: int
    training_root: Path
    training_hash: str
    source: BehaviorEvaluationSource
    handoff: JudgeSnapshotHandoff
    validation_slate: Path
    validation_manifest_hash: str


def _hash(path: Path) -> str:
    return sha256(_read_regular(path)).hexdigest()


def _disjoint(output: Path, *inputs: Path) -> None:
    for root in inputs:
        if output.resolve().is_relative_to(root.resolve()) or root.resolve().is_relative_to(output.resolve()):
            raise ValueError("experiment_input_output_overlap")


def load_worlds(training: Path, evaluation: Path) -> tuple[list[World], dict]:
    """고정 summary/bundle과 모든 평가 원본 파일을 읽기 검증한다."""
    payload = _read_regular(evaluation / "summary.json")
    if sha256(payload).hexdigest() != EVALUATION_SUMMARY_HASH:
        raise ValueError("evaluation_summary_hash_mismatch")
    summary = json.loads(payload)
    for root in (training, evaluation):
        if _hash(root / "comparison-contract.md") != CONTRACT_HASH:
            raise ValueError("comparison_contract_hash_mismatch")
    if _hash(evaluation / "policy.json") != summary["policy_sha256"]:
        raise ValueError("evaluation_policy_hash_mismatch")
    if tuple(w["cohort_seed"] for w in summary["worlds"]) != COHORT_SEEDS:
        raise ValueError("evaluation_worlds_mismatch")
    worlds = []
    for saved in summary["worlds"]:
        seed = saved["cohort_seed"]
        training_seed = saved["training_seed_world"]
        if training_seed != seed - 200:
            raise ValueError("training_world_mapping_mismatch")
        training_root = training / f"world-{training_seed}"
        bundle = load_behavior_training(training_root, expected_manifest_sha256=BUNDLE_HASHES[training_seed])
        for indexes in bundle.splits.values():
            for name in set(arm_columns("with_recent")) - set(arm_columns("without_recent")):
                values = bundle.features["with_recent"][name].take(pa.array(indexes["train"]))
                if len(set(values.to_pylist())) < 2:
                    raise ValueError("recent_training_feature_constant")
        root = evaluation / f"world-{seed}"
        source = BehaviorEvaluationSource(root / "judge-owned/raw", expected_manifest_sha256=saved["raw_manifest_sha256"])
        for day, partition in source.partitions.items():
            with source.open_partition(day) as stream:
                stream.read()
            source.read_metadata(partition["videos"], f"inputs/youtube_trending_kr/dt={day}/part-0.parquet")
        source.read_metadata(source.manifest["users"], "inputs/virtual_users.parquet")
        expected_snapshot = root / "judge-owned/state/evaluation-snapshots/by-hash" / saved["snapshot_fingerprint"]
        if saved["snapshot_relative_path"] != expected_snapshot.relative_to(root).as_posix():
            raise ValueError("snapshot_path_mismatch")
        handoff, manifest = _validated_judge_snapshot(expected_snapshot,
                                                     expected_fingerprint=saved["snapshot_fingerprint"])
        if (handoff.manifest_sha256 != saved["snapshot_manifest_sha256"]
                or str(handoff.validation_id) != saved["validation_evaluation_id"]
                or str(handoff.final_holdout_id) != saved["final_evaluation_id"]
                or manifest.source.root != source.opaque_root):
            raise ValueError("snapshot_identity_mismatch")
        for part in manifest.source.partitions:
            raw = source.partitions[part.dt]["events"]
            if (part.uri, part.rows, part.sha256) != (source.partition_uri(part.dt), raw["rows"], raw["sha256"]):
                raise ValueError("snapshot_raw_receipt_mismatch")
        slate = root / "candidate-validation/harness_in/slate.parquet"
        if _hash(slate.parent / "candidate-view.json") != saved["candidate_manifest_sha256"]:
            raise ValueError("candidate_manifest_hash_mismatch")
        marker = handoff.snapshot_root.parents[2] / "final-holdout-consumed" / str(handoff.final_holdout_id)
        if os.path.lexists(marker):
            raise ValueError("final_already_consumed")
        worlds.append(World(seed, training_root, BUNDLE_HASHES[training_seed], source, handoff,
                            slate, saved["candidate_manifest_sha256"]))
    return worlds, summary


def claim_run(common_git: Path, output: Path, commit: str) -> None:
    """출력/worktree 이름을 바꿔도 동일 평가 묶음의 재실행을 거절한다."""
    root = common_git / "research-experiment-claims"
    root.mkdir(exist_ok=True)
    path = root / f"behavior-ablation-{EVALUATION_SUMMARY_HASH}.json"
    with path.open("xb") as stream:
        stream.write(canonical_json_bytes({"issue": 113, "evaluation_summary_sha256": EVALUATION_SUMMARY_HASH,
                                          "commit": commit, "output": str(output)}))
        stream.flush()
        os.fsync(stream.fileno())


def prediction_features(slate: Path, expected_manifest: str, embedding: LocalSentenceTransformer,
                        output: Path) -> tuple[pa.Table, pa.Table, str]:
    """검증된 candidate 파일만 읽고 동일 요청 순서의 15열 batch를 조립한다."""
    if _hash(slate.parent / "candidate-view.json") != expected_manifest:
        raise ValueError("prediction_candidate_manifest_mismatch")
    inputs = load_local_training_input(slate)
    batch = build_local_features(inputs.slate, history=inputs.history, users=inputs.users, videos=inputs.videos,
                                 embedding=embedding, evaluation_start_date=inputs.manifest.evaluation_start_date,
                                 history_start_date=inputs.manifest.history_partitions[0].dt)
    for name in ("user_metadata_missing", "video_metadata_missing"):
        if any(batch.diagnostics[name].to_pylist()):
            raise ValueError("prediction_metadata_missing")
    for name in ("history_7d_complete", "history_30d_complete"):
        if not all(batch.diagnostics[name].to_pylist()):
            raise ValueError("prediction_history_incomplete")
    features = batch.features.select(arm_columns("with_recent"))
    keys = inputs.slate.select(["evaluation_id", "slate_id", "video_id"])
    key_hash = sha256(canonical_json_bytes(keys.to_pylist())).hexdigest()
    output.mkdir(parents=True)
    _write_table(features, output / "features.parquet")
    _write_table(keys, output / "keys.parquet")
    _write_json(output / "receipt.json", {"rows": len(keys), "row_keys_sha256": key_hash,
                "features_sha256": _hash(output / "features.parquet"), "keys_sha256": _hash(output / "keys.parquet"),
                "candidate_manifest_sha256": expected_manifest, "embedding_identity": embedding.identity})
    return keys, features, key_hash


class Experiment:
    """새 출력에 단계별 결과를 기록하는 한 번의 고정 실행."""

    def __init__(self, output: Path, commit: str) -> None:
        self.output = output
        self.commit = commit
        self.started = perf_counter()
        self.state = {"stage": "initializing", "fit_attempts": 0, "fit_completed": 0,
                      "evaluation_attempts": 0, "evaluation_completed": 0, "final_claims": 0}
        self.observations: list[dict] = []
        self.models: dict[tuple[int, int, str], str] = {}
        self.domain = YouTubeCTRDomain()

    def progress(self, stage: str) -> None:
        if perf_counter() - self.started >= MAX_SECONDS:
            raise TimeoutError("experiment_time_budget_exceeded")
        self.state["stage"] = stage
        temporary = self.output / ".progress.tmp"
        temporary.write_bytes(canonical_json_bytes(self.state))
        temporary.replace(self.output / "progress.json")

    def model_root(self, world: int, seed: int, arm: str) -> Path:
        return self.output / f"world-{world}/models/{arm}/{seed}"

    def fit_all(self, worlds: list[World]) -> None:
        for world in worlds:
            for seed in TRAINING_SEEDS:
                pair = []
                for arm in ARMS:
                    if self.state["fit_attempts"] >= 18:
                        raise ValueError("fit_budget_exceeded")
                    self.state["fit_attempts"] += 1
                    self.progress(f"fit:{world.seed}:{seed}:{arm}")
                    root = self.model_root(world.seed, seed, arm)
                    receipt = train_behavior_model(world.training_root, root, expected_bundle_sha256=world.training_hash,
                                                   seed=seed, arm=arm)
                    pair.append(receipt["split_receipts"])
                    self.models[world.seed, seed, arm] = _hash(root / "receipt.json")
                    self.state["fit_completed"] += 1
                if pair[0] != pair[1]:
                    raise ValueError("paired_fit_split_mismatch")
            print(f"FIT world={world.seed} completed={self.state['fit_completed']}/18", flush=True)
        _write_json(self.output / "models-sealed.json", {
            "models": [{"world": w, "seed": s, "arm": a, "receipt_sha256": h}
                       for (w, s, a), h in self.models.items()], "fit_calls": self.state["fit_completed"],
        })

    def score_split(self, world: World, split: str, slate: Path, manifest_hash: str,
                    embedding: LocalSentenceTransformer, grant: FinalConsumptionGrant | None) -> None:
        self.progress(f"features:{world.seed}:{split}")
        root = self.output / f"world-{world.seed}/{split}"
        keys, features, key_hash = prediction_features(slate, manifest_hash, embedding, root / "input")
        sealed = []
        # 6개 예측을 모두 봉인한 뒤 채점을 시작한다. 이 단계에서는 모델을 fit하지 않는다.
        for seed in TRAINING_SEEDS:
            for arm in ARMS:
                self.progress(f"predict:{world.seed}:{split}:{seed}:{arm}")
                scores = predict_behavior_model(self.model_root(world.seed, seed, arm), features.select(arm_columns(arm)),
                          expected_receipt_sha256=self.models[world.seed, seed, arm], embedding_identity=embedding.identity)
                path = root / arm / str(seed)
                path.mkdir(parents=True)
                prediction = path / "prediction.csv"
                _atomic_write(prediction, _prediction_bytes(keys.append_column("score", pa.array(scores))))
                receipt = self.domain.validate_candidate(prediction, path / "judge-copy.csv")
                sealed.append((seed, arm, path, receipt))
        bundle = {"split": split, "world": world.seed, "row_keys_sha256": key_hash,
                  "models_sealed_sha256": _hash(self.output / "models-sealed.json"),
                  "predictions": [{"seed": s, "arm": a, "prediction_sha256": _hash(p / "prediction.csv"),
                                   "judge_copy_sha256": _hash(p / "judge-copy.csv"),
                                   "model_receipt_sha256": self.models[world.seed, s, a]} for s, a, p, _ in sealed]}
        _write_json(root / "predictions-sealed.json", bundle)
        for seed, arm, path, receipt in sealed:
            if self.state["evaluation_attempts"] >= 36:
                raise ValueError("evaluation_budget_exceeded")
            self.state["evaluation_attempts"] += 1
            self.progress(f"score:{world.seed}:{split}:{seed}:{arm}")
            metrics = scoring_result_dict(self.domain.evaluate(world.handoff, receipt, final_grant=grant))
            _write_json(path / "metrics.json", metrics)
            observation = {"world_seed": world.seed, "split": split, "training_seed": seed, "arm": arm,
                           "metrics": metrics, "row_keys_sha256": key_hash}
            self.observations.append(observation)
            self.state["evaluation_completed"] += 1
        print(f"SCORED world={world.seed} split={split} completed={self.state['evaluation_completed']}/36", flush=True)

    def execute(self, worlds: list[World], embedding: LocalSentenceTransformer) -> dict:
        self.fit_all(worlds)
        for world in worlds:
            self.score_split(world, "validation", world.validation_slate, world.validation_manifest_hash, embedding, None)
        self.progress("validation_gate")
        valid = validation_gate(self.observations)
        _write_json(self.output / "validation-gate.json", {"valid": valid, "observations": self.observations})
        if not valid:
            return {"verdict": "uninformative", "reason": "validation_validity_failed", "coverage_valid": False}
        for world in worlds:
            self.progress(f"claim_final:{world.seed}")
            if self.state["final_claims"] >= 3:
                raise ValueError("final_budget_exceeded")
            state = world.handoff.snapshot_root.parents[2]
            (state / "final-holdout-consumed").mkdir(exist_ok=True)
            request = FinalConsumptionRequest(state, world.handoff, BASELINE_SHA, self.commit, datetime.now(UTC))
            grant = claim_final_consumption(request)
            self.state["final_claims"] += 1
            if not _second_claim_fails_closed(request):
                raise ValueError("final_second_claim_not_rejected")
            root = self.output / f"world-{world.seed}"
            _write_json(root / "final-claim.json", {"evaluation_id": str(world.handoff.final_holdout_id),
                       "marker_sha256": grant.evidence.marker_sha256, "second_claim_fail_closed": True,
                       "models_sealed_sha256": _hash(self.output / "models-sealed.json")})
            destination = root / "candidate-final"
            destination.mkdir()
            metadata = prepare_behavior_metadata(world.source, world.handoff, final=True)
            view = materialize_final_candidate_data_view(CandidateDataViewRequest(world.handoff, destination),
                                                         source=world.source, metadata=metadata, grant=grant)
            self.score_split(world, "final_holdout", view.root / "slate.parquet", view.manifest_sha256, embedding, grant)
        return summarize(self.observations)


def run(output: Path, training: Path, evaluation: Path, model_dir: Path, cache_dir: Path) -> None:
    """검증된 고정 묶음의 한 번 실행을 수행하고 성공/실패 receipt를 남긴다."""
    repository = Path(__file__).resolve().parents[1]
    _disjoint(output, training, evaluation, model_dir, cache_dir)
    if subprocess.run(["git", "status", "--porcelain"], cwd=repository, check=True,
                      capture_output=True, text=True).stdout.strip():
        raise ValueError("experiment_requires_clean_checkout")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True,
                            capture_output=True, text=True).stdout.strip()
    worlds, prepared = load_worlds(training, evaluation)
    common_git = Path(subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                     cwd=repository, check=True, capture_output=True, text=True).stdout.strip())
    output.mkdir(parents=True, exist_ok=False)
    _write_json(output / "preflight.json", {"issue": 113, "commit": commit,
                "evaluation_summary_sha256": EVALUATION_SUMMARY_HASH, "comparison_contract_sha256": CONTRACT_HASH,
                "max_fit_calls": 18, "max_seconds": MAX_SECONDS, "max_final_claims": 3,
                "os_sandbox": False, "paid_api_calls": 0})
    experiment = Experiment(output, commit)
    try:
        embedding = LocalSentenceTransformer(LocalEmbeddingConfig(
            model_id="intfloat/multilingual-e5-small", revision="614241f622f53c4eeff9890bdc4f31cfecc418b3",
            model_dir=model_dir, cache_dir=cache_dir, device="cuda", batch_size=8,
        ))
        for world in worlds:
            manifest = json.loads(_read_regular(world.training_root / "bundle.json"))
            if embedding.identity != manifest["embedding"]["identity"]:
                raise ValueError("actual_e5_identity_mismatch")
        claim_run(common_git, output, commit)
        summary = experiment.execute(worlds, embedding)
        experiment.progress("complete")
        # 원본/정책/model 파일은 재생성하지 않는다. final marker만 원래 registry에 추가된다.
        if _hash(evaluation / "summary.json") != EVALUATION_SUMMARY_HASH:
            raise ValueError("prepared_summary_changed")
        for world in worlds:
            load_behavior_training(world.training_root, expected_manifest_sha256=world.training_hash)
        result = {"version": "diverse-behavior-ablation-v1", "commit": commit,
                  "prepared_evaluation_summary_sha256": EVALUATION_SUMMARY_HASH,
                  "comparison_contract_sha256": CONTRACT_HASH, "worlds": prepared["worlds"],
                  "counts": experiment.state, "observations": experiment.observations, "summary": summary,
                  "embedding_identity": embedding.identity, "embedding_stats": embedding.stats,
                  "elapsed_seconds": perf_counter() - experiment.started, "os_sandbox": False, "paid_api_calls": 0}
        _write_json(output / "result.json", result)
        _atomic_write(output / "result.sha256", (_hash(output / "result.json") + "  result.json\n").encode())
        print(f"COMPLETE verdict={summary['verdict']} counts={experiment.state}", flush=True)
    except Exception as error:
        _write_json(output / "failure.json", {"error_type": type(error).__name__, "counts": experiment.state,
                    "observations": experiment.observations, "elapsed_seconds": perf_counter() - experiment.started,
                    "retry_allowed": False})
        raise


def supervise(command: list[str], output: Path, *, timeout: float = MAX_SECONDS) -> int:
    """실행 프로세스 트리를 제한 시간에 회수하고 중단 기록을 보존한다."""
    process = subprocess.Popen(command, start_new_session=os.name != "nt",
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            if process.poll() is None:
                process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        output.mkdir(parents=True, exist_ok=True)
        _write_json(output / "timeout.json", {"reason": "hard_time_limit", "seconds": timeout, "retry_allowed": False})
        return 124


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "training", "evaluation", "model-dir", "cache-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        run(args.output.absolute(), args.training.absolute(), args.evaluation.absolute(),
            args.model_dir.absolute(), args.cache_dir.absolute())
    else:
        raise SystemExit(supervise([sys.executable, "-m", "tools.run_diverse_behavior", *sys.argv[1:], "--worker"],
                                   args.output.absolute()))


if __name__ == "__main__":
    main()
