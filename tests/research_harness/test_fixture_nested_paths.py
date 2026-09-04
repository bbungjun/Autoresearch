"""중첩된 Judge fixture와 candidate 입력 경계의 content identity를 검증한다."""

from datetime import date
from hashlib import sha256
import os
from pathlib import Path
import shutil
import stat
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from autoresearch.action_log_generation.pipeline import ActionLogGenerationError
from autoresearch.research_harness.candidate_data_view import (
    materialize_candidate_data_view,
    materialize_candidate_data_view_v2,
    prepare_candidate_metadata,
    prepare_final_candidate_metadata,
)
import autoresearch.research_harness.local_evaluation_fixture as fixture_module
from autoresearch.research_harness.fixture_models import (
    CandidateDataViewReceipt,
    CandidateDataViewRequest,
    FixtureDescriptor,
    LocalEvaluationFixtureRequest,
    LocalEvaluationFixtureReceipt,
    PreparedCandidateMetadata,
)
from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode
from autoresearch.research_harness.local_evaluation_fixture import (
    FixtureActionLogSource,
    build_local_evaluation_fixture,
)


def _test_io_path(path: Path) -> Path:
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        return Path(f"\\\\?\\{path.absolute()}")
    return path


def _artifact_hashes(root: Path) -> dict[str, str]:
    io_root = _test_io_path(root)
    return {
        path.relative_to(io_root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in io_root.rglob("*")
        if path.is_file()
    }


def _nested_root(tmp_path: Path, name: str, minimum_length: int) -> Path:
    root = tmp_path / name
    root /= "x" * max(1, minimum_length - len(str(root)) - 1)
    root.mkdir(parents=True)
    assert len(str(root)) >= minimum_length
    return root


def _short_root(name: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"ar77-{name}-"))


def _remove_nested_root(root: Path) -> None:
    io_root = _test_io_path(root)
    if io_root.exists():
        shutil.rmtree(io_root)


def _source(fixture: LocalEvaluationFixtureReceipt) -> FixtureActionLogSource:
    return FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)


def _materialize_view(
    contract_version: str,
    fixture: LocalEvaluationFixtureReceipt,
    source: FixtureActionLogSource,
    destination_root: Path,
    metadata: PreparedCandidateMetadata | None,
) -> CandidateDataViewReceipt:
    destination_root.mkdir()
    request = CandidateDataViewRequest(fixture.judge, destination_root)
    if contract_version == "v1":
        return materialize_candidate_data_view(request, source=source)
    assert metadata is not None
    return materialize_candidate_data_view_v2(
        request,
        source=source,
        metadata=metadata,
    )


@pytest.mark.parametrize("minimum_root_length", (130, 153))
def test_nested_fixture_build_and_reuse_match_short_root(
    tmp_path: Path, minimum_root_length: int,
) -> None:
    short_root = tmp_path / "short"
    short_root.mkdir()
    nested_root = tmp_path / "nested"
    nested_root /= "x" * max(1, minimum_root_length - len(str(nested_root)) - 1)
    nested_root.mkdir(parents=True)
    assert len(str(nested_root)) >= minimum_root_length
    evaluation_date = date(2026, 9, 1)
    short = build_local_evaluation_fixture(
        LocalEvaluationFixtureRequest(short_root, evaluation_date, 1937)
    )
    request = LocalEvaluationFixtureRequest(nested_root, evaluation_date, 1937)
    nested = build_local_evaluation_fixture(request)
    reused = build_local_evaluation_fixture(request)

    assert not short.reused and not nested.reused and reused.reused
    assert reused.judge == nested.judge
    assert reused.fixture_root == nested.fixture_root
    assert nested.descriptor_sha256 == short.descriptor_sha256
    assert nested.action_log_partitions == short.action_log_partitions
    assert nested.judge.snapshot_fingerprint == short.judge.snapshot_fingerprint
    assert nested.judge.manifest_sha256 == short.judge.manifest_sha256
    assert nested.judge.validation_id == short.judge.validation_id
    assert nested.judge.final_holdout_id == short.judge.final_holdout_id
    assert len(str(nested.judge.snapshot_root / "validation" / "slate.parquet")) > 260
    for receipt in (short, nested, reused):
        for path in (receipt.fixture_root, receipt.descriptor_path, receipt.judge.snapshot_root):
            assert not str(path).startswith("\\\\?\\")
    assert _artifact_hashes(nested.fixture_root) == _artifact_hashes(short.fixture_root)
    assert not tuple((nested_root / "fixtures" / "by-hash").glob(".staging-*"))


def test_failed_nested_fixture_cleans_owned_staging_and_preserves_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state_root = tmp_path / "nested"
    state_root /= "x" * max(1, 153 - len(str(state_root)) - 1)
    state_root.mkdir(parents=True)
    build_staged = fixture_module._build_staged_fixture
    generated: list[Path] = []

    def fail_after_generation(
        staging: Path, descriptor: FixtureDescriptor, digest: str,
    ) -> None:
        build_staged(staging, descriptor, digest)
        generated.append(staging)
        raise ActionLogGenerationError("private-fixture-path")

    monkeypatch.setattr(fixture_module, "_build_staged_fixture", fail_after_generation)
    with pytest.raises(StageCError) as caught:
        build_local_evaluation_fixture(
            LocalEvaluationFixtureRequest(state_root, date(2026, 9, 1), 1937)
        )

    assert generated
    assert caught.value.code == StageCErrorCode.FIXTURE_STATE_CONFLICT
    assert caught.value.stage == "fixture_build"
    assert "private-fixture-path" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert all(not _test_io_path(staging).exists() for staging in generated)
    assert not any(path.is_dir() for path in (state_root / "fixtures" / "by-hash").iterdir())
    assert "fixture_staging_cleanup_failed" not in caplog.text


