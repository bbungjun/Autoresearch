"""실제 coding agent와 독립 학습을 Controller의 trial 계약에 연결한다.

[파이프라인] 고정된 데이터·champion에서 가설 구현, seed별 재학습, trusted Judge 봉인·채점을
수행하여 자율 실험 Controller의 validation/final 판정 입력을 만든다.

[기능] 일회성 workspace, 로컬 candidate commit, append-only attempt 증거, paired 예측과
모델·receipt 보존을 조립한다. Agent의 개선 주장은 수치 판정과 분리한다.
Coding prepare에만 validation 입력 identity를 전달하며 실제 Windows Codex adapter가
입력 READ 접근을 준비한다. Host prediction과 final 실행에는 이 권한 요청을 전달하지 않는다.
Coding temp 회수는 candidate commit/patch 증거를 게시한 뒤 수행하고 실패 후보는 반환하지 않는다.
직전 실패 후보의 검증된 diff를 champion checkout에 복원하여 agent의 수정 출발점으로 제공한다.

[비책임] LLM 프로세스 실행은 coding_agent, 학습 프로세스 회수는 runner, 수치 판정은
domain, final 권한 발급·재개 정책은 Controller, immutable run 입력은 run_inputs가 소유한다.
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from applications.experiment_platform.executor.safety import contains_credential_value
from autoresearch.research_harness.coding_agent import (
    CandidateInputIdentity, CodingAgent, CodingAgentError, CodingAgentRequest,
)
from autoresearch.research_harness.consumption_registry import FinalConsumptionGrant
from autoresearch.research_harness.controller import (
    FinalPairRequest, PairedRunReceipt, PrepareCandidateRequest, PreparedCandidate,
    TrialExecutionError, ValidationPairRequest,
)
from autoresearch.research_harness.domain import ResearchDomain
from autoresearch.research_harness.evaluation_source import ActionLogSource
from autoresearch.research_harness.fixture_models import JudgeSnapshotHandoff, PreparedCandidateMetadata
from autoresearch.research_harness.judge import JudgeScoringResult
from autoresearch.research_harness.judge_errors import JudgeError
from autoresearch.research_harness.judge_decision import PairedJudgeResult
from autoresearch.research_harness.ledger import LedgerArtifactEvidence
from autoresearch.research_harness.runner import LocalRunner, LocalRunReceipt, LocalRunRequest, RunnerError
from autoresearch.research_harness.workspace import (
    CandidateWorkspace, CandidateWorkspaceRequest, WorkspaceError,
    open_candidate_workspace, open_final_candidate_workspace,
)


class AgentExperimentResponse(BaseModel):
    """설명과 자기 보고만 받으며 authoritative metric 필드를 갖지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    status: Literal["implemented", "no_change", "blocked"]
    experiment_summary: str = Field(min_length=1, max_length=4096)
    changes: list[str] = Field(max_length=100)
    tests: list[str] = Field(max_length=100)
    claimed_improvement: str | None = Field(max_length=4096)


class PredictionRunner(Protocol):
    """고정 CLI 실행을 단위 테스트에서 대체하는 기존 LocalRunner seam."""

    def run(self, request: LocalRunRequest) -> LocalRunReceipt: ...


