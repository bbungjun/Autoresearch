"""#105 날짜순 warm-up 생성부터 최근 행동 ablation의 Judge 평가까지 실행한다.

[파이프라인] opt-in fixture v2, candidate view, 날짜별 학습 선택, 모델 fit과 봉인 평가를 연결한다.
[기능] 30일 선행 이력을 갖는 15/10피처 비교 36회, 시간 감사·단일 final 소비·paired 결과를 기록한다.
[비책임] production 모델 승격, LLM label 생성, 기존 final 재평가와 자동 재시도는 하지 않는다.
"""

import argparse
from datetime import UTC, date, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from time import perf_counter

from autoresearch.research_harness.candidate_data_view import (
    materialize_candidate_data_view_v2, materialize_final_candidate_data_view,
    prepare_candidate_metadata, prepare_final_candidate_metadata,
)
from autoresearch.research_harness.consumption_registry import (
    FinalConsumptionGrant, FinalConsumptionRequest, claim_final_consumption,
)
from autoresearch.research_harness.domain import YouTubeCTRDomain
from autoresearch.research_harness.fixture_models import (
    CandidateDataViewRequest, JudgeSnapshotHandoff, LocalEvaluationFixtureRequest,
)
from autoresearch.research_harness.local_embedding import LocalEmbeddingConfig, LocalSentenceTransformer
from autoresearch.research_harness.local_evaluation_fixture import FixtureActionLogSource, build_local_evaluation_fixture
from autoresearch.research_harness.local_training import load_local_training_input, train_local_candidate
from autoresearch.research_harness.temporal_training import select_training_window
from tools.popularity_recall_analysis import input_diagnostics, model_importance
from tools.recent_behavior_analysis import (
    ARMS, WORLD_SEEDS, TRAINING_SEEDS, audit_training_features, columns, informative, summarize,
)
from tools.run_popularity_recall import _atomic_write, _git_head, _score_table, _second_claim_fails_closed, _write_json


EVALUATION_DATE = date(2026, 9, 3)
TRAINING_DATE = date(2026, 9, 1)
BASELINE_SHA = "45c416a5ad1f4510ea6c690203fb9fd0662dc642"
PREVIOUS_DIGESTS = frozenset({
    "e504042bded46fa385b6164c3d45136f041a15cbe6d8b9f896965feefc24d7cc",
    "b70172bbcebe0cd7dfc47d243e0741b8e7f48def3a86f53c765f9d2fd648f5f5",
})


def claim_run(output_root: Path, candidate_sha: str) -> None:
    """출력/worktree를 바꾸어도 동일 #105를 반복 소비하지 못하게 한다."""
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            check=True, capture_output=True, text=True).stdout.strip()
    root = Path(common) / "research-experiment-claims"
    root.mkdir(exist_ok=True)
    payload = json.dumps({"candidate_sha": candidate_sha, "output_root": str(output_root),
                          "evaluation_date": str(EVALUATION_DATE), "world_seeds": WORLD_SEEDS}).encode()
    with (root / "issue-105-20260903.json").open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _run_split(
    handoff: JudgeSnapshotHandoff, slate_path: Path, root: Path, world: int, split: str,
    embedding: LocalSentenceTransformer, observations: list[dict], grant: FinalConsumptionGrant | None,
) -> bool:
    root.mkdir()
    selection = select_training_window(load_local_training_input(slate_path), TRAINING_DATE, TRAINING_DATE)
    inputs = selection.inputs
    _write_json(root / "training-window.json", selection.receipt)
    diagnostics = input_diagnostics(inputs, embedding, training_seeds=TRAINING_SEEDS)
    _write_json(root / "feature-diagnostics.json", diagnostics)
    _write_json(root / "temporal-audit.json", audit_training_features(inputs, embedding, TRAINING_DATE))
    if not informative(diagnostics):
        _write_json(root / "uninformative.json", {"reason": "history_or_recent_variation_insufficient"})
        return False
    domain = YouTubeCTRDomain()
    paired_receipts: dict[int, dict] = {}
    for arm in ARMS:
        for seed in TRAINING_SEEDS:
            trained = train_local_candidate(inputs, seed=seed, embedding=embedding, feature_columns=columns(arm))
            receipt = {**trained.receipt, "training_window": selection.receipt}
            previous = paired_receipts.setdefault(seed, receipt)
            if previous["splits"] != receipt["splits"] or previous["model_config"] != receipt["model_config"]:
                raise ValueError("paired_training_receipt_mismatch")
            arm_root = root / arm / str(seed)
            arm_root.mkdir(parents=True)
            _atomic_write(arm_root / "model.txt", trained.model_text.encode())
            _write_json(arm_root / "training.json", receipt)
            _write_json(arm_root / "importance.json", model_importance(trained.model_text))
            metrics = _score_table(domain, handoff, trained.predictions, arm_root, grant)
            _write_json(arm_root / "metrics.json", metrics)
            observations.append(dict(world_seed=world, split=split, training_seed=seed, arm=arm, metrics=metrics))
        print(f"PROGRESS world={world} split={split} arm={arm}", flush=True)
    return True


