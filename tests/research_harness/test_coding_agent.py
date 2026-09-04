from __future__ import annotations

from dataclasses import replace
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import Literal

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
    recorded = json.loads((request.artifact_root / "receipt.json").read_text())
    assert recorded["approval_policy"] == "never"
    assert recorded["windows_sandbox"] == ("elevated" if os.name == "nt" else None)


@pytest.mark.parametrize("platform_name", ["nt", "posix"])
@pytest.mark.parametrize("mode", ["workspace-write", "read-only"])
def test_platform_sandbox_settings_are_explicit_without_broadening_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform_name: str,
    mode: Literal["workspace-write", "read-only"],
) -> None:
    request = replace(_request(tmp_path), mode=mode)
    config = _config()
    monkeypatch.setattr(agent, "os", SimpleNamespace(name=platform_name))
    args = agent._codex_argv(config, request)
    settings = [args[index + 1] for index, value in enumerate(args) if value == "-c"]
    assert 'approval_policy="never"' in settings
    if platform_name == "nt":
        assert 'windows.sandbox="elevated"' in settings
    else:
        assert not any(value.startswith("windows.") for value in settings)
    assert args == (
        str(config.executable), "exec", "--ephemeral", "--ignore-user-config",
        "--sandbox", mode, "--json", "--skip-git-repo-check",
        "--output-schema", str(request.artifact_root / "schema.json"),
        "-o", str(request.artifact_root / "response.json"), "-m", "explicit-model",
        "-c", 'model_reasoning_effort="medium"', "-c", 'approval_policy="never"',
        *(("-c", 'windows.sandbox="elevated"') if platform_name == "nt" else ()),
        "-C", str(request.cwd), "-",
    )


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


@pytest.mark.parametrize("platform,has_inputs,mode,expected", [
    ("nt", True, "workspace-write", 1), ("nt", False, "workspace-write", 0),
    ("nt", False, "read-only", 0), ("posix", True, "workspace-write", 0),
])
def test_candidate_input_access_is_windows_codex_prepare_only(tmp_path, monkeypatch, platform, has_inputs, mode, expected):
    request = replace(_request(tmp_path), mode=mode,
                      candidate_inputs=agent.CandidateInputIdentity("a" * 64, "eval_" + "b" * 64) if has_inputs else None)
    request.artifact_root.mkdir()
    calls = []
    monkeypatch.setattr(agent, "grant_input_read", lambda *args: calls.append(args) or {"status": "complete"})
    monkeypatch.setattr(agent, "os", SimpleNamespace(**{**vars(os), "name": platform}))
    agent._prepare_input_access(request)
    assert len(calls) == expected
    assert (request.artifact_root / "input-access.json").exists() == bool(expected)


@pytest.mark.parametrize("platform,has_inputs", [("nt", True), ("nt", False), ("posix", True)])
def test_temp_settings_apply_only_to_candidate_shell_not_host(tmp_path, monkeypatch, platform, has_inputs):
    request = replace(_request(tmp_path), candidate_inputs=agent.CandidateInputIdentity("a" * 64, "eval_" + "b" * 64) if has_inputs else None)
    monkeypatch.setattr(agent, "os", SimpleNamespace(**{**vars(os), "name": platform}))
    monkeypatch.setenv("TEMP", "host-temp-sentinel")
    settings = agent._temp_settings(request)
    assert bool(settings) == (platform == "nt" and has_inputs)
    assert agent._environment(_config())["TEMP"] == "host-temp-sentinel"
    if settings:
        assert len(settings) == 8
        assert all(str(request.cwd / "harness_out/.agent-tmp/runtime") == json.loads(value.split("=", 1)[1]) for value in settings[1::2])


def _temp_request(tmp_path):
    request = _request(tmp_path)
    request.artifact_root.mkdir()
    (request.cwd / ".git").write_text("gitdir: trusted")
    (request.cwd / "harness_out").mkdir()
    return request


