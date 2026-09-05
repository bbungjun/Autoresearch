"""학습 bundle의 날짜 귀속·paired 정렬·무결성 회귀. 실제 fit은 하지 않는다."""

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import shutil

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sklearn.model_selection import train_test_split

from autoresearch.feature_engineering.model_contract import FeatureContractError
from autoresearch.research_harness.behavior_data import BehaviorDataRequest, KST, generate_behavior_data
from autoresearch.research_harness.behavior_data_audit import AuditEmbedding
from autoresearch.research_harness.behavior_training import (
    LABEL_SCHEMA, TRAINING_SEEDS, arm_columns, attribute_training_day,
    load_behavior_training, prepare_behavior_training, split_labels,
)
from autoresearch.research_harness.evaluation_source_models import LoadedPartition, SourceEvent, SourcePartitionReceipt


def test_midnight_and_30_minute_attribution_excludes_warmup() -> None:
    day = date(2026, 9, 2)
    at = datetime(2026, 9, 2, 14, 59, tzinfo=UTC)  # KST 9/2 23:59

    def event(identifier: str, kind: str, time: datetime, video: str) -> SourceEvent:
        return SourceEvent(time.astimezone(KST).date(),
                           identifier, kind, "u", video, time, "s", None, None, None)

    events = [event("warm", "impression", at-timedelta(days=1), "warm"),
              event("i1", "impression", at, "v1"), event("i2", "impression", at, "v2"),
              event("c1", "click", at+timedelta(minutes=30), "v1"),
              event("c2", "click", at+timedelta(minutes=30, microseconds=1), "v2")]
    partitions = tuple(LoadedPartition(SourcePartitionReceipt(d, "unused", len(group), "a"*64), tuple(group))
                       for d in (day-timedelta(days=1), day, day+timedelta(days=1))
                       if (group := [e for e in events if e.partition_date == d]))
    labels = attribute_training_day(partitions, day)
    assert dict(zip(labels["source_event_id"].to_pylist(), labels["clicked"].to_pylist())) == {"i1": 1, "i2": 0}


def test_projection_contract() -> None:
    assert len(arm_columns("with_recent")) == 15
    assert len(arm_columns("without_recent")) == 10
    assert set(arm_columns("without_recent")) < set(arm_columns("with_recent"))
    with pytest.raises(ValueError):
        arm_columns("invented")


@pytest.fixture(scope="module")
def prepared(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, str]:
    root = tmp_path_factory.mktemp("behavior-training")
    source, destination = root / "source", root / "bundle"
    generate_behavior_data(source, BehaviorDataRequest(10701))
    source_digest = sha256((source / "manifest.json").read_bytes()).hexdigest()
    prepare_behavior_training(source, destination, expected_source_sha256=source_digest,
                              embedding=AuditEmbedding(), embedding_manifest={"kind": "unit-test-only"})
    return source, destination, source_digest


def load(root: Path) -> object:
    return load_behavior_training(root, expected_manifest_sha256=sha256((root / "bundle.json").read_bytes()).hexdigest())


def test_bundle_labels_projections_and_legacy_split_parity(prepared: tuple[Path, Path, str]) -> None:
    source, root, source_digest = prepared
    bundle = load(root)
    assert bundle.labels.schema == LABEL_SCHEMA
    assert len(bundle.labels) == 1632
    assert bundle.manifest["source_manifest_sha256"] == source_digest
    assert bundle.manifest["fit_calls"] == bundle.manifest["evaluation_calls"] == 0
    assert "latent_profiles" not in (root / "bundle.json").read_text()
    assert sha256((source / "manifest.json").read_bytes()).hexdigest() == source_digest
    assert bundle.features["without_recent"].equals(bundle.features["with_recent"].select(bundle.features["without_recent"].column_names))
    y = np.array(bundle.labels["clicked"].to_pylist())
    for seed in TRAINING_SEEDS:
        train_val, test = train_test_split(np.arange(len(y)), test_size=.2, random_state=seed, stratify=y)
        train, val = train_test_split(train_val, test_size=.25, random_state=seed, stratify=y[train_val])
        expected = {"train": train.tolist(), "validation": val.tolist(), "test": test.tolist()}
        assert bundle.splits[str(seed)] == expected == split_labels(bundle.labels, seed)
        sets = [set(indices) for indices in expected.values()]
        assert set.union(*sets) == set(range(len(y)))
        assert not (sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2])


def test_source_hash_and_destination_overlap_rejected(prepared: tuple[Path, Path, str], tmp_path: Path) -> None:
    source, _, digest = prepared
    args = dict(embedding=AuditEmbedding(), embedding_manifest={"kind": "unit-test-only"})
    with pytest.raises(ValueError, match="source_manifest_mismatch"):
        prepare_behavior_training(source, tmp_path / "bad", expected_source_sha256="0"*64, **args)
    assert not (tmp_path / "bad").exists()
    with pytest.raises(ValueError, match="source_destination_overlap"):
        prepare_behavior_training(source, source / "child", expected_source_sha256=digest, **args)


@pytest.mark.parametrize("mutation", ["bytes", "row_order", "split", "nonfinite", "label_date"])
def test_bundle_rejects_corruption(prepared: tuple[Path, Path, str], tmp_path: Path, mutation: str) -> None:
    _, original, _ = prepared
    root = tmp_path / "copy"
    shutil.copytree(original, root)
    manifest = json.loads((root / "bundle.json").read_text())
    if mutation == "bytes":
        with (root / "labels.parquet").open("ab") as stream:
            stream.write(b"broken")
        expected = FeatureContractError
    elif mutation == "split":
        path = root / "splits.json"
        splits = json.loads(path.read_text())
        splits["401"]["train"][0] = splits["401"]["test"][0]
        path.write_text(json.dumps(splits))
        manifest["splits"]["sha256"] = sha256(path.read_bytes()).hexdigest()
        expected = ValueError
    else:
        receipt = manifest["labels"] if mutation == "label_date" else manifest["features"]["with_recent"]
        path = root / receipt["path"]
        table = pq.ParquetFile(path).read()
        if mutation == "row_order":
            table = table.take(list(reversed(range(len(table)))))
        else:
            rows = table.to_pylist()
            if mutation == "nonfinite":
                rows[0]["topic_similarity"] = float("nan")
            else:
                rows[0]["event_timestamp"] -= timedelta(days=1)
            table = pa.Table.from_pylist(rows, schema=table.schema)
        pq.write_table(table, path)
        receipt["sha256"] = sha256(path.read_bytes()).hexdigest()
        expected = ValueError
    (root / "bundle.json").write_text(json.dumps(manifest))
    with pytest.raises(expected):
        load(root)
