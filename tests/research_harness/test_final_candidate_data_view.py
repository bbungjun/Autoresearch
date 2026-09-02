"""Final 입력의 준비·권한 검사·불변 파일 게시 계약.

[파이프라인] validation 종료 뒤 final 소비 claim과 격리 실행 입력 사이를 검증한다.
[기능] 실제 grant, paired bytes, metadata 시점·identity 및 게시 실패의 회수를 확인한다.
[비책임] subprocess 실행과 metric 판정·checkpoint 복구는 별도 통합 테스트가 담당한다.
"""

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
import inspect
import json
from pathlib import Path
import shutil

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import autoresearch.research_harness.candidate_data_view as module
from autoresearch.research_harness.consumption_registry import (
    FinalConsumptionGrant, FinalConsumptionRequest, claim_final_consumption,
)
from autoresearch.research_harness.evaluation_snapshot_models import ArtifactReceipt
from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode
from autoresearch.research_harness.fixture_models import (
    CandidateDataManifestV2, CandidateDataViewReceipt, CandidateDataViewRequest,
    LocalEvaluationFixtureReceipt, PreparedCandidateMetadata, PreparedMetadataArtifact,
)
from autoresearch.research_harness.local_evaluation_fixture import FixtureActionLogSource, _io_path
from tests.research_harness.test_workspace import candidate_fixture as candidate_fixture


FinalCase = tuple[
    LocalEvaluationFixtureReceipt, FixtureActionLogSource,
    PreparedCandidateMetadata, FinalConsumptionGrant,
]


def test_final_interfaces_exist_without_validation_split_selector() -> None:
    assert callable(getattr(module, "prepare_final_candidate_metadata", None))
    assert callable(getattr(module, "materialize_final_candidate_data_view", None))
    for function in (
        module.prepare_candidate_metadata, module.materialize_candidate_data_view,
        module.materialize_candidate_data_view_v2,
    ):
        assert set(inspect.signature(function).parameters).isdisjoint({"final", "split", "grant"})


@pytest.fixture()
def final_case(
    candidate_fixture: tuple[LocalEvaluationFixtureReceipt, FixtureActionLogSource],
    tmp_path_factory: pytest.TempPathFactory,
) -> FinalCase:
    original, _ = candidate_fixture
    root = tmp_path_factory.mktemp("fv") / "fixtures/by-hash" / original.fixture_root.name
    shutil.copytree(_io_path(original.fixture_root), _io_path(root))
    judge = replace(
        original.judge,
        snapshot_root=root / "evaluation-snapshots/by-hash" / str(original.judge.snapshot_fingerprint),
    )
    fixture = replace(original, fixture_root=root, descriptor_path=root / "fixture.json", judge=judge)
    source = FixtureActionLogSource(root, fixture.descriptor_sha256)
    metadata = module.prepare_final_candidate_metadata(judge, source=source)
    (root / "final-holdout-consumed").mkdir()
    grant = claim_final_consumption(FinalConsumptionRequest(
        root, judge, "a" * 40, "b" * 40, datetime(2026, 9, 3, tzinfo=UTC),
    ))
    return fixture, source, metadata, grant


def _publish(case: FinalCase, root: Path, *, metadata: PreparedCandidateMetadata | None = None) -> CandidateDataViewReceipt:
    fixture, source, prepared, grant = case
    root.mkdir(exist_ok=True)
    return module.materialize_final_candidate_data_view(
        CandidateDataViewRequest(fixture.judge, root), source=source,
        metadata=prepared if metadata is None else metadata, grant=grant,
    )


def _artifact(table: pa.Table) -> PreparedMetadataArtifact:
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    payload = sink.getvalue().to_pybytes()
    return PreparedMetadataArtifact(ArtifactReceipt("metadata/users.parquet", len(table), sha256(payload).hexdigest()), payload)


def test_preparation_is_judge_only_and_does_not_claim_or_publish(candidate_fixture, tmp_path: Path) -> None:
    fixture, source = candidate_fixture
    before = sorted(path.relative_to(_io_path(fixture.fixture_root)) for path in _io_path(fixture.fixture_root).rglob("*"))
    prepared = module.prepare_final_candidate_metadata(fixture.judge, source=source)
    assert prepared.evaluation_id == fixture.judge.final_holdout_id
    assert prepared.snapshot_fingerprint == fixture.judge.snapshot_fingerprint
    assert prepared.users.receipt.rows > 0
    assert prepared.videos.receipt.rows > 0
    assert before == sorted(path.relative_to(_io_path(fixture.fixture_root)) for path in _io_path(fixture.fixture_root).rglob("*"))
    assert not list(tmp_path.iterdir())


