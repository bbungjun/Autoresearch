from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import NoReturn

import pytest

import autoresearch.research_harness.runner as runner_module
import autoresearch.research_harness._runner_worker as worker_module
from autoresearch.research_harness import (
    CandidateProcessContext,
    LocalRunRequest,
    LocalRunner,
    RunnerError,
    RunnerErrorCode,
)


_INHERITED_ENVIRONMENT = (
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
)


def _candidate_environment() -> tuple[tuple[str, str], ...]:
    environment = {
        name: os.environ[name]
        for name in _INHERITED_ENVIRONMENT
        if name in os.environ and os.environ[name]
    }
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return tuple(sorted(environment.items()))


def _candidate(
    tmp_path: Path,
    body: str,
    *,
    environment: tuple[tuple[str, str], ...] | None = None,
) -> CandidateProcessContext:
    root = tmp_path / "candidate"
    package = root / "autoresearch"
    harness_in = root / "harness_in"
    harness_out = root / "harness_out"
    package.mkdir(parents=True)
    harness_in.mkdir()
    harness_out.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        """from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("command")
parser.add_argument("--slate", required=True)
parser.add_argument("--out", required=True)
parser.add_argument("--seed", required=True, type=int)
args = parser.parse_args()
if args.command != "harness-predict":
    raise SystemExit(2)
"""
        + body,
        encoding="utf-8",
    )
    slate = harness_in / "slate.parquet"
    slate.write_bytes(b"fixture")
    return CandidateProcessContext(
        cwd=root,
        slate=slate,
        predictions=harness_out / "predictions.csv",
        environment=_candidate_environment() if environment is None else environment,
    )


def _run(
    process: CandidateProcessContext,
    *,
    seed: int = 17,
    timeout_seconds: float = 5.0,
) -> runner_module.LocalRunReceipt:
    return LocalRunner().run(LocalRunRequest(process, seed, timeout_seconds))


def _process_is_alive(pid: int) -> bool:
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    raise AssertionError(f"unsupported test platform: {os.name}")


def _wait_for_pid(path: Path) -> int:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            return int(path.read_text(encoding="ascii"))
        except (FileNotFoundError, ValueError):
            time.sleep(0.02)
    raise AssertionError("grandchild pid was not published")


