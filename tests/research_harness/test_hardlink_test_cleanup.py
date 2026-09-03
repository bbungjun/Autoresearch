"""보안 테스트가 생성한 하드링크의 수명과 coding temp 회수를 검증한다.

[파이프라인] Candidate 테스트 실행 뒤, workspace 회수 전의 임시 파일 경계다.
[기능] 기존 보안 테스트를 등록 anchor 아래에서 실행하고 정상·실패 경로 모두
자신의 alias를 해제하여 원본과 외부 sentinel을 보존하는지 검증한다.
[비책임] 학습 입력 거부와 회수 정책은 local_training 및 _agent_temp 소유다.
"""

from pathlib import Path

import pytest

from autoresearch.research_harness import _agent_temp as temp
from tests.research_harness import test_agent_temp as temp_tests
from tests.research_harness import test_local_training as training_tests


@pytest.mark.parametrize("outcome", ["normal", "loader_error", "assertion_failure"])
def test_training_security_test_releases_alias_before_temp_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    cwd, registration = temp_tests._workspace(tmp_path)
    sentinel = cwd / "harness_out/keep.txt"
    sentinel.write_text("untouched")
    case = cwd / "harness_out/.agent-tmp/training-case"
    case.mkdir()
    original_write = training_tests.write_view
    before: list[bytes] = []

    def capture_view(root: Path) -> Path:
        path = original_write(root)
        before.append(path.read_bytes())
        return path

    def fail_loader(path: Path) -> None:
        raise RuntimeError("injected loader failure")

    monkeypatch.setattr(training_tests, "write_view", capture_view)
    if outcome == "loader_error":
        monkeypatch.setattr(training_tests.module(), "load_local_training_input", fail_loader)
    elif outcome == "assertion_failure":
        monkeypatch.setattr(training_tests.module(), "load_local_training_input", lambda path: None)
    try:
        if outcome == "normal":
            training_tests.test_hardlinked_input_is_rejected(case)
        elif outcome == "loader_error":
            with pytest.raises(RuntimeError, match="injected loader failure"):
                training_tests.test_hardlinked_input_is_rejected(case)
        else:
            with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
                training_tests.test_hardlinked_input_is_rejected(case)
        assert (case / "slate.parquet").read_bytes() == before[0]
        receipt = temp_tests._receipt()
        temp.clean(cwd, registration, receipt)
        assert receipt["status"] == "complete"
        temp.validate(cwd, registration, empty=True)
        assert sentinel.read_text() == "untouched"
        assert (cwd / ".git").read_text() == "gitdir: sentinel"
    finally:
        # 회귀가 실패해도 이 새 테스트가 만든 alias만 해제한다.
        (case / "alias.parquet").unlink(missing_ok=True)


@pytest.mark.parametrize("outcome", ["normal", "assertion_failure"])
def test_temp_security_test_releases_alias_before_outer_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    cwd, registration = temp_tests._workspace(tmp_path)
    sentinel = cwd / "harness_out/keep.txt"
    sentinel.write_text("untouched")
    case = cwd / "harness_out/.agent-tmp/security-case"
    case.mkdir()
    try:
        with monkeypatch.context() as patch:
            if outcome == "assertion_failure":
                patch.setattr(temp, "clean", lambda *args: None)
                with pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"):
                    temp_tests.test_hardlink_preflight_deletes_nothing(case)
            else:
                temp_tests.test_hardlink_preflight_deletes_nothing(case)
        assert (case / "outside").read_text() == "safe"
        assert (case / "workspace/harness_out/.agent-tmp/ordinary").read_text() == "preserved"
        receipt = temp_tests._receipt()
        temp.clean(cwd, registration, receipt)
        assert receipt["status"] == "complete"
        temp.validate(cwd, registration, empty=True)
        assert sentinel.read_text() == "untouched"
        assert (cwd / ".git").read_text() == "gitdir: sentinel"
    finally:
        (case / "workspace/harness_out/.agent-tmp/linked").unlink(missing_ok=True)
