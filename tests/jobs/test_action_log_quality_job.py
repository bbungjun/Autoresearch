import json
from datetime import UTC, datetime

import pyarrow as pa
import pytest

import autoresearch.jobs.action_log_quality as quality_job
from autoresearch.action_log_generation.pipeline import EVENT_LOG_PARQUET_SCHEMA


_ARGS = [
    "--partition-date",
    "2026-07-13",
    "--youtube-base-path",
    "gs://test-bucket/data_lake/youtube_trending_kr",
    "--virtual-users-path",
    "gs://test-bucket/asset/virtual_user/vu_1000.parquet",
    "--action-log-base-path",
    "gs://test-bucket/data_lake/action_log",
    "--expected-model",
    "test-model",
]


def _event(event_type: str, *, watch_time_sec=None) -> dict[str, object]:
    return {
        "event_id": f"evt-{event_type}",
        "event_timestamp": datetime(2026, 7, 13, tzinfo=UTC),
        "user_id": "vu-1",
        "event_type": event_type,
        "video_id": "video-1",
        "watch_time_sec": watch_time_sec,
        "rank": None,
        "source": "historical",
        "schema_version": "action_log_schema_v1",
        "prompt_version": "prompt-v1",
        "llm_model": "test-model",
        "generated_at": "2026-07-13T00:00:00+00:00",
    }


def _valid_rows() -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
]:
    return (
        [{"video_id": "video-1"}],
        [
            _event("impression"),
            _event("click"),
            _event("view", watch_time_sec=10),
        ],
        [{"user_id": "vu-1"}],
    )


def _json_lines(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line]


def test_valid_rows_pass_legacy_references_and_final_schema():
    youtube_rows, action_rows, virtual_user_rows = _valid_rows()

    summary = quality_job.summarize_rows(
        youtube_rows, action_rows, virtual_user_rows
    )
    summary.update(
        quality_job.summarize_final_schema(
            action_rows,
            EVENT_LOG_PARQUET_SCHEMA,
        )
    )

    assert quality_job.validate_summary(summary, expected_model="test-model") == []
    assert summary["event_type_counts"] == {
        "click": 1,
        "impression": 1,
        "view": 1,
    }
    assert summary["ctr"] == 1.0


def test_quality_detects_reference_model_and_schema_failures():
    youtube_rows, action_rows, virtual_user_rows = _valid_rows()
    action_rows[0]["video_id"] = "missing-video"
    action_rows[0]["user_id"] = "missing-user"
    action_rows[1]["llm_model"] = "other-model"
    action_rows[2]["watch_time_sec"] = None
    mismatched_schema = EVENT_LOG_PARQUET_SCHEMA.remove(
        EVENT_LOG_PARQUET_SCHEMA.get_field_index("prompt_version")
    ).set(
        EVENT_LOG_PARQUET_SCHEMA.get_field_index("event_timestamp"),
        pa.field("event_timestamp", pa.string()),
    )

    summary = quality_job.summarize_rows(
        youtube_rows, action_rows, virtual_user_rows
    )
    summary.update(
        quality_job.summarize_final_schema(action_rows, mismatched_schema)
    )
    errors = quality_job.validate_summary(summary, expected_model="expected-model")

    assert summary["action_video_ids_missing_from_youtube"] == 1
    assert summary["action_user_ids_missing_from_virtual_users"] == 1
    assert summary["action_schema_invalid_rows"] == 1
    assert summary["action_schema_missing_columns"] == ["prompt_version"]
    assert summary["action_schema_type_mismatches"] == ["event_timestamp"]
    assert "expected llm_model expected-model not found" in errors


@pytest.mark.parametrize("optional_column", ("exposure_source", "slate_id"))
def test_quality_treats_additive_columns_as_optional_for_legacy_partitions(
    optional_column,
):
    legacy_schema = pa.schema(
        [f for f in EVENT_LOG_PARQUET_SCHEMA if f.name != optional_column]
    )
    summary = quality_job.summarize_final_schema([], legacy_schema)
    assert optional_column not in summary["action_schema_missing_columns"]


def test_run_resolves_canonical_partition_paths(monkeypatch):
    youtube_rows, action_rows, virtual_user_rows = _valid_rows()
    paths = []

    def fake_read(path):
        paths.append(path)
        if "youtube_trending" in path:
            return youtube_rows, pa.schema([pa.field("video_id", pa.string())])
        if "action_log/dt=" in path:
            return action_rows, EVENT_LOG_PARQUET_SCHEMA
        return virtual_user_rows, pa.schema([pa.field("user_id", pa.string())])

    monkeypatch.setattr(quality_job, "read_parquet", fake_read)
    args = quality_job._build_parser().parse_args(_ARGS)
    quality_job._validate_args(args)

    result = quality_job._run(args)

    assert result["errors"] == []
    assert paths == [
        (
            "gs://test-bucket/data_lake/youtube_trending_kr/"
            "dt=2026-07-13/part-0.parquet"
        ),
        "gs://test-bucket/data_lake/action_log/dt=2026-07-13/part-0.parquet",
        "gs://test-bucket/asset/virtual_user/vu_1000.parquet",
    ]


def test_main_maps_quality_errors_to_exit_1(monkeypatch, capsys):
    monkeypatch.setattr(
        quality_job,
        "_run",
        lambda args: {
            "quality": {"action_rows": 0},
            "errors": ["action log parquet has no rows"],
        },
    )

    assert quality_job.main(_ARGS) == 1

    summary = _json_lines(capsys.readouterr().out)[-1]
    assert summary["status"] == "failed"
    assert summary["job"] == "action_log_quality"
    assert summary["errors"] == ["action log parquet has no rows"]


def test_main_emits_success_summary(monkeypatch, capsys):
    monkeypatch.setattr(
        quality_job,
        "_run",
        lambda args: {"quality": {"action_rows": 3}, "errors": []},
    )

    assert quality_job.main(_ARGS) == 0
    assert _json_lines(capsys.readouterr().out)[-1]["status"] == "succeeded"


def test_main_rejects_noncanonical_path_before_read(monkeypatch, capsys):
    monkeypatch.setattr(quality_job, "_run", lambda args: pytest.fail("must not run"))
    args = list(_ARGS)
    args[args.index("--action-log-base-path") + 1] = "C:/local/action_log"

    assert quality_job.main(args) == 2
    assert _json_lines(capsys.readouterr().out)[-1]["error_type"] == (
        "invalid_arguments"
    )


def test_main_hides_runtime_failure_detail(monkeypatch, capsys, caplog):
    def fail(args):
        raise RuntimeError("token=secret user_id=vu-1")

    monkeypatch.setattr(quality_job, "_run", fail)

    assert quality_job.main(_ARGS) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err + caplog.text
    assert "secret" not in combined
    assert "vu-1" not in combined
    assert _json_lines(captured.out)[-1]["error_type"] == "runtime_failure"


def test_version_reports_revision_and_contract(monkeypatch, capsys):
    monkeypatch.setattr(quality_job, "_REVISION", "abc123")

    with pytest.raises(SystemExit) as exc_info:
        quality_job._build_parser().parse_args(["--version"])

    assert exc_info.value.code == 0
    assert json.loads(capsys.readouterr().out) == {
        "application_revision": "abc123",
        "contract_version": "batch-contract-v1",
    }
