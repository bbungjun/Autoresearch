"""Validation metadata 준비·게시와 disposable workspace의 통합 계약.

[파이프라인] fixture 원본에서 candidate 입력 파일을 거쳐 실행 context까지 검증한다.
[기능] 허용 ID/시간, 독립 복사·불변 bytes·변조 거부·v1 보존·workspace 회수를 확인한다.
[비책임] final 소비 권한·checkpoint 영속화·피처 학습은 후속 테스트 범위다.
"""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from autoresearch.research_harness.candidate_data_view import (
    materialize_candidate_data_view, materialize_candidate_data_view_v2, prepare_candidate_metadata,
)
from autoresearch.research_harness.evaluation_snapshot_models import ArtifactReceipt, EvaluationId
from autoresearch.research_harness.fixture_errors import StageCError
from autoresearch.research_harness.fixture_models import (
    CandidateDataManifestV2, CandidateDataViewReceipt, CandidateDataViewRequest,
    PreparedCandidateMetadata, PreparedMetadataArtifact,
)
from autoresearch.research_harness.local_evaluation_fixture import FixtureActionLogSource
from autoresearch.research_harness.workspace import (
    CandidateWorkspaceRequest, WorkspaceError, WorkspaceErrorCode, open_candidate_workspace,
)
from tests.research_harness.test_workspace import (
    candidate_fixture as candidate_fixture, repository as repository,
)
from tests.research_harness.test_candidate_data_view import _io_path


@pytest.fixture(scope="module")
def metadata(candidate_fixture) -> PreparedCandidateMetadata:
    fixture, source = candidate_fixture
    return prepare_candidate_metadata(fixture.judge, source=source)


def _publish(candidate_fixture, metadata: PreparedCandidateMetadata, root: Path) -> CandidateDataViewReceipt:
    fixture, source = candidate_fixture
    root.mkdir(exist_ok=True)
    return materialize_candidate_data_view_v2(
        CandidateDataViewRequest(fixture.judge, root), source=source, metadata=metadata,
    )


def _artifact(table: pa.Table, path: str) -> PreparedMetadataArtifact:
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    payload = sink.getvalue().to_pybytes()
    return PreparedMetadataArtifact(ArtifactReceipt(path, len(table), sha256(payload).hexdigest()), payload)


def test_prepares_only_allowed_ids_and_observation_times(candidate_fixture, metadata, tmp_path: Path) -> None:
    fixture, _ = candidate_fixture
    view = _publish(candidate_fixture, metadata, tmp_path)
    requests = pq.read_table(view.root / "slate.parquet").to_pylist()
    for receipt in view.manifest.history_partitions:
        requests.extend(row for row in pq.read_table(view.root / receipt.relative_path).to_pylist()
                        if row["event_type"] == "impression")
    descriptor = json.loads(fixture.descriptor_path.read_text())
    for key, artifact, inputs in (
        ("user_id", metadata.users, [descriptor["virtual_users"]]),
        ("video_id", metadata.videos, descriptor["youtube_partitions"]),
    ):
        maximum = {}
        for row in requests:
            maximum[row[key]] = max(maximum.get(row[key], row["event_timestamp"]), row["event_timestamp"])
        expected = set()
        for item in inputs:
            for row in pq.read_table(fixture.fixture_root / item["relative_path"]).to_pylist():
                observed = (datetime.fromisoformat(row["generated_at"]) if key == "user_id"
                            else max(row["collected_at"], row["video_trending_date"]))
                if row[key] in maximum and observed <= maximum[row[key]]:
                    expected.add((row[key], observed))
        table = pq.read_table(pa.BufferReader(artifact.payload))
        assert {(row[key], row["available_at"]) for row in table.to_pylist()} == expected
        assert table.num_rows > 0
        assert set(table.column_names).isdisjoint({"fixture_seed", "source_persona_json", "source_hash", "video_title"})


def test_paired_views_have_identical_bytes_and_reuse_is_write_once(candidate_fixture, metadata, tmp_path: Path) -> None:
    first = _publish(candidate_fixture, metadata, tmp_path / "first")
    second = _publish(candidate_fixture, metadata, tmp_path / "second")
    assert isinstance(first.manifest, CandidateDataManifestV2)
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.reused is False
    assert _publish(candidate_fixture, metadata, tmp_path / "first").reused is True
    for artifact in (metadata.users, metadata.videos):
        a = first.root / artifact.receipt.relative_path
        b = second.root / artifact.receipt.relative_path
        assert a.read_bytes() == b.read_bytes() == artifact.payload
        assert a.stat().st_ino != b.stat().st_ino
    manifest_bytes = (first.root / "candidate-view.json").read_bytes()
    assert b"fixture_seed" not in manifest_bytes
    assert b"fixture://" not in manifest_bytes
    assert str(candidate_fixture[0].fixture_root).encode() not in manifest_bytes


