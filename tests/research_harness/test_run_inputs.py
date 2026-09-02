"""불변 run 입력 게시·복구와 계약/metadata drift 거부를 검증한다."""

from dataclasses import asdict, replace
from hashlib import sha256
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pyarrow as pa
import pyarrow.parquet as pq

import pytest

from autoresearch.research_harness.candidate_data_view import (
    prepare_candidate_metadata, prepare_final_candidate_metadata,
)
from autoresearch.research_harness.controller import ResearchBudget
from autoresearch.research_harness.feedback import ExperimentCard
from autoresearch.research_harness.fixture_errors import StageCError
from autoresearch.research_harness.evaluation_snapshot_models import ArtifactReceipt
from autoresearch.research_harness.fixture_models import PreparedMetadataArtifact
from autoresearch.research_harness.judge_decision import JudgeMetric
from tests.research_harness.test_workspace import candidate_fixture as candidate_fixture


def test_run_input_interfaces_exist() -> None:
    name = "autoresearch.research_harness.run_inputs"
    assert importlib.util.find_spec(name) is not None, "run_inputs module is required"
    module = importlib.import_module(name)
    assert callable(module.freeze_run_inputs) and callable(module.load_run_inputs)


@pytest.fixture(scope="module")
def prepared(candidate_fixture):
    fixture, source = candidate_fixture
    return (fixture, prepare_candidate_metadata(fixture.judge, source=source),
            prepare_final_candidate_metadata(fixture.judge, source=source))


@pytest.fixture()
def case(prepared, tmp_path: Path):
    module = importlib.import_module("autoresearch.research_harness.run_inputs")
    fixture, validation, final = prepared
    contract = module.RunInputContract(
        initial_card=ExperimentCard("card-1", "Hypothesis", "Change", "No gain"),
        budget=ResearchBudget(2, 600.0), baseline_sha="a" * 40, champion_sha="a" * 40,
        handoff=fixture.judge, judge_state_root=fixture.fixture_root,
        baseline_sigmas=tuple((metric.value, 0.01) for metric in JudgeMetric),
        screening_seed=0, confirmation_seeds=(1, 2, 3, 4, 5),
        runtime_json='{"model":"fixed-model","timeout":60}',
    )
    return module, tmp_path / "run", contract, validation, final


def _freeze(case):
    module, root, contract, validation, final = case
    return module.freeze_run_inputs(root, contract=contract,
                                    validation_metadata=validation, final_metadata=final)


def test_freeze_reuse_and_load_preserve_exact_bytes(case) -> None:
    module, root, contract, validation, final = case
    first = _freeze(case)
    second = _freeze(case)
    loaded = module.load_run_inputs(root, expected_contract=contract)
    assert first == second == loaded
    assert loaded.validation_metadata == validation and loaded.final_metadata == final
    manifest = root / "run-inputs/manifest.json"
    assert loaded.manifest_sha256 == sha256(manifest.read_bytes()).hexdigest()
    assert loaded.artifact.sha256 == loaded.manifest_sha256
    assert loaded.artifact.uri == manifest.as_uri()
    assert loaded.artifact.name == "run-inputs"
    assert {p.relative_to(root / "run-inputs").as_posix()
            for p in (root / "run-inputs").rglob("*") if p.is_file()} == {
        "manifest.json", "validation/users.parquet", "validation/videos.parquet",
        "final/users.parquet", "final/videos.parquet",
    }


@pytest.mark.parametrize("field,value", [
    ("baseline_sha", "b" * 40), ("champion_sha", "b" * 40),
    ("screening_seed", 6), ("confirmation_seeds", (2, 3, 4, 5, 6)),
    ("budget", ResearchBudget(3, 600.0)), ("runtime_json", '{"model":"different"}'),
    ("initial_card", ExperimentCard("card-2", "Hypothesis", "Change", "No gain")),
])
def test_changed_contract_cannot_resume(case, field: str, value: object) -> None:
    module, root, contract, _, _ = case
    _freeze(case)
    with pytest.raises(StageCError):
        module.load_run_inputs(root, expected_contract=replace(contract, **{field: value}))


