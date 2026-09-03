"""합성 prediction으로 실제 ingestion과 별도 parser 자원 관측을 수동 수행한다.

[파이프라인] 실험 전 parser의 300k행·226byte·65MiB·10초·256MiB 가정을 측정한다.
[기능] 기존 두 최대 길이 사례와 최대 JSON 확장 사례·음성 입력을 만들고,
104 MiB parsed 상한의 실제 ingestion 및 같은 worker의 관측 실행을
새 출력에 기록한다. 측정 장치는 표준 라이브러리만 사용하며 모르는 실패 원인은 unknown이다.
[비책임] 실제 평가 dataset 품질·학습·Judge 판정이나 상한 변경은 수행하지 않는다.
"""

from __future__ import annotations

import argparse
import ctypes
from hashlib import sha256
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter


HEADER = b"evaluation_id,slate_id,video_id,score\r\n"
ROWS = 300_000
MEMORY_LIMIT = 256 * 1024 * 1024
OUTPUT_LIMIT = 104 * 1024 * 1024
INPUT_LIMIT = 65 * 1024 * 1024
TIMEOUT = 10.0
WORKER = Path(__file__).resolve().parents[2] / "autoresearch/research_harness/prediction_parser_worker.py"


def _write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=True, sort_keys=True, allow_nan=False, indent=2)
        stream.write("\n")


def _digest(path: Path) -> str:
    value = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def generate_csv(path: Path, *, rows: int, escaped: bool, max_json_expansion: bool = False) -> dict:
    """226-byte CSV를 작성한다. 최대 JSON 사례만 중복 ID인 parser 전용 입력이다.

    Args:
        path: 새 합성 CSV 경로.
        rows: 생성할 행 수; parser 음성 검증을 위해 300k 초과도 허용한다.
        escaped: 기존 backslash-heavy identifier 사례를 선택한다.
        max_json_expansion: 두 ID를 64 backslash로 채우고 긴 score를 사용한다.

    Returns:
        파일 digest·CSV/예상 JSONL byte 수와 합성 입력의 제한된 의미.
    """
    if type(rows) is not int or not 0 < rows <= 16**6:
        raise ValueError("invalid benchmark rows")
    escaped = escaped or max_json_expansion
    prefix = b"\\" if escaped else b"s"
    video = (b"\\" if escaped else b"v") * 64
    evaluation = b"eval_" + b"a" * 64
    score = b"+1.2345678901234567e-100" if max_json_expansion else b"0.5" + b"0" * 21
    digest, total, parsed_length = sha256(), len(HEADER), 0
    with path.open("xb") as stream:
        stream.write(HEADER)
        digest.update(HEADER)
        for index in range(rows):
            slate = prefix * 64 if max_json_expansion else prefix * 58 + f"{index:06x}".encode("ascii")
            row = b",".join((evaluation, slate, video, score)) + b"\r\n"
            assert len(row) == 226
            stream.write(row)
            digest.update(row)
            total += len(row)
            if not parsed_length:
                parsed_length = len(json.dumps([evaluation.decode(), slate.decode(), video.decode(), float(score)],
                                               separators=(",", ":")).encode("ascii")) + 1
    return {"rows": rows, "row_bytes": 226, "size_bytes": total, "sha256": digest.hexdigest(),
            "escaped_identifiers": escaped, "expected_parsed_bytes": parsed_length * rows,
            "unique_ids": not max_json_expansion,
            "data_scope": "synthetic parser input, not evaluation metric evidence"}