def _assert_process_gone(pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _process_is_alive(pid):
        time.sleep(0.05)
    assert not _process_is_alive(pid)


def _require_symlink(tmp_path: Path) -> None:
    target = tmp_path / "symlink-target"
    link = tmp_path / "symlink-probe"
    target.write_text("target", encoding="utf-8")
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    link.unlink()


def test_runner_passes_fixed_command_seed_and_exact_environment(tmp_path: Path) -> None:
    secret_name = "RUNNER_TEST_HOST_SECRET"
    os.environ[secret_name] = "must-not-be-inherited"
    try:
        process = _candidate(
            tmp_path,
            """
payload = {
    "command": args.command,
    "slate": args.slate,
    "out": args.out,
    "seed": args.seed,
    "environment": {name: os.environ[name] for name in sorted(os.environ)},
    "stdin": sys.stdin.read(),
}
Path(args.out).write_text(json.dumps(payload), encoding="utf-8")
os.write(1, b"x" * 70000 + b"stdout-end")
os.write(2, b"y" * 70000 + b"stderr-end")
""",
        )
        receipt = _run(process, seed=1937)
    finally:
        os.environ.pop(secret_name, None)

    payload = json.loads(process.predictions.read_text(encoding="utf-8"))
    assert payload["command"] == "harness-predict"
    assert payload["slate"] == str(process.slate)
    assert payload["out"] == str(process.predictions)
    assert payload["seed"] == 1937
    expected_environment = dict(process.environment)
    runtime = process.cwd / "harness_out" / ".runtime"
    expected_environment.update({
        "HOME": str(runtime / "home"), "USERPROFILE": str(runtime / "home"),
        "TEMP": str(runtime / "tmp"), "TMP": str(runtime / "tmp"),
        "TMPDIR": str(runtime / "tmp"),
        "USERNAME": "harness",
    })
    assert {
        name: payload["environment"][name] for name in expected_environment
    } == expected_environment
    assert set(payload["environment"]) - set(expected_environment) <= {"LC_CTYPE"}
    assert secret_name not in payload["environment"]
    assert payload["stdin"] == ""
    assert receipt.predictions == process.predictions
    assert receipt.exit_code == 0
    assert receipt.duration_ms >= 0
    assert receipt.stdout_tail.endswith("stdout-end")
    assert receipt.stderr_tail.endswith("stderr-end")
    assert len(receipt.stdout_tail.encode("utf-8")) <= 65536
    assert len(receipt.stderr_tail.encode("utf-8")) <= 65536
    assert str(process.predictions) not in repr(receipt)
    assert "stdout-end" not in repr(receipt)


@pytest.mark.parametrize(
    ("seed", "timeout"),
    [(True, 1.0), (-1, 1.0), (2**32, 1.0), (1, 0.0), (1, float("inf"))],
)
def test_runner_rejects_invalid_scalar_request(
    tmp_path: Path,
    seed: int,
    timeout: float,
) -> None:
    process = _candidate(tmp_path, "Path(args.out).write_text('ok')\n")

    with pytest.raises(RunnerError) as captured:
        _run(process, seed=seed, timeout_seconds=timeout)

    assert captured.value.code is RunnerErrorCode.INVALID_REQUEST
    assert captured.value.exit_code is None
    assert captured.value.duration_ms == 0
    assert captured.value.stdout_tail == ""
    assert captured.value.stderr_tail == ""


@pytest.mark.parametrize(
    "environment",
    [
        (("PYTHONPATH", "candidate"),),
        (("PATH", "one"), ("PATH", "two")),
        (("PATH\0BAD", "value"),),
        (("PATH", "value\0bad"),),
    ],
)
def test_runner_rejects_forged_environment(
    tmp_path: Path,
    environment: tuple[tuple[str, str], ...],
) -> None:
    process = _candidate(tmp_path, "Path(args.out).write_text('ok')\n", environment=environment)

    with pytest.raises(RunnerError) as captured:
        _run(process)

    assert captured.value.code is RunnerErrorCode.INVALID_REQUEST


@pytest.mark.parametrize("stale_kind", ["file", "directory"])
def test_runner_rejects_stale_output(tmp_path: Path, stale_kind: str) -> None:
    process = _candidate(tmp_path, "raise AssertionError('must not execute')\n")
    if stale_kind == "file":
        process.predictions.write_text("stale", encoding="utf-8")
    else:
        process.predictions.mkdir()

    with pytest.raises(RunnerError) as captured:
        _run(process)

    assert captured.value.code is RunnerErrorCode.INVALID_REQUEST


def test_runner_rejects_stale_broken_symlink(tmp_path: Path) -> None:
    _require_symlink(tmp_path)
    process = _candidate(tmp_path, "raise AssertionError('must not execute')\n")
    process.predictions.symlink_to(process.cwd / "missing")

    with pytest.raises(RunnerError) as captured:
        _run(process)

    assert captured.value.code is RunnerErrorCode.INVALID_REQUEST


def test_runner_classifies_nonzero_exit_and_hides_tails_from_repr(tmp_path: Path) -> None:
    process = _candidate(
        tmp_path,
        "os.write(2, b'credential-shaped-secret')\nraise SystemExit(7)\n",
    )

    with pytest.raises(RunnerError) as captured:
        _run(process)

    error = captured.value
    assert error.code is RunnerErrorCode.PREDICT_CRASH
    assert error.exit_code == 7
    assert error.stderr_tail.endswith("credential-shaped-secret")
    assert "credential-shaped-secret" not in repr(error)
    assert str(process.cwd) not in repr(error)


def test_candidate_exit_127_is_a_crash_not_a_start_failure(tmp_path: Path) -> None:
    process = _candidate(tmp_path, "raise SystemExit(127)\n")

    with pytest.raises(RunnerError) as captured:
        _run(process)

    assert captured.value.code is RunnerErrorCode.PREDICT_CRASH
    assert captured.value.exit_code == 127


def test_worker_reports_candidate_popen_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = tmp_path / "start.status"
    (tmp_path / "harness_out").mkdir()
    monkeypatch.chdir(tmp_path)

    class GateInput:
        buffer = io.BytesIO(b"\0")

    def fail_candidate_start(*args: object, **kwargs: object) -> NoReturn:
        raise OSError("candidate executable unavailable")

    monkeypatch.setattr(worker_module.sys, "stdin", GateInput())
    monkeypatch.setattr(worker_module.sys, "argv", ["worker", str(status), "candidate"])
    monkeypatch.setattr(worker_module.subprocess, "Popen", fail_candidate_start)

    assert worker_module.main() == 127
    assert status.read_bytes() == b"F"


def test_worker_filters_interpreter_environment_before_candidate_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = tmp_path / "start.status"
    (tmp_path / "harness_out").mkdir()
    monkeypatch.chdir(tmp_path)
    captured_environment: dict[str, str] = {}

    class GateInput:
        buffer = io.BytesIO(b"\0")

    class SuccessfulProcess:
        def wait(self) -> int:
            return 0

        def kill(self) -> None:
            raise AssertionError("successful candidate must not be killed")

    def capture_candidate_start(
        *args: object,
        **kwargs: object,
    ) -> SuccessfulProcess:
        del args
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        captured_environment.update(environment)
        return SuccessfulProcess()

    monkeypatch.setenv("LC_CTYPE", "interpreter-added-locale")
    monkeypatch.setenv("RUNNER_TEST_HOST_SECRET", "must-not-be-inherited")
    expected_environment = {
        name: value
        for name, value in os.environ.items()
        if name in worker_module._ALLOWED_ENVIRONMENT
    }
    runtime = tmp_path / "harness_out" / ".runtime"
    expected_environment.update({
        "HOME": str(runtime / "home"), "USERPROFILE": str(runtime / "home"),
        "TEMP": str(runtime / "tmp"), "TMP": str(runtime / "tmp"),
        "TMPDIR": str(runtime / "tmp"),
        "USERNAME": "harness",
    })
    monkeypatch.setattr(worker_module.sys, "stdin", GateInput())
    monkeypatch.setattr(worker_module.sys, "argv", ["worker", str(status), "candidate"])
    monkeypatch.setattr(worker_module.subprocess, "Popen", capture_candidate_start)

    assert worker_module.main() == 0
    assert status.read_bytes() == b"S"
    assert captured_environment == expected_environment


def test_prediction_child_uses_disposable_home_and_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "CODEX_HOME",
                 "USERNAME", "USER", "LOGNAME", "LNAME",
                 "OPENAI_API_KEY", "GITHUB_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.setenv(name, "host-private-must-not-inherit")
    process = _candidate(tmp_path, """
import tempfile
import getpass
payload = {"home": str(Path.home()), "tmp": tempfile.gettempdir(), "user": getpass.getuser(), "environment": dict(os.environ)}
Path(args.out).write_text(json.dumps(payload), encoding="utf-8")
""")
    _run(process)
    payload = json.loads(process.predictions.read_text(encoding="utf-8"))
    runtime = process.cwd / "harness_out" / ".runtime"
    assert Path(payload["home"]) == runtime / "home"
    assert Path(payload["tmp"]) == runtime / "tmp"
    assert payload["user"] == "harness"
    assert (runtime / "home").is_dir() and (runtime / "tmp").is_dir()
    assert "host-private-must-not-inherit" not in payload["environment"].values()
    assert not {"CODEX_HOME", "OPENAI_API_KEY", "GITHUB_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS"} & payload["environment"].keys()


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "broken_symlink"])
def test_prediction_child_rejects_existing_runtime_without_start(
    tmp_path: Path, kind: str,
) -> None:
    if "symlink" in kind:
        _require_symlink(tmp_path)
    process = _candidate(tmp_path, "Path('must-not-start').write_text('bad')\n")
    runtime = process.cwd / "harness_out" / ".runtime"
    if kind == "directory":
        runtime.mkdir()
    elif kind == "file":
        runtime.write_text("preserve")
    else:
        target = tmp_path / "outside"
        if kind == "symlink":
            target.mkdir()
        runtime.symlink_to(target, target_is_directory=True)
    with pytest.raises(RunnerError) as caught:
        _run(process)
    assert caught.value.code is RunnerErrorCode.START_FAILED
    assert not (process.cwd / "must-not-start").exists()
    if kind == "file":
        assert runtime.read_text() == "preserve"


