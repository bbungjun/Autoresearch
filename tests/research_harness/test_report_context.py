"""보고서 v2의 지표 출처와 기존 v1 게시물의 바이트 호환성을 검증한다."""

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
import html
import json
import math
import sys
from types import SimpleNamespace

import pytest

from autoresearch.research_harness.controller import _feedback_from_record
from autoresearch.research_harness.judge_decision import JudgeMetric, MetricDelta, PairedJudgeResult, compare_confirmation
from autoresearch.research_harness.ledger import CheckpointRecord, LedgerMetric, TrialRecord, open_trial_ledger
from tests.research_harness.test_controller import _score_for
from tests.research_harness.test_judge_decision import _score
from tests.research_harness.test_report import (
    FakeJudge, evidence, final_case, finished as finished, module, publish, write_json,
)
from tests.research_harness.test_run_inputs import case as case, prepared as prepared, candidate_fixture as candidate_fixture


def validation_case(finished, *, confirmed=True, count=5, linked=True, duplicate=False):
    root, contract, result, parent = finished
    seeds = [contract.screening_seed]
    if confirmed:
        seeds += list(contract.confirmation_seeds[:count])
    if duplicate:
        seeds.append(contract.confirmation_seeds[0])
    artifacts = []
    screening_value = 0.51 if confirmed else 0.49
    for index, seed in enumerate(seeds):
        attempt = root / "attempts" / f"{index + 1000:032x}"
        write_json(attempt / "attempt.json", {"stage": "validation", "trial_id": "trial-0001",
                   "seed": seed, "started_at_unix_ns": seed})
        write_json(attempt / "pair.json", {"baseline_sha": contract.baseline_sha, "candidate_sha": "b" * 40,
                   "seed": seed, "duration_ms": 20, "baseline": asdict(_score_for(0.5, contract.handoff.validation_id)),
                   "candidate": asdict(_score_for(screening_value if seed == contract.screening_seed else 0.6, contract.handoff.validation_id))})
        if linked:
            artifacts.append(evidence(attempt / "pair.json"))
    trial = TrialRecord("trial-0001", "validation", contract.baseline_sha, "b" * 40,
                        "sha256:" + "d" * 64, str(contract.handoff.validation_id), contract.screening_seed,
                        (LedgerMetric("ndcg_at_10", screening_value), LedgerMetric("delta__ndcg_at_10", 0.1 if confirmed else -0.01)),
                        "promote" if confirmed else "discard", "promotion_threshold_met" if confirmed else "primary_not_improved",
                        100, None, tuple(artifacts), (contract.baseline_sha, "b" * 40) if confirmed else (contract.baseline_sha,),
                        None, contract.initial_card.canonical_summary())
    ledger = open_trial_ledger(root / "experiment-ledger.jsonl")
    ledger.append(trial)
    ledger.append(CheckpointRecord("trial-0001:validation-recorded", "validation_recorded", trial.trial_id,
                                  datetime.now(UTC), tuple(artifacts), None))
    feedback = (_feedback_from_record(contract.initial_card, contract.initial_card, trial, []),)
    return root, contract, replace(result, champion_sha="b" * 40 if confirmed else contract.baseline_sha,
                                   validation_trials=1, feedback_history=feedback), parent


def test_new_record_separates_screening_values_and_confirmation_deltas(finished):
    finished = validation_case(finished)
    judge = FakeJudge()
    publish(finished, judge)
    record = json.loads((finished[0] / "research-record.json").read_bytes())
    assert record["version"] == "research-record-v2"
    trial = record["trials"][0]
    assert "metric_scope" not in trial and "observed_metrics" not in trial
    absolute, delta = (trial["metric_groups"][key] for key in ("candidate_absolute", "decision_delta"))
    assert absolute["scope"] == "validation_screening" and absolute["seeds"] == [finished[1].screening_seed]
    assert absolute["aggregation"] == "single_value" and absolute["metrics"] == {"ndcg_at_10": 0.51}
    assert delta["scope"] == "validation_confirmation" and delta["seeds"] == list(finished[1].confirmation_seeds)
    assert delta["aggregation"] == "mean_of_paired_direction_normalized_deltas"
    assert delta["metrics"] == {"ndcg_at_10": 0.1}
    assert len(absolute["evidence_refs"]) == 1 and len(delta["evidence_refs"]) == 5
    assert set(delta["evidence_refs"][0]) == {"attempt_id", "path", "sha256"}
    markdown = html.unescape((finished[0] / "research-report.md").read_text(encoding="utf-8"))
    assert "validation_screening" in markdown and "validation_confirmation" in markdown
    assert "mean_of_paired_direction_normalized_deltas" in judge.requests[0].prompt
    assert "Do not subtract" in judge.requests[0].prompt