@pytest.mark.parametrize("field,value", [
    ("baseline_sha", "invalid"), ("screening_seed", True),
    ("screening_seed", 2**32), ("confirmation_seeds", (1, 2, 3, 4, 4)),
    ("confirmation_seeds", (0, 1, 2, 3, 4)), ("confirmation_seeds", (1, 2)),
    ("budget", ResearchBudget(0, 600.0)), ("budget", ResearchBudget(2, float("inf"))),
    ("runtime_json", '{ "x": 1 }'), ("runtime_json", '{"x":NaN}'),
    ("runtime_json", '[]'), ("runtime_json", '{"x":1,"x":1}'),
    ("baseline_sigmas", (("ndcg_at_10", float("nan")),)),
])
def test_invalid_contract_fails_before_persistence(case, field: str, value: object) -> None:
    module, root, contract, validation, final = case
    with pytest.raises(StageCError):
        module.freeze_run_inputs(root, contract=replace(contract, **{field: value}),
                                 validation_metadata=validation, final_metadata=final)
    assert not root.exists()


@pytest.mark.parametrize("relative", ["validation/users.parquet", "final/videos.parquet", "manifest.json"])
def test_file_corruption_fails_closed(case, relative: str) -> None:
    module, root, contract, _, _ = case
    _freeze(case)
    (root / "run-inputs" / relative).write_bytes(b"corrupt")
    with pytest.raises(StageCError):
        module.load_run_inputs(root, expected_contract=contract)
    with pytest.raises(StageCError):
        _freeze(case)


@pytest.mark.parametrize("mutation", ["missing", "extra", "extra-directory"])
def test_exact_tree_required(case, mutation: str) -> None:
    module, root, contract, _, _ = case
    _freeze(case)
    if mutation == "missing":
        (root / "run-inputs/final/users.parquet").unlink()
    elif mutation == "extra":
        (root / "run-inputs/extra").write_text("unexpected")
    else:
        (root / "run-inputs/extra").mkdir()
    with pytest.raises(StageCError):
        module.load_run_inputs(root, expected_contract=contract)


def test_final_and_validation_cannot_be_swapped(case) -> None:
    module, root, contract, validation, final = case
    with pytest.raises(StageCError):
        module.freeze_run_inputs(root, contract=contract,
                                 validation_metadata=final, final_metadata=validation)
    assert not root.exists()


def test_manifest_is_canonical_and_has_private_contract(case) -> None:
    _, root, contract, _, _ = case
    _freeze(case)
    payload = json.loads((root / "run-inputs/manifest.json").read_bytes())
    assert payload["contract"]["handoff"]["snapshot_root"] == str(contract.handoff.snapshot_root)
    assert payload["contract"]["judge_state_root"] == str(contract.judge_state_root)


def test_fixture_root_cannot_be_used_as_run_root(case) -> None:
    module, _, contract, validation, final = case
    with pytest.raises(StageCError):
        module.freeze_run_inputs(contract.judge_state_root / "bad-run", contract=contract,
                                 validation_metadata=validation, final_metadata=final)
    assert not (contract.judge_state_root / "bad-run").exists()


