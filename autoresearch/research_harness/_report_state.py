"""Controller 종료와 연구 REPORT 사이의 내부 파일·결속 경계.

[파이프라인] 수치 판정 종료 뒤, 기록 Judge 호출 전에 durable 종료 상태를 검증한다.
[기능] bounded regular-file 읽기, write-once 게시, report 잠금과 input/ledger/result
결속을 제공한다. 종료 파일 복구는 수행하지만 Controller나 final claim은 실행하지 않는다.
[비책임] 실험 실행·판정은 controller, 기록 투영·advisory 검토는 report가 소유한다.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import TypeAdapter

from autoresearch.research_harness._filesystem import sync_directory
from autoresearch.research_harness.coding_agent import _parse_object
from autoresearch.research_harness.consumption_registry import ConsumptionRegistryErrorCode
from autoresearch.research_harness.controller import (
    ControllerConclusion, ControllerRunResult, ControllerTerminalReasonCode,
    _feedback_from_record, _is_valid_validation_candidate, _result_from_final,
)
from autoresearch.research_harness.feedback import ExperimentCard, FeedbackPayload
from autoresearch.research_harness.fixture_errors import StageCError
from autoresearch.research_harness.ledger import LedgerError, TrialLedgerState, open_trial_ledger
from autoresearch.research_harness.local_evaluation_fixture import (
    _open_lock_matches, _prepare_descriptor_lock, _release_descriptor_lock,
    _resolved_without_link, _safe_regular_file_identity,
)
from autoresearch.research_harness.run_inputs import FrozenRunInputs, RunInputContract, load_run_inputs


_MAX_JSON = 16 * 1024 * 1024
_RESULT = TypeAdapter(ControllerRunResult)


@dataclass(slots=True)
class ReportError(Exception):
    """Private 경로·원문을 포함하지 않는 기록 게시 실패."""

    stage: str

    def __str__(self) -> str:
        return f"research_report_failed: stage={self.stage}"


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, default=str, ensure_ascii=True, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")


def read_file(path: Path, *, limit: int = _MAX_JSON) -> bytes:
    """Alias·읽는 중 교체·과대 파일은 거부한다."""
    identity = _safe_regular_file_identity(path)
    if identity is None:
        raise ReportError("unsafe_file")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != identity or before.st_size > limit:
            raise ReportError("file_identity")
        payload = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(payload) > limit or _safe_regular_file_identity(path) != identity
            or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
        raise ReportError("file_changed")
    return payload


def read_json(path: Path) -> dict:
    return _parse_object(read_file(path))


def file_digest(path: Path) -> str:
    """모델·prediction bytes는 메모리에 모으지 않고 identity 대조와 함께 해시한다."""
    identity = _safe_regular_file_identity(path)
    if identity is None:
        raise ReportError("unsafe_file")
    digest = sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != identity:
            raise ReportError("file_identity")
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (_safe_regular_file_identity(path) != identity
            or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
        raise ReportError("file_changed")
    return digest.hexdigest()


def publish_bytes(path: Path, payload: bytes) -> None:
    """완전한 임시 파일을 원자적으로 선점 게시하며 기존 bytes는 바꾸지 않는다."""
    if os.path.lexists(path):
        if read_file(path) != payload:
            raise ReportError("publication_conflict")
        sync_directory(path.parent)
        return
    if not _resolved_without_link(path.parent):
        raise ReportError("publication_parent")
    with NamedTemporaryFile(dir=path.parent, prefix=".report-staging-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            if read_file(path) != payload:
                raise ReportError("publication_conflict") from None
    finally:
        temporary.unlink()
    sync_directory(path.parent)


@contextmanager
def report_lock(root: Path) -> Iterator[None]:
    """동일 run의 기록 게시와 단일 Judge 호출을 직렬화한다."""
    if not root.is_absolute() or not _resolved_without_link(root):
        raise ReportError("run_root")
    lock_path = root / ".research-report.lock"
    identity = _prepare_descriptor_lock(lock_path)
    with lock_path.open("r+b") as stream:
        if not _open_lock_matches(lock_path, stream.fileno(), identity):
            raise ReportError("report_lock")
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ReportError("report_already_active") from None
        try:
            yield
        except StageCError:
            raise ReportError("run_inputs") from None
        finally:
            _release_descriptor_lock(stream)


def terminal_context(root: Path, contract: RunInputContract) -> tuple[FrozenRunInputs, TrialLedgerState, str]:
    """입력 manifest checkpoint 및 ledger bytes를 검증해 읽는다."""
    frozen = load_run_inputs(root, expected_contract=contract)
    path = root / "experiment-ledger.jsonl"
    before = read_file(path)
    state = open_trial_ledger(path).read_state()
    if read_file(path) != before or state.recovered_trailing_bytes:
        raise ReportError("terminal_ledger_changed")
    checkpoint = state.checkpoint("run-inputs")
    if (checkpoint.stage != "run-inputs" or checkpoint.trial_id != "run"
            or checkpoint.artifacts != (frozen.artifact,) or checkpoint.final_consumption is not None):
        raise ReportError("terminal_input_checkpoint")
    return frozen, state, sha256(before).hexdigest()


def validate_terminal(result: ControllerRunResult, contract: RunInputContract, state: TrialLedgerState) -> None:
    """기존 수치 판정을 재계산하지 않고 결과와 durable 이력을 의미 대조한다."""
    champion = contract.champion_sha
    lineage = (contract.baseline_sha,) if champion == contract.baseline_sha else (contract.baseline_sha, champion)
    feedback: list[FeedbackPayload] = []
    final = []
    for record in state.trials:
        suffix, stage = ("validation-recorded", "validation_recorded") if record.split == "validation" else ("final-recorded", "final_recorded")
        checkpoint = state.checkpoint(f"{record.trial_id}:{suffix}")
        if (checkpoint.stage != stage or checkpoint.trial_id != record.trial_id
                or checkpoint.artifacts != record.artifacts or checkpoint.final_consumption != record.final_consumption):
            raise ReportError("terminal_trial_checkpoint")
        if record.split == "final_holdout":
            final.append(record)
            continue
        if (final or record.trial_id != f"trial-{len(feedback) + 1:04d}"
                or record.base_sha != champion or record.seed != contract.screening_seed
                or record.evaluation_id != contract.handoff.validation_id
                or record.experiment_summary is None):
            raise ReportError("terminal_validation")
        card = ExperimentCard.from_summary(record.experiment_summary)
        if not feedback and card != contract.initial_card:
            raise ReportError("terminal_card")
        feedback.append(_feedback_from_record(contract.initial_card, card, record, feedback))
        if record.decision == "promote":
            if record.candidate_sha is None:
                raise ReportError("terminal_champion")
            champion = record.candidate_sha
            lineage = (*lineage, champion)
        if record.champion_lineage != lineage:
            raise ReportError("terminal_lineage")
    if (len(feedback) > contract.budget.max_trials or result.validation_trials != len(feedback)
            or result.feedback_history != tuple(feedback) or result.champion_sha != champion):
        raise ReportError("terminal_result_history")
    if final:
        if len(final) != 1:
            raise ReportError("terminal_final_count")
        record = final[0]
        if (record.trial_id != "final-holdout" or record.base_sha != contract.baseline_sha
                or record.evaluation_id != contract.handoff.final_holdout_id
                or record.seed != contract.confirmation_seeds[0] or record.champion_lineage != lineage
                or (record.candidate_sha is None and champion != contract.baseline_sha)
                or _result_from_final(record, champion, feedback) != result):
            raise ReportError("terminal_final_result")
    elif (result.conclusion is not ControllerConclusion.INCONCLUSIVE or result.final_decision is not None
          or result.final_consumption is not None
          or result.final_reason_code not in {
              *(code.value for code in ConsumptionRegistryErrorCode),
              *(code.value for code in ControllerTerminalReasonCode),
          }
          or (
              result.final_reason_code
              == ControllerTerminalReasonCode.NO_VALID_VALIDATION_CANDIDATE.value
              and any(_is_valid_validation_candidate(record) for record in state.trials)
          )):
        raise ReportError("terminal_without_final")


def _parse_result(value: dict) -> ControllerRunResult:
    payload = json_bytes(value)
    result = _RESULT.validate_json(payload, strict=True)
    if json.loads(json_bytes(asdict(result))) != value:
        raise ReportError("terminal_result_schema")
    return result


def _load_terminal(root: Path, contract: RunInputContract) -> ControllerRunResult | None:
    binding_path, result_path = root / "controller-result-binding.json", root / "controller-result.json"
    if not os.path.lexists(binding_path):
        if os.path.lexists(result_path) or any(root.glob("research-*")):
            raise ReportError("terminal_binding_missing")
        return None
    binding = read_json(binding_path)
    if set(binding) != {"version", "input_sha256", "ledger_sha256", "last_sequence", "result_sha256", "result"}:
        raise ReportError("terminal_binding_schema")
    frozen, state, ledger_digest = terminal_context(root, contract)
    result = _parse_result(binding["result"])
    payload = json_bytes(asdict(result))
    if (binding["version"] != "research-terminal-v1" or binding["input_sha256"] != frozen.manifest_sha256
            or binding["ledger_sha256"] != ledger_digest or type(binding["last_sequence"]) is not int
            or binding["last_sequence"] != state.last_sequence
            or binding["result_sha256"] != sha256(payload).hexdigest()):
        raise ReportError("terminal_binding_conflict")
    validate_terminal(result, contract, state)
    publish_bytes(result_path, payload)
    return result


def load_terminal_result(root: Path, *, contract: RunInputContract) -> ControllerRunResult | None:
    """봉인된 종료 결과만 복구한다. Controller나 소비 registry를 호출하지 않는다."""
    try:
        with report_lock(root):
            return _load_terminal(root, contract)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, LedgerError, StageCError):
        raise ReportError("terminal_load") from None


def seal_terminal_result(root: Path, *, contract: RunInputContract, result: ControllerRunResult) -> None:
    """입력/ledger/result를 먼저 결속한 뒤 결과 파일을 게시한다."""
    try:
        with report_lock(root):
            existing = _load_terminal(root, contract)
            if existing is not None:
                if existing != result:
                    raise ReportError("terminal_result_conflict")
                return
            frozen, state, digest = terminal_context(root, contract)
            result = _parse_result(json.loads(json_bytes(asdict(result))))
            validate_terminal(result, contract, state)
            payload = json_bytes(asdict(result))
            publish_bytes(root / "controller-result-binding.json", json_bytes({
                "version": "research-terminal-v1", "input_sha256": frozen.manifest_sha256,
                "ledger_sha256": digest, "last_sequence": state.last_sequence,
                "result_sha256": sha256(payload).hexdigest(), "result": asdict(result),
            }))
            publish_bytes(root / "controller-result.json", payload)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, LedgerError, StageCError):
        raise ReportError("terminal_seal") from None
