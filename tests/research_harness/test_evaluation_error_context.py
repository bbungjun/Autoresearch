"""Stage B snapshot 오류가 contextlib 경계에서 원형을 유지하는지 검증한다."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from types import TracebackType

import pytest

from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)


@contextmanager
def _operation_boundary(events: list[str]) -> Iterator[None]:
    try:
        yield
    finally:
        events.append("cleanup")


def _error(
    code: SnapshotErrorCode = SnapshotErrorCode.SNAPSHOT_WRITE_CONFLICT,
) -> EvaluationSnapshotError:
    return EvaluationSnapshotError(
        code,
        "snapshot_publish",
        date(2026, 9, 1),
        2,
        "비밀식별자-extra",
    )


def test_contextmanager_propagates_original_snapshot_error() -> None:
    original = _error()
    events: list[str] = []

    try:
        with _operation_boundary(events):
            raise original
    except EvaluationSnapshotError as caught:
        assert caught is original
        assert caught.code is SnapshotErrorCode.SNAPSHOT_WRITE_CONFLICT
        assert caught.stage == "snapshot_publish"
        assert isinstance(caught.__traceback__, TracebackType)
    else:
        pytest.fail("EvaluationSnapshotError가 전파되어야 합니다")
    assert events == ["cleanup"]


@pytest.mark.parametrize("code", list(SnapshotErrorCode))
def test_nested_contextmanagers_preserve_every_snapshot_code(
    code: SnapshotErrorCode,
) -> None:
    original = _error(code)
    events: list[str] = []

    with pytest.raises(EvaluationSnapshotError) as caught:
        with _operation_boundary(events), _operation_boundary(events):
            raise original

    assert caught.value is original
    assert caught.value.code is code
    assert caught.value.stage == "snapshot_publish"
    assert caught.value.dt == date(2026, 9, 1)
    assert caught.value.count == 2
    assert caught.value.identifier_prefix == "비밀식별자-"
    assert events == ["cleanup", "cleanup"]