def test_worker_does_not_create_runtime_before_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "harness_out").mkdir()
    monkeypatch.chdir(tmp_path)
    status = tmp_path / "start.status"

    class NoRelease:
        buffer = io.BytesIO(b"")

    monkeypatch.setattr(worker_module.sys, "stdin", NoRelease())
    monkeypatch.setattr(worker_module.sys, "argv", ["worker", str(status), "candidate"])
    assert worker_module.main() == 127
    assert not (tmp_path / "harness_out" / ".runtime").exists()
    assert status.read_bytes() == b"F"


def test_runner_classifies_missing_and_nonregular_predictions(tmp_path: Path) -> None:
    missing = _candidate(tmp_path / "missing", "pass\n")
    with pytest.raises(RunnerError) as captured_missing:
        _run(missing)
    assert captured_missing.value.code is RunnerErrorCode.INVALID_PREDICTIONS

    directory = _candidate(tmp_path / "directory", "Path(args.out).mkdir()\n")
    with pytest.raises(RunnerError) as captured_directory:
        _run(directory)
    assert captured_directory.value.code is RunnerErrorCode.INVALID_PREDICTIONS


def test_runner_rejects_prediction_symlink_created_by_candidate(tmp_path: Path) -> None:
    _require_symlink(tmp_path)
    process = _candidate(
        tmp_path,
        "Path('target.csv').write_text('target')\nPath(args.out).symlink_to(Path('target.csv').resolve())\n",
    )

    with pytest.raises(RunnerError) as captured:
        _run(process)

    assert captured.value.code is RunnerErrorCode.INVALID_PREDICTIONS


