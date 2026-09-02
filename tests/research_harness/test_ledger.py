"""Task 4 append-only Trial Ledger와 checkpoint 재개 계약 테스트."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
import errno
import json
import multiprocessing
import os
from pathlib import Path

import pytest

import autoresearch.research_harness as research_harness
import autoresearch.research_harness.ledger as ledger_module
from autoresearch.research_harness import (
    CheckpointRecord,
    FinalConsumptionEvidence,
    LedgerAppendReceipt,
    LedgerArtifactEvidence,
    LedgerError,
    LedgerErrorCode,
    LedgerMetric,
    TrialLedgerState,
    TrialRecord,
    open_trial_ledger,
)


_BASE_SHA = "a" * 40
_CANDIDATE_SHA = "b" * 40
_DIFF_FINGERPRINT = "sha256:" + "c" * 64
_EVALUATION_ID = "eval_" + "d" * 64
_COMPLETED_AT = datetime(2026, 9, 2, 4, 5, 6, tzinfo=UTC)


def _artifact(tmp_path: Path) -> LedgerArtifactEvidence:
    return LedgerArtifactEvidence(
        name="predictions",
        uri=str((tmp_path / "predictions.csv").resolve()),
        sha256="e" * 64,
    )


def _marker(tmp_path: Path) -> FinalConsumptionEvidence:
    return FinalConsumptionEvidence(
        marker_path=(tmp_path / "judge" / "final-holdout-consumed" / _EVALUATION_ID).resolve(),
        marker_sha256="f" * 64,
    )


def _trial(
    tmp_path: Path,
    *,
    trial_id: str = "trial-001",
    final: bool = False,
) -> TrialRecord:
    return TrialRecord(
        trial_id=trial_id,
        split="final_holdout" if final else "validation",
        base_sha=_BASE_SHA,
        candidate_sha=_CANDIDATE_SHA,
        diff_fingerprint=_DIFF_FINGERPRINT,
        evaluation_id=_EVALUATION_ID,
        seed=42,
        metrics=(
            LedgerMetric(name="ndcg_at_10", value=0.75),
            LedgerMetric(name="log_loss", value=None),
        ),
        decision="promote",
        reason_code="promotion_threshold_met",
        duration_ms=1234,
        failure_reason_code=None,
        artifacts=(_artifact(tmp_path),),
        champion_lineage=(_BASE_SHA, _CANDIDATE_SHA),
        final_consumption=_marker(tmp_path) if final else None,
    )


def _checkpoint(
    tmp_path: Path,
    *,
    checkpoint_id: str = "trial-001:validation-scored",
) -> CheckpointRecord:
    return CheckpointRecord(
        checkpoint_id=checkpoint_id,
        stage="validation_scored",
        trial_id="trial-001",
        completed_at=_COMPLETED_AT,
        artifacts=(_artifact(tmp_path),),
        final_consumption=None,
    )


def _ledger_path(tmp_path: Path) -> Path:
    run_root = tmp_path / "run"
    run_root.mkdir()
    return run_root / "experiment-ledger.jsonl"


def _append_in_process(
    path: Path,
    trial: TrialRecord,
    output: multiprocessing.Queue[tuple[int, bool]],
) -> None:
    receipt = open_trial_ledger(path).append(trial)
    output.put((receipt.sequence, receipt.created))


def test_public_ledger_interface_is_small() -> None:
    expected = {
        "CheckpointRecord",
        "LedgerAppendReceipt",
        "LedgerArtifactEvidence",
        "LedgerError",
        "LedgerErrorCode",
        "LedgerMetric",
        "TrialLedger",
        "TrialLedgerState",
        "TrialRecord",
        "open_trial_ledger",
    }
    assert expected <= set(research_harness.__all__)
    assert {
        name
        for name in vars(research_harness.TrialLedger)
        if not name.startswith("_")
    } == {"append", "path", "read_state"}


def test_append_trial_round_trips_all_evidence(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    ledger = open_trial_ledger(path)
    trial = _trial(tmp_path, final=True)

    receipt = ledger.append(trial)
    state = ledger.read_state()

    assert receipt == LedgerAppendReceipt(sequence=0, created=True)
    assert state == TrialLedgerState(
        last_sequence=0,
        trials=(trial,),
        checkpoints=(),
        completed_checkpoint_ids=frozenset(),
        registry_evidence=(trial.final_consumption,),
        recovered_trailing_bytes=0,
    )
    payload = path.read_bytes()
    assert payload.endswith(b"\n")
    parsed = json.loads(payload)
    assert parsed["contract_version"] == "trial-ledger-v1"
    assert parsed["sequence"] == 0
    assert parsed["record_type"] == "trial"


def test_same_trial_retry_is_noop_and_conflicting_retry_fails(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    ledger = open_trial_ledger(path)
    trial = _trial(tmp_path)
    first = ledger.append(trial)
    before = path.read_bytes()

    same = ledger.append(trial)
    with pytest.raises(LedgerError) as captured:
        ledger.append(replace(trial, duration_ms=trial.duration_ms + 1))

    assert first == LedgerAppendReceipt(0, True)
    assert same == LedgerAppendReceipt(0, False)
    assert captured.value.code is LedgerErrorCode.IDEMPOTENCY_CONFLICT
    assert path.read_bytes() == before


def test_checkpoint_reopen_skips_completed_stage_without_duplicate_line(
    tmp_path: Path,
) -> None:
    path = _ledger_path(tmp_path)
    checkpoint = _checkpoint(tmp_path)
    ledger = open_trial_ledger(path)
    ledger.append(checkpoint)

    resumed = open_trial_ledger(path)
    state = resumed.read_state()
    retry = resumed.append(checkpoint)

    assert state.completed(checkpoint.checkpoint_id)
    assert state.checkpoint(checkpoint.checkpoint_id) == checkpoint
    assert retry == LedgerAppendReceipt(0, False)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_checkpoint_same_key_with_different_payload_conflicts(tmp_path: Path) -> None:
    ledger = open_trial_ledger(_ledger_path(tmp_path))
    checkpoint = _checkpoint(tmp_path)
    ledger.append(checkpoint)

    with pytest.raises(LedgerError) as captured:
        ledger.append(replace(checkpoint, stage="candidate_executed"))

    assert captured.value.code is LedgerErrorCode.IDEMPOTENCY_CONFLICT


def test_idempotent_receipt_preserves_physical_sequence_when_types_interleave(
    tmp_path: Path,
) -> None:
    ledger = open_trial_ledger(_ledger_path(tmp_path))
    checkpoint = _checkpoint(tmp_path)
    trial = _trial(tmp_path)
    ledger.append(checkpoint)
    ledger.append(trial)

    retry = ledger.append(trial)

    assert retry == LedgerAppendReceipt(sequence=1, created=False)


def test_final_trial_requires_marker_and_validation_trial_rejects_it(
    tmp_path: Path,
) -> None:
    ledger = open_trial_ledger(_ledger_path(tmp_path))

    with pytest.raises(LedgerError) as missing:
        ledger.append(replace(_trial(tmp_path, final=True), final_consumption=None))
    with pytest.raises(LedgerError) as leaked:
        ledger.append(replace(_trial(tmp_path), final_consumption=_marker(tmp_path)))

    assert missing.value.code is LedgerErrorCode.INVALID_REQUEST
    assert leaked.value.code is LedgerErrorCode.INVALID_REQUEST


def test_only_bytes_after_last_newline_are_recovered(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    trial = _trial(tmp_path)
    open_trial_ledger(path).append(trial)
    complete = path.read_bytes()
    path.write_bytes(complete + b'{"contract_version":"trial-ledger-v1"')

    state = open_trial_ledger(path).read_state()

    assert state.trials == (trial,)
    assert state.recovered_trailing_bytes > 0
    assert path.read_bytes() == complete


def test_newline_terminated_invalid_last_record_fails_closed(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    open_trial_ledger(path).append(_trial(tmp_path))
    damaged = path.read_bytes() + b"{broken}\n"
    path.write_bytes(damaged)

    with pytest.raises(LedgerError) as captured:
        open_trial_ledger(path)

    assert captured.value.code is LedgerErrorCode.INTEGRITY_VIOLATION
    assert path.read_bytes() == damaged


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_newline_terminated_noncanonical_record_fails_closed(
    tmp_path: Path,
    line_ending: str,
) -> None:
    path = _ledger_path(tmp_path)
    open_trial_ledger(path).append(_trial(tmp_path))
    parsed = json.loads(path.read_bytes())
    noncanonical = (json.dumps(parsed, sort_keys=False) + line_ending).encode()
    path.write_bytes(noncanonical)

    with pytest.raises(LedgerError) as captured:
        open_trial_ledger(path)

    assert captured.value.code is LedgerErrorCode.INTEGRITY_VIOLATION
    assert path.read_bytes() == noncanonical


def test_integer_metric_is_rejected_instead_of_aliasing_float_payload(
    tmp_path: Path,
) -> None:
    trial = _trial(tmp_path)
    integer_metric = replace(
        trial,
        metrics=(LedgerMetric(name="ndcg_at_10", value=1),),
    )

    with pytest.raises(LedgerError) as captured:
        open_trial_ledger(_ledger_path(tmp_path)).append(integer_metric)

    assert captured.value.code is LedgerErrorCode.INVALID_REQUEST


def test_sequence_gap_and_duplicate_key_fail_closed(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    ledger = open_trial_ledger(path)
    trial = _trial(tmp_path)
    ledger.append(trial)
    first = json.loads(path.read_bytes())
    first["sequence"] = 2
    path.write_bytes(
        (json.dumps(first, separators=(",", ":"), sort_keys=True) + "\n").encode()
    )

    with pytest.raises(LedgerError) as captured:
        open_trial_ledger(path)

    assert captured.value.code is LedgerErrorCode.INTEGRITY_VIOLATION


def test_duplicate_physical_key_fails_closed_even_when_payload_matches(
    tmp_path: Path,
) -> None:
    path = _ledger_path(tmp_path)
    open_trial_ledger(path).append(_trial(tmp_path))
    first = json.loads(path.read_bytes())
    duplicate = {**first, "sequence": 1}
    path.write_bytes(
        path.read_bytes()
        + (json.dumps(duplicate, separators=(",", ":"), sort_keys=True) + "\n").encode()
    )

    with pytest.raises(LedgerError) as captured:
        open_trial_ledger(path)

    assert captured.value.code is LedgerErrorCode.INTEGRITY_VIOLATION


def test_concurrent_same_trial_appends_once(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    trial = _trial(tmp_path)

    def append() -> LedgerAppendReceipt:
        return open_trial_ledger(path).append(trial)

    with ThreadPoolExecutor(max_workers=8) as executor:
        receipts = tuple(executor.map(lambda _: append(), range(8)))

    assert sum(receipt.created for receipt in receipts) == 1
    assert {receipt.sequence for receipt in receipts} == {0}
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_processes_serialize_same_trial_append(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    trial = _trial(tmp_path)
    open_trial_ledger(path)
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [
        context.Process(target=_append_in_process, args=(path, trial, output))
        for _ in range(4)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)

    assert all(process.exitcode == 0 for process in processes)
    receipts = [output.get(timeout=2) for _ in processes]
    assert sum(created for _, created in receipts) == 1
    assert {sequence for sequence, _ in receipts} == {0}
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_fsync_failure_is_ambiguous_but_retry_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _ledger_path(tmp_path)
    ledger = open_trial_ledger(path)
    trial = _trial(tmp_path)
    original_sync = ledger_module._sync_file

    calls = 0

    def fail_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("sensitive detail")
        original_sync(descriptor)

    monkeypatch.setattr(ledger_module, "_sync_file", fail_fsync)
    with pytest.raises(LedgerError) as captured:
        ledger.append(trial)
    monkeypatch.setattr(ledger_module, "_sync_file", original_sync)

    retry = open_trial_ledger(path).append(trial)
    assert captured.value.code is LedgerErrorCode.IO_FAILED
    assert "sensitive" not in str(captured.value)
    assert retry == LedgerAppendReceipt(0, False)


def test_new_ledger_syncs_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _ledger_path(tmp_path)
    synced: list[Path] = []
    monkeypatch.setattr(ledger_module, "sync_directory", synced.append, raising=False)

    open_trial_ledger(path)

    assert synced == [path.parent]


def test_directory_sync_failure_is_retried_before_open_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _ledger_path(tmp_path)
    calls = 0

    def fail_once(_: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("sensitive detail")

    monkeypatch.setattr(ledger_module, "sync_directory", fail_once)
    with pytest.raises(LedgerError) as captured:
        open_trial_ledger(path)

    resumed = open_trial_ledger(path)

    assert captured.value.code is LedgerErrorCode.IO_FAILED
    assert resumed.read_state().last_sequence == -1
    assert calls >= 2


def test_lock_hardlink_is_rejected_without_mutating_external_file(
    tmp_path: Path,
) -> None:
    path = _ledger_path(tmp_path)
    external = tmp_path / "external"
    external.write_bytes(b"")
    lock_path = path.with_name(f".{path.name}.lock")
    os.link(external, lock_path)

    with pytest.raises(LedgerError) as captured:
        open_trial_ledger(path)

    assert captured.value.code is LedgerErrorCode.IO_FAILED
    assert external.read_bytes() == b""


def test_empty_lock_left_by_crash_is_recovered_under_lock(tmp_path: Path) -> None:
    path = _ledger_path(tmp_path)
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.write_bytes(b"")

    ledger = open_trial_ledger(path)

    assert ledger.read_state().last_sequence == -1
    assert lock_path.read_bytes() == b"0"


def test_windows_non_contention_lock_error_is_not_retried() -> None:
    access_denied = OSError(errno.EINVAL, "sensitive")

    assert not ledger_module._windows_lock_is_contended(access_denied)


@pytest.mark.parametrize(
    "invalid_path",
    [Path("relative/experiment-ledger.jsonl"), Path("relative/wrong.jsonl")],
)
def test_invalid_ledger_path_is_rejected(invalid_path: Path) -> None:
    with pytest.raises(LedgerError) as captured:
        open_trial_ledger(invalid_path)

    assert captured.value.code is LedgerErrorCode.INVALID_REQUEST