def test_new_process_restores_without_source_query(case) -> None:
    _, root, contract, validation, final = case
    original = _freeze(case)
    # Expected contract crosses a real process boundary separately from the stored manifest.
    script = """
import json, sys
from pathlib import Path
from autoresearch.research_harness.controller import ResearchBudget
from autoresearch.research_harness.feedback import ExperimentCard
from autoresearch.research_harness.fixture_models import JudgeSnapshotHandoff
from autoresearch.research_harness.run_inputs import RunInputContract, load_run_inputs
import autoresearch.research_harness.candidate_data_view as source
def forbidden(*args, **kwargs):
    raise AssertionError('source metadata must not be queried')
source.prepare_candidate_metadata = source.prepare_final_candidate_metadata = forbidden
value = json.loads(sys.stdin.read())
value['initial_card'] = ExperimentCard(**value['initial_card'])
value['budget'] = ResearchBudget(**value['budget'])
value['handoff']['snapshot_root'] = Path(value['handoff']['snapshot_root'])
value['handoff'] = JudgeSnapshotHandoff(**value['handoff'])
value['judge_state_root'] = Path(value['judge_state_root'])
value['baseline_sigmas'] = tuple(tuple(pair) for pair in value['baseline_sigmas'])
value['confirmation_seeds'] = tuple(value['confirmation_seeds'])
result = load_run_inputs(Path(sys.argv[1]), expected_contract=RunInputContract(**value))
print(json.dumps([result.manifest_sha256, result.validation_metadata.users.receipt.sha256,
                  result.final_metadata.videos.receipt.sha256]))
"""
    process = subprocess.run([sys.executable, "-c", script, str(root)],
                             input=json.dumps(asdict(contract), default=str), text=True,
                             capture_output=True, check=False, timeout=60)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == [original.manifest_sha256, validation.users.receipt.sha256,
                                         final.videos.receipt.sha256]


def _artifact(table: pa.Table, path: str) -> PreparedMetadataArtifact:
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    payload = sink.getvalue().to_pybytes()
    return PreparedMetadataArtifact(ArtifactReceipt(path, table.num_rows, sha256(payload).hexdigest()), payload)


def test_changed_metadata_bytes_cannot_reuse(case) -> None:
    module, root, contract, validation, final = case
    original = _freeze(case)
    table = pq.ParquetFile(pa.BufferReader(validation.users.payload)).read().slice(0, 1)
    changed = replace(validation, users=_artifact(table, "metadata/users.parquet"))
    with pytest.raises(StageCError):
        module.freeze_run_inputs(root, contract=contract, validation_metadata=changed, final_metadata=final)
    assert module.load_run_inputs(root, expected_contract=contract) == original


@pytest.mark.parametrize("kind", ["rows", "schema", "relative-path", "duplicate", "snapshot"])
def test_invalid_bundle_fails_before_publication(case, kind: str) -> None:
    module, root, contract, validation, final = case
    table = pq.ParquetFile(pa.BufferReader(validation.users.payload)).read()
    if kind == "rows":
        artifact = PreparedMetadataArtifact(replace(validation.users.receipt, rows=0), validation.users.payload)
    elif kind == "schema":
        artifact = _artifact(table.drop(["age"]), "metadata/users.parquet")
    elif kind == "relative-path":
        artifact = _artifact(table, "metadata/other.parquet")
    elif kind == "duplicate":
        artifact = _artifact(pa.concat_tables([table, table.slice(0, 1)]), "metadata/users.parquet")
    else:
        artifact = validation.users
    changed = replace(validation, users=artifact,
                      snapshot_fingerprint="f" * 64 if kind == "snapshot" else validation.snapshot_fingerprint)
    with pytest.raises(StageCError):
        module.freeze_run_inputs(root, contract=contract, validation_metadata=changed, final_metadata=final)
    assert not root.exists()


def test_partial_write_is_not_published_and_can_retry(case, monkeypatch: pytest.MonkeyPatch) -> None:
    module, root, _, _, _ = case
    original = module._write_file

    def fail_manifest(path: Path, payload: bytes) -> None:
        if path.name == "manifest.json":
            raise OSError("private path should not leak")
        original(path, payload)

    monkeypatch.setattr(module, "_write_file", fail_manifest)
    with pytest.raises(StageCError) as error:
        _freeze(case)
    assert "private path" not in str(error.value)
    assert not (root / "run-inputs").exists()
    assert not list(root.glob(".run-inputs-staging-*"))
    monkeypatch.setattr(module, "_write_file", original)
    assert _freeze(case).validation_metadata == case[3]


