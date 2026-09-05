"""#119의 저장 모델 보정·개발 진단·신규 봉인 평가 실행 구간.

[기능] 입력 pin, 12보정 상한, 모델/보정 선행 봉인, 3개 신규 final 단일 소비,
오류 및 단계 비용 기록을 연결한다. 기존 실험 파일은 해시로 보호한다.
[비책임] 보정/진단/판정은 calibration_study, 생성/평가 계약은 기존 harness가 맡는다.
서빙 배포나 OS sandbox는 제공하지 않는다.
"""

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from scipy.special import expit

from autoresearch.research_harness.behavior_evaluation import (
    BehaviorEvaluationRequest, BehaviorEvaluationSource, evaluation_policy,
    generate_behavior_evaluation, prepare_behavior_metadata, seal_behavior_evaluation,
)
from autoresearch.research_harness.candidate_data_view import materialize_final_candidate_data_view
from autoresearch.research_harness.consumption_registry import FinalConsumptionRequest, claim_final_consumption
from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes
from autoresearch.research_harness.fixture_inputs import select_fixture_user_ids
from autoresearch.research_harness.fixture_models import CandidateDataViewRequest
from autoresearch.research_harness.local_evaluation_fixture import _validated_judge_snapshot
from autoresearch.research_harness.local_training import load_local_training_input
from autoresearch.research_harness.recall_experiment import _verified_receipt, preference_features, predict_model
from tools.calibration_study import ARMS, FAMILIES, expanded_calibration, probability_diagnostics, study_summary
from tools.recall_analysis import METRICS, SEEDS, WORLDS, score_predictions
from tools.run_diverse_behavior import EVALUATION_SUMMARY_HASH, prediction_features, supervise
from tools.run_recall_experiment import digest, read_verified, tree_hashes, write_json

SPEC = "docs/specs/2026-09-06-calibration-sample-experiment.md"
PINS = {"result.json": "b40b80f0ce50494097cc74c903edcb86e63f33362b084e8ba3920f39465e1dfe",
        "selection.json": "5c9fe0929c3ac29982576ced639dad27f855804907589e79639d7389e8004fd0",
        "models-sealed.json": "21e921bc7ee924d260ce28447aca911614c67e794fe4baa3d7c3c8e1546fc009",
        "development.json": "4d30e98d7338507d6fd70eb2fc03a12c9f81640f282133f7c0cb090d470929df"}
COHORTS = (11901, 11902, 11903)
MAX_SECONDS = 7200
MAX_FITS = 12
BASE_SHA = "03b255a0614b881715833452ff038613c19e6053"
PREVIOUS_TREE_HASH = "bcf52af1b20552c83c095cace8f6c29cfaa4cfc7e49d64eeadaf47e882547407"


def code_hashes(repository: Path) -> dict:
    paths = [p for folder in ("autoresearch", "tools") for p in (repository / folder).rglob("*.py")]
    paths += [repository / SPEC, repository / "pyproject.toml", repository / "uv.lock"]
    return {p.relative_to(repository).as_posix(): digest(p) for p in sorted(paths)}


def old_cohort_users(root: Path, world: dict) -> set[str]:
    """pin된 과거 manifest에서 사용자 제외 목록만 읽는다."""
    path = root / f"world-{world['cohort_seed']}/judge-owned/raw/manifest.json"
    if digest(path) != world["raw_manifest_sha256"]:
        raise ValueError("old_cohort_manifest_pin_mismatch")
    manifest = json.loads(path.read_bytes())
    return set(manifest["validation_users"] + manifest["reserved_final_users"])


