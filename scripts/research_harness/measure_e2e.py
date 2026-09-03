"""실제 Controller E2E 호출의 증거와 명시적 checkpoint 중단을 수동 측정한다.

[파이프라인] 준비된 fixture·calibration 뒤 기존 run_local_research 실행 전후 구간이다.
[기능] 새 측정 출력에 config/script identity, 비변경 파일 관측과 구간 시간을 보존한다.
선택한 첫 validation checkpoint의 신규 durable append 직후에만 중단을 주입한다.
[비책임] Controller/재개/agent/학습/REPORT는 기존 runtime 소유이며, registry 준비·reset,
자동 재실행·모델 다운로드·실제 성공 판정은 수행하지 않는다.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
from time import perf_counter

from autoresearch.research_harness._report_state import ReportError, file_digest, read_file
from autoresearch.research_harness.ledger import (
    CheckpointRecord, LedgerAppendReceipt, LedgerError, LedgerRecord, TrialLedger,
    TrialRecord, _canonical_line, _record_from_payload, _validate_record,
)
from autoresearch.research_harness.local_evaluation_fixture import _resolved_without_link
from autoresearch.research_harness.local_runtime import HarnessRunConfig, load_run_config, run_local_research


_CHECKPOINT = "trial-0001:validation-recorded"
_AGENT_FILES = ("prompt.txt", "schema.json", "response.json", "receipt.json", "stdout.log", "stderr.log")
_ROOT_FILES = (
    "experiment-ledger.jsonl", "run-inputs/manifest.json", "run-inputs/validation/users.parquet",
    "run-inputs/validation/videos.parquet", "run-inputs/final/users.parquet", "run-inputs/final/videos.parquet",
    "controller-result.json", "controller-result-binding.json", "research-record.json", "research-judge.json",
    "research-report.md", "research-report-manifest.json", "research-judge-intent.json", "research-judge-workspace-failure.json",
)
_ATTEMPT_FILES = ("attempt.json", "candidate.json", "candidate.patch", "agent-explanation.json", "pair.json", "failure.json")
_TRAINING_FILES = ("execution.json", "predictions.csv", "predictions.model.txt", "predictions.training.json",
                   "sealed.csv", "sealed.csv.parsed.jsonl")


class MeasurementError(Exception):
    """원본 입력·예외 원문을 노출하지 않는 측정 요청 오류."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _CheckpointInterrupted(KeyboardInterrupt):
    """원래 append가 신규 durable checkpoint를 반환한 뒤에만 발생한다."""


def _write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=True, sort_keys=True, allow_nan=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _inspect_file(path: Path) -> dict:
    if not os.path.lexists(path):
        return {"status": "absent", "sha256": None, "bytes": None}
    try:
        digest = file_digest(path)
        return {"status": "available", "sha256": digest, "bytes": path.stat().st_size}
    except (ReportError, OSError, ValueError):
        return {"status": "unavailable", "sha256": None, "bytes": None}


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(read_file(path, limit=1024 * 1024))
        return value if isinstance(value, dict) else {}
    except (ReportError, OSError, ValueError, UnicodeError):
        return {}


def _observe_ledger(path: Path, evidence: dict) -> dict:
    result = {"status": evidence["status"], "trials": [], "checkpoints": [], "trailing_bytes": None}
    if evidence["status"] != "available":
        return result
    try:
        payload = read_file(path)
        if sha256(payload).hexdigest() != evidence["sha256"]:
            raise ValueError
        boundary = len(payload) if payload.endswith(b"\n") else payload.rfind(b"\n") + 1
        result["trailing_bytes"] = len(payload) - boundary
        result["status"] = "partial" if boundary < len(payload) else "available"
        seen: set[tuple[str, str]] = set()
        for sequence, line in enumerate(payload[:boundary].splitlines(keepends=True)):
            envelope = json.loads(line)
            if (not isinstance(envelope, dict) or envelope.get("sequence") != sequence
                    or envelope.get("contract_version") != "trial-ledger-v1"):
                raise ValueError
            # These helpers only decode/validate bytes; never open, repair, lock, or append a ledger.
            record = _record_from_payload(envelope.get("record_type"), envelope.get("payload"))
            _validate_record(record)
            if _canonical_line(sequence, record) != line:
                raise ValueError
            key = (envelope["record_type"], record.trial_id if isinstance(record, TrialRecord) else record.checkpoint_id)
            if key in seen:
                raise ValueError
            seen.add(key)
            if isinstance(record, TrialRecord):
                result["trials"].append({"trial_id": record.trial_id, "split": record.split,
                    "base_sha": record.base_sha, "candidate_sha": record.candidate_sha, "decision": record.decision,
                    "reason_code": record.reason_code, "seed": record.seed, "duration_ms": record.duration_ms,
                    "metrics": {metric.name: metric.value for metric in record.metrics}})
            else:
                result["checkpoints"].append({"checkpoint_id": record.checkpoint_id, "stage": record.stage,
                                               "trial_id": record.trial_id, "sequence": sequence})
    except (ReportError, LedgerError, OSError, ValueError, TypeError, KeyError, UnicodeError):
        result["status"] = "unavailable"
    return result


