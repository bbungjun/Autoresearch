from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from autoresearch.research_harness.evaluation_snapshot_models import (
    AttributedImpression,
    SplitContract,
    SplitCounts,
)
from autoresearch.research_harness.evaluation_split import (
    SPLIT_CONTRACT,
    split_evaluation_rows,
    split_statistics,
    user_bucket,
)


def _row(user_id: str, slate_id: str, clicked: bool) -> AttributedImpression:
    return AttributedImpression(
        slate_id=slate_id,
        user_id=user_id,
        video_id=f"{slate_id}-video-{int(clicked)}",
        event_timestamp=datetime(2026, 9, 1, tzinfo=UTC),
        source_event_id=f"{slate_id}-event-{int(clicked)}",
        clicked=clicked,
        original_rank=1 if clicked else None,
        candidate_source="model" if clicked else None,
    )


def test_split_evaluation_rows_uses_independently_precomputed_fixed_user_buckets() -> None:
    rows = (
        _row("vector-user-00", "validation-slate", True),
        _row("vector-user-00", "validation-slate", False),
        _row("fixture-user-04", "final-slate", True),
        _row("fixture-user-04", "final-slate", False),
    )

    validation, final_holdout = split_evaluation_rows(rows)

    assert validation.user_ids == ("vector-user-00",)
    assert final_holdout.user_ids == ("fixture-user-04",)


@pytest.mark.parametrize(
    ("user_id", "expected_bucket"),
    (
        ("vector-user-00", 7),
        ("vector-user-01", 0),
        ("vector-user-05", 1),
        ("fixture-user-04", 8),
        ("fixture-user-15", 9),
    ),
)
def test_user_bucket_matches_independently_precomputed_hash_vectors(
    user_id: str,
    expected_bucket: int,
) -> None:
    assert user_bucket(user_id) == expected_bucket


def test_split_evaluation_rows_keeps_each_user_in_exactly_one_split() -> None:
    rows = (
        _row("vector-user-00", "validation-slate-a", True),
        _row("vector-user-00", "validation-slate-a", False),
        _row("fixture-user-04", "final-slate-a", True),
        _row("fixture-user-04", "final-slate-a", False),
    )

    validation, final_holdout = split_evaluation_rows(rows)

    assert set(validation.user_ids).isdisjoint(final_holdout.user_ids)
    assert {row.user_id for row in validation.rows} == set(validation.user_ids)
    assert {row.user_id for row in final_holdout.rows} == set(final_holdout.user_ids)


def test_split_evaluation_rows_is_deterministic_when_input_order_is_reversed() -> None:
    rows = (
        _row("vector-user-00", "validation-slate-a", True),
        _row("vector-user-00", "validation-slate-a", False),
        _row("fixture-user-04", "final-slate-a", True),
        _row("fixture-user-04", "final-slate-a", False),
    )

    forward = split_evaluation_rows(rows)
    reversed_result = split_evaluation_rows(tuple(reversed(rows)))

    assert reversed_result == forward


def test_split_statistics_returns_exact_immutable_counts_and_optional_ratios() -> None:
    rows = (
        _row("vector-user-00", "validation-slate", True),
        _row("vector-user-00", "validation-slate", False),
        _row("fixture-user-04", "final-slate", True),
        _row("fixture-user-04", "final-slate", False),
    )
    validation, _ = split_evaluation_rows(rows)

    counts, ratios = split_statistics(validation)

    assert counts == SplitCounts(1, 1, 2, 1, 1, 1.0, 2.0)
    assert ratios.candidate_source == 0.5
    assert ratios.original_rank == 0.5
    with pytest.raises(FrozenInstanceError):
        counts.user_count = 2
    with pytest.raises(ValidationError):
        ratios.candidate_source = 1.0


def test_split_statistics_returns_immutable_count_and_ratio_values() -> None:
    rows = (
        _row("vector-user-00", "validation-slate", True),
        _row("vector-user-00", "validation-slate", False),
        _row("fixture-user-04", "final-slate", True),
        _row("fixture-user-04", "final-slate", False),
    )
    validation, _ = split_evaluation_rows(rows)

    counts, ratios = split_statistics(validation)

    with pytest.raises(FrozenInstanceError):
        counts.user_count = 2
    with pytest.raises(ValidationError):
        ratios.candidate_source = 1.0


def test_split_contract_locks_version_salt_and_bucket_sets() -> None:
    expected = SplitContract(
        version="user-hash-80-20-v1",
        salt="research-harness-slate-v1:",
        validation_buckets=(0, 1, 2, 3, 4, 5, 6, 7),
        final_holdout_buckets=(8, 9),
    )

    assert SPLIT_CONTRACT == expected
