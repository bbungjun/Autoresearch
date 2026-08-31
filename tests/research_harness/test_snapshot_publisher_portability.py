from pathlib import Path
import subprocess
import sys


def test_concurrency_module_collects_when_msvcrt_is_unavailable_on_posix() -> None:
    # Given
    module_path = Path(__file__).with_name("test_snapshot_publisher_concurrency.py")
    code = """
import builtins
import os
import pytest
import runpy
import sys
import types

original_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "msvcrt":
        raise ModuleNotFoundError("simulated POSIX: msvcrt unavailable")
    return original_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
posix_os = types.ModuleType("os")
posix_os.__dict__.update(os.__dict__)
posix_os.name = "posix"
sys.modules["os"] = posix_os
runpy.run_path(sys.argv[1], run_name="snapshot_publisher_concurrency_collection")
print("COLLECTION_OK")
"""

    # When
    completed = subprocess.run(
        [sys.executable, "-c", code, str(module_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    # Then
    assert (
        completed.returncode,
        completed.stdout.strip(),
        completed.stderr.strip(),
    ) == (0, "COLLECTION_OK", "")
