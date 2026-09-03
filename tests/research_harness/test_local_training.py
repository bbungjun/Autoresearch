"""Candidate 로컬 재학습의 손 계산 라벨과 입력·seed 계약 검증."""

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from importlib import import_module
from importlib.util import find_spec
import json
import os
from pathlib import Path
from types import ModuleType

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from autoresearch.action_log_generation.pipeline import EVENT_LOG_PARQUET_SCHEMA
from autoresearch.feature_engineering.model_contract import FeatureContractError, MODEL_FEATURE_COLUMNS
from tests.research_harness.metadata_cases import normalized_users, normalized_videos
from tests.research_harness.test_workspace import candidate_fixture as candidate_fixture


T = date(2026, 9, 1)
TS = pa.timestamp('us', tz='UTC')


def module() -> ModuleType:
    name = 'autoresearch.research_harness.local_training'
    assert find_spec(name), 'RED: local_training 구현이 필요합니다'
    return import_module(name)


class Embedding:
    def encode(self, texts: list[str], *, role: str) -> np.ndarray:
        return np.tile([0.6, 0.8], (len(texts), 1))


def event(at: datetime, kind: str = 'impression', *, slate: str = 's', user: str = 'u', video: str = 'v') -> dict[str, object]:
    return dict(event_timestamp=at, event_type=kind, user_id=user, video_id=video,
                watch_time_sec=None, source='historical', slate_id=slate)


def write_view(root: Path, events: list[dict[str, object]] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 8, 29, 15, tzinfo=UTC)
    if events is None:
        events = []
        for i in range(80):
            at = start + timedelta(minutes=i)
            events.append(event(at, slate=f's-{i}', user=f'u-{i}'))
            if i % 2:
                events.append(event(at + timedelta(seconds=1), 'click', slate=f's-{i}', user=f'u-{i}'))
    receipts = []
    for day in (T - timedelta(days=2), T - timedelta(days=1)):
        rows = [dict(row, event_id=f'evt_{day:%Y%m%d}_{i:08d}') for i, row in enumerate(events)
                if (row['event_timestamp'] + timedelta(hours=9)).date() == day]
        path = f'history/action_log/dt={day}/part-0.parquet'
        receipts.append(dict(dt=str(day), **write_table(root, path, pa.Table.from_pylist(rows, schema=EVENT_LOG_PARQUET_SCHEMA))))
    evaluation_id = 'eval_' + 'a' * 64
    slate = pa.table(dict(evaluation_id=[evaluation_id], slate_id=['eval-s'], user_id=['u-1'],
                          video_id=['v'], event_timestamp=pa.array([datetime(2026, 9, 1, tzinfo=UTC)], type=TS),
                          candidate_source=pa.array([None], type=pa.string()), original_rank=pa.array([None], type=pa.int64())))
    manifest = dict(contract_version='candidate-data-view-v2', evaluation_id=evaluation_id,
                    evaluation_start_date=str(T), complete_history_label_end_date=str(T - timedelta(days=2)),
                    history_partitions=receipts, slate=write_table(root, 'slate.parquet', slate),
                    metadata_contract='candidate-metadata-v1',
                    user_metadata=write_table(root, 'metadata/users.parquet', normalized_users()),
                    video_metadata=write_table(root, 'metadata/videos.parquet', normalized_videos()))
    (root / 'candidate-view.json').write_text(json.dumps(manifest), encoding='utf-8')
    return root / 'slate.parquet'


def write_table(root: Path, relative: str, table: pa.Table) -> dict[str, object]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    return dict(relative_path=relative, rows=len(table), sha256=sha256(path.read_bytes()).hexdigest())


def test_golden_latest_strict_prior_and_midnight_complete_labels(tmp_path: Path) -> None:
    at = datetime(2026, 8, 30, 14, 50, tzinfo=UTC)  # KST 8/30 23:50
    rows = [event(at, slate='old'), event(at + timedelta(minutes=15), 'click', slate='old'),
            event(at, slate='same', user='same'), event(at, 'click', slate='same', user='same'),
            event(at - timedelta(minutes=20), slate='edge', user='edge'),
            event(at + timedelta(minutes=10), 'click', slate='edge', user='edge'),
            event(at, slate='before', user='steal'),
            event(at + timedelta(minutes=11), slate='after', user='steal'),
            event(at + timedelta(minutes=12), 'click', slate='after', user='steal')]
    loaded = module().load_local_training_input(write_view(tmp_path, rows))
    assert {row.user_id: row.clicked for row in loaded.training_rows} == {
        'u': True, 'same': False, 'edge': True, 'steal': False,
    }
    assert len(loaded.training_rows) == 4