def observe_run(config: HarnessRunConfig) -> dict:
    """정해진 receipt 집합을 읽기만 한다. 누락/파손을 복구하거나 원문을 복제하지 않는다."""
    root = config.run_root
    files = {name: _inspect_file(root / name) for name in _ROOT_FILES}
    files.update({"research-judge-attempt/" + name: _inspect_file(root / "research-judge-attempt" / name)
                  for name in _AGENT_FILES})
    marker = config.handoff.snapshot_root.parents[2] / "final-holdout-consumed" / str(config.handoff.final_holdout_id)
    files["final-marker"] = _inspect_file(marker)
    ledger = _observe_ledger(root / "experiment-ledger.jsonl", files["experiment-ledger.jsonl"])
    trials: dict[str, dict[str, int]] = {}
    attempts = []
    inventory_status = "available"
    parent = root / "attempts"
    directories = []
    if os.path.lexists(parent):
        try:
            if not parent.is_dir() or not _resolved_without_link(parent):
                raise ValueError
            directories = sorted(parent.iterdir())
        except (OSError, ValueError):
            inventory_status = "unavailable"
    for directory in directories:
        if (re.fullmatch(r"[0-9a-f]{32}", directory.name) is None
                or not directory.is_dir() or not _resolved_without_link(directory)):
            inventory_status = "unavailable"
            continue
        prefix = "attempts/" + directory.name + "/"
        names = (*_ATTEMPT_FILES, *("agent/" + name for name in _AGENT_FILES),
                 *(role + "/" + name for role in ("baseline", "candidate") for name in _TRAINING_FILES))
        files.update({prefix + name: _inspect_file(directory / name) for name in names})
        metadata = _read_object(directory / "attempt.json")
        stage, trial_id = metadata.get("stage"), metadata.get("trial_id")
        if (not isinstance(stage, str) or stage not in {"prepare", "validation", "final"} or not isinstance(trial_id, str)
                or re.fullmatch(r"trial-[0-9]{4}|final-holdout", trial_id) is None):
            inventory_status = "unavailable"
            continue
        counts = trials.setdefault(trial_id, {name: 0 for name in (
            "prepare_attempts", "validation_attempts", "final_attempts", "candidate_receipts",
            "pair_receipts", "agent_receipts", "training_receipts")})
        counts[stage + "_attempts"] += 1
        for name, field in (("candidate.json", "candidate_receipts"), ("pair.json", "pair_receipts"),
                            ("agent/receipt.json", "agent_receipts"), ("baseline/predictions.training.json", "training_receipts"),
                            ("candidate/predictions.training.json", "training_receipts")):
            counts[field] += files[prefix + name]["status"] == "available"
        seed = metadata.get("seed")
        attempts.append({"id": directory.name, "stage": stage, "trial_id": trial_id,
                         "seed": seed if type(seed) is int else None})
    return {"ledger": ledger, "files": files, "trials": trials, "attempts": attempts,
            "inventory_status": inventory_status,
            "count_scope": "observed attempt metadata and existing receipt files; not verified successful calls",
            "final_marker_path": str(marker)}


def compare_observations(before: dict, after: dict) -> dict:
    """전후 파일 identity와 trial별 관측 개수를 나란히 둔다. 재개 성공을 추정하지 않는다."""
    old = {name: item["sha256"] for name, item in before["files"].items() if item["status"] == "available"}
    new = {name: item["sha256"] for name, item in after["files"].items() if item["status"] == "available"}
    return {"added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()),
            "changed": sorted(name for name in old.keys() & new.keys() if old[name] != new[name]),
            "unchanged": sorted(name for name in old.keys() & new.keys() if old[name] == new[name]),
            "unavailable": sorted({name for observation in (before, after) for name, item in observation["files"].items()
                                    if item["status"] == "unavailable"}),
            "trials_before": before["trials"], "trials_after": after["trials"]}


@contextmanager
def interrupt_after_checkpoint(target: Path) -> Iterator[None]:
    """기존 append 완료 후 대상 신규 checkpoint에만 중단을 주입하고 원래 메서드를 복원한다."""
    original = TrialLedger.append
    def append(ledger: TrialLedger, record: LedgerRecord) -> LedgerAppendReceipt:
        receipt = original(ledger, record)
        if (ledger.path == target and isinstance(record, CheckpointRecord) and record.checkpoint_id == _CHECKPOINT
                and record.stage == "validation_recorded" and receipt.created is True):
            raise _CheckpointInterrupted
        return receipt
    TrialLedger.append = append
    try:
        yield
    finally:
        TrialLedger.append = original