@pytest.mark.skipif(os.name != "nt", reason="Windows long-path regression")
@pytest.mark.parametrize("split", ("validation", "final"))
def test_nested_fixture_metadata_matches_short_root(
    tmp_path: Path, split: str,
) -> None:
    short_root = _short_root(f"metadata-{split}")
    nested_root = _nested_root(tmp_path, f"nested-metadata-{split}", 130)
    request_date = date(2026, 9, 1)
    try:
        short = build_local_evaluation_fixture(
            LocalEvaluationFixtureRequest(short_root, request_date, 1937)
        )
        nested = build_local_evaluation_fixture(
            LocalEvaluationFixtureRequest(nested_root, request_date, 1937)
        )
        prepare = (
            prepare_candidate_metadata
            if split == "validation"
            else prepare_final_candidate_metadata
        )
        short_metadata = prepare(short.judge, source=_source(short))
        nested_metadata = prepare(nested.judge, source=_source(nested))

        assert nested_metadata == short_metadata
    finally:
        _remove_nested_root(short_root)
        _remove_nested_root(nested_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows long-path regression")
@pytest.mark.parametrize("contract_version", ("v1", "v2"))
def test_nested_fixture_candidate_view_matches_short_root(
    tmp_path: Path, contract_version: str,
) -> None:
    short_root = _short_root(f"view-{contract_version}")
    nested_root = _nested_root(tmp_path, f"nested-view-{contract_version}", 130)
    request_date = date(2026, 9, 1)
    try:
        short = build_local_evaluation_fixture(
            LocalEvaluationFixtureRequest(short_root, request_date, 1937)
        )
        nested = build_local_evaluation_fixture(
            LocalEvaluationFixtureRequest(nested_root, request_date, 1937)
        )
        short_source = _source(short)
        nested_source = _source(nested)
        metadata = (
            prepare_candidate_metadata(short.judge, source=short_source)
            if contract_version == "v2"
            else None
        )
        short_destination = tmp_path / f"short-output-{contract_version}"
        nested_destination = tmp_path / f"nested-output-{contract_version}"
        short_view = _materialize_view(
            contract_version,
            short,
            short_source,
            short_destination,
            metadata,
        )
        nested_view = _materialize_view(
            contract_version,
            nested,
            nested_source,
            nested_destination,
            metadata,
        )
        reused = (
            materialize_candidate_data_view(
                CandidateDataViewRequest(nested.judge, nested_destination),
                source=nested_source,
            )
            if contract_version == "v1"
            else materialize_candidate_data_view_v2(
                CandidateDataViewRequest(nested.judge, nested_destination),
                source=nested_source,
                metadata=metadata,
            )
        )

        assert not short_view.reused and not nested_view.reused and reused.reused
        assert nested_view.manifest == short_view.manifest
        assert nested_view.manifest_sha256 == short_view.manifest_sha256
        assert reused.manifest == nested_view.manifest
        assert reused.manifest_sha256 == nested_view.manifest_sha256
        assert _artifact_hashes(nested_view.root) == _artifact_hashes(short_view.root)
        for view in (short_view, nested_view, reused):
            assert not str(view.root).startswith("\\\\?\\")
    finally:
        _remove_nested_root(short_root)
        _remove_nested_root(nested_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows long-path regression")
def test_nested_fixture_source_opens_partition_over_windows_limit(
    tmp_path: Path,
) -> None:
    nested_root = _nested_root(tmp_path, "nested-source", 153)
    try:
        fixture = build_local_evaluation_fixture(
            LocalEvaluationFixtureRequest(nested_root, date(2026, 9, 1), 1937)
        )
        source = _source(fixture)
        receipt = fixture.action_log_partitions[0]
        partition_path = source._physical_partition_path(receipt.dt)
        io_partition = _test_io_path(partition_path)
        source_stat = io_partition.stat()

        assert len(str(partition_path)) > 260
        assert not str(partition_path).startswith("\\\\?\\")
        assert stat.S_ISREG(source_stat.st_mode)
        assert source_stat.st_nlink == 1
        with source.open_partition(receipt.dt) as handle:
            opened_stat = os.fstat(handle.fileno())
            payload = handle.read()

        assert (opened_stat.st_dev, opened_stat.st_ino) == (
            source_stat.st_dev,
            source_stat.st_ino,
        )
        assert stat.S_ISREG(opened_stat.st_mode)
        assert opened_stat.st_nlink == 1
        assert sha256(payload).hexdigest() == receipt.sha256
        assert pq.read_table(pa.BufferReader(payload)).num_rows == receipt.rows
    finally:
        _remove_nested_root(nested_root)
