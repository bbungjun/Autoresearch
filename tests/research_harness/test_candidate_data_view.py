from __future__ import annotations

from datetime import date
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path
import subprocess
import traceback

import pyarrow.parquet as pq
import pyarrow as pa
import pytest

from autoresearch.research_harness import (
    CandidateDataViewRequest,
    EvaluationSnapshotRequest,
    LocalEvaluationFixtureRequest,
    StageCError,
    StageCErrorCode,
    build_local_evaluation_fixture,
    build_evaluation_snapshot,
    materialize_candidate_data_view,
)
from autoresearch.research_harness.evaluation_snapshot_models import (
    EvaluationSnapshotManifest,
)
from autoresearch.research_harness.evaluation_source import ArrowActionLogSource
from autoresearch.research_harness.local_evaluation_fixture import (
    FixtureActionLogSource,
    _validated_judge_handoff,
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


@pytest.fixture(scope="module")
def arrow_snapshot(candidate_fixture, tmp_path_factory: pytest.TempPathFactory):
    _, fixture = candidate_fixture
    output_root = tmp_path_factory.mktemp("candidate-arrow-snapshot")
    action_root = fixture.fixture_root / "action_log"
    source = ArrowActionLogSource.from_root(action_root.as_uri())
    receipt = build_evaluation_snapshot(
        EvaluationSnapshotRequest(
            action_log_root=source.opaque_root,
            history_start_date=date(2026, 8, 30),
            evaluation_start_date=EVALUATION_DATE,
            evaluation_end_date=EVALUATION_DATE,
            slate_id_cutover_date=date(2026, 8, 30),
            output_root=output_root,
        ),
        source=source,
    )
    handoff = _validated_judge_handoff(
        receipt.target_path,
        expected_fingerprint=str(receipt.snapshot_fingerprint),
    )
    return action_root, source, handoff


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
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
    materialize_candidate_data_view(
        CandidateDataViewRequest(fixture.judge, tmp_path), source=source
    )
    tampered = _io_path(
        fixture.fixture_root / "action_log/dt=2026-08-30/part-0.parquet"
    )
    original = tampered.read_bytes()
    try:
        tampered.write_bytes(b"tampered")
        with pytest.raises(StageCError) as captured:
            materialize_candidate_data_view(
                CandidateDataViewRequest(fixture.judge, tmp_path), source=source
            )
    finally:
        tampered.write_bytes(original)
    assert captured.value.code is StageCErrorCode.JUDGE_HANDOFF_INVALID


def test_existing_candidate_history_cannot_be_reused_as_its_own_source(
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, fixture = candidate_fixture
    genuine = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
    receipt = materialize_candidate_data_view(
        CandidateDataViewRequest(fixture.judge, tmp_path), source=genuine
    )
    adversarial = FixtureActionLogSource(
        receipt.root / "history",
        fixture.descriptor_sha256,
    )

    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, tmp_path), source=adversarial
        )

    assert captured.value.code in {
        StageCErrorCode.FIXTURE_REQUEST_INVALID,
        StageCErrorCode.JUDGE_HANDOFF_INVALID,
        StageCErrorCode.CANDIDATE_VIEW_CONFLICT,
    }


def test_first_materialization_rejects_source_inside_destination(
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, fixture = candidate_fixture
    adversarial_root = tmp_path / "harness_in" / "history"
    for partition in fixture.action_log_partitions[:2]:
        source_path = (
            fixture.fixture_root
            / "action_log"
            / f"dt={partition.dt.isoformat()}"
            / "part-0.parquet"
        )
        target_path = (
            adversarial_root
            / "action_log"
            / f"dt={partition.dt.isoformat()}"
            / "part-0.parquet"
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(_io_path(source_path).read_bytes())
    source = FixtureActionLogSource(adversarial_root, fixture.descriptor_sha256)

    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, tmp_path), source=source
        )

    assert captured.value.code is StageCErrorCode.FIXTURE_REQUEST_INVALID


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


class _WrongPartitionUriSource(FixtureActionLogSource):
    def __init__(self, fixture_root: Path, descriptor_digest: str) -> None:
        self.opened: list[date] = []
        super().__init__(
            fixture_root,
            descriptor_digest,
            _opened_dates=self.opened,
        )

    def partition_uri(self, dt: date) -> str:
        return f"{self.opaque_root}/wrong/dt={dt.isoformat()}"


class _WrongTailPartitionUriSource(FixtureActionLogSource):
    def __init__(
        self,
        fixture_root: Path,
        descriptor_digest: str,
        tampered_date: date,
    ) -> None:
        self.opened: list[date] = []
        self.checked: list[date] = []
        self._tampered_date = tampered_date
        super().__init__(
            fixture_root,
            descriptor_digest,
            _opened_dates=self.opened,
        )

    def partition_uri(self, dt: date) -> str:
        self.checked.append(dt)
        if dt == self._tampered_date:
            return f"{self.opaque_root}/wrong-tail"
        return super().partition_uri(dt)


