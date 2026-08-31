import json
from dataclasses import replace
from pathlib import Path

import pytest

from autoresearch.research_harness.evaluation_artifacts import (
    calculate_snapshot_fingerprint,
    canonical_json_bytes,
)
from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.snapshot_publisher import publish_snapshot
from tests.research_harness.test_snapshot_publisher import prepared_staging


def test_bad_success_marker_raises_conflict_and_preserves_target(tmp_path: Path) -> None:
    # Given
    first, manifest = prepared_staging(tmp_path, "first")
    target = publish_snapshot(first, tmp_path / "output", manifest).target_path
    marker = target / "_SUCCESS"
    marker.write_text("wrong\n", encoding="utf-8")
    staging, retry_manifest = prepared_staging(tmp_path, "retry")

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        publish_snapshot(staging, tmp_path / "output", retry_manifest)

    # Then
    assert (captured.value.code, marker.read_text(encoding="utf-8")) == (
        SnapshotErrorCode.SNAPSHOT_WRITE_CONFLICT,
        "wrong\n",
    )


def test_missing_existing_artifact_raises_conflict(tmp_path: Path) -> None:
    # Given
    first, manifest = prepared_staging(tmp_path, "first")
    target = publish_snapshot(first, tmp_path / "output", manifest).target_path
    missing = target / manifest.validation.artifacts.slate.relative_path
    missing.unlink()
    staging, retry_manifest = prepared_staging(tmp_path, "retry")

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        publish_snapshot(staging, tmp_path / "output", retry_manifest)

    # Then
    assert (captured.value.code, missing.exists()) == (
        SnapshotErrorCode.SNAPSHOT_WRITE_CONFLICT,
        False,
    )


def test_digest_changed_existing_artifact_raises_conflict_and_preserves_bytes(
    tmp_path: Path,
) -> None:
    # Given
    first, manifest = prepared_staging(tmp_path, "first")
    target = publish_snapshot(first, tmp_path / "output", manifest).target_path
    changed = target / manifest.validation.artifacts.labels.relative_path
    changed.write_bytes(b"tampered-artifact")
    staging, retry_manifest = prepared_staging(tmp_path, "retry")

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        publish_snapshot(staging, tmp_path / "output", retry_manifest)

    # Then
    assert (captured.value.code, changed.read_bytes()) == (
        SnapshotErrorCode.SNAPSHOT_WRITE_CONFLICT,
        b"tampered-artifact",
    )


def test_non_artifact_manifest_tamper_raises_conflict(tmp_path: Path) -> None:
    # Given
    first, manifest = prepared_staging(tmp_path, "first")
    target = publish_snapshot(first, tmp_path / "output", manifest).target_path
    manifest_path = target / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["source"]["root"] = "memory://different"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    staging, retry_manifest = prepared_staging(tmp_path, "retry")

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        publish_snapshot(staging, tmp_path / "output", retry_manifest)

    # Then
    assert (captured.value.code, json.loads(manifest_path.read_text())["source"]["root"]) == (
        SnapshotErrorCode.SNAPSHOT_WRITE_CONFLICT,
        "memory://different",
    )


def test_expected_staging_manifest_tamper_raises_conflict(tmp_path: Path) -> None:
    # Given
    staging, manifest = prepared_staging(tmp_path)
    tampered = manifest.model_copy(
        update={"source": replace(manifest.source, root="memory://different")}
    )

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        publish_snapshot(staging, tmp_path / "output", tampered)

    # Then
    assert (captured.value.code, staging.exists()) == (
        SnapshotErrorCode.SNAPSHOT_WRITE_CONFLICT,
        False,
    )


def test_artifact_row_count_mismatch_in_staging_manifest_raises_conflict(
    tmp_path: Path,
) -> None:
    # Given
    staging, manifest = prepared_staging(tmp_path)
    slate = replace(manifest.validation.artifacts.slate, rows=99)
    validation = replace(
        manifest.validation,
        artifacts=replace(manifest.validation.artifacts, slate=slate),
    )
    changed = manifest.model_copy(update={"validation": validation})
    changed = changed.model_copy(
        update={"snapshot_fingerprint": calculate_snapshot_fingerprint(changed)}
    )
    (staging / "manifest.json").write_bytes(
        canonical_json_bytes(changed.model_dump(mode="json"))
    )

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        publish_snapshot(staging, tmp_path / "output", changed)

    # Then
    assert captured.value.code == SnapshotErrorCode.SNAPSHOT_WRITE_CONFLICT
