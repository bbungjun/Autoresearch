"""준비된 로컬 실험 입력을 실제 agent·Controller 실행에 연결한다.

[파이프라인] Judge fixture 준비 이후, validation 반복과 단일 final 종료 구간이다.
[기능] typed 로컬 설정, 불변 run 입력·ledger 결속, 한 번의 feedback revision,
실제 coding/training adapter와 Controller 조립 및 재개 결과 보존을 제공한다.
[비책임] fixture 생성·모델 다운로드·sigma calibration·연구 기록 Judge/REPORT는 별도
준비/후속 단계다. candidate 코드로 채점하지 않고 운영 MLflow/GCP 게시도 하지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from autoresearch.feature_engineering.model_contract import FeatureContractError
from autoresearch.research_harness.controller import (
    ControllerRunRequest, ControllerRunResult, ResearchBudget, ResearchController,
)
from autoresearch.research_harness.feedback import ExperimentCard, FeedbackPayload
from autoresearch.research_harness.fixture_models import JudgeSnapshotHandoff
from autoresearch.research_harness.fixture_errors import StageCError
from autoresearch.research_harness.judge_decision import JudgeMetric
from autoresearch.research_harness.ledger import (
    CheckpointRecord, LedgerArtifactEvidence, TrialLedger, open_trial_ledger,
)
from autoresearch.research_harness.local_evaluation_fixture import (
    FixtureActionLogSource, _open_lock_matches, _prepare_descriptor_lock,
    _release_descriptor_lock, _safe_regular_file_identity,
)
from autoresearch.research_harness.prediction import HarnessPredictConfig


@dataclass(slots=True)
class LocalRuntimeError(Exception):
    """로컬 경로/설정 원문 없이 실행 실패 지점을 전달한다."""

    stage: str

    def __str__(self) -> str:
        return f"harness_runtime_failed: stage={self.stage}"


class RevisionPlanner:
    """한 초기 가설에 validation feedback revision을 한 번 허용한다."""

    def next_card(
        self, initial_card: ExperimentCard, feedback_history: tuple[FeedbackPayload, ...],
    ) -> ExperimentCard | None:
        if not feedback_history:
            return initial_card
        if len(feedback_history) > 1:
            return None
        return ExperimentCard(
            initial_card.card_id[:110] + "-revision-1", initial_card.hypothesis,
            initial_card.change, initial_card.falsification_condition,
        )


def bind_input_checkpoint(ledger: TrialLedger, artifact: LedgerArtifactEvidence) -> None:
    """Controller 앞에서 입력 manifest와 기존 ledger를 같은 run으로 결속한다."""
    state = ledger.read_state()
    if state.completed("run-inputs"):
        checkpoint = state.checkpoint("run-inputs")
        if (checkpoint.stage != "run-inputs" or checkpoint.trial_id != "run"
                or checkpoint.artifacts != (artifact,) or checkpoint.final_consumption is not None):
            raise LocalRuntimeError("run_inputs_checkpoint")
        return
    if state.last_sequence >= 0:
        raise LocalRuntimeError("run_inputs_checkpoint_missing")
    ledger.append(CheckpointRecord(
        "run-inputs", "run-inputs", "run", datetime.now(UTC), (artifact,), None,
    ))


# CLI dependency stays lazy for ordinary prediction imports and optional embedding installs.
def load_run_config(path: Path) -> HarnessRunConfig:
    """64 KiB 이하의 명시적 로컬 실행 설정을 읽는다. 상대 경로는 허용하지 않는다."""
    try:
        with path.open("rb") as stream:
            payload = stream.read(64 * 1024 + 1)
        if len(payload) > 64 * 1024:
            raise ValueError
        return HarnessRunConfig.model_validate_json(payload)
    except (OSError, ValueError, ValidationError):
        raise LocalRuntimeError("configuration") from None


# Import after the helpers so their contracts remain independent of CLI orchestration.
from autoresearch.research_harness.coding_agent import CodexAgentConfig  # noqa: E402


class HarnessRunConfig(BaseModel):
    """이미 준비된 fixture/model 및 저장 로그인으로 실행하는 Judge-only 설정."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)
    repository_root: Path
    workspace_parent: Path
    run_root: Path
    handoff: JudgeSnapshotHandoff
    fixture_descriptor_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    champion_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    initial_card: ExperimentCard
    max_trials: int = Field(default=2, ge=1, le=2)
    max_duration_seconds: float = Field(default=3600.0, gt=0, allow_inf_nan=False)
    screening_seed: int = Field(ge=0, le=2**32 - 1)
    confirmation_seeds: tuple[Annotated[int, Field(ge=0, le=2**32 - 1)], ...]
    baseline_sigmas: dict[str, Annotated[float, Field(ge=0, allow_inf_nan=False)]]
    prediction: HarnessPredictConfig
    agent: CodexAgentConfig
    prediction_timeout_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_run(self) -> HarnessRunConfig:
        paths = (self.repository_root, self.workspace_parent, self.run_root,
                 self.handoff.snapshot_root, self.prediction.embedding.model_dir,
                 self.prediction.embedding.cache_dir)
        if any(not path.is_absolute() or "\0" in str(path) for path in paths):
            raise ValueError("absolute local paths required")
        if (len(self.confirmation_seeds) != 5 or len(set(self.confirmation_seeds)) != 5
                or self.screening_seed in self.confirmation_seeds
                or set(self.baseline_sigmas) != {metric.value for metric in JudgeMetric}):
            raise ValueError("invalid calibration contract")
        return self


