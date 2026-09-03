"""Stage C 오류의 예외 전달·구조 필드 불변성·안전한 메시지 계약 회귀."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import date
from traceback import format_exception
from types import TracebackType

import pytest

from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode


@contextmanager
def _operation_boundary(events: list[str]) -> Iterator[None]:
    try:
        yield
    finally:
        events.append("cleanup")


def _error(code: StageCErrorCode = StageCErrorCode.FIXTURE_STATE_CONFLICT) -> StageCError:
    return StageCError(code, "fixture_build", date(2026, 9, 1), 2, "비밀식별자-extra")


def test_contextmanager_propagates_original_error_without_pytest_wrapper() -> None:
    original = _error()
    events: list[str] = []

    try:
        with _operation_boundary(events):
            raise original
    except StageCError as caught:
        assert caught is original
        assert caught.code is StageCErrorCode.FIXTURE_STATE_CONFLICT
        assert caught.stage == "fixture_build"
        assert isinstance(caught.__traceback__, TracebackType)
    else:
        pytest.fail("StageCError가 전파되어야 합니다")
    assert events == ["cleanup"]


@pytest.mark.parametrize("code", list(StageCErrorCode))
def test_pytest_catches_original_error_through_nested_contextmanagers(
    code: StageCErrorCode,
) -> None:
    original = _error(code)
    events: list[str] = []

    with pytest.raises(StageCError) as caught:
        with _operation_boundary(events), _operation_boundary(events):
            raise original

    assert caught.value is original
    assert caught.value.code is code
    assert caught.value.stage == "fixture_build"
    assert caught.value.dt == date(2026, 9, 1)
    assert caught.value.count == 2
    assert events == ["cleanup", "cleanup"]


@pytest.mark.parametrize("field", ["code", "stage", "dt", "count", "identifier_prefix"])
@pytest.mark.parametrize("operation", ["assign", "delete"])
def test_structured_fields_remain_immutable(field: str, operation: str) -> None:
    error = _error()
    original = asdict(error)
    original_hash = hash(error)

    with pytest.raises(FrozenInstanceError):
        if operation == "assign":
            setattr(error, field, None)
        else:
            delattr(error, field)

    assert asdict(error) == original
    assert hash(error) == original_hash


@pytest.mark.parametrize("field", ["dt", "count", "identifier_prefix"])
def test_optional_fields_are_immutable_when_initialized_to_none(field: str) -> None:
    error = StageCError(StageCErrorCode.FIXTURE_STATE_CONFLICT, "fixture_build")

    with pytest.raises(FrozenInstanceError):
        setattr(error, field, "replacement")
    with pytest.raises(FrozenInstanceError):
        delattr(error, field)
    assert getattr(error, field) is None


def test_exception_runtime_metadata_can_be_updated_without_changing_fields() -> None:
    error = _error()
    original = asdict(error)
    original_hash = hash(error)
    cause = OSError("private-source-path")
    with pytest.raises(StageCError) as caught:
        raise error
    traceback = caught.value.__traceback__

    error.__traceback__ = None
    assert error.__traceback__ is None
    assert error.with_traceback(traceback) is error
    assert error.__traceback__ is traceback
    error.__context__ = cause
    error.__cause__ = cause
    error.__suppress_context__ = False
    assert error.__context__ is cause
    assert error.__cause__ is cause
    assert error.__suppress_context__ is False
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = True
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.__suppress_context__ is True
    error.add_note("safe operation note")
    assert error.__notes__ == ["safe operation note"]
    del error.__notes__
    assert not hasattr(error, "__notes__")
    assert asdict(error) == original
    assert hash(error) == original_hash


@pytest.mark.parametrize(
    "field", ["__traceback__", "__context__", "__cause__", "__suppress_context__"]
)
def test_exception_runtime_metadata_keeps_builtin_type_validation(field: str) -> None:
    error = _error()

    with pytest.raises(TypeError):
        setattr(error, field, "invalid metadata")


@pytest.mark.parametrize("suppress_cause", [False, True])
def test_exception_chaining_survives_contextmanager(suppress_cause: bool) -> None:
    original = _error()
    cause = OSError("private-source-path")

    with pytest.raises(StageCError) as caught:
        with _operation_boundary([]):
            try:
                raise cause
            except OSError:
                raise original from None if suppress_cause else cause

    assert caught.value is original
    assert original.__context__ is cause
    assert original.__cause__ is (None if suppress_cause else cause)
    assert original.__suppress_context__ is True
    assert "private-source-path" not in str(original)
    rendered = "".join(format_exception(original))
    if suppress_cause:
        assert "private-source-path" not in rendered
    else:
        assert "private-source-path" in rendered


def test_dataclass_value_contract_and_identifier_sanitization_are_preserved() -> None:
    error = _error()
    equivalent = _error()

    assert error == equivalent
    assert hash(error) == hash(equivalent)
    assert repr(error) == repr(equivalent)
    assert replace(error) == error
    assert replace(error, count=3) != error
    assert error.identifier_prefix == "비밀식별자-"
    assert len(error.identifier_prefix.encode("utf-8")) == 16
    assert str(error) == (
        "fixture_state_conflict: stage=fixture_build, dt=2026-09-01, "
        "count=2, identifier_present=True"
    )
    assert "비밀" not in str(error)
