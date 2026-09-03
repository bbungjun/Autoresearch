"""Validation baseline의 5회 독립 single-fit을 수동 측정한다.

[파이프라인] Controller 실험 전에 baseline noise를 관측하는 calibration 구간이다.
[기능] 기존 Workspace/LocalRunner/Domain을 조립하고 원본·checkpoint·raw metric과
ddof=1 표준편차를 새 출력 root에 보존한다. 기존 출력은 재실행하거나 덮어쓰지 않는다.
[비책임] agent, final holdout, registry 초기화, sigma 정책 변경과 모델 다운로드는 하지 않는다.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from autoresearch.research_harness.judge import JudgeScoringResult
    from autoresearch.research_harness.ledger import LedgerArtifactEvidence, TrialLedger


BASELINE_SHA = "8dd67038d98817b3b4a5f33a4d9dd5009c2ce9fd"
SEEDS = (101, 102, 103, 104, 105)
METRICS = ("ndcg_at_10", "recall_at_10", "ndcg_at_24", "grouped_roc_auc", "pr_auc", "log_loss", "brier")


class CalibrationError(Exception):
    """Private 경로/원문을 노출하지 않는 수동 측정 실패."""


@dataclass(frozen=True)
class CalibrationRequest:
    repository: Path
    workspace_parent: Path
    snapshot_root: Path
    fixture_descriptor_sha256: str
    prediction_config: Path
    out: Path
    baseline_sha: str = BASELINE_SHA
    seeds: tuple[int, ...] = SEEDS
    timeout_seconds: float = 300.0


def _write_json(path: Path, value: object) -> None:
    payload = (json.dumps(value, default=str, ensure_ascii=True, sort_keys=True, allow_nan=False, indent=2) + "\n").encode("ascii")
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _evidence(path: Path) -> LedgerArtifactEvidence:
    from autoresearch.research_harness._report_state import file_digest
    from autoresearch.research_harness.ledger import LedgerArtifactEvidence
    return LedgerArtifactEvidence(path.name + ":" + sha256(path.as_uri().encode()).hexdigest()[:16],
                                  path.as_uri(), file_digest(path))


def _checkpoint(ledger: TrialLedger, identifier: str, artifacts: tuple[LedgerArtifactEvidence, ...]) -> None:
    from autoresearch.research_harness.ledger import CheckpointRecord
    ledger.append(CheckpointRecord(identifier, "calibration", identifier, datetime.now(UTC), artifacts, None))


def summarize_metrics(samples: list[dict[str, float | None]]) -> dict:
    """각 지표에 정확히 유효한 5개 raw 값이 있을 때만 평균/표본 편차를 계산한다."""
    result = {}
    for name in METRICS:
        values = [sample.get(name) for sample in samples]
        valid = [value for value in values if type(value) in {int, float} and math.isfinite(value)]
        complete = len(values) == len(valid) == 5
        result[name] = {"raw_values": [value if type(value) in {int, float} and math.isfinite(value) else None for value in values],
                        "valid_count": len(valid), "required_count": 5, "ddof": 1,
                        "mean": statistics.mean(valid) if complete else None,
                        "sample_stddev": statistics.stdev(valid) if complete else None}
    return result


def _prepare_inputs(request: CalibrationRequest) -> dict:
    from autoresearch.research_harness.candidate_data_view import prepare_candidate_metadata
    from autoresearch.research_harness.local_embedding import _model_files
    from autoresearch.research_harness.local_evaluation_fixture import FixtureActionLogSource, _validated_judge_snapshot, _resolved_without_link
    from autoresearch.research_harness.prediction import _load_config

    if (len(request.snapshot_root.parents) < 3 or request.snapshot_root.parent.name != "by-hash"
            or request.snapshot_root.parent.parent.name != "evaluation-snapshots"):
        raise CalibrationError("snapshot_location")
    fixture_root = request.snapshot_root.parents[2]
    for path in (request.repository, request.workspace_parent, request.snapshot_root, request.out):
        if not path.is_absolute() or not _resolved_without_link(path):
            raise CalibrationError("root_identity")
    for left, right in ((request.workspace_parent, request.repository), (request.workspace_parent, fixture_root),
                        (request.out, fixture_root), (request.out, request.workspace_parent)):
        if left.is_relative_to(right) or right.is_relative_to(left):
            raise CalibrationError("root_overlap")
    resolved = subprocess.run(["git", "-C", str(request.repository), "rev-parse", request.baseline_sha + "^{commit}"],
                              check=True, capture_output=True).stdout.decode("ascii").strip()
    if resolved != request.baseline_sha:
        raise CalibrationError("baseline_identity")
    handoff, _ = _validated_judge_snapshot(request.snapshot_root, expected_fingerprint=request.snapshot_root.name)
    source = FixtureActionLogSource(fixture_root, request.fixture_descriptor_sha256)
    metadata = prepare_candidate_metadata(handoff, source=source)
    config = _load_config(request.prediction_config)
    for path in (config.embedding.model_dir, config.embedding.cache_dir):
        if request.out.is_relative_to(path) or path.is_relative_to(request.out):
            raise CalibrationError("model_output_overlap")
    model_files = _model_files(config.embedding.model_dir)
    inputs = request.out / "inputs"
    inputs.mkdir()
    for name, artifact in (("users.parquet", metadata.users), ("videos.parquet", metadata.videos)):
        with (inputs / name).open("xb") as stream:
            stream.write(artifact.payload)
            stream.flush()
            os.fsync(stream.fileno())
    libraries = {}
    for name in ("numpy", "lightgbm", "pandas", "pyarrow", "scikit-learn", "torch", "sentence-transformers"):
        try:
            libraries[name] = version(name)
        except PackageNotFoundError:
            libraries[name] = None
    code_root = Path(__file__).resolve().parents[2] / "autoresearch/research_harness"
    manifest = {"version": "baseline-calibration-inputs-v1", "baseline_sha": request.baseline_sha,
                "seeds": list(request.seeds), "snapshot_fingerprint": str(handoff.snapshot_fingerprint),
                "validation_id": str(handoff.validation_id), "snapshot_manifest_sha256": handoff.manifest_sha256,
                "fixture_descriptor_sha256": request.fixture_descriptor_sha256,
                "prediction_config": config.model_dump(mode="json", exclude_unset=True),
                "resolved_prediction_config": config.model_dump(mode="json"),
                "metadata": {name: asdict(artifact.receipt) for name, artifact in (("users", metadata.users), ("videos", metadata.videos))},
                "model_files": model_files, "libraries": libraries,
                "trusted_harness_files": {path.name: sha256(path.read_bytes()).hexdigest() for path in sorted(code_root.glob("*.py"))},
                "measurement_script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
                "agent_calls": 0, "final_calls": 0, "registry_initialization": False}
    return {"manifest": manifest, "source": source, "handoff": handoff, "metadata": metadata,
            "config": config, "model_files": model_files}


def _single_fit(request: CalibrationRequest, prepared: dict, seed: int, output: Path) -> JudgeScoringResult:
    from autoresearch.research_harness.domain import YouTubeCTRDomain
    from autoresearch.research_harness.local_embedding import _model_files
    from autoresearch.research_harness.local_trial_runner import _copy_outputs, _safe_tail
    from autoresearch.research_harness.runner import LocalRunner, LocalRunRequest, RunnerError
    from autoresearch.research_harness.workspace import CandidateWorkspaceRequest, open_candidate_workspace

    workspace_request = CandidateWorkspaceRequest(request.repository, request.baseline_sha,
                                                  request.workspace_parent / uuid4().hex, prepared["handoff"])
    with open_candidate_workspace(workspace_request, source=prepared["source"], metadata=prepared["metadata"]) as workspace:
        _write_json(workspace.root / "harness_config.json", prepared["config"].model_dump(mode="json", exclude_unset=True))
        try:
            receipt = LocalRunner().run(LocalRunRequest(workspace.process, seed, request.timeout_seconds))
        except (KeyboardInterrupt, SystemExit) as interruption:
            try:
                _copy_outputs(workspace.process.predictions, output, require_all=False)
                _write_json(output / "execution.json", {"seed": seed, "code_sha": request.baseline_sha,
                            "duration_ms": None, "reason_code": "interrupted", "stdout_tail": "", "stderr_tail": ""})
            except Exception:
                interruption.add_note("interrupted_evidence_preservation_failed")
            raise
        except RunnerError as error:
            _copy_outputs(workspace.process.predictions, output, require_all=False)
            _write_json(output / "execution.json", {"seed": seed, "code_sha": request.baseline_sha,
                        "duration_ms": getattr(error, "duration_ms", None), "exit_code": getattr(error, "exit_code", None),
                        "reason_code": str(getattr(error, "code", "interrupted")),
                        "stdout_tail": _safe_tail(getattr(error, "stdout_tail", "")),
                        "stderr_tail": _safe_tail(getattr(error, "stderr_tail", ""))})
            raise
        if receipt.predictions != workspace.process.predictions:
            raise CalibrationError("prediction_path_mismatch")
        _copy_outputs(receipt.predictions, output, require_all=True)
        _write_json(output / "execution.json", {"seed": seed, "code_sha": request.baseline_sha,
                    "duration_ms": receipt.duration_ms, "exit_code": receipt.exit_code,
                    "candidate_view_sha256": workspace.candidate_view_sha256,
                    "stdout_tail": _safe_tail(receipt.stdout_tail), "stderr_tail": _safe_tail(receipt.stderr_tail)})
    if _model_files(prepared["config"].embedding.model_dir) != prepared["model_files"]:
        raise CalibrationError("model_identity_changed")
    domain = YouTubeCTRDomain()
    sealed = domain.validate_candidate(output / "predictions.csv", output / "sealed.csv")
    return domain.evaluate(prepared["handoff"], sealed, final_grant=None)


def run_calibration(request: CalibrationRequest) -> dict:
    """새 출력에서 baseline seed 5개를 각 1회 실행하고 실패 시 중단 사실을 보존한다."""
    if (request.seeds != SEEDS or request.baseline_sha != BASELINE_SHA
            or type(request.timeout_seconds) not in {float, int} or not math.isfinite(request.timeout_seconds)
            or request.timeout_seconds <= 0 or not request.out.is_absolute() or os.path.lexists(request.out)
            or not request.out.parent.is_dir() or request.out.parent.resolve() != request.out.parent.absolute()):
        raise CalibrationError("request_or_existing_output")
    request.out.mkdir()
    from autoresearch.research_harness.controller import _score_values
    from autoresearch.research_harness.ledger import open_trial_ledger

    ledger = open_trial_ledger(request.out / "experiment-ledger.jsonl")
    samples, attempts = [], []
    interruption = None
    try:
        prepared = _prepare_inputs(request)
        _write_json(request.out / "calibration-inputs.json", prepared.get("manifest", prepared))
        input_files = [request.out / "calibration-inputs.json", *sorted((request.out / "inputs").glob("*.parquet"))]
        _checkpoint(ledger, "calibration-inputs", tuple(_evidence(path) for path in input_files))
        for seed in request.seeds:
            output = request.out / f"seed-{seed}"
            output.mkdir()
            started = perf_counter()
            _write_json(output / "intent.json", {"seed": seed, "baseline_sha": request.baseline_sha,
                        "input_sha256": _evidence(request.out / "calibration-inputs.json").sha256})
            _checkpoint(ledger, f"calibration-seed-{seed}-intent", (_evidence(output / "intent.json"),))
            try:
                score = _single_fit(request, prepared, seed, output)
                values = _score_values(score)
                _write_json(output / "score.json", {"seed": seed, "baseline_sha": request.baseline_sha,
                            "metrics": values, "scoring_result": asdict(score)})
                _write_json(output / "measurement.json", {"duration_seconds": perf_counter() - started,
                            "scope": "single_fit_workspace_prediction_cleanup_and_scoring", "cost_usd": None})
                artifacts = tuple(_evidence(path) for path in sorted(output.iterdir()) if path.is_file())
                _checkpoint(ledger, f"calibration-seed-{seed}-complete", artifacts)
                samples.append(values)
                attempts.append({"seed": seed, "status": "complete", "artifacts": [asdict(item) for item in artifacts]})
                print(json.dumps({"seed": seed, "status": "complete"}), flush=True)
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    interruption = error
                elif not isinstance(error, Exception):
                    raise
                _write_json(output / "failure.json", {"seed": seed, "duration_seconds": perf_counter() - started,
                            "error_type": type(error).__name__, "reason_code": str(getattr(error, "code", "measurement_failed")),
                            "stage": getattr(error, "stage", None), "cost_usd": None})
                artifacts = tuple(_evidence(path) for path in sorted(output.iterdir()) if path.is_file())
                _checkpoint(ledger, f"calibration-seed-{seed}-failed", artifacts)
                attempts.append({"seed": seed, "status": "failed", "artifacts": [asdict(item) for item in artifacts]})
                break
    except Exception as error:
        _write_json(request.out / "preparation-failure.json", {"error_type": type(error).__name__, "reason_code": "calibration_preparation_failed"})
        raise CalibrationError("preparation_failed") from None
    metrics = summarize_metrics(samples)
    report = {"version": "baseline-calibration-v1", "baseline_sha": request.baseline_sha, "seeds": list(request.seeds),
              "status": "complete" if len(samples) == 5 and len(attempts) == 5 and all(item["status"] == "complete" for item in attempts) else "incomplete",
              "metrics": metrics, "attempts": attempts, "cost_usd": None,
              "current_sigma_gate_satisfied": all(item["sample_stddev"] is not None and item["sample_stddev"] > 1e-6 for item in metrics.values()),
              "sigma_policy": "raw sample stddev; no epsilon replacement; not passed to Controller", "agent_calls": 0, "final_calls": 0}
    _write_json(request.out / "calibration.json", report)
    _checkpoint(ledger, "calibration-complete" if report["status"] == "complete" else "calibration-incomplete",
                (_evidence(request.out / "calibration.json"),))
    if interruption is not None:
        raise interruption
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repository", "workspace-parent", "snapshot-root", "prediction-config", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--fixture-descriptor-sha256", required=True)
    parser.add_argument("--baseline-sha", default=BASELINE_SHA)
    parser.add_argument("--seeds", type=int, nargs=5, default=SEEDS)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    args = parser.parse_args(argv)
    request = CalibrationRequest(args.repository.absolute(), args.workspace_parent.absolute(), args.snapshot_root.absolute(),
                                 args.fixture_descriptor_sha256, args.prediction_config.absolute(), args.out.absolute(),
                                 args.baseline_sha, tuple(args.seeds), args.timeout_seconds)
    try:
        result = run_calibration(request)
    except (CalibrationError, OSError):
        print("baseline_calibration_failed; inspect local evidence", file=sys.stderr)
        return 1
    print(json.dumps({"status": result["status"], "current_sigma_gate_satisfied": result["current_sigma_gate_satisfied"]}))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
