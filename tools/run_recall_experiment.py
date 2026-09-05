"""#117 통제 실험의 준비·학습·개발 선택·신규 봉인 평가 구간.

[파이프라인] 고정 행동 bundle 이후 저장 모델의 오프라인 비교를 실행한다.
[기능] 단계별 write-once receipt, 전체 계산/fit 예산, 선택 선행 봉인과 신규
final 단일 소비를 연결한다. 기존 final은 무결성 hash 외에는 사용하지 않는다.
[비책임] 모델/피처 계산은 recall_experiment, 지표는 recall_analysis가 맡는다.
서빙·production 승격·유료 API 및 OS 수준 보안 격리는 제공하지 않는다.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from typing import Callable

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.research_harness.behavior_evaluation import (
    BehaviorEvaluationRequest, BehaviorEvaluationSource, evaluation_policy,
    generate_behavior_evaluation, prepare_behavior_metadata, seal_behavior_evaluation,
)
from autoresearch.research_harness.behavior_training import load_behavior_training
from autoresearch.research_harness.candidate_data_view import materialize_final_candidate_data_view
from autoresearch.research_harness.candidate_metadata import normalize_video_metadata
from autoresearch.research_harness.consumption_registry import (
    FinalConsumptionRequest, claim_final_consumption,
)
from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes
from autoresearch.research_harness.fixture_models import CandidateDataViewRequest
from autoresearch.research_harness.local_evaluation_fixture import _validated_judge_snapshot
from autoresearch.research_harness.local_training import load_local_training_input
from autoresearch.research_harness.recall_experiment import (
    ARMS, TRAINING_SEEDS, preference_features, predict_model, train_model,
)
from tools.prepare_behavior_evaluation import BUNDLE_HASHES
from tools.recall_analysis import score_predictions, select_candidate, summarize
from tools.run_diverse_behavior import EVALUATION_SUMMARY_HASH, prediction_features, supervise


MAX_FITS = 72
MAX_SECONDS = 7200
PHASE_SECONDS = {"prepare": 900, "models": 1800, "development": 600,
                 "fresh": 2400, "final": 900, "audit": 600}
SPEC = "docs/archive/specs/2026-09-06-recall-controlled-experiment.md"
BASE_SHA = "f9bdba9d9b0c7576d48c46368504d70f8233eaab"
NEW_COHORTS = (11701, 11702, 11703)
CACHED_RECEIPTS = {
    10901: "d0341022bde8c63d7e355381970958263a1fcfbc56ea485e9861abdb560ea400",
    10902: "3dee3c7f86bdd203d049ca515f84ef03430735e6fe73b12a91424e80071d592e",
    10903: "ad223bf9708a1f34f89e83a9c42dfbc78efa156de0c06a30bc1cd51647a204e7",
}


def digest(path: Path) -> str:
    """파일 bytes의 SHA256을 계산한다."""
    return sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    """완료/시도 증거를 기존 파일을 덮어쓰지 않고 게시한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(canonical_json_bytes(value))


def tree_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): digest(p) for p in sorted(root.rglob("*")) if p.is_file()}


def code_hashes(repository: Path) -> dict[str, str]:
    paths = [p for folder in ("autoresearch", "tools") for p in (repository / folder).rglob("*.py")]
    paths += [repository / SPEC, repository / "pyproject.toml", repository / "uv.lock"]
    return {p.relative_to(repository).as_posix(): digest(p) for p in sorted(paths)}


def read_verified(root: Path, receipt: dict) -> pa.Table:
    path = root / receipt["path"]
    if digest(path) != receipt["sha256"]:
        raise ValueError("input_receipt_hash_mismatch")
    frame = pq.ParquetFile(path).read()
    if len(frame) != receipt["rows"]:
        raise ValueError("input_receipt_rows_mismatch")
    return frame


