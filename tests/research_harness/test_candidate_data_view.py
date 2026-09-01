from __future__ import annotations

from datetime import date
from dataclasses import replace
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path
import subprocess

import pyarrow.parquet as pq
import pyarrow as pa
import pytest

from autoresearch.research_harness import (
    CandidateDataViewRequest,
    LocalEvaluationFixtureRequest,
    StageCError,
    StageCErrorCode,
    build_local_evaluation_fixture,
    materialize_candidate_data_view,
)
from autoresearch.research_harness.evaluation_snapshot_models import (
    EvaluationSnapshotManifest,
)
from autoresearch.research_harness.local_evaluation_fixture import (
    FixtureActionLogSource,
)


EVALUATION_DATE = date(2026, 9, 1)


def _io_path(path: Path) -> Path:
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        return Path(f"\\\\?\\{path.absolute()}")
    return path


def _copy_snapshot(source: Path, destination: Path) -> None:
    manifest = EvaluationSnapshotManifest.model_validate_json(
        _io_path(source / "manifest.json").read_bytes()
    )
    destination.mkdir(parents=True)
    for name in ("manifest.json", "_SUCCESS"):
        (destination / name).write_bytes(_io_path(source / name).read_bytes())
    for artifact in (
        manifest.validation.artifacts.slate,
        manifest.validation.artifacts.labels,
        manifest.final_holdout.artifacts.slate,
        manifest.final_holdout.artifacts.labels,
    ):
        target = destination.joinpath(*artifact.relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            _io_path(source.joinpath(*artifact.relative_path.split("/"))).read_bytes()
        )


def _make_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip("directory junction creation is unavailable")
    else:
        os.symlink(target, link, target_is_directory=True)


def _remove_directory_link(link: Path) -> None:
    link.rmdir() if os.name == "nt" else link.unlink()


def test_materializer_has_validation_only_public_interface() -> None:
    assert inspect.signature(materialize_candidate_data_view) == inspect.Signature(
        parameters=(
            inspect.Parameter(
                "request",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation="CandidateDataViewRequest",
            ),
            inspect.Parameter(
                "source",
                inspect.Parameter.KEYWORD_ONLY,
                annotation="ActionLogSource",
            ),
        ),
        return_annotation="CandidateDataViewReceipt",
    )
    assert tuple(inspect.signature(CandidateDataViewRequest).parameters) == (
        "judge",
        "destination_root",
    )


@pytest.fixture(scope="module")
def candidate_fixture(tmp_path_factory: pytest.TempPathFactory):
    state_root = tmp_path_factory.mktemp("candidate-judge")
    receipt = build_local_evaluation_fixture(
        LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 1937)
    )
    return state_root, receipt