class _UntrustedFixtureSource(FixtureActionLogSource):
    def __init__(self, fixture_root: Path, descriptor_digest: str) -> None:
        self.opened: list[date] = []
        super().__init__(fixture_root, descriptor_digest)

    def open_partition(self, dt: date):
        self.opened.append(dt)
        return pa.BufferReader(b"not parquet")


def test_source_identity_is_checked_before_any_open(candidate_fixture, tmp_path: Path) -> None:
    _, fixture = candidate_fixture
    opened: list[date] = []
    source = FixtureActionLogSource(
        fixture.fixture_root,
        "0" * 64,
        _opened_dates=opened,
    )
    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, tmp_path), source=source
        )
    assert captured.value.code is StageCErrorCode.JUDGE_HANDOFF_INVALID
    assert opened == []


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


@pytest.mark.parametrize(
    ("tampered_date", "expected_checked"),
    (
        (
            date(2026, 9, 1),
            [date(2026, 8, 30), date(2026, 8, 31), date(2026, 9, 1)],
        ),
        (
            date(2026, 9, 2),
            [
                date(2026, 8, 30),
                date(2026, 8, 31),
                date(2026, 9, 1),
                date(2026, 9, 2),
            ],
        ),
    ),
)
def test_all_source_partition_uris_are_checked_before_history_open(
    candidate_fixture,
    tmp_path: Path,
    tampered_date: date,
    expected_checked: list[date],
) -> None:
    _, fixture = candidate_fixture
    source = _WrongTailPartitionUriSource(
        fixture.fixture_root,
        fixture.descriptor_sha256,
        tampered_date,
    )
    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, tmp_path), source=source
        )
    assert captured.value.code is StageCErrorCode.JUDGE_HANDOFF_INVALID
    assert source.checked == expected_checked
    assert source.opened == []


def test_fixture_uri_rejects_untrusted_source_implementation(
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, fixture = candidate_fixture
    source = _UntrustedFixtureSource(fixture.fixture_root, fixture.descriptor_sha256)
    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, tmp_path), source=source
        )
    assert captured.value.code is StageCErrorCode.JUDGE_HANDOFF_INVALID
    assert source.opened == []


class _WrongArrowBytesSource:
    def __init__(self, source: ArrowActionLogSource) -> None:
        self._source = source
        self.opaque_root = source.opaque_root
        self.opened: list[date] = []

    def partition_uri(self, dt: date) -> str:
        return self._source.partition_uri(dt)

    def open_partition(self, dt: date):
        self.opened.append(dt)
        return pa.BufferReader(b"not parquet")

    def _physical_source_root(self) -> Path | None:
        return self._source._physical_source_root()

    def _physical_partition_path(self, dt: date) -> Path | None:
        return self._source._physical_partition_path(dt)


def test_local_arrow_source_bytes_and_parquet_integrity_are_checked(
    arrow_snapshot,
    tmp_path: Path,
) -> None:
    _, genuine, handoff = arrow_snapshot
    source = _WrongArrowBytesSource(genuine)
    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(handoff, tmp_path), source=source
        )
    assert captured.value.code is StageCErrorCode.JUDGE_HANDOFF_INVALID
    assert source.opened == [date(2026, 8, 30)]


def test_local_arrow_source_root_cannot_contain_destination(
    arrow_snapshot,
) -> None:
    action_root, source, handoff = arrow_snapshot
    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(handoff, action_root), source=source
        )
    assert captured.value.code is StageCErrorCode.FIXTURE_REQUEST_INVALID


class _BufferBackedSource:
    def __init__(self, source: ArrowActionLogSource) -> None:
        self._source = source
        self.opaque_root = source.opaque_root
        self.checked: list[date] = []
        self.opened: list[date] = []

    def partition_uri(self, dt: date) -> str:
        self.checked.append(dt)
        return self._source.partition_uri(dt)

    def open_partition(self, dt: date):
        self.opened.append(dt)
        with self._source.open_partition(dt) as handle:
            return pa.BufferReader(handle.read())


class _FailingBufferSource(_BufferBackedSource):
    def __init__(self, source: ArrowActionLogSource, secret: str) -> None:
        super().__init__(source)
        self._secret = secret

    def open_partition(self, dt: date):
        self.opened.append(dt)
        raise OSError(self._secret)


class _IdentityFailingBufferSource(_BufferBackedSource):
    def __init__(self, source: ArrowActionLogSource, secret: str) -> None:
        super().__init__(source)
        self._secret = secret

    def partition_uri(self, dt: date) -> str:
        raise OSError(self._secret)


def test_buffer_backed_remote_shape_checks_all_uris_but_opens_only_history(
    arrow_snapshot,
    tmp_path: Path,
) -> None:
    _, genuine, handoff = arrow_snapshot
    source = _BufferBackedSource(genuine)
    receipt = materialize_candidate_data_view(
        CandidateDataViewRequest(handoff, tmp_path), source=source
    )

    assert receipt.reused is False
    assert source.checked == [
        date(2026, 8, 30),
        date(2026, 8, 31),
        date(2026, 9, 1),
        date(2026, 9, 2),
    ]
    assert source.opened == [date(2026, 8, 30), date(2026, 8, 31)]


