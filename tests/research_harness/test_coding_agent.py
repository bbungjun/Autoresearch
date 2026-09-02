from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import pytest
from pydantic import ValidationError

from autoresearch.research_harness import coding_agent as agent


def _request(tmp_path: Path) -> agent.CodingAgentRequest:
    cwd = tmp_path / "candidate"
    cwd.mkdir()
    return agent.CodingAgentRequest(
        cwd=cwd,
        prompt="한 가설만 구현하십시오.",
        output_schema={"type": "object"},
        artifact_root=tmp_path / "evidence",
        mode="workspace-write",
    )


def _config(**changes: object) -> agent.CodexAgentConfig:
    values = dict(
        executable=Path(sys.executable).resolve(), model="explicit-model",
        reasoning_effort="medium", timeout_seconds=10.0,
    )
    values.update(changes)
    return agent.CodexAgentConfig(**values)


def _fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str) -> None:
    script = tmp_path / "fake_cli.py"
    script.write_text(
        "import json, os, sys, time, subprocess\nfrom pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "prompt = sys.stdin.read()\n"
        "out = Path(args[args.index('-o') + 1])\n" + body,
        encoding="utf-8",
    )
    original = agent._codex_argv
    monkeypatch.setattr(
        agent, "_codex_argv",
        lambda config, request: (sys.executable, str(script), *original(config, request)[1:]),
    )


def test_structured_response_explicit_arguments_usage_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    _fake(monkeypatch, tmp_path,
          "out.write_text(json.dumps({'args': args, 'prompt': prompt}), encoding='utf-8')\n"
          "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':13,"
          "'cached_input_tokens':7,'output_tokens':4,'reasoning_output_tokens':2}}))\n")
    receipt = agent.CodexCodingAgent(_config()).run(request)
    assert receipt.response["prompt"] == request.prompt
    args = receipt.response["args"]
    assert args[:6] == ["exec", "--ephemeral", "--ignore-user-config", "--sandbox", "workspace-write", "--json"]
    assert args[args.index("-m") + 1] == "explicit-model"
    assert 'model_reasoning_effort="medium"' in args
    assert args[-1] == "-"
    assert receipt.usage.input_tokens == 13
    assert receipt.usage.cached_input_tokens == 7
    assert receipt.usage.output_tokens == 4
    assert receipt.usage.reasoning_output_tokens == 2
    assert receipt.duration_ms > 0
    assert len(receipt.artifacts) >= 5
    for evidence in receipt.artifacts:
        path = request.artifact_root / evidence.uri.rsplit("/", 1)[-1]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == evidence.sha256
    assert "한 가설" not in repr(receipt)


def test_environment_drops_credentials_and_preserves_explicit_auth_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    auth = tmp_path / "auth"
    auth.mkdir()
    monkeypatch.setenv("OPENAI_API_KEY", "never-forward")
    monkeypatch.setenv("GITHUB_TOKEN", "never-forward")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "never-forward")
    _fake(monkeypatch, tmp_path, "out.write_text(json.dumps(dict(os.environ)), encoding='utf-8')\n")
    receipt = agent.CodexCodingAgent(_config(codex_home=auth)).run(request)
    assert receipt.response["CODEX_HOME"] == str(auth)
    assert not {"OPENAI_API_KEY", "GITHUB_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS"} & receipt.response.keys()
    assert receipt.usage.input_tokens is None
    assert json.loads((request.artifact_root / "receipt.json").read_text())["cost_usd"] is None


@pytest.mark.parametrize("body,code", [
    ("print('private-log'); sys.exit(3)\n", "agent_crash"),
    ("out.write_text('not-json')\n", "agent_invalid_response"),
    ("out.write_text('[]')\n", "agent_invalid_response"),
    ("out.write_text('{\"x\":NaN}')\n", "agent_invalid_response"),
    ("out.write_text('{\"x\":1e400}')\n", "agent_invalid_response"),
    ("out.write_text('{\"x\":1,\"x\":2}')\n", "agent_invalid_response"),
    ("pass\n", "agent_invalid_response"),
])
def test_failures_are_safe_and_preserve_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, code: str,
) -> None:
    request = _request(tmp_path)
    _fake(monkeypatch, tmp_path, body)
    with pytest.raises(agent.CodingAgentError) as caught:
        agent.CodexCodingAgent(_config()).run(request)
    assert caught.value.code == code
    assert caught.value.artifacts
    assert "private-log" not in repr(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert json.loads((request.artifact_root / "receipt.json").read_text())["error_code"] == code


def test_timeout_is_owned_and_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request(tmp_path)
    _fake(monkeypatch, tmp_path, "print('started', flush=True)\ntime.sleep(30)\n")
    with pytest.raises(agent.CodingAgentError) as caught:
        agent.CodexCodingAgent(_config(timeout_seconds=0.5)).run(request)
    assert caught.value.code == "agent_timeout"
    assert caught.value.duration_ms < 10000
    assert caught.value.artifacts


def test_logs_are_bounded_without_losing_late_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request(tmp_path)
    _fake(monkeypatch, tmp_path,
          "print('x' * (2 * 1024 * 1024))\n"
          "sys.stderr.write('e' * (2 * 1024 * 1024))\n"
          "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':99}}))\n"
          "out.write_text('{}')\n")
    receipt = agent.CodexCodingAgent(_config()).run(request)
    assert receipt.usage.input_tokens == 99
    assert (request.artifact_root / "stdout.log").stat().st_size <= 1024 * 1024
    assert (request.artifact_root / "stderr.log").stat().st_size <= 1024 * 1024
    assert json.loads((request.artifact_root / "receipt.json").read_text())["stdout_truncated"]


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True, "10"])
def test_timeout_configuration_is_strict(value: object) -> None:
    with pytest.raises(ValidationError):
        _config(timeout_seconds=value)