def test_materializes_only_validation_slate_and_candidate_history(
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, fixture = candidate_fixture
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
    receipt = materialize_candidate_data_view(
        CandidateDataViewRequest(fixture.judge, tmp_path), source=source
    )
    snapshot = EvaluationSnapshotManifest.model_validate_json(
        _io_path(fixture.judge.snapshot_root / "manifest.json").read_bytes()
    )

    assert receipt.root == tmp_path / "harness_in"
    assert receipt.reused is False
    assert receipt.manifest.evaluation_id == fixture.judge.validation_id
    assert receipt.manifest.slate.relative_path == "slate.parquet"
    assert tuple(part.dt for part in receipt.manifest.history_partitions) == (
        EVALUATION_DATE.replace(day=30, month=8),
        EVALUATION_DATE.replace(day=31, month=8),
    )
    assert (receipt.root / "slate.parquet").read_bytes() == (
        _io_path(
            fixture.judge.snapshot_root
            / snapshot.validation.artifacts.slate.relative_path
        )
    ).read_bytes()
    assert pq.read_metadata(receipt.root / "slate.parquet").num_rows == receipt.manifest.slate.rows
    assert sorted(
        path.relative_to(receipt.root).as_posix()
        for path in receipt.root.rglob("*")
        if path.is_file()
    ) == [
        "candidate-view.json",
        "history/action_log/dt=2026-08-30/part-0.parquet",
        "history/action_log/dt=2026-08-31/part-0.parquet",
        "slate.parquet",
    ]
    manifest_bytes = (receipt.root / "candidate-view.json").read_bytes()
    assert sha256(manifest_bytes).hexdigest() == receipt.manifest_sha256
    assert json.loads(manifest_bytes) == receipt.manifest.model_dump(mode="json")


def test_complete_identical_view_is_reused(candidate_fixture, tmp_path: Path) -> None:
    _, fixture = candidate_fixture
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
    request = CandidateDataViewRequest(fixture.judge, tmp_path)
    first = materialize_candidate_data_view(request, source=source)
    second = materialize_candidate_data_view(request, source=source)
    assert first.reused is False
    assert second.reused is True
    assert second.manifest == first.manifest


def test_reuse_still_revalidates_source_bytes(candidate_fixture, tmp_path: Path) -> None:
    _, fixture = candidate_fixture
    copied_fixture = tmp_path / "copied-fixture"
    for partition in fixture.action_log_partitions[:2]:
        source_path = (
            fixture.fixture_root
            / "action_log"
            / f"dt={partition.dt.isoformat()}"
            / "part-0.parquet"
        )
        target_path = (
            copied_fixture
            / "action_log"
            / f"dt={partition.dt.isoformat()}"
            / "part-0.parquet"
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(_io_path(source_path).read_bytes())
    source = FixtureActionLogSource(copied_fixture, fixture.descriptor_sha256)
    destination = tmp_path / "candidate"
    destination.mkdir()
    materialize_candidate_data_view(
        CandidateDataViewRequest(fixture.judge, destination), source=source
    )
    tampered = copied_fixture / "action_log/dt=2026-08-30/part-0.parquet"
    tampered.write_bytes(b"tampered")
    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, destination), source=source
        )
    assert captured.value.code is StageCErrorCode.JUDGE_HANDOFF_INVALID


def test_candidate_payload_does_not_disclose_judge_only_values(
    candidate_fixture,
    tmp_path: Path,
) -> None:
    state_root, fixture = candidate_fixture
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
    receipt = materialize_candidate_data_view(
        CandidateDataViewRequest(fixture.judge, tmp_path), source=source
    )
    payload = "\n".join(
        [
            repr(receipt),
            repr(receipt.manifest.model_dump(mode="json")),
            *(path.name for path in receipt.root.rglob("*")),
            (receipt.root / "candidate-view.json").read_text(encoding="utf-8"),
            *(
                path.read_bytes().decode("latin-1")
                for path in receipt.root.rglob("*")
                if path.is_file()
            ),
        ]
    )
    snapshot = EvaluationSnapshotManifest.model_validate_json(
        _io_path(fixture.judge.snapshot_root / "manifest.json").read_bytes()
    )
    forbidden = (
        "labels.parquet",
        "final_holdout",
        str(fixture.judge.final_holdout_id),
        str(fixture.judge.snapshot_fingerprint),
        str(state_root),
        fixture.descriptor_sha256,
        snapshot.source.root,
        "fixture_seed",
        "virtual_users",
    )
    assert all(token not in payload for token in forbidden)


class _RecordingSource(FixtureActionLogSource):
    def __init__(self, fixture_root: Path, descriptor_digest: str) -> None:
        super().__init__(fixture_root, descriptor_digest)
        self.opened: list[date] = []

    def open_partition(self, dt: date):
        self.opened.append(dt)
        return super().open_partition(dt)


class _WrongPartitionUriSource(_RecordingSource):
    def partition_uri(self, dt: date) -> str:
        return f"{self.opaque_root}/wrong/dt={dt.isoformat()}"


class _WrongBytesSource(_RecordingSource):
    def open_partition(self, dt: date):
        self.opened.append(dt)
        return pa.BufferReader(b"not parquet")


def test_source_identity_is_checked_before_any_open(candidate_fixture, tmp_path: Path) -> None:
    _, fixture = candidate_fixture
    source = _RecordingSource(fixture.fixture_root, "0" * 64)
    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, tmp_path), source=source
        )
    assert captured.value.code is StageCErrorCode.JUDGE_HANDOFF_INVALID
    assert source.opened == []


