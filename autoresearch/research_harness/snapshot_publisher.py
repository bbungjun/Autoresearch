"""평가 snapshot의 local write-once 게시를 담당한다.

[파이프라인] artifact writer 뒤와 Stage C 소비자 앞에서 완성된 staging snapshot을
content-addressed target으로 게시한다.

[기능] cooperating publisher 잠금 아래 기존 snapshot을 검증·재사용하거나 atomic
rename으로 새 snapshot을 게시한다.
[비책임] artifact 생성과 GCS 게시, 임의 filesystem actor와의 경쟁은 담당하지 않는다.
"""

from typing import BinaryIO
import errno
import os
from pathlib import Path
import shutil
from hashlib import sha256
from pathlib import PurePosixPath, PureWindowsPath
from time import sleep

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from autoresearch.research_harness.evaluation_artifacts import (
    calculate_snapshot_fingerprint,
)
from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.evaluation_snapshot_models import (
    ArtifactReceipt,
    EvaluationSnapshotManifest,
    EvaluationSnapshotReceipt,
)


def publish_snapshot(
    staging_dir: Path,
    output_root: Path,
    manifest: EvaluationSnapshotManifest,
) -> EvaluationSnapshotReceipt:
    """Publish a completed local snapshot without replacing an existing target."""
    target = output_root / manifest.snapshot_fingerprint
    lock_path = output_root / f".{manifest.snapshot_fingerprint}.lock"
    try:
        try:
            output_root.mkdir(parents=True, exist_ok=True)
            lock_path.touch(exist_ok=True)
            with lock_path.open("a+b") as lock_file:
                _acquire_lock(lock_file)
                try:
                    reused = target.exists()
                    if reused:
                        if not _snapshot_is_valid(target, manifest, require_marker=True):
                            raise _conflict()
                    else:
                        if not _snapshot_is_valid(staging_dir, manifest, require_marker=False):
                            raise _conflict()
                        (staging_dir / "_SUCCESS").write_text(
                            f"{manifest.snapshot_fingerprint}\n", encoding="utf-8"
                        )
                        staging_dir.rename(target)
                finally:
                    _release_lock(lock_file)
        except OSError:
            raise _conflict() from None
    finally:
        if staging_dir.exists():
            try:
                shutil.rmtree(staging_dir)
            except OSError:
                raise _conflict() from None
    return EvaluationSnapshotReceipt(
        snapshot_fingerprint=manifest.snapshot_fingerprint,
        target_path=target,
        validation_id=manifest.validation.evaluation_id,
        final_holdout_id=manifest.final_holdout.evaluation_id,
        reused=reused,
    )


def _acquire_lock(lock_file: BinaryIO) -> None:
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        while True:
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if not _windows_lock_is_contended(error):
                    raise
                sleep(0)
            else:
                break
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.flush()


def _windows_lock_is_contended(error: OSError) -> bool:
    if error.winerror is not None:
        return error.winerror in {33, 36}
    return error.errno in {errno.EACCES, errno.EDEADLK}


def _release_lock(lock_file: BinaryIO) -> None:
    lock_file.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _snapshot_is_valid(
    root: Path,
    expected: EvaluationSnapshotManifest,
    *,
    require_marker: bool,
) -> bool:
    try:
        parsed = EvaluationSnapshotManifest.model_validate_json(
            (root / "manifest.json").read_bytes()
        )
        receipts = (
            parsed.validation.artifacts.slate,
            parsed.validation.artifacts.labels,
            parsed.final_holdout.artifacts.slate,
            parsed.final_holdout.artifacts.labels,
        )
        marker_valid = not require_marker or (
            (root / "_SUCCESS").read_text(encoding="utf-8")
            == f"{expected.snapshot_fingerprint}\n"
        )
        identity_valid = (
            parsed.snapshot_fingerprint == expected.snapshot_fingerprint
            and calculate_snapshot_fingerprint(parsed) == expected.snapshot_fingerprint
            and calculate_snapshot_fingerprint(expected) == expected.snapshot_fingerprint
            and (not require_marker or root.name == expected.snapshot_fingerprint)
        )
        artifacts_valid = all(_artifact_is_valid(root, receipt) for receipt in receipts)
        return marker_valid and identity_valid and artifacts_valid
    except (OSError, UnicodeError, ValidationError, pa.ArrowInvalid):
        return False


def _artifact_is_valid(root: Path, receipt: ArtifactReceipt) -> bool:
    path = _artifact_path(root, receipt.relative_path)
    if path is None:
        return False
    return (
        path.is_file()
        and sha256(path.read_bytes()).hexdigest() == receipt.sha256
        and pq.read_metadata(path).num_rows == receipt.rows
    )


def _artifact_path(root: Path, relative_path: str) -> Path | None:
    """Return the artifact path when its manifest path is locally contained."""
    windows = PureWindowsPath(relative_path)
    if windows.drive:
        return None
    relative = PurePosixPath(relative_path)
    if (
        "\\" in relative_path
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != relative_path
        or not relative.parts
    ):
        return None
    root_resolved = _resolved_without_link(root)
    if root_resolved is None:
        return None
    current = root
    resolved = root_resolved
    for part in relative.parts:
        current = current / part
        resolved = _resolved_without_link(current)
        if resolved is None or not resolved.is_relative_to(root_resolved):
            return None
    return current


def _resolved_without_link(path: Path) -> Path | None:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    absolute = path.absolute()
    if os.path.normcase(resolved) != os.path.normcase(absolute):
        return None
    return resolved


def _conflict() -> EvaluationSnapshotError:
    return EvaluationSnapshotError(
        code=SnapshotErrorCode.SNAPSHOT_WRITE_CONFLICT,
        stage="snapshot_publish",
    )
