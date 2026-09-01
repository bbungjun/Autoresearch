from __future__ import annotations

from datetime import UTC, date, datetime
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path
import subprocess
import traceback

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from autoresearch.action_log_generation.pipeline import ActionLogGenerationError
from autoresearch.research_harness import (
    CandidateDataViewRequest,
    LocalEvaluationFixtureRequest,
    StageCError,
    StageCErrorCode,
    build_local_evaluation_fixture,
    materialize_candidate_data_view,
)
from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
import autoresearch.research_harness.local_evaluation_fixture as fixture_module
from autoresearch.research_harness.evaluation_artifacts import (
    calculate_snapshot_fingerprint,
    canonical_json_bytes,
)
from autoresearch.research_harness.evaluation_snapshot_models import (
    EvaluationSnapshotManifest,
)
from autoresearch.research_harness.local_evaluation_fixture import (
    FixtureActionLogSource,
    _validate_coverage,
    _validated_judge_handoff,
)
from autoresearch.research_harness.fixture_reproducibility import (
    _slate_projection_sha256,
    _verify_independent_fixture_reproduction,
)


EVALUATION_DATE = date(2026, 9, 1)


def _long_path(path: Path) -> Path:
    if os.name == "nt":
        return Path(f"\\\\?\\{path.absolute()}")
    return path


def _copy_snapshot(source: Path, destination: Path) -> None:
    manifest = EvaluationSnapshotManifest.model_validate_json(
        (source / "manifest.json").read_bytes()
    )
    destination.mkdir(parents=True)
    for name in ("manifest.json", "_SUCCESS"):
        (destination / name).write_bytes((source / name).read_bytes())
    for artifact in (
        manifest.validation.artifacts.slate,
        manifest.validation.artifacts.labels,
        manifest.final_holdout.artifacts.slate,
        manifest.final_holdout.artifacts.labels,
    ):
        target = destination / artifact.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_long_path(source / artifact.relative_path).read_bytes())


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
    if os.name == "nt":
        link.rmdir()
    else:
        link.unlink()


def test_builder_has_exact_public_interface() -> None:
    assert inspect.signature(build_local_evaluation_fixture) == inspect.Signature(
        parameters=(
            inspect.Parameter(
                "request",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation="LocalEvaluationFixtureRequest",
            ),
        ),
        return_annotation="LocalEvaluationFixtureReceipt",
    )


@pytest.fixture(scope="module")
def built_fixture(tmp_path_factory: pytest.TempPathFactory):
    state_root = tmp_path_factory.mktemp("fixture-state")
    receipt = build_local_evaluation_fixture(
        LocalEvaluationFixtureRequest(
            judge_state_root=state_root,
            evaluation_start_date=EVALUATION_DATE,
            fixture_seed=917,
        )
    )
    return state_root, receipt


@pytest.fixture(scope="module")
def independent_fixture_pair(built_fixture, tmp_path_factory: pytest.TempPathFactory):
    _, first = built_fixture
    second_root = tmp_path_factory.mktemp("fixture-second")
    second = build_local_evaluation_fixture(
        LocalEvaluationFixtureRequest(second_root, EVALUATION_DATE, 917)
    )
    return first, second