def run(*, output_root: Path, model_dir: Path, cache_dir: Path, previous_results: list[Path]) -> Path:
    """사전 등록된 새 데이터에서 한 번 실행하고 결과 및 digest를 게시한다."""
    previous_bytes = [p.read_bytes() for p in previous_results]
    if len(previous_bytes) != 2 or {sha256(b).hexdigest() for b in previous_bytes} != PREVIOUS_DIGESTS:
        raise ValueError("previous_result_digests_invalid")
    forbidden = {str(w[key]) for payload in previous_bytes for w in json.loads(payload)["worlds"]
                 for key in ("snapshot_fingerprint", "validation_evaluation_id", "final_evaluation_id")}
    if subprocess.run(["git", "status", "--porcelain"], check=True, capture_output=True, text=True).stdout.strip():
        raise RuntimeError("experiment_requires_clean_checkout")
    candidate_sha = _git_head()
    output_root.mkdir(parents=True, exist_ok=False)
    claim_run(output_root, candidate_sha)
    started = perf_counter()
    embedding = LocalSentenceTransformer(LocalEmbeddingConfig(
        model_id="intfloat/multilingual-e5-small", revision="614241f622f53c4eeff9890bdc4f31cfecc418b3",
        model_dir=model_dir, cache_dir=cache_dir, device="cuda", batch_size=8,
    ))
    observations: list[dict] = []
    worlds: list[dict] = []
    for world in WORLD_SEEDS:
        root = output_root / f"world-{world}"
        state = root / "judge-state"
        state.mkdir(parents=True)
        print(f"BUILD world={world} dates=2026-08-02..2026-09-04", flush=True)
        fixture = build_local_evaluation_fixture(LocalEvaluationFixtureRequest(state, EVALUATION_DATE, world, history_days=32))
        handoff = fixture.judge
        ids = {str(handoff.snapshot_fingerprint), str(handoff.validation_id), str(handoff.final_holdout_id)}
        if forbidden & ids:
            raise ValueError("evaluation_identity_reused")
        forbidden.update(ids)
        world_receipt = {"world_seed": world, "fixture_descriptor_sha256": fixture.descriptor_sha256,
                         "snapshot_fingerprint": str(handoff.snapshot_fingerprint),
                         "validation_evaluation_id": str(handoff.validation_id),
                         "final_evaluation_id": str(handoff.final_holdout_id),
                         "generated_dates": [str(p.dt) for p in fixture.action_log_partitions]}
        _write_json(root / "fixture-receipt.json", world_receipt)
        source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
        destination = root / "candidate-validation"
        destination.mkdir()
        view = materialize_candidate_data_view_v2(CandidateDataViewRequest(handoff, destination),
                    source=source, metadata=prepare_candidate_metadata(handoff, source=source))
        if not _run_split(handoff, view.root / "slate.parquet", root / "validation-results",
                          world, "validation", embedding, observations, None):
            path = output_root / "result.json"
            _write_json(path, {"verdict": "uninformative", "world_seed": world,
                               "final_consumed_for_current_world": False, "candidate_sha": candidate_sha})
            return path
        metadata = prepare_final_candidate_metadata(handoff, source=source)
        (fixture.fixture_root / "final-holdout-consumed").mkdir()
        request = FinalConsumptionRequest(fixture.fixture_root, handoff, BASELINE_SHA, candidate_sha, datetime.now(UTC))
        grant = claim_final_consumption(request)
        if not _second_claim_fails_closed(request):
            raise RuntimeError("final_second_claim_not_rejected")
        world_receipt.update(final_marker_sha256=grant.evidence.marker_sha256, second_claim_fail_closed=True)
        _write_json(root / "final-claim.json", world_receipt)
        destination = root / "candidate-final"
        destination.mkdir()
        view = materialize_final_candidate_data_view(CandidateDataViewRequest(handoff, destination),
                                                     source=source, metadata=metadata, grant=grant)
        if not _run_split(handoff, view.root / "slate.parquet", root / "final-results",
                          world, "final_holdout", embedding, observations, grant):
            raise RuntimeError("final_training_inputs_uninformative")
        worlds.append(world_receipt)
    if [p.read_bytes() for p in previous_results] != previous_bytes:
        raise RuntimeError("previous_results_changed")
    summary = summarize(observations)
    payload = {"contract_version": "recent-behavior-temporal-v1", "scope": "rule_based_synthetic_fixture",
               "candidate_sha": candidate_sha, "baseline_sha": BASELINE_SHA,
               "evaluation_date": str(EVALUATION_DATE), "training_date": str(TRAINING_DATE),
               "world_seeds": WORLD_SEEDS, "training_seeds": TRAINING_SEEDS,
               "arms": {a: columns(a) for a in ARMS}, "worlds": worlds,
               "observations": observations, "summary": summary,
               "previous_result_sha256": sorted(PREVIOUS_DIGESTS),
               "embedding_identity": embedding.identity, "embedding_manifest": embedding.manifest,
               "embedding_stats": embedding.stats, "duration_seconds": perf_counter()-started}
    path = output_root / "result.json"
    _write_json(path, payload)
    _atomic_write(path.with_suffix(".sha256"), (sha256(path.read_bytes()).hexdigest()+"  result.json\n").encode())
    print(f"COMPLETE {path} verdict={summary['verdict']}", flush=True)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "model-dir", "cache-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--previous-result", type=Path, action="append", required=True)
    args = parser.parse_args()
    run(output_root=args.output.absolute(), model_dir=args.model_dir.absolute(),
        cache_dir=args.cache_dir.absolute(), previous_results=[p.absolute() for p in args.previous_result])


if __name__ == "__main__":
    main()