class Study:
    """실험 원본을 보존하고 새 실행 디렉터리에만 증거를 작성한다."""

    def __init__(self, args: argparse.Namespace, repository: Path, commit: str) -> None:
        self.args, self.repository, self.commit = args, repository, commit
        self.output = args.output
        self.started = perf_counter()
        self.fits, self.claims = 0, 0
        self.costs: dict[str, float] = {}
        self.models: dict = {}
        self.calibrations: dict = {}
        self.prepared: dict = {}
        self.protected: dict = {}
        self.worlds: dict = {}
        self.old_users: set = set()
        self.old_ids: set = set()
        self.seal: dict = {}
        self.development: dict = {}

    def check(self) -> None:
        if perf_counter() - self.started >= MAX_SECONDS:
            raise TimeoutError("calibration_total_budget_exceeded")

    def before_fit(self, kind: str) -> None:
        self.check()
        if kind != "calibration" or self.fits >= MAX_FITS:
            raise ValueError("calibration_fit_budget_exceeded")
        self.fits += 1
        write_json(self.output / f"fit-attempts/{self.fits:02}.json", {"kind": kind, "attempt": self.fits})

    def prepare(self) -> dict:
        old = self.args.previous_run
        for name, expected in PINS.items():
            if digest(old / name) != expected:
                raise ValueError("previous_run_pin_mismatch")
        self.protected = {name: tree_hashes(getattr(self.args, name)) for name in ("previous_run", "old_evaluation")}
        from hashlib import sha256
        if (sha256(canonical_json_bytes(self.protected["previous_run"])).hexdigest() != PREVIOUS_TREE_HASH
                or digest(self.args.old_evaluation / "summary.json") != EVALUATION_SUMMARY_HASH):
            raise ValueError("previous_tree_or_evaluation_pin_mismatch")
        write_json(self.output / "protected-inputs.json", self.protected)
        selection = json.loads((old / "selection.json").read_bytes())
        self.prepared = selection["prepared_inputs"]
        if tree_hashes(old / "models") != selection["models"]:
            raise ValueError("previous_models_changed")
        pins = json.loads((old / "models-sealed.json").read_bytes())
        for w in WORLDS:
            root = old / f"prepared/{w}"
            if tree_hashes(root) != self.prepared[str(w)]["hashes"]:
                raise ValueError("previous_prepared_changed")
            self.old_users.update(pq.read_table(root / "train-labels.parquet")["user_id"].to_pylist())
            for s in SEEDS:
                for family in FAMILIES:
                    key = f"{w}/{s}/{family}"
                    _verified_receipt(old / "models" / key, pins[key])
                    self.models[key] = pins[key]
        for seed in (1601, 1602, 1603, 10301, 10302, 10303, 10501, 10502, 10503, 10701, 10702, 10703):
            self.old_users.update(sum(select_fixture_user_ids(seed), ()))
        old_eval = json.loads((self.args.old_evaluation / "summary.json").read_bytes())
        self.old_ids.update(old_eval["forbidden_evaluation_identities"])
        for w in old_eval["worlds"]:
            self.old_users.update(old_cohort_users(self.args.old_evaluation, w))
            self.old_ids.update(w[k] for k in ("snapshot_fingerprint", "validation_evaluation_id", "final_evaluation_id"))
        for w in WORLDS:
            m = json.loads((old / f"fresh/{w}/judge-owned/raw/manifest.json").read_bytes())
            self.old_users.update(m["validation_users"] + m["reserved_final_users"])
            r = json.loads((old / f"fresh/{w}/receipt.json").read_bytes())
            self.old_ids.update(r[k] for k in ("snapshot_fingerprint", "validation_id", "final_id"))
        return {"models": self.models, "protected_files": sum(map(len, self.protected.values()))}

    def fit(self) -> dict:
        for w in WORLDS:
            root = self.args.previous_run / f"prepared/{w}"
            labels, features = [pq.read_table(root / f"train-{n}.parquet") for n in ("labels", "features")]
            for s in SEEDS:
                for family in FAMILIES:
                    key = f"{w}/{s}/{family}"
                    self.calibrations[key] = expanded_calibration(self.args.previous_run / "models" / key,
                        self.models[key], labels, features, self.output / "calibrations" / key, self.before_fit)
        if self.fits != MAX_FITS:
            raise ValueError("calibration_fit_count_invalid")
        write_json(self.output / "calibrations-sealed.json", tree_hashes(self.output / "calibrations"))
        return {"model_fits": 0, "calibration_fits": self.fits}

    def predict(self, world: int, split: str, keys: pa.Table, features: pa.Table) -> list:
        root = self.output / f"predictions/{split}/{world}"
        paths = []
        for s in SEEDS:
            for family in FAMILIES:
                self.check()
                key = f"{world}/{s}/{family}"
                if split == "development":
                    source = self.args.previous_run / f"predictions/development/{world}"
                    sealed = json.loads((source / "predictions-sealed.json").read_bytes())
                    pin = next(p for p in sealed["predictions"] if p["seed"] == s and p["arm"] == family)
                    path = source / f"{s}/{family}/prediction.parquet"
                    if digest(path) != pin["sha256"]:
                        raise ValueError("development_prediction_changed")
                    table = pq.read_table(path)
                    if not table.select(keys.column_names).equals(keys):
                        raise ValueError("development_keys_changed")
                    raw, probability = table["raw_score"].to_numpy(), table["probability"].to_numpy()
                else:
                    raw, probability = predict_model(self.args.previous_run / "models" / key, features, self.models[key])
                calibration = self.calibrations[key]["expanded"]
                expanded = expit(calibration["slope"] * raw + calibration["intercept"])
                name = "baseline_expanded" if family == "baseline15" else "preference_expanded"
                for arm, p in ((family, probability), (name, expanded)):
                    target = root / f"{s}/{arm}"
                    target.mkdir(parents=True, exist_ok=False)
                    table = keys.append_column("raw_score", pa.array(raw)).append_column("probability", pa.array(p))
                    pq.write_table(table, target / "prediction.parquet")
                    paths.append((s, arm, target))
        write_json(root / "predictions-sealed.json", {"keys_sha256": digest_keys(keys),
            "calibrations_sha256": digest(self.output / "calibrations-sealed.json"),
            "predictions": [{"seed": s, "arm": a, "sha256": digest(p / "prediction.parquet")} for s, a, p in paths]})
        return paths

    def score(self, w: int, keys: pa.Table, labels: pa.Table, paths: list) -> list:
        sealed = json.loads((paths[0][2].parents[1] / "predictions-sealed.json").read_bytes())
        if (sealed["keys_sha256"] != digest_keys(keys) or len(paths) != 8
                or {(s, a) for s, a, _ in paths} != {(s, a) for s in SEEDS for a in ARMS}
                or sealed["calibrations_sha256"] != digest(self.output / "calibrations-sealed.json")):
            raise ValueError("calibration_prediction_bundle_invalid")
        expected = {(p["seed"], p["arm"]): p["sha256"] for p in sealed["predictions"]}
        if len(expected) != 8:
            raise ValueError("calibration_prediction_bundle_incomplete")
        tables = {}
        for s, a, path in paths:
            if digest(path / "prediction.parquet") != expected[s, a]:
                raise ValueError("calibration_prediction_hash_changed")
            tables[s, a] = pq.read_table(path / "prediction.parquet")
        for s in SEEDS:
            for original, expanded in (("baseline15", "baseline_expanded"), ("preference", "preference_expanded")):
                if not tables[s, original]["raw_score"].equals(tables[s, expanded]["raw_score"]):
                    raise ValueError("calibration_raw_changed")
        label_map = {(r["evaluation_id"], r["slate_id"], r["video_id"]): r["clicked"] for r in labels.to_pylist()}
        y = np.array([label_map[tuple(r[k] for k in ("evaluation_id", "slate_id", "video_id"))] for r in keys.to_pylist()])
        observations = []
        for s, a, path in paths:
            self.check()
            table = tables[s, a]
            metrics = score_predictions(table.select(keys.column_names), labels, table["raw_score"].to_numpy(), table["probability"].to_numpy())
            write_json(path / "metrics.json", metrics)
            write_json(path / "diagnostics.json", probability_diagnostics(y, table["probability"].to_numpy()))
            observations.append({"world": w, "seed": s, "arm": a, "metrics": metrics, "row_keys_sha256": digest_keys(keys)})
        return observations

    def develop(self) -> dict:
        observations = []
        original = json.loads((self.args.previous_run / "development.json").read_bytes())["observations"]
        for w in WORLDS:
            root = self.args.previous_run / f"prepared/{w}"
            keys, features, labels = [pq.read_table(root / f"validation-{n}.parquet") for n in ("keys", "features", "labels")]
            rows = self.score(w, keys, labels, self.predict(w, "development", keys, features))
            for r in rows:
                if r["arm"] in FAMILIES:
                    old = next(v for v in original if all(v[k] == r[k] for k in ("world", "seed", "arm")))
                    if any(not np.isclose(r["metrics"][k], old["metrics"][k], rtol=0, atol=1e-12) for k in METRICS):
                        raise ValueError("old_development_not_reproduced")
            observations.extend(rows)
        self.development = study_summary(observations)
        write_json(self.output / "development.json", {"observations": observations, "summary": self.development})
        self.seal = {"commit": self.commit, "code": code_hashes(self.repository), "models": self.models,
                     "calibrations": tree_hashes(self.output / "calibrations"), "cohorts": COHORTS,
                     "arms": ARMS, "development_sha256": digest(self.output / "development.json")}
        write_json(self.output / "selection.json", self.seal)
        return self.development

    def verify_seal(self) -> None:
        if ((self.output / "selection.json").read_bytes() != canonical_json_bytes(self.seal)
                or self.seal["code"] != code_hashes(self.repository)
                or self.seal["calibrations"] != tree_hashes(self.output / "calibrations")
                or digest(self.output / "development.json") != self.seal["development_sha256"]):
            raise ValueError("calibration_selection_changed")

    def fresh(self) -> dict:
        self.verify_seal()
        requests = [BehaviorEvaluationRequest(seed, validation_users=200, final_users=800) for seed in COHORTS]
        policy = self.output / "fresh-policy.json"
        write_json(policy, evaluation_policy(tuple(requests)))
        result = {}
        for w, request in zip(WORLDS, requests, strict=True):
            self.check()
            root = self.output / f"fresh/{w}"
            raw = root / "judge-owned/raw"
            manifest = generate_behavior_evaluation(raw, request, policy_path=policy, expected_policy_sha256=digest(policy))
            users = set(manifest["validation_users"] + manifest["reserved_final_users"])
            if users & self.old_users:
                raise ValueError("calibration_new_users_overlap")
            self.old_users.update(users)
            source = BehaviorEvaluationSource(raw, expected_manifest_sha256=digest(raw / "manifest.json"))
            handoff = seal_behavior_evaluation(source, root / "judge-owned/state")
            ids = {str(handoff.snapshot_fingerprint), str(handoff.validation_id), str(handoff.final_holdout_id)}
            if ids & self.old_ids:
                raise ValueError("calibration_new_identity_overlap")
            self.old_ids.update(ids)
            self.worlds[w] = (source, handoff)
            result[str(w)] = {"cohort": request.seed, "snapshot": str(handoff.snapshot_fingerprint),
                              "final_id": str(handoff.final_holdout_id), "selection_sha256": digest(self.output / "selection.json")}
            write_json(root / "receipt.json", result[str(w)])
        return result

    def final(self) -> dict:
        from autoresearch.research_harness.local_embedding import LocalEmbeddingConfig, LocalSentenceTransformer
        self.verify_seal()
        embedding = LocalSentenceTransformer(LocalEmbeddingConfig(model_id="intfloat/multilingual-e5-small",
            revision="614241f622f53c4eeff9890bdc4f31cfecc418b3", model_dir=self.args.model_dir,
            cache_dir=self.args.cache_dir, device="cuda", batch_size=8))
        if any(embedding.identity != p["embedding"]["identity"] for p in self.prepared.values()):
            raise ValueError("calibration_embedding_identity_changed")
        observations = []
        for w, (source, handoff) in self.worlds.items():
            self.verify_seal()
            self.check()
            if self.claims >= 3:
                raise ValueError("calibration_final_budget_exceeded")
            state = handoff.snapshot_root.parents[2]
            (state / "final-holdout-consumed").mkdir(exist_ok=True)
            grant = claim_final_consumption(FinalConsumptionRequest(state, handoff, BASE_SHA, self.commit, datetime.now(UTC)))
            self.claims += 1
            root = self.output / f"final-inputs/{w}"
            root.mkdir(parents=True)
            write_json(root / "claim.json", {"marker_sha256": grant.evidence.marker_sha256, "evaluation_id": str(handoff.final_holdout_id)})
            metadata = prepare_behavior_metadata(source, handoff, final=True)
            (root / "candidate").mkdir()
            view = materialize_final_candidate_data_view(CandidateDataViewRequest(handoff, root / "candidate"), source=source, metadata=metadata, grant=grant)
            slate = view.root / "slate.parquet"
            keys, features, _ = prediction_features(slate, view.manifest_sha256, embedding, root / "base-features")
            inputs = load_local_training_input(slate)
            extra = preference_features(inputs.slate, inputs.history, inputs.videos)
            for column in extra.column_names:
                features = features.append_column(column, extra[column])
            paths = self.predict(w, "final", keys, features)
            if not grant._authorizes(handoff):
                raise ValueError("calibration_final_grant_invalid")
            _, manifest = _validated_judge_snapshot(handoff.snapshot_root, expected_fingerprint=str(handoff.snapshot_fingerprint))
            receipt = manifest.final_holdout.artifacts.labels
            labels = read_verified(handoff.snapshot_root, {"path": receipt.relative_path, "sha256": receipt.sha256, "rows": receipt.rows})
            observations.extend(self.score(w, keys, labels, paths))
        summary = study_summary(observations)
        verdict = ("uninformative" if not summary["coverage_valid"] else "supported"
                   if self.development["verdict"] == summary["verdict"] == "supported" else "not_supported")
        result = {"observations": observations, "summary": summary, "verdict": verdict,
                  "final_claims": self.claims, "embedding_stats": embedding.stats}
        write_json(self.output / "final.json", result)
        return result


