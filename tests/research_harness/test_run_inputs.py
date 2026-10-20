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


# Captured from freeze_run_inputs at f06c9ab75461a04df4ccef65bc9ec7bcd90313fa
# using the deterministic workspace fixture. Only private paths and receipt identities
# are replaced by JSON token placeholders; no current serializer generates this oracle.
_LEGACY_MANIFEST_TEMPLATE = (
    b'{"contract":{"baseline_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","baseline_sigmas":{"brier":0.01,'
    b'"grouped_roc_auc":0.01,"log_loss":0.01,"ndcg_at_10":0.01,"ndcg_at_24":0.01,"pr_auc":0.01,"recall_at_10":0.01},'
    b'"budget":{"max_duration_seconds":600.0,"max_trials":2},"champion_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    b'"confirmation_seeds":[1,2,3,4,5],"handoff":{"final_holdout_id":__FINAL_ID__,"manifest_sha256":__MANIFEST_SHA256__,'
    b'"snapshot_fingerprint":__SNAPSHOT_FINGERPRINT__,"snapshot_root":__SNAPSHOT_ROOT__,"validation_id":__VALIDATION_ID__},'
    b'"initial_card":{"card_id":"card-1","change":"Change","falsification_condition":"No gain","hypothesis":"Hypothesis"},'
    b'"judge_state_root":__JUDGE_STATE_ROOT__,"runtime":{"model":"fixed-model","timeout":60},"screening_seed":0},'
    b'"contract_version":"run-inputs-v1","metadata":{"final":{"evaluation_id":__FINAL_ID__,'
    b'"snapshot_fingerprint":__SNAPSHOT_FINGERPRINT__,"users":{"relative_path":"metadata/users.parquet","rows":200,'
    b'"sha256":__V_USERS_SHA256__},"videos":{"relative_path":"metadata/videos.parquet","rows":144,"sha256":__V_VIDEOS_SHA256__}},'
    b'"validation":{"evaluation_id":__VALIDATION_ID__,"snapshot_fingerprint":__SNAPSHOT_FINGERPRINT__,'
    b'"users":{"relative_path":"metadata/users.parquet","rows":200,"sha256":__V_USERS_SHA256__},'
    b'"videos":{"relative_path":"metadata/videos.parquet","rows":144,"sha256":__V_VIDEOS_SHA256__}}}}'
)


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
    assert payload["contract_version"] == "run-inputs-v2"
    assert payload["contract"]["judge_policy_version"] == "v1"


def test_explicit_policy_version_is_part_of_new_input_identity(case) -> None:
    module, root, contract, validation, final = case
    v1 = _freeze(case)
    changed = replace(contract, judge_policy_version="v2")
    v2 = module.freeze_run_inputs(root.parent / "v2-run", contract=changed,
                                 validation_metadata=validation, final_metadata=final)
    assert v1.manifest_sha256 != v2.manifest_sha256
    assert module.load_run_inputs(root.parent / "v2-run", expected_contract=changed) == v2
    with pytest.raises(StageCError):
        module.load_run_inputs(root, expected_contract=changed)
    with pytest.raises(StageCError):
        module.freeze_run_inputs(root, contract=changed,
                                 validation_metadata=validation, final_metadata=final)
    assert module.load_run_inputs(root, expected_contract=contract) == v1


@pytest.mark.parametrize("version", ["unknown", "", True, None, ["v1"]])
def test_unknown_policy_fails_before_persistence(case, version: object) -> None:
    module, root, contract, validation, final = case
    with pytest.raises(StageCError):
        module.freeze_run_inputs(root, contract=replace(contract, judge_policy_version=version),
                                 validation_metadata=validation, final_metadata=final)
    assert not root.exists()


def test_legacy_layout_load_and_refreeze_do_not_upgrade_bytes(case) -> None:
    module, root, contract, validation, final = case
    assert validation.users == final.users and validation.videos == final.videos
    values = {
        "SNAPSHOT_ROOT": str(contract.handoff.snapshot_root),
        "JUDGE_STATE_ROOT": str(contract.judge_state_root),
        "SNAPSHOT_FINGERPRINT": contract.handoff.snapshot_fingerprint,
        "MANIFEST_SHA256": contract.handoff.manifest_sha256,
        "VALIDATION_ID": contract.handoff.validation_id,
        "FINAL_ID": contract.handoff.final_holdout_id,
        "V_USERS_SHA256": validation.users.receipt.sha256,
        "V_VIDEOS_SHA256": validation.videos.receipt.sha256,
    }
    legacy_bytes = _LEGACY_MANIFEST_TEMPLATE
    for key, item in values.items():
        legacy_bytes = legacy_bytes.replace(f"__{key}__".encode(),
                                            json.dumps(item, ensure_ascii=False).encode())
    for split, bundle in (("validation", validation), ("final", final)):
        target = root / "run-inputs" / split
        target.mkdir(parents=True)
        (target / "users.parquet").write_bytes(bundle.users.payload)
        (target / "videos.parquet").write_bytes(bundle.videos.payload)
    manifest = root / "run-inputs/manifest.json"
    manifest.write_bytes(legacy_bytes)
    before = {path.relative_to(root): path.read_bytes()
              for path in (root / "run-inputs").rglob("*") if path.is_file()}
    original = sha256(manifest.read_bytes()).hexdigest()
    loaded = module.load_run_inputs(root, expected_contract=contract)
    assert loaded.manifest_sha256 == original
    assert _freeze(case) == loaded
    assert loaded.validation_metadata == validation and loaded.final_metadata == final
    with pytest.raises(StageCError):
        module.load_run_inputs(root, expected_contract=replace(contract, judge_policy_version="v2"))
    with pytest.raises(StageCError):
        module.freeze_run_inputs(root, contract=replace(contract, judge_policy_version="v2"),
                                 validation_metadata=validation, final_metadata=final)
    assert {path.relative_to(root): path.read_bytes()
            for path in (root / "run-inputs").rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("mutation", ["legacy-with-version", "v2-missing-version", "unknown-format"])
def test_policy_format_is_strict(case, mutation: str) -> None:
    module, root, contract, _, _ = case
    _freeze(case)
    manifest = root / "run-inputs/manifest.json"
    value = json.loads(manifest.read_bytes())
    if mutation == "legacy-with-version":
        value["contract_version"] = "run-inputs-v1"
    elif mutation == "v2-missing-version":
        del value["contract"]["judge_policy_version"]
    else:
        value["contract_version"] = "run-inputs-v3"
    malformed = module.canonical_json_bytes(value)
    manifest.write_bytes(malformed)
    with pytest.raises(StageCError):
        module.load_run_inputs(root, expected_contract=contract)
    with pytest.raises(StageCError):
        _freeze(case)
    assert manifest.read_bytes() == malformed


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
