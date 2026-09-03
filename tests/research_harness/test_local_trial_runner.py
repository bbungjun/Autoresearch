"""실제 workspace/commit과 trusted paired 실행 adapter의 경계를 검증한다."""

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import os
import re
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest

from autoresearch.research_harness.controller import (
    FinalPairRequest, PrepareCandidateRequest, TrialExecutionError, ValidationPairRequest,
)
from autoresearch.research_harness.feedback import ExperimentCard
from autoresearch.research_harness.candidate_data_view import prepare_candidate_metadata
from autoresearch.research_harness.domain import YouTubeCTRDomain
from autoresearch.research_harness.local_trial_runner import LocalResearchTrialRunner
import autoresearch.research_harness.local_trial_runner as trial_module
from autoresearch.research_harness.runner import LocalRunReceipt, RunnerError, RunnerErrorCode
from tests.research_harness.test_final_candidate_data_view import final_case as final_case
from tests.research_harness.test_workspace import (
    _git, candidate_fixture as candidate_fixture, repository as repository,
)


CARD = ExperimentCard("learning-rate", "Lower rate may generalize", "Change rate", "No ranking gain")


class Agent:
    def __init__(self, behavior: str = "change") -> None:
        self.behavior = behavior
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        assert not (request.cwd / "harness_config.json").exists()
        if self.behavior == "change":
            (request.cwd / "README.md").write_text("candidate\n", encoding="utf-8")
            (request.cwd / "new.py").write_text("VALUE = 2\n", encoding="utf-8")
            (request.cwd / "cache.parquet").write_bytes(b"ignored generated data")
        elif self.behavior == "credential":
            (request.cwd / "new.py").write_text("AKIA" + "A" * 16, encoding="utf-8")
        elif self.behavior == "commit":
            (request.cwd / "README.md").write_text("agent commit\n", encoding="utf-8")
            _git(request.cwd, "add", "README.md")
            _git(request.cwd, "commit", "-m", "unauthorized agent commit")
        elif self.behavior == "runtime_config":
            (request.cwd / "harness_config.json").write_text("{}", encoding="utf-8")
        elif self.behavior == "stage_input":
            _git(request.cwd, "add", "-f", "harness_in/slate.parquet")
        elif self.behavior == "interrupt":
            raise KeyboardInterrupt
        elif self.behavior == "crlf":
            (request.cwd / "README.md").write_bytes(b"baseline\r\n")
        return SimpleNamespace(
            response={"status": "blocked" if self.behavior == "blocked" else "no_change" if self.behavior in {"none", "crlf"} else "implemented",
                      "experiment_summary": "Policy blocked" if self.behavior == "blocked" else "Changed code", "changes": [] if self.behavior == "blocked" else ["rate"],
                      "tests": ["not run"], "claimed_improvement": None},
            artifacts=(), duration_ms=5,
            usage=SimpleNamespace(input_tokens=10, cached_input_tokens=None,
                                  output_tokens=4, reasoning_output_tokens=None),
        )


class Predictor:
    def __init__(self, *, fail: bool = False, missing_sidecar: bool = False, alias_sidecar: bool = False, interrupt: type[BaseException] | None = None) -> None:
        self.requests = []
        self.fail = fail
        self.missing_sidecar = missing_sidecar
        self.alias_sidecar = alias_sidecar
        self.interrupt = interrupt

    def run(self, request):
        self.requests.append(request)
        process = request.process
        assert json.loads((process.cwd / "harness_config.json").read_text()) == {"embedding": {"device": "cpu"}}
        table = pq.read_table(process.slate).to_pylist()
        text = "evaluation_id,slate_id,video_id,score\n"
        text += "".join(f"{row['evaluation_id']},{row['slate_id']},{row['video_id']},0.5\n" for row in table)
        process.predictions.write_text(text, encoding="utf-8")
        process.predictions.with_suffix(".model.txt").write_text("native model", encoding="utf-8")
        if not self.missing_sidecar:
            process.predictions.with_suffix(".training.json").write_text('{"seed":' + str(request.seed) + '}', encoding="utf-8")
        if self.alias_sidecar:
            os.link(process.predictions.with_suffix(".model.txt"), process.predictions.parent / "alias.txt")
        if self.interrupt is not None:
            raise self.interrupt
        if self.fail:
            raise RunnerError(RunnerErrorCode.PREDICT_CRASH, "candidate_exit", duration_ms=17, stderr_tail="failed")
        return LocalRunReceipt(process.predictions, 0, 12, "trained", "")