class LocalResearchTrialRunner:
    """Agent·workspace·LocalRunner·trusted domain을 조립하는 구체적 trial adapter.

    Args:
        repository_root: 로컬 commit object를 보존할 원본 저장소.
        workspace_parent: 원본 저장소 밖의 disposable checkout 부모.
        artifacts_root: Candidate 밖의 Judge-owned attempt 증거 부모.
        source: 고정 fixture history source.
        handoff: 이 run의 불변 Judge snapshot identity.
        validation_metadata: 저장 bytes에서 복원한 validation bundle.
        final_metadata: 저장 bytes에서 복원한 final bundle.
        prediction_config: 호출자가 검증한 JSON; absolute embedding 경로와 명시적 training override.
        coding_agent: 구조화된 코드 변경 agent.
        predict_timeout_seconds: 각 독립 학습의 시간 상한.
        local_runner: 기본 LocalRunner를 대체할 테스트 seam.
    """

    def __init__(
        self, *, repository_root: Path, workspace_parent: Path, artifacts_root: Path,
        source: ActionLogSource, handoff: JudgeSnapshotHandoff,
        validation_metadata: PreparedCandidateMetadata, final_metadata: PreparedCandidateMetadata,
        prediction_config: dict[str, JsonValue], coding_agent: CodingAgent,
        predict_timeout_seconds: float, local_runner: PredictionRunner | None = None,
    ) -> None:
        if (isinstance(predict_timeout_seconds, bool)
                or not math.isfinite(predict_timeout_seconds) or predict_timeout_seconds <= 0):
            raise ValueError("invalid prediction timeout")
        self._repository = repository_root.resolve(strict=True)
        self._workspaces = workspace_parent.resolve()
        self._artifacts = artifacts_root.resolve()
        if (self._workspaces.is_relative_to(self._repository)
                or self._artifacts.is_relative_to(self._workspaces)
                or self._workspaces.is_relative_to(self._artifacts)):
            raise ValueError("invalid trial path separation")
        self._workspaces.mkdir(parents=True, exist_ok=True)
        self._artifacts.mkdir(parents=True, exist_ok=True)
        self._source = source
        self._handoff = handoff
        self._validation_metadata = validation_metadata
        self._final_metadata = final_metadata
        self._config_bytes = _json_bytes(prediction_config)
        self._agent = coding_agent
        self._timeout = predict_timeout_seconds
        self._runner = local_runner or LocalRunner()

    def prepare_candidate(self, request: PrepareCandidateRequest) -> PreparedCandidate:
        """Validation-only 가설을 구현하고 로컬 commit과 변경 증거를 보존한다."""
        started = time.monotonic()
        attempt = self._new_attempt("prepare", request.trial_id, None)
        evidence: list[LedgerArtifactEvidence] = []
        try:
            with open_candidate_workspace(
                self._workspace_request(request.champion_sha), source=self._source,
                metadata=self._validation_metadata,
            ) as workspace, ExitStack() as cleanup_stack:
                _restore_repair_candidate(workspace, request.repair_candidate_sha)
                if os.path.lexists(workspace.root / "harness_config.json"):
                    raise TrialExecutionError("prepare", "candidate_runtime_config")
                response = self._agent.run(CodingAgentRequest(
                    cwd=workspace.root, prompt=_prompt(request),
                    output_schema=AgentExperimentResponse.model_json_schema(),
                    artifact_root=attempt / "agent", mode="workspace-write",
                    candidate_inputs=CandidateInputIdentity(workspace.candidate_view_sha256, workspace.evaluation_id),
                    cleanup_stack=cleanup_stack,
                ))
                evidence.extend(response.artifacts)
                explanation = AgentExperimentResponse.model_validate(response.response)
                evidence.append(_write_json(attempt / "agent-explanation.json", explanation.model_dump(mode="json")))
                if explanation.status == "blocked":
                    raise TrialExecutionError("prepare", "agent_blocked")
                if _git(workspace.root, "rev-parse", "HEAD").decode().strip() != request.champion_sha:
                    raise TrialExecutionError("prepare", "candidate_head_changed")
                if os.path.lexists(workspace.root / "harness_config.json"):
                    raise TrialExecutionError("prepare", "candidate_runtime_config")
                workspace.inspect_changes()  # 검사 전에는 candidate bytes를 stage/commit하지 않는다.
                candidate_sha, fingerprint, paths, diff = _commit_candidate(workspace, attempt.name)
                evidence.append(_write_bytes(attempt / "candidate.patch", diff))
                evidence.append(_write_json(attempt / "candidate.json", {
                    "trial_id": request.trial_id, "card": json.loads(request.card.canonical_summary()),
                    "base_sha": request.champion_sha, "candidate_sha": candidate_sha,
                    "repair_candidate_sha": request.repair_candidate_sha,
                    "diff_fingerprint": fingerprint, "changed_paths": list(paths),
                    "agent_duration_ms": response.duration_ms,
                    "duration_ms": _duration(started), "cost_usd": None,
                    "usage": {name: getattr(response.usage, name) for name in (
                        "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens",
                    )},
                }))
            for name in ("temp-cleanup.json", "temp-cleanup.stdout.log", "temp-cleanup.stderr.log"):
                cleanup_artifact = attempt / "agent" / name
                if cleanup_artifact.exists():
                    evidence.append(_file_evidence(cleanup_artifact))
            return PreparedCandidate(request.trial_id, request.card, request.champion_sha,
                                     candidate_sha, fingerprint, tuple(evidence))
        except (CodingAgentError, WorkspaceError, RunnerError, JudgeError,
                OSError, subprocess.SubprocessError, ValidationError, TrialExecutionError) as error:
            failure = _trial_error(error, "prepare", started)
            _record_failure(attempt, failure)
            raise failure from None
        except (KeyboardInterrupt, SystemExit):
            _record_failure(attempt, TrialExecutionError("prepare", "trial_interrupted", _duration(started)))
            raise

    def run_validation(self, request: ValidationPairRequest, domain: ResearchDomain) -> PairedRunReceipt:
        """현재 champion과 candidate를 동일 seed로 각각 새로 학습·채점한다."""
        return self._pair(request.candidate.base_sha, request.candidate.candidate_sha,
                          request.handoff, request.seed, domain, request.candidate.trial_id, None)

    def run_final(self, request: FinalPairRequest, domain: ResearchDomain) -> PairedRunReceipt:
        """기존 grant 아래 초기 baseline과 최종 champion의 paired 결과를 만든다."""
        if not isinstance(request.grant, FinalConsumptionGrant) or not request.grant._authorizes(request.handoff):
            raise TrialExecutionError("final", "final_grant_invalid")
        return self._pair(request.baseline_sha, request.candidate_sha, request.handoff,
                          request.seed, domain, "final-holdout", request.grant)

    def _pair(
        self, baseline_sha: str, candidate_sha: str, handoff: JudgeSnapshotHandoff,
        seed: int, domain: ResearchDomain, trial_id: str, grant: FinalConsumptionGrant | None,
    ) -> PairedRunReceipt:
        if handoff != self._handoff:
            raise TrialExecutionError("pair", "handoff_mismatch")
        started = time.monotonic()
        attempt = self._new_attempt("final" if grant is not None else "validation", trial_id, seed)
        evidence: list[LedgerArtifactEvidence] = []
        scores: list[JudgeScoringResult] = []
        stdout, stderr = "", ""
        try:
            for role, code_sha in (("baseline", baseline_sha), ("candidate", candidate_sha)):
                output = attempt / role
                output.mkdir()
                score, artifacts, receipt = self._predict(code_sha, seed, domain, output, grant)
                scores.append(score)
                evidence.extend(artifacts)
                stdout = _safe_tail(stdout + receipt.stdout_tail)
                stderr = _safe_tail(stderr + receipt.stderr_tail)
            evidence.append(_write_json(attempt / "pair.json", {
                "baseline_sha": baseline_sha, "candidate_sha": candidate_sha, "seed": seed,
                "duration_ms": _duration(started), "baseline": asdict(scores[0]),
                "candidate": asdict(scores[1]),
            }))
            return PairedRunReceipt(PairedJudgeResult(seed, scores[0], scores[1]),
                                    _duration(started), tuple(evidence), stdout, stderr)
        except (WorkspaceError, RunnerError, JudgeError, OSError,
                subprocess.SubprocessError, TrialExecutionError) as error:
            failure = _trial_error(error, "pair", started)
            _record_failure(attempt, failure)
            raise failure from None
        except (KeyboardInterrupt, SystemExit):
            _record_failure(attempt, TrialExecutionError("pair", "trial_interrupted", _duration(started)))
            raise

    def _predict(
        self, code_sha: str, seed: int, domain: ResearchDomain, output: Path,
        grant: FinalConsumptionGrant | None,
    ) -> tuple[JudgeScoringResult, tuple[LedgerArtifactEvidence, ...], LocalRunReceipt]:
        started = time.monotonic()
        request = self._workspace_request(code_sha)
        if grant is None:
            manager = open_candidate_workspace(request, source=self._source, metadata=self._validation_metadata)
        else:
            manager = open_final_candidate_workspace(request, source=self._source,
                                                     metadata=self._final_metadata, grant=grant)
        artifacts: list[LedgerArtifactEvidence] = []
        with manager as workspace:
            _write_bytes(workspace.root / "harness_config.json", self._config_bytes)
            try:
                receipt = self._runner.run(LocalRunRequest(workspace.process, seed, self._timeout))
            except RunnerError as error:
                _copy_outputs(workspace.process.predictions, output, require_all=False)
                _write_json(output / "execution.json", {
                    "code_sha": code_sha, "seed": seed, "duration_ms": error.duration_ms,
                    "reason_code": str(error.code), "stdout_tail": _safe_tail(error.stdout_tail),
                    "stderr_tail": _safe_tail(error.stderr_tail),
                })
                raise
            except (KeyboardInterrupt, SystemExit) as interruption:
                # LocalRunner가 process tree를 회수한 뒤, worktree 삭제 전에 부분 산출물을 보존한다.
                try:
                    _copy_outputs(workspace.process.predictions, output, require_all=False)
                except (OSError, TrialExecutionError):
                    interruption.add_note("interrupted_artifact_copy_failed")
                try:
                    _write_json(output / "execution.json", {
                        "code_sha": code_sha, "seed": seed, "duration_ms": _duration(started),
                        "reason_code": "trial_interrupted", "stdout_tail": "", "stderr_tail": "",
                    })
                except OSError:
                    interruption.add_note("interrupted_execution_record_failed")
                raise
            if receipt.predictions != workspace.process.predictions:
                raise TrialExecutionError("prediction", "prediction_path_mismatch")
            artifacts.extend(_copy_outputs(receipt.predictions, output, require_all=True))
            artifacts.append(_write_json(output / "execution.json", {
                "code_sha": code_sha, "seed": seed, "duration_ms": receipt.duration_ms,
                "candidate_view_sha256": workspace.candidate_view_sha256,
                "exit_code": receipt.exit_code, "stdout_tail": _safe_tail(receipt.stdout_tail),
                "stderr_tail": _safe_tail(receipt.stderr_tail),
            }))
        # Workspace 회수 뒤 Judge-owned 사본만 trusted domain에 전달한다.
        sealed_path = output / "sealed.csv"
        sealed = domain.validate_candidate(output / "predictions.csv", sealed_path)
        result = domain.evaluate(self._handoff, sealed, final_grant=grant)
        artifacts.append(_file_evidence(sealed_path))
        return result, tuple(artifacts), receipt

    def _workspace_request(self, code_sha: str) -> CandidateWorkspaceRequest:
        return CandidateWorkspaceRequest(self._repository, code_sha,
                                         self._workspaces / uuid4().hex, self._handoff)

    def _new_attempt(self, stage: str, trial_id: str, seed: int | None) -> Path:
        attempt = self._artifacts / uuid4().hex
        try:
            attempt.mkdir()
            _write_json(attempt / "attempt.json", {"stage": stage, "trial_id": trial_id, "seed": seed,
                                                  "started_at_unix_ns": time.time_ns()})
        except OSError:
            raise TrialExecutionError("attempt", "trial_artifact_failed") from None
        return attempt


