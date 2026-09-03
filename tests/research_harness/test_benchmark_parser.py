"""수동 parser benchmark의 생성 바이트·작은 실제 worker·원인 분리 검증."""

import importlib
import json
from pathlib import Path

import pytest


def module():
    return importlib.import_module("scripts.research_harness.benchmark_parser")


def test_observer_and_ingestion_share_the_resource_contract() -> None:
    from autoresearch.research_harness import prediction_ingestion as ingestion
    m = module()
    assert m.OUTPUT_LIMIT == ingestion._MAX_PARSED_BYTES == 104 * 1024 * 1024
    assert m.INPUT_LIMIT == ingestion.MAX_PREDICTION_BYTES
    assert m.MEMORY_LIMIT == ingestion.PARSER_MEMORY_BYTES
    assert m.TIMEOUT == ingestion.PARSER_TIMEOUT_SECONDS
    assert m.ROWS == ingestion.MAX_PREDICTION_ROWS


def test_max_json_expansion_preserves_226_byte_csv_and_360_byte_jsonl(tmp_path: Path) -> None:
    m = module()
    source = tmp_path / "max.csv"
    generated = m.generate_csv(source, rows=3, escaped=True, max_json_expansion=True)
    assert generated["size_bytes"] == 39 + 226 * 3
    assert generated["expected_parsed_bytes"] == 360 * 3
    assert generated["unique_ids"] is False
    assert b",+1.2345678901234567e-100\r\n" in source.read_bytes()
    observed = m.observe_worker(source, tmp_path / "observed")
    ingested = m.measure_ingestion(source, tmp_path / "ingested")
    assert observed["returncode"] == 0 and ingested["status"] == "success"
    assert observed["parsed_bytes"] == ingested["parsed_bytes"] == 360 * 3
    assert observed["parsed_sha256"] == ingested["parsed_sha256"]


def test_benchmark_keeps_existing_cases_and_adds_max_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = module()
    generated_cases = []
    def generate(path: Path, **kwargs: object) -> dict:
        generated_cases.append((path.parent.name, kwargs))
        path.touch()
        return {}
    monkeypatch.setattr(m, "generate_csv", generate)
    monkeypatch.setattr(m, "INPUT_LIMIT", 3)
    monkeypatch.setattr(m, "measure_ingestion", lambda *args: {"status": "observed"})
    monkeypatch.setattr(m, "observe_worker", lambda *args: {})
    result = m.run_benchmark(tmp_path / "benchmark", rows=3)
    assert [case["name"] for case in result["cases"]] == [
        "max-alnum", "max-backslash", "max-json-expansion", "too-many-rows", "row-too-long", "over-65mib",
    ]
    assert generated_cases[2][1]["max_json_expansion"] is True


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