def digest_keys(keys: pa.Table) -> str:
    from hashlib import sha256
    return sha256(canonical_json_bytes(keys.to_pylist())).hexdigest()


def run(args: argparse.Namespace) -> None:
    repository = Path(__file__).resolve().parents[1]
    for name in ("previous_run", "old_evaluation", "model_dir", "cache_dir"):
        p = getattr(args, name).resolve()
        if args.output.resolve().is_relative_to(p) or p.is_relative_to(args.output.resolve()):
            raise ValueError("calibration_input_output_overlap")
    if subprocess.run(["git", "status", "--porcelain"], cwd=repository, check=True, capture_output=True, text=True).stdout:
        raise ValueError("calibration_requires_clean_checkout")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()
    common = Path(subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repository,
                                check=True, capture_output=True, text=True).stdout.strip())
    args.output.mkdir(parents=True, exist_ok=False)
    study = Study(args, repository, commit)
    write_json(args.output / "preflight.json", {"commit": commit, "code": code_hashes(repository),
               "fit_cap": MAX_FITS, "seconds_cap": MAX_SECONDS, "os_sandbox": False, "paid_api_calls": 0})
    try:
        for name in ("prepare", "fit", "develop", "fresh", "final"):
            if name in ("fresh", "final") and not study.development["coverage_valid"]:
                continue
            study.check()
            start = perf_counter()
            write_json(args.output / f"stages/{name}-attempt.json", {"started": datetime.now(UTC).isoformat()})
            try:
                if name == "fit":
                    write_json(common / "research-experiment-claims/issue-119-calibration.json", {"commit": commit, "output": str(args.output)})
                value = getattr(study, name)()
            finally:
                study.costs[name] = perf_counter() - start
            study.check()
            write_json(args.output / f"stages/{name}-complete.json", {"elapsed_seconds": study.costs[name], "result": value})
        for name, hashes in study.protected.items():
            if tree_hashes(getattr(args, name)) != hashes:
                raise ValueError("calibration_protected_input_changed")
        study.verify_seal()
        done = study.claims == 3 and (args.output / "final.json").exists()
        verdict = json.loads((args.output / "final.json").read_bytes())["verdict"] if done else "uninformative"
        write_json(args.output / "result.json", {"completed": done, "verdict": verdict, "model_fit_calls": 0,
                   "calibration_fit_calls": study.fits, "final_claims": study.claims, "costs": study.costs,
                   "elapsed_seconds": perf_counter() - study.started, "protected_files": sum(map(len, study.protected.values())),
                   "unperformed": [] if done else ["fresh", "final"], "paid_api_calls": 0})
    except Exception as error:
        write_json(args.output / "failure.json", {"error_type": type(error).__name__, "reason": str(error),
                   "fit_attempts": study.fits, "final_claims": study.claims, "costs": study.costs,
                   "elapsed_seconds": perf_counter()-study.started, "retry_allowed": False})
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "previous-run", "old-evaluation", "model-dir", "cache-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        run(args)
    else:
        raise SystemExit(supervise([sys.executable, "-m", "tools.run_calibration_study", *sys.argv[1:], "--worker"], args.output, timeout=MAX_SECONDS))


if __name__ == "__main__":
    main()
