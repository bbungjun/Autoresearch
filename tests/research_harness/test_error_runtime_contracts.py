"""Snapshot/Judge 오류의 구조 필드와 Python 예외 metadata 계약을 검증한다."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import date
from traceback import format_exception

import pytest

from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.judge_errors import JudgeError, JudgeErrorCode


ErrorFactory = Callable[[], EvaluationSnapshotError | JudgeError]


@contextmanager
def _operation_boundary() -> Iterator[None]:
    yield


def _snapshot_error() -> EvaluationSnapshotError:
    return EvaluationSnapshotError(
        SnapshotErrorCode.SNAPSHOT_WRITE_CONFLICT,
        "snapshot_publish",
        date(2026, 9, 1),
        2,
        "비밀식별자-extra",
    )


def _judge_error() -> JudgeError:
    return JudgeError(JudgeErrorCode.INVALID_TARGET, "judge_target", 7)


@pytest.fixture(params=(_snapshot_error, _judge_error), ids=("snapshot", "judge"))
def error_factory(request: pytest.FixtureRequest) -> ErrorFactory:
    return request.param


@pytest.mark.parametrize("operation", ("assign", "delete"))
def test_structured_fields_remain_immutable(
    error_factory: ErrorFactory,
    operation: str,
) -> None:
    error = error_factory()
    original = asdict(error)
    original_hash = hash(error)

    for field in fields(error):
        with pytest.raises(FrozenInstanceError):
            if operation == "assign":
                setattr(error, field.name, None)
            else:
                delattr(error, field.name)

    assert asdict(error) == original
    assert hash(error) == original_hash


def test_exception_runtime_metadata_can_change_without_structured_fields(
    error_factory: ErrorFactory,
) -> None:
    error = error_factory()
    original = asdict(error)
    original_hash = hash(error)
    cause = OSError("private-source-path")
    with pytest.raises(type(error)) as caught:
        raise error
    traceback = caught.value.__traceback__

    error.__traceback__ = None
    assert error.with_traceback(traceback) is error
    error.__context__ = cause
    error.__cause__ = cause
    error.__suppress_context__ = False
    error.add_note("safe operation note")

    assert error.__traceback__ is traceback
    assert error.__context__ is cause
    assert error.__cause__ is cause
    assert error.__suppress_context__ is False
    assert error.__notes__ == ["safe operation note"]
    assert asdict(error) == original
    assert hash(error) == original_hash


@pytest.mark.parametrize(
    "field", ("__traceback__", "__context__", "__cause__", "__suppress_context__")
)
def test_exception_runtime_metadata_keeps_builtin_type_validation(
    error_factory: ErrorFactory,
    field: str,
) -> None:
    with pytest.raises(TypeError):
        setattr(error_factory(), field, "invalid metadata")


@pytest.mark.parametrize("suppress_cause", (False, True))
def test_exception_chaining_survives_contextmanager(
    error_factory: ErrorFactory,
    suppress_cause: bool,
) -> None:
    error = error_factory()
    cause = OSError("private-source-path")

    with pytest.raises(type(error)) as caught:
        with _operation_boundary():
            try:
                raise cause
            except OSError:
                raise error from None if suppress_cause else cause

    assert caught.value is error
    assert error.__context__ is cause
    assert error.__cause__ is (None if suppress_cause else cause)
    assert error.__suppress_context__ is True
    rendered = "".join(format_exception(error))
    if suppress_cause:
        assert "private-source-path" not in rendered
    else:
        assert "private-source-path" in rendered


def test_dataclass_value_contract_is_preserved(error_factory: ErrorFactory) -> None:
    error = error_factory()
    equivalent = error_factory()

    assert error == equivalent
    assert hash(error) == hash(equivalent)
    assert repr(error) == repr(equivalent)
    assert replace(error) == error