@pytest.mark.parametrize("kind", ["missing", "corrupt", "extra", "manifest", "hardlink"])
def test_reuse_rejects_damaged_or_unexpected_files(candidate_fixture, metadata, tmp_path: Path, kind: str) -> None:
    view = _publish(candidate_fixture, metadata, tmp_path)
    users = view.root / "metadata/users.parquet"
    if kind == "missing":
        users.unlink()
    elif kind == "corrupt":
        users.write_bytes(users.read_bytes() + b"changed")
    elif kind == "extra":
        (view.root / "metadata/unexpected.txt").write_text("unexpected")
    elif kind == "hardlink":
        (tmp_path / "alias.parquet").hardlink_to(users)
    else:
        (view.root / "candidate-view.json").write_text("{}")
    with pytest.raises(StageCError):
        _publish(candidate_fixture, metadata, tmp_path)


def test_v1_target_is_not_upgraded_in_place(candidate_fixture, metadata, tmp_path: Path) -> None:
    fixture, source = candidate_fixture
    v1 = materialize_candidate_data_view(CandidateDataViewRequest(fixture.judge, tmp_path), source=source)
    before = (v1.root / "candidate-view.json").read_bytes()
    with pytest.raises(StageCError):
        _publish(candidate_fixture, metadata, tmp_path)
    assert (v1.root / "candidate-view.json").read_bytes() == before
    assert not (v1.root / "metadata").exists()


@pytest.mark.parametrize("kind", ["identity", "rows", "schema", "future", "unrequested"])
def test_invalid_bundle_cannot_be_published(candidate_fixture, metadata, tmp_path: Path, kind: str) -> None:
    if kind == "identity":
        changed = replace(metadata, evaluation_id=EvaluationId("eval_" + "f" * 64))
    else:
        artifact = metadata.users
        table = pq.read_table(pa.BufferReader(artifact.payload))
        if kind == "rows":
            artifact = replace(artifact, receipt=replace(artifact.receipt, rows=table.num_rows + 1))
        elif kind == "schema":
            artifact = _artifact(table.drop(["age"]), artifact.receipt.relative_path)
        else:
            rows = table.to_pylist()
            if kind == "future":
                rows[0]["available_at"] = rows[0]["available_at"].replace(year=2099)
            else:
                rows[0]["user_id"] = "not-an-allowed-user"
            artifact = _artifact(pa.Table.from_pylist(rows, schema=table.schema), artifact.receipt.relative_path)
        changed = replace(metadata, users=artifact)
    with pytest.raises(StageCError):
        _publish(candidate_fixture, changed, tmp_path)
    assert not (tmp_path / "harness_in").exists()


def test_prepared_bytes_are_frozen_and_digest_checked(metadata) -> None:
    with pytest.raises(FrozenInstanceError):
        metadata.users.payload = b"changed"
    with pytest.raises(StageCError):
        replace(metadata.users, payload=b"changed")


def test_changed_metadata_changes_view_identity_not_evaluation(candidate_fixture, metadata, tmp_path: Path) -> None:
    table = pq.read_table(pa.BufferReader(metadata.users.payload))
    rows = table.to_pylist()
    rows[0]["age"] += 1
    changed = replace(metadata, users=_artifact(pa.Table.from_pylist(rows, schema=table.schema), "metadata/users.parquet"))
    first = _publish(candidate_fixture, metadata, tmp_path / "first")
    second = _publish(candidate_fixture, changed, tmp_path / "second")
    assert first.manifest.evaluation_id == second.manifest.evaluation_id
    assert first.manifest_sha256 != second.manifest_sha256
    with pytest.raises(StageCError):
        _publish(candidate_fixture, changed, tmp_path / "first")


def test_workspace_receives_v2_digest_without_judge_context(repository, candidate_fixture, metadata, tmp_path: Path) -> None:
    fixture, source = candidate_fixture
    request = CandidateWorkspaceRequest(repository[0], repository[1], tmp_path / "candidate", fixture.judge)
    with open_candidate_workspace(request, source=source, metadata=metadata) as workspace:
        payload = (workspace.root / "harness_in/candidate-view.json").read_bytes()
        assert json.loads(payload)["contract_version"] == "candidate-data-view-v2"
        assert workspace.candidate_view_sha256 == sha256(payload).hexdigest()
        assert (workspace.root / "harness_in/metadata/users.parquet").read_bytes() == metadata.users.payload
        assert str(fixture.fixture_root) not in repr(workspace.process)
        assert workspace.inspect_changes().changed_paths == ()
    assert not request.workspace_root.exists()