def test_source_partition_uri_is_checked_before_any_open(
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, fixture = candidate_fixture
    source = _WrongPartitionUriSource(fixture.fixture_root, fixture.descriptor_sha256)
    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, tmp_path), source=source
        )
    assert captured.value.code is StageCErrorCode.JUDGE_HANDOFF_INVALID
    assert source.opened == []


def test_source_bytes_and_parquet_integrity_are_checked(
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, fixture = candidate_fixture
    source = _WrongBytesSource(fixture.fixture_root, fixture.descriptor_sha256)
    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, tmp_path), source=source
        )
    assert captured.value.code is StageCErrorCode.JUDGE_HANDOFF_INVALID
    assert source.opened == [date(2026, 8, 30)]


def test_only_manifest_history_dates_are_opened(candidate_fixture, tmp_path: Path) -> None:
    _, fixture = candidate_fixture
    source = _RecordingSource(fixture.fixture_root, fixture.descriptor_sha256)
    materialize_candidate_data_view(
        CandidateDataViewRequest(fixture.judge, tmp_path), source=source
    )
    assert source.opened == [date(2026, 8, 30), date(2026, 8, 31)]


def test_existing_partial_extra_or_tampered_view_conflicts(
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, fixture = candidate_fixture
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
    target = tmp_path / "harness_in"
    target.mkdir()
    (target / "unexpected").write_bytes(b"x")
    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, tmp_path), source=source
        )
    assert captured.value.code is StageCErrorCode.CANDIDATE_VIEW_CONFLICT


def test_existing_complete_but_tampered_view_conflicts(
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, fixture = candidate_fixture
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
    receipt = materialize_candidate_data_view(
        CandidateDataViewRequest(fixture.judge, tmp_path), source=source
    )
    (receipt.root / "slate.parquet").write_bytes(b"tampered")
    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, tmp_path), source=source
        )
    assert captured.value.code is StageCErrorCode.CANDIDATE_VIEW_CONFLICT


@pytest.mark.parametrize(
    "tamper",
    (
        "marker",
        "manifest",
        "validation/slate.parquet",
        "validation/labels.parquet",
        "final_holdout/slate.parquet",
        "final_holdout/labels.parquet",
    ),
)
def test_judge_snapshot_marker_manifest_and_all_artifacts_are_revalidated(
    candidate_fixture,
    tmp_path: Path,
    tamper: str,
) -> None:
    _, fixture = candidate_fixture
    copied_root = (
        tmp_path
        / "judge"
        / "fixtures"
        / "by-hash"
        / "copied-fixture"
        / "evaluation-snapshots"
        / "by-hash"
        / str(fixture.judge.snapshot_fingerprint)
    )
    _copy_snapshot(fixture.judge.snapshot_root, copied_root)
    target = copied_root / tamper if "/" not in tamper else copied_root.joinpath(*tamper.split("/"))
    if tamper == "marker":
        target = copied_root / "_SUCCESS"
    elif tamper == "manifest":
        target = copied_root / "manifest.json"
    target.write_bytes(target.read_bytes() + b"tamper")
    handoff = replace(fixture.judge, snapshot_root=copied_root)
    destination = tmp_path / "candidate"
    destination.mkdir()
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(handoff, destination), source=source
        )
    assert captured.value.code is StageCErrorCode.JUDGE_HANDOFF_INVALID


def test_destination_must_be_safe_and_disjoint_from_judge(candidate_fixture, tmp_path: Path) -> None:
    _, fixture = candidate_fixture
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
    invalid_roots = (Path("relative"), tmp_path / "missing", fixture.fixture_root)
    for root in invalid_roots:
        with pytest.raises(StageCError) as captured:
            materialize_candidate_data_view(
                CandidateDataViewRequest(fixture.judge, root), source=source
            )
        assert captured.value.code is StageCErrorCode.FIXTURE_REQUEST_INVALID