@contextmanager
def _run_lock(root: Path) -> Iterator[None]:
    """같은 run에서 두 Controller가 동시에 agent를 실행하지 못하도록 한다."""
    path = root / ".harness-run.lock"
    identity = _prepare_descriptor_lock(path)
    with path.open("r+b") as stream:
        if not _open_lock_matches(path, stream.fileno(), identity):
            raise LocalRuntimeError("run_lock")
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LocalRuntimeError("run_already_active") from None
        try:
            yield
        except StageCError as error:
            # Frozen StageC exceptions cannot receive contextlib's traceback assignment.
            # Translate at this orchestration seam, preserving the safe stage.
            raise LocalRuntimeError(error.stage) from None
        except FeatureContractError as error:
            raise LocalRuntimeError(error.reason) from None
        finally:
            _release_descriptor_lock(stream)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _runtime_identity(config: HarnessRunConfig) -> str:
    """모델 파일과 trusted Harness 코드·실행 라이브러리까지 재개 계약에 넣는다."""
    from autoresearch.research_harness.local_embedding import _model_files

    libraries = {}
    for name in ("numpy", "torch", "sentence-transformers", "transformers", "tokenizers",
                 "safetensors", "lightgbm", "scikit-learn", "pandas", "pyarrow"):
        try:
            libraries[name] = version(name)
        except PackageNotFoundError:
            libraries[name] = None
    code_root = Path(__file__).parent
    code = {path.name: sha256(path.read_bytes()).hexdigest() for path in sorted(code_root.glob("*.py"))}
    return _canonical({
        "config": config.model_dump(mode="json", exclude_unset=True),
        "resolved_config": config.model_dump(mode="json"),
        "model_files": _model_files(config.prediction.embedding.model_dir),
        "libraries": libraries, "trusted_harness_files": code,
        "agent_executable_sha256": sha256(config.agent.executable.read_bytes()).hexdigest(),
    })


def _validate_locations(config: HarnessRunConfig) -> Path:
    snapshot = config.handoff.snapshot_root
    if (len(snapshot.parents) < 3 or snapshot.parent.name != "by-hash"
            or snapshot.parent.parent.name != "evaluation-snapshots"):
        raise LocalRuntimeError("snapshot_location")
    fixture_root = config.handoff.snapshot_root.parents[2]
    roots = (config.repository_root, config.workspace_parent, config.run_root, fixture_root)
    if any(path.resolve() != path.absolute() for path in roots):
        raise LocalRuntimeError("aliased_root")
    for left, right in ((config.run_root, fixture_root), (config.workspace_parent, fixture_root),
                        (config.workspace_parent, config.run_root),
                        (config.workspace_parent, config.repository_root)):
        if left.is_relative_to(right) or right.is_relative_to(left):
            raise LocalRuntimeError("overlapping_roots")
    if not all(path.is_dir() for path in roots):
        raise LocalRuntimeError("root_missing")
    registry = fixture_root / "final-holdout-consumed"
    if not registry.is_dir() or registry.resolve() != registry.absolute():
        raise LocalRuntimeError("registry_missing")
    return fixture_root