def test_runner_classifies_launcher_start_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _candidate(tmp_path, "Path(args.out).write_text('ok')\n")

    def fail_start(*args: object, **kwargs: object) -> NoReturn:
        raise OSError("sensitive local executable path")

    monkeypatch.setattr(runner_module.subprocess, "Popen", fail_start)

    with pytest.raises(RunnerError) as captured:
        _run(process)

    assert captured.value.code is RunnerErrorCode.START_FAILED
    assert "sensitive" not in str(captured.value)


def test_runner_does_not_release_candidate_when_tree_attach_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _candidate(
        tmp_path,
        "Path('candidate-started').write_text('bad', encoding='utf-8')\n",
    )

    class AttachFailureOwner:
        def attach(self, launcher: subprocess.Popen[bytes]) -> NoReturn:
            raise OSError("job attach failed")

        def is_alive(self, launcher: subprocess.Popen[bytes]) -> bool:
            return launcher.poll() is None

        def terminate(
            self,
            launcher: subprocess.Popen[bytes],
        ) -> runner_module._TerminationResult:
            launcher.kill()
            launcher.wait(timeout=5)
            return runner_module._TerminationResult(True)

        def close(self) -> bool:
            return True

    monkeypatch.setattr(runner_module, "_new_tree_owner", AttachFailureOwner)

    with pytest.raises(RunnerError) as captured:
        _run(process)

    assert captured.value.code is RunnerErrorCode.START_FAILED
    assert not (process.cwd / "candidate-started").exists()