def test_artifact_overlap_and_existing_output_fail_before_launch(tmp_path: Path) -> None:
    request = _request(tmp_path)
    for root in (request.cwd / "evidence", request.cwd, tmp_path):
        with pytest.raises(agent.CodingAgentError, match="agent_invalid_request"):
            agent.CodexCodingAgent(_config()).run(replace(request, artifact_root=root))


def test_mode_is_required_and_rejects_permission_escalation(tmp_path: Path) -> None:
    request = _request(tmp_path)
    with pytest.raises(agent.CodingAgentError, match="agent_invalid_request"):
        agent.CodexCodingAgent(_config()).run(replace(request, mode="danger-full-access"))


def test_response_alias_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request(tmp_path)
    target = tmp_path / "response-source.json"
    target.write_text('{}')
    _fake(monkeypatch, tmp_path, f"os.link({str(target)!r}, out)\n")
    with pytest.raises(agent.CodingAgentError, match="agent_invalid_response"):
        agent.CodexCodingAgent(_config()).run(request)


def test_owner_assignment_failure_never_releases_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    marker = tmp_path / "must-not-run"
    _fake(monkeypatch, tmp_path, f"Path({str(marker)!r}).write_text(prompt)\n")
    original = agent._new_tree_owner

    def faulty_owner() -> object:
        owner = original()
        monkeypatch.setattr(owner, "attach", lambda process: (_ for _ in ()).throw(OSError("private-error")))
        return owner

    monkeypatch.setattr(agent, "_new_tree_owner", faulty_owner)
    with pytest.raises(agent.CodingAgentError, match="agent_start_failed"):
        agent.CodexCodingAgent(_config()).run(request)
    assert not marker.exists()


def test_interruption_is_recorded_without_swallowing_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    _fake(monkeypatch, tmp_path, "out.write_text('{}')\n")
    original = agent._new_tree_owner

    def interrupted_owner() -> object:
        owner = original()
        query = owner.is_alive
        calls = 0

        def interrupt_once(process: object) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise KeyboardInterrupt
            return query(process)

        monkeypatch.setattr(owner, "is_alive", interrupt_once)
        return owner

    monkeypatch.setattr(agent, "_new_tree_owner", interrupted_owner)
    with pytest.raises(KeyboardInterrupt):
        agent.CodexCodingAgent(_config()).run(request)
    assert json.loads((request.artifact_root / "receipt.json").read_text())["error_code"] == "agent_interrupted"


def test_cleanup_failure_is_not_reported_as_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request(tmp_path)
    _fake(monkeypatch, tmp_path, "out.write_text('{}')\n")
    original = agent._new_tree_owner

    def failing_owner() -> object:
        owner = original()
        close = owner.close

        def failed_close() -> bool:
            close()
            return False

        monkeypatch.setattr(owner, "close", failed_close)
        return owner

    monkeypatch.setattr(agent, "_new_tree_owner", failing_owner)
    with pytest.raises(agent.CodingAgentError, match="agent_cleanup_failed"):
        agent.CodexCodingAgent(_config()).run(request)


@pytest.mark.parametrize("sleep_parent", [True, False])
def test_descendant_process_is_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleep_parent: bool,
) -> None:
    request = _request(tmp_path)
    child_code = "import time; time.sleep(30)"
    _fake(monkeypatch, tmp_path,
          f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
          "Path('child.pid').write_text(str(child.pid))\n"
          "out.write_text('{}')\n" + ("time.sleep(30)\n" if sleep_parent else ""))
    with pytest.raises(agent.CodingAgentError) as caught:
        agent.CodexCodingAgent(_config(timeout_seconds=1.0)).run(request)
    assert caught.value.code in {"agent_timeout", "agent_process_leaked"}
    pid = int((request.cwd / "child.pid").read_text())
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            try:
                exit_code = wintypes.DWORD()
                assert kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                assert exit_code.value != 259
            finally:
                kernel.CloseHandle(handle)
    else:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("owned descendant remains alive")


def test_oversized_response_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request(tmp_path)
    _fake(monkeypatch, tmp_path, "out.write_bytes(b'x' * (1024 * 1024 + 1))\n")
    with pytest.raises(agent.CodingAgentError, match="agent_invalid_response"):
        agent.CodexCodingAgent(_config()).run(request)


def test_usage_missing_invalid_values_and_unterminated_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    _fake(monkeypatch, tmp_path,
          "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':-1,"
          "'cached_input_tokens':True,'output_tokens':0}}), end='')\n"
          "out.write_text('{}')\n")
    receipt = agent.CodexCodingAgent(_config()).run(request)
    assert receipt.usage == agent.AgentUsage(output_tokens=0)
