"""Judge 오류가 generator context와 실제 run lock에서 원형을 유지하는지 검증한다."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType

import pytest

from autoresearch.research_harness.judge_errors import JudgeError, JudgeErrorCode
from autoresearch.research_harness.local_runtime import _run_lock


@contextmanager
def _operation_boundary(events: list[str]) -> Iterator[None]:
    try:
        yield
    finally:
        events.append("cleanup")


def _error(code: JudgeErrorCode = JudgeErrorCode.INVALID_TARGET) -> JudgeError:
    return JudgeError(code, "judge_target", 7)


@pytest.mark.parametrize("code", list(JudgeErrorCode))
def test_nested_contextmanagers_preserve_every_judge_code(
    code: JudgeErrorCode,
) -> None:
    original = _error(code)
    events: list[str] = []

    with pytest.raises(JudgeError) as caught:
        with _operation_boundary(events), _operation_boundary(events):
            raise original

    assert caught.value is original
    assert caught.value.code is code
    assert caught.value.stage == "judge_target"
    assert caught.value.row_number == 7
    assert isinstance(caught.value.__traceback__, TracebackType)
    assert events == ["cleanup", "cleanup"]


@pytest.mark.parametrize("code", list(JudgeErrorCode))
def test_run_lock_propagates_original_judge_error(
    tmp_path: Path, code: JudgeErrorCode,
) -> None:
    original = _error(code)

    with pytest.raises(JudgeError) as caught:
        with _run_lock(tmp_path):
            raise original

    assert caught.value is original
    assert caught.value.code is code
    assert caught.value.stage == "judge_target"
    assert caught.value.row_number == 7
    assert isinstance(caught.value.__traceback__, TracebackType)
