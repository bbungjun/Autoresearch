"""Research Harness의 로컬 candidate 실행·회수 경계.

[파이프라인] disposable ``CandidateWorkspace`` 준비 뒤 Sealed Judge ingestion 전에 고정
``harness-predict`` 명령을 별도 process tree에서 한 번 실행한다.

[기능] 고정 argv·최소 환경 검증, wall-clock timeout, bounded stdout/stderr tail, POSIX process
group과 Windows Job Object 기반 child/grandchild 회수, typed receipt/error를 제공한다.

[비책임] candidate 코드 변경, prediction CSV 의미 검증·Judge copy, metric 계산, trial ledger와
반복 Controller는 각각 workspace, sealed ingestion/Judge, Task 5b가 담당한다.
"""

from __future__ import annotations

from collections.abc import Callable
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum, unique
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
from typing import Any, BinaryIO, Protocol

from autoresearch.research_harness.workspace import CandidateProcessContext


_MAX_SEED = 2**32 - 1
_TAIL_BYTES = 64 * 1024
_READ_BYTES = 8192
_TERMINATION_GRACE_SECONDS = 2.0
_INHERITED_ENVIRONMENT = (
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
)
_RELEASE_BYTE = b"\x00"


@unique
class RunnerErrorCode(StrEnum):
    """Controller가 실행 실패의 다음 행동을 정할 때 쓰는 안정된 코드."""

    INVALID_REQUEST = "runner_invalid_request"
    START_FAILED = "runner_start_failed"
    PREDICT_TIMEOUT = "predict_timeout"
    PREDICT_CRASH = "predict_crash"
    INVALID_PREDICTIONS = "invalid_predictions"
    PROCESS_LEAKED = "runner_process_leaked"
    CLEANUP_FAILED = "runner_cleanup_failed"


@dataclass(slots=True, repr=False)
class RunnerError(Exception):
    """Candidate 로그와 로컬 경로를 implicit representation에서 숨기는 실행 오류."""

    code: RunnerErrorCode
    stage: str
    exit_code: int | None = None
    duration_ms: int = 0
    stdout_tail: str = field(default="", repr=False)
    stderr_tail: str = field(default="", repr=False)

    def __str__(self) -> str:
        return f"{self.code.value}: stage={self.stage}"

    def __repr__(self) -> str:
        return f"RunnerError(code={self.code!r}, stage={self.stage!r})"


@dataclass(frozen=True, slots=True)
class LocalRunRequest:
    """하나의 candidate 예측 실행에 필요한 Harness 소유 입력."""

    process: CandidateProcessContext
    seed: int
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class LocalRunReceipt:
    """성공한 candidate 실행의 Controller 전달용 결과."""

    predictions: Path = field(repr=False)
    exit_code: int
    duration_ms: int
    stdout_tail: str = field(repr=False)
    stderr_tail: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    device: int
    inode: int
    mode: int


@dataclass(frozen=True, slots=True)
class _ValidatedRequest:
    process: CandidateProcessContext
    environment: dict[str, str]
    cwd_identity: _PathIdentity
    input_identity: _PathIdentity
    slate_identity: _PathIdentity
    output_identity: _PathIdentity


@dataclass(frozen=True, slots=True)
class _TerminationResult:
    cleanup_ok: bool
    process_leaked: bool = False


class _TreeOwner(Protocol):
    def attach(self, process: subprocess.Popen[bytes]) -> None: ...

    def is_alive(self, process: subprocess.Popen[bytes]) -> bool: ...

    def terminate(self, process: subprocess.Popen[bytes]) -> _TerminationResult: ...

    def close(self) -> bool: ...


class _TailBuffer:
    def __init__(self) -> None:
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._lock = threading.Lock()

    def append(self, payload: bytes) -> None:
        if not payload:
            return
        with self._lock:
            if len(payload) >= _TAIL_BYTES:
                self._chunks.clear()
                self._chunks.append(payload[-_TAIL_BYTES:])
                self._size = _TAIL_BYTES
                return
            self._chunks.append(payload)
            self._size += len(payload)
            overflow = self._size - _TAIL_BYTES
            while overflow > 0:
                first = self._chunks[0]
                if len(first) <= overflow:
                    self._chunks.popleft()
                    self._size -= len(first)
                    overflow = self._size - _TAIL_BYTES
                    continue
                self._chunks[0] = first[overflow:]
                self._size -= overflow
                overflow = 0

    def decode(self) -> str:
        with self._lock:
            return b"".join(self._chunks).decode("utf-8", errors="replace")


