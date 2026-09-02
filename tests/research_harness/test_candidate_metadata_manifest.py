"""Task 6 candidate view v2의 manifest 계약 테스트.

[파이프라인] metadata 게시와 workspace 소비 사이의 버전·receipt 결속을 검증한다.
[기능] v1 호환성, v2 왕복, metadata identity, cutoff·경로·extra-field 거부를 검증한다.
[비책임] 실제 파일 hash 검증·게시·final 권한·checkpoint 재개는 integration 테스트 범위다.
"""

from copy import deepcopy
from hashlib import sha256

import pytest
from pydantic import BaseModel, ValidationError

from autoresearch.research_harness import fixture_models
from autoresearch.research_harness.evaluation_artifacts import canonical_json_bytes
from autoresearch.research_harness.fixture_errors import StageCError


def _v2_model() -> type[BaseModel]:
    model = getattr(fixture_models, "CandidateDataManifestV2", None)
    assert model is not None, "Task 6 RED: CandidateDataManifestV2 구현이 필요합니다"
    assert issubclass(model, BaseModel)
    return model


def _payload() -> dict:
    return {
        "contract_version": "candidate-data-view-v2",
        "evaluation_id": "eval_" + "a" * 64,
        "evaluation_start_date": "2026-09-01",
        "complete_history_label_end_date": "2026-08-30",
        "slate": {"relative_path": "slate.parquet", "rows": 24, "sha256": "b" * 64},
        "history_partitions": [{
            "dt": "2026-08-30",
            "relative_path": "history/action_log/dt=2026-08-30/part-0.parquet",
            "rows": 50, "sha256": "c" * 64,
        }],
        "metadata_contract": "candidate-metadata-v1",
        "user_metadata": {"relative_path": "metadata/users.parquet", "rows": 2, "sha256": "d" * 64},
        "video_metadata": {"relative_path": "metadata/videos.parquet", "rows": 3, "sha256": "e" * 64},
    }


def test_v1_manifest_round_trip_remains_unchanged() -> None:
    payload = _payload()
    payload["contract_version"] = "candidate-data-view-v1"
    for name in ("metadata_contract", "user_metadata", "video_metadata"):
        del payload[name]
    model = fixture_models.CandidateDataManifest.model_validate(payload)
    assert model.model_dump(mode="json") == payload


def test_v1_reader_does_not_silently_accept_v2_metadata() -> None:
    payload = _payload()
    payload["contract_version"] = "candidate-data-view-v1"
    with pytest.raises((ValidationError, StageCError)):
        fixture_models.CandidateDataManifest.model_validate(payload)


def test_v2_manifest_round_trip_is_exact_and_preserves_evaluation_identity() -> None:
    payload = _payload()
    model = _v2_model().model_validate(payload)
    assert model.model_dump(mode="json") == payload
    assert _v2_model().model_validate_json(model.model_dump_json()) == model


@pytest.mark.parametrize("artifact", ["user_metadata", "video_metadata"])
def test_changed_metadata_changes_view_digest_not_evaluation_id(artifact: str) -> None:
    original = _v2_model().model_validate(_payload()).model_dump(mode="json")
    changed = deepcopy(original)
    changed[artifact]["sha256"] = "f" * 64
    modified = _v2_model().model_validate(changed).model_dump(mode="json")
    assert original["evaluation_id"] == modified["evaluation_id"]
    assert sha256(canonical_json_bytes(original)).digest() != sha256(canonical_json_bytes(modified)).digest()


@pytest.mark.parametrize("field", ["metadata_contract", "user_metadata", "video_metadata"])
def test_v2_requires_each_metadata_field(field: str) -> None:
    model = _v2_model()
    model.model_validate(_payload())
    payload = _payload()
    del payload[field]
    with pytest.raises((ValidationError, StageCError)):
        model.model_validate(payload)


@pytest.mark.parametrize("field,value", [
    ("contract_version", "candidate-data-view-v1"),
    ("metadata_contract", "candidate-metadata-v2"),
    ("complete_history_label_end_date", "2026-08-31"),
    ("evaluation_id", "invalid"),
    ("fixture_seed", 17), ("judge_root", "DO_NOT_EXPORT_JUDGE"),
    ("final_holdout_id", "eval_" + "f" * 64),
])
def test_v2_rejects_wrong_contract_cutoff_and_extra_fields(field: str, value: object) -> None:
    model = _v2_model()
    model.model_validate(_payload())
    payload = _payload()
    payload[field] = value
    with pytest.raises((ValidationError, StageCError)):
        model.model_validate(payload)


@pytest.mark.parametrize("artifact", ["user_metadata", "video_metadata"])
@pytest.mark.parametrize("field,value", [
    ("relative_path", "../outside.parquet"), ("relative_path", "/outside.parquet"),
    ("relative_path", "metadata/wrong.parquet"), ("relative_path", "metadata\\users.parquet"),
    ("rows", -1), ("rows", True), ("sha256", "f" * 63), ("sha256", "A" * 64),
    ("sha256", "z" * 64),
    ("rows", "2"), ("rows", 2.0), ("sha256", "f" * 64 + "\n"),
    ("source_uri", "DO_NOT_EXPORT_SOURCE"),
])
def test_metadata_receipt_rejects_bad_path_rows_and_digest(
    artifact: str, field: str, value: object,
) -> None:
    model = _v2_model()
    model.model_validate(_payload())
    payload = _payload()
    payload[artifact][field] = value
    with pytest.raises((ValidationError, StageCError)):
        model.model_validate(payload)


def test_v2_does_not_admit_evaluation_day_action_log() -> None:
    model = _v2_model()
    model.model_validate(_payload())
    payload = _payload()
    payload["history_partitions"][0].update({
        "dt": "2026-09-01",
        "relative_path": "history/action_log/dt=2026-09-01/part-0.parquet",
    })
    with pytest.raises((ValidationError, StageCError)):
        model.model_validate(payload)