def _adapter(repository, final_case, tmp_path, *, agent=None, predictor=None):
    fixture, source, metadata, _grant = final_case
    validation = prepare_candidate_metadata(fixture.judge, source=source)
    return LocalResearchTrialRunner(
        repository_root=repository[0], workspace_parent=tmp_path / "workspaces",
        artifacts_root=tmp_path / "attempts", source=source, handoff=fixture.judge,
        validation_metadata=validation, final_metadata=metadata,
        prediction_config={"embedding": {"device": "cpu"}},
        coding_agent=agent or Agent(), predict_timeout_seconds=10,
        local_runner=predictor or Predictor(),
    )


def _prepare(adapter, repository):
    return adapter.prepare_candidate(PrepareCandidateRequest("trial-0001", CARD, repository[1], ()))


def test_agent_commit_preserves_code_and_not_generated_inputs(repository, final_case, tmp_path: Path) -> None:
    agent = Agent()
    candidate = _prepare(_adapter(repository, final_case, tmp_path, agent=agent), repository)
    assert candidate.base_sha == repository[1]
    assert candidate.candidate_sha != repository[1]
    assert _git(repository[0], "rev-parse", "HEAD") == repository[1]
    assert _git(repository[0], "show", f"{candidate.candidate_sha}:new.py") == "VALUE = 2"
    paths = _git(repository[0], "ls-tree", "-r", "--name-only", candidate.candidate_sha).splitlines()
    assert not any(path.startswith("harness_") or path == "cache.parquet" for path in paths)
    assert _git(repository[0], "for-each-ref", "--format=%(objectname)", "refs/harness/candidates") == candidate.candidate_sha
    assert len(candidate.diff_fingerprint) == 71
    assert not list((tmp_path / "workspaces").iterdir())
    assert "final_holdout" not in agent.requests[0].prompt
    assert str(final_case[0].fixture_root) not in agent.requests[0].prompt
    for artifact in candidate.artifacts:
        from urllib.request import url2pathname
        path = Path(url2pathname(artifact.uri.removeprefix("file:")))
        assert sha256(path.read_bytes()).hexdigest() == artifact.sha256
    receipt = json.loads(next((tmp_path / "attempts").glob("*/candidate.json")).read_text())
    assert receipt["changed_paths"] == ["README.md", "new.py"]
    patch = next((tmp_path / "attempts").glob("*/candidate.patch")).read_bytes()
    assert candidate.diff_fingerprint == "sha256:" + sha256(patch).hexdigest()
    assert receipt["usage"]["cached_input_tokens"] is None
    assert receipt["cost_usd"] is None


def test_no_change_keeps_champion_sha(repository, final_case, tmp_path: Path) -> None:
    result = _prepare(_adapter(repository, final_case, tmp_path, agent=Agent("none")), repository)
    assert result.candidate_sha == repository[1]


def test_blocked_agent_is_failure_not_successful_no_change(repository, final_case, tmp_path: Path) -> None:
    with pytest.raises(TrialExecutionError, match="agent_blocked"):
        _prepare(_adapter(repository, final_case, tmp_path, agent=Agent("blocked")), repository)
    report = json.loads(next((tmp_path / "attempts").glob("*/agent-explanation.json")).read_text())
    assert report["status"] == "blocked"
    assert report["experiment_summary"] == "Policy blocked"
    assert not list((tmp_path / "attempts").glob("*/candidate.json"))


