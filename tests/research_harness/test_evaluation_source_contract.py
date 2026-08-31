from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from autoresearch.research_harness import evaluation_source
from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.evaluation_source import ArrowActionLogSource, load_required_partitions
from tests.research_harness.test_evaluation_source_fixtures import (
    event_for,
    request_for,
    table_for,
)


def _parquet_bytes(partition_date: date) -> bytes:
    sink = pa.BufferOutputStream()
    slate_id = (
        "slt_20260901_0123456789abcdef01234567"
        if partition_date == date(2026, 9, 1)
        else None
    )
    table = (
        table_for((event_for(partition_date, slate_id=slate_id),))
        if partition_date != date(2026, 9, 2)
        else table_for(())
    )
    pq.write_table(table, sink)
    return sink.getvalue().to_pybytes()


class _MemorySource:
    def __init__(self) -> None:
        self.opaque_root = "memory://action-log"
        self.open_counts: dict[date, int] = {}
        self.handles: list[pa.BufferReader] = []

    def partition_uri(self, dt: date) -> str:
        return f"{self.opaque_root}/dt={dt.isoformat()}/part-0.parquet"

    def open_partition(self, dt: date) -> pa.BufferReader:
        self.open_counts[dt] = self.open_counts.get(dt, 0) + 1
        handle = pa.BufferReader(_parquet_bytes(dt))
        self.handles.append(handle)
        return handle


def test_loader_opens_each_partition_once_and_closes_context(tmp_path: Path) -> None:
    source = _MemorySource()

    loaded = load_required_partitions(
        request_for(source.opaque_root, tmp_path / "output"),
        source=source,
    )

    assert len(loaded) == 3
    assert source.open_counts == {
        date(2026, 8, 31): 1,
        date(2026, 9, 1): 1,
        date(2026, 9, 2): 1,
    }
    assert all(handle.closed for handle in source.handles)


class _SpyFilesystem:
    def __init__(self) -> None:
        self.opened_paths: list[str] = []

    def open_input_file(self, path: str) -> pa.BufferReader:
        self.opened_paths.append(path)
        return pa.BufferReader(_parquet_bytes(date.fromisoformat(path.split("dt=")[1][:10])))


class _SpyFactory:
    calls: list[str] = []
    filesystem = _SpyFilesystem()

    @classmethod
    def from_uri(cls, root: str) -> tuple[_SpyFilesystem, str]:
        cls.calls.append(root)
        return cls.filesystem, "bucket/root"


class _FailingFactory:
    @classmethod
    def from_uri(cls, root: str) -> tuple[_SpyFilesystem, str]:
        raise pa.ArrowInvalid("private-root")


def test_arrow_source_resolves_gcs_root_once_and_preserves_uri(monkeypatch) -> None:
    _SpyFactory.calls.clear()
    _SpyFactory.filesystem.opened_paths.clear()
    monkeypatch.setattr(evaluation_source.pafs, "FileSystem", _SpyFactory)

    source = ArrowActionLogSource.from_root("gs://bucket/root")
    uri = source.partition_uri(date(2026, 9, 1))
    with source.open_partition(date(2026, 9, 1)) as handle:
        assert handle.read(4) == b"PAR1"

    assert _SpyFactory.calls == ["gs://bucket/root"]
    assert uri == "gs://bucket/root/dt=2026-09-01/part-0.parquet"
    assert _SpyFactory.filesystem.opened_paths == ["bucket/root/dt=2026-09-01/part-0.parquet"]


def test_arrow_source_preserves_file_uri_without_pathlib(monkeypatch) -> None:
    monkeypatch.setattr(evaluation_source.pafs, "FileSystem", _SpyFactory)

    observed = ArrowActionLogSource.from_root("file:///C:/action-log").partition_uri(
        date(2026, 9, 1)
    )

    assert observed == "file:///C:/action-log/dt=2026-09-01/part-0.parquet"


def test_arrow_source_normalizes_windows_absolute_root_without_pathlib(monkeypatch) -> None:
    monkeypatch.setattr(evaluation_source.pafs, "FileSystem", _SpyFactory)

    observed = ArrowActionLogSource.from_root("C:\\action-log").partition_uri(date(2026, 9, 1))

    assert observed == "C:/action-log/dt=2026-09-01/part-0.parquet"


def test_arrow_source_preserves_posix_absolute_root_without_pathlib(monkeypatch) -> None:
    monkeypatch.setattr(evaluation_source.pafs, "FileSystem", _SpyFactory)

    observed = ArrowActionLogSource.from_root("/var/action-log").partition_uri(date(2026, 9, 1))

    assert observed == "/var/action-log/dt=2026-09-01/part-0.parquet"


def test_loader_sanitizes_source_resolution_errors(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(evaluation_source.pafs, "FileSystem", _FailingFactory)

    with pytest.raises(EvaluationSnapshotError) as captured:
        load_required_partitions(request_for("gs://private/root", tmp_path / "output"))

    assert captured.value.code is SnapshotErrorCode.SOURCE_PARTITION_MISSING
    assert "private" not in str(captured.value)