@dataclass(slots=True)
class _PipeReader:
    pipe: BinaryIO
    buffer: _TailBuffer = field(default_factory=_TailBuffer)
    failed: bool = False
    started: bool = False
    thread: threading.Thread = field(init=False)

    def __post_init__(self) -> None:
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self.thread.start()
        self.started = True

    def _drain(self) -> None:
        try:
            while payload := self.pipe.read(_READ_BYTES):
                self.buffer.append(payload)
        except OSError:
            self.failed = True

    def finish(self) -> bool:
        if not self.started:
            try:
                self.pipe.close()
            except OSError:
                self.failed = True
            return not self.failed
        self.thread.join(timeout=_TERMINATION_GRACE_SECONDS)
        if self.thread.is_alive():
            return False
        try:
            self.pipe.close()
        except OSError:
            self.failed = True
        return not self.failed and not self.thread.is_alive()


class _PosixTreeOwner:
    def attach(self, process: subprocess.Popen[bytes]) -> None:
        del process

    def is_alive(self, process: subprocess.Popen[bytes]) -> bool:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return False
        return True

    def terminate(self, process: subprocess.Popen[bytes]) -> _TerminationResult:
        try:
            _send_posix_signal(process.pid, signal.SIGTERM)
            _poll_absent(lambda: self.is_alive(process), _TERMINATION_GRACE_SECONDS)
            if self.is_alive(process):
                _send_posix_signal(process.pid, signal.SIGKILL)
                _poll_absent(lambda: self.is_alive(process), _TERMINATION_GRACE_SECONDS)
            _final_wait(process)
        except (OSError, subprocess.SubprocessError):
            return _TerminationResult(False)
        return _TerminationResult(True, self.is_alive(process))

    def close(self) -> bool:
        return True


class _WindowsTreeOwner:
    def __init__(self) -> None:
        self._api, self._handle = _create_windows_job()
        self._closed = False

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        self._api.assign(self._handle, process.pid)

    def is_alive(self, process: subprocess.Popen[bytes]) -> bool:
        del process
        if self._closed:
            return False
        return self._api.active_processes(self._handle) > 0

    def terminate(self, process: subprocess.Popen[bytes]) -> _TerminationResult:
        if self._closed:
            return _TerminationResult(True)
        try:
            if process.poll() is None:
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                except (OSError, ValueError):
                    pass
            _poll_absent(lambda: self.is_alive(process), _TERMINATION_GRACE_SECONDS)
            if self.is_alive(process):
                self._api.terminate(self._handle)
                _poll_absent(
                    lambda: self.is_alive(process),
                    _TERMINATION_GRACE_SECONDS,
                )
            _final_wait(process)
            leaked = self.is_alive(process)
        except (OSError, subprocess.SubprocessError):
            return _TerminationResult(False)
        return _TerminationResult(True, leaked)

    def close(self) -> bool:
        if self._closed:
            return True
        self._closed = True
        return self._api.close(self._handle)


class _WindowsJobApi:
    def __init__(self, kernel32: Any, basic_accounting_type: type[Any]) -> None:
        self._kernel32 = kernel32
        self._basic_accounting_type = basic_accounting_type

    def assign(self, job_handle: object, pid: int) -> None:
        process_handle = self._kernel32.OpenProcess(0x0101, False, pid)
        if not process_handle:
            raise OSError("windows process handle unavailable")
        try:
            if not self._kernel32.AssignProcessToJobObject(job_handle, process_handle):
                raise OSError("windows job assignment failed")
        finally:
            self._kernel32.CloseHandle(process_handle)

    def active_processes(self, job_handle: object) -> int:
        import ctypes

        information = self._basic_accounting_type()
        if not self._kernel32.QueryInformationJobObject(
            job_handle,
            1,
            ctypes.byref(information),
            ctypes.sizeof(information),
            None,
        ):
            raise OSError("windows job query failed")
        return int(information.active_processes)

    def terminate(self, job_handle: object) -> None:
        if not self._kernel32.TerminateJobObject(job_handle, 1):
            raise OSError("windows job termination failed")

    def close(self, job_handle: object) -> bool:
        return bool(self._kernel32.CloseHandle(job_handle))


