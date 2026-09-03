"""Research Harness의 실제 coding agent 실행 경계.

[파이프라인] Controller가 가설을 선택한 뒤 candidate 코드 작성과 실험 기록 검토를
수행하는 외부 CLI 호출 구간이다. 학습·수치 판정은 LocalRunner/Domain 소유다.
[기능] 명시적 Codex 설정, 최소 인증 환경, owned process tree, bounded JSON/log/usage와
실패 evidence를 제공한다. 승인 정책은 never로 고정하고 Windows에서는 elevated
sandbox 구현을 명시하되 요청의 read-only/workspace-write 범위는 유지한다.
반환 JSON의 업무 스키마 검증은 호출자가 담당한다.
검증된 candidate 입력 identity가 전달된 Windows coding prepare에만 비상속 READ
ACE를 추가하며, 권한 준비가 실패하면 CLI를 시작하지 않고 실패 증거를 남긴다.
[비책임] Git 변경·commit, final grant·정답, Controller 재개와 REPORT는 각각 workspace,
local trial adapter와 Controller가 소유한다. 동일 OS 사용자에 대한 보안 격리는 아니다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, field_validator

from autoresearch.research_harness.ledger import LedgerArtifactEvidence
from autoresearch.research_harness._windows_sandbox_inputs import CandidateInputIdentity, InputAccessError, grant_input_read
from autoresearch.research_harness.runner import (
    _PipeReader,
    _TailBuffer,
    _TreeOwner,
    _cleanup_started,
    _new_tree_owner,
)


_LIMIT = 1024 * 1024
_EVENT_LIMIT = 64 * 1024
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_ENVIRONMENT = (
    "COMSPEC", "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE",
    "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "TMPDIR",
)


class CodexAgentConfig(BaseModel):
    """저장 로그인과 명시적 모델만 사용하는 실행 설정."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)
    executable: Path = Field(repr=False)
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: str = Field(min_length=1, max_length=40)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    codex_home: Path | None = Field(default=None, repr=False)

    @field_validator("executable", "codex_home")
    @classmethod
    def absolute_path(cls, value: Path | None) -> Path | None:
        if value is not None and (not value.is_absolute() or "\x00" in str(value)):
            raise ValueError("absolute path required")
        return value

    @field_validator("model", "reasoning_effort")
    @classmethod
    def nonblank_setting(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("nonblank setting required")
        return value


@dataclass(frozen=True, slots=True)
class CodingAgentRequest:
    """업무별 prompt/schema와 별도 evidence 루트를 전달하는 1회 호출."""

    cwd: Path = field(repr=False)
    prompt: str = field(repr=False)
    output_schema: dict[str, JsonValue] = field(repr=False)
    artifact_root: Path = field(repr=False)
    mode: Literal["workspace-write", "read-only"]
    candidate_inputs: CandidateInputIdentity | None = None


@dataclass(frozen=True, slots=True)
class AgentUsage:
    """CLI가 보고한 토큰만 기록하며 관측되지 않은 값은 null로 둔다."""

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class CodingAgentReceipt:
    """업무 호출자가 검증할 JSON과 재현 가능한 실행 evidence."""

    response: dict[str, JsonValue] = field(repr=False)
    artifacts: tuple[LedgerArtifactEvidence, ...] = field(repr=False)
    usage: AgentUsage
    duration_ms: int


@dataclass(slots=True, repr=False)
class CodingAgentError(Exception):
    """원본 로그·경로를 노출하지 않는 안전한 실행 실패."""

    code: str
    stage: str
    duration_ms: int = 0
    artifacts: tuple[LedgerArtifactEvidence, ...] = field(default=(), repr=False)

    def __str__(self) -> str:
        return f"{self.code}: stage={self.stage}"

    def __repr__(self) -> str:
        return f"CodingAgentError(code={self.code!r}, stage={self.stage!r})"


class CodingAgent(Protocol):
    """실제 CLI와 네트워크 없는 테스트가 공유하는 작은 호출 seam."""

    def run(self, request: CodingAgentRequest) -> CodingAgentReceipt: ...


class CodexCodingAgent:
    """같은 대화를 재개하지 않는 독립 Codex exec adapter."""

    def __init__(self, config: CodexAgentConfig) -> None:
        self.config = config

    def run(self, request: CodingAgentRequest) -> CodingAgentReceipt:
        """새 evidence 디렉터리에 한 번 실행하고 응답 또는 typed failure를 반환한다."""
        _validate_request(self.config, request)
        return _run(self.config, request)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def _safe_path(path: Path, *, directory: bool) -> bool:
    info = path.lstat()
    return (
        (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        and not bool(getattr(info, "st_file_attributes", 0) & 0x400)
        and (directory or info.st_nlink == 1)
    )


def _safe_ancestors(path: Path) -> bool:
    return all(_safe_path(parent, directory=True) for parent in (path, *path.parents))


def _validate_request(config: CodexAgentConfig, request: CodingAgentRequest) -> None:
    try:
        if not isinstance(config, CodexAgentConfig) or not isinstance(request, CodingAgentRequest):
            raise ValueError
        if request.mode not in {"workspace-write", "read-only"}:
            raise ValueError
        if request.candidate_inputs is not None and (
            request.mode != "workspace-write" or not isinstance(request.candidate_inputs, CandidateInputIdentity)
        ):
            raise ValueError
        if not isinstance(request.prompt, str) or not 0 < len(request.prompt.encode("utf-8")) <= _LIMIT:
            raise ValueError
        schema = _JSON_OBJECT.validate_python(request.output_schema, strict=True)
        if len(_json_bytes(schema)) > _LIMIT:
            raise ValueError
        if any(not isinstance(path, Path) or not path.is_absolute()
               for path in (request.cwd, request.artifact_root)):
            raise ValueError
        cwd = request.cwd.resolve(strict=True)
        root = request.artifact_root.resolve()
        if root.is_relative_to(cwd) or cwd.is_relative_to(root) or os.path.lexists(root):
            raise ValueError
        if not _safe_ancestors(request.cwd) or not _safe_ancestors(request.artifact_root.parent):
            raise ValueError
        if not _safe_path(config.executable, directory=False):
            raise ValueError
        if config.codex_home is not None and not _safe_ancestors(config.codex_home):
            raise ValueError
    except (ValueError, OSError, TypeError, RecursionError):
        raise CodingAgentError("agent_invalid_request", "request") from None


def _codex_argv(config: CodexAgentConfig, request: CodingAgentRequest) -> tuple[str, ...]:
    return (
        str(config.executable), "exec", "--ephemeral", "--ignore-user-config",
        "--sandbox", request.mode, "--json", "--skip-git-repo-check",
        "--output-schema", str(request.artifact_root / "schema.json"),
        "-o", str(request.artifact_root / "response.json"), "-m", config.model,
        "-c", "model_reasoning_effort=" + json.dumps(config.reasoning_effort),
        "-c", 'approval_policy="never"',
        *(("-c", 'windows.sandbox="elevated"') if os.name == "nt" else ()),
        "-C", str(request.cwd), "-",
    )


def _environment(config: CodexAgentConfig) -> dict[str, str]:
    result = {key: os.environ[key] for key in _ENVIRONMENT if os.environ.get(key)}
    auth = str(config.codex_home) if config.codex_home is not None else os.environ.get("CODEX_HOME")
    if auth:
        result["CODEX_HOME"] = auth
    result.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite JSON")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite JSON")
    return number


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON key")
        result[name] = value
    return result


def _parse_object(payload: bytes) -> dict[str, JsonValue]:
    value = json.loads(payload, parse_constant=_reject_constant, parse_float=_finite_float,
                       object_pairs_hook=_unique_object)
    return _JSON_OBJECT.validate_python(value, strict=True)


class _CapturedLog(_TailBuffer):
    """유한 prefix와 한 줄짜리 usage만 보존하며 pipe 전체는 계속 배출한다."""

    def __init__(self, *, events: bool) -> None:
        super().__init__()
        self.payload = bytearray()
        self.total = 0
        self.events = events
        self.line = bytearray()
        self.discard_line = False
        self.usage = AgentUsage()

    def append(self, payload: bytes) -> None:
        super().append(payload)
        self.total += len(payload)
        self.payload.extend(payload[:max(0, _LIMIT - len(self.payload))])
        if not self.events:
            return
        for index, part in enumerate(payload.split(b"\n")):
            if index:
                if not self.discard_line:
                    self._event(bytes(self.line))
                self.line.clear()
                self.discard_line = False
            if len(self.line) + len(part) > _EVENT_LIMIT:
                self.discard_line = True
                self.line.clear()
            elif not self.discard_line:
                self.line.extend(part)

    def _event(self, payload: bytes) -> None:
        try:
            item = _parse_object(payload)
            usage = item.get("usage")
            if item.get("type") != "turn.completed" or not isinstance(usage, dict):
                return
            values = {
                name: value if type(value := usage.get(name)) is int and value >= 0 else None
                for name in AgentUsage.__dataclass_fields__
            }
            self.usage = AgentUsage(**values)
        except (ValueError, UnicodeError, RecursionError):
            return


def _write(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _read_response(path: Path) -> bytes:
    if not _safe_path(path, directory=False):
        raise ValueError("unsafe response")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        payload = stream.read(_LIMIT + 1)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    def identity(info: os.stat_result) -> tuple[int, ...]:
        # Windows fstat/lstat의 ctime은 변경시각/생성시각으로 달라질 수 있다.
        # atime은 이 함수의 읽기 자체로 바뀌므로 내용 identity에 포함하지 않는다.
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
                info.st_mtime_ns)

    if (len(payload) > _LIMIT or identity(before) != identity(after)
            or identity(before) != identity(current)
            or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
        raise ValueError("response changed")
    return payload


def _evidence(root: Path) -> tuple[LedgerArtifactEvidence, ...]:
    result = []
    for name in ("prompt.txt", "schema.json", "stdout.log", "stderr.log", "response.json", "receipt.json", "input-access.json"):
        path = root / name
        try:
            payload = _read_response(path)
        except (OSError, ValueError):
            continue
        result.append(LedgerArtifactEvidence("coding-agent-" + name, path.as_uri(), hashlib.sha256(payload).hexdigest()))
    return tuple(result)


def _prepare_input_access(request: CodingAgentRequest) -> None:
    if os.name != "nt" or request.candidate_inputs is None:
        return
    if request.mode != "workspace-write":
        raise CodingAgentError("agent_invalid_request", "input_access")
    try:
        receipt = grant_input_read(request.cwd, request.candidate_inputs)
    except InputAccessError as error:
        _write(request.artifact_root / "input-access.json", _json_bytes(error.receipt))
        raise CodingAgentError("agent_input_access_failed", "input_access") from None
    except (KeyboardInterrupt, SystemExit) as interruption:
        receipt = getattr(interruption, "input_access_receipt", None)
        if receipt is not None:
            try:
                _write(request.artifact_root / "input-access.json", _json_bytes(receipt))
            except (OSError, ValueError):
                interruption.add_note("input_access_evidence_failed")
        raise
    _write(request.artifact_root / "input-access.json", _json_bytes(receipt))


def _run(config: CodexAgentConfig, request: CodingAgentRequest) -> CodingAgentReceipt:
    started = time.monotonic()
    root = request.artifact_root
    owner: _TreeOwner | None = None
    process: subprocess.Popen[bytes] | None = None
    readers: tuple[_PipeReader, _PipeReader] | None = None
    stdout, stderr = _CapturedLog(events=True), _CapturedLog(events=False)
    error: CodingAgentError | None = None
    interruption: BaseException | None = None
    response: dict[str, JsonValue] = {}
    created = False
    try:
        root.mkdir(mode=0o700)
        created = True
        _write(root / "prompt.txt", request.prompt.encode("utf-8"))
        _write(root / "schema.json", _json_bytes(request.output_schema))
        _prepare_input_access(request)
        worker = Path(__file__).with_name("_agent_worker.py").resolve(strict=True)
        owner = _new_tree_owner()
        process = subprocess.Popen(
            (sys.executable, "-I", str(worker), str(root / "prompt.txt"), *_codex_argv(config, request)),
            cwd=request.cwd, env=_environment(config), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
            start_new_session=os.name == "posix",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        owner.attach(process)
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        readers = (_PipeReader(process.stdout, buffer=stdout), _PipeReader(process.stderr, buffer=stderr))
        for reader in readers:
            reader.start()
        process.stdin.write(b"\x00")
        process.stdin.flush()
        process.stdin.close()
        while process.poll() is None:
            remaining = config.timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise CodingAgentError("agent_timeout", "wait")
            try:
                if (root / "response.json").lstat().st_size > _LIMIT:
                    raise CodingAgentError("agent_invalid_response", "response_size")
            except FileNotFoundError:
                pass
            try:
                process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                pass
        if owner.is_alive(process):
            raise CodingAgentError("agent_process_leaked", "descendants")
        if process.returncode != 0:
            raise CodingAgentError("agent_crash", "exit")
        try:
            response = _parse_object(_read_response(root / "response.json"))
        except (ValueError, OSError, UnicodeError, RecursionError):
            raise CodingAgentError("agent_invalid_response", "response") from None
    except CodingAgentError as caught:
        error = caught
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        error = CodingAgentError("agent_start_failed", "launch")
    except BaseException as caught:
        interruption = caught
        error = CodingAgentError("agent_interrupted", "cancelled")
    finally:
        cleanup = _cleanup_started(owner, process, readers)
        if not cleanup.cleanup_ok:
            error = CodingAgentError("agent_cleanup_failed", "cleanup")
        elif cleanup.process_leaked:
            error = CodingAgentError("agent_process_leaked", "cleanup")
        if not stdout.discard_line and stdout.line:
            stdout._event(bytes(stdout.line))
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        if created:
            try:
                _write(root / "stdout.log", bytes(stdout.payload))
                _write(root / "stderr.log", bytes(stderr.payload))
                _write(root / "receipt.json", _json_bytes({
                    "model": config.model, "reasoning_effort": config.reasoning_effort,
                    "mode": request.mode, "duration_ms": duration_ms,
                    "approval_policy": "never",
                    "windows_sandbox": "elevated" if os.name == "nt" else None,
                    "exit_code": process.returncode if process is not None else None,
                    "usage": asdict(stdout.usage), "cost_usd": None,
                    "stdout_truncated": stdout.total > _LIMIT,
                    "stderr_truncated": stderr.total > _LIMIT,
                    "error_code": error.code if error else None,
                }))
            except (OSError, ValueError):
                error = CodingAgentError("agent_artifact_failed", "evidence")
    artifacts = _evidence(root) if created else ()
    if interruption is not None:
        if error is not None and error.code != "agent_interrupted":
            interruption.add_note(error.code)
        raise interruption
    if error is not None:
        error.duration_ms = duration_ms
        error.artifacts = artifacts
        raise error from None
    return CodingAgentReceipt(response, artifacts, stdout.usage, duration_ms)