def test_builder_runs_production_daily_and_publishes_canonical_fixture(
    built_fixture,
) -> None:
    state_root, receipt = built_fixture
    assert receipt.reused is False
    assert receipt.fixture_root == (
        state_root / "fixtures" / "by-hash" / receipt.descriptor_sha256
    )
    assert receipt.descriptor_path == receipt.fixture_root / "fixture.json"
    assert sha256(receipt.descriptor_path.read_bytes()).hexdigest() == receipt.descriptor_sha256
    integrity = json.loads(
        (receipt.fixture_root / "_SUCCESS").read_text(encoding="utf-8")
    )
    assert integrity["contract_version"] == "local-fixture-integrity-v1"
    assert integrity["descriptor_sha256"] == receipt.descriptor_sha256
    assert integrity["snapshot_fingerprint"] == receipt.judge.snapshot_fingerprint
    assert integrity["manifest_sha256"] == receipt.judge.manifest_sha256
    assert len(integrity["action_log_partitions"]) == 4
    assert len(integrity["snapshot_artifacts"]) == 4
    assert [partition.rows for partition in receipt.action_log_partitions] == [5400] * 4
    assert [partition.sha256 for partition in receipt.action_log_partitions] == [
        "e894c158e3f4b758de76eca8b82d33145f95769482ee9137f2e7ccfac373399d",
        "129f18f22d18b0da7cd57c000ae856438c598306806651068ac9c395d879ddcd",
        "ff79a1cd65d41846eb66e95e1de030ee2b4c6561b0c5c7853a9a6c176cd0d012",
        "1293c632f840e0015ebb1cd0c86ae51528629fb4f650e4d480912b1358778504",
    ]
    assert [partition.uri for partition in receipt.action_log_partitions] == [
        f"fixture://{receipt.descriptor_sha256}/action-log/dt={day}/part-0.parquet"
        for day in ("2026-08-30", "2026-08-31", "2026-09-01", "2026-09-02")
    ]
    assert all(
        (receipt.fixture_root / "action_log" / f"dt={partition.dt}" / "part-0.parquet").is_file()
        for partition in receipt.action_log_partitions
    )

    manifest = EvaluationSnapshotManifest.model_validate_json(
        (receipt.judge.snapshot_root / "manifest.json").read_bytes()
    )
    assert manifest.source.root == f"fixture://{receipt.descriptor_sha256}/action-log"
    assert manifest.snapshot_fingerprint == receipt.judge.snapshot_fingerprint
    assert calculate_snapshot_fingerprint(manifest) == receipt.judge.snapshot_fingerprint
    assert manifest.validation.counts.user_count == 160
    assert manifest.final_holdout.counts.user_count == 40
    for split in (manifest.validation, manifest.final_holdout):
        assert split.counts.click_positive_slate_count >= 30
        assert split.counts.click_positive_slate_ratio >= 0.2
        assert 0 < split.counts.clicked_row_count < split.counts.row_count
        assert split.counts.mean_slate_size == 24.0


def test_same_target_is_reused_only_after_full_validation(built_fixture) -> None:
    state_root, first = built_fixture
    second = build_local_evaluation_fixture(
        LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 917)
    )
    assert second.reused is True
    assert second.descriptor_sha256 == first.descriptor_sha256
    assert second.action_log_partitions == first.action_log_partitions
    assert second.judge == first.judge


def test_request_root_must_be_existing_absolute_directory(tmp_path: Path) -> None:
    file_root = tmp_path / "file-root"
    file_root.write_text("not a directory", encoding="utf-8")
    requests = (
        LocalEvaluationFixtureRequest(Path("relative"), EVALUATION_DATE, 1),
        LocalEvaluationFixtureRequest(tmp_path / "missing", EVALUATION_DATE, 1),
        LocalEvaluationFixtureRequest(file_root, EVALUATION_DATE, 1),
    )
    for request in requests:
        with pytest.raises(StageCError) as caught:
            build_local_evaluation_fixture(request)
        assert caught.value.code == StageCErrorCode.FIXTURE_REQUEST_INVALID
        assert str(request.judge_state_root) not in str(caught.value)


def test_tampered_complete_fixture_conflicts(built_fixture) -> None:
    state_root, receipt = built_fixture
    target = receipt.fixture_root / "action_log" / "dt=2026-08-30" / "part-0.parquet"
    original = target.read_bytes()
    target.write_bytes(original + b"tamper")
    try:
        with pytest.raises(StageCError) as caught:
            build_local_evaluation_fixture(
                LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 917)
            )
        assert caught.value.code == StageCErrorCode.FIXTURE_STATE_CONFLICT
    finally:
        target.write_bytes(original)


def test_snapshot_artifact_row_count_tamper_conflicts(built_fixture) -> None:
    state_root, receipt = built_fixture
    manifest_path = receipt.judge.snapshot_root / "manifest.json"
    payload = manifest_path.read_text(encoding="utf-8")
    manifest = EvaluationSnapshotManifest.model_validate_json(payload)
    changed = manifest.model_copy(
        update={
            "validation": manifest.validation.__class__(
                evaluation_id=manifest.validation.evaluation_id,
                counts=manifest.validation.counts,
                optional_non_null_ratio=manifest.validation.optional_non_null_ratio,
                artifacts=manifest.validation.artifacts.__class__(
                    slate=manifest.validation.artifacts.slate.__class__(
                        relative_path=manifest.validation.artifacts.slate.relative_path,
                        rows=manifest.validation.artifacts.slate.rows + 1,
                        sha256=manifest.validation.artifacts.slate.sha256,
                    ),
                    labels=manifest.validation.artifacts.labels,
                ),
            )
        }
    )
    manifest_path.write_text(changed.model_dump_json(), encoding="utf-8")
    try:
        with pytest.raises(StageCError) as caught:
            build_local_evaluation_fixture(
                LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 917)
            )
        assert caught.value.code == StageCErrorCode.FIXTURE_STATE_CONFLICT
    finally:
        manifest_path.write_text(payload, encoding="utf-8")