def test_temp_cleanup_is_deferred_and_adds_verified_sidecar(tmp_path, monkeypatch):
    request = _temp_request(tmp_path)
    calls = []
    def clean(*args):
        calls.append("cleanup")
        (request.cwd / "harness_out/.agent-tmp/runtime").rmdir()
        return {"status": "complete", "removed_count": 1}
    monkeypatch.setattr(agent, "_cleanup_temp_process", clean)
    with ExitStack() as stack:
        state = stack.enter_context(agent._temp_lifecycle(_config(), request))
        state["process_ready"] = True
        assert calls == []
    assert calls == ["cleanup"]
    receipt = json.loads((request.artifact_root / "temp-cleanup.json").read_bytes())
    assert receipt["host_verified_empty"] is True


def test_unreclaimed_agent_never_starts_temp_helper(tmp_path, monkeypatch):
    request = _temp_request(tmp_path)
    monkeypatch.setattr(agent, "_cleanup_temp_process", lambda *args: pytest.fail("unsafe helper launch"))
    with pytest.raises(agent.CodingAgentError, match="agent_temp_cleanup_failed"):
        with agent._temp_lifecycle(_config(), request):
            pass
    assert json.loads((request.artifact_root / "temp-cleanup.json").read_bytes())["status"] == "not_run"


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_cleanup_failure_preserves_original_exception_and_partial_count(tmp_path, monkeypatch, error_type):
    request = _temp_request(tmp_path)
    monkeypatch.setattr(agent, "_cleanup_temp_process", lambda *args: {"status": "failed", "removed_count": 2})
    original = error_type("original")
    with pytest.raises(error_type) as caught:
        with agent._temp_lifecycle(_config(), request) as state:
            state["process_ready"] = True
            raise original
    assert caught.value is original
    assert "agent_temp_cleanup_failed" in original.__notes__
    assert json.loads((request.artifact_root / "temp-cleanup.json").read_bytes())["removed_count"] == 2


def test_helper_success_without_actual_empty_anchor_is_failure(tmp_path, monkeypatch):
    request = _temp_request(tmp_path)
    monkeypatch.setattr(agent, "_cleanup_temp_process", lambda *args: {"status": "complete", "removed_count": 0})
    with pytest.raises(agent.CodingAgentError, match="agent_temp_cleanup_failed"):
        with agent._temp_lifecycle(_config(), request) as state:
            state["process_ready"] = True
            (request.cwd / "harness_out/.agent-tmp/remains").touch()
    assert json.loads((request.artifact_root / "temp-cleanup.json").read_bytes())["status"] == "failed"


def test_helper_interrupt_preserves_partial_sidecar_and_cancellation(tmp_path, monkeypatch):
    request = _temp_request(tmp_path)
    interruption = KeyboardInterrupt()
    interruption.temp_cleanup_receipt = {"status": "interrupted", "removed_count": 1}
    def interrupt(*args):
        raise interruption
    monkeypatch.setattr(agent, "_cleanup_temp_process", interrupt)
    with pytest.raises(KeyboardInterrupt) as caught:
        with agent._temp_lifecycle(_config(), request) as state:
            state["process_ready"] = True
    assert caught.value is interruption
    assert json.loads((request.artifact_root / "temp-cleanup.json").read_bytes())["removed_count"] == 1


def test_original_coding_error_receives_deferred_cleanup_evidence(tmp_path, monkeypatch):
    request = _temp_request(tmp_path)
    monkeypatch.setattr(agent, "_cleanup_temp_process", lambda *args: {"status": "failed", "removed_count": 1})
    original = agent.CodingAgentError("agent_crash", "exit", duration_ms=73)
    with pytest.raises(agent.CodingAgentError) as caught:
        with agent._temp_lifecycle(_config(), request) as state:
            state["process_ready"] = True
            raise original
    assert caught.value is original and original.duration_ms == 73
    assert any(item.uri.endswith("temp-cleanup.json") for item in original.artifacts)


