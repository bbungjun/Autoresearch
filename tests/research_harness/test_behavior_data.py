"""다양한 행동 데이터의 재현성·시간 계약과 실제 피처 분산 회귀."""

from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pyarrow.parquet as pq
import pyarrow as pa
import pytest

from autoresearch.research_harness.behavior_data import (
    BehaviorDataRequest, daily_drafts, generate_behavior_data,
)
from autoresearch.research_harness.behavior_data_audit import audit_behavior_data
from autoresearch.research_harness.fixture_inputs import _fixture_video_rows, select_fixture_user_ids
from autoresearch.research_harness.fixture_errors import StageCError


@pytest.mark.parametrize("seed", [-1, True, 1.0, "1"])
def test_invalid_seed(seed: object) -> None:
    with pytest.raises(ValueError):
        BehaviorDataRequest(seed)


@pytest.mark.parametrize("day", [date.max, date.min])
def test_invalid_date(day: date) -> None:
    with pytest.raises((ValueError, StageCError)):
        BehaviorDataRequest(1, day)


def test_drafts_are_order_independent_seeded_and_active_days_vary() -> None:
    request = BehaviorDataRequest(10701)
    users = list(sum(select_fixture_user_ids(request.seed), ()))
    videos = _fixture_video_rows(request.training_date)
    expected = daily_drafts(request, request.training_date, users, videos)
    assert expected == daily_drafts(request, request.training_date, users[::-1], videos[::-1])
    assert expected != daily_drafts(BehaviorDataRequest(10702), request.training_date, users, videos)
    counts = {}
    for draft in expected:
        counts[draft.user_id] = counts.get(draft.user_id, 0) + 1
    assert set(counts.values()) == {8, 16, 24}
    assert 0 < len(counts) < len(users)
    with pytest.raises(ValueError):
        daily_drafts(request, request.training_date + timedelta(days=2), users, videos)


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("behavior") / "world"
    generate_behavior_data(root, BehaviorDataRequest(10701))
    return root


def test_real_files_have_variable_recent_features_and_interest_shift(generated: Path) -> None:
    audit = audit_behavior_data(generated)
    assert audit["quality_passed"]
    assert all(audit["checks"].values())
    assert audit["generated_dates"][0] == "2026-08-03"
    assert audit["generated_dates"][-1] == "2026-09-03"
    assert len(audit["generated_dates"]) == 32
    assert audit["final_evaluations"] == 0
    assert audit["interest_shift"]["changing"]["observed_both_sides"] >= 20
    assert not (generated / "action_log" / "dt=2026-09-04").exists()


def test_full_generation_is_reproducible_and_cannot_overwrite(generated: Path, tmp_path: Path) -> None:
    second = tmp_path / "reproduced"
    original = json.loads((generated / "manifest.json").read_text())
    duplicate = generate_behavior_data(second, BehaviorDataRequest(10701))
    assert original == duplicate
    assert (generated / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()
    for partition in original["partitions"]:
        path = partition["events"]["path"]
        assert (generated / path).read_bytes() == (second / path).read_bytes()
    with pytest.raises(FileExistsError):
        generate_behavior_data(second, BehaviorDataRequest(10701))


def test_audit_rejects_wrong_date_order(generated: Path, tmp_path: Path) -> None:
    # 날짜 순서는 파일을 읽기 전에 검사한다.
    manifest = json.loads((generated / "manifest.json").read_text())
    manifest["partitions"].reverse()
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="noncanonical_partition_dates"):
        audit_behavior_data(tmp_path)
    user_path = generated / "inputs/virtual_users.parquet"
    assert pq.ParquetFile(user_path).read().num_rows == 200


@pytest.mark.parametrize("mutation", ["hash", "missing", "profile", "same_timestamp"])
def test_audit_rejects_tampering(generated: Path, tmp_path: Path, mutation: str) -> None:
    copied = tmp_path / "copied"
    shutil.copytree(generated, copied)
    manifest = json.loads((copied / "manifest.json").read_text())
    path = copied / manifest["partitions"][0]["events"]["path"]
    if mutation == "hash":
        with path.open("ab") as stream:
            stream.write(b"tampered")
        expected, match = ValueError, "partition_hash_mismatch"
    elif mutation == "missing":
        path.unlink()
        expected, match = FileNotFoundError, None
    elif mutation == "profile":
        manifest["latent_profiles"][0]["changes_interest"] = not manifest["latent_profiles"][0]["changes_interest"]
        (copied / "manifest.json").write_text(json.dumps(manifest))
        expected, match = ValueError, "invalid_behavior_manifest"
    else:
        table = pq.ParquetFile(path).read()
        rows = table.to_pylist()
        click = next(row for row in rows if row["event_type"] == "click")
        impression = next(row for row in rows if row["event_type"] == "impression"
                          and (row["user_id"], row["video_id"]) == (click["user_id"], click["video_id"]))
        click["event_timestamp"] = impression["event_timestamp"]
        pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
        manifest["partitions"][0]["events"]["sha256"] = sha256(path.read_bytes()).hexdigest()
        (copied / "manifest.json").write_text(json.dumps(manifest))
        expected, match = ValueError, "non_increasing_attribution_time"
    with pytest.raises(expected, match=match):
        audit_behavior_data(copied)