def test_fixture_contains_no_candidate_or_workspace_side_effects(built_fixture) -> None:
    _, receipt = built_fixture
    relative_files = {
        path.relative_to(receipt.fixture_root).as_posix()
        for path in receipt.fixture_root.rglob("*")
        if path.is_file()
    }
    assert not any("candidate" in path or "workspace" in path for path in relative_files)
    assert pq.read_metadata(
        receipt.fixture_root / "inputs" / "virtual_users.parquet"
    ).num_rows == 200


def test_independent_roots_and_candidate_views_are_reproducible(
    independent_fixture_pair, tmp_path_factory: pytest.TempPathFactory
) -> None:
    first, same = independent_fixture_pair
    first_destination = tmp_path_factory.mktemp("candidate-first")
    second_destination = tmp_path_factory.mktemp("candidate-second")

    _verify_independent_fixture_reproduction(first, same)
    assert first.reused is False
    assert same.reused is False
    assert first.fixture_root.resolve() != same.fixture_root.resolve()

    first_source = FixtureActionLogSource(first.fixture_root, first.descriptor_sha256)
    same_source = FixtureActionLogSource(same.fixture_root, same.descriptor_sha256)
    assert first_source.opaque_root == same_source.opaque_root
    assert first_source.partition_uri(EVALUATION_DATE) == same_source.partition_uri(
        EVALUATION_DATE
    )

    first_view = materialize_candidate_data_view(
        CandidateDataViewRequest(first.judge, first_destination),
        source=first_source,
    )
    second_view = materialize_candidate_data_view(
        CandidateDataViewRequest(same.judge, second_destination),
        source=same_source,
    )
    assert first_view.reused is False
    assert second_view.reused is False
    assert first_view.manifest == second_view.manifest
    assert first_view.manifest_sha256 == second_view.manifest_sha256
    first_tree = {
        path.relative_to(first_view.root).as_posix(): path.read_bytes()
        for path in first_view.root.rglob("*")
        if path.is_file()
    }
    second_tree = {
        path.relative_to(second_view.root).as_posix(): path.read_bytes()
        for path in second_view.root.rglob("*")
        if path.is_file()
    }
    assert first_tree == second_tree
    assert set(first_tree) == {
        "candidate-view.json",
        "history/action_log/dt=2026-08-30/part-0.parquet",
        "history/action_log/dt=2026-08-31/part-0.parquet",
        "slate.parquet",
    }
    candidate_payload = "\n".join(
        (
            repr(first_view),
            *(path for path in first_tree),
            *(payload.decode("latin-1") for payload in first_tree.values()),
        )
    )
    snapshot = EvaluationSnapshotManifest.model_validate_json(
        _long_path(first.judge.snapshot_root / "manifest.json").read_bytes()
    )
    forbidden = (
        "labels.parquet",
        "final_holdout",
        str(first.judge.final_holdout_id),
        str(first.judge.snapshot_fingerprint),
        snapshot.source.root,
        "fixture_seed",
        "virtual_users",
        str(first.fixture_root),
        str(same.fixture_root),
    )
    assert all(value not in candidate_payload for value in forbidden)


def test_reproducibility_verifier_reports_sanitized_typed_mismatch(
    independent_fixture_pair,
) -> None:
    first, second = independent_fixture_pair
    controlled_difference = replace(
        second,
        descriptor_sha256="0" * 64,
    )

    with pytest.raises(StageCError) as caught:
        _verify_independent_fixture_reproduction(first, controlled_difference)

    assert caught.value.code == StageCErrorCode.FIXTURE_REPRODUCIBILITY_MISMATCH
    assert str(first.fixture_root) not in str(caught.value)
    assert str(second.fixture_root) not in str(caught.value)
    assert "917" not in str(caught.value)
    assert "user" not in str(caught.value).lower()


