"""P0-2C candidate prediction 봉인 ingestion 계약 테스트."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from autoresearch.research_harness.judge import JudgeError, JudgeErrorCode
import autoresearch.research_harness.prediction_ingestion as ingestion
from autoresearch.research_harness.prediction_ingestion import (
    SealedPredictionReceipt,
    seal_prediction_copy,
)


_HEADER = b"evaluation_id,slate_id,video_id,score\n"
_ROW = b"eval_" + b"a" * 64 + b",slate,video,0.5\n"


def _valid_prediction(path: Path) -> bytes:
    payload = _HEADER + _ROW
    path.write_bytes(payload)
    return payload


def test_seal_prediction_copy_uses_65_mib_limit_and_exact_copy(tmp_path: Path) -> None:
    source = tmp_path / "candidate" / "predictions.csv"
    source.parent.mkdir()
    payload = _valid_prediction(source)
    destination = tmp_path / "judge" / "predictions.csv"
    destination.parent.mkdir()

    receipt = seal_prediction_copy(source, destination)

    assert ingestion.MAX_PREDICTION_BYTES == 65 * 1024 * 1024
    assert receipt.path == destination
    assert receipt.size_bytes == len(payload)
    assert destination.read_bytes() == payload


def test_seal_prediction_copy_rejects_existing_destination_without_overwrite(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate.csv"
    _valid_prediction(source)
    destination = tmp_path / "judge.csv"
    destination.write_bytes(b"existing")

    with pytest.raises(JudgeError) as error:
        seal_prediction_copy(source, destination)

    assert error.value.code is JudgeErrorCode.INVALID_PREDICTIONS
    assert destination.read_bytes() == b"existing"


def test_seal_prediction_copy_rejects_symlink_source(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    _valid_prediction(source)
    link = tmp_path / "link.csv"
    try:
        link.symlink_to(source)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(JudgeError) as error:
        seal_prediction_copy(link, tmp_path / "copy.csv")

    assert error.value.code is JudgeErrorCode.INVALID_PREDICTIONS


def test_seal_prediction_copy_rejects_payload_over_65_mib(tmp_path: Path) -> None:
    source = tmp_path / "oversized.csv"
    with source.open("wb") as stream:
        stream.truncate(ingestion.MAX_PREDICTION_BYTES + 1)

    with pytest.raises(JudgeError) as error:
        seal_prediction_copy(source, tmp_path / "copy.csv")

    assert error.value.code is JudgeErrorCode.INVALID_PREDICTIONS
    assert not (tmp_path / "copy.csv").exists()


def test_seal_prediction_copy_rejects_source_growth_during_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "candidate.csv"
    _valid_prediction(source)
    real_signature = ingestion._source_signature
    calls = 0

    def changed_signature(fd: int):
        nonlocal calls
        calls += 1
        signature = real_signature(fd)
        if calls == 3:
            return signature._replace(size=signature.size + 1)
        return signature

    monkeypatch.setattr(ingestion, "_source_signature", changed_signature)

    with pytest.raises(JudgeError) as error:
        seal_prediction_copy(source, tmp_path / "copy.csv")

    assert error.value.code is JudgeErrorCode.INVALID_PREDICTIONS
    assert not (tmp_path / "copy.csv").exists()


def test_seal_prediction_copy_maps_parser_timeout_to_invalid_predictions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "candidate.csv"
    _valid_prediction(source)

    def timeout(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise subprocess.TimeoutExpired(cmd="parser", timeout=10)

    monkeypatch.setattr(ingestion.subprocess, "run", timeout)

    with pytest.raises(JudgeError) as error:
        seal_prediction_copy(source, tmp_path / "copy.csv")

    assert error.value.code is JudgeErrorCode.INVALID_PREDICTIONS
    assert error.value.stage == "parser_timeout"
    assert not (tmp_path / "copy.csv").exists()


def test_seal_prediction_copy_maps_parser_memory_failure_to_invalid_predictions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "candidate.csv"
    _valid_prediction(source)

    def failed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del args, kwargs
        return subprocess.CompletedProcess(args=("parser",), returncode=1)

    monkeypatch.setattr(ingestion.subprocess, "run", failed)

    with pytest.raises(JudgeError) as error:
        seal_prediction_copy(source, tmp_path / "copy.csv")

    assert error.value.code is JudgeErrorCode.INVALID_PREDICTIONS
    assert error.value.stage == "parser_subprocess"
    assert not (tmp_path / "copy.csv").exists()


def test_seal_prediction_copy_removes_copy_after_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "candidate.csv"
    _valid_prediction(source)
    destination = tmp_path / "copy.csv"

    def interrupted(
        judge_copy: Path,
        parsed_copy: Path,
        parsed_owner: ingestion._SourceSignature,
    ) -> None:
        del judge_copy, parsed_owner
        parsed_copy.write_bytes(b"partial")
        raise KeyboardInterrupt

    monkeypatch.setattr(ingestion, "_run_isolated_parser", interrupted)

    with pytest.raises(KeyboardInterrupt):
        seal_prediction_copy(source, destination)

    assert not destination.exists()
    assert not destination.with_name(f"{destination.name}.parsed.jsonl").exists()


def test_seal_prediction_copy_runs_the_shared_parser_on_the_judge_copy(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate.csv"
    source.write_bytes(b"wrong,header\n")
    destination = tmp_path / "copy.csv"

    with pytest.raises(JudgeError) as error:
        seal_prediction_copy(source, destination)

    assert error.value.code is JudgeErrorCode.INVALID_PREDICTIONS
    assert error.value.stage == "parser_subprocess"
    assert not destination.exists()


def test_ingestion_errors_do_not_disclose_candidate_or_judge_paths(tmp_path: Path) -> None:
    source = tmp_path / "secret-candidate-name.csv"
    destination = tmp_path / "secret-judge-name.csv"

    with pytest.raises(JudgeError) as error:
        seal_prediction_copy(source, destination)

    rendered = str(error.value)
    assert str(source) not in rendered
    assert str(destination) not in rendered


def test_judge_scoring_does_not_open_candidate_workspace_paths() -> None:
    source = Path(ingestion.__file__).with_name("judge.py").read_text(encoding="utf-8")

    assert "candidate_prediction" not in source
    assert "os.open(" not in source


def test_sealed_receipt_rejects_direct_construction() -> None:
    with pytest.raises(JudgeError) as error:
        SealedPredictionReceipt()

    assert error.value.code is JudgeErrorCode.INVALID_PREDICTIONS
    assert error.value.stage == "sealed_receipt"


def test_seal_prediction_copy_preserves_existing_parsed_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate.csv"
    _valid_prediction(source)
    destination = tmp_path / "copy.csv"
    parsed = destination.with_name(f"{destination.name}.parsed.jsonl")
    parsed.write_bytes(b"other-run")

    with pytest.raises(JudgeError) as error:
        seal_prediction_copy(source, destination)

    assert error.value.code is JudgeErrorCode.INVALID_PREDICTIONS
    assert error.value.stage == "parsed_destination_exists"
    assert parsed.read_bytes() == b"other-run"
    assert not destination.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO contract")
def test_seal_prediction_copy_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    source = tmp_path / "candidate.fifo"
    os.mkfifo(source)

    with pytest.raises(JudgeError) as error:
        seal_prediction_copy(source, tmp_path / "copy.csv")

    assert error.value.code is JudgeErrorCode.INVALID_PREDICTIONS
    assert error.value.stage == "source_contract"


@pytest.mark.skipif(os.name != "posix", reason="POSIX FIFO contract")
def test_seal_prediction_copy_rejects_regular_to_fifo_swap_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "candidate.csv"
    _valid_prediction(source)
    real_open = ingestion.os.open
    swapped = False

    def swap_then_open(path: Path, flags: int, mode: int = 0o777) -> int:
        nonlocal swapped
        if Path(path) == source and not swapped:
            swapped = True
            source.unlink()
            os.mkfifo(source)
        return real_open(path, flags, mode)

    monkeypatch.setattr(ingestion.os, "open", swap_then_open)

    with pytest.raises(JudgeError) as error:
        seal_prediction_copy(source, tmp_path / "copy.csv")

    assert error.value.code is JudgeErrorCode.INVALID_PREDICTIONS
    assert error.value.stage == "source_contract"
