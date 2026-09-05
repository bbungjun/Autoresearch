"""#103 인기도 제거 확인 실험을 합성 fixture의 validation/final에서 실행한다.

[파이프라인] 로컬 Stage C fixture 생성부터 candidate view, 학습, prediction 봉인과
Sealed Judge 채점까지 실험 전 구간을 조립한다.

[기능] 사전 등록된 3×3 seed와 3개 arm을 실행하고 final 단일 소비 증거, 원시 metric,
paired delta·표본 표준편차와 판정을 원자적 JSON으로 게시한다.

[비책임] 실제 사용자 CTR, LLM relevance/Judge, watch time과 장기 폐루프 효과를 측정하지
않으며 production 모델을 등록하거나 승격하지 않는다.
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter

from autoresearch.research_harness.candidate_data_view import (
    materialize_candidate_data_view_v2,
    materialize_final_candidate_data_view,
    prepare_candidate_metadata,
    prepare_final_candidate_metadata,
)
from autoresearch.research_harness.consumption_registry import (
    ConsumptionRegistryError,
    ConsumptionRegistryErrorCode,
    FinalConsumptionGrant,
    FinalConsumptionRequest,
    claim_final_consumption,
)
from autoresearch.research_harness.domain import YouTubeCTRDomain
from autoresearch.research_harness.fixture_models import (
    CandidateDataViewRequest,
    LocalEvaluationFixtureRequest,
)
from autoresearch.research_harness.local_embedding import (
    LocalEmbeddingConfig,
    LocalSentenceTransformer,
)
from autoresearch.research_harness.local_evaluation_fixture import (
    FixtureActionLogSource,
    build_local_evaluation_fixture,
)
from autoresearch.research_harness.local_training import (
    load_local_training_input,
    train_local_candidate,
)
from autoresearch.research_harness.personalization_ablation import (
    ABLATION_FEATURE_GROUPS,
    feature_columns_for_arm,
    scoring_result_dict,
)
from autoresearch.research_harness.prediction import _prediction_bytes


from tools.popularity_recall_analysis import (
    ARMS, WORLD_SEEDS, TRAINING_SEEDS, summarize, input_diagnostics, model_importance,
)

EVALUATION_DATE = date(2026, 9, 2)
BASELINE_SHA = "399fbbb7d221130beb3dc061bbffe0e95c2e44cb"
PREVIOUS_RESULT_SHA256 = "e504042bded46fa385b6164c3d45136f041a15cbe6d8b9f896965feefc24d7cc"


def main() -> int:
    """Parse fixed-run paths, execute the experiment, and publish its receipt."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    parser.add_argument("--previous-result", required=True, type=Path)
    args = parser.parse_args()
    run_experiment(
        output_root=args.output.absolute(),
        model_dir=args.model_dir.absolute(),
        cache_dir=args.cache_dir.absolute(),
        device=args.device,
        previous_result=args.previous_result.absolute(),
    )
    return 0