def test_crlf_only_change_has_empty_committed_evidence(repository, final_case, tmp_path: Path) -> None:
    _git(repository[0], "config", "core.autocrlf", "true")
    result = _prepare(_adapter(repository, final_case, tmp_path, agent=Agent("crlf")), repository)
    assert result.candidate_sha == repository[1]
    assert result.diff_fingerprint == "sha256:" + sha256(b"").hexdigest()
    receipt = json.loads(next((tmp_path / "attempts").glob("*/candidate.json")).read_text())
    assert receipt["changed_paths"] == []
    assert next((tmp_path / "attempts").glob("*/candidate.patch")).read_bytes() == b""


@pytest.mark.parametrize("behavior, reason", [("credential", "workspace_credential_detected"), ("commit", "candidate_head_changed"), ("runtime_config", "candidate_runtime_config"), ("stage_input", "candidate_harness_artifact_staged")])
def test_unsafe_candidate_is_rejected_with_failure_evidence(repository, final_case, tmp_path, behavior, reason) -> None:
    with pytest.raises(TrialExecutionError, match=reason):
        _prepare(_adapter(repository, final_case, tmp_path, agent=Agent(behavior)), repository)
    assert len(list((tmp_path / "attempts").glob("*/failure.json"))) == 1
    assert not list((tmp_path / "workspaces").iterdir())


def test_validation_runs_fresh_paired_seed_and_preserves_native_artifacts(repository, final_case, tmp_path: Path) -> None:
    predictor = Predictor()
    adapter = _adapter(repository, final_case, tmp_path, predictor=predictor)
    candidate = _prepare(adapter, repository)
    request = ValidationPairRequest(candidate, final_case[0].judge, 42)
    first = adapter.run_validation(request, YouTubeCTRDomain())
    second = adapter.run_validation(request, YouTubeCTRDomain())
    assert first.pair.seed == 42
    assert first.pair.baseline == first.pair.candidate
    assert len(predictor.requests) == 4
    assert all(item.seed == 42 for item in predictor.requests)
    assert len({item.process.cwd for item in predictor.requests}) == 4
    assert set(artifact.uri for artifact in first.artifacts).isdisjoint(artifact.uri for artifact in second.artifacts)
    artifacts = (*candidate.artifacts, *first.artifacts, *second.artifacts)
    assert len({item.name for item in artifacts}) == len(artifacts)
    assert all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", item.name) for item in artifacts)
    assert len(list((tmp_path / "attempts").glob("*/baseline/predictions.model.txt"))) == 2
    assert not list((tmp_path / "workspaces").iterdir())


def test_final_pair_passes_same_grant_to_both_evaluations(repository, final_case, tmp_path: Path) -> None:
    fixture, _, _, grant = final_case
    adapter = _adapter(repository, final_case, tmp_path)
    candidate = _prepare(adapter, repository)
    domain = YouTubeCTRDomain()
    seen = []
    original = domain.evaluate
    # Class-level monkeypatch is unnecessary: a narrow proxy records only the grant seam.
    class RecordingDomain:
        validate_candidate = staticmethod(domain.validate_candidate)
        def evaluate(self, handoff, prediction, *, final_grant=None):
            seen.append(final_grant)
            return original(handoff, prediction, final_grant=final_grant)
    receipt = adapter.run_final(FinalPairRequest(repository[1], candidate.candidate_sha, fixture.judge, grant, 43), RecordingDomain())
    assert seen == [grant, grant]
    assert receipt.pair.seed == 43
    assert receipt.pair.baseline.evaluation_id == fixture.judge.final_holdout_id
    assert grant._authorizes(fixture.judge)


@pytest.mark.parametrize("grant", [None, object()])
def test_final_never_downgrades_to_validation_on_invalid_grant(repository, final_case, tmp_path, grant) -> None:
    predictor = Predictor()
    adapter = _adapter(repository, final_case, tmp_path, predictor=predictor)
    with pytest.raises(TrialExecutionError, match="final_grant_invalid"):
        adapter.run_final(FinalPairRequest(repository[1], repository[1], final_case[0].judge, grant, 2), YouTubeCTRDomain())
    assert not predictor.requests