def _validate_output(out: Path, config: HarnessRunConfig) -> None:
    if (not out.is_absolute() or os.path.lexists(out) or not out.parent.is_dir()
            or not _resolved_without_link(out.parent)):
        raise MeasurementError("new_absolute_output_required")
    for path in (config.run_root, config.workspace_parent, config.handoff.snapshot_root.parents[2],
                 config.prediction.embedding.model_dir, config.prediction.embedding.cache_dir):
        if path.resolve() != path.absolute():
            raise MeasurementError("aliased_input_root")
        if out.is_relative_to(path) or path.is_relative_to(out):
            raise MeasurementError("overlapping_output")


def _error_identifier(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value) else None


def measure_run(config_path: Path, out: Path, *, interrupt_after_first_validation: bool = False) -> dict:
    """설정된 runtime을 한 번만 호출하고 새 출력에 전후 증거를 보존한다.

    Args:
        config_path: 기존 HarnessRunConfig JSON. 변경하거나 다른 설정으로 재시도하지 않는다.
        out: run/fixture/workspace/model/cache와 분리된 새 절대 측정 디렉터리.
        interrupt_after_first_validation: 첫 신규 validation checkpoint 직후 명시적 중단 주입.

    Returns:
        completed/failed/interrupted 상태, 원본 digest와 시간 및 전후 차이. completed는
        runtime 반환을 뜻하며 모델 개선이나 전체 증거 무결성을 새로 판정하지 않는다.
    """
    started = perf_counter()
    config_sha = file_digest(config_path)
    config = load_run_config(config_path)
    if file_digest(config_path) != config_sha:
        raise MeasurementError("config_changed")
    _validate_output(out, config)
    out.mkdir()
    script_sha = file_digest(Path(__file__).absolute())
    _write_json(out / "invocation.json", {"config_sha256": config_sha, "script_sha256": script_sha,
                "interruption_requested": interrupt_after_first_validation, "python": sys.version})
    before = observe_run(config)
    _write_json(out / "before.json", before)
    status, injected, runtime_seconds, error_type, result_summary = "failed", False, None, None, None
    error_code, error_stage = None, None
    try:
        ledger = before["ledger"]
        if interrupt_after_first_validation and (
            ledger["status"] not in {"absent", "available"}
            or any(item["checkpoint_id"] == _CHECKPOINT for item in ledger["checkpoints"])
        ):
            raise MeasurementError("checkpoint_unavailable_or_existing")
        manager = interrupt_after_checkpoint(config.run_root / "experiment-ledger.jsonl") if interrupt_after_first_validation else nullcontext()
        with manager:
            runtime_started = perf_counter()
            try:
                result = run_local_research(config)
            finally:
                runtime_seconds = perf_counter() - runtime_started
        result_summary = {"conclusion": result.conclusion.value, "validation_trials": result.validation_trials,
                          "final_reason_code": result.final_reason_code}
        if interrupt_after_first_validation:
            raise MeasurementError("interruption_not_reached")
        status = "completed"
    except _CheckpointInterrupted:
        status, injected, error_type = "interrupted", True, "KeyboardInterrupt"
    except (KeyboardInterrupt, SystemExit) as error:
        status, error_type = "interrupted", type(error).__name__
    except Exception as error:
        error_type = type(error).__name__
        error_code = _error_identifier(getattr(error, "code", None))
        error_stage = _error_identifier(getattr(error, "stage", None))
    after = observe_run(config)
    _write_json(out / "after.json", after)
    measured = {"version": "controller-e2e-measurement-v1", "status": status, "result": result_summary,
                "error_type": error_type, "error_code": error_code, "error_stage": error_stage,
                "interruption_requested": interrupt_after_first_validation,
                "interruption_injected": injected, "runtime_seconds": runtime_seconds,
                "total_seconds": perf_counter() - started,
                "timing_scope": {"runtime": "run_local_research call only", "total": "entry through post-observation before summary publication"},
                "config_sha256": config_sha, "script_sha256": script_sha,
                "before_sha256": file_digest(out / "before.json"), "after_sha256": file_digest(out / "after.json"),
                "changes": compare_observations(before, after), "cost_usd": None, "human_interventions": None}
    _write_json(out / "measurement.json", measured)
    return measured


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--interrupt-after-first-validation", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = measure_run(args.config.absolute(), args.out.absolute(),
                             interrupt_after_first_validation=args.interrupt_after_first_validation)
    except Exception:
        print("e2e_measurement_failed; inspect local evidence", file=sys.stderr)
        return 1
    print(json.dumps({"status": result["status"], "interruption_injected": result["interruption_injected"]}))
    return 0 if result["status"] == "completed" else 130 if result["status"] == "interrupted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
