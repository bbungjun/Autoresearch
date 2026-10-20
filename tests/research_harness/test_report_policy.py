"""고정 판정 정책의 보고서 설명과 legacy 증거 무결성을 검증한다."""

from dataclasses import replace
from datetime import UTC, datetime
import html
import json
import math

import pytest

from autoresearch.research_harness.controller import ControllerConclusion, ControllerRunResult, _feedback_from_record
from autoresearch.research_harness.judge_decision import JudgeMetric
from autoresearch.research_harness.ledger import CheckpointRecord, TrialRecord, open_trial_ledger
from autoresearch.research_harness.local_runtime import bind_input_checkpoint
from tests.research_harness.test_report import FakeJudge, module, publish
from tests.research_harness.test_run_inputs import (
    _freeze, case as case, prepared as prepared, candidate_fixture as candidate_fixture,
)


def make_finished(case, version):
    """합성 입력만 봉인하고 Agent/final 없이 종료한 fixture를 만든다."""
    inputs, root, contract, validation, final = case
    contract = replace(contract, judge_policy_version=version,
                       baseline_sigmas=tuple((metric.value, 0.0 if metric is JudgeMetric.RECALL_AT_10 else 0.01)
                                             for metric in JudgeMetric))
    frozen = _freeze((inputs, root, contract, validation, final))
    bind_input_checkpoint(open_trial_ledger(root / "experiment-ledger.jsonl"), frozen.artifact)
    result = ControllerRunResult(ControllerConclusion.INCONCLUSIVE, contract.champion_sha,
                                 0, (), None, "already_consumed", None)
    parent = root.parent / "judge-workspaces"
    parent.mkdir()
    return root, contract, result, parent


@pytest.mark.parametrize("sigma", [0.0, math.nextafter(1e-6, 0.0), 1e-6, math.nextafter(1e-6, math.inf)])
def test_policy_explains_guardrail_boundary_by_fixed_version(sigma):
    sigmas = {metric.value: 0.01 for metric in JudgeMetric}
    sigmas["recall_at_10"] = sigma
    m = module()
    v1, v2 = (m._decision_policy(sigmas, version) for version in ("v1", "v2"))
    old = v1["confirmation"]["thresholds"]["recall_at_10"]
    new = v2["confirmation"]["thresholds"]["recall_at_10"]
    zero = sigma <= 1e-6
    assert old["threshold"] == (None if zero else -sigma)
    assert new["threshold"] == (0.0 if zero else -sigma)
    assert new["status"] == ("zero_observed_variance" if zero else "available")
    assert new["basis"] == ("zero_tolerance_non_inferiority" if zero else "baseline_seed_sigma")
    assert new["at_bound"] is None
    assert new["at_bound_basis"] == "deferred_calibration_raw_values_not_in_run_contract"
    assert v2["validity"]["sigma"]["primary"] == {"operator": ">", "threshold": 1e-6}
    assert v2["validity"]["sigma"]["guardrails"]["threshold"] == 0.0


@pytest.mark.parametrize("sigma", [None, True, -1.0, float("nan"), float("inf")])
def test_v2_policy_does_not_explain_invalid_guardrail_as_zero_variance(sigma):
    item = module()._decision_policy({"recall_at_10": sigma}, "v2")["confirmation"]["thresholds"]["recall_at_10"]
    assert item["status"] == "not_available" and item["threshold"] is None


def test_unknown_policy_is_rejected():
    with pytest.raises(module().ReportError, match="judge_policy_version"):
        module()._decision_policy({}, "v3")


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_new_report_preserves_fixed_policy_without_experiment_calls(case, version, monkeypatch):
    finished = make_finished(case, version)
    root, contract, _, _ = finished
    judge = FakeJudge()
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    publish(finished, judge)
    record = json.loads((root / "research-record.json").read_bytes())
    assert record["version"] == "research-record-v3"
    assert record["run"]["judge_policy_version"] == version
    policy = record["run"]["decision_policy"]
    assert policy["judge_policy_version"] == version
    assert record["outcome"]["final_decision"] is None
    item = policy["confirmation"]["thresholds"]["recall_at_10"]
    assert item["status"] == ("zero_observed_variance" if version == "v2" else "not_available")
    markdown = html.unescape((root / "research-report.md").read_text(encoding="utf-8"))
    assert f"고정 판정 정책 버전: {version}" in markdown
    if version == "v2":
        assert "zero_tolerance_non_inferiority" in markdown and "at_bound" in markdown
        assert "각 sigma > 1e-6가 필요" not in markdown
    published = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    publish(finished, judge)
    assert len(judge.requests) == 1
    assert published == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert all((root / path).read_bytes() == payload for path, payload in before.items())


