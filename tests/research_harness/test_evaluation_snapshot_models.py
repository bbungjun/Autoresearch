from dataclasses import FrozenInstanceError, fields
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from autoresearch.research_harness import evaluation_snapshot_models
from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.evaluation_source_models import SourcePartitionReceipt


def test_request_maps_invalid_date_range_to_typed_error() -> None:
    with pytest.raises(EvaluationSnapshotError) as raised:
        evaluation_snapshot_models.EvaluationSnapshotRequest(
            action_log_root="memory://action-log",
            history_start_date=date(2026, 9, 1),
            evaluation_start_date=date(2026, 9, 1),
            evaluation_end_date=date(2026, 9, 2),
            slate_id_cutover_date=date(2026, 9, 1),
            output_root=Path("output"),
        )

    assert raised.value.code is SnapshotErrorCode.INVALID_DATE_RANGE


def test_request_is_frozen_after_valid_date_range() -> None:
    request = evaluation_snapshot_models.EvaluationSnapshotRequest(
        action_log_root="memory://action-log",
        history_start_date=date(2026, 8, 31),
        evaluation_start_date=date(2026, 9, 1),
        evaluation_end_date=date(2026, 9, 2),
        slate_id_cutover_date=date(2026, 9, 1),
        output_root=Path("output"),
    )

    with pytest.raises(FrozenInstanceError):
        request.action_log_root = "memory://changed"


def test_writer_options_are_exactly_ten_frozen_contract_fields() -> None:
    options = evaluation_snapshot_models.WriterOptions(
        version="2.6",
        coerce_timestamps="us",
        allow_truncated_timestamps=False,
        use_deprecated_int96_timestamps=False,
        compression="NONE",
        use_dictionary=False,
        row_group_size=50000,
        write_statistics=True,
        data_page_version="1.0",
        store_schema=True,
    )

    assert [field.name for field in fields(options)] == [
        "version",
        "coerce_timestamps",
        "allow_truncated_timestamps",
        "use_deprecated_int96_timestamps",
        "compression",
        "use_dictionary",
        "row_group_size",
        "write_statistics",
        "data_page_version",
        "store_schema",
    ]
    with pytest.raises(FrozenInstanceError):
        options.version = "2.4"


def test_manifest_serializes_utc_timestamp_and_dates_as_contract_json() -> None:
    receipt = SourcePartitionReceipt(
        dt=date(2026, 9, 1),
        uri="memory://action-log/dt=2026-09-01/part-0.parquet",
        rows=1,
        sha256="a" * 64,
    )
    artifacts = evaluation_snapshot_models.SplitArtifacts(
        slate=evaluation_snapshot_models.ArtifactReceipt(
            relative_path="validation/slate.parquet", rows=1, sha256="b" * 64
        ),
        labels=evaluation_snapshot_models.ArtifactReceipt(
            relative_path="validation/labels.parquet", rows=1, sha256="c" * 64
        ),
    )
    summary = evaluation_snapshot_models.SplitSummary(
        evaluation_id=evaluation_snapshot_models.EvaluationId("eval_" + "d" * 64),
        counts=evaluation_snapshot_models.SplitCounts(1, 1, 1, 0, 0, 0.0, 1.0),
        optional_non_null_ratio={"candidate_source": 1.0, "original_rank": 1.0},
        artifacts=artifacts,
    )
    manifest = evaluation_snapshot_models.EvaluationSnapshotManifest(
        contract_version="evaluation-slate-snapshot-v1",
        snapshot_fingerprint=evaluation_snapshot_models.SnapshotFingerprint("e" * 64),
        created_at=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
        source=evaluation_snapshot_models.SnapshotSource("memory://action-log", (receipt,), date(2026, 9, 1)),
        window=evaluation_snapshot_models.EvaluationWindow(
            date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 1), date(2026, 9, 2), date(2026, 8, 30), (receipt,)
        ),
        attribution=evaluation_snapshot_models.AttributionContract(
            "click-attribution-v1", 1800, ("event_timestamp_desc", "source_event_id_desc")
        ),
        split=evaluation_snapshot_models.SplitContract(
            "user-hash-80-20-v1", "research-harness-slate-v1:", tuple(range(8)), (8, 9)
        ),
        writer=evaluation_snapshot_models.WriterIdentity(
            "pyarrow",
            "21.0.0",
            evaluation_snapshot_models.WriterOptions("2.6", "us", False, False, "NONE", False, 50000, True, "1.0", True),
        ),
        validation=summary,
        final_holdout=summary,
    )

    encoded = manifest.model_dump(mode="json")

    assert encoded["created_at"] == "2026-09-01T00:00:00.000000Z"
    assert encoded["source"]["slate_id_cutover_date"] == "2026-09-01"