def test_startup_cancellation_reclaims_gated_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _candidate(
        tmp_path,
        "Path('candidate-started').write_text('bad', encoding='utf-8')\n",
    )
    events: list[str] = []

    class CancelAttachOwner:
        def attach(self, launcher: subprocess.Popen[bytes]) -> NoReturn:
            events.append("attach")
            raise KeyboardInterrupt

        def is_alive(self, launcher: subprocess.Popen[bytes]) -> bool:
            return launcher.poll() is None

        def terminate(
            self,
            launcher: subprocess.Popen[bytes],
        ) -> runner_module._TerminationResult:
            events.append("terminate")
            launcher.kill()
            launcher.wait(timeout=5)
            return runner_module._TerminationResult(True)

        def close(self) -> bool:
            events.append("close")
            return True

    monkeypatch.setattr(runner_module, "_new_tree_owner", CancelAttachOwner)

    with pytest.raises(KeyboardInterrupt):
        _run(process)

    assert events[:3] == ["attach", "terminate", "close"]
    assert not (process.cwd / "candidate-started").exists()


def test_cleanup_exception_is_typed_and_does_not_replace_start_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _candidate(tmp_path, "raise AssertionError('must not execute')\n")

    class CleanupFailureOwner:
        def attach(self, launcher: subprocess.Popen[bytes]) -> NoReturn:
            raise OSError("attach failed")

        def is_alive(self, launcher: subprocess.Popen[bytes]) -> bool:
            return launcher.poll() is None

        def terminate(
            self,
            launcher: subprocess.Popen[bytes],
        ) -> runner_module._TerminationResult:
            launcher.kill()
            launcher.wait(timeout=5)
            raise OSError("cleanup path detail")

        def close(self) -> bool:
            return True

    monkeypatch.setattr(runner_module, "_new_tree_owner", CleanupFailureOwner)

    with pytest.raises(RunnerError) as captured:
        _run(process)

    assert captured.value.code is RunnerErrorCode.CLEANUP_FAILED
    assert "detail" not in str(captured.value)


def test_pipe_reader_cleanup_is_bounded_while_writer_is_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.PIPE,
    )
    assert writer.stdout is not None
    reader = runner_module._PipeReader(writer.stdout)
    reader.start()
    monkeypatch.setattr(runner_module, "_TERMINATION_GRACE_SECONDS", 0.05)

    started_at = time.monotonic()
    try:
        assert reader.finish() is False
        assert time.monotonic() - started_at < 1.0
    finally:
        writer.kill()
        writer.wait(timeout=5)
        reader.thread.join(timeout=5)
        writer.stdout.close()


def test_persistent_process_after_successful_cleanup_is_process_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _candidate(tmp_path, "pass\n")

    class PersistentLeakOwner:
        def attach(self, launcher: subprocess.Popen[bytes]) -> None:
            del launcher

        def is_alive(self, launcher: subprocess.Popen[bytes]) -> bool:
            del launcher
            return False

        def terminate(
            self,
            launcher: subprocess.Popen[bytes],
        ) -> runner_module._TerminationResult:
            del launcher
            return runner_module._TerminationResult(True, True)

        def close(self) -> bool:
            return True

    monkeypatch.setattr(runner_module, "_new_tree_owner", PersistentLeakOwner)

    with pytest.raises(RunnerError) as captured:
        _run(process)

    assert captured.value.code is RunnerErrorCode.PROCESS_LEAKED