def test_candidate_source_oserror_traceback_is_sanitized(
    arrow_snapshot,
    tmp_path: Path,
) -> None:
    _, genuine, handoff = arrow_snapshot
    secret = str(tmp_path / "SECRET-SOURCE-PATH")
    source = _FailingBufferSource(genuine, secret)

    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(handoff, tmp_path), source=source
        )

    assert captured.value.code is StageCErrorCode.JUDGE_HANDOFF_INVALID
    assert captured.value.__suppress_context__ is True
    assert secret not in "".join(traceback.format_exception(captured.value))


def test_candidate_source_identity_oserror_traceback_is_sanitized(
    arrow_snapshot,
    tmp_path: Path,
) -> None:
    _, genuine, handoff = arrow_snapshot
    secret = str(tmp_path / "SECRET-SOURCE-IDENTITY-PATH")
    source = _IdentityFailingBufferSource(genuine, secret)

    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(handoff, tmp_path), source=source
        )

    assert captured.value.code is StageCErrorCode.JUDGE_HANDOFF_INVALID
    assert captured.value.__suppress_context__ is True
    assert secret not in "".join(traceback.format_exception(captured.value))
    assert source.opened == []


def test_concurrent_materialization_publishes_once_and_reuses_once(
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, fixture = candidate_fixture

    def materialize() -> bool:
        source = FixtureActionLogSource(
            fixture.fixture_root,
            fixture.descriptor_sha256,
        )
        return materialize_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, tmp_path),
            source=source,
        ).reused

    with ThreadPoolExecutor(max_workers=2) as executor:
        reused = tuple(executor.map(lambda _: materialize(), range(2)))

    assert sorted(reused) == [False, True]


def test_only_manifest_history_dates_are_opened(candidate_fixture, tmp_path: Path) -> None:
    _, fixture = candidate_fixture
    opened: list[date] = []
    source = FixtureActionLogSource(
        fixture.fixture_root,
        fixture.descriptor_sha256,
        _opened_dates=opened,
    )
    materialize_candidate_data_view(
        CandidateDataViewRequest(fixture.judge, tmp_path), source=source
    )
    assert opened == [date(2026, 8, 30), date(2026, 8, 31)]


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


def test_manifest_semantic_error_is_sanitized_as_judge_handoff_invalid(
    candidate_fixture,
    tmp_path: Path,
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
    manifest_path = copied_root / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    secret = str(tmp_path / "SECRET-ABS-PATH")
    payload["created_at"] = secret
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    handoff = replace(fixture.judge, snapshot_root=copied_root)
    destination = tmp_path / "candidate"
    destination.mkdir()
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)

    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(handoff, destination), source=source
        )

    assert captured.value.code is StageCErrorCode.JUDGE_HANDOFF_INVALID
    assert captured.value.__suppress_context__ is True
    assert secret not in "".join(traceback.format_exception(captured.value))


def test_fixture_source_is_bound_to_snapshot_outer_provenance(
    candidate_fixture,
    tmp_path: Path,
) -> None:
    _, fixture = candidate_fixture
    spoof_fixture = tmp_path / "spoof-fixture"
    copied_root = (
        spoof_fixture
        / "evaluation-snapshots"
        / "by-hash"
        / str(fixture.judge.snapshot_fingerprint)
    )
    _copy_snapshot(fixture.judge.snapshot_root, copied_root)
    spoof_handoff = replace(fixture.judge, snapshot_root=copied_root)
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
    destination = tmp_path / "candidate"
    destination.mkdir()

    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(spoof_handoff, destination), source=source
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


def test_destination_inside_judge_state_root_is_rejected(candidate_fixture) -> None:
    state_root, fixture = candidate_fixture
    destination = state_root / "candidate"
    destination.mkdir(exist_ok=True)
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)

    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, destination), source=source
        )

    assert captured.value.code is StageCErrorCode.FIXTURE_REQUEST_INVALID
    assert not (destination / "harness_in").exists()


def test_destination_containing_judge_state_root_is_rejected(candidate_fixture) -> None:
    state_root, fixture = candidate_fixture
    destination = state_root.parent
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)

    with pytest.raises(StageCError) as captured:
        materialize_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, destination), source=source
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
    snapshot = EvaluationSnapshotManifest.model_validate_json(
        _io_path(fixture.judge.snapshot_root / "manifest.json").read_bytes()
    )
    slate_source = _io_path(
        fixture.judge.snapshot_root / snapshot.validation.artifacts.slate.relative_path
    ).stat()
    slate_target = (receipt.root / "slate.parquet").stat()
    assert (slate_source.st_dev, slate_source.st_ino) != (
        slate_target.st_dev,
        slate_target.st_ino,
    )
    assert slate_target.st_nlink == 1