def test_validation_promote_and_final_discard_keep_different_scopes_and_decisions(finished):
    finished = validation_case(finished)
    finished = final_case(finished, champion="b" * 40, candidate_value=0.51, feedback=finished[2].feedback_history,
                          deltas=(MetricDelta(JudgeMetric.NDCG_AT_10, 0.01),))
    publish(finished)
    record = json.loads((finished[0] / "research-record.json").read_bytes())
    validation, final = record["trials"]
    assert validation["decision"] == "promote" and final["decision"] == "discard"
    assert record["outcome"]["baseline_retained"] is True
    assert validation["metric_groups"]["decision_delta"]["scope"] == "validation_confirmation"
    assert final["metric_groups"]["candidate_absolute"]["scope"] == "final_confirmation"
    assert final["metric_groups"]["decision_delta"]["scope"] == "final_confirmation"
    assert final["metric_groups"]["decision_delta"]["metrics"] == {"ndcg_at_10": 0.01}


def test_baseline_is_champion_still_has_valid_zero_final_delta(finished):
    publish(final_case(finished, deltas=(MetricDelta(JudgeMetric.NDCG_AT_10, 0.0),)))
    record = json.loads((finished[0] / "research-record.json").read_bytes())
    final = record["trials"][0]
    assert final["candidate_sha"] is None
    assert final["metric_groups"]["decision_delta"]["status"] == "available"
    assert final["metric_groups"]["decision_delta"]["metrics"] == {"ndcg_at_10": 0.0}


@pytest.mark.parametrize("options", [{"count": 2}, {"linked": False}, {"duplicate": True}])
def test_incomplete_or_unlinked_confirmation_never_claims_five_seed_mean(finished, options):
    publish(validation_case(finished, **options))
    trial = json.loads((finished[0] / "research-record.json").read_bytes())["trials"][0]
    delta = trial["metric_groups"]["decision_delta"]
    assert delta["status"] == "not_available" and delta["scope"] == "unknown" and delta["seeds"] == []
    assert delta["metrics"] == {"ndcg_at_10": 0.1}


def test_screening_delta_uses_single_pair_not_confirmation_seeds(finished):
    publish(validation_case(finished, confirmed=False))
    delta = json.loads((finished[0] / "research-record.json").read_bytes())["trials"][0]["metric_groups"]["decision_delta"]
    assert delta["scope"] == "validation_screening"
    assert delta["aggregation"] == "single_pair_direction_normalized_delta"
    assert delta["seeds"] == [finished[1].screening_seed]


@pytest.mark.parametrize("count", [2, 5])
def test_final_absolute_group_needs_five_linked_pairs(finished, count):
    publish(final_case(finished, count=count))
    group = json.loads((finished[0] / "research-record.json").read_bytes())["trials"][0]["metric_groups"]["candidate_absolute"]
    assert group["status"] == ("available" if count == 5 else "not_available")
    assert group["scope"] == ("final_confirmation" if count == 5 else "unknown")


def test_policy_explains_existing_exact_threshold_without_rejudging(finished):
    publish(finished)
    record = json.loads((finished[0] / "research-record.json").read_bytes())
    policy = record["run"]["decision_policy"]
    assert policy["authority"] == "explanation_only_not_rescoring"
    assert policy["screening"] == {"metric": "ndcg_at_10", "operator": ">", "threshold": 0.0}
    assert policy["confirmation"]["required_pairs"] == 5 and policy["confirmation"]["comparison_tolerance"] == 0.0
    thresholds = policy["confirmation"]["thresholds"]
    for name, item in thresholds.items():
        factor = 2.0 if name == "ndcg_at_10" else -1.0
        assert item["sigma"] == record["run"]["baseline_sigmas"][name]
        assert item["factor"] == factor and item["operator"] == ">="
        assert item["threshold"] == factor * item["sigma"]
        assert item["direction"] == ("baseline_minus_candidate" if name in {"log_loss", "brier"} else "candidate_minus_baseline")
    assert policy["validity"]["sigma"] == {"operator": ">", "threshold": 1e-6}
    assert policy["validity"]["coverage"] == "max(30, ceil(total * 0.20))"
    markdown = html.unescape((finished[0] / "research-report.md").read_text(encoding="utf-8"))
    assert str(thresholds["ndcg_at_10"]["threshold"]) in markdown
    assert "재채점" in markdown and "global ROC-AUC" in markdown


@pytest.mark.parametrize("sigma", [None, 0.0, 1e-6, math.nextafter(1e-6, math.inf), sys.float_info.max])
def test_sigma_boundary_missing_and_threshold_overflow_are_not_coerced(sigma):
    thresholds = module()._decision_policy({"ndcg_at_10": sigma})["confirmation"]["thresholds"]
    item = thresholds["ndcg_at_10"]
    valid = sigma == math.nextafter(1e-6, math.inf)
    assert item["sigma"] == sigma and item["status"] == ("available" if valid else "not_available")
    assert item["threshold"] == (2.0 * sigma if valid else None)
    assert thresholds["brier"]["sigma"] is None and thresholds["brier"]["threshold"] is None