@pytest.mark.parametrize('mutation', ['hash', 'rows', 'v1', 'gap', 'missing_slate', 'duplicate', 'timestamp'])
def test_bad_input_rejected_before_training(tmp_path: Path, mutation: str) -> None:
    path = write_view(tmp_path)
    manifest_path = tmp_path / 'candidate-view.json'
    manifest = json.loads(manifest_path.read_text())
    if mutation == 'hash':
        manifest['slate']['sha256'] = '0' * 64
    elif mutation == 'rows':
        manifest['slate']['rows'] += 1
    elif mutation == 'v1':
        manifest['contract_version'] = 'candidate-data-view-v1'
    elif mutation == 'gap':
        manifest['history_partitions'].pop()
    else:
        receipt = manifest['history_partitions'][0]
        table = pq.ParquetFile(tmp_path / receipt['relative_path']).read()
        rows = table.to_pylist()
        if mutation == 'missing_slate':
            rows[0]['slate_id'] = None
        elif mutation == 'duplicate':
            rows[1]['event_id'] = rows[0]['event_id']
        else:
            rows[0]['event_timestamp'] += timedelta(days=1)
        manifest['history_partitions'][0] = dict(dt=receipt['dt'], **write_table(tmp_path, receipt['relative_path'], pa.Table.from_pylist(rows, schema=table.schema)))
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(FeatureContractError, match='^local_training_'):
        module().load_local_training_input(path)


def test_actual_lightgbm_seed_retraining_and_receipt(tmp_path: Path) -> None:
    m = module()
    loaded = m.load_local_training_input(write_view(tmp_path))
    config = m.LocalTrainingConfig(n_estimators=3)
    results = [m.train_local_candidate(loaded, seed=seed, embedding=Embedding(), config=config) for seed in (7, 7, 19)]
    first, repeated, other = results
    assert first.predictions.column_names == ['evaluation_id', 'slate_id', 'video_id', 'score']
    assert first.predictions.equals(repeated.predictions)
    assert first.model_text == repeated.model_text
    assert first.receipt['splits'] == repeated.receipt['splits']
    assert first.receipt['splits'] != other.receipt['splits']
    assert [first.receipt['splits'][name]['rows'] for name in ('train', 'validation', 'test')] == [48, 16, 16]
    assert first.receipt['model_text_sha256'] == sha256(first.model_text.encode()).hexdigest()
    assert first.receipt['input_manifest_sha256'] == loaded.manifest_sha256
    assert first.receipt['sampling']['realized_rate'] == 1.0
    assert str(tmp_path) not in json.dumps(first.receipt)
    assert first.receipt['feature_columns'] == list(MODEL_FEATURE_COLUMNS)


@pytest.mark.parametrize('seed', [-1, 2**32, True])
def test_invalid_seed(tmp_path: Path, seed: int) -> None:
    m = module()
    loaded = m.load_local_training_input(write_view(tmp_path))
    with pytest.raises(FeatureContractError, match='local_training_seed_invalid'):
        m.train_local_candidate(loaded, seed=seed, embedding=Embedding(), config=m.LocalTrainingConfig())


def test_sampling_corrects_probabilities_and_refits_each_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = module()
    loaded = m.load_local_training_input(write_view(tmp_path))
    calls = []
    original = m.LGBMModel.fit

    def fit(self: object, x: object, y: object, **kwargs: object) -> None:
        calls.append((self, x.copy(), y.copy()))
        original(self, x, y, **kwargs)

    monkeypatch.setattr(m.LGBMModel, 'fit', fit)
    monkeypatch.setattr(m.LGBMModel, 'predict_proba', lambda self, x: np.tile([0.25, 0.75], (len(x), 1)))
    config = m.LocalTrainingConfig(n_estimators=2, sampling_rate=0.5)
    first = m.train_local_candidate(loaded, seed=7, embedding=Embedding(), config=config)
    second = m.train_local_candidate(loaded, seed=7, embedding=Embedding(), config=config)
    assert len(calls) == 2 and calls[0][0] is not calls[1][0]
    assert calls[0][1].columns.tolist() == list(MODEL_FEATURE_COLUMNS)
    assert len(calls[0][2]) == 36
    assert first.receipt['sampling']['realized_rate'] == 0.5
    assert first.receipt['sampling']['scale_pos_weight'] == 1.0
    assert first.predictions['score'].to_pylist() == [0.6]  # .75/(.75+.25/.5)
    assert first.predictions.equals(second.predictions)


@pytest.mark.parametrize('value', [np.nan, np.inf, -0.1, 1.1])
def test_invalid_prediction_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: float) -> None:
    m = module()
    loaded = m.load_local_training_input(write_view(tmp_path))
    monkeypatch.setattr(m.LGBMModel, 'predict_proba', lambda self, x: np.tile([0.5, value], (len(x), 1)))
    with pytest.raises(FeatureContractError, match='local_training_probabilities_invalid'):
        m.train_local_candidate(loaded, seed=7, embedding=Embedding(), config=m.LocalTrainingConfig(n_estimators=2))


