"""수동 parser benchmark의 생성 바이트·작은 실제 worker·원인 분리 검증."""

import importlib
import json

import pytest


def module():
    return importlib.import_module("scripts.research_harness.benchmark_parser")


def test_max_row_generator_preserves_csv_schema_and_escape_expansion(tmp_path):
    m = module()
    from autoresearch.research_harness.prediction_parser import iter_prediction_copy
    for escaped in (False, True):
        path = tmp_path / f"input-{escaped}.csv"
        receipt = m.generate_csv(path, rows=3, escaped=escaped)
        rows = list(iter_prediction_copy(path))
        assert receipt["row_bytes"] == 226 and len(rows) == 3
        assert receipt["size_bytes"] == 39 + 3 * 226
        parsed_bytes = sum(len(json.dumps([row.evaluation_id, row.slate_id, row.video_id, row.score], separators=(",", ":")).encode()) + 1 for row in rows)
        assert receipt["expected_parsed_bytes"] == parsed_bytes
        if escaped:
            assert receipt["expected_parsed_bytes"] > 3 * 300


def test_observed_worker_uses_real_parser_with_small_fixture(tmp_path):
    m = module()
    source = tmp_path / "source.csv"
    m.generate_csv(source, rows=3, escaped=False)
    result = m.observe_worker(source, tmp_path / "observed")
    assert result["returncode"] == 0 and result["cause"] is None
    assert result["timeout_seconds"] == 10.0 and result["memory_limit_bytes"] == 256 * 1024 * 1024
    assert result["parsed_rows"] == 3
    assert result["memory"]["measurement"] in {"windows_process_high_water_at_exit", "ru_maxrss_at_exit", "unavailable"}


def test_worker_failure_is_unknown_without_causal_evidence(tmp_path):
    m = module()
    source = tmp_path / "invalid.csv"
    source.write_bytes(b"invalid\n")
    result = m.observe_worker(source, tmp_path / "observed")
    assert result["returncode"] != 0 and result["cause"] == "unknown"


def test_small_actual_ingestion_records_separate_total_time(tmp_path):
    m = module()
    source = tmp_path / "source.csv"
    m.generate_csv(source, rows=2, escaped=True)
    result = m.measure_ingestion(source, tmp_path / "ingestion")
    assert result["status"] == "success" and result["parsed_rows"] == 2
    assert result["scope"] == "seal_prediction_copy_total"


@pytest.mark.parametrize("payload", [b'{"memory":', b"[]", b'{"memory": null}', b'{"memory": {"measurement": 5}}'])
def test_timeout_preserves_receipt_when_memory_telemetry_is_incomplete(tmp_path, monkeypatch, payload):
    m = module()
    def run(command, **kwargs):
        from pathlib import Path
        Path(command[-1]).write_bytes(payload)
        raise m.subprocess.TimeoutExpired(command, 10.0, output=b"partial", stderr=b"")
    monkeypatch.setattr(m.subprocess, "run", run)
    result = m.observe_worker(tmp_path / "source.csv", tmp_path / "observed")
    assert result["cause"] == "timeout" and result["returncode"] is None
    assert result["memory"]["measurement"] == "unavailable"
    assert result["memory"]["peak_working_set_bytes"] is None
    assert (tmp_path / "observed/receipt.json").exists()
