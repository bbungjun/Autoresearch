"""신규 평가의 날짜 규칙, 원본 pin 및 candidate 정답 경계를 검증한다."""

from datetime import date
from hashlib import sha256

import pyarrow.parquet as pq
import pytest

from autoresearch.research_harness.behavior_data import BehaviorDataRequest, KST, daily_drafts
from autoresearch.research_harness.behavior_evaluation import (
    BehaviorEvaluationRequest, BehaviorEvaluationSource, evaluation_policy, generate_behavior_evaluation,
    prepare_behavior_metadata, seal_behavior_evaluation, select_evaluation_users,
)
from autoresearch.research_harness.candidate_data_view import (
    materialize_candidate_data_view_v2, materialize_final_candidate_data_view,
)
from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes
from autoresearch.research_harness.fixture_errors import StageCError
from autoresearch.research_harness.fixture_inputs import _fixture_video_rows, select_fixture_user_ids
from autoresearch.research_harness.fixture_models import CandidateDataViewRequest
from autoresearch.research_harness.local_training import load_local_training_input


@pytest.fixture(scope="module")
def sealed(tmp_path_factory):
    root = tmp_path_factory.mktemp("behavior-eval")
    policy = root / "policy.json"
    request = BehaviorEvaluationRequest(11191, validation_users=40, final_users=20)
    policy.write_bytes(canonical_json_bytes(evaluation_policy((request,))))
    raw = root / "raw"
    generate_behavior_evaluation(raw, request, policy_path=policy,
                                 expected_policy_sha256=sha256(policy.read_bytes()).hexdigest())
    digest = sha256((raw / "manifest.json").read_bytes()).hexdigest()
    source = BehaviorEvaluationSource(raw, expected_manifest_sha256=digest)
    handoff = seal_behavior_evaluation(source, root / "judge")
    return root, source, handoff, request


def test_extended_dates_keep_anchor_and_original_daily_rule():
    extended = BehaviorEvaluationRequest(11191)
    original = BehaviorDataRequest(11191)
    assert extended.start_date == original.start_date == date(2026, 8, 3)
    assert extended.dates[:32] == original.dates
    assert extended.dates[-2:] == (date(2026, 9, 4), date(2026, 9, 5))
    validation, final = select_evaluation_users(extended)
    old_validation, old_final = select_fixture_user_ids(11191)
    assert (len(validation), len(final)) == (800, 200)
    assert validation[:160] == old_validation and final[:40] == old_final
    assert not set(validation) & set(final)
    for day in (date(2026, 8, 3), date(2026, 8, 21), date(2026, 9, 2)):
        users = list(old_validation[:8] + old_final[:2])
        assert daily_drafts(extended, day, users, _fixture_video_rows(day)) == daily_drafts(
            original, day, users, _fixture_video_rows(day))


def test_snapshot_candidate_boundary_and_final_requires_grant(sealed):
    root, source, handoff, request = sealed
    destination = root / "candidate"
    destination.mkdir()
    metadata = prepare_behavior_metadata(source, handoff)
    view = materialize_candidate_data_view_v2(CandidateDataViewRequest(handoff, destination),
                                              source=source, metadata=metadata)
    inputs = load_local_training_input(view.root / "slate.parquet")
    assert len(inputs.manifest.history_partitions) == 32
    assert inputs.manifest.history_partitions[-1].dt == date(2026, 9, 3)
    assert "clicked" not in inputs.slate.column_names
    assert not any(p.name in {"labels.parquet", "manifest.json"} for p in view.root.rglob("*"))
    assert "latent_profiles" not in (view.root / "candidate-view.json").read_text()
    validation, final = select_evaluation_users(request)
    assert set(inputs.slate["user_id"].to_pylist()) <= set(validation)
    assert not set(inputs.slate["user_id"].to_pylist()) & set(final)
    assert {r.astimezone(KST).date() for r in inputs.slate["event_timestamp"].to_pylist()} == {date(2026, 9, 4)}
    final_metadata = prepare_behavior_metadata(source, handoff, final=True)
    with pytest.raises(StageCError):
        materialize_final_candidate_data_view(CandidateDataViewRequest(handoff, root / "final"),
                                             source=source, metadata=final_metadata, grant=None)
    assert not (root / "judge/final-holdout-consumed").exists()
    # 재봉인은 원본 bytes/identity를 유지하며 final을 소비하지 않는다.
    assert seal_behavior_evaluation(source, root / "judge") == handoff


def test_pin_and_corruption_rejected(sealed, tmp_path):
    _, source, _, _ = sealed
    with pytest.raises(ValueError, match="manifest_mismatch"):
        BehaviorEvaluationSource(source.root, expected_manifest_sha256="0" * 64)
    with pytest.raises(ValueError, match="overlap"):
        seal_behavior_evaluation(source, source.root / "nested")
    with pytest.raises(ValueError, match="policy_mismatch"):
        generate_behavior_evaluation(tmp_path / "output", BehaviorEvaluationRequest(11192),
                                    policy_path=source.root.parent / "policy.json", expected_policy_sha256="0" * 64)
    assert not (tmp_path / "output").exists()
    with pytest.raises(ValueError, match="policy_mismatch"):
        generate_behavior_evaluation(tmp_path / "output", BehaviorEvaluationRequest(11191),
                                    policy_path=source.root.parent / "policy.json",
                                    expected_policy_sha256=sha256((source.root.parent / "policy.json").read_bytes()).hexdigest())
    # 기대 receipt와 달라진 bytes를 source 단계에서 거절한다.
    receipt = source.partitions[date(2026, 9, 4)]["events"]
    original = receipt["sha256"]
    receipt["sha256"] = "0" * 64
    try:
        with pytest.raises(ValueError, match="partition_hash_mismatch"):
            source.open_partition(date(2026, 9, 4))
    finally:
        receipt["sha256"] = original


def test_real_generated_slates_do_not_pad_inactive_users(sealed):
    _, source, _, request = sealed
    table = pq.ParquetFile(source._physical_partition_path(date(2026, 9, 4))).read()
    rows = [r for r in table.to_pylist() if r["event_type"] == "impression"]
    counts = {}
    for row in rows:
        counts[row["user_id"]] = counts.get(row["user_id"], 0) + 1
    assert set(counts.values()) <= {8, 16, 24}
    assert 0 < len(counts) < request.validation_users + request.final_users
    assert all(row["event_id"].startswith("evt_20260904_") for row in rows)