def test_reproducibility_verifier_rejects_cross_root_snapshot_redirect(
    independent_fixture_pair,
) -> None:
    first, second = independent_fixture_pair
    redirected = replace(
        second,
        judge=replace(second.judge, snapshot_root=first.judge.snapshot_root),
    )

    with pytest.raises(StageCError) as caught:
        _verify_independent_fixture_reproduction(first, redirected)

    assert caught.value.code == StageCErrorCode.FIXTURE_REPRODUCIBILITY_MISMATCH
    assert str(first.fixture_root) not in str(caught.value)
    assert str(second.fixture_root) not in str(caught.value)


@pytest.mark.parametrize("spoofed_field", ("fixture_root", "descriptor_path"))
def test_reproducibility_verifier_rejects_receipt_path_spoof(
    independent_fixture_pair,
    spoofed_field: str,
) -> None:
    first, second = independent_fixture_pair
    spoofed = replace(second, **{spoofed_field: getattr(first, spoofed_field)})

    with pytest.raises(StageCError) as caught:
        _verify_independent_fixture_reproduction(first, spoofed)

    assert caught.value.code == StageCErrorCode.FIXTURE_REPRODUCIBILITY_MISMATCH


def test_fixture_snapshot_clock_is_deterministic(independent_fixture_pair) -> None:
    first, second = independent_fixture_pair
    manifests = tuple(
        EvaluationSnapshotManifest.model_validate_json(
            _long_path(receipt.judge.snapshot_root / "manifest.json").read_bytes()
        )
        for receipt in (first, second)
    )

    assert manifests[0].created_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert manifests[1].created_at == manifests[0].created_at


def test_slate_projection_preserves_stored_order_and_duplicates(tmp_path: Path) -> None:
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    pq.write_table(pa.table({"slate_id": ["a", "b"]}), first)
    pq.write_table(pa.table({"slate_id": ["b", "a", "a"]}), second)

    assert _slate_projection_sha256(tmp_path, first.name) != _slate_projection_sha256(
        tmp_path,
        second.name,
    )


def test_missing_success_marker_is_partial_target_conflict(built_fixture) -> None:
    state_root, receipt = built_fixture
    marker = receipt.fixture_root / "_SUCCESS"
    payload = marker.read_bytes()
    marker.unlink()
    try:
        with pytest.raises(StageCError) as caught:
            build_local_evaluation_fixture(
                LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 917)
            )
        assert caught.value.code == StageCErrorCode.FIXTURE_STATE_CONFLICT
    finally:
        marker.write_bytes(payload)


def test_hardlink_alias_in_complete_fixture_conflicts(built_fixture) -> None:
    state_root, receipt = built_fixture
    alias = receipt.fixture_root / "descriptor-alias"
    try:
        os.link(receipt.descriptor_path, alias)
    except OSError:
        pytest.skip("hardlink creation is unavailable on this filesystem")
    try:
        with pytest.raises(StageCError) as caught:
            build_local_evaluation_fixture(
                LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 917)
            )
        assert caught.value.code == StageCErrorCode.FIXTURE_STATE_CONFLICT
    finally:
        alias.unlink()


@pytest.mark.parametrize("kind", ("file", "directory"))
def test_extra_fixture_entry_conflicts(built_fixture, kind: str) -> None:
    state_root, receipt = built_fixture
    extra = receipt.fixture_root / f"extra-{kind}"
    extra.write_text("extra", encoding="utf-8") if kind == "file" else extra.mkdir()
    try:
        with pytest.raises(StageCError) as caught:
            build_local_evaluation_fixture(
                LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 917)
            )
        assert caught.value.code == StageCErrorCode.FIXTURE_STATE_CONFLICT
    finally:
        extra.unlink() if kind == "file" else extra.rmdir()


@pytest.mark.parametrize("kind", ("file", "directory"))
def test_extra_snapshot_entry_is_invalid_handoff_and_fixture_conflict(
    built_fixture, kind: str
) -> None:
    state_root, receipt = built_fixture
    extra = receipt.judge.snapshot_root / f"extra-{kind}"
    io_extra = _long_path(extra)
    io_extra.write_text("extra", encoding="utf-8") if kind == "file" else io_extra.mkdir()
    try:
        with pytest.raises(StageCError) as handoff_error:
            _validated_judge_handoff(
                receipt.judge.snapshot_root,
                expected_fingerprint=str(receipt.judge.snapshot_fingerprint),
            )
        assert handoff_error.value.code == StageCErrorCode.JUDGE_HANDOFF_INVALID
        with pytest.raises(StageCError) as fixture_error:
            build_local_evaluation_fixture(
                LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 917)
            )
        assert fixture_error.value.code == StageCErrorCode.FIXTURE_STATE_CONFLICT
    finally:
        io_extra.unlink() if kind == "file" else io_extra.rmdir()