def run_experiment(
    *, output_root: Path, model_dir: Path, cache_dir: Path, device: str, previous_result: Path
) -> Path:
    """Run the complete preregistered experiment into a new output directory."""

    previous_bytes = previous_result.read_bytes()
    if sha256(previous_bytes).hexdigest() != PREVIOUS_RESULT_SHA256:
        raise ValueError("previous_result_digest_mismatch")
    previous = json.loads(previous_bytes)
    forbidden = {str(w[key]) for w in previous["worlds"] for key in
                 ("snapshot_fingerprint", "validation_evaluation_id", "final_evaluation_id")}
    if subprocess.run(["git", "status", "--porcelain"], check=True,
                      capture_output=True, text=True).stdout.strip():
        raise RuntimeError("experiment_requires_clean_checkout")
    started = perf_counter()
    output_root.mkdir(parents=True, exist_ok=False)
    candidate_sha = _git_head()
    _claim_experiment(output_root, candidate_sha)
    embedding = LocalSentenceTransformer(
        LocalEmbeddingConfig(
            model_id="intfloat/multilingual-e5-small",
            revision="614241f622f53c4eeff9890bdc4f31cfecc418b3",
            model_dir=model_dir,
            cache_dir=cache_dir,
            device=device,
            batch_size=8,
        )
    )
    observations: list[dict[str, object]] = []
    worlds: list[dict[str, object]] = []
    for world_seed in WORLD_SEEDS:
        world_root = output_root / f"world-{world_seed}"
        (world_root / "judge-state").mkdir(parents=True)
        fixture = build_local_evaluation_fixture(
            LocalEvaluationFixtureRequest(
                world_root / "judge-state", EVALUATION_DATE, world_seed
            )
        )
        identities = {str(fixture.judge.snapshot_fingerprint), str(fixture.judge.validation_id),
                      str(fixture.judge.final_holdout_id)}
        if identities & forbidden:
            raise RuntimeError("evaluation_identity_reused")
        forbidden.update(identities)
        source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
        validation_metadata = prepare_candidate_metadata(fixture.judge, source=source)
        validation_root = world_root / "candidate-validation"
        validation_root.mkdir()
        validation = materialize_candidate_data_view_v2(
            CandidateDataViewRequest(fixture.judge, validation_root),
            source=source,
            metadata=validation_metadata,
        )
        _run_split(
            fixture.judge,
            validation.root / "slate.parquet",
            world_root / "validation-results",
            world_seed,
            "validation",
            embedding,
            observations,
            final_grant=None,
        )

        final_metadata = prepare_final_candidate_metadata(fixture.judge, source=source)
        (fixture.fixture_root / "final-holdout-consumed").mkdir()
        claim_request = FinalConsumptionRequest(
            judge_state_root=fixture.fixture_root,
            handoff=fixture.judge,
            baseline_sha=BASELINE_SHA,
            candidate_sha=candidate_sha,
            started_at=datetime.now(UTC),
        )
        grant = claim_final_consumption(claim_request)
        fail_closed = _second_claim_fails_closed(claim_request)
        if not fail_closed:
            raise RuntimeError("second_final_claim_not_rejected")
        _write_json(world_root / "final-claim.json", {
            "evaluation_id": str(fixture.judge.final_holdout_id),
            "marker_sha256": grant.evidence.marker_sha256, "second_claim_fail_closed": fail_closed,
        })
        final_root = world_root / "candidate-final"
        final_root.mkdir()
        final = materialize_final_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, final_root),
            source=source,
            metadata=final_metadata,
            grant=grant,
        )
        _run_split(
            fixture.judge,
            final.root / "slate.parquet",
            world_root / "final-results",
            world_seed,
            "final_holdout",
            embedding,
            observations,
            final_grant=grant,
        )
        worlds.append(
            {
                "world_seed": world_seed,
                "fixture_descriptor_sha256": fixture.descriptor_sha256,
                "snapshot_fingerprint": str(fixture.judge.snapshot_fingerprint),
                "validation_evaluation_id": str(fixture.judge.validation_id),
                "final_evaluation_id": str(fixture.judge.final_holdout_id),
                "final_marker_sha256": grant.evidence.marker_sha256,
                "second_claim_fail_closed": fail_closed,
            }
        )

    if sha256(previous_result.read_bytes()).hexdigest() != PREVIOUS_RESULT_SHA256:
        raise RuntimeError("previous_result_changed")
    summary = summarize(observations)
    payload = {
        "contract_version": "popularity-recall-v1",
        "scope": "rule_based_synthetic_fixture",
        "evidence_excludes": [
            "actual_user_ctr",
            "llm_relevance",
            "multiple_llm_judges",
            "simulated_watch_time",
            "long_term_closed_loop_bias",
        ],
        "evaluation_date": EVALUATION_DATE.isoformat(),
        "world_seeds": list(WORLD_SEEDS),
        "training_seeds": list(TRAINING_SEEDS),
        "comparison_arms": list(ARMS),
        "ablation_feature_groups": {
            arm: sorted(features) for arm, features in ABLATION_FEATURE_GROUPS.items()
        },
        "previous_result_sha256": PREVIOUS_RESULT_SHA256,
        "baseline_sha": BASELINE_SHA,
        "candidate_sha": candidate_sha,
        "embedding_identity": embedding.identity,
        "embedding_manifest": embedding.manifest,
        "embedding_stats": embedding.stats,
        "worlds": worlds,
        "observations": observations,
        "summary": summary,
        "duration_seconds": perf_counter() - started,
    }
    result_path = output_root / "result.json"
    result_bytes = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    _atomic_write(result_path, result_bytes)
    _atomic_write(
        result_path.with_suffix(".sha256"),
        (sha256(result_bytes).hexdigest() + "  result.json\n").encode("ascii"),
    )
    print(f"COMPLETE {result_path} verdict={summary['verdict']}", flush=True)
    return result_path