class LocalRunner:
    """검증된 workspace에서 고정 candidate 명령을 실행하는 stateless runner."""

    def run(self, request: LocalRunRequest) -> LocalRunReceipt:
        """Candidate를 한 번 실행하고 성공 receipt를 반환한다."""

        validated = _validate_request(request)
        return _execute(validated, request.seed, request.timeout_seconds)


def _expected_environment() -> tuple[tuple[str, str], ...]:
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


def _validate_request(request: LocalRunRequest) -> _ValidatedRequest:
    if not isinstance(request, LocalRunRequest):
        raise _invalid_request()
    process = request.process
    if (
        not isinstance(process, CandidateProcessContext)
        or isinstance(request.seed, bool)
        or not isinstance(request.seed, int)
        or not 0 <= request.seed <= _MAX_SEED
        or isinstance(request.timeout_seconds, bool)
        or not isinstance(request.timeout_seconds, (int, float))
        or not math.isfinite(request.timeout_seconds)
        or request.timeout_seconds <= 0
    ):
        raise _invalid_request()
    paths = (process.cwd, process.slate, process.predictions)
    if any(not isinstance(path, Path) or not path.is_absolute() for path in paths):
        raise _invalid_request()
    expected_slate = process.cwd / "harness_in" / "slate.parquet"
    expected_predictions = process.cwd / "harness_out" / "predictions.csv"
    if process.slate != expected_slate or process.predictions != expected_predictions:
        raise _invalid_request()
    if os.path.lexists(process.predictions):
        raise _invalid_request()
    environment = process.environment
    if (
        not isinstance(environment, tuple)
        or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
            or "\0" in item[0]
            or "\0" in item[1]
            for item in environment
        )
        or len({name for name, _ in environment}) != len(environment)
        or environment != _expected_environment()
    ):
        raise _invalid_request()
    try:
        cwd_identity = _require_path(process.cwd, stat.S_ISDIR)
        input_identity = _require_path(process.slate.parent, stat.S_ISDIR)
        slate_identity = _require_path(process.slate, stat.S_ISREG)
        output_identity = _require_path(process.predictions.parent, stat.S_ISDIR)
    except OSError:
        raise _invalid_request() from None
    return _ValidatedRequest(
        process=process,
        environment=dict(environment),
        cwd_identity=cwd_identity,
        input_identity=input_identity,
        slate_identity=slate_identity,
        output_identity=output_identity,
    )


def _invalid_request() -> RunnerError:
    return RunnerError(RunnerErrorCode.INVALID_REQUEST, "request_validation")


