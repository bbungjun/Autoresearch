from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from autoresearch.research_harness.evaluation_artifacts import write_snapshot_artifacts
from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.evaluation_snapshot_models import (
    AttributedImpression,
    EvaluationSnapshotRequest,
    EvaluationSplit,
    EvaluationSnapshotManifest,
    EvaluationWindow,
    SnapshotArtifactInput,
)
from autoresearch.research_harness.evaluation_source_models import SourcePartitionReceipt
from autoresearch.research_harness.snapshot_publisher import publish_snapshot


def artifact_input(output_root: Path) -> SnapshotArtifactInput:
    partitions = tuple(
        SourcePartitionReceipt(
            dt=day,
            uri=f"memory://fixture/dt={day.isoformat()}/part-0.parquet",
            rows=index,
            sha256=f"{index:064x}",
        )
        for index, day in enumerate(
            (date(2026, 8, 30), date(2026, 8, 31), date(2026, 9, 1)), start=1
        )
    )
    request = EvaluationSnapshotRequest(
        action_log_root="memory://fixture",
        history_start_date=date(2026, 8, 30),
        evaluation_start_date=date(2026, 9, 1),
        evaluation_end_date=date(2026, 9, 1),
        slate_id_cutover_date=date(2026, 8, 30),
        output_root=output_root,
    )
    window = EvaluationWindow(
        history_start_date=request.history_start_date,
        evaluation_start_date=request.evaluation_start_date,
        evaluation_end_date=request.evaluation_end_date,
        label_scan_end_date=date(2026, 9, 2),
        complete_history_label_end_date=date(2026, 8, 30),
        candidate_history_partitions=partitions[:2],
    )

    def row(user_id: str, slate_id: str, video_id: str) -> AttributedImpression:
        return AttributedImpression(
            slate_id=slate_id,
            user_id=user_id,
            video_id=video_id,
            event_timestamp=datetime(2026, 9, 1, 0, int(video_id[-1]), tzinfo=UTC),
            source_event_id=f"evt_20260901_0000000{video_id[-1]}",
            clicked=video_id == "v1",
            original_rank=int(video_id[-1]),
            candidate_source="model",
        )

    return SnapshotArtifactInput(
        request=request,
        window=window,
        partitions=partitions,
        validation=EvaluationSplit(
            name="validation",
            rows=(
                row("validation-user", "validation-slate", "v1"),
                row("validation-user", "validation-slate", "v2"),
            ),
            user_ids=("validation-user",),
        ),
        final_holdout=EvaluationSplit(
            name="final_holdout",
            rows=(
                row("final-user", "final-slate", "v1"),
                row("final-user", "final-slate", "v2"),
            ),
            user_ids=("final-user",),
        ),
        created_at=datetime(2026, 9, 2, tzinfo=UTC),
    )


def prepared_staging(
    tmp_path: Path, name: str = "staging"
) -> tuple[Path, EvaluationSnapshotManifest]:
    staging = tmp_path / name
    manifest = write_snapshot_artifacts(staging, artifact_input(tmp_path / "output"))
    return staging, manifest


def test_fresh_snapshot_is_renamed_to_fingerprint_target(tmp_path: Path) -> None:
    # Given
    staging, manifest = prepared_staging(tmp_path)
    output_root = tmp_path / "output"

    # When
    receipt = publish_snapshot(staging, output_root, manifest)

    # Then
    assert (receipt.target_path, receipt.reused, staging.exists()) == (
        output_root / manifest.snapshot_fingerprint,
        False,
        False,
    )


def test_identical_existing_snapshot_is_reused(tmp_path: Path) -> None:
    # Given
    first_staging, manifest = prepared_staging(tmp_path, "first")
    publish_snapshot(first_staging, tmp_path / "output", manifest)
    second_staging, second_manifest = prepared_staging(tmp_path, "second")

    # When
    receipt = publish_snapshot(second_staging, tmp_path / "output", second_manifest)

    # Then
    assert (receipt.reused, second_staging.exists()) == (True, False)


def test_partial_existing_target_raises_typed_conflict_without_mutation(
    tmp_path: Path,
) -> None:
    # Given
    staging, manifest = prepared_staging(tmp_path)
    target = tmp_path / "output" / manifest.snapshot_fingerprint
    target.mkdir(parents=True)
    sentinel = target / "old.bin"
    sentinel.write_bytes(b"old-target")

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        publish_snapshot(staging, tmp_path / "output", manifest)

    # Then
    assert (captured.value.code, sentinel.read_bytes()) == (
        SnapshotErrorCode.SNAPSHOT_WRITE_CONFLICT,
        b"old-target",
    )


def test_identical_snapshot_with_different_created_at_is_reused(tmp_path: Path) -> None:
    # Given
    output_root = tmp_path / "output"
    first_input = artifact_input(output_root)
    first = tmp_path / "first"
    first_manifest = write_snapshot_artifacts(first, first_input)
    publish_snapshot(first, output_root, first_manifest)
    second = tmp_path / "second"
    second_manifest = write_snapshot_artifacts(
        second,
        replace(first_input, created_at=datetime(2026, 9, 3, tzinfo=UTC)),
    )

    # When
    receipt = publish_snapshot(second, output_root, second_manifest)

    # Then
    assert (
        first_manifest.created_at != second_manifest.created_at,
        receipt.reused,
    ) == (True, True)