def test_final_paired_views_have_identical_independent_bytes_and_reuse(final_case: FinalCase, tmp_path: Path) -> None:
    fixture, _, metadata, grant = final_case
    first = _publish(final_case, tmp_path / "baseline")
    second = _publish(final_case, tmp_path / "candidate")
    assert isinstance(first.manifest, CandidateDataManifestV2)
    assert first.manifest.evaluation_id == fixture.judge.final_holdout_id
    assert first.manifest.evaluation_id != fixture.judge.validation_id
    assert first.manifest_sha256 == second.manifest_sha256
    assert not first.reused and _publish(final_case, tmp_path / "baseline").reused
    slate_source = fixture.judge.snapshot_root / "final_holdout/slate.parquet"
    assert (first.root / "slate.parquet").read_bytes() == _io_path(slate_source).read_bytes()
    assert (first.root / "slate.parquet").stat().st_ino != _io_path(slate_source).stat().st_ino
    for path in first.root.rglob("*"):
        if path.is_file():
            other = second.root / path.relative_to(first.root)
            assert path.read_bytes() == other.read_bytes()
            assert path.stat().st_ino != other.stat().st_ino
    assert (first.root / "metadata/users.parquet").read_bytes() == metadata.users.payload
    assert grant._authorizes(fixture.judge)
    manifest_bytes = (first.root / "candidate-view.json").read_bytes()
    for forbidden in (b"labels", b"fixture://", b"final-holdout-consumed", b"snapshot_root", b"fixture_seed"):
        assert forbidden not in manifest_bytes
    assert not list(first.root.rglob("labels.parquet"))


def test_final_metadata_matches_only_allowed_ids_and_observation_times(final_case: FinalCase, tmp_path: Path) -> None:
    fixture, _, metadata, _ = final_case
    view = _publish(final_case, tmp_path)
    requests = pq.read_table(view.root / "slate.parquet").to_pylist()
    for receipt in view.manifest.history_partitions:
        requests.extend(
            row for row in pq.read_table(view.root / receipt.relative_path).to_pylist()
            if row["event_type"] == "impression"
        )
    descriptor = json.loads(_io_path(fixture.descriptor_path).read_bytes())
    for key, artifact, inputs in (
        ("user_id", metadata.users, [descriptor["virtual_users"]]),
        ("video_id", metadata.videos, descriptor["youtube_partitions"]),
    ):
        maximum: dict[str, datetime] = {}
        for row in requests:
            maximum[row[key]] = max(maximum.get(row[key], row["event_timestamp"]), row["event_timestamp"])
        expected: set[tuple[str, datetime]] = set()
        for item in inputs:
            for row in pq.read_table(_io_path(fixture.fixture_root / item["relative_path"])).to_pylist():
                observed = (
                    datetime.fromisoformat(row["generated_at"]) if key == "user_id"
                    else max(row["collected_at"], row["video_trending_date"])
                )
                if row[key] in maximum and observed <= maximum[row[key]]:
                    expected.add((row[key], observed))
        table = pq.read_table(pa.BufferReader(artifact.payload))
        assert {(row[key], row["available_at"]) for row in table.to_pylist()} == expected


def test_final_publication_does_not_recalculate_prepared_metadata(final_case: FinalCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_normalization(raw: pa.Table) -> pa.Table:
        pytest.fail("final publication must reuse prepared bytes")

    monkeypatch.setattr(module, "normalize_user_metadata", unexpected_normalization)
    monkeypatch.setattr(module, "normalize_video_metadata", unexpected_normalization)
    assert _publish(final_case, tmp_path).manifest.user_metadata.sha256 == final_case[2].users.receipt.sha256


def test_final_metadata_is_required(final_case: FinalCase, tmp_path: Path) -> None:
    fixture, source, _, grant = final_case
    with pytest.raises(StageCError) as error:
        module.materialize_final_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, tmp_path), source=source, metadata=None, grant=grant,
        )
    assert error.value.code is StageCErrorCode.CANDIDATE_VIEW_CONFLICT
    assert not list(tmp_path.iterdir())


class _DuckGrant:
    def _authorizes(self, handoff: object) -> bool:
        return True


@pytest.mark.parametrize("grant", [None, object(), _DuckGrant()])
def test_missing_or_duck_grant_fails_before_any_publication(final_case: FinalCase, tmp_path: Path, grant: object) -> None:
    fixture, source, metadata, _ = final_case
    with pytest.raises(StageCError) as error:
        module.materialize_final_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, tmp_path), source=source, metadata=metadata, grant=grant,
        )
    assert error.value.code is StageCErrorCode.JUDGE_HANDOFF_INVALID
    assert not list(tmp_path.iterdir())