def test_invalid_metadata_cleans_up_workspace(repository, candidate_fixture, metadata, tmp_path: Path) -> None:
    fixture, source = candidate_fixture
    request = CandidateWorkspaceRequest(repository[0], repository[1], tmp_path / "candidate", fixture.judge)
    changed = replace(metadata, evaluation_id=EvaluationId("eval_" + "f" * 64))
    with pytest.raises(WorkspaceError) as error:
        with open_candidate_workspace(request, source=source, metadata=changed):
            pytest.fail("invalid input must not reach candidate")
    assert error.value.code is WorkspaceErrorCode.DATA_VIEW_INVALID
    assert not request.workspace_root.exists()


@pytest.mark.parametrize("kind", ["missing", "corrupt", "hardlink"])
def test_preparation_rejects_damaged_raw_metadata(
    candidate_fixture, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, kind: str,
) -> None:
    fixture, _ = candidate_fixture
    # 기존 fixture validator의 Windows 경로 길이 조건과 동일한 짧은 state root를 쓴다.
    root = tmp_path_factory.mktemp("md") / "fixtures/by-hash" / fixture.fixture_root.name
    shutil.copytree(_io_path(fixture.fixture_root), _io_path(root))
    judge = replace(fixture.judge, snapshot_root=root / "evaluation-snapshots/by-hash" / str(fixture.judge.snapshot_fingerprint))
    source = FixtureActionLogSource(root, fixture.descriptor_sha256)
    assert prepare_candidate_metadata(judge, source=source).users.receipt.rows > 0
    users = root / "inputs/virtual_users.parquet"
    if kind == "missing":
        users.unlink()
    elif kind == "corrupt":
        users.write_bytes(users.read_bytes() + b"corrupt")
    else:
        (tmp_path / "alias.parquet").hardlink_to(users)
    with pytest.raises(StageCError):
        prepare_candidate_metadata(judge, source=source)


def test_failed_staging_does_not_publish_partial_view(candidate_fixture, metadata, tmp_path: Path, monkeypatch) -> None:
    from autoresearch.research_harness import candidate_data_view as module

    original = module._copy_verified_payload

    def fail_video(payload: bytes, target: Path, receipt, *, source_identity) -> None:
        if target.name == "videos.parquet":
            raise OSError("simulated disk failure")
        original(payload, target, receipt, source_identity=source_identity)

    monkeypatch.setattr(module, "_copy_verified_payload", fail_video)
    with pytest.raises(StageCError):
        _publish(candidate_fixture, metadata, tmp_path)
    assert not (tmp_path / "harness_in").exists()
    assert not list(tmp_path.glob(".harness-in-staging-*"))


def test_publishing_does_not_recalculate_prepared_metadata(candidate_fixture, metadata, tmp_path: Path, monkeypatch) -> None:
    from autoresearch.research_harness import candidate_data_view as module

    def unexpected_normalization(*args, **kwargs) -> None:
        pytest.fail("paired publication must use the prepared bytes")

    monkeypatch.setattr(module, "normalize_user_metadata", unexpected_normalization)
    monkeypatch.setattr(module, "normalize_video_metadata", unexpected_normalization)
    assert _publish(candidate_fixture, metadata, tmp_path).manifest.user_metadata.sha256 == metadata.users.receipt.sha256


def test_duplicate_between_partitions_is_rejected_before_filtering(candidate_fixture, monkeypatch) -> None:
    from autoresearch.research_harness import candidate_data_view as module

    original = module.normalize_video_metadata

    def duplicate_unrequested(raw: pa.Table) -> pa.Table:
        table = original(raw).slice(0, 1)
        rows = table.to_pylist()
        rows[0]["video_id"] = "unrequested-duplicate"
        rows[0]["available_at"] = datetime.fromisoformat("2099-01-01T00:00:00+00:00")
        return pa.Table.from_pylist(rows, schema=table.schema)

    monkeypatch.setattr(module, "normalize_video_metadata", duplicate_unrequested)
    fixture, source = candidate_fixture
    with pytest.raises(StageCError):
        prepare_candidate_metadata(fixture.judge, source=source)
