"""Candidate의 검증된 로컬 입력을 이용한 seed별 CTR 재학습.

[파이프라인] candidate v2 게시와 prediction CSV 게시 사이의 학습 구간이다.
[기능] 동일 bytes의 receipt 검증, 완전 과거 라벨, 시점 피처, seed별 분할·sampling·
LightGBM fit과 예측 및 경로 없는 재현 receipt를 제공한다.
[비책임] GPU 모델 준비는 local_embedding, CLI·산출물 게시는 prediction,
평가 라벨·지표·최종 판정은 Sealed Judge가 담당한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta, timezone
from hashlib import sha256
from importlib.metadata import version
import json
import os
from pathlib import Path
import re
import stat
from time import perf_counter
from typing import Annotated, Literal, Self

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from lightgbm.basic import LightGBMError
from sklearn.model_selection import train_test_split

from autoresearch.action_log_generation.pipeline import EVENT_LOG_PARQUET_SCHEMA, OPTIONAL_ADDITIVE_COLUMNS
from autoresearch.action_log_generation.schema import EventLog
from autoresearch.feature_engineering.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS, FeatureContractError, MODEL_FEATURE_COLUMNS,
)
from autoresearch.model_training.downsampling import downsample_negatives, apply_downsampling_calibration
from autoresearch.model_training.lgbm_model import LGBMModel
from autoresearch.research_harness.candidate_metadata import select_metadata_as_of
from autoresearch.research_harness.click_attribution import attribute_clicks
from autoresearch.research_harness.embedding import TextEmbedder
from autoresearch.research_harness.evaluation_errors import EvaluationSnapshotError
from autoresearch.research_harness.evaluation_snapshot_models import ArtifactReceipt, AttributedImpression, EvaluationWindow
from autoresearch.research_harness.evaluation_source_models import LoadedPartition, SourceEvent, SourcePartitionReceipt
from autoresearch.research_harness.fixture_errors import StageCError
from autoresearch.research_harness.fixture_models import CandidateDataManifestV2, CandidateHistoryReceipt, _MetadataArtifactReceipt
from autoresearch.research_harness.local_features import build_local_features


_KST = timezone(timedelta(hours=9))
_TS = pa.timestamp('us', tz='UTC')
_SLATE_SCHEMA = pa.schema([
    pa.field(name, pa.string(), nullable=False)
    for name in ('evaluation_id', 'slate_id', 'user_id', 'video_id')
] + [pa.field('event_timestamp', _TS, nullable=False),
     pa.field('candidate_source', pa.string()), pa.field('original_rank', pa.int64())])


class LocalTrainingConfig(BaseModel):
    """Harness baseline의 작은 모델·sampling 설정; split 비율은 고정한다."""

    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    n_estimators: Annotated[int, Field(gt=0)] = 200
    learning_rate: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 0.05
    num_leaves: Annotated[int, Field(ge=2)] = 31
    sampling_rate: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)] = 1.0
    scale_pos_weight: Literal['auto'] | Annotated[float, Field(gt=0, allow_inf_nan=False)] = 'auto'

    @model_validator(mode='after')
    def no_double_correction(self) -> Self:
        if self.sampling_rate < 1 and self.scale_pos_weight not in ('auto', 1.0):
            raise ValueError('local_training_double_correction')
        return self


@dataclass(frozen=True)
class LocalTrainingInput:
    """파일을 재조회하지 않는 검증된 candidate 입력."""

    manifest: CandidateDataManifestV2
    manifest_sha256: str
    history: pa.Table = field(repr=False)
    slate: pa.Table = field(repr=False)
    users: pa.Table = field(repr=False)
    videos: pa.Table = field(repr=False)
    training_rows: tuple[AttributedImpression, ...] = field(repr=False)


@dataclass(frozen=True)
class LocalTrainingResult:
    """게시 전 prediction·native 모델·재현 정보; metric 판정은 포함하지 않는다."""

    predictions: pa.Table
    model_text: str = field(repr=False)
    receipt: dict[str, object]


def _read_regular(path: Path) -> bytes:
    # Check every ancestor: a regular leaf beneath a junction is still an alias.
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise FeatureContractError('local_training_path_invalid')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise FeatureContractError('local_training_path_invalid')
    with path.open('rb') as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_nlink) != (info.st_dev, info.st_ino, 1):
            raise FeatureContractError('local_training_path_invalid')
        payload = handle.read()
        after = os.fstat(handle.fileno())
        current = path.lstat()
        if (after.st_size, after.st_mtime_ns, current.st_dev, current.st_ino) != (
                info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino):
            raise FeatureContractError('local_training_path_invalid')
    return payload


def _read_table(root: Path, relative: str, expected_hash: str, rows: int) -> pa.Table:
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise FeatureContractError('local_training_path_invalid')
    payload = _read_regular(path)
    if sha256(payload).hexdigest() != expected_hash:
        raise FeatureContractError('local_training_receipt_invalid')
    table = pq.ParquetFile(pa.BufferReader(payload)).read()
    if len(table) != rows or len(set(table.column_names)) != len(table.column_names):
        raise FeatureContractError('local_training_receipt_invalid')
    return table


def _check_schema(table: pa.Table, schema: pa.Schema, *, optional: set[str] | frozenset[str] = frozenset()) -> None:
    for column in schema:
        if column.name not in table.column_names:
            if column.name in optional:
                continue
            raise FeatureContractError('local_training_schema_invalid')
        if table.schema.field(column.name).type != column.type or (
            not column.nullable and table[column.name].null_count
        ):
            raise FeatureContractError('local_training_schema_invalid')


def load_local_training_input(slate: Path) -> LocalTrainingInput:
    """v2 입력 receipt·날짜·schema를 검증하고 T-2까지의 완전 라벨을 만든다.

    GPU 적재 전에 호출한다. 로컬 경로·원문 입력을 오류에 포함하지 않는다.
    """
    try:
        root = slate.absolute().parent
        if slate.name != 'slate.parquet':
            raise FeatureContractError('local_training_path_invalid')
        payload = _read_regular(root / 'candidate-view.json')
        manifest = CandidateDataManifestV2.model_validate_json(payload)
        dates = [item.dt for item in manifest.history_partitions]
        if (len(dates) < 2 or dates[-1] != manifest.evaluation_start_date - timedelta(days=1)
                or any(right - left != timedelta(days=1) for left, right in zip(dates, dates[1:]))):
            raise FeatureContractError('local_training_history_incomplete')
        def read(receipt: ArtifactReceipt | CandidateHistoryReceipt | _MetadataArtifactReceipt) -> pa.Table:
            return _read_table(root, receipt.relative_path, receipt.sha256, receipt.rows)
        slate_table = read(manifest.slate)
        _check_schema(slate_table, _SLATE_SCHEMA)
        if set(slate_table.column_names) != set(_SLATE_SCHEMA.names) or not len(slate_table):
            raise FeatureContractError('local_training_slate_invalid')
        keys: set[tuple[str, str, str]] = set()
        for row in slate_table.to_pylist():
            key = (row['evaluation_id'], row['slate_id'], row['video_id'])
            if (row['evaluation_id'] != str(manifest.evaluation_id) or key in keys
                    or any(not row[name].strip() for name in ('slate_id', 'user_id', 'video_id'))
                    or row['event_timestamp'].astimezone(_KST).date() < manifest.evaluation_start_date):
                raise FeatureContractError('local_training_slate_invalid')
            keys.add(key)
        users, videos = read(manifest.user_metadata), read(manifest.video_metadata)
        # This public selector validates complete metadata even with zero requests.
        for table, key in ((users, 'user_id'), (videos, 'video_id')):
            select_metadata_as_of(table, pa.table({key: pa.array([], type=pa.string()),
                                                 'event_timestamp': pa.array([], type=_TS)}), entity_key=key)
        history_tables: list[pa.Table] = []
        partitions: list[LoadedPartition] = []
        seen: set[str] = set()
        for receipt in manifest.history_partitions:
            table = read(receipt)
            _check_schema(table, EVENT_LOG_PARQUET_SCHEMA, optional=OPTIONAL_ADDITIVE_COLUMNS)
            history_tables.append(table)
            events: list[SourceEvent] = []
            for row in table.to_pylist():
                event = EventLog.model_validate(row)
                if (event.event_id in seen
                        or not re.fullmatch(r'evt_' + receipt.dt.strftime('%Y%m%d') + r'_\d{8}', event.event_id)
                        or event.event_timestamp.tzinfo is None
                        or event.event_timestamp.astimezone(_KST).date() != receipt.dt
                        or not event.user_id.strip() or not event.video_id.strip()
                        or not event.slate_id or not event.slate_id.strip()
                        or (event.exposure_source is not None and (event.rank is None or event.rank < 1))):
                    raise FeatureContractError('local_training_history_invalid')
                seen.add(event.event_id)
                events.append(SourceEvent(receipt.dt, event.event_id, event.event_type,
                                          event.user_id, event.video_id, event.event_timestamp,
                                          event.slate_id, event.rank, event.exposure_source, event.policy_version))
            partitions.append(LoadedPartition(SourcePartitionReceipt(receipt.dt, receipt.relative_path,
                                                                     receipt.rows, receipt.sha256), tuple(events)))
        window = EvaluationWindow(dates[0], dates[0], manifest.complete_history_label_end_date,
                                  dates[-1], manifest.complete_history_label_end_date, ())
        attributed = attribute_clicks(tuple(partitions), window)
        if not attributed:
            raise FeatureContractError('local_training_labels_empty')
        return LocalTrainingInput(manifest, sha256(payload).hexdigest(),
                                  pa.concat_tables(history_tables, promote_options='default'),
                                  slate_table, users, videos, attributed)
    except FeatureContractError:
        raise
    except (OSError, ValueError, TypeError, OverflowError, pa.ArrowException,
            ValidationError, StageCError, EvaluationSnapshotError):
        raise FeatureContractError('local_training_input_invalid') from None


def _binary(labels: pd.Series) -> None:
    if set(labels.unique()) != {0, 1}:
        raise FeatureContractError('local_training_labels_invalid')


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def train_local_candidate(
    inputs: LocalTrainingInput, *, seed: int, embedding: TextEmbedder,
    config: LocalTrainingConfig = LocalTrainingConfig(),
) -> LocalTrainingResult:
    """매번 새 LightGBM을 fit하고 고정 slate 순서로 보정된 확률을 반환한다."""
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise FeatureContractError('local_training_seed_invalid')
    started = perf_counter()
    labels = pd.Series([int(row.clicked) for row in inputs.training_rows], name='clicked')
    _binary(labels)
    positions = np.arange(len(labels))
    try:
        train_val, test = train_test_split(positions, test_size=0.2, random_state=seed, stratify=labels)
        train, validation = train_test_split(train_val, test_size=0.25, random_state=seed, stratify=labels.iloc[train_val])
    except ValueError:
        raise FeatureContractError('local_training_split_invalid') from None
    splits = {'train': train, 'validation': validation, 'test': test}
    for indexes in splits.values():
        _binary(labels.iloc[indexes])
    requests = pa.table({
        'user_id': [row.user_id for row in inputs.training_rows],
        'video_id': [row.video_id for row in inputs.training_rows],
        'event_timestamp': pa.array([row.event_timestamp for row in inputs.training_rows], type=_TS),
    })
    kwargs = dict(history=inputs.history, users=inputs.users, videos=inputs.videos,
                  embedding=embedding, evaluation_start_date=inputs.manifest.evaluation_start_date,
                  history_start_date=inputs.manifest.history_partitions[0].dt)
    training = build_local_features(requests, **kwargs)
    prediction = build_local_features(inputs.slate, **kwargs)
    frame = training.features.to_pandas()
    x_train, y_train, realized = downsample_negatives(frame.iloc[train].copy(), labels.iloc[train],
                                                   config.sampling_rate, random_state=seed)
    x_validation = frame.iloc[validation].copy()
    x_prediction = prediction.features.to_pandas()
    categories: dict[str, list[str]] = {}
    for name in CATEGORICAL_FEATURE_COLUMNS:
        vocabulary = pd.api.types.union_categoricals([x_train[name].astype('category'),
                                                      x_validation[name].astype('category')]).categories
        categories[name] = vocabulary.tolist()
        x_train[name] = pd.Categorical(x_train[name], categories=vocabulary)
        x_prediction[name] = pd.Categorical(x_prediction[name], categories=vocabulary)
    weight = (1.0 if config.sampling_rate < 1 else
              float((y_train == 0).sum() / (y_train == 1).sum()) if config.scale_pos_weight == 'auto'
              else float(config.scale_pos_weight))
    model = LGBMModel(scale_pos_weight=weight, n_estimators=config.n_estimators,
                      learning_rate=config.learning_rate, num_leaves=config.num_leaves, random_state=seed)
    try:
        model.fit(x_train, y_train, categorical_features=list(CATEGORICAL_FEATURE_COLUMNS))
        probabilities = np.asarray(model.predict_proba(x_prediction))
        if probabilities.shape != (len(inputs.slate), 2) or not np.isfinite(probabilities).all() or (
            (probabilities < 0).any() or (probabilities > 1).any()
        ):
            raise FeatureContractError('local_training_probabilities_invalid')
        scores = np.asarray(apply_downsampling_calibration(probabilities[:, 1], realized), dtype=np.float64)
        model_text = model.model.booster_.model_to_string()
    except FeatureContractError:
        raise
    except (ValueError, TypeError, RuntimeError, LightGBMError):
        raise FeatureContractError('local_training_fit_failed') from None
    output = inputs.slate.select(['evaluation_id', 'slate_id', 'video_id']).append_column('score', pa.array(scores))
    receipt: dict[str, object] = {
        'contract_version': 'local-training-v1', 'input_manifest_sha256': inputs.manifest_sha256,
        'evaluation_id': str(inputs.manifest.evaluation_id),
        'seed': seed, 'split_seed': seed, 'sampler_seed': seed, 'model_seed': seed,
        'history_start_date': str(inputs.manifest.history_partitions[0].dt),
        'complete_history_label_end_date': str(inputs.manifest.complete_history_label_end_date),
        'splits': {name: {'rows': len(indexes), 'positive_rows': int(labels.iloc[indexes].sum()),
                          'source_event_ids_sha256': _digest([inputs.training_rows[int(i)].source_event_id for i in indexes])}
                   for name, indexes in splits.items()},
        'sampling': {'nominal_rate': config.sampling_rate, 'realized_rate': realized,
                     'scale_pos_weight': weight, 'sampled_train_rows': len(y_train),
                     'source_event_ids_sha256': _digest([inputs.training_rows[int(i)].source_event_id for i in y_train.index])},
        'model_config': config.model_dump(), 'model_text_sha256': sha256(model_text.encode()).hexdigest(),
        'feature_columns': list(MODEL_FEATURE_COLUMNS), 'categorical_categories': categories,
        'feature_diagnostics': {name: {'rows': len(batch.diagnostics), **{
            column: sum(batch.diagnostics[column].to_pylist()) for column in batch.diagnostics.column_names}}
            for name, batch in (('training', training), ('prediction', prediction))},
        'versions': {name: version(name) for name in ('numpy', 'pandas', 'pyarrow', 'scikit-learn', 'lightgbm')},
        'duration_seconds': perf_counter() - started,
    }
    return LocalTrainingResult(output, model_text, receipt)