def test_explained_threshold_matches_real_judge_at_exact_boundary():
    sigmas = {metric.value: 0.0625 for metric in JudgeMetric}
    policy = module()._decision_policy(sigmas)
    assert policy["confirmation"]["thresholds"]["ndcg_at_10"]["threshold"] == 0.125
    baseline = _score(ndcg_at_10=0.5)
    for candidate_value, expected in ((0.625, "promote"), (math.nextafter(0.625, 0.0), "discard")):
        pairs = tuple(PairedJudgeResult(seed, baseline, _score(ndcg_at_10=candidate_value)) for seed in range(101, 106))
        assert compare_confirmation(pairs, baseline_sigmas=sigmas).decision.value == expected
    baseline = _score(ndcg_at_10=0.5, brier=0.25)
    assert policy["confirmation"]["thresholds"]["brier"]["threshold"] == -0.0625
    for brier, expected in ((0.3125, "promote"), (math.nextafter(0.3125, math.inf), "revise")):
        pairs = tuple(PairedJudgeResult(seed, baseline, _score(ndcg_at_10=0.625, brier=brier)) for seed in range(101, 106))
        assert compare_confirmation(pairs, baseline_sigmas=sigmas).decision.value == expected


@pytest.mark.parametrize("observed,source", [(None, None), (0.1, 0.2)])
def test_null_or_mismatched_source_preserves_value_but_marks_group_unavailable(observed, source):
    trial = {"trial_id": "trial-1", "seed": 42, "split": "validation", "reason_code": "primary_not_improved",
             "failure_reason_code": None, "observed_metrics": {"ndcg_at_10": observed}}
    pair = {"trial_id": "trial-1", "seed": 42, "evidence": {},
            "metrics": {"baseline": {"ndcg_at_10": 0.5}, "candidate": {"ndcg_at_10": source}}}
    contract = SimpleNamespace(screening_seed=42, confirmation_seeds=(101, 102, 103, 104, 105))
    group = module()._metric_groups(trial, [pair], contract)["candidate_absolute"]
    assert group["status"] == "not_available" and group["metrics"] == {"ndcg_at_10": observed}


def test_final_direction_normalized_delta_is_paired_mean_not_absolute_mean():
    trial = {"trial_id": "final-holdout", "seed": 1, "split": "final_holdout", "reason_code": "primary_threshold_not_met",
             "failure_reason_code": None, "observed_metrics": {"log_loss": 0.375, "delta__log_loss": 0.125}}
    pairs = [{"trial_id": trial["trial_id"], "seed": seed, "evidence": {"attempt_id": f"attempt:{seed}"},
              "metrics": {"baseline": {"log_loss": 0.5}, "candidate": {"log_loss": 0.375}}} for seed in range(101, 106)]
    contract = SimpleNamespace(screening_seed=42, confirmation_seeds=(101, 102, 103, 104, 105))
    groups = module()._metric_groups(trial, pairs, contract)
    assert groups["candidate_absolute"]["aggregation"] == "arithmetic_mean"
    assert groups["decision_delta"]["scope"] == "final_confirmation"
    assert groups["decision_delta"]["metrics"]["log_loss"] == 0.125


def test_existing_record_content_is_revalidated_not_just_its_version(finished):
    judge = FakeJudge()
    publish(finished, judge)
    path = finished[0] / "research-record.json"
    record = json.loads(path.read_bytes())
    record["outcome"]["conclusion"] = "tampered"
    write_json(path, record)
    with pytest.raises(module().ReportError, match="record_projection_changed"):
        publish(finished, judge)
    assert len(judge.requests) == 1


@pytest.mark.parametrize("partial", [False, True])
def test_existing_v1_keeps_every_published_byte_and_never_reinvokes(finished, partial, monkeypatch):
    m = module()
    root, contract, result, _ = finished
    m.seal_terminal_result(root, contract=contract, result=result)
    legacy = m._collect_record(root, contract, result, version="research-record-v1")
    (root / "research-record.json").write_bytes(m.json_bytes(legacy))
    judge = FakeJudge()
    publish(finished, judge)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("research-*") if p.is_file()}
    if partial:
        (root / "research-report-manifest.json").unlink()
    publish(finished, judge)
    assert len(judge.requests) == 1
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("research-*") if p.is_file()}