def test_outer_integrity_marker_extra_field_conflicts(built_fixture) -> None:
    state_root, receipt = built_fixture
    marker = receipt.fixture_root / "_SUCCESS"
    original = marker.read_bytes()
    payload = json.loads(original)
    payload["unexpected"] = True
    marker.write_text(json.dumps(payload), encoding="utf-8")
    try:
        with pytest.raises(StageCError) as caught:
            build_local_evaluation_fixture(
                LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 917)
            )
        assert caught.value.code == StageCErrorCode.FIXTURE_STATE_CONFLICT
    finally:
        marker.write_bytes(original)


def test_coverage_insufficient_is_typed_and_does_not_publish(
    built_fixture, tmp_path: Path
) -> None:
    _, receipt = built_fixture
    copied = tmp_path / receipt.judge.snapshot_fingerprint
    _copy_snapshot(receipt.judge.snapshot_root, copied)
    manifest_path = copied / "manifest.json"
    manifest = EvaluationSnapshotManifest.model_validate_json(manifest_path.read_bytes())
    low_counts = manifest.validation.counts.__class__(
        user_count=manifest.validation.counts.user_count,
        slate_count=manifest.validation.counts.slate_count,
        row_count=manifest.validation.counts.row_count,
        clicked_row_count=0,
        click_positive_slate_count=0,
        click_positive_slate_ratio=0.0,
        mean_slate_size=24.0,
    )
    changed = manifest.model_copy(
        update={
            "validation": manifest.validation.__class__(
                evaluation_id=manifest.validation.evaluation_id,
                counts=low_counts,
                optional_non_null_ratio=manifest.validation.optional_non_null_ratio,
                artifacts=manifest.validation.artifacts,
            )
        }
    )
    manifest_path.write_bytes(canonical_json_bytes(changed.model_dump(mode="json")))
    with pytest.raises(StageCError) as caught:
        _validate_coverage(copied)
    assert caught.value.code == StageCErrorCode.FIXTURE_COVERAGE_INSUFFICIENT
    assert not (tmp_path / "_SUCCESS").exists()


def test_coverage_requires_exact_split_user_counts(
    built_fixture, tmp_path: Path
) -> None:
    _, receipt = built_fixture
    copied = tmp_path / receipt.judge.snapshot_fingerprint
    _copy_snapshot(receipt.judge.snapshot_root, copied)
    manifest_path = copied / "manifest.json"
    manifest = EvaluationSnapshotManifest.model_validate_json(manifest_path.read_bytes())
    counts = manifest.validation.counts
    changed_counts = counts.__class__(
        user_count=159,
        slate_count=counts.slate_count,
        row_count=counts.row_count,
        clicked_row_count=counts.clicked_row_count,
        click_positive_slate_count=counts.click_positive_slate_count,
        click_positive_slate_ratio=counts.click_positive_slate_ratio,
        mean_slate_size=counts.mean_slate_size,
    )
    changed = manifest.model_copy(
        update={
            "validation": manifest.validation.__class__(
                evaluation_id=manifest.validation.evaluation_id,
                counts=changed_counts,
                optional_non_null_ratio=manifest.validation.optional_non_null_ratio,
                artifacts=manifest.validation.artifacts,
            )
        }
    )
    manifest_path.write_bytes(canonical_json_bytes(changed.model_dump(mode="json")))

    with pytest.raises(StageCError) as caught:
        _validate_coverage(copied)
    assert caught.value.code == StageCErrorCode.FIXTURE_COVERAGE_INSUFFICIENT


