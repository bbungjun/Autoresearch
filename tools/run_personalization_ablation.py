"""#16 고정 비교군·ablation을 합성 fixture의 validation/final에서 실행한다.

[파이프라인] 로컬 Stage C fixture 생성부터 candidate view, 학습, prediction 봉인과
Sealed Judge 채점까지 실험 전 구간을 조립한다.

[기능] 사전 등록된 3×3 seed와 10개 arm을 실행하고 final 단일 소비 증거, 원시 metric,
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
from autoresearch.research_harness.judge import (
    build_final_target,
    build_validation_target,
    score_oracle_upper_bound,
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
    COMPARISON_ARMS,
    TRAINING_SEEDS,
    WORLD_SEEDS,
    feature_columns_for_arm,
    heuristic_predictions,
    scoring_result_dict,
    summarize_observations,
)
from autoresearch.research_harness.prediction import _prediction_bytes


EVALUATION_DATE = date(2026, 9, 1)
BASELINE_SHA = "c4e0d07a882bb60c15579346476a410c2f0ffed8"
LEARNED_ARMS = (
    "video_only_lgbm",
    "personalized_lgbm",
    *ABLATION_FEATURE_GROUPS,
)
ALL_ARMS = (*COMPARISON_ARMS, *ABLATION_FEATURE_GROUPS)


def main() -> int:
    """Parse fixed-run paths, execute the experiment, and publish its receipt."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    args = parser.parse_args()
    run_experiment(
        output_root=args.output.absolute(),
        model_dir=args.model_dir.absolute(),
        cache_dir=args.cache_dir.absolute(),
        device=args.device,
    )
    return 0


def run_experiment(
    *, output_root: Path, model_dir: Path, cache_dir: Path, device: str
) -> Path:
    """Run the complete preregistered experiment into a new output directory."""

    started = perf_counter()
    output_root.mkdir(parents=True, exist_ok=False)
    candidate_sha = _git_head()
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

    summary = summarize_observations(observations)
    payload = {
        "contract_version": "personalization-ablation-v1",
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
        "comparison_arms": list(COMPARISON_ARMS),
        "ablation_feature_groups": {
            arm: sorted(features) for arm, features in ABLATION_FEATURE_GROUPS.items()
        },
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

    for arm in ("trending", "popularity"):
        result = _score_table(
            domain,
            handoff,
            heuristic_predictions(inputs, arm),
            output_root / arm,
            final_grant,
        )
        for training_seed in TRAINING_SEEDS:
            observations.append(
                _observation(world_seed, split, training_seed, arm, result)
            )
        _progress(world_seed, split, arm)

    for arm in LEARNED_ARMS:
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
            result = _score_table(
                domain,
                handoff,
                trained.predictions,
                arm_root,
                final_grant,
            )
            observations.append(
                _observation(world_seed, split, training_seed, arm, result)
            )
        _progress(world_seed, split, arm)

    target = (
        build_validation_target(handoff)
        if final_grant is None
        else build_final_target(handoff, final_grant)
    )
    oracle = scoring_result_dict(score_oracle_upper_bound(target))
    for training_seed in TRAINING_SEEDS:
        observations.append(
            _observation(world_seed, split, training_seed, "oracle_upper_bound", oracle)
        )
    _progress(world_seed, split, "oracle_upper_bound")


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
