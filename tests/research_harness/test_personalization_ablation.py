"""#16 비교군·개인화 피처군 ablation의 고정 arm 계약을 검증한다.

[파이프라인] candidate-safe feature batch와 Sealed Judge 사이의 실험 전용 비교 구간이다.
[기능] 비교군/ablation 열, slate 내부 heuristic 점수와 입력 거부를 검증한다.
[비책임] 실제 fixture 생성·LightGBM fit·final 소비와 결과 보고는 실행 CLI가 담당한다.
"""

from importlib import import_module
from importlib.util import find_spec
from types import ModuleType

import pytest

from autoresearch.feature_engineering.model_contract import MODEL_FEATURE_COLUMNS


def module() -> ModuleType:
    name = "autoresearch.research_harness.personalization_ablation"
    assert find_spec(name), "RED: personalization_ablation 구현이 필요합니다"
    return import_module(name)


def test_five_comparison_arms_and_five_ablation_arms_are_fixed() -> None:
    m = module()

    assert m.COMPARISON_ARMS == (
        "trending",
        "popularity",
        "video_only_lgbm",
        "personalized_lgbm",
        "oracle_upper_bound",
    )
    assert tuple(m.ABLATION_FEATURE_GROUPS) == (
        "without_user_static",
        "without_recent_behavior",
        "without_category_match",
        "without_topic_similarity",
        "without_video_popularity",
    )
    assert m.feature_columns_for_arm("personalized_lgbm") == MODEL_FEATURE_COLUMNS
    assert m.feature_columns_for_arm("video_only_lgbm") == m.VIDEO_ONLY_FEATURE_COLUMNS
    for arm, removed in m.ABLATION_FEATURE_GROUPS.items():
        selected = m.feature_columns_for_arm(arm)
        assert selected == tuple(name for name in MODEL_FEATURE_COLUMNS if name not in removed)
        assert set(selected).isdisjoint(removed)


def test_slate_normalization_preserves_order_and_direction() -> None:
    m = module()
    slate_ids = ("s1", "s1", "s1", "s2", "s2")

    higher = m.normalize_scores_by_slate(slate_ids, (10, 30, 20, 5, 5), higher_is_better=True)
    lower = m.normalize_scores_by_slate(slate_ids, (1, 3, 2, 8, 8), higher_is_better=False)

    assert higher == (0.0, 1.0, 0.5, 0.5, 0.5)
    assert lower == (1.0, 0.0, 0.5, 0.5, 0.5)


@pytest.mark.parametrize(
    ("slates", "values"),
    [
        (("s",), ()),
        (("",), (1.0,)),
        (("s", "s"), (1.0, float("nan"))),
    ],
)
def test_slate_normalization_rejects_invalid_input(
    slates: tuple[str, ...],
    values: tuple[float, ...],
) -> None:
    with pytest.raises(ValueError, match="personalization_ablation_scores_invalid"):
        module().normalize_scores_by_slate(slates, values, higher_is_better=True)


def test_unknown_learned_arm_is_rejected() -> None:
    with pytest.raises(ValueError, match="personalization_ablation_arm_invalid"):
        module().feature_columns_for_arm("trending")


def test_synthetic_fixture_trending_rank_is_recovered_from_safe_video_id() -> None:
    assert module().synthetic_fixture_trending_ranks(
        ("fixture-video-20260901-0015", "fixture-video-20260901-0002")
    ) == (16, 3)

    with pytest.raises(ValueError, match="personalization_ablation_scores_invalid"):
        module().synthetic_fixture_trending_ranks(("production-video",))


def test_summary_applies_direction_and_two_thirds_rule() -> None:
    m = module()
    observations = _supported_observations(m)

    summary = m.summarize_observations(observations)

    assert summary["verdict"] == "supported"
    assert all(summary["checks"].values())
    assert summary["paired_deltas"]["personalized_minus_video_only"]["log_loss"][
        "mean_directional_delta"
    ] == pytest.approx(0.0)


def test_summary_rejects_incomplete_fixed_grid() -> None:
    m = module()
    observations = []
    for split in m.EVALUATION_SPLITS:
        for arm in (*m.COMPARISON_ARMS, *m.ABLATION_FEATURE_GROUPS):
            observations.append(
                {
                    "world_seed": m.WORLD_SEEDS[0],
                    "split": split,
                    "training_seed": m.TRAINING_SEEDS[0],
                    "arm": arm,
                    "metrics": _metrics(0.7),
                }
            )

    with pytest.raises(ValueError, match="personalization_ablation_observations_invalid"):
        m.summarize_observations(observations)


def test_summary_requires_judge_coverage_floor() -> None:
    m = module()
    observations = _supported_observations(m)
    for observation in observations:
        metrics = observation["metrics"]
        metrics["ndcg_at_10"] = {
            **metrics["ndcg_at_10"],
            "total_slates": 100,
            "scored_slates": 1,
            "skipped_zero_click_slates": 99,
            "coverage": 0.01,
        }

    assert m.summarize_observations(observations)["checks"]["all_coverage_valid"] is False


def test_summary_rejects_mixed_evaluation_identity() -> None:
    m = module()
    observations = _supported_observations(m)
    observations[0]["metrics"]["evaluation_id"] = "eval_" + "b" * 64

    with pytest.raises(ValueError, match="personalization_ablation_observations_invalid"):
        m.summarize_observations(observations)


def _supported_observations(m: ModuleType) -> list[dict[str, object]]:
    observations = []
    arms = (*m.COMPARISON_ARMS, *m.ABLATION_FEATURE_GROUPS)
    for world_seed in m.WORLD_SEEDS:
        for split in m.EVALUATION_SPLITS:
            for training_seed in m.TRAINING_SEEDS:
                for arm in arms:
                    ndcg = 0.7
                    if arm == "trending":
                        ndcg = 0.5
                    elif arm == "video_only_lgbm":
                        ndcg = 0.6
                    elif arm == "without_topic_similarity":
                        ndcg = 0.65
                    observations.append(
                        {
                            "world_seed": world_seed,
                            "split": split,
                            "training_seed": training_seed,
                            "arm": arm,
                            "metrics": _metrics(ndcg),
                        }
                    )
    return observations


def _metrics(ndcg: float) -> dict[str, object]:
    ranking = {
        "value": ndcg,
        "total_slates": 30,
        "scored_slates": 30,
        "skipped_zero_click_slates": 0,
        "coverage": 1.0,
    }
    return {
        "evaluation_id": "eval_" + "a" * 64,
        "row_count": 60,
        "ndcg_at_10": ranking,
        "recall_at_10": ranking,
        "ndcg_at_24": ranking,
        "probability": {
            "row_count": 60,
            "positive_count": 30,
            "negative_count": 30,
            "roc_auc": 0.7,
            "pr_auc": 0.7,
            "log_loss": 0.4,
            "brier": 0.2,
            "grouped_roc_auc": {
                "value": 0.7,
                "total_groups": 30,
                "scored_groups": 30,
                "skipped_groups": 0,
                "null_key_rows": 0,
            },
        },
    }