def test_manifest_forbids_nested_extra_fields_and_is_frozen() -> None:
    receipt = SourcePartitionReceipt(date(2026, 9, 1), "memory://source", 1, "a" * 64)
    summary = evaluation_snapshot_models.SplitSummary(
        evaluation_snapshot_models.EvaluationId("eval_" + "d" * 64),
        evaluation_snapshot_models.SplitCounts(1, 1, 1, 0, 0, 0.0, 1.0),
        {"candidate_source": 1.0, "original_rank": 1.0},
        evaluation_snapshot_models.SplitArtifacts(
            evaluation_snapshot_models.ArtifactReceipt("validation/slate.parquet", 1, "b" * 64),
            evaluation_snapshot_models.ArtifactReceipt("validation/labels.parquet", 1, "c" * 64),
        ),
    )
    manifest = evaluation_snapshot_models.EvaluationSnapshotManifest(
        contract_version="evaluation-slate-snapshot-v1",
        snapshot_fingerprint=evaluation_snapshot_models.SnapshotFingerprint("e" * 64),
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        source=evaluation_snapshot_models.SnapshotSource("memory://source", (receipt,), date(2026, 9, 1)),
        window=evaluation_snapshot_models.EvaluationWindow(date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 1), date(2026, 9, 2), date(2026, 8, 30), (receipt,)),
        attribution=evaluation_snapshot_models.AttributionContract("click-attribution-v1", 1800, ("event_timestamp_desc", "source_event_id_desc")),
        split=evaluation_snapshot_models.SplitContract("user-hash-80-20-v1", "research-harness-slate-v1:", tuple(range(8)), (8, 9)),
        writer=evaluation_snapshot_models.WriterIdentity("pyarrow", "21.0.0", evaluation_snapshot_models.WriterOptions("2.6", "us", False, False, "NONE", False, 50000, True, "1.0", True)),
        validation=summary,
        final_holdout=summary,
    )
    payload = manifest.model_dump(mode="json")
    payload["source"]["unexpected"] = "forbidden"

    with pytest.raises(ValidationError):
        evaluation_snapshot_models.EvaluationSnapshotManifest.model_validate(payload)
    with pytest.raises(ValidationError):
        manifest.snapshot_fingerprint = evaluation_snapshot_models.SnapshotFingerprint("f" * 64)


def test_all_internal_snapshot_value_models_are_frozen() -> None:
    model_types = (
        evaluation_snapshot_models.SnapshotSource,
        evaluation_snapshot_models.EvaluationWindow,
        evaluation_snapshot_models.AttributedImpression,
        evaluation_snapshot_models.EvaluationSplit,
        evaluation_snapshot_models.AttributionContract,
        evaluation_snapshot_models.SplitContract,
        evaluation_snapshot_models.WriterIdentity,
        evaluation_snapshot_models.ArtifactReceipt,
        evaluation_snapshot_models.SplitArtifacts,
        evaluation_snapshot_models.SplitCounts,
        evaluation_snapshot_models.SplitSummary,
        evaluation_snapshot_models.SnapshotArtifactInput,
        evaluation_snapshot_models.EvaluationSnapshotReceipt,
    )

    assert all(model_type.__dataclass_params__.frozen for model_type in model_types)


