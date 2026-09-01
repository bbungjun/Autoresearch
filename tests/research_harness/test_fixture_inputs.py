from datetime import date
from hashlib import sha256

import pyarrow.parquet as pq
import pytest

from autoresearch.action_log_generation.daily import _read_virtual_users
from autoresearch.action_log_generation.video_source import load_video_records
from autoresearch.data_collection.load import _SCHEMA as TRENDING_VIDEO_SCHEMA
from autoresearch.research_harness.evaluation_artifacts import WRITER_OPTIONS, canonical_json_bytes
from autoresearch.research_harness.evaluation_split import user_bucket
from autoresearch.research_harness.fixture_inputs import (
    canonical_fixture_dates,
    descriptor_sha256,
    select_fixture_user_ids,
    write_canonical_fixture_inputs,
)
from autoresearch.research_harness.fixture_models import LocalEvaluationFixtureRequest
from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode
from autoresearch.virtual_user_generation.pipeline import VIRTUAL_USERS_PARQUET_SCHEMA


def test_fixture_dates_cover_history_evaluation_and_scan_tail() -> None:
    assert canonical_fixture_dates(date(2026, 9, 1)) == (
        date(2026, 8, 30),
        date(2026, 8, 31),
        date(2026, 9, 1),
        date(2026, 9, 2),
    )


def test_user_selection_is_seeded_deterministic_and_exactly_matches_split_buckets() -> None:
    validation, final_holdout = select_fixture_user_ids(917)

    assert len(validation) == 160
    assert len(final_holdout) == 40
    assert len(set(validation) | set(final_holdout)) == 200
    assert all(user_bucket(user_id) < 8 for user_id in validation)
    assert all(user_bucket(user_id) >= 8 for user_id in final_holdout)
    assert select_fixture_user_ids(917) == (validation, final_holdout)
    assert select_fixture_user_ids(918) != (validation, final_holdout)


def test_user_selection_rejects_negative_seed_with_typed_error() -> None:
    with pytest.raises(StageCError) as raised:
        select_fixture_user_ids(-1)

    assert raised.value.code is StageCErrorCode.FIXTURE_REQUEST_INVALID


def test_canonical_inputs_use_production_schemas_and_exact_descriptor(tmp_path) -> None:
    fixture_root = tmp_path / "fixture"
    request = LocalEvaluationFixtureRequest(tmp_path, date(2026, 9, 1), 917)

    descriptor = write_canonical_fixture_inputs(fixture_root, request)

    assert descriptor.history_start_date == date(2026, 8, 30)
    assert descriptor.slate_id_cutover_date == date(2026, 8, 30)
    assert descriptor.evaluation_start_date == date(2026, 9, 1)
    assert descriptor.evaluation_end_date == date(2026, 9, 1)
    assert descriptor.input_writer.options == WRITER_OPTIONS
    assert descriptor.virtual_users.relative_path == "inputs/virtual_users.parquet"
    assert descriptor.virtual_users.rows == 200
    assert len(descriptor.youtube_partitions) == 4
    assert [receipt.dt for receipt in descriptor.youtube_partitions] == list(
        canonical_fixture_dates(date(2026, 9, 1))
    )
    assert all(receipt.rows == 48 for receipt in descriptor.youtube_partitions)

    users_path = fixture_root / descriptor.virtual_users.relative_path
    assert pq.read_schema(users_path).equals(VIRTUAL_USERS_PARQUET_SCHEMA)
    users = _read_virtual_users(str(users_path))
    assert len(users) == 200
    assert sum(user_bucket(row["user_id"]) < 8 for row in users) == 160
    assert sum(user_bucket(row["user_id"]) >= 8 for row in users) == 40

    for receipt in descriptor.youtube_partitions:
        partition_path = fixture_root / receipt.relative_path
        assert pq.read_schema(partition_path).equals(TRENDING_VIDEO_SCHEMA)
        assert len(load_video_records(partition_path)) == 48

    descriptor_bytes = (fixture_root / "fixture.json").read_bytes()
    assert descriptor_bytes == canonical_json_bytes(descriptor.model_dump(mode="json"))
    assert descriptor_sha256(descriptor) == sha256(descriptor_bytes).hexdigest()


def test_same_seed_and_date_produce_identical_input_order_and_bytes(tmp_path) -> None:
    request = LocalEvaluationFixtureRequest(tmp_path, date(2026, 9, 1), 123456)
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"

    first = write_canonical_fixture_inputs(first_root, request)
    second = write_canonical_fixture_inputs(second_root, request)

    assert first == second
    relative_paths = (
        "fixture.json",
        first.virtual_users.relative_path,
        *(receipt.relative_path for receipt in first.youtube_partitions),
    )
    assert all(
        (first_root / relative_path).read_bytes()
        == (second_root / relative_path).read_bytes()
        for relative_path in relative_paths
    )


def test_different_seed_changes_user_input_and_descriptor_identity(tmp_path) -> None:
    first = write_canonical_fixture_inputs(
        tmp_path / "first",
        LocalEvaluationFixtureRequest(tmp_path, date(2026, 9, 1), 1),
    )
    second = write_canonical_fixture_inputs(
        tmp_path / "second",
        LocalEvaluationFixtureRequest(tmp_path, date(2026, 9, 1), 2),
    )

    assert first.virtual_users.sha256 != second.virtual_users.sha256
    assert descriptor_sha256(first) != descriptor_sha256(second)