@pytest.mark.parametrize("record_version", ["v1", "v2"])
def test_terminal_refuses_record_contract_policy_mismatch_before_advisory_judge(case, record_version):
    finished = make_finished(case, "v2" if record_version == "v1" else "v1")
    root, contract, result, _ = finished
    ledger = open_trial_ledger(root / "experiment-ledger.jsonl")
    ledger.append(TrialRecord("trial-0001", "validation", contract.baseline_sha, None, None,
                             str(contract.handoff.validation_id), contract.screening_seed, (),
                             "inconclusive", "prediction_failed", 0, "prediction_failed", (),
                             (contract.baseline_sha,), None, failure_stage="pair",
                             judge_policy_version=record_version))
    before = (root / "experiment-ledger.jsonl").read_bytes()
    with pytest.raises(module().ReportError, match="terminal_judge_policy_version"):
        module().seal_terminal_result(root, contract=contract, result=result)
    assert (root / "experiment-ledger.jsonl").read_bytes() == before
    assert not (root / "controller-result-binding.json").exists()


def test_record_projection_preserves_recorded_zero_variance_metadata(case):
    finished = make_finished(case, "v2")
    root, contract, result, _ = finished
    ledger = open_trial_ledger(root / "experiment-ledger.jsonl")
    record = TrialRecord("trial-0001", "validation", contract.baseline_sha, None, None,
                         str(contract.handoff.validation_id), contract.screening_seed, (),
                         "inconclusive", "insufficient_baseline_noise", 0, "insufficient_baseline_noise", (),
                         (contract.baseline_sha,), None,
                         experiment_summary=contract.initial_card.canonical_summary(), judge_policy_version="v2",
                         zero_variance_guardrails=("recall_at_10",))
    ledger.append(record)
    # The projection hashes the sealed terminal files; establish their provenance first.
    ledger.append(CheckpointRecord("trial-0001:validation-recorded", "validation_recorded", "trial-0001",
                                   datetime.now(UTC), (), None))
    feedback = _feedback_from_record(contract.initial_card, contract.initial_card, record, [])
    result = replace(result, validation_trials=1, feedback_history=(feedback,))
    module().seal_terminal_result(root, contract=contract, result=result)
    # Projection is explanatory; independently recorded invalid decision is never recomputed.
    projection = module()._collect_record(root, contract, result)
    trial = projection["trials"][0]
    assert trial["judge_policy_version"] == "v2"
    assert trial["zero_variance_guardrails"] == ["recall_at_10"]
    assert trial["decision"] == "inconclusive" and trial["reason_code"] == "insufficient_baseline_noise"


def test_v2_frozen_inputs_controller_final_and_report_share_policy(case):
    """실제 공통 경로 전체를 합성 점수와 임시 final registry로 검증한다."""
    from autoresearch.research_harness.controller import ControllerRunRequest, ResearchController
    from autoresearch.research_harness.domain import YouTubeCTRDomain
    from tests.research_harness.test_consumption_registry import _case
    from tests.research_harness.test_controller import SequencePlanner
    from tests.research_harness.test_controller_policy_integration import SyntheticRunner

    inputs, root, contract, validation, final = case
    state_root, handoff, _ = _case(contract.handoff, root.parent)
    contract = replace(contract, handoff=handoff, judge_state_root=state_root, judge_policy_version="v2",
                       baseline_sigmas=tuple((metric.value, 0.0 if metric is JudgeMetric.RECALL_AT_10 else 0.01)
                                             for metric in JudgeMetric))
    frozen = _freeze((inputs, root, contract, validation, final))
    ledger = open_trial_ledger(root / "experiment-ledger.jsonl")
    bind_input_checkpoint(ledger, frozen.artifact)
    request = ControllerRunRequest(
        contract.initial_card, contract.budget, contract.baseline_sha, contract.champion_sha,
        handoff, state_root, contract.baseline_sigmas, contract.screening_seed,
        contract.confirmation_seeds, ledger, judge_policy_version=contract.judge_policy_version,
    )
    runner = SyntheticRunner()
    controller = ResearchController(YouTubeCTRDomain(), SequencePlanner((contract.initial_card,), []), runner)
    result = controller.run(request)
    assert result.conclusion is ControllerConclusion.IMPROVED
    parent = root.parent / "judge-workspaces"
    parent.mkdir()
    judge = FakeJudge()
    publish((root, contract, result, parent), judge)
    projection = json.loads((root / "research-record.json").read_bytes())
    assert projection["run"]["judge_policy_version"] == "v2"
    assert [trial["split"] for trial in projection["trials"]] == ["validation", "final_holdout"]
    assert all(trial["judge_policy_version"] == "v2" and trial["decision"] == "promote"
               and trial["zero_variance_guardrails"] == ["recall_at_10"] for trial in projection["trials"])
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    marker_bytes = result.final_consumption.marker_path.read_bytes()
    replay_runner = SyntheticRunner()
    replay_planner = SequencePlanner((contract.initial_card,), [])
    assert ResearchController(YouTubeCTRDomain(), replay_planner, replay_runner).run(request) == result
    publish((root, contract, result, parent), judge)
    assert replay_runner.preparations == replay_runner.validation_runs == replay_runner.final_runs == []
    assert len(judge.requests) == 1
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert result.final_consumption.marker_path.read_bytes() == marker_bytes