def test_grant_from_other_snapshot_root_is_rejected(final_case: FinalCase, candidate_fixture, tmp_path: Path) -> None:
    _, _, metadata, grant = final_case
    fixture, source = candidate_fixture
    with pytest.raises(StageCError):
        module.materialize_final_candidate_data_view(
            CandidateDataViewRequest(fixture.judge, tmp_path), source=source, metadata=metadata, grant=grant,
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("kind", ["deleted", "changed"])
def test_marker_loss_or_corruption_rejects_final_but_not_validation(final_case: FinalCase, tmp_path: Path, kind: str) -> None:
    fixture, source, _, grant = final_case
    if kind == "deleted":
        grant.evidence.marker_path.unlink()
    else:
        grant.evidence.marker_path.write_bytes(b"changed")
    with pytest.raises(StageCError):
        _publish(final_case, tmp_path / "final")
    validation_metadata = module.prepare_candidate_metadata(fixture.judge, source=source)
    root = tmp_path / "validation"
    root.mkdir()
    view = module.materialize_candidate_data_view_v2(
        CandidateDataViewRequest(fixture.judge, root), source=source, metadata=validation_metadata,
    )
    assert view.manifest.evaluation_id == fixture.judge.validation_id


def test_validation_and_final_bundles_cannot_be_mixed(final_case: FinalCase, tmp_path: Path) -> None:
    fixture, source, final_metadata, _ = final_case
    validation_metadata = module.prepare_candidate_metadata(fixture.judge, source=source)
    with pytest.raises(StageCError):
        _publish(final_case, tmp_path / "final", metadata=validation_metadata)
    root = tmp_path / "validation"
    root.mkdir()
    with pytest.raises(StageCError):
        module.materialize_candidate_data_view_v2(
            CandidateDataViewRequest(fixture.judge, root), source=source, metadata=final_metadata,
        )
    assert not (root / "harness_in").exists()


@pytest.mark.parametrize("kind", ["rows", "schema", "future", "unrequested"])
def test_invalid_final_metadata_fails_closed(final_case: FinalCase, tmp_path: Path, kind: str) -> None:
    metadata = final_case[2]
    table = pq.read_table(pa.BufferReader(metadata.users.payload))
    if kind == "rows":
        artifact = replace(metadata.users, receipt=replace(metadata.users.receipt, rows=len(table) + 1))
    elif kind == "schema":
        artifact = _artifact(table.drop(["age"]))
    else:
        rows = table.to_pylist()
        if kind == "future":
            rows[0]["available_at"] = datetime(2099, 1, 1, tzinfo=UTC)
        else:
            rows[0]["user_id"] = "unrequested-user"
        artifact = _artifact(pa.Table.from_pylist(rows, schema=table.schema))
    with pytest.raises(StageCError):
        _publish(final_case, tmp_path, metadata=replace(metadata, users=artifact))
    assert not (tmp_path / "harness_in").exists()


@pytest.mark.parametrize("kind", ["corrupt", "extra", "hardlink"])
def test_reuse_rejects_tampered_final_target(final_case: FinalCase, tmp_path: Path, kind: str) -> None:
    view = _publish(final_case, tmp_path)
    target = view.root / "metadata/users.parquet"
    if kind == "corrupt":
        target.write_bytes(target.read_bytes() + b"changed")
    elif kind == "extra":
        (view.root / "extra.txt").write_text("extra")
    else:
        (tmp_path / "alias.parquet").hardlink_to(target)
    with pytest.raises(StageCError):
        _publish(final_case, tmp_path)


@pytest.mark.parametrize("reuse", [False, True])
def test_grant_is_rechecked_after_view_validation(final_case: FinalCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reuse: bool) -> None:
    if reuse:
        _publish(final_case, tmp_path)
    original = module._view_is_valid

    def validate_then_revoke(*args: object, **kwargs: object) -> bool:
        valid = original(*args, **kwargs)
        final_case[3].evidence.marker_path.write_bytes(b"changed-after-validation")
        return valid

    monkeypatch.setattr(module, "_view_is_valid", validate_then_revoke)
    with pytest.raises(StageCError):
        _publish(final_case, tmp_path)
    assert (tmp_path / "harness_in").exists() is reuse
    assert not list(tmp_path.glob(".harness-in-staging-*"))


def test_final_staging_failure_leaves_no_partial_view(final_case: FinalCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = module._copy_verified_payload

    def fail_video(payload: bytes, target: Path, receipt: object, *, source_identity: tuple[int, int] | None) -> None:
        if target.name == "videos.parquet":
            raise OSError("simulated disk failure")
        original(payload, target, receipt, source_identity=source_identity)

    monkeypatch.setattr(module, "_copy_verified_payload", fail_video)
    with pytest.raises(StageCError):
        _publish(final_case, tmp_path)
    assert not (tmp_path / "harness_in").exists()
    assert not list(tmp_path.glob(".harness-in-staging-*"))
