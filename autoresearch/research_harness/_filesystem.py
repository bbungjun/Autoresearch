"""Research Harness의 durable local filesystem 동기화 primitive를 제공한다.

[파이프라인] Judge registry와 Trial Ledger가 write-once 상태를 게시하는 마지막
로컬 filesystem 동기화 구간을 담당한다.

[기능] POSIX와 Windows에서 부모 directory entry를 flush하는 내부 함수를 제공한다.

[비책임] 파일 생성·내용 쓰기, 원자 선점, 잠금과 상태 복구는 호출 모듈이 담당한다.
"""

from __future__ import annotations

import os
from pathlib import Path


def sync_directory(path: Path) -> None:
    """Flush directory metadata or raise OSError."""

    if os.name == "nt":
        _sync_windows_directory(path)
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_windows_directory(path: Path) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
    kernel32.FlushFileBuffers.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.CreateFileW(
        str(path),
        0x40000000,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x02000000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in {None, invalid_handle}:
        raise OSError(ctypes.get_last_error(), "directory_open_failed")
    try:
        if not kernel32.FlushFileBuffers(handle):
            raise OSError(ctypes.get_last_error(), "directory_sync_failed")
    finally:
        kernel32.CloseHandle(handle)