def test_manifest_rejects_naive_created_at() -> None:
    receipt = SourcePartitionReceipt(date(2026, 9, 1), "memory://source", 1, "a" * 64)
    summary = evaluation_snapshot_models.SplitSummary(
        evaluation_snapshot_models.EvaluationId("eval_" + "d" * 64),
        evaluation_snapshot_models.SplitCounts(1, 1, 1, 0, 0, 0.0, 1.0),
        {"candidate_source": 1.0, "original_rank": 1.0},
        evaluation_snapshot_models.SplitArtifacts(
            evaluation_snapshot_models.ArtifactReceipt("validation/slate.parquet", 1, "b" * 64),
            evaluation_snapshot_models.ArtifactReceipt("validation/labels.parquet", 1, "c" * 64),
        ),
    )

    with pytest.raises(EvaluationSnapshotError):
        evaluation_snapshot_models.EvaluationSnapshotManifest(
            contract_version="evaluation-slate-snapshot-v1",
            snapshot_fingerprint=evaluation_snapshot_models.SnapshotFingerprint("e" * 64),
            created_at=datetime(2026, 9, 1),
            source=evaluation_snapshot_models.SnapshotSource("memory://source", (receipt,), date(2026, 9, 1)),
            window=evaluation_snapshot_models.EvaluationWindow(date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 1), date(2026, 9, 2), date(2026, 8, 30), (receipt,)),
            attribution=evaluation_snapshot_models.AttributionContract("click-attribution-v1", 1800, ("event_timestamp_desc", "source_event_id_desc")),
            split=evaluation_snapshot_models.SplitContract("user-hash-80-20-v1", "research-harness-slate-v1:", tuple(range(8)), (8, 9)),
            writer=evaluation_snapshot_models.WriterIdentity("pyarrow", "21.0.0", evaluation_snapshot_models.WriterOptions("2.6", "us", False, False, "NONE", False, 50000, True, "1.0", True)),
            validation=summary,
            final_holdout=summary,
        )


def test_manifest_ratio_detaches_input_alias_and_rejects_mutation() -> None:
    ratio = {"candidate_source": 1.0, "original_rank": 1.0}
    receipt = SourcePartitionReceipt(date(2026, 9, 1), "memory://source", 1, "a" * 64)
    summary = evaluation_snapshot_models.SplitSummary(
        evaluation_snapshot_models.EvaluationId("eval_" + "d" * 64),
        evaluation_snapshot_models.SplitCounts(1, 1, 1, 0, 0, 0.0, 1.0),
        ratio,
        evaluation_snapshot_models.SplitArtifacts(
            evaluation_snapshot_models.ArtifactReceipt("validation/slate.parquet", 1, "b" * 64),
            evaluation_snapshot_models.ArtifactReceipt("validation/labels.parquet", 1, "c" * 64),
        ),
    )
    manifest = evaluation_snapshot_models.EvaluationSnapshotManifest(
        contract_version="evaluation-slate-snapshot-v1",
        snapshot_fingerprint=evaluation_snapshot_models.SnapshotFingerprint("e" * 64),
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        source=evaluation_snapshot_models.SnapshotSource("memory://source", (receipt,), date(2026, 9, 1)),
        window=evaluation_snapshot_models.EvaluationWindow(date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 1), date(2026, 9, 2), date(2026, 8, 30), (receipt,)),
        attribution=evaluation_snapshot_models.AttributionContract("click-attribution-v1", 1800, ("event_timestamp_desc", "source_event_id_desc")),
        split=evaluation_snapshot_models.SplitContract("user-hash-80-20-v1", "research-harness-slate-v1:", tuple(range(8)), (8, 9)),
        writer=evaluation_snapshot_models.WriterIdentity("pyarrow", "21.0.0", evaluation_snapshot_models.WriterOptions("2.6", "us", False, False, "NONE", False, 50000, True, "1.0", True)),
        validation=summary,
        final_holdout=summary,
    )
    before = manifest.model_dump(mode="json")
    ratio["candidate_source"] = 0.0

    assert manifest.model_dump(mode="json") == before
    with pytest.raises(ValidationError):
        summary.optional_non_null_ratio.candidate_source = 0.0


def test_optional_non_null_ratio_rejects_numeric_strings() -> None:
    with pytest.raises(ValidationError):
        evaluation_snapshot_models.OptionalNonNullRatio(
            candidate_source="0.5",
            original_rank=0.5,
        )