def test_coverage_rejects_when_only_some_evaluation_slates_are_click_positive(
    built_fixture, tmp_path: Path
) -> None:
    _, receipt = built_fixture
    copied = tmp_path / receipt.judge.snapshot_fingerprint
    _copy_snapshot(receipt.judge.snapshot_root, copied)
    manifest_path = copied / "manifest.json"
    manifest = EvaluationSnapshotManifest.model_validate_json(manifest_path.read_bytes())
    labels_path = copied / manifest.validation.artifacts.labels.relative_path
    labels = pq.read_table(labels_path)
    rows = labels.to_pylist()
    positive_slates = sorted({row["slate_id"] for row in rows})[:40]
    positive_set = set(positive_slates)
    for row in rows:
        row["clicked"] = row["clicked"] and row["slate_id"] in positive_set
    pq.write_table(pa.Table.from_pylist(rows, schema=labels.schema), labels_path)
    counts = manifest.validation.counts
    changed_counts = counts.__class__(
        user_count=counts.user_count,
        slate_count=counts.slate_count,
        row_count=counts.row_count,
        clicked_row_count=40,
        click_positive_slate_count=40,
        click_positive_slate_ratio=0.25,
        mean_slate_size=counts.mean_slate_size,
    )
    changed = manifest.model_copy(
        update={
            "validation": manifest.validation.__class__(
                evaluation_id=manifest.validation.evaluation_id,
                counts=changed_counts,
                optional_non_null_ratio=manifest.validation.optional_non_null_ratio,
                artifacts=manifest.validation.artifacts,
            )
        }
    )
    manifest_path.write_bytes(canonical_json_bytes(changed.model_dump(mode="json")))

    with pytest.raises(StageCError) as caught:
        _validate_coverage(copied)
    assert caught.value.code == StageCErrorCode.FIXTURE_COVERAGE_INSUFFICIENT


@pytest.mark.parametrize(
    "tamper",
    ("success", "schema", "fingerprint", "manifest_sha", "artifact_digest", "artifact_rows"),
)
def test_judge_handoff_reopens_and_validates_every_sealed_component(
    built_fixture, tmp_path: Path, tamper: str
) -> None:
    _, receipt = built_fixture
    copied = tmp_path / tamper / receipt.judge.snapshot_fingerprint
    _copy_snapshot(receipt.judge.snapshot_root, copied)
    manifest_path = copied / "manifest.json"
    manifest = EvaluationSnapshotManifest.model_validate_json(manifest_path.read_bytes())
    if tamper == "success":
        (copied / "_SUCCESS").write_text("wrong\n", encoding="utf-8")
    elif tamper == "schema":
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["unexpected"] = True
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    elif tamper == "fingerprint":
        manifest_path.write_bytes(
            canonical_json_bytes(
                manifest.model_copy(update={"snapshot_fingerprint": "0" * 64}).model_dump(
                    mode="json"
                )
            )
        )
    elif tamper == "manifest_sha":
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
    elif tamper == "artifact_digest":
        path = copied / manifest.validation.artifacts.slate.relative_path
        path.write_bytes(path.read_bytes() + b"tamper")
    else:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["validation"]["artifacts"]["slate"]["rows"] += 1
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StageCError) as caught:
        _validated_judge_handoff(
            copied,
            expected_fingerprint=str(receipt.judge.snapshot_fingerprint),
        )
    assert caught.value.code == StageCErrorCode.JUDGE_HANDOFF_INVALID


def test_reparse_state_root_is_rejected_without_path_disclosure(tmp_path: Path) -> None:
    physical = tmp_path / "physical"
    linked = tmp_path / "linked"
    physical.mkdir()
    try:
        os.symlink(physical, linked, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    with pytest.raises(StageCError) as caught:
        build_local_evaluation_fixture(
            LocalEvaluationFixtureRequest(linked, EVALUATION_DATE, 1)
        )
    assert caught.value.code == StageCErrorCode.FIXTURE_REQUEST_INVALID
    assert str(linked) not in str(caught.value)


@pytest.mark.parametrize("component", ("fixtures", "by-hash"))
def test_derived_fixture_root_does_not_follow_directory_links(
    tmp_path: Path, component: str
) -> None:
    state_root = tmp_path / "state"
    external = tmp_path / "external"
    state_root.mkdir()
    external.mkdir()
    if component == "fixtures":
        link = state_root / "fixtures"
    else:
        (state_root / "fixtures").mkdir()
        link = state_root / "fixtures" / "by-hash"
    _make_directory_link(link, external)
    try:
        with pytest.raises(StageCError) as caught:
            build_local_evaluation_fixture(
                LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 917)
            )
        assert caught.value.code == StageCErrorCode.FIXTURE_REQUEST_INVALID
        assert not tuple(external.iterdir())
        assert str(external) not in str(caught.value)
    finally:
        _remove_directory_link(link)


