"""실제 candidate v2 Parquet가 로컬 피처 interface로 이어지는지 검증한다.

테스트 임베딩은 계산 배선만 검증하며 실제 모델 추론이나 품질의 근거가 아니다.
"""

from collections.abc import Sequence
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.feature_engineering.model_contract import MODEL_FEATURE_COLUMNS
from autoresearch.research_harness.candidate_data_view import (
    materialize_candidate_data_view_v2,
    prepare_candidate_metadata,
)
from autoresearch.research_harness.fixture_models import CandidateDataViewRequest
from tests.research_harness.test_workspace import candidate_fixture as candidate_fixture


class WiringEmbedding:
    """모델 배선 검증을 위한 결정적 adapter; 의미 유사도 모델이 아니다."""

    def encode(self, texts: Sequence[str], *, role: str) -> np.ndarray:
        assert role in ("query", "document")
        return np.tile(np.array([0.6, 0.8]), (len(texts), 1))


def test_published_fixture_builds_training_and_prediction_features(
    candidate_fixture, tmp_path: Path,
) -> None:
    from autoresearch.research_harness.local_features import build_local_features

    fixture, source = candidate_fixture
    metadata = prepare_candidate_metadata(fixture.judge, source=source)
    view = materialize_candidate_data_view_v2(
        CandidateDataViewRequest(fixture.judge, tmp_path), source=source, metadata=metadata,
    )
    # ParquetFile reads the file schema, without inventing a Hive dt column.
    history = pa.concat_tables([
        pq.ParquetFile(view.root / receipt.relative_path).read()
        for receipt in view.manifest.history_partitions
    ])
    slate = pq.ParquetFile(view.root / "slate.parquet").read()
    users = pq.ParquetFile(view.root / "metadata/users.parquet").read()
    videos = pq.ParquetFile(view.root / "metadata/videos.parquet").read()
    args = dict(
        history=history, users=users, videos=videos, embedding=WiringEmbedding(),
        evaluation_start_date=view.manifest.evaluation_start_date,
        history_start_date=min(item.dt for item in view.manifest.history_partitions),
    )
    predicted = build_local_features(slate, **args)
    assert predicted.features.column_names == list(MODEL_FEATURE_COLUMNS)
    assert len(predicted.features) == len(slate) > 0
    assert all(column.null_count == 0 for column in predicted.features.columns)
    assert not any(predicted.diagnostics["history_7d_complete"].to_pylist())
    assert not any(predicted.diagnostics["history_30d_complete"].to_pylist())
    assert not all(predicted.diagnostics["user_metadata_missing"].to_pylist())
    assert not all(predicted.diagnostics["video_metadata_missing"].to_pylist())

    # Independent raw-event count checks: no product aggregation helper for expected values.
    rows = history.to_pylist()
    kst = ZoneInfo("Asia/Seoul")
    for request, actual in zip(slate.to_pylist()[:10], predicted.features.to_pylist()[:10]):
        day = datetime.combine(request["event_timestamp"].astimezone(kst).date(), time(), kst)
        recent = [row for row in rows if row["user_id"] == request["user_id"]
                  and day - timedelta(days=7) <= row["event_timestamp"] < day]
        assert actual["total_event_count_7d"] == len(recent)
        assert actual["recent_view_count_7d"] == sum(row["event_type"] == "view" for row in recent)
        assert actual["recent_watch_time_7d"] == sum(
            row["watch_time_sec"] for row in recent if row["event_type"] == "view"
        )

    # The first history day has no earlier behavioral history; later clicks must not leak.
    first_day = args["history_start_date"]
    training_requests = pa.Table.from_pylist([
        row for row in rows if row["event_type"] == "impression"
        and row["event_timestamp"].astimezone(kst).date() == first_day
    ], schema=history.schema)
    trained = build_local_features(training_requests, **args)
    assert len(trained.features) > 0
    assert set(trained.features["total_event_count_7d"].to_pylist()) == {0}
    assert set(trained.features["historical_category_affinity"].to_pylist()) == {"unknown"}
    assert any(trained.diagnostics["user_metadata_missing"].to_pylist())
