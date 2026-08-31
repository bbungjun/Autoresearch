from dataclasses import replace
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from autoresearch.research_harness.evaluation_artifacts import (
    calculate_snapshot_fingerprint,
    canonical_json_bytes,
)
from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.snapshot_publisher import (
    _artifact_path,
    publish_snapshot,
)
from tests.research_harness.test_snapshot_publisher import prepared_staging


def test_windows_drive_relative_and_absolute_artifact_paths_are_rejected(
    tmp_path: Path,
) -> None:
    # Given
    drive_relative = r"C:sensitive\slate.parquet"
    drive_absolute = r"C:\sensitive\slate.parquet"
    slash_absolute = "C:/sensitive/slate.parquet"

    # When
    paths = tuple(
        _artifact_path(tmp_path, value)
        for value in (drive_relative, drive_absolute, slash_absolute)
    )

    # Then
    assert paths == (None, None, None)


def test_unc_and_windows_device_artifact_paths_are_rejected(tmp_path: Path) -> None:
    # Given
    unc = r"\\server\share\slate.parquet"
    device = r"\\?\C:\sensitive\slate.parquet"

    # When
    paths = (_artifact_path(tmp_path, unc), _artifact_path(tmp_path, device))

    # Then
    assert paths == (None, None)


def test_noncanonical_relative_artifact_paths_are_rejected(tmp_path: Path) -> None:
    # Given
    invalid = (
        "/absolute/slate.parquet",
        "../escape.parquet",
        "validation\\slate.parquet",
        "validation//slate.parquet",
        "./validation/slate.parquet",
    )

    # When
    paths = tuple(_artifact_path(tmp_path, value) for value in invalid)

    # Then
    assert paths == (None,) * len(invalid)


def test_junction_or_symlink_artifact_escape_raises_conflict(tmp_path: Path) -> None:
    # Given
    staging, manifest = prepared_staging(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    source = staging / manifest.validation.artifacts.slate.relative_path
    shutil.copy2(source, external / "slate.parquet")
    link = staging / "linked"
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(external)],
            check=False,
            capture_output=True,
        )
        assert completed.returncode == 0
    else:
        link.symlink_to(external, target_is_directory=True)
    receipt = replace(
        manifest.validation.artifacts.slate,
        relative_path="linked/slate.parquet",
    )
    validation = replace(
        manifest.validation,
        artifacts=replace(manifest.validation.artifacts, slate=receipt),
    )
    changed = manifest.model_copy(update={"validation": validation})
    changed = changed.model_copy(
        update={"snapshot_fingerprint": calculate_snapshot_fingerprint(changed)}
    )
    (staging / "manifest.json").write_bytes(
        canonical_json_bytes(changed.model_dump(mode="json"))
    )
    target = tmp_path / "output" / changed.snapshot_fingerprint

    # When
    try:
        with pytest.raises(EvaluationSnapshotError) as captured:
            publish_snapshot(staging, tmp_path / "output", changed)
    finally:
        published_link = target / "linked"
        if published_link.exists():
            os.rmdir(published_link)

    # Then
    assert captured.value.code == SnapshotErrorCode.SNAPSHOT_WRITE_CONFLICT