def _prompt(request: PrepareCandidateRequest) -> str:
    return (
        "Implement one experiment in this disposable repository checkout. "
        "This is a local ML candidate experiment already authorized under issue #52. "
        "Harness owns issue/branch/commit/PR workflow; do not start GitHub workflow or seek approval. "
        "Use the hypothesis and validation feedback below. Change code only; do not commit, push, "
        "download data/models, access remote services, create cloud resources, or write harness_config.json. "
        "Do not change the scoring contract or attempt to access Judge-owned data. "
        "harness_in contains label-free inputs and harness_out is for disposable outputs. "
        "Do not claim measured improvement unless provided by validation feedback. "
        "Report status implemented, no_change, or blocked accurately. "
        "If execution policy blocks a needed action, do not try alternate shells or guessed patch paths "
        "to bypass it; stop and return blocked with the reason. "
        "Return the required JSON explanation.\n"
        "When repair_candidate_sha is non-null, its failed code changes are already restored in this checkout. "
        "Repair those changes using the failure feedback. HEAD and the paired comparison baseline remain "
        "champion_sha; the failed candidate is not a promoted baseline.\n"
        + json.dumps({"card": json.loads(request.card.canonical_summary()),
                      "champion_sha": request.champion_sha,
                      "repair_candidate_sha": request.repair_candidate_sha,
                      "validation_feedback": [asdict(item) for item in request.feedback_history]},
                     ensure_ascii=True, sort_keys=True)
    )