def test_destination_reparse_component_is_rejected(candidate_fixture, tmp_path: Path) -> None:
    _, fixture = candidate_fixture
    physical = tmp_path / "physical"
    linked = tmp_path / "linked"
    physical.mkdir()
    _make_directory_link(linked, physical)
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
    try:
        with pytest.raises(StageCError) as captured:
            materialize_candidate_data_view(
                CandidateDataViewRequest(fixture.judge, linked), source=source
            )
        assert captured.value.code is StageCErrorCode.FIXTURE_REQUEST_INVALID
    finally:
        _remove_directory_link(linked)


def test_existing_target_reparse_directory_is_rejected(candidate_fixture, tmp_path: Path) -> None:
    _, fixture = candidate_fixture
    physical = tmp_path / "physical"
    physical.mkdir()
    target = tmp_path / "harness_in"
    _make_directory_link(target, physical)
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
    try:
        with pytest.raises(StageCError) as captured:
            materialize_candidate_data_view(
                CandidateDataViewRequest(fixture.judge, tmp_path), source=source
            )
        assert captured.value.code is StageCErrorCode.CANDIDATE_VIEW_CONFLICT
    finally:
        _remove_directory_link(target)


def test_source_reparse_component_is_rejected(candidate_fixture, tmp_path: Path) -> None:
    _, fixture = candidate_fixture
    alias_fixture = tmp_path / "alias-fixture"
    alias_fixture.mkdir()
    _make_directory_link(alias_fixture / "action_log", fixture.fixture_root / "action_log")
    source = FixtureActionLogSource(alias_fixture, fixture.descriptor_sha256)
    destination = tmp_path / "candidate"
    destination.mkdir()
    try:
        with pytest.raises(StageCError) as captured:
            materialize_candidate_data_view(
                CandidateDataViewRequest(fixture.judge, destination), source=source
            )
        assert captured.value.code is StageCErrorCode.JUDGE_HANDOFF_INVALID
    finally:
        _remove_directory_link(alias_fixture / "action_log")


def test_source_hardlink_alias_is_rejected(candidate_fixture, tmp_path: Path) -> None:
    _, fixture = candidate_fixture
    alias_fixture = tmp_path / "alias-fixture"
    for partition in fixture.action_log_partitions[:2]:
        source_path = (
            fixture.fixture_root
            / "action_log"
            / f"dt={partition.dt.isoformat()}"
            / "part-0.parquet"
        )
        target_path = (
            alias_fixture
            / "action_log"
            / f"dt={partition.dt.isoformat()}"
            / "part-0.parquet"
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(_io_path(source_path).read_bytes())
        if partition == fixture.action_log_partitions[0]:
            os.link(target_path, tmp_path / "hardlink-alias.parquet")
    source = FixtureActionLogSource(alias_fixture, fixture.descriptor_sha256)
    destination = tmp_path / "candidate"
    destination.mkdir()
    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, destination), source=source
        )
    assert captured.value.code is StageCErrorCode.JUDGE_HANDOFF_INVALID


def test_existing_target_hardlink_is_rejected(candidate_fixture, tmp_path: Path) -> None:
    _, fixture = candidate_fixture
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
    receipt = materialize_candidate_data_view(
        CandidateDataViewRequest(fixture.judge, tmp_path), source=source
    )
    slate = receipt.root / "slate.parquet"
    alias = tmp_path / "slate-alias.parquet"
    os.link(slate, alias)
    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, tmp_path), source=source
        )
    assert captured.value.code is StageCErrorCode.CANDIDATE_VIEW_CONFLICT


def test_destination_files_are_independent_single_link_copies(
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, fixture = candidate_fixture
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
    receipt = materialize_candidate_data_view(
        CandidateDataViewRequest(fixture.judge, tmp_path), source=source
    )
    source_path = fixture.fixture_root / "action_log/dt=2026-08-30/part-0.parquet"
    target_path = receipt.root / "history/action_log/dt=2026-08-30/part-0.parquet"
    source_stat = source_path.stat()
    target_stat = target_path.stat()
    assert (source_stat.st_dev, source_stat.st_ino) != (
        target_stat.st_dev,
        target_stat.st_ino,
    )
    assert target_stat.st_nlink == 1