def test_preexisting_hardlinked_descriptor_lock_is_not_opened_or_modified(
    built_fixture, tmp_path: Path
) -> None:
    _, receipt = built_fixture
    state_root = tmp_path / "state"
    output_root = state_root / "fixtures" / "by-hash"
    output_root.mkdir(parents=True)
    external_lock = tmp_path / "external-lock"
    external_lock.write_bytes(b"external-content")
    lock_path = output_root / f".{receipt.descriptor_sha256}.lock"
    try:
        os.link(external_lock, lock_path)
    except OSError:
        pytest.skip("hardlink creation is unavailable")

    with pytest.raises(StageCError) as caught:
        build_local_evaluation_fixture(
            LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 917)
        )
    assert caught.value.code == StageCErrorCode.FIXTURE_STATE_CONFLICT
    assert external_lock.read_bytes() == b"external-content"
    assert not (output_root / receipt.descriptor_sha256).exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_extra_fifo_in_complete_fixture_conflicts(built_fixture) -> None:
    state_root, receipt = built_fixture
    fifo = receipt.fixture_root / "extra-fifo"
    os.mkfifo(fifo)
    try:
        with pytest.raises(StageCError) as caught:
            build_local_evaluation_fixture(
                LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 917)
            )
        assert caught.value.code == StageCErrorCode.FIXTURE_STATE_CONFLICT
    finally:
        fifo.unlink()


def test_staging_cleanup_failure_warns_without_replacing_success(
    built_fixture,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state_root, _ = built_fixture
    private_path = "private-cleanup-path"

    def fail_cleanup(_path: Path) -> None:
        raise OSError(private_path)

    monkeypatch.setattr(fixture_module.shutil, "rmtree", fail_cleanup)
    with caplog.at_level("WARNING", logger=fixture_module.__name__):
        receipt = build_local_evaluation_fixture(
            LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 917)
        )

    assert receipt.reused is True
    assert "fixture_staging_cleanup_failed" in caplog.text
    assert private_path not in caplog.text

    receipt.fixture_root.joinpath("_SUCCESS").write_bytes(b"{}")
    with pytest.raises(StageCError) as caught:
        build_local_evaluation_fixture(
            LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 917)
        )
    assert caught.value.code == StageCErrorCode.FIXTURE_STATE_CONFLICT
    assert caught.value.stage == "fixture_reuse_validation"


def test_concurrent_builders_publish_once_and_reuse_once(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    request = LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 1234)

    with ProcessPoolExecutor(max_workers=2) as executor:
        receipts = tuple(executor.map(build_local_evaluation_fixture, (request, request)))

    assert sorted(receipt.reused for receipt in receipts) == [False, True]
    assert receipts[0].fixture_root == receipts[1].fixture_root
    by_hash = state_root / "fixtures" / "by-hash"
    assert not tuple(by_hash.glob(".staging-*"))
    assert build_local_evaluation_fixture(request).reused is True


@pytest.mark.parametrize("boundary", ("producer", "snapshot"))
def test_domain_failures_are_mapped_without_original_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    secret_context = str(tmp_path / "private-user-input")
    state_root = tmp_path / boundary
    state_root.mkdir()
    if boundary == "producer":
        def fail_producer(**_kwargs):
            raise ActionLogGenerationError(secret_context)

        monkeypatch.setattr(fixture_module, "run_daily_action_log", fail_producer)
    else:
        monkeypatch.setattr(fixture_module, "run_daily_action_log", lambda **_kwargs: {})

        def fail_snapshot(*_args, **_kwargs):
            raise EvaluationSnapshotError(
                SnapshotErrorCode.SOURCE_PARTITION_MISSING,
                secret_context,
            )

        monkeypatch.setattr(fixture_module, "_build_evaluation_snapshot", fail_snapshot)

    with pytest.raises(StageCError) as caught:
        build_local_evaluation_fixture(
            LocalEvaluationFixtureRequest(state_root, EVALUATION_DATE, 917)
        )
    assert caught.value.code == StageCErrorCode.FIXTURE_STATE_CONFLICT
    assert secret_context not in str(caught.value)
    assert caught.value.__suppress_context__ is True
    assert secret_context not in "".join(traceback.format_exception(caught.value))