def _require_path(
    path: Path,
    expected_type: Callable[[int], bool],
) -> _PathIdentity:
    status = path.lstat()
    is_reparse = bool(getattr(status, "st_file_attributes", 0) & 0x400)
    resolved = path.resolve(strict=True)
    lexical = path.absolute()
    if (
        not expected_type(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or is_reparse
        or os.path.normcase(str(resolved)) != os.path.normcase(str(lexical))
    ):
        raise OSError("unexpected path type")
    return _PathIdentity(status.st_dev, status.st_ino, status.st_mode)


def _same_path_identity(path: Path, expected: _PathIdentity) -> bool:
    try:
        current = path.lstat()
    except OSError:
        return False
    return _PathIdentity(current.st_dev, current.st_ino, current.st_mode) == expected


def _execute(
    validated: _ValidatedRequest,
    seed: int,
    timeout_seconds: float,
) -> LocalRunReceipt:
    process_context = validated.process
    candidate_argv = (
        sys.executable,
        "-m",
        "autoresearch.cli",
        "harness-predict",
        "--slate",
        str(process_context.slate),
        "--out",
        str(process_context.predictions),
        "--seed",
        str(seed),
    )
    started_at = time.monotonic()
    owner: _TreeOwner | None = None
    launcher: subprocess.Popen[bytes] | None = None
    readers: tuple[_PipeReader, _PipeReader] | None = None
    status_directory: TemporaryDirectory[str] | None = None
    status_path: Path | None = None
    try:
        worker = Path(__file__).with_name("_runner_worker.py").resolve(strict=True)
        status_directory = TemporaryDirectory(prefix="autoresearch-runner-")
        status_path = Path(status_directory.name) / "candidate-start.status"
        launcher_argv = (
            sys.executable,
            "-I",
            str(worker),
            str(status_path),
            *candidate_argv,
        )
        owner = _new_tree_owner()
        launcher = subprocess.Popen(
            launcher_argv,
            cwd=process_context.cwd,
            env=validated.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            close_fds=True,
            start_new_session=os.name == "posix",
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
        )
        owner.attach(launcher)
        if launcher.poll() is not None:
            raise OSError("trusted launcher exited before release")
        assert launcher.stdin is not None
        assert launcher.stdout is not None
        assert launcher.stderr is not None
        readers = (_PipeReader(launcher.stdout), _PipeReader(launcher.stderr))
        for reader in readers:
            reader.start()
        launcher.stdin.write(_RELEASE_BYTE)
        launcher.stdin.flush()
        launcher.stdin.close()
    except (OSError, RuntimeError):
        cleanup = _cleanup_started(owner, launcher, readers)
        status_ok = _cleanup_status_directory(status_directory)
        if not cleanup.cleanup_ok or not status_ok:
            code = RunnerErrorCode.CLEANUP_FAILED
            stage = "start_cleanup"
        elif cleanup.process_leaked:
            code = RunnerErrorCode.PROCESS_LEAKED
            stage = "start_process_leak"
        else:
            code = RunnerErrorCode.START_FAILED
            stage = "launcher_start"
        raise RunnerError(
            code,
            stage,
            duration_ms=_duration_ms(started_at),
            **_tails(readers),
        ) from None
    except BaseException as error:
        cleanup = _cleanup_started(owner, launcher, readers)
        status_ok = _cleanup_status_directory(status_directory)
        _annotate_cleanup_failure(error, cleanup, status_ok)
        raise

    try:
        assert status_directory is not None
        assert status_path is not None
        try:
            remaining_seconds = timeout_seconds - (time.monotonic() - started_at)
            if remaining_seconds <= 0:
                raise subprocess.TimeoutExpired(launcher.args, timeout_seconds)
            exit_code = _wait_for_exit(launcher, remaining_seconds)
        except subprocess.TimeoutExpired:
            primary = (
                RunnerErrorCode.PREDICT_TIMEOUT
                if _launch_status(status_path) == b"S"
                else RunnerErrorCode.START_FAILED
            )
            raise _failure_after_cleanup(
                primary,
                "candidate_wait" if primary is RunnerErrorCode.PREDICT_TIMEOUT else "candidate_start",
                started_at,
                launcher.returncode,
                owner,
                launcher,
                readers,
                status_directory,
            )
        except BaseException as error:
            cleanup = _cleanup_started(owner, launcher, readers)
            status_ok = _cleanup_status_directory(status_directory)
            _annotate_cleanup_failure(error, cleanup, status_ok)
            raise

        if _launch_status(status_path) != b"S":
            raise _failure_after_cleanup(
                RunnerErrorCode.START_FAILED,
                "candidate_start",
                started_at,
                exit_code,
                owner,
                launcher,
                readers,
                status_directory,
            )
        try:
            tree_alive = owner.is_alive(launcher)
        except (OSError, RuntimeError):
            raise _failure_after_cleanup(
                RunnerErrorCode.CLEANUP_FAILED,
                "descendant_query",
                started_at,
                exit_code,
                owner,
                launcher,
                readers,
                status_directory,
            ) from None
        if tree_alive:
            raise _failure_after_cleanup(
                RunnerErrorCode.PROCESS_LEAKED,
                "descendant_check",
                started_at,
                exit_code,
                owner,
                launcher,
                readers,
                status_directory,
            )

        try:
            readers_ok = _finish_readers(readers)
            close_ok = owner.close()
        except BaseException:
            readers_ok = False
            close_ok = False
        status_ok = _cleanup_status_directory(status_directory)
        if not readers_ok or not close_ok or not status_ok:
            raise _run_error(
                RunnerErrorCode.CLEANUP_FAILED,
                "success_cleanup",
                started_at,
                exit_code,
                readers,
            )
        if exit_code != 0:
            raise _run_error(
                RunnerErrorCode.PREDICT_CRASH,
                "candidate_exit",
                started_at,
                exit_code,
                readers,
            )
        if not _valid_predictions(validated):
            raise _run_error(
                RunnerErrorCode.INVALID_PREDICTIONS,
                "prediction_artifact",
                started_at,
                exit_code,
                readers,
            )
        stdout_tail, stderr_tail = _tail_values(readers)
        return LocalRunReceipt(
            predictions=process_context.predictions,
            exit_code=exit_code,
            duration_ms=_duration_ms(started_at),
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
        )
    except BaseException as error:
        cleanup = _cleanup_started(owner, launcher, readers)
        status_ok = _cleanup_status_directory(status_directory)
        if isinstance(error, RunnerError):
            if (
                (not cleanup.cleanup_ok or not status_ok)
                and error.code is not RunnerErrorCode.CLEANUP_FAILED
            ):
                raise _run_error(
                    RunnerErrorCode.CLEANUP_FAILED,
                    "exception_cleanup",
                    started_at,
                    launcher.returncode,
                    readers,
                ) from None
            if cleanup.process_leaked and error.code is not RunnerErrorCode.PROCESS_LEAKED:
                raise _run_error(
                    RunnerErrorCode.PROCESS_LEAKED,
                    "exception_process_leak",
                    started_at,
                    launcher.returncode,
                    readers,
                ) from None
            raise
        _annotate_cleanup_failure(error, cleanup, status_ok)
        raise
    finally:
        _safe_close_owner(owner)
        _cleanup_status_directory(status_directory)


def _new_tree_owner() -> _TreeOwner:
    if os.name == "posix":
        return _PosixTreeOwner()
    if os.name == "nt":
        return _WindowsTreeOwner()
    raise OSError("unsupported process-tree platform")


def _wait_for_exit(process: subprocess.Popen[bytes], timeout_seconds: float) -> int:
    return process.wait(timeout=timeout_seconds)


def _cleanup_started(
    owner: _TreeOwner | None,
    process: subprocess.Popen[bytes] | None,
    readers: tuple[_PipeReader, _PipeReader] | None,
) -> _TerminationResult:
    ok = True
    leaked = False
    if process is not None:
        if process.stdin is not None:
            try:
                process.stdin.close()
            except BaseException:
                ok = False
        if owner is not None:
            try:
                termination = owner.terminate(process)
            except BaseException:
                termination = _TerminationResult(False)
            ok = termination.cleanup_ok and ok
            leaked = termination.process_leaked
        elif process.poll() is None:
            try:
                process.kill()
                _final_wait(process)
            except BaseException:
                ok = False
        if readers is None:
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except BaseException:
                        ok = False
    try:
        readers_ok = _finish_readers(readers)
    except BaseException:
        readers_ok = False
    ok = readers_ok and ok
    if owner is not None:
        try:
            close_ok = owner.close()
        except BaseException:
            close_ok = False
        ok = close_ok and ok
    return _TerminationResult(ok, leaked)


def _failure_after_cleanup(
    primary_code: RunnerErrorCode,
    stage: str,
    started_at: float,
    exit_code: int | None,
    owner: _TreeOwner,
    process: subprocess.Popen[bytes],
    readers: tuple[_PipeReader, _PipeReader],
    status_directory: TemporaryDirectory[str],
) -> RunnerError:
    cleanup = _cleanup_started(owner, process, readers)
    status_ok = _cleanup_status_directory(status_directory)
    if not cleanup.cleanup_ok or not status_ok:
        code = RunnerErrorCode.CLEANUP_FAILED
        failure_stage = "failure_cleanup"
    elif cleanup.process_leaked:
        code = RunnerErrorCode.PROCESS_LEAKED
        failure_stage = "failure_process_leak"
    else:
        code = primary_code
        failure_stage = stage
    return _run_error(
        code,
        failure_stage,
        started_at,
        exit_code,
        readers,
    )


def _launch_status(path: Path) -> bytes | None:
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    return payload if payload in {b"S", b"F"} else None


def _cleanup_status_directory(directory: TemporaryDirectory[str] | None) -> bool:
    if directory is None:
        return True
    try:
        directory.cleanup()
    except OSError:
        return False
    return True


def _annotate_cleanup_failure(
    error: BaseException,
    cleanup: _TerminationResult,
    status_ok: bool,
) -> None:
    if not cleanup.cleanup_ok or not status_ok:
        error.add_note(RunnerErrorCode.CLEANUP_FAILED.value)
    elif cleanup.process_leaked:
        error.add_note(RunnerErrorCode.PROCESS_LEAKED.value)


def _safe_close_owner(owner: _TreeOwner | None) -> None:
    if owner is None:
        return
    try:
        owner.close()
    except BaseException:
        pass


def _finish_readers(readers: tuple[_PipeReader, _PipeReader] | None) -> bool:
    if readers is None:
        return True
    results = tuple(reader.finish() for reader in readers)
    return all(results)


def _tail_values(readers: tuple[_PipeReader, _PipeReader] | None) -> tuple[str, str]:
    if readers is None:
        return "", ""
    return readers[0].buffer.decode(), readers[1].buffer.decode()


def _tails(readers: tuple[_PipeReader, _PipeReader] | None) -> dict[str, str]:
    stdout_tail, stderr_tail = _tail_values(readers)
    return {"stdout_tail": stdout_tail, "stderr_tail": stderr_tail}


def _run_error(
    code: RunnerErrorCode,
    stage: str,
    started_at: float,
    exit_code: int | None,
    readers: tuple[_PipeReader, _PipeReader] | None,
) -> RunnerError:
    return RunnerError(
        code=code,
        stage=stage,
        exit_code=exit_code,
        duration_ms=_duration_ms(started_at),
        **_tails(readers),
    )


def _duration_ms(started_at: float) -> int:
    return max(0, int((time.monotonic() - started_at) * 1000))


def _valid_predictions(validated: _ValidatedRequest) -> bool:
    process = validated.process
    if (
        not _same_path_identity(process.cwd, validated.cwd_identity)
        or not _same_path_identity(process.slate.parent, validated.input_identity)
        or not _same_path_identity(process.slate, validated.slate_identity)
        or not _same_path_identity(process.predictions.parent, validated.output_identity)
    ):
        return False
    try:
        status = process.predictions.lstat()
    except OSError:
        return False
    return stat.S_ISREG(status.st_mode)


def _send_posix_signal(process_group: int, signal_number: int) -> None:
    try:
        os.killpg(process_group, signal_number)
    except ProcessLookupError:
        pass


def _poll_absent(is_alive: Callable[[], bool], timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)


def _final_wait(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)


def _create_windows_job() -> tuple[_WindowsJobApi, object]:
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_operation_count", ctypes.c_ulonglong),
            ("write_operation_count", ctypes.c_ulonglong),
            ("other_operation_count", ctypes.c_ulonglong),
            ("read_transfer_count", ctypes.c_ulonglong),
            ("write_transfer_count", ctypes.c_ulonglong),
            ("other_transfer_count", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time_limit", ctypes.c_longlong),
            ("per_job_user_time_limit", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("basic_limit_information", _BasicLimitInformation),
            ("io_info", _IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        ]

    class _BasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("total_user_time", ctypes.c_longlong),
            ("total_kernel_time", ctypes.c_longlong),
            ("this_period_total_user_time", ctypes.c_longlong),
            ("this_period_total_kernel_time", ctypes.c_longlong),
            ("total_page_fault_count", wintypes.DWORD),
            ("total_processes", wintypes.DWORD),
            ("active_processes", wintypes.DWORD),
            ("total_terminated_processes", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise OSError("windows job creation failed")
    information = _ExtendedLimitInformation()
    information.basic_limit_information.limit_flags = 0x00002000
    if not kernel32.SetInformationJobObject(
        handle,
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        kernel32.CloseHandle(handle)
        raise OSError("windows job configuration failed")
    return _WindowsJobApi(kernel32, _BasicAccountingInformation), handle