def _process_memory() -> dict:
    """자기 process의 OS high-water를 종료 직전 읽는다. 샘플링 peak가 아니다."""
    try:
        if os.name == "nt":
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            *[(name, ctypes.c_size_t) for name in (
                                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                                "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]]
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                raise OSError("memory_counters_unavailable")
            return {"measurement": "windows_process_high_water_at_exit", "peak_working_set_bytes": counters.PeakWorkingSetSize,
                    "peak_commit_bytes": counters.PeakPagefileUsage, "scope": "observer process through worker return"}
        if os.name == "posix":
            import resource
            value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return {"measurement": "ru_maxrss_at_exit", "peak_working_set_bytes": int(value if sys.platform == "darwin" else value * 1024),
                    "peak_commit_bytes": None, "scope": "observer process through worker return"}
    except (OSError, ImportError, AttributeError, ValueError):
        pass
    return {"measurement": "unavailable", "peak_working_set_bytes": None, "peak_commit_bytes": None}


def _observed_worker(arguments: list[str]) -> int:
    # Load only the existing stdlib-only worker, not research_harness/__init__ and its ML imports.
    if len(arguments) != 7:
        return 2
    memory_report = Path(arguments[-1])
    specification = importlib.util.spec_from_file_location("observed_prediction_parser_worker", WORKER)
    if specification is None or specification.loader is None:
        return 2
    module = importlib.util.module_from_spec(specification)
    sys.path.insert(0, str(WORKER.parent))
    specification.loader.exec_module(module)
    started = perf_counter()
    result = module.main(arguments[:-1])
    observation = {"worker_seconds": perf_counter() - started, "returncode": result, "memory": _process_memory(),
                   "worker_sha256": _digest(WORKER)}
    _write_json(memory_report, observation)
    return result


def _parsed_evidence(path: Path) -> dict:
    if not path.exists():
        return {"parsed_bytes": None, "parsed_rows": None, "parsed_sha256": None}
    count = 0
    with path.open("rb") as stream:
        for _ in stream:
            count += 1
    return {"parsed_bytes": path.stat().st_size, "parsed_rows": count, "parsed_sha256": _digest(path)}


def _read_observation(path: Path) -> dict:
    """회수 중 잘린 telemetry는 원본을 남기고 관측 불가로 취급한다."""
    try:
        with path.open("rb") as stream:
            raw = json.loads(stream.read(16 * 1024 + 1))
        if not isinstance(raw, dict):
            return {}
        memory = raw.get("memory")
        if (not isinstance(memory, dict)
                or memory.get("measurement") not in {"windows_process_high_water_at_exit", "ru_maxrss_at_exit", "unavailable"}
                or any(value is not None and (type(value) is not int or value < 0)
                       for value in (memory.get("peak_working_set_bytes"), memory.get("peak_commit_bytes")))):
            return {}
        seconds = raw.get("worker_seconds")
        if seconds is not None and (type(seconds) not in {int, float} or not math.isfinite(seconds) or seconds < 0):
            return {}
        return {"worker_seconds": seconds, "memory": memory}
    except (OSError, ValueError, UnicodeError):
        return {}


def observe_worker(source: Path, output: Path) -> dict:
    """같은 worker·상한을 별도 subprocess에서 관측한다. ingestion 시간과 합치지 않는다."""
    output.mkdir()
    parsed, report = output / "parsed.jsonl", output / "worker-observation.json"
    parsed.touch(exist_ok=False)
    identity = parsed.stat()
    command = [sys.executable, str(Path(__file__).resolve()), "--observe-worker", str(MEMORY_LIMIT), str(OUTPUT_LIMIT),
               str(source), str(parsed), str(identity.st_dev), str(identity.st_ino), str(report)]
    started = perf_counter()
    returncode, cause = None, None
    try:
        completed = subprocess.run(command, check=False, capture_output=True, timeout=TIMEOUT)
        returncode = completed.returncode
        cause = None if returncode == 0 else "unknown"
        (output / "stdout.log").write_bytes(completed.stdout)
        (output / "stderr.log").write_bytes(completed.stderr)
    except subprocess.TimeoutExpired as error:
        cause = "timeout"
        (output / "stdout.log").write_bytes(error.stdout or b"")
        (output / "stderr.log").write_bytes(error.stderr or b"")
    elapsed = perf_counter() - started
    observed = _read_observation(report)
    result = {"scope": "observed_worker_process_start_to_exit", "wall_seconds": elapsed, "returncode": returncode,
              "cause": cause, "timeout_seconds": TIMEOUT, "memory_limit_bytes": MEMORY_LIMIT,
              "parsed_limit_bytes": OUTPUT_LIMIT, "worker_seconds": observed.get("worker_seconds"),
              "memory": observed.get("memory", {"measurement": "unavailable", "peak_working_set_bytes": None, "peak_commit_bytes": None}),
              "worker_sha256": _digest(WORKER), **_parsed_evidence(parsed)}
    _write_json(output / "receipt.json", result)
    return result


def measure_ingestion(source: Path, output: Path) -> dict:
    """변경하지 않은 실제 seal_prediction_copy의 전체 벽시계 시간과 원본 결과를 기록한다."""
    from autoresearch.research_harness.judge_errors import JudgeError
    from autoresearch.research_harness.prediction_ingestion import seal_prediction_copy
    output.mkdir()
    destination = output / "sealed.csv"
    started = perf_counter()
    try:
        receipt = seal_prediction_copy(source, destination)
        status, reason, stage = "success", None, None
        digest = receipt.sha256
    except JudgeError as error:
        status, reason, stage, digest = "failed", str(error.code), error.stage, None
    elapsed = perf_counter() - started
    result = {"scope": "seal_prediction_copy_total", "status": status, "reason_code": reason, "stage": stage,
              "wall_seconds": elapsed, "sealed_sha256": digest,
              **_parsed_evidence(destination.with_name("sealed.csv.parsed.jsonl"))}
    _write_json(output / "receipt.json", result)
    return result


def run_benchmark(out: Path, *, rows: int = ROWS) -> dict:
    if os.path.lexists(out) or not out.is_absolute() or out.parent.resolve() != out.parent.absolute():
        raise ValueError("new absolute benchmark output required")
    out.mkdir()
    cases = []
    for name, escaped, max_json in (("max-alnum", False, False), ("max-backslash", True, False),
                                  ("max-json-expansion", True, True)):
        directory = out / name
        directory.mkdir()
        source = directory / "input.csv"
        generated = generate_csv(source, rows=rows, escaped=escaped, max_json_expansion=max_json)
        _write_json(directory / "input.json", generated)
        cases.append({"name": name, "input": generated, "ingestion": measure_ingestion(source, directory / "ingestion"),
                      "observer": observe_worker(source, directory / "observed")})
    for name in ("too-many-rows", "row-too-long", "over-65mib"):
        directory = out / name
        directory.mkdir()
        source = directory / "input.csv"
        if name == "too-many-rows":
            generated = generate_csv(source, rows=ROWS + 1, escaped=False)
        elif name == "row-too-long":
            generate_csv(source, rows=1, escaped=False)
            with source.open("ab") as stream:
                stream.write(b"x" * 225 + b"\r\n")
            generated = {"size_bytes": source.stat().st_size, "sha256": _digest(source)}
        else:
            with source.open("xb") as stream:
                stream.truncate(INPUT_LIMIT + 1)
            generated = {"size_bytes": source.stat().st_size, "sha256": _digest(source)}
        measured = measure_ingestion(source, directory / "ingestion")
        cases.append({"name": name, "input": generated, "ingestion": measured})
    result = {"version": "parser-benchmark-v1", "cases": cases, "python": sys.version,
              "measurement_script_sha256": _digest(Path(__file__)), "worker_sha256": _digest(WORKER),
              "limits": {"rows": ROWS, "row_bytes": 226, "input_bytes": INPUT_LIMIT, "parsed_bytes": OUTPUT_LIMIT,
                         "timeout_seconds": TIMEOUT, "memory_bytes": MEMORY_LIMIT},
              "scope": "synthetic parser capacity; not model/evaluation quality", "cause_policy": "unknown unless directly observed"}
    _write_json(out / "benchmark.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "--observe-worker":
        return _observed_worker(arguments[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=ROWS)
    args = parser.parse_args(arguments)
    try:
        result = run_benchmark(args.out.absolute(), rows=args.rows)
    except (OSError, ValueError):
        print("parser_benchmark_failed; inspect local evidence", file=sys.stderr)
        return 1
    print(json.dumps({"cases": [{"name": case["name"], "ingestion_status": case["ingestion"]["status"]} for case in result["cases"]]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