@pytest.mark.parametrize("attempt_completed", [False, True])
def test_legacy_intent_only_and_completed_attempt_recover_without_new_judge(finished, attempt_completed):
    m = module()
    root, contract, result, _ = finished
    m.seal_terminal_result(root, contract=contract, result=result)
    record_bytes = m.json_bytes(m._collect_record(root, contract, result, version="research-record-v1"))
    (root / "research-record.json").write_bytes(record_bytes)
    class Interrupted(FakeJudge):
        def run(self, request):
            if attempt_completed:
                return super().run(request)
            self.requests.append(request)
            raise KeyboardInterrupt
    judge = Interrupted("interrupt")
    with pytest.raises(KeyboardInterrupt):
        publish(finished, judge)
    assert not (root / "research-judge.json").exists()
    intent_bytes = (root / "research-judge-intent.json").read_bytes()
    publish(finished, judge)
    assert len(judge.requests) == 1
    assert (root / "research-record.json").read_bytes() == record_bytes
    assert (root / "research-judge-intent.json").read_bytes() == intent_bytes
    review = json.loads((root / "research-judge.json").read_bytes())
    assert review["availability"] == ("available" if attempt_completed else "unavailable")


@pytest.mark.parametrize("version", ["research-record-v0", "research-record-v3", None])
def test_unsupported_record_version_fails_before_judge(finished, version):
    module().seal_terminal_result(finished[0], contract=finished[1], result=finished[2])
    write_json(finished[0] / "research-record.json", {"version": version})
    judge = FakeJudge()
    with pytest.raises(module().ReportError, match="record_version"):
        publish(finished, judge)
    assert judge.requests == []


@pytest.mark.parametrize("name", ["research-judge-intent.json", "research-judge.json", "research-report.md",
                                  "research-report-manifest.json", "research-judge-attempt", "research-judge-workspace-failure.json"])
def test_missing_record_with_later_evidence_fails_before_any_publication(finished, name):
    root = finished[0]
    module().seal_terminal_result(root, contract=finished[1], result=finished[2])
    path = root / name
    path.mkdir() if name == "research-judge-attempt" else path.write_text("{}")
    judge = FakeJudge()
    with pytest.raises(module().ReportError, match="record_missing"):
        publish(finished, judge)
    assert not (root / "research-record.json").exists() and judge.requests == []


def legacy_golden_record(tmp_path, monkeypatch):
    """수정 전 5025926 구현으로 계산한 고정 projection/prompt/render 해시의 입력."""
    @dataclass
    class Card:
        hypothesis: str = "golden"
    contract = SimpleNamespace(initial_card=Card(), budget=Card(), baseline_sha="a" * 40, champion_sha="a" * 40,
                               screening_seed=42, confirmation_seeds=(101, 102, 103, 104, 105), baseline_sigmas={"ndcg_at_10": 0.01},
                               handoff=SimpleNamespace(snapshot_fingerprint="snapshot", validation_id="validation", final_holdout_id="final"))
    trial = SimpleNamespace(trial_id="trial-0001", split="validation", base_sha="a" * 40, candidate_sha="b" * 40,
                            diff_fingerprint="diff", experiment_summary=None, evaluation_id="validation", seed=42,
                            metrics=(LedgerMetric("ndcg_at_10", 0.5), LedgerMetric("delta__ndcg_at_10", -0.1)),
                            decision="discard", reason_code="primary_not_improved", failure_reason_code=None,
                            champion_lineage=("a" * 40,), artifacts=())
    m = module()
    monkeypatch.setattr(m, "terminal_context", lambda *args: (SimpleNamespace(manifest_sha256="m" * 64), SimpleNamespace(trials=[trial], checkpoints=[]), "l" * 64))
    monkeypatch.setattr(m, "read_file", lambda *args, **kwargs: b"terminal")
    monkeypatch.setattr(m, "_runtime_summary", lambda contract: {"runtime_sha256": "r" * 64, "embedding": {}})
    monkeypatch.setattr(m, "_private_strings", lambda *args: [])
    result = SimpleNamespace(conclusion=SimpleNamespace(value="inconclusive"), champion_sha="a" * 40,
                             final_decision=None, final_reason_code="already_consumed")
    return m._collect_record(tmp_path, contract, result, version="research-record-v1")


def test_v1_projection_prompt_and_render_match_prechange_golden(tmp_path, monkeypatch):
    record = legacy_golden_record(tmp_path, monkeypatch)
    m = module()
    review = {"availability": "unavailable", "response": None, "reason_code": "judge_unavailable", "usage": None, "duration_ms": None}
    assert sha256(m.json_bytes(record)).hexdigest() == "b02581927bbac630a1bfec21453b0258c9640fe235eaf62d6f3bda6bda9cccc4"
    assert sha256(m._judge_prompt(record).encode()).hexdigest() == "b48eab35517593f85c54c0a94c3f293c5e8c2da6ac2e2056a8e86591bf0a757f"
    assert sha256(m._render_report(record, review)).hexdigest() == "82e860fee71852ed6fcb4d3bfdf482e0fc87708327b5f41e5a7dd95dfbb2154f"