def test_temp_helper_owned_gate_uses_fixed_sandbox_and_preserves_logs(tmp_path, monkeypatch):
    request = _temp_request(tmp_path)
    registration = agent._agent_temp.register(request.cwd)
    agent._write(request.artifact_root / "temp-registration.json", agent._json_bytes(registration))
    (request.cwd / "harness_out/.agent-tmp/scratch").touch()
    popen = agent.subprocess.Popen
    commands = []
    def fake_sandbox(command, **kwargs):
        commands.append(command)
        # CLI 전용 경계만 대체하고 실제 gate/owner/stdlib worker는 그대로 실행한다.
        index = command.index("sandbox")
        helper = command[-1]
        return popen((*command[:index - 1], sys.executable, "-I", "-S", helper), **kwargs)
    monkeypatch.setattr(agent.subprocess, "Popen", fake_sandbox)
    result = agent._cleanup_temp_process(_config(), request)
    assert result["status"] == "complete" and result["removed_count"] == 2
    assert result["process_cleanup_ok"] is True
    command = commands[0]
    assert command[command.index("--permission-profile") + 1] == ":workspace"
    assert "--include-managed-config" in command and 'windows.sandbox="elevated"' in command
    assert command[-4:-1] == (sys.executable, "-I", "-S")
    assert (request.artifact_root / "temp-cleanup.stdout.log").exists()
    assert (request.artifact_root / "temp-cleanup.stderr.log").exists()


def test_temp_helper_accepts_missing_runtime_root_after_anchor_preflight(tmp_path, monkeypatch):
    request = _temp_request(tmp_path)
    registration = agent._agent_temp.register(request.cwd)
    agent._write(request.artifact_root / "temp-registration.json", agent._json_bytes(registration))
    (request.cwd / "harness_out/.agent-tmp/runtime").rmdir()
    popen = agent.subprocess.Popen
    commands = []

    def fake_sandbox(command, **kwargs):
        commands.append(command)
        index = command.index("sandbox")
        helper = command[-1]
        return popen((*command[:index - 1], sys.executable, "-I", "-S", helper), **kwargs)

    monkeypatch.setattr(agent.subprocess, "Popen", fake_sandbox)

    result = agent._cleanup_temp_process(_config(), request)

    assert result["status"] == "complete"
    assert result["object_count"] == result["removed_count"] == 0
    assert result["process_cleanup_ok"] is True
    assert len(commands) == 1


def test_temp_lifecycle_host_verification_accepts_missing_runtime_root(tmp_path, monkeypatch):
    request = _temp_request(tmp_path)
    monkeypatch.setattr(
        agent,
        "_cleanup_temp_process",
        lambda *args: {"status": "complete", "object_count": 0, "removed_count": 0},
    )

    with agent._temp_lifecycle(_config(), request) as state:
        state["process_ready"] = True
        (request.cwd / "harness_out/.agent-tmp/runtime").rmdir()

    receipt = json.loads((request.artifact_root / "temp-cleanup.json").read_bytes())
    assert receipt["status"] == "complete"
    assert receipt["host_verified_empty"] is True


def test_changed_temp_boundary_never_launches_helper(tmp_path, monkeypatch):
    request = _temp_request(tmp_path)
    registration = agent._agent_temp.register(request.cwd)
    agent._write(request.artifact_root / "temp-registration.json", agent._json_bytes(registration))
    (request.cwd / ".git").write_text("changed")
    monkeypatch.setattr(agent.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("boundary must stop launch"))
    assert agent._cleanup_temp_process(_config(), request)["status"] == "failed"


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
def test_helper_log_failure_cannot_mask_interruption_and_partial_receipt(tmp_path, monkeypatch, kind):
    request = _temp_request(tmp_path)
    registration = agent._agent_temp.register(request.cwd)
    agent._write(request.artifact_root / "temp-registration.json", agent._json_bytes(registration))
    (request.cwd / "harness_out/.agent-tmp/scratch").touch()
    popen = agent.subprocess.Popen
    def fake_sandbox(command, **kwargs):
        index = command.index("sandbox")
        return popen((*command[:index - 1], sys.executable, "-I", "-S", command[-1]), **kwargs)
    monkeypatch.setattr(agent.subprocess, "Popen", fake_sandbox)
    new_owner = agent._new_tree_owner
    interruption = kind()
    def interrupted_owner():
        owner = new_owner()
        monkeypatch.setattr(owner, "is_alive", lambda process: (_ for _ in ()).throw(interruption))
        return owner
    monkeypatch.setattr(agent, "_new_tree_owner", interrupted_owner)
    write = agent._write
    def failed_log(path, payload):
        if path.name.endswith(".log"):
            raise OSError("local evidence unavailable")
        write(path, payload)
    monkeypatch.setattr(agent, "_write", failed_log)
    with pytest.raises(kind) as caught:
        agent._cleanup_temp_process(_config(), request)
    assert caught.value is interruption
    assert interruption.temp_cleanup_receipt["removed_count"] == 2
    assert interruption.temp_cleanup_receipt["status"] == "interrupted"
    assert interruption.temp_cleanup_receipt["log_evidence_failed"] is True


