from contextlib import AbstractContextManager
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.research_harness import build_evaluation_snapshot
from autoresearch.research_harness.evaluation_snapshot_models import (
    EvaluationSnapshotManifest,
)
from autoresearch.research_harness.evaluation_source import ArrowActionLogSource
from tests.research_harness.test_slate import _snapshot_request


class _RecordingSource:
    def __init__(self, root: str) -> None:
        self._delegate = ArrowActionLogSource.from_root(root)
        self.opened_dates: list[date] = []

    @property
    def opaque_root(self) -> str:
        return self._delegate.opaque_root

    def partition_uri(self, dt: date) -> str:
        return self._delegate.partition_uri(dt)

    def open_partition(self, dt: date) -> AbstractContextManager[pa.NativeFile]:
        self.opened_dates.append(dt)
        return self._delegate.open_partition(dt)


def test_reverse_ordered_source_rebuild_reuses_same_identity(tmp_path: Path) -> None:
    # Given
    request = _snapshot_request(tmp_path)
    evaluation_path = (
        Path(request.action_log_root) / "dt=2026-09-01" / "part-0.parquet"
    )
    table = pq.read_table(evaluation_path)
    pq.write_table(table.take(list(reversed(range(table.num_rows)))), evaluation_path)
    first = build_evaluation_snapshot(request)

    # When
    second = build_evaluation_snapshot(request)

    # Then
    assert (
        second.validation_id,
        second.final_holdout_id,
        second.snapshot_fingerprint,
        second.reused,
    ) == (
        first.validation_id,
        first.final_holdout_id,
        first.snapshot_fingerprint,
        True,
    )


def test_manifest_preserves_history_output_and_scan_boundaries(tmp_path: Path) -> None:
    # Given
    request = _snapshot_request(tmp_path)

    # When
    receipt = build_evaluation_snapshot(request)
    manifest = EvaluationSnapshotManifest.model_validate_json(
        (receipt.target_path / "manifest.json").read_bytes()
    )
    output_dates = {
        timestamp.date()
        for split_name in ("validation", "final_holdout")
        for timestamp in pq.read_table(
            receipt.target_path / split_name / "slate.parquet"
        ).column("event_timestamp").to_pylist()
    }

    # Then
    assert tuple(item.dt for item in manifest.window.candidate_history_partitions) == (
        request.history_start_date,
    )
    assert tuple(item.dt for item in manifest.source.partitions) == (
        request.history_start_date,
        request.evaluation_start_date,
        date(2026, 9, 2),
    )
    assert manifest.window.label_scan_end_date == date(2026, 9, 2)
    assert manifest.window.complete_history_label_end_date == date(2026, 8, 30)
    assert output_dates == {request.evaluation_start_date}


def test_optional_action_log_source_is_used_for_every_required_partition(
    tmp_path: Path,
) -> None:
    # Given
    request = _snapshot_request(tmp_path)
    source = _RecordingSource(request.action_log_root)

    # When
    receipt = build_evaluation_snapshot(request, source=source)

    # Then
    assert tuple(source.opened_dates) == (
        date(2026, 8, 31),
        date(2026, 9, 1),
        date(2026, 9, 2),
    )
    assert receipt.target_path.is_dir()