def test_runner_rejects_replaced_output_directory_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _candidate(
        tmp_path,
        "Path(args.out).write_text('candidate-output', encoding='utf-8')\n",
    )
    original_wait = runner_module._wait_for_exit

    def swap_output_after_exit(launcher: subprocess.Popen[bytes], timeout_seconds: float) -> int:
        # Windows는 child home/temp 아래를 실행 중 rename하는 것부터 거부할 수 있다.
        # child 종료 후, Runner의 identity 판정 직전에 실제 디렉터리를 교체한다.
        exit_code = original_wait(launcher, timeout_seconds)
        output = process.predictions.parent
        output.rename(process.cwd / "original-harness-out")
        output.mkdir()
        process.predictions.write_text("replacement-output", encoding="utf-8")
        return exit_code

    monkeypatch.setattr(runner_module, "_wait_for_exit", swap_output_after_exit)

    with pytest.raises(RunnerError) as captured:
        _run(process)

    assert captured.value.code is RunnerErrorCode.INVALID_PREDICTIONS


def test_runner_rejects_workspace_ancestor_symlink(tmp_path: Path) -> None:
    _require_symlink(tmp_path)
    real = _candidate(tmp_path / "real", "raise AssertionError('must not execute')\n")
    linked_root = tmp_path / "linked-candidate"
    linked_root.symlink_to(real.cwd, target_is_directory=True)
    process = CandidateProcessContext(
        cwd=linked_root,
        slate=linked_root / "harness_in" / "slate.parquet",
        predictions=linked_root / "harness_out" / "predictions.csv",
        environment=real.environment,
    )

    with pytest.raises(RunnerError) as captured:
        _run(process)

    assert captured.value.code is RunnerErrorCode.INVALID_REQUEST


@pytest.mark.skipif(os.name != "posix", reason="FIFO exists only on POSIX")
def test_runner_rejects_stale_fifo(tmp_path: Path) -> None:
    process = _candidate(tmp_path, "raise AssertionError('must not execute')\n")
    os.mkfifo(process.predictions)

    with pytest.raises(RunnerError) as captured:
        _run(process)

    assert captured.value.code is RunnerErrorCode.INVALID_REQUEST


def test_timeout_reclaims_candidate_grandchild(tmp_path: Path) -> None:
    process = _candidate(
        tmp_path,
        """
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path("grandchild.pid").write_text(str(child.pid), encoding="ascii")
time.sleep(60)
""",
    )

    with pytest.raises(RunnerError) as captured:
        _run(process, timeout_seconds=1.5)

    pid = _wait_for_pid(process.cwd / "grandchild.pid")
    assert captured.value.code is RunnerErrorCode.PREDICT_TIMEOUT
    _assert_process_gone(pid)


def test_normal_parent_exit_with_grandchild_is_reclaimed_and_rejected(tmp_path: Path) -> None:
    process = _candidate(
        tmp_path,
        """
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path("grandchild.pid").write_text(str(child.pid), encoding="ascii")
Path(args.out).write_text("candidate-output", encoding="utf-8")
""",
    )

    with pytest.raises(RunnerError) as captured:
        _run(process)

    pid = _wait_for_pid(process.cwd / "grandchild.pid")
    assert captured.value.code is RunnerErrorCode.PROCESS_LEAKED
    _assert_process_gone(pid)


def test_cancellation_reclaims_candidate_grandchild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _candidate(
        tmp_path,
        """
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path("grandchild.pid").write_text(str(child.pid), encoding="ascii")
time.sleep(60)
""",
    )
    original_wait = runner_module._wait_for_exit

    def interrupt_after_spawn(
        launcher: subprocess.Popen[bytes],
        timeout_seconds: float,
    ) -> NoReturn:
        del launcher, timeout_seconds
        _wait_for_pid(process.cwd / "grandchild.pid")
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_module, "_wait_for_exit", interrupt_after_spawn)
    with pytest.raises(KeyboardInterrupt):
        _run(process)
    monkeypatch.setattr(runner_module, "_wait_for_exit", original_wait)

    pid = _wait_for_pid(process.cwd / "grandchild.pid")
    _assert_process_gone(pid)