@pytest.mark.parametrize("behavior", ["attach_failure", "timeout", "leak", "cleanup_failure"])
def test_temp_helper_gate_and_process_failures_are_closed(tmp_path, monkeypatch, behavior):
    request = _temp_request(tmp_path)
    registration = agent._agent_temp.register(request.cwd)
    agent._write(request.artifact_root / "temp-registration.json", agent._json_bytes(registration))
    marker = tmp_path / "released"
    script = tmp_path / "fake_cleanup.py"
    script.write_text(
        "import sys, time, subprocess\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).touch()\n"
        + ("time.sleep(30)\n" if behavior == "timeout" else "")
        + ("subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n" if behavior == "leak" else "")
        + "print('{\"status\":\"complete\",\"removed_count\":0}')\n", encoding="utf-8")
    popen = agent.subprocess.Popen
    def fake_sandbox(command, **kwargs):
        index = command.index("sandbox")
        return popen((*command[:index - 1], sys.executable, "-I", "-S", str(script)), **kwargs)
    monkeypatch.setattr(agent.subprocess, "Popen", fake_sandbox)
    if behavior == "attach_failure":
        new_owner = agent._new_tree_owner
        def bad_owner():
            owner = new_owner()
            monkeypatch.setattr(owner, "attach", lambda process: (_ for _ in ()).throw(OSError("attach failed")))
            return owner
        monkeypatch.setattr(agent, "_new_tree_owner", bad_owner)
    if behavior == "timeout":
        monkeypatch.setattr(agent, "_TEMP_TIMEOUT_SECONDS", 0.3)
    if behavior == "cleanup_failure":
        original = agent._cleanup_started
        def cleanup_failed(*args):
            result = original(*args)
            return SimpleNamespace(cleanup_ok=False, process_leaked=result.process_leaked)
        monkeypatch.setattr(agent, "_cleanup_started", cleanup_failed)
    result = agent._cleanup_temp_process(_config(), request)
    assert result["status"] == "failed"
    if behavior == "attach_failure":
        assert not marker.exists()
    if behavior == "timeout":
        assert result["error_type"] == "TimeoutExpired"
    if behavior == "cleanup_failure":
        assert result["process_cleanup_ok"] is False


