"""P0-2B validation-only prediction 계약과 Judge scoring 테스트."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from hashlib import sha256
import inspect
from pathlib import Path

import pytest

import autoresearch.research_harness as research_harness
import autoresearch.research_harness.judge as judge_module
from autoresearch.research_harness.evaluation_artifacts import write_snapshot_artifacts
from autoresearch.research_harness.evaluation_snapshot_models import (
    AttributedImpression,
    EvaluationSnapshotRequest,
    EvaluationSplit,
    EvaluationWindow,
    SnapshotArtifactInput,
)
from autoresearch.research_harness.evaluation_source_models import SourcePartitionReceipt
from autoresearch.research_harness.judge import (
    JudgeError,
    JudgeErrorCode,
    JudgeEvaluationTarget,
    build_validation_target,
    parse_prediction_copy,
    score_predictions,
)
from autoresearch.research_harness.local_evaluation_fixture import (
    _validated_judge_handoff,
)
from autoresearch.research_harness.snapshot_publisher import publish_snapshot


def _source_receipt(day: date, rows: int) -> SourcePartitionReceipt:
    return SourcePartitionReceipt(
        dt=day,
        uri=f"memory://judge/dt={day.isoformat()}/part-0.parquet",
        rows=rows,
        sha256=f"{day.day:064x}",
    )


def _impression(
    *,
    split: str,
    video_id: str,
    clicked: bool,
) -> AttributedImpression:
    return AttributedImpression(
        slate_id=f"{split}-slate",
        user_id=f"{split}-user",
        video_id=video_id,
        event_timestamp=datetime(2026, 9, 1, 0, int(video_id[-1]), tzinfo=UTC),
        source_event_id=f"evt_20260901_0000000{video_id[-1]}",
        clicked=clicked,
        original_rank=int(video_id[-1]),
        candidate_source="model",
    )


def _artifact_input() -> SnapshotArtifactInput:
    days = (date(2026, 8, 30), date(2026, 8, 31), date(2026, 9, 1))
    partitions = tuple(
        _source_receipt(day, rows=index)
        for index, day in enumerate(days, start=1)
    )
    request = EvaluationSnapshotRequest(
        action_log_root="memory://judge",
        history_start_date=days[0],
        evaluation_start_date=days[-1],
        evaluation_end_date=days[-1],
        slate_id_cutover_date=days[0],
        output_root=Path("unused"),
    )
    window = EvaluationWindow(
        history_start_date=days[0],
        evaluation_start_date=days[-1],
        evaluation_end_date=days[-1],
        label_scan_end_date=date(2026, 9, 2),
        complete_history_label_end_date=days[0],
        candidate_history_partitions=partitions[:2],
    )
    validation_rows = (
        _impression(split="validation", video_id="video-1", clicked=True),
        _impression(split="validation", video_id="video-2", clicked=False),
    )
    final_rows = (
        _impression(split="final", video_id="video-1", clicked=True),
        _impression(split="final", video_id="video-2", clicked=False),
    )
    return SnapshotArtifactInput(
        request=request,
        window=window,
        partitions=partitions,
        validation=EvaluationSplit(
            name="validation",
            rows=validation_rows,
            user_ids=("validation-user",),
        ),
        final_holdout=EvaluationSplit(
            name="final_holdout",
            rows=final_rows,
            user_ids=("final-user",),
        ),
        created_at=datetime(2026, 9, 2, tzinfo=UTC),
    )


@pytest.fixture
def judge_handoff(tmp_path: Path):
    staging = tmp_path / "staging"
    staging.mkdir()
    manifest = write_snapshot_artifacts(staging, _artifact_input())
    receipt = publish_snapshot(staging, tmp_path / "by-hash", manifest)
    return _validated_judge_handoff(
        receipt.target_path,
        expected_fingerprint=str(receipt.snapshot_fingerprint),
    )


def _prediction_bytes(
    evaluation_id: str,
    rows: tuple[tuple[str, str, str], ...] = (
        ("validation-slate", "video-1", "0.9"),
        ("validation-slate", "video-2", "0.1"),
    ),
    *,
    newline: bytes = b"\n",
) -> bytes:
    lines = [b"evaluation_id,slate_id,video_id,score"]
    lines.extend(
        f"{evaluation_id},{slate_id},{video_id},{score}".encode("ascii")
        for slate_id, video_id, score in rows
    )
    return newline.join(lines) + newline


def _write_predictions(
    tmp_path: Path,
    evaluation_id: str,
    rows: tuple[tuple[str, str, str], ...] = (
        ("validation-slate", "video-1", "0.9"),
        ("validation-slate", "video-2", "0.1"),
    ),
) -> Path:
    path = tmp_path / "predictions.csv"
    path.write_bytes(_prediction_bytes(evaluation_id, rows))
    return path


def test_public_interface_keeps_target_and_parser_types_opaque() -> None:
    assert "build_validation_target" in research_harness.__all__
    assert "score_predictions" in research_harness.__all__
    assert "JudgeEvaluationTarget" not in research_harness.__all__
    assert "PredictionRow" not in research_harness.__all__
    assert "parse_prediction_copy" not in research_harness.__all__
    assert not hasattr(judge_module, "build_final_target")
    assert inspect.signature(build_validation_target).return_annotation == (
        "JudgeEvaluationTarget"
    )


def test_target_rejects_direct_construction() -> None:
    with pytest.raises(JudgeError) as error:
        JudgeEvaluationTarget()

    assert error.value.code is JudgeErrorCode.INVALID_TARGET


def test_validation_target_rejects_forged_handoff(judge_handoff) -> None:
    forged = replace(judge_handoff, validation_id="eval_" + "0" * 64)

    with pytest.raises(JudgeError) as error:
        build_validation_target(forged)

    assert error.value.code is JudgeErrorCode.INVALID_TARGET


def test_parser_accepts_lf_and_crlf_canonical_rows(
    judge_handoff,
    tmp_path: Path,
) -> None:
    for newline in (b"\n", b"\r\n"):
        path = tmp_path / f"predictions-{len(newline)}.csv"
        path.write_bytes(_prediction_bytes(str(judge_handoff.validation_id), newline=newline))

        rows = parse_prediction_copy(path)

        assert len(rows) == 2
        assert rows[0].evaluation_id == judge_handoff.validation_id
        assert rows[0].score == pytest.approx(0.9)


@pytest.mark.parametrize(
    "payload",
    [
        b"slate_id,evaluation_id,video_id,score\n",
        b"evaluation_id,slate_id,video_id,score\n\"eval_bad\",slate,video,0.5\n",
        (
            b"evaluation_id,slate_id,video_id,score\n"
            + b"eval_"
            + b"a" * 64
            + b",slate,video,NaN\n"
        ),
        (
            b"evaluation_id,slate_id,video_id,score\n"
            + b"eval_"
            + b"a" * 64
            + b",slate,video,1.1\n"
        ),
        (
            b"evaluation_id,slate_id,video_id,score\n"
            + b"eval_"
            + b"a" * 64
            + b","
            + b"s" * 65
            + b",video,0.5\n"
        ),
        (
            b"evaluation_id,slate_id,video_id,score\n"
            + b"eval_"
            + b"a" * 64
            + ",slate,비디오,0.5\n".encode()
        ),
    ],
)
def test_parser_rejects_noncanonical_schema_or_fields(
    tmp_path: Path,
    payload: bytes,
) -> None:
    path = tmp_path / "predictions.csv"
    path.write_bytes(payload)

    with pytest.raises(JudgeError) as error:
        parse_prediction_copy(path)

    assert error.value.code is JudgeErrorCode.INVALID_PREDICTIONS


def test_parser_rejects_more_than_300_000_rows(tmp_path: Path) -> None:
    path = tmp_path / "predictions.csv"
    evaluation_id = b"eval_" + b"a" * 64
    row = evaluation_id + b",slate,video,0.5\n"
    path.write_bytes(b"evaluation_id,slate_id,video_id,score\n" + row * 300_001)

    with pytest.raises(JudgeError) as error:
        parse_prediction_copy(path)

    assert error.value.code is JudgeErrorCode.INVALID_PREDICTIONS


@pytest.mark.parametrize(
    "rows",
    [
        (("validation-slate", "video-1", "0.9"),),
        (
            ("validation-slate", "video-1", "0.9"),
            ("validation-slate", "video-1", "0.1"),
        ),
        (
            ("validation-slate", "video-1", "0.9"),
            ("validation-slate", "video-2", "0.1"),
            ("validation-slate", "video-extra", "0.5"),
        ),
    ],
)
def test_scoring_rejects_missing_duplicate_or_extra_prediction_keys(
    judge_handoff,
    tmp_path: Path,
    rows: tuple[tuple[str, str, str], ...],
) -> None:
    target = build_validation_target(judge_handoff)
    path = _write_predictions(tmp_path, str(judge_handoff.validation_id), rows)

    with pytest.raises(JudgeError) as error:
        score_predictions(target, path)

    assert error.value.code is JudgeErrorCode.INVALID_PREDICTIONS


def test_prediction_cannot_select_the_final_holdout_id(
    judge_handoff,
    tmp_path: Path,
) -> None:
    target = build_validation_target(judge_handoff)
    path = _write_predictions(tmp_path, str(judge_handoff.final_holdout_id))

    with pytest.raises(JudgeError) as error:
        score_predictions(target, path)

    assert error.value.code is JudgeErrorCode.INVALID_PREDICTIONS


def test_scoring_combines_ranking_and_probability_metrics(
    judge_handoff,
    tmp_path: Path,
) -> None:
    target = build_validation_target(judge_handoff)
    path = _write_predictions(tmp_path, str(judge_handoff.validation_id))

    result = score_predictions(target, path)

    assert result.evaluation_id == judge_handoff.validation_id
    assert result.row_count == 2
    assert result.ndcg_at_10.value == pytest.approx(1.0)
    assert result.recall_at_10.value == pytest.approx(1.0)
    assert result.ndcg_at_24.value == pytest.approx(1.0)
    assert result.probability.roc_auc == pytest.approx(1.0)
    assert result.probability.pr_auc == pytest.approx(1.0)
    assert result.probability.log_loss == pytest.approx(0.10536051565782628)
    assert result.probability.brier == pytest.approx(0.01)
    assert result.probability.grouped_roc_auc is not None
    assert result.probability.grouped_roc_auc.value == pytest.approx(1.0)


def test_scoring_revalidates_target_artifact_after_target_creation(
    judge_handoff,
    tmp_path: Path,
) -> None:
    target = build_validation_target(judge_handoff)
    labels = judge_handoff.snapshot_root / "validation" / "labels.parquet"
    labels.write_bytes(labels.read_bytes() + b"tampered")
    path = _write_predictions(tmp_path, str(judge_handoff.validation_id))

    with pytest.raises(JudgeError) as error:
        score_predictions(target, path)

    assert error.value.code is JudgeErrorCode.INVALID_TARGET


def test_judge_error_does_not_disclose_prediction_or_target_values(
    judge_handoff,
    tmp_path: Path,
) -> None:
    target = build_validation_target(judge_handoff)
    path = _write_predictions(tmp_path, "eval_" + "f" * 64)

    with pytest.raises(JudgeError) as error:
        score_predictions(target, path)

    rendered = str(error.value)
    assert str(path) not in rendered
    assert str(judge_handoff.snapshot_root) not in rendered
    assert "eval_" not in rendered
    assert "video-" not in rendered
    assert sha256(path.read_bytes()).hexdigest() not in rendered