@pytest.mark.parametrize("missing_sidecar", [False, True])
def test_prediction_failure_keeps_partial_outputs_and_safe_error(repository, final_case, tmp_path, missing_sidecar) -> None:
    adapter = _adapter(repository, final_case, tmp_path, predictor=Predictor(fail=not missing_sidecar, missing_sidecar=missing_sidecar))
    candidate = _prepare(adapter, repository)
    with pytest.raises(TrialExecutionError):
        adapter.run_validation(ValidationPairRequest(candidate, final_case[0].judge, 3), YouTubeCTRDomain())
    assert list((tmp_path / "attempts").glob("*/baseline/predictions.csv"))
    assert list((tmp_path / "attempts").glob("*/failure.json"))
    assert not list((tmp_path / "workspaces").iterdir())


def test_wrong_handoff_fails_before_prediction(repository, final_case, tmp_path: Path) -> None:
    predictor = Predictor()
    adapter = _adapter(repository, final_case, tmp_path, predictor=predictor)
    candidate = _prepare(adapter, repository)
    handoff = replace(final_case[0].judge, manifest_sha256="0" * 64)
    with pytest.raises(TrialExecutionError, match="handoff_mismatch"):
        adapter.run_validation(ValidationPairRequest(candidate, handoff, 3), YouTubeCTRDomain())
    assert not predictor.requests


def test_hardlinked_native_model_is_rejected(repository, final_case, tmp_path: Path) -> None:
    adapter = _adapter(repository, final_case, tmp_path, predictor=Predictor(alias_sidecar=True))
    candidate = _prepare(adapter, repository)
    with pytest.raises(TrialExecutionError, match="prediction_artifact_invalid"):
        adapter.run_validation(ValidationPairRequest(candidate, final_case[0].judge, 3), YouTubeCTRDomain())
    assert not list((tmp_path / "attempts").glob("*/baseline/sealed.csv"))


def test_interruption_is_not_swallowed_and_records_elapsed_attempt(repository, final_case, tmp_path: Path) -> None:
    adapter = _adapter(repository, final_case, tmp_path, agent=Agent("interrupt"))
    with pytest.raises(KeyboardInterrupt):
        _prepare(adapter, repository)
    failures = list((tmp_path / "attempts").glob("*/failure.json"))
    assert len(failures) == 1
    assert json.loads(failures[0].read_text())["reason_code"] == "trial_interrupted"
    assert not list((tmp_path / "workspaces").iterdir())


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_prediction_cancellation_preserves_partial_model_and_elapsed_time(repository, final_case, tmp_path, interrupt) -> None:
    adapter = _adapter(repository, final_case, tmp_path, predictor=Predictor(interrupt=interrupt))
    candidate = _prepare(adapter, repository)
    with pytest.raises(interrupt):
        adapter.run_validation(ValidationPairRequest(candidate, final_case[0].judge, 7), YouTubeCTRDomain())
    executions = list((tmp_path / "attempts").glob("*/baseline/execution.json"))
    assert len(executions) == 1
    receipt = json.loads(executions[0].read_text())
    assert receipt["reason_code"] == "trial_interrupted"
    assert receipt["duration_ms"] >= 0
    assert (executions[0].parent / "predictions.model.txt").read_text() == "native model"
    assert list((tmp_path / "attempts").glob("*/failure.json"))
    assert not list((tmp_path / "workspaces").iterdir())


def test_attempt_creation_failure_uses_safe_typed_error(repository, final_case, tmp_path, monkeypatch) -> None:
    adapter = _adapter(repository, final_case, tmp_path)
    def fail_write(*args, **kwargs):
        raise OSError("private local path")
    monkeypatch.setattr(trial_module, "_write_json", fail_write)
    with pytest.raises(TrialExecutionError, match="trial_artifact_failed") as error:
        _prepare(adapter, repository)
    assert "private local path" not in str(error.value)