def _restore_repair_candidate(workspace: CandidateWorkspace, candidate_sha: str | None) -> None:
    """Restore a direct child diff without changing HEAD or the comparison baseline."""
    if candidate_sha is None:
        return
    if not isinstance(candidate_sha, str) or re.fullmatch(r"[0-9a-f]{40}", candidate_sha) is None:
        raise TrialExecutionError("prepare", "repair_candidate_invalid")
    try:
        identity = _git(workspace.root, "rev-parse", "--verify", candidate_sha + "^{commit}").decode().strip()
        parents = _git(workspace.root, "show", "-s", "--format=%P", candidate_sha).decode().split()
        if identity != candidate_sha or (candidate_sha != workspace.base_sha and parents != [workspace.base_sha]):
            raise TrialExecutionError("prepare", "repair_candidate_lineage")
        diff = _git(workspace.root, "diff", "--binary", "--full-index", "--no-renames",
                    "--no-ext-diff", "--no-textconv", workspace.base_sha, candidate_sha, "--")
        if diff:
            _git(workspace.root, "apply", "--check", "--binary", "-", input_bytes=diff)
            _git(workspace.root, "apply", "--binary", "-", input_bytes=diff)
    except subprocess.SubprocessError:
        raise TrialExecutionError("prepare", "repair_candidate_invalid") from None
    workspace.inspect_changes()


