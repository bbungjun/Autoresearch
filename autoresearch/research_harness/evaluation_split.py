"""평가 snapshot의 결정적 user 분할과 구조 coverage 검증.

[파이프라인] click attribution 뒤와 artifact writer 앞에서 평가 impression을 user 단위로
validation/final holdout으로 분리하고, 각 split의 최소 평가 구조를 검증한다.

[기능] 고정 해시 계약에 따른 불변 split 및 split 통계를 제공한다.

[비책임] click 귀속, slate identity 검증, evaluation ID 생성과 Parquet 기록은 인접
Stage B 모듈이 담당한다.
"""

from datetime import datetime
from hashlib import sha256
from typing import Final

from autoresearch.research_harness.evaluation_snapshot_models import (
    AttributedImpression,
    EvaluationSplit,
    OptionalNonNullRatio,
    SplitContract,
    SplitCounts,
)
from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)


SPLIT_CONTRACT: Final = SplitContract(
    version="user-hash-80-20-v1",
    salt="research-harness-slate-v1:",
    validation_buckets=(0, 1, 2, 3, 4, 5, 6, 7),
    final_holdout_buckets=(8, 9),
)


def split_evaluation_rows(
    rows: tuple[AttributedImpression, ...],
) -> tuple[EvaluationSplit, EvaluationSplit]:
    validation_rows = tuple(
        sorted(
            (
                row
                for row in rows
                if user_bucket(row.user_id) in SPLIT_CONTRACT.validation_buckets
            ),
            key=_row_sort_key,
        )
    )
    final_holdout_rows = tuple(
        sorted(
            (
                row
                for row in rows
                if user_bucket(row.user_id) in SPLIT_CONTRACT.final_holdout_buckets
            ),
            key=_row_sort_key,
        )
    )
    validation = EvaluationSplit(
        name="validation",
        rows=validation_rows,
        user_ids=tuple(sorted({row.user_id for row in validation_rows})),
    )
    final_holdout = EvaluationSplit(
        name="final_holdout",
        rows=final_holdout_rows,
        user_ids=tuple(sorted({row.user_id for row in final_holdout_rows})),
    )
    split_statistics(validation)
    split_statistics(final_holdout)
    return validation, final_holdout


def split_statistics(
    split: EvaluationSplit,
) -> tuple[SplitCounts, OptionalNonNullRatio]:
    row_count = len(split.rows)
    row_user_ids = frozenset(row.user_id for row in split.rows)
    slate_ids = frozenset(row.slate_id for row in split.rows)
    clicked_row_count = sum(row.clicked for row in split.rows)
    click_positive_slate_ids = frozenset(
        row.slate_id for row in split.rows if row.clicked
    )
    missing_metrics = tuple(
        metric
        for metric, present in (
            ("user", bool(split.user_ids)),
            ("slate", bool(slate_ids)),
            ("click_positive_slate", bool(click_positive_slate_ids)),
            ("clicked_impression", clicked_row_count != 0),
            ("non_clicked_impression", clicked_row_count != row_count),
        )
        if not present
    )
    if missing_metrics:
        raise EvaluationSnapshotError(
            code=SnapshotErrorCode.SPLIT_COVERAGE_INSUFFICIENT,
            stage=f"split_coverage:{split.name}:{','.join(missing_metrics)}",
            count=row_count,
        )
    counts = SplitCounts(
        user_count=len(row_user_ids),
        slate_count=len(slate_ids),
        row_count=row_count,
        clicked_row_count=clicked_row_count,
        click_positive_slate_count=len(click_positive_slate_ids),
        click_positive_slate_ratio=len(click_positive_slate_ids) / len(slate_ids),
        mean_slate_size=row_count / len(slate_ids),
    )
    ratios = OptionalNonNullRatio(
        candidate_source=sum(
            row.candidate_source is not None for row in split.rows
        )
        / row_count,
        original_rank=sum(row.original_rank is not None for row in split.rows)
        / row_count,
    )
    return counts, ratios


def user_bucket(user_id: str) -> int:
    payload = f"{SPLIT_CONTRACT.salt}{user_id}".encode("utf-8")
    return int(sha256(payload).hexdigest()[:8], 16) % 10


def _row_sort_key(row: AttributedImpression) -> tuple[str, str, datetime, str]:
    return (row.user_id, row.slate_id, row.event_timestamp, row.video_id)
