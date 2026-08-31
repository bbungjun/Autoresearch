from concurrent.futures import ThreadPoolExecutor
import errno
import os
from pathlib import Path
from threading import Barrier

import pytest

from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.snapshot_publisher import publish_snapshot
from autoresearch.research_harness.evaluation_snapshot_models import (
    EvaluationSnapshotManifest,
    EvaluationSnapshotReceipt,
)
from tests.research_harness.test_snapshot_publisher import prepared_staging


def test_two_concurrent_identical_publishers_cooperate(tmp_path: Path) -> None:
    # Given
    first, first_manifest = prepared_staging(tmp_path, "first")
    second, second_manifest = prepared_staging(tmp_path, "second")
    gate = Barrier(2)

    def publish(
        staging: Path, manifest: EvaluationSnapshotManifest
    ) -> EvaluationSnapshotReceipt:
        gate.wait()
        return publish_snapshot(staging, tmp_path / "output", manifest)

    # When
    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = tuple(
            future.result()
            for future in (
                executor.submit(publish, first, first_manifest),
                executor.submit(publish, second, second_manifest),
            )
        )

    # Then
    assert sorted(receipt.reused for receipt in receipts) == [False, True]


def test_stale_unheld_lock_file_does_not_block_publish(tmp_path: Path) -> None:
    # Given
    staging, manifest = prepared_staging(tmp_path)
    output_root = tmp_path / "output"
    output_root.mkdir()
    lock_path = output_root / f".{manifest.snapshot_fingerprint}.lock"
    lock_path.write_bytes(b"stale")

    # When
    receipt = publish_snapshot(staging, output_root, manifest)

    # Then
    assert (receipt.reused, lock_path.read_bytes()) == (False, b"")


def test_rename_failure_is_typed_and_cleans_owned_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    staging, manifest = prepared_staging(tmp_path)

    def fail_rename(_source: Path, _target: Path) -> Path:
        raise OSError("sensitive absolute path must not escape")

    monkeypatch.setattr(Path, "rename", fail_rename)

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        publish_snapshot(staging, tmp_path / "output", manifest)

    # Then
    assert (
        captured.value.code,
        staging.exists(),
        "sensitive" in str(captured.value),
    ) == (SnapshotErrorCode.SNAPSHOT_WRITE_CONFLICT, False, False)


@pytest.mark.skipif(os.name != "nt", reason="Windows msvcrt lock semantics")
def test_windows_contention_beyond_crt_retry_limit_eventually_reuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    import msvcrt

    first, manifest = prepared_staging(tmp_path, "first")
    publish_snapshot(first, tmp_path / "output", manifest)
    retry, retry_manifest = prepared_staging(tmp_path, "retry")
    attempts = 0

    def contend_then_acquire(_fd: int, mode: int, _length: int) -> None:
        nonlocal attempts
        if mode == msvcrt.LK_UNLCK:
            return
        attempts += 1
        if attempts <= 11:
            error = PermissionError(errno.EACCES, "lock contention")
            error.winerror = 33
            raise error

    monkeypatch.setattr(msvcrt, "locking", contend_then_acquire)

    # When
    receipt = publish_snapshot(retry, tmp_path / "output", retry_manifest)

    # Then
    assert (attempts, receipt.reused, retry.exists()) == (12, True, False)


@pytest.mark.skipif(os.name != "nt", reason="Windows msvcrt lock semantics")
def test_non_contention_lock_failure_is_typed_sanitized_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    import msvcrt

    staging, manifest = prepared_staging(tmp_path)

    def fail_acquire(_fd: int, mode: int, _length: int) -> None:
        if mode != msvcrt.LK_UNLCK:
            error = PermissionError(errno.EACCES, "sensitive-user-path")
            error.winerror = 5
            raise error

    monkeypatch.setattr(msvcrt, "locking", fail_acquire)

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        publish_snapshot(staging, tmp_path / "output", manifest)

    # Then
    assert (
        captured.value.code,
        staging.exists(),
        "sensitive" in str(captured.value),
    ) == (SnapshotErrorCode.SNAPSHOT_WRITE_CONFLICT, False, False)


@pytest.mark.skipif(os.name != "nt", reason="Windows msvcrt lock semantics")
def test_lock_release_failure_is_typed_sanitized_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    import msvcrt

    first, manifest = prepared_staging(tmp_path, "first")
    target = publish_snapshot(first, tmp_path / "output", manifest).target_path
    retry, retry_manifest = prepared_staging(tmp_path, "retry")

    def fail_release(_fd: int, mode: int, _length: int) -> None:
        if mode == msvcrt.LK_UNLCK:
            raise OSError(errno.EIO, "sensitive-user-path")

    monkeypatch.setattr(msvcrt, "locking", fail_release)

    # When
    with pytest.raises(EvaluationSnapshotError) as captured:
        publish_snapshot(retry, tmp_path / "output", retry_manifest)

    # Then
    assert (
        captured.value.code,
        retry.exists(),
        "sensitive" in str(captured.value),
        target.exists(),
    ) == (SnapshotErrorCode.SNAPSHOT_WRITE_CONFLICT, False, False, True)