def _run_split(
    handoff: object,
    slate_path: Path,
    output_root: Path,
    world_seed: int,
    split: str,
    embedding: LocalSentenceTransformer,
    observations: list[dict[str, object]],
    *,
    final_grant: FinalConsumptionGrant | None,
) -> None:
    output_root.mkdir()
    inputs = load_local_training_input(slate_path)
    domain = YouTubeCTRDomain()

    _write_json(output_root / "feature-diagnostics.json", input_diagnostics(inputs, embedding))

    for arm in ARMS:
        for training_seed in TRAINING_SEEDS:
            trained = train_local_candidate(
                inputs,
                seed=training_seed,
                embedding=embedding,
                feature_columns=feature_columns_for_arm(arm),
            )
            arm_root = output_root / arm / str(training_seed)
            arm_root.mkdir(parents=True)
            model_bytes = trained.model_text.encode("utf-8")
            _atomic_write(arm_root / "model.txt", model_bytes)
            _atomic_write(
                arm_root / "training.json",
                (
                    json.dumps(
                        trained.receipt,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                        allow_nan=False,
                    )
                    + "\n"
                ).encode("utf-8"),
            )
            _write_json(arm_root / "importance.json", model_importance(trained.model_text))
            result = _score_table(
                domain,
                handoff,
                trained.predictions,
                arm_root,
                final_grant,
            )
            _write_json(arm_root / "metrics.json", result)
            observations.append(
                _observation(world_seed, split, training_seed, arm, result)
            )
        _progress(world_seed, split, arm)



def _score_table(
    domain: YouTubeCTRDomain,
    handoff: object,
    table: object,
    output_root: Path,
    final_grant: FinalConsumptionGrant | None,
) -> dict[str, object]:
    output_root.mkdir(parents=True, exist_ok=True)
    candidate = output_root / "prediction.csv"
    judge_copy = output_root / "judge-copy.csv"
    _atomic_write(candidate, _prediction_bytes(table))
    sealed = domain.validate_candidate(candidate, judge_copy)
    return scoring_result_dict(
        domain.evaluate(handoff, sealed, final_grant=final_grant)
    )


def _observation(
    world_seed: int,
    split: str,
    training_seed: int,
    arm: str,
    metrics: dict[str, object],
) -> dict[str, object]:
    return {
        "world_seed": world_seed,
        "split": split,
        "training_seed": training_seed,
        "arm": arm,
        "metrics": metrics,
    }


def _second_claim_fails_closed(request: FinalConsumptionRequest) -> bool:
    try:
        claim_final_consumption(request)
    except ConsumptionRegistryError as error:
        if error.code is ConsumptionRegistryErrorCode.ALREADY_CONSUMED:
            return True
        raise
    return False


def _progress(world_seed: int, split: str, arm: str) -> None:
    print(f"PROGRESS world={world_seed} split={split} arm={arm}", flush=True)


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise RuntimeError("personalization_ablation_git_head_invalid")
    return value


def _claim_experiment(output_root: Path, candidate_sha: str) -> None:
    """모든 worktree가 공유하는 단일 실행 claim으로 출력 변경 재소비를 막는다.

    중단해도 claim을 보존한다. 다른 clone/수동 marker 삭제를 막는 보안 경계는 아니다.
    """
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    root = Path(common) / "research-experiment-claims"
    root.mkdir(exist_ok=True)
    payload = json.dumps({
        "candidate_sha": candidate_sha, "output_root": str(output_root),
        "evaluation_date": str(EVALUATION_DATE), "world_seeds": WORLD_SEEDS,
    }, sort_keys=True).encode("utf-8")
    # 최종 claim 이름을 직접 exclusive create한다. 부분 쓰기도 재시도를 막는다.
    with (root / "issue-103-20260902.json").open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _write_json(path: Path, value: object) -> None:
    _atomic_write(path, (json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   indent=2, allow_nan=False) + "\n").encode("utf-8"))


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    if os.path.lexists(path) or os.path.lexists(temporary):
        raise FileExistsError(path.name)
    with temporary.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


if __name__ == "__main__":
    sys.exit(main())