@pytest.mark.parametrize('config', [dict(sampling_rate=0.0), dict(sampling_rate=float('nan')),
                                   dict(scale_pos_weight=0.0), dict(n_estimators=True),
                                   dict(sampling_rate=0.5, scale_pos_weight=2.0)])
def test_invalid_configuration(config: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        module().LocalTrainingConfig(**config)


def test_hardlinked_input_is_rejected(tmp_path: Path) -> None:
    path = write_view(tmp_path)
    os.link(path, tmp_path / 'alias.parquet')
    with pytest.raises(FeatureContractError, match='local_training_path_invalid'):
        module().load_local_training_input(path)


def test_input_is_not_read_again_after_loading(tmp_path: Path) -> None:
    m = module()
    path = write_view(tmp_path)
    loaded = m.load_local_training_input(path)
    path.write_bytes(b'changed after load')
    result = m.train_local_candidate(loaded, seed=7, embedding=Embedding(), config=m.LocalTrainingConfig(n_estimators=2))
    assert len(result.predictions) == 1


def test_published_fixture_real_lightgbm_and_native_model(candidate_fixture: object, tmp_path: Path) -> None:
    import lightgbm as lgb
    import pandas as pd
    from autoresearch.research_harness.candidate_data_view import materialize_candidate_data_view_v2, prepare_candidate_metadata
    from autoresearch.research_harness.fixture_models import CandidateDataViewRequest
    from autoresearch.research_harness.local_features import build_local_features

    m = module()
    fixture, source = candidate_fixture
    metadata = prepare_candidate_metadata(fixture.judge, source=source)
    view = materialize_candidate_data_view_v2(CandidateDataViewRequest(fixture.judge, tmp_path), source=source, metadata=metadata)
    loaded = m.load_local_training_input(view.root / 'slate.parquet')
    result = m.train_local_candidate(loaded, seed=42, embedding=Embedding(), config=m.LocalTrainingConfig(n_estimators=3))
    assert len(result.predictions) == len(loaded.slate) > 0
    features = build_local_features(loaded.slate, history=loaded.history, users=loaded.users, videos=loaded.videos,
                                   embedding=Embedding(), evaluation_start_date=loaded.manifest.evaluation_start_date,
                                   history_start_date=loaded.manifest.history_partitions[0].dt).features.to_pandas()
    for column, values in result.receipt['categorical_categories'].items():
        features[column] = pd.Categorical(features[column], categories=values)
    restored = lgb.Booster(model_str=result.model_text).predict(features)
    np.testing.assert_allclose(restored, result.predictions['score'].to_numpy(), atol=1e-12)
    assert result.receipt['feature_diagnostics']['training']['history_7d_complete'] == 0


def test_external_training_error_is_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lightgbm.basic import LightGBMError

    m = module()
    loaded = m.load_local_training_input(write_view(tmp_path))

    def fail(*args: object, **kwargs: object) -> None:
        raise LightGBMError('private-path-and-secret')

    monkeypatch.setattr(m.LGBMModel, 'fit', fail)
    with pytest.raises(FeatureContractError) as error:
        m.train_local_candidate(loaded, seed=7, embedding=Embedding(), config=m.LocalTrainingConfig())
    assert str(error.value) == 'local_training_fit_failed'
    assert 'private' not in repr(error.value)


@pytest.mark.parametrize('count', [0, 1, 2])
def test_empty_single_class_and_too_small_training_fail(tmp_path: Path, count: int) -> None:
    m = module()
    at = datetime(2026, 8, 30, tzinfo=UTC)
    rows = [event(at, user=f'u-{i}', slate=f's-{i}') for i in range(count)]
    if count == 2:
        rows.append(event(at + timedelta(seconds=1), 'click', user='u-1', slate='s-1'))
    path = write_view(tmp_path, rows)
    with pytest.raises(FeatureContractError, match='^local_training_'):
        loaded = m.load_local_training_input(path)
        m.train_local_candidate(loaded, seed=7, embedding=Embedding(), config=m.LocalTrainingConfig())


def test_attribution_tie_chooses_largest_id_and_duplicate_click_is_binary(tmp_path: Path) -> None:
    at = datetime(2026, 8, 30, tzinfo=UTC)
    rows = [event(at), event(at), event(at + timedelta(seconds=1), 'click'),
            event(at + timedelta(seconds=2), 'click')]
    loaded = module().load_local_training_input(write_view(tmp_path, rows))
    assert [(row.source_event_id, row.clicked) for row in loaded.training_rows] == [
        ('evt_20260830_00000000', False), ('evt_20260830_00000001', True),
    ]
