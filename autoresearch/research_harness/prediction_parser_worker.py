"""격리된 prediction parser subprocess 진입점.

[파이프라인] Judge ingestion이 candidate 파일을 소유 사본으로 만든 뒤, scoring 전에 parser
시간·메모리 상한을 강제하는 구간을 담당한다.

[기능] POSIX address-space 상한을 먼저 설정하고 공통 prediction parser를 실행해 검증된 행을
exclusive 정규화 JSONL로 저장하고 성공 여부를 종료 코드로 반환한다.

[비책임] candidate 파일 복사, Judge target 결합, metric 계산과 판정은 각각
``prediction_ingestion``, ``judge``, ``judge_decision``이 담당한다.
"""

from __future__ import annotations

from pathlib import Path
import ctypes
import json
import os
import stat
import sys


_WINDOWS_JOB_HANDLE: object | None = None


def _apply_memory_limit(limit_bytes: int) -> None:
    if os.name == "posix":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        return
    if os.name == "nt":
        _apply_windows_job_memory_limit(limit_bytes)
        return
    raise OSError("unsupported memory-limit platform")


def _apply_windows_job_memory_limit(limit_bytes: int) -> None:
    """현재 parser process를 process-memory 제한 Job Object에 넣는다."""

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
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    information = _ExtendedLimitInformation()
    information.basic_limit_information.limit_flags = 0x00000100
    information.process_memory_limit = limit_bytes
    if not kernel32.SetInformationJobObject(
        handle,
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ) or not kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise ctypes.WinError(error)

    global _WINDOWS_JOB_HANDLE
    _WINDOWS_JOB_HANDLE = handle


def main(argv: list[str] | None = None) -> int:
    """자원 상한 적용 뒤 공통 parser를 실행한다."""

    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 6:
        return 2
    try:
        memory_limit = int(arguments[0])
        output_limit = int(arguments[1])
        expected_device = int(arguments[4])
        expected_inode = int(arguments[5])
        if memory_limit <= 0 or output_limit <= 0:
            return 2
        _apply_memory_limit(memory_limit)
        from prediction_parser import iter_prediction_copy
    except (MemoryError, OSError, ValueError):
        return 1
    output_fd = -1
    try:
        output_path = Path(arguments[3])
        flags = os.O_WRONLY | getattr(os, "O_BINARY", 0)
        output_fd = os.open(output_path, flags | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(output_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != expected_device
            or opened.st_ino != expected_inode
            or opened.st_size != 0
        ):
            os.close(output_fd)
            output_fd = -1
            return 1
        with os.fdopen(output_fd, "wb", closefd=True) as output:
            output_fd = -1
            written = 0
            for row in iter_prediction_copy(Path(arguments[2])):
                payload = (
                    json.dumps(
                        [row.evaluation_id, row.slate_id, row.video_id, row.score],
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ).encode("ascii")
                    + b"\n"
                )
                written += len(payload)
                if written > output_limit:
                    raise MemoryError
                output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        return 1
    finally:
        if output_fd >= 0:
            os.close(output_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