def run_local_research(config: HarnessRunConfig) -> ControllerRunResult:
    """불변 입력을 복구하고 실제 agent·독립 학습·trusted 판정을 끝까지 실행한다."""
    from autoresearch.research_harness.candidate_data_view import (
        prepare_candidate_metadata, prepare_final_candidate_metadata,
    )
    from autoresearch.research_harness.coding_agent import CodexCodingAgent
    from autoresearch.research_harness.domain import YouTubeCTRDomain
    from autoresearch.research_harness.local_trial_runner import LocalResearchTrialRunner
    from autoresearch.research_harness.run_inputs import (
        RunInputContract, freeze_run_inputs, load_run_inputs,
    )

    fixture_root = _validate_locations(config)
    source = FixtureActionLogSource(fixture_root, config.fixture_descriptor_sha256)
    with _run_lock(config.run_root):
        from autoresearch.research_harness.local_evaluation_fixture import _validated_judge_snapshot
        handoff, _ = _validated_judge_snapshot(
            config.handoff.snapshot_root, expected_fingerprint=str(config.handoff.snapshot_fingerprint),
        )
        if handoff != config.handoff:
            raise LocalRuntimeError("snapshot_identity")
        ledger = open_trial_ledger(config.run_root / "experiment-ledger.jsonl")
        state = ledger.read_state()
        contract = RunInputContract(
            initial_card=config.initial_card,
            budget=ResearchBudget(config.max_trials, config.max_duration_seconds),
            baseline_sha=config.baseline_sha, champion_sha=config.champion_sha,
            handoff=config.handoff, judge_state_root=fixture_root,
            baseline_sigmas=tuple(sorted(config.baseline_sigmas.items())),
            screening_seed=config.screening_seed, confirmation_seeds=config.confirmation_seeds,
            runtime_json=_runtime_identity(config),
        )
        if os.path.lexists(config.run_root / "run-inputs"):
            frozen = load_run_inputs(config.run_root, expected_contract=contract)
        else:
            if state.last_sequence >= 0:
                raise LocalRuntimeError("run_inputs_missing")
            frozen = freeze_run_inputs(
                config.run_root, contract=contract,
                validation_metadata=prepare_candidate_metadata(config.handoff, source=source),
                final_metadata=prepare_final_candidate_metadata(config.handoff, source=source),
            )
        bind_input_checkpoint(ledger, frozen.artifact)
        runner = LocalResearchTrialRunner(
            repository_root=config.repository_root, workspace_parent=config.workspace_parent,
            artifacts_root=config.run_root / "attempts", source=source, handoff=config.handoff,
            validation_metadata=frozen.validation_metadata, final_metadata=frozen.final_metadata,
            prediction_config=config.prediction.model_dump(mode="json", exclude_unset=True),
            coding_agent=CodexCodingAgent(config.agent),
            predict_timeout_seconds=config.prediction_timeout_seconds,
        )
        result = ResearchController(YouTubeCTRDomain(), RevisionPlanner(), runner).run(
            ControllerRunRequest(
                config.initial_card, contract.budget, config.baseline_sha, config.champion_sha,
                config.handoff, fixture_root, contract.baseline_sigmas, config.screening_seed,
                config.confirmation_seeds, ledger,
            ),
        )
        _preserve_result(config.run_root / "controller-result.json", result)
        return result


def _preserve_result(path: Path, result: ControllerRunResult) -> None:
    # Final evidence contains private paths. This artifact stays Judge-owned, never prompt input.
    payload = json.dumps(asdict(result), default=str, sort_keys=True, ensure_ascii=True,
                         separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n"
    if os.path.lexists(path):
        if _safe_regular_file_identity(path) is None or path.read_bytes() != payload:
            raise LocalRuntimeError("result_conflict")
        return
    # A complete ledger can regenerate a missing result after publication interruption.
    from tempfile import NamedTemporaryFile
    with NamedTemporaryFile(dir=path.parent, prefix=".result-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
