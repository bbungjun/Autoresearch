"""중첩된 Judge fixture의 생성·재사용·실패 회수와 content identity를 검증한다."""

from datetime import date
from hashlib import sha256
import os
from pathlib import Path

import pytest

from autoresearch.action_log_generation.pipeline import ActionLogGenerationError
import autoresearch.research_harness.local_evaluation_fixture as fixture_module
from autoresearch.research_harness.fixture_models import (
    FixtureDescriptor,
    LocalEvaluationFixtureRequest,
)
from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode
from autoresearch.research_harness.local_evaluation_fixture import (
    build_local_evaluation_fixture,
)


def _test_io_path(path: Path) -> Path:
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        return Path(f"\\\\?\\{path.absolute()}")
    return path


def _artifact_hashes(root: Path) -> dict[str, str]:
    io_root = _test_io_path(root)
    return {
        path.relative_to(io_root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in io_root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("minimum_root_length", (130, 153))
def test_nested_fixture_build_and_reuse_match_short_root(
    tmp_path: Path, minimum_root_length: int,
) -> None:
    short_root = tmp_path / "short"
    short_root.mkdir()
    nested_root = tmp_path / "nested"
    nested_root /= "x" * max(1, minimum_root_length - len(str(nested_root)) - 1)
    nested_root.mkdir(parents=True)
    assert len(str(nested_root)) >= minimum_root_length
    evaluation_date = date(2026, 9, 1)
    short = build_local_evaluation_fixture(
        LocalEvaluationFixtureRequest(short_root, evaluation_date, 1937)
    )
    request = LocalEvaluationFixtureRequest(nested_root, evaluation_date, 1937)
    nested = build_local_evaluation_fixture(request)
    reused = build_local_evaluation_fixture(request)

    assert not short.reused and not nested.reused and reused.reused
    assert reused.judge == nested.judge
    assert reused.fixture_root == nested.fixture_root
    assert nested.descriptor_sha256 == short.descriptor_sha256
    assert nested.action_log_partitions == short.action_log_partitions
    assert nested.judge.snapshot_fingerprint == short.judge.snapshot_fingerprint
    assert nested.judge.manifest_sha256 == short.judge.manifest_sha256
    assert nested.judge.validation_id == short.judge.validation_id
    assert nested.judge.final_holdout_id == short.judge.final_holdout_id
    assert len(str(nested.judge.snapshot_root / "validation" / "slate.parquet")) > 260
    for receipt in (short, nested, reused):
        for path in (receipt.fixture_root, receipt.descriptor_path, receipt.judge.snapshot_root):
            assert not str(path).startswith("\\\\?\\")
    assert _artifact_hashes(nested.fixture_root) == _artifact_hashes(short.fixture_root)
    assert not tuple((nested_root / "fixtures" / "by-hash").glob(".staging-*"))


def test_failed_nested_fixture_cleans_owned_staging_and_preserves_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state_root = tmp_path / "nested"
    state_root /= "x" * max(1, 153 - len(str(state_root)) - 1)
    state_root.mkdir(parents=True)
    build_staged = fixture_module._build_staged_fixture
    generated: list[Path] = []

    def fail_after_generation(
        staging: Path, descriptor: FixtureDescriptor, digest: str,
    ) -> None:
        build_staged(staging, descriptor, digest)
        generated.append(staging)
        raise ActionLogGenerationError("private-fixture-path")

    monkeypatch.setattr(fixture_module, "_build_staged_fixture", fail_after_generation)
    with pytest.raises(StageCError) as caught:
        build_local_evaluation_fixture(
            LocalEvaluationFixtureRequest(state_root, date(2026, 9, 1), 1937)
        )

    assert generated
    assert caught.value.code == StageCErrorCode.FIXTURE_STATE_CONFLICT
    assert caught.value.stage == "fixture_build"
    assert "private-fixture-path" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert all(not _test_io_path(staging).exists() for staging in generated)
    assert not any(path.is_dir() for path in (state_root / "fixtures" / "by-hash").iterdir())
    assert "fixture_staging_cleanup_failed" not in caplog.text