class Budget:
    """단일 직렬 실행의 합산 시간을 단계와 fit 시도에 결속한다."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.started = perf_counter()
        self.fit_attempts = 0
        self.costs: dict[str, float] = {}

    def check(self) -> None:
        if perf_counter() - self.started >= MAX_SECONDS:
            raise TimeoutError("total_compute_budget_exceeded")

    def before_fit(self, kind: str) -> None:
        self.check()
        if self.fit_attempts >= MAX_FITS:
            raise ValueError("fit_budget_exceeded")
        self.fit_attempts += 1
        write_json(self.root / "fit-attempts" / f"{self.fit_attempts:03d}.json",
                   {"kind": kind, "attempt": self.fit_attempts,
                    "elapsed_seconds": perf_counter() - self.started, "paid_api_calls": 0})

    def phase(self, name: str, action: Callable[[], object]) -> object:
        self.check()
        start = perf_counter()
        write_json(self.root / "stages" / f"{name}-attempt.json",
                   {"stage": name, "started_at": datetime.now(UTC).isoformat(),
                    "max_seconds": PHASE_SECONDS[name]})
        try:
            result = action()
        except Exception as error:
            self.costs[name] = perf_counter() - start
            write_json(self.root / "stages" / f"{name}-failed.json", {
                "elapsed_seconds": self.costs[name], "fit_attempts": self.fit_attempts,
                "error_type": type(error).__name__, "paid_api_calls": 0})
            raise
        elapsed = perf_counter() - start
        self.costs[name] = elapsed
        self.check()
        if elapsed > PHASE_SECONDS[name]:
            raise TimeoutError(f"phase_budget_exceeded:{name}")
        write_json(self.root / "stages" / f"{name}-complete.json",
                   {"elapsed_seconds": elapsed, "fit_attempts": self.fit_attempts,
                    "paid_api_calls": 0, "result": result})
        print(f"COMPLETE stage={name} seconds={elapsed:.3f} fits={self.fit_attempts}", flush=True)
        return result


class Experiment:
    """원본 입력을 보존하고 신규 출력에만 실험 증거를 저장한다."""

    def __init__(self, args: argparse.Namespace, repository: Path, commit: str) -> None:
        self.args, self.repository, self.commit = args, repository, commit
        self.output: Path = args.output
        self.budget = Budget(self.output)
        self.prepared: dict[int, dict] = {}
        self.models: dict[tuple[int, int, str], str] = {}
        self.new_worlds: dict[int, tuple] = {}
        self.selection: dict = {}
        self.development: list[dict] = []
        self.final: list[dict] = []
        self.protected: dict[str, dict[str, str]] = {}
        self.old_users: set[str] = set()
        self.old_identities: set[str] = set()
        self.final_claims = 0
        self.selection_hash: str | None = None

    def verify_selection(self) -> None:
        path = self.output / "selection.json"
        if (self.selection_hash is None or digest(path) != self.selection_hash
                or path.read_bytes() != canonical_json_bytes(self.selection)
                or self.selection["code"] != code_hashes(self.repository)
                or self.selection["models"] != tree_hashes(self.output / "models")):
            raise ValueError("selection_seal_changed")

    def prepare(self) -> dict:
        args = self.args
        for name in ("training", "raw_training", "evaluation", "previous_run"):
            root = getattr(args, name)
            self.protected[name] = tree_hashes(root)
        write_json(self.output / "protected-inputs.json", self.protected)
        if digest(args.evaluation / "summary.json") != EVALUATION_SUMMARY_HASH:
            raise ValueError("old_evaluation_summary_mismatch")
        summary = json.loads((args.evaluation / "summary.json").read_bytes())
        from autoresearch.research_harness.fixture_inputs import select_fixture_user_ids
        for world, bundle_hash in BUNDLE_HASHES.items():
            root = self.output / "prepared" / str(world)
            root.mkdir(parents=True)
            bundle = load_behavior_training(args.training / f"world-{world}", expected_manifest_sha256=bundle_hash)
            self.old_users.update(sum(select_fixture_user_ids(world), ()))
            source = args.raw_training / f"world-{world}"
            raw = json.loads((source / "manifest.json").read_bytes())
            if digest(source / "manifest.json") != bundle.manifest["source_manifest_sha256"]:
                raise ValueError("training_raw_manifest_mismatch")
            history = pa.concat_tables([read_verified(source, p["events"]) for p in raw["partitions"]])
            videos = normalize_video_metadata(pa.concat_tables([read_verified(source, p["videos"]) for p in raw["partitions"]]))
            preference = preference_features(bundle.labels, history, videos)
            frame = bundle.features["with_recent"]
            for column in preference.column_names:
                frame = frame.append_column(column, preference[column])
            pq.write_table(bundle.labels, root / "train-labels.parquet")
            pq.write_table(frame, root / "train-features.parquet")
            saved = next(w for w in summary["worlds"] if w["training_seed_world"] == world)
            cohort = saved["cohort_seed"]
            self.old_identities.update(str(saved[k]) for k in (
                "snapshot_fingerprint", "validation_evaluation_id", "final_evaluation_id"))
            old_root = args.evaluation / f"world-{cohort}"
            if digest(old_root / "judge-owned/raw/manifest.json") != saved["raw_manifest_sha256"]:
                raise ValueError("old_raw_manifest_mismatch")
            old_raw = json.loads((old_root / "judge-owned/raw/manifest.json").read_bytes())
            self.old_users.update(old_raw["validation_users"] + old_raw["reserved_final_users"])
            snapshot = old_root / saved["snapshot_relative_path"]
            handoff, manifest = _validated_judge_snapshot(snapshot, expected_fingerprint=saved["snapshot_fingerprint"])
            if handoff.manifest_sha256 != saved["snapshot_manifest_sha256"]:
                raise ValueError("old_snapshot_manifest_mismatch")
            # 기존 final의 정답/예측은 열지 않고 validation만 사용한다.
            label_receipt = manifest.validation.artifacts.labels
            labels = read_verified(snapshot, {"path": label_receipt.relative_path,
                                   "sha256": label_receipt.sha256, "rows": label_receipt.rows})
            candidate_slate = old_root / "candidate-validation/harness_in/slate.parquet"
            inputs = load_local_training_input(candidate_slate)
            if inputs.manifest_sha256 != saved["candidate_manifest_sha256"]:
                raise ValueError("old_candidate_manifest_mismatch")
            cached = args.previous_run / f"world-{cohort}/validation/input"
            if digest(cached / "receipt.json") != CACHED_RECEIPTS[cohort]:
                raise ValueError("cached_validation_receipt_unpinned")
            receipt = json.loads((cached / "receipt.json").read_bytes())
            if (digest(cached / "features.parquet") != receipt["features_sha256"]
                    or digest(cached / "keys.parquet") != receipt["keys_sha256"]
                    or receipt["candidate_manifest_sha256"] != saved["candidate_manifest_sha256"]
                    or receipt["embedding_identity"] != bundle.manifest["embedding"]["identity"]):
                raise ValueError("cached_validation_features_mismatch")
            keys, features = pq.read_table(cached / "keys.parquet"), pq.read_table(cached / "features.parquet")
            if not keys.equals(inputs.slate.select(keys.column_names)):
                raise ValueError("cached_validation_order_mismatch")
            if set(inputs.slate["user_id"].to_pylist()) & set(bundle.labels["user_id"].to_pylist()):
                raise ValueError("training_validation_users_overlap")
            additional = preference_features(inputs.slate, inputs.history, inputs.videos)
            for column in additional.column_names:
                features = features.append_column(column, additional[column])
            for name, table in (("validation-labels", labels), ("validation-keys", keys), ("validation-features", features)):
                pq.write_table(table, root / f"{name}.parquet")
            self.prepared[world] = {"bundle_hash": bundle_hash, "embedding": bundle.manifest["embedding"],
                                    "hashes": tree_hashes(root)}
        return self.prepared

    def fit(self) -> dict:
        for world in BUNDLE_HASHES:
            root = self.output / "prepared" / str(world)
            labels, features = pq.read_table(root / "train-labels.parquet"), pq.read_table(root / "train-features.parquet")
            for seed in TRAINING_SEEDS:
                for arm in ARMS:
                    target = self.output / "models" / str(world) / str(seed) / arm
                    train_model(labels, features, target, seed=seed, arm=arm,
                                input_hashes=self.prepared[world]["hashes"], before_fit=self.budget.before_fit)
                    self.models[world, seed, arm] = digest(target / "receipt.json")
        if self.budget.fit_attempts != MAX_FITS:
            raise ValueError("unexpected_fit_count")
        receipts = {f"{w}/{s}/{a}": h for (w, s, a), h in self.models.items()}
        write_json(self.output / "models-sealed.json", receipts)
        return {"fit_attempts": self.budget.fit_attempts, "models": receipts}

    def predict_bundle(self, world: int, split: str, keys: pa.Table, features: pa.Table,
                       arms: tuple[str, ...]) -> list[tuple[int, str, Path]]:
        root = self.output / "predictions" / split / str(world)
        paths = []
        for seed in TRAINING_SEEDS:
            for arm in arms:
                self.budget.check()
                raw, probability = predict_model(self.output / "models" / str(world) / str(seed) / arm,
                                features, expected_receipt_sha256=self.models[world, seed, arm])
                target = root / str(seed) / arm
                target.mkdir(parents=True, exist_ok=False)
                table = keys.append_column("raw_score", pa.array(raw)).append_column("probability", pa.array(probability))
                pq.write_table(table, target / "prediction.parquet")
                paths.append((seed, arm, target))
        write_json(root / "predictions-sealed.json", {
            "row_keys_sha256": sha256(canonical_json_bytes(keys.to_pylist())).hexdigest(),
            "models_sealed_sha256": digest(self.output / "models-sealed.json"),
            "predictions": [{"seed": s, "arm": a, "sha256": digest(p / "prediction.parquet")}
                            for s, a, p in paths]})
        return paths

    def score_bundle(self, world: int, keys: pa.Table, labels: pa.Table,
                     paths: list[tuple[int, str, Path]]) -> list[dict]:
        observations = []
        if not paths:
            raise ValueError("empty_prediction_bundle")
        sealed = json.loads((paths[0][2].parents[1] / "predictions-sealed.json").read_bytes())
        expected = {(row["seed"], row["arm"]): row["sha256"] for row in sealed["predictions"]}
        if (len(expected) != len(paths) or set(expected) != {(s, a) for s, a, _ in paths}
                or sealed["row_keys_sha256"] != sha256(canonical_json_bytes(keys.to_pylist())).hexdigest()
                or sealed["models_sealed_sha256"] != digest(self.output / "models-sealed.json")):
            raise ValueError("prediction_bundle_seal_invalid")
        for seed, arm, path in paths:
            self.budget.check()
            if digest(path / "prediction.parquet") != expected[seed, arm]:
                raise ValueError("sealed_prediction_changed")
            table = pq.read_table(path / "prediction.parquet")
            metrics = score_predictions(table.select(keys.column_names), labels,
                          table["raw_score"].to_numpy(), table["probability"].to_numpy())
            write_json(path / "metrics.json", metrics)
            observations.append({"world": world, "seed": seed, "arm": arm, "metrics": metrics,
                                 "row_keys_sha256": sha256(canonical_json_bytes(keys.to_pylist())).hexdigest()})
        return observations

    def develop(self) -> dict:
        for world in BUNDLE_HASHES:
            root = self.output / "prepared" / str(world)
            keys, features, labels = [pq.read_table(root / f"validation-{name}.parquet") for name in ("keys", "features", "labels")]
            paths = self.predict_bundle(world, "development", keys, features, ARMS)
            self.development.extend(self.score_bundle(world, keys, labels, paths))
        summary = summarize(self.development, arms=list(ARMS))
        write_json(self.output / "development.json", {"observations": self.development, "summary": summary})
        self.selection = {**select_candidate(summary), "code": code_hashes(self.repository),
                          "commit": self.commit, "models": tree_hashes(self.output / "models"),
                          "prepared_inputs": self.prepared,
                          "criterion": "recall_delta_0.005_pairs4_worlds2_all_guardrails_nonnegative",
                          "new_cohorts": list(NEW_COHORTS), "paid_api_calls": 0}
        write_json(self.output / "selection.json", self.selection)
        self.selection_hash = digest(self.output / "selection.json")
        return {"summary": summary, "selection_sha256": digest(self.output / "selection.json")}

    def fresh(self) -> dict:
        if self.selection["selected_arm"] is None:
            return {"performed": False, "reason": "development_uninformative"}
        self.verify_selection()
        requests = tuple(BehaviorEvaluationRequest(seed, validation_users=200, final_users=800) for seed in NEW_COHORTS)
        policy_path = self.output / "fresh-policy.json"
        write_json(policy_path, evaluation_policy(requests))
        receipts = {}
        for world, request in zip(BUNDLE_HASHES, requests, strict=True):
            self.budget.check()
            root = self.output / "fresh" / str(world)
            raw = root / "judge-owned/raw"
            manifest = generate_behavior_evaluation(raw, request, policy_path=policy_path, expected_policy_sha256=digest(policy_path))
            users = set(manifest["validation_users"] + manifest["reserved_final_users"])
            if users & self.old_users:
                raise ValueError("new_cohort_users_reused")
            self.old_users.update(users)
            source = BehaviorEvaluationSource(raw, expected_manifest_sha256=digest(raw / "manifest.json"))
            handoff = seal_behavior_evaluation(source, root / "judge-owned/state")
            identities = {str(handoff.snapshot_fingerprint), str(handoff.validation_id), str(handoff.final_holdout_id)}
            if identities & self.old_identities:
                raise ValueError("new_evaluation_identity_reused")
            self.old_identities.update(identities)
            self.new_worlds[world] = (source, handoff)
            receipts[str(world)] = {"cohort_seed": request.seed, "raw_manifest_sha256": source.digest,
                "snapshot_fingerprint": str(handoff.snapshot_fingerprint), "manifest_sha256": handoff.manifest_sha256,
                "final_id": str(handoff.final_holdout_id), "validation_id": str(handoff.validation_id),
                "selection_sha256": digest(self.output / "selection.json"), "files": tree_hashes(root)}
            write_json(root / "receipt.json", receipts[str(world)])
        return receipts

    def evaluate_final(self) -> dict:
        if self.selection["selected_arm"] is None:
            return {"performed": False, "verdict": "uninformative"}
        self.verify_selection()
        from autoresearch.research_harness.local_embedding import LocalEmbeddingConfig, LocalSentenceTransformer
        embedding = LocalSentenceTransformer(LocalEmbeddingConfig(
            model_id="intfloat/multilingual-e5-small", revision="614241f622f53c4eeff9890bdc4f31cfecc418b3",
            model_dir=self.args.model_dir, cache_dir=self.args.cache_dir, device="cuda", batch_size=8))
        if any(embedding.identity != p["embedding"]["identity"] for p in self.prepared.values()):
            raise ValueError("actual_embedding_identity_changed")
        if self.selection["models"] != tree_hashes(self.output / "models"):
            raise ValueError("selected_models_changed")
        arms = ("baseline15", "reference10", self.selection["selected_arm"])
        for world, (source, handoff) in self.new_worlds.items():
            self.budget.check()
            state = handoff.snapshot_root.parents[2]
            (state / "final-holdout-consumed").mkdir(exist_ok=True)
            grant = claim_final_consumption(FinalConsumptionRequest(state, handoff, BASE_SHA, self.commit, datetime.now(UTC)))
            self.final_claims += 1
            root = self.output / "final-inputs" / str(world)
            root.mkdir(parents=True)
            write_json(root / "claim.json", {"marker_sha256": grant.evidence.marker_sha256,
                       "evaluation_id": str(handoff.final_holdout_id), "selection_sha256": digest(self.output / "selection.json")})
            metadata = prepare_behavior_metadata(source, handoff, final=True)
            destination = root / "candidate"
            destination.mkdir()
            view = materialize_final_candidate_data_view(CandidateDataViewRequest(handoff, destination),
                                                          source=source, metadata=metadata, grant=grant)
            slate = view.root / "slate.parquet"
            keys, features, _ = prediction_features(slate, view.manifest_sha256, embedding, root / "base-features")
            inputs = load_local_training_input(slate)
            additional = preference_features(inputs.slate, inputs.history, inputs.videos)
            for column in additional.column_names:
                features = features.append_column(column, additional[column])
            pq.write_table(features, root / "all-features.parquet")
            paths = self.predict_bundle(world, "final", keys, features, arms)
            if not grant._authorizes(handoff):
                raise ValueError("final_grant_invalid")
            _, manifest = _validated_judge_snapshot(handoff.snapshot_root, expected_fingerprint=str(handoff.snapshot_fingerprint))
            receipt = manifest.final_holdout.artifacts.labels
            labels = read_verified(handoff.snapshot_root, {"path": receipt.relative_path, "sha256": receipt.sha256, "rows": receipt.rows})
            self.final.extend(self.score_bundle(world, keys, labels, paths))
        summary = summarize(self.final, arms=list(arms))
        passed = summary["coverage_valid"] and summary["comparisons"][self.selection["selected_arm"]]["passed"]
        verdict = ("uninformative" if not summary["coverage_valid"] else
                   "supported" if self.selection["development_eligible"] and passed else "not_supported")
        write_json(self.output / "final.json", {"observations": self.final, "summary": summary,
                   "verdict": verdict, "final_claims": self.final_claims, "embedding_stats": embedding.stats})
        return {"summary": summary, "verdict": verdict, "final_claims": self.final_claims}

    def audit(self) -> dict:
        for name, hashes in self.protected.items():
            if tree_hashes(getattr(self.args, name)) != hashes:
                raise ValueError("protected_old_inputs_changed")
        if code_hashes(self.repository) != self.selection["code"]:
            raise ValueError("experiment_code_changed")
        return {"protected_files": sum(map(len, self.protected.values())), "unchanged": True,
                "artifact_hashes": tree_hashes(self.output), "fit_attempts": self.budget.fit_attempts}


def run(args: argparse.Namespace) -> None:
    repository = Path(__file__).resolve().parents[1]
    if sum(PHASE_SECONDS.values()) > MAX_SECONDS or 6 * 3 * 2 * 2 != MAX_FITS or MAX_FITS > 90:
        raise ValueError("preflight_budget_grid_invalid")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repository, check=True, capture_output=True, text=True).stdout
    if status.strip():
        raise ValueError("experiment_requires_clean_checkout")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()
    for name in ("training", "raw_training", "evaluation", "previous_run", "model_dir", "cache_dir"):
        other = getattr(args, name).resolve()
        if args.output.resolve().is_relative_to(other) or other.is_relative_to(args.output.resolve()):
            raise ValueError("input_output_overlap")
    common = Path(subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                  cwd=repository, check=True, capture_output=True, text=True).stdout.strip())
    write_json(common / "research-experiment-claims/issue-117-recall.json", {"commit": commit, "output": str(args.output)})
    args.output.mkdir(parents=True, exist_ok=False)
    experiment = Experiment(args, repository, commit)
    write_json(args.output / "preflight.json", {"commit": commit, "code": code_hashes(repository),
        "fit_cap": MAX_FITS, "requested_fit_cap": 90, "seconds_cap": MAX_SECONDS, "phase_seconds": PHASE_SECONDS,
        "paid_api_calls": 0, "os_sandbox": False})
    try:
        for name, action in (("prepare", experiment.prepare), ("models", experiment.fit),
                             ("development", experiment.develop), ("fresh", experiment.fresh),
                             ("final", experiment.evaluate_final), ("audit", experiment.audit)):
            experiment.budget.phase(name, action)
        final_done = (args.output / "final.json").exists() and experiment.final_claims == 3
        result = {"completed": final_done, "protocol_terminated": True,
                  "final_performed": final_done,
                  "verdict": json.loads((args.output / "final.json").read_bytes())["verdict"] if final_done else "uninformative",
                  "unperformed": [] if final_done else ["fresh_final_evaluation"],
                  "fit_attempts": experiment.budget.fit_attempts,
                  "final_claims": experiment.final_claims, "costs": experiment.budget.costs,
                  "elapsed_seconds": perf_counter() - experiment.budget.started, "paid_api_calls": 0,
                  "selection_sha256": digest(args.output / "selection.json"),
                  "final_sha256": digest(args.output / "final.json") if (args.output / "final.json").exists() else None}
        write_json(args.output / "result.json", result)
    except Exception as error:
        write_json(args.output / "failure.json", {"error_type": type(error).__name__, "reason": str(error),
            "fit_attempts": experiment.budget.fit_attempts, "final_claims": experiment.final_claims,
            "elapsed_seconds": perf_counter() - experiment.budget.started, "costs": experiment.budget.costs,
            "paid_api_calls": 0, "retry_allowed": False})
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "training", "raw-training", "evaluation", "previous-run", "model-dir", "cache-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        run(args)
    else:
        raise SystemExit(supervise([sys.executable, "-m", "tools.run_recall_experiment", *sys.argv[1:], "--worker"],
                                   args.output, timeout=MAX_SECONDS))


if __name__ == "__main__":
    main()