@pytest.mark.skipif(os.name != "nt", reason="Windows coding prepare integration")
@pytest.mark.parametrize("behavior", ["success", "leak", "cleanup_failure"])
def test_windows_coding_prepare_runs_temp_cleanup_only_after_safe_process_reclaim(tmp_path, monkeypatch, behavior):
    request = replace(_request(tmp_path), candidate_inputs=agent.CandidateInputIdentity("a" * 64, "eval_" + "b" * 64))
    (request.cwd / ".git").write_text("gitdir: trusted")
    (request.cwd / "harness_out").mkdir()
    monkeypatch.setattr(agent, "_prepare_input_access", lambda request: None)
    calls = []
    def clean(*args):
        calls.append("cleanup")
        (request.cwd / "harness_out/.agent-tmp/runtime").rmdir()
        return {"status": "complete", "removed_count": 1}
    monkeypatch.setattr(agent, "_cleanup_temp_process", clean)
    _fake(monkeypatch, tmp_path, "out.write_text('{}')\n" + (
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n" if behavior == "leak" else ""))
    if behavior == "cleanup_failure":
        original = agent._cleanup_started
        def cleanup_failed(*args):
            result = original(*args)
            return SimpleNamespace(cleanup_ok=False, process_leaked=result.process_leaked)
        monkeypatch.setattr(agent, "_cleanup_started", cleanup_failed)
    if behavior == "success":
        receipt = agent.CodexCodingAgent(_config()).run(request)
        assert any(item.uri.endswith("temp-cleanup.json") for item in receipt.artifacts)
        assert calls == ["cleanup"]
    else:
        with pytest.raises(agent.CodingAgentError) as caught:
            agent.CodexCodingAgent(_config()).run(request)
        assert caught.value.code in {"agent_process_leaked", "agent_cleanup_failed"}
        assert calls == []
        assert json.loads((request.artifact_root / "temp-cleanup.json").read_bytes())["status"] == "not_run"


def test_read_only_request_cannot_receive_input_access_authority(tmp_path, monkeypatch):
    request = replace(_request(tmp_path), mode="read-only", candidate_inputs=agent.CandidateInputIdentity("a" * 64, "eval_" + "b" * 64))
    monkeypatch.setattr(agent, "_run", lambda *args: pytest.fail("invalid request must not start"))
    with pytest.raises(agent.CodingAgentError, match="agent_invalid_request"):
        agent.CodexCodingAgent(_config()).run(request)


def test_input_grant_failure_is_receipted_without_any_process(tmp_path, monkeypatch):
    request = _request(tmp_path)
    def fail_before_process(request):
        agent._write(request.artifact_root / "input-access.json", b'{"status":"failed","applied_count":1}')
        raise agent.CodingAgentError("agent_input_access_failed", "input_access")
    monkeypatch.setattr(agent, "_prepare_input_access", fail_before_process)
    monkeypatch.setattr(agent, "_new_tree_owner", lambda: pytest.fail("process ownership must not start"))
    monkeypatch.setattr(agent.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("CLI must not start"))
    with pytest.raises(agent.CodingAgentError) as caught:
        agent.CodexCodingAgent(_config()).run(request)
    assert caught.value.code == "agent_input_access_failed"
    receipt = json.loads((request.artifact_root / "receipt.json").read_bytes())
    assert receipt["exit_code"] is None and receipt["usage"]["input_tokens"] is None
    assert any(item.uri.endswith("input-access.json") for item in caught.value.artifacts)


def test_input_access_sidecar_preserves_partial_native_failure(tmp_path, monkeypatch):
    request = replace(_request(tmp_path), candidate_inputs=agent.CandidateInputIdentity("a" * 64, "eval_" + "b" * 64))
    request.artifact_root.mkdir()
    def failed_grant(*args):
        raise agent.InputAccessError("acl_apply", {"status": "failed", "applied_count": 1})
    monkeypatch.setattr(agent, "grant_input_read", failed_grant)
    monkeypatch.setattr(agent, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    with pytest.raises(agent.CodingAgentError, match="agent_input_access_failed"):
        agent._prepare_input_access(request)
    assert json.loads((request.artifact_root / "input-access.json").read_bytes()) == {"status": "failed", "applied_count": 1}


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
def test_input_access_interruption_publishes_partial_sidecar_and_rethrows(tmp_path, monkeypatch, kind):
    request = replace(_request(tmp_path), candidate_inputs=agent.CandidateInputIdentity("a" * 64, "eval_" + "b" * 64))
    request.artifact_root.mkdir()
    interruption = kind()
    interruption.input_access_receipt = {"status": "failed", "applied_count": 1, "interrupted": True}
    def interrupted_grant(*args):
        raise interruption
    monkeypatch.setattr(agent, "grant_input_read", interrupted_grant)
    monkeypatch.setattr(agent, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    with pytest.raises(kind) as caught:
        agent._prepare_input_access(request)
    assert caught.value is interruption
    assert json.loads((request.artifact_root / "input-access.json").read_bytes())["applied_count"] == 1


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
