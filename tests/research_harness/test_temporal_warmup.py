"""선행 이력, 실제 학습 행과 KST 피처 cutoff를 검증한다."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from importlib import import_module
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from autoresearch.research_harness.fixture_errors import StageCError
from autoresearch.research_harness.fixture_inputs import canonical_fixture_dates, write_canonical_fixture_inputs
from autoresearch.research_harness.fixture_models import LocalEvaluationFixtureRequest
from tests.research_harness.test_local_features import Q, START, build, event, history, requests
from tests.research_harness.test_local_training import module, write_view


def test_opt_in_history_has_30_warmup_days_before_training_and_preserves_v1(tmp_path: Path) -> None:
    evaluation = date(2026, 9, 3)
    request = LocalEvaluationFixtureRequest(tmp_path, evaluation, 10501, history_days=32)
    descriptor = write_canonical_fixture_inputs(tmp_path / "v2", request)
    assert descriptor.contract_version == "youtube-ctr-local-fixture-v2"
    assert descriptor.history_start_date == date(2026, 8, 2)
    dates = canonical_fixture_dates(evaluation, history_days=32)
    assert len(dates) == 34
    assert dates == tuple(date(2026, 8, 2) + timedelta(days=i) for i in range(34))
    assert tuple(p.dt for p in descriptor.youtube_partitions) == dates
    users = pq.read_table(tmp_path / "v2" / descriptor.virtual_users.relative_path)
    assert all(str(v).startswith("2026-08-02") for v in users["generated_at"].to_pylist())
    old = write_canonical_fixture_inputs(tmp_path / "v1", LocalEvaluationFixtureRequest(tmp_path, evaluation, 10501))
    assert old.contract_version == "youtube-ctr-local-fixture-v1"
    assert len(old.youtube_partitions) == 4


@pytest.mark.parametrize("days", [0, 1, 7, 31, 33, True, 32.0])
def test_history_profile_rejects_unregistered_values(tmp_path: Path, days: object) -> None:
    with pytest.raises(StageCError):
        LocalEvaluationFixtureRequest(tmp_path, date(2026, 9, 3), 1, history_days=days)


def test_training_selection_excludes_warmup_and_uses_kst_date(tmp_path: Path) -> None:
    temporal = import_module("autoresearch.research_harness.temporal_training")
    inputs = module().load_local_training_input(write_view(tmp_path))
    # 검증된 입력의 선택 단위 테스트. 이력 receipt의 날짜 범위만 확장한다.
    start = date(2026, 8, 30)
    receipts = tuple(replace(inputs.manifest.history_partitions[0], dt=start-timedelta(days=i),
                             relative_path=f"history/action_log/dt={start-timedelta(days=i)}/part-0.parquet")
                     for i in range(30, -2, -1))
    manifest = inputs.manifest.model_copy(update={"history_partitions": receipts})
    first = inputs.training_rows[0]
    warm = replace(first, event_timestamp=datetime(2026, 8, 29, 14, 59, tzinfo=UTC))
    training = replace(first, event_timestamp=datetime(2026, 8, 29, 15, tzinfo=UTC))
    inputs = replace(inputs, manifest=manifest, training_rows=(warm, training))
    selection = temporal.select_training_window(inputs, start, start)
    assert selection.inputs.training_rows == (training,)
    assert selection.inputs.history is inputs.history
    assert selection.receipt["excluded_warmup_rows"] == 1
    assert selection.receipt["selected_rows"] == 1
    with pytest.raises(ValueError):
        temporal.select_training_window(inputs, start-timedelta(days=1), start)
    with pytest.raises(ValueError):
        temporal.select_training_window(inputs, start, start+timedelta(days=1))


def test_training_features_ignore_own_day_and_later_history() -> None:
    # 학습 요청은 평가일 이틀 전이다. 당일/다음날 행은 candidate history에 있어도 제외한다.
    training_at = Q - timedelta(days=2)
    past = event(training_at-timedelta(days=1), "view", watch_time_sec=13)
    query = requests({"event_timestamp": training_at})
    clean = build(query, history=history(past), history_start_date=START-timedelta(days=2))
    poisoned = build(query, history=history(
        past, event(training_at, "view", watch_time_sec=99999),
        event(training_at+timedelta(hours=23), "click"),
        event(training_at+timedelta(days=1), "view", watch_time_sec=99999),
    ), history_start_date=START-timedelta(days=2))
    assert clean.features.equals(poisoned.features)
    assert clean.features["recent_watch_time_7d"].to_pylist() == [13]
    assert all(clean.diagnostics["history_30d_complete"].to_pylist())


def test_v2_producer_calls_advance_in_date_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from autoresearch.research_harness import local_evaluation_fixture as fixture

    descriptor = write_canonical_fixture_inputs(
        tmp_path, LocalEvaluationFixtureRequest(tmp_path, date(2026, 9, 3), 10500, history_days=32))
    seen = []

    class StopBeforeSnapshot(Exception):
        pass

    def record(**kwargs: object) -> None:
        seen.append(kwargs["partition_date"])

    def stop(*args: object, **kwargs: object) -> None:
        raise StopBeforeSnapshot

    monkeypatch.setattr(fixture, "run_daily_action_log", record)
    monkeypatch.setattr(fixture, "_build_evaluation_snapshot", stop)
    with pytest.raises(StopBeforeSnapshot):
        fixture._build_staged_fixture(tmp_path, descriptor, "a"*64)
    assert seen == list(canonical_fixture_dates(date(2026, 9, 3), history_days=32))


def test_v2_real_fixture_reuse_and_candidate_history(tmp_path: Path) -> None:
    from autoresearch.research_harness.candidate_data_view import materialize_candidate_data_view_v2, prepare_candidate_metadata
    from autoresearch.research_harness.fixture_models import CandidateDataViewRequest
    from autoresearch.research_harness.local_evaluation_fixture import FixtureActionLogSource, build_local_evaluation_fixture

    state = tmp_path / "judge-state"
    state.mkdir()
    request = LocalEvaluationFixtureRequest(state, date(2026, 9, 3), 10500, history_days=32)
    fixture = build_local_evaluation_fixture(request)
    reused = build_local_evaluation_fixture(request)
    assert reused.reused and fixture.descriptor_sha256 == reused.descriptor_sha256
    source = FixtureActionLogSource(fixture.fixture_root, fixture.descriptor_sha256)
    destination = tmp_path / "candidate"
    destination.mkdir()
    view = materialize_candidate_data_view_v2(CandidateDataViewRequest(fixture.judge, destination),
              source=source, metadata=prepare_candidate_metadata(fixture.judge, source=source))
    inputs = module().load_local_training_input(view.root / "slate.parquet")
    temporal = import_module("autoresearch.research_harness.temporal_training")
    selection = temporal.select_training_window(inputs, date(2026, 9, 1), date(2026, 9, 1))
    assert len(inputs.manifest.history_partitions) == 32
    assert max(p.dt for p in inputs.manifest.history_partitions) == date(2026, 9, 2)
    assert selection.receipt["selected_rows"] == 4800
    assert selection.receipt["excluded_warmup_rows"] == 30*4800