def test_rename_success_sync_failure_can_be_recovered(case, monkeypatch: pytest.MonkeyPatch) -> None:
    module, root, contract, _, _ = case
    original = module.sync_directory

    def fail_root(path: Path) -> None:
        if path == root:
            raise OSError("sync failure")
        original(path)

    monkeypatch.setattr(module, "sync_directory", fail_root)
    with pytest.raises(StageCError):
        _freeze(case)
    synced: list[Path] = []

    def record_sync(path: Path) -> None:
        synced.append(path)
        original(path)

    monkeypatch.setattr(module, "sync_directory", record_sync)
    assert _freeze(case) == module.load_run_inputs(root, expected_contract=contract)
    assert root in synced


def test_hardlink_artifact_is_rejected(case, tmp_path: Path) -> None:
    module, root, contract, _, _ = case
    _freeze(case)
    artifact = root / "run-inputs/final/users.parquet"
    os.link(artifact, tmp_path / "alias.parquet")
    with pytest.raises(StageCError):
        module.load_run_inputs(root, expected_contract=contract)


def test_missing_load_is_read_only(case) -> None:
    module, root, contract, _, _ = case
    with pytest.raises(StageCError):
        module.load_run_inputs(root, expected_contract=contract)
    assert not root.exists()


def test_private_paths_cannot_drift(case, tmp_path: Path) -> None:
    module, root, contract, _, _ = case
    _freeze(case)
    for changed in (replace(contract, judge_state_root=tmp_path / "other-state"),
                    replace(contract, handoff=replace(contract.handoff, snapshot_root=tmp_path / "other-snapshot"))):
        with pytest.raises(StageCError):
            module.load_run_inputs(root, expected_contract=changed)


def test_sigma_duplicates_and_changed_values_are_rejected(case) -> None:
    module, root, contract, validation, final = case
    invalid = replace(contract, baseline_sigmas=(*contract.baseline_sigmas, contract.baseline_sigmas[0]))
    with pytest.raises(StageCError):
        module.freeze_run_inputs(root, contract=invalid, validation_metadata=validation, final_metadata=final)
    _freeze(case)
    changed = replace(contract, baseline_sigmas=tuple((name, value * 2) for name, value in contract.baseline_sigmas))
    with pytest.raises(StageCError):
        module.load_run_inputs(root, expected_contract=changed)


def test_nested_new_run_root_is_durably_created(case, monkeypatch: pytest.MonkeyPatch) -> None:
    module, root, contract, validation, final = case
    synced: list[Path] = []
    original = module.sync_directory

    def record_sync(path: Path) -> None:
        synced.append(path)
        original(path)

    monkeypatch.setattr(module, "sync_directory", record_sync)
    nested = root / "nested/run"
    module.freeze_run_inputs(nested, contract=contract, validation_metadata=validation, final_metadata=final)
    assert root.parent in synced and root in synced and root / "nested" in synced


@pytest.mark.parametrize("bad_path", [Path("relative"), Path("bad\x00path")])
def test_invalid_root_is_sanitized(case, bad_path: Path) -> None:
    module, _, contract, validation, final = case
    with pytest.raises(StageCError) as error:
        module.freeze_run_inputs(bad_path, contract=contract, validation_metadata=validation, final_metadata=final)
    assert "bad" not in str(error.value)


def test_manifest_noncanonical_json_is_rejected(case) -> None:
    module, root, contract, _, _ = case
    _freeze(case)
    path = root / "run-inputs/manifest.json"
    path.write_text(json.dumps(json.loads(path.read_bytes()), indent=2), encoding="utf-8")
    with pytest.raises(StageCError):
        module.load_run_inputs(root, expected_contract=contract)
