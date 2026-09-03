"""로컬 학습 입력의 추가 피처·receipt·native 모델 정합성 검증."""

from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from autoresearch.feature_engineering.model_contract import FeatureContractError, MODEL_FEATURE_COLUMNS
from autoresearch.research_harness.local_features import LocalFeatureBatch
from tests.research_harness.test_local_training import Embedding, module, write_view


def test_extra_feature_reaches_actual_fit_predict_native_model_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = module()
    loaded = m.load_local_training_input(write_view(tmp_path))
    build = m.build_local_features
    fit, predict = m.LGBMModel.fit, m.LGBMModel.predict_proba
    observed: dict[str, pd.DataFrame] = {}

    def extra(requests: pa.Table, **kwargs: object) -> LocalFeatureBatch:
        batch = build(requests, **kwargs)
        # Synthetic input-contract probe, not the actual experiment's feature.
        values = pa.array(np.arange(len(requests), dtype=np.float64) / 10)
        return LocalFeatureBatch(batch.features.append_column('synthetic_extra', values), batch.diagnostics)

    def capture_fit(self: object, x: pd.DataFrame, y: pd.Series, **kwargs: object) -> None:
        observed['fit'] = x.copy()
        fit(self, x, y, **kwargs)

    def capture_predict(self: object, x: pd.DataFrame) -> np.ndarray:
        observed['predict'] = x.copy()
        return predict(self, x)

    monkeypatch.setattr(m, 'build_local_features', extra)
    monkeypatch.setattr(m.LGBMModel, 'fit', capture_fit)
    monkeypatch.setattr(m.LGBMModel, 'predict_proba', capture_predict)
    result = m.train_local_candidate(loaded, seed=7, embedding=Embedding(), config=m.LocalTrainingConfig(n_estimators=3))
    expected = [*MODEL_FEATURE_COLUMNS, 'synthetic_extra']
    booster = lgb.Booster(model_str=result.model_text)
    assert observed['fit'].columns.tolist() == observed['predict'].columns.tolist() == expected
    assert observed['fit'].dtypes.equals(observed['predict'].dtypes)
    assert booster.feature_name() == result.receipt['feature_columns'] == expected
    np.testing.assert_allclose(booster.predict(observed['predict']), result.predictions['score'].to_numpy())


@pytest.mark.parametrize('mutation', [
    'missing_base', 'reordered_base', 'reordered_prediction', 'dtype_mismatch',
    'duplicate_extra', 'base_collision', 'null', 'nan', 'inf', 'negative_inf', 'string', 'bool',
    'user_id', 'video_id', 'slate_id', 'evaluation_id', 'event_id', 'source_event_id',
    'event_timestamp', 'clicked', 'label', 'score', 'candidate_source', 'original_rank',
    'user_metadata_missing', 'custom_diagnostic',
])
def test_malformed_feature_batches_fail_before_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str,
) -> None:
    m = module()
    loaded = m.load_local_training_input(write_view(tmp_path))
    build = m.build_local_features
    calls = 0

    def malformed(requests: pa.Table, **kwargs: object) -> LocalFeatureBatch:
        nonlocal calls
        calls += 1
        batch = build(requests, **kwargs)
        table, diagnostics = batch.features, batch.diagnostics
        if mutation == 'missing_base':
            table = table.drop(['topic_similarity'])
        elif mutation == 'reordered_base':
            table = table.select(list(reversed(table.column_names)))
        elif mutation == 'reordered_prediction':
            table = table.append_column('extra_a', pa.array([1.0] * len(table)))
            table = table.append_column('extra_b', pa.array([2.0] * len(table)))
            if calls == 2:
                table = table.select([*MODEL_FEATURE_COLUMNS, 'extra_b', 'extra_a'])
        elif mutation == 'dtype_mismatch':
            table = table.append_column('extra', pa.array([1] * len(table), type=pa.int64() if calls == 1 else pa.float64()))
        else:
            values = {'null': None, 'nan': np.nan, 'inf': np.inf, 'negative_inf': -np.inf,
                      'string': 'value', 'bool': True}
            name = 'extra' if mutation in values or mutation == 'duplicate_extra' else mutation
            if mutation == 'base_collision':
                name = 'topic_similarity'
            if mutation == 'custom_diagnostic':
                diagnostics = diagnostics.append_column(name, pa.array([False] * len(table)))
            value = values.get(mutation, 1.0)
            array = pa.array([value] * len(table), type=pa.float64() if value is None else None)
            table = table.append_column(name, array)
            if mutation == 'duplicate_extra':
                table = table.append_column(name, array)
        return LocalFeatureBatch(table, diagnostics)

    def no_fit(*args: object, **kwargs: object) -> None:
        pytest.fail('Malformed feature inputs reached model.fit')

    monkeypatch.setattr(m, 'build_local_features', malformed)
    monkeypatch.setattr(m.LGBMModel, 'fit', no_fit)
    with pytest.raises(FeatureContractError, match='^local_training_features_'):
        m.train_local_candidate(loaded, seed=7, embedding=Embedding(), config=m.LocalTrainingConfig(n_estimators=3))
