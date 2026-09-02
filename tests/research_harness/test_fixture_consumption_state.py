"""Fixture 데이터 무결성과 final 소비 상태의 공존 계약을 검증한다."""

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
import shutil

import pytest

from autoresearch.research_harness.candidate_data_view import prepare_candidate_metadata
from autoresearch.research_harness.consumption_registry import (
    FinalConsumptionRequest, claim_final_consumption,
)
from autoresearch.research_harness.fixture_errors import StageCError
from autoresearch.research_harness.fixture_models import LocalEvaluationFixtureRequest
from autoresearch.research_harness.local_evaluation_fixture import (
    FixtureActionLogSource, _io_path, build_local_evaluation_fixture,
)
from tests.research_harness.test_workspace import candidate_fixture as candidate_fixture


@pytest.fixture()
def registry_case(candidate_fixture, tmp_path_factory):
    receipt, _ = candidate_fixture
    parent = tmp_path_factory.mktemp("registry-fixture")
    root = parent / "fixtures" / "by-hash" / receipt.descriptor_sha256
    shutil.copytree(_io_path(receipt.fixture_root), _io_path(root))
    handoff = replace(receipt.judge, snapshot_root=root / receipt.judge.snapshot_root.relative_to(receipt.fixture_root))
    return root, handoff, FixtureActionLogSource(root, receipt.descriptor_sha256)


def _claim(root, handoff):
    return claim_final_consumption(FinalConsumptionRequest(
        root, handoff, "a" * 40, "b" * 40, datetime(2026, 9, 3, tzinfo=UTC),
    ))


def test_fixture_metadata_survives_empty_registry_and_real_claim(registry_case) -> None:
    root, handoff, source = registry_case
    before = prepare_candidate_metadata(handoff, source=source)
    (root / "final-holdout-consumed").mkdir()
    assert prepare_candidate_metadata(handoff, source=source) == before
    grant = _claim(root, handoff)
    assert grant._authorizes(handoff)
    assert prepare_candidate_metadata(handoff, source=source) == before


def test_fixture_reuse_preserves_consumption_marker(registry_case) -> None:
    root, handoff, _ = registry_case
    (root / "final-holdout-consumed").mkdir()
    grant = _claim(root, handoff)
    before = grant.evidence.marker_path.read_bytes()
    reused = build_local_evaluation_fixture(LocalEvaluationFixtureRequest(
        root.parents[2], date(2026, 9, 1), 1937,
    ))
    assert reused.reused and reused.judge == handoff
    assert grant.evidence.marker_path.read_bytes() == before
    assert grant._authorizes(handoff)


@pytest.mark.parametrize("damage", ["corrupt", "missing"])
def test_validation_data_is_independent_of_marker_contents(registry_case, damage: str) -> None:
    root, handoff, source = registry_case
    before = prepare_candidate_metadata(handoff, source=source)
    (root / "final-holdout-consumed").mkdir()
    grant = _claim(root, handoff)
    marker = grant.evidence.marker_path
    if damage == "corrupt":
        marker.write_bytes(b"broken marker")
    else:
        marker.unlink()
    assert not grant._authorizes(handoff)
    assert prepare_candidate_metadata(handoff, source=source) == before


@pytest.mark.parametrize("extra", ["unknown-file", "directory", "other-evaluation", "hardlink"])
def test_fixture_registry_does_not_relax_other_tree_checks(registry_case, extra: str, tmp_path: Path) -> None:
    root, handoff, source = registry_case
    registry = root / "final-holdout-consumed"
    registry.mkdir()
    grant = _claim(root, handoff)
    if extra == "directory":
        (registry / "nested").mkdir()
    elif extra == "hardlink":
        (tmp_path / "marker-alias").hardlink_to(grant.evidence.marker_path)
    else:
        name = "unexpected" if extra == "unknown-file" else "eval_" + "0" * 64
        (registry / name).write_bytes(b"unexpected")
    with pytest.raises(StageCError):
        prepare_candidate_metadata(handoff, source=source)