def _commit_candidate(workspace: CandidateWorkspace, attempt_id: str) -> tuple[str, str, tuple[str, ...], bytes]:
    # Normal add honors ignore rules. Harness inputs/outputs and runtime config never enter the commit.
    indexed = _git(workspace.root, "diff", "--cached", "--name-only", "-z", workspace.base_sha)
    if any(path.split(b"/", 1)[0] in {b"harness_in", b"harness_out", b".harness-in.lock", b"harness_config.json"}
           for path in indexed.split(b"\0") if path):
        raise TrialExecutionError("prepare", "candidate_harness_artifact_staged")
    _git(workspace.root, "add", "-A", "--", ".", ":(exclude)harness_in", ":(exclude)harness_out",
         ":(exclude).harness-in.lock", ":(exclude)harness_config.json")
    workspace.inspect_changes()
    diff = _git(workspace.root, "diff", "--cached", "--binary", "--full-index", "--no-renames",
                "--no-ext-diff", "--no-textconv", workspace.base_sha)
    fingerprint = "sha256:" + sha256(diff).hexdigest()
    if not diff:
        return workspace.base_sha, fingerprint, (), diff
    paths = tuple(path.decode("utf-8") for path in _git(
        workspace.root, "diff", "--cached", "--name-only", "--no-renames", "-z", workspace.base_sha,
    ).split(b"\0") if path)
    tree = _git(workspace.root, "write-tree").decode().strip()
    candidate = _git(workspace.root, "-c", "user.name=Research Harness", "-c",
                     "user.email=harness@localhost", "commit-tree", tree, "-p", workspace.base_sha,
                     "-m", "Implement local research candidate").decode().strip()
    _git(workspace.root, "update-ref", "refs/harness/candidates/" + attempt_id, candidate, "0" * 40)
    return candidate, fingerprint, paths, diff


def _git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          input=input_bytes, timeout=60).stdout


def _copy_outputs(predictions: Path, destination: Path, *, require_all: bool) -> tuple[LedgerArtifactEvidence, ...]:
    artifacts = []
    for source in (predictions, predictions.with_suffix(".model.txt"), predictions.with_suffix(".training.json")):
        if not os.path.lexists(source):
            if require_all:
                raise TrialExecutionError("artifact", "prediction_sidecar_missing")
            continue
        before = source.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or getattr(before, "st_file_attributes", 0) & 0x400
                or source.resolve(strict=True) != source.absolute()):
            raise TrialExecutionError("artifact", "prediction_artifact_invalid")
        target = destination / source.name
        with source.open("rb") as reader, target.open("xb") as writer:
            opened = os.fstat(reader.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise TrialExecutionError("artifact", "prediction_artifact_changed")
            while chunk := reader.read(1024 * 1024):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
            after = os.fstat(reader.fileno())
            if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise TrialExecutionError("artifact", "prediction_artifact_changed")
        artifacts.append(_file_evidence(target))
    return tuple(artifacts)


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _write_json(path: Path, payload: object) -> LedgerArtifactEvidence:
    return _write_bytes(path, _json_bytes(payload))


def _write_bytes(path: Path, payload: bytes) -> LedgerArtifactEvidence:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return LedgerArtifactEvidence(_artifact_name(path), path.as_uri(), sha256(payload).hexdigest())


def _file_evidence(path: Path) -> LedgerArtifactEvidence:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return LedgerArtifactEvidence(_artifact_name(path), path.as_uri(), digest.hexdigest())


def _artifact_name(path: Path) -> str:
    # Controller가 confirmation의 여러 seed receipt를 하나로 합쳐도 이름이 충돌하지 않는다.
    return path.name + ":" + sha256(path.as_uri().encode()).hexdigest()[:20]


def _safe_tail(value: str) -> str:
    return "[credential-like output redacted]" if contains_credential_value(value) else value[-65536:]


def _duration(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _trial_error(error: Exception, stage: str, started: float) -> TrialExecutionError:
    if isinstance(error, TrialExecutionError):
        return TrialExecutionError(error.stage, error.reason_code, _duration(started),
                                   _safe_tail(error.stdout_tail), _safe_tail(error.stderr_tail))
    code = str(error.code) if isinstance(error, (CodingAgentError, WorkspaceError, RunnerError, JudgeError)) else "trial_adapter_failed"
    return TrialExecutionError(stage, code, _duration(started),
                               _safe_tail(getattr(error, "stdout_tail", "")),
                               _safe_tail(getattr(error, "stderr_tail", "")))


def _record_failure(attempt: Path, failure: TrialExecutionError) -> None:
    try:
        _write_json(attempt / "failure.json", {
            "stage": failure.stage, "reason_code": failure.reason_code,
            "duration_ms": failure.duration_ms, "stdout_tail": failure.stdout_tail,
            "stderr_tail": failure.stderr_tail,
        })
    except OSError:
        failure.add_note("attempt_failure_record_failed")
