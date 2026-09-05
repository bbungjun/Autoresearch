"""Candidate 이력과 모델 fit 사이에서 warm-up과 학습 날짜를 분리한다.

[파이프라인] 검증된 local training input을 읽은 후 피처 조립·모델 학습 전에 적용한다.
[기능] 완전한 선행 30일을 요구하고 지정한 KST 날짜의 학습 행만 선택한다.
과거 이벤트 테이블은 그대로 유지하며 선택 기간과 행 hash를 재현 receipt로 제공한다.
[비책임] 원시 파일 검증·click 귀속은 local_training, 피처 cutoff는 local_features,
모델 학습·Sealed Judge 평가와 final 소비는 각 실행기의 책임이다.
"""

from dataclasses import dataclass, replace
from datetime import date, timedelta, timezone
from hashlib import sha256
import json

from autoresearch.research_harness.local_training import LocalTrainingInput


_KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class TemporalTrainingSelection:
    inputs: LocalTrainingInput
    receipt: dict[str, object]


def select_training_window(
    inputs: LocalTrainingInput, start: date, end: date,
) -> TemporalTrainingSelection:
    """검증된 입력에서 선행 30일 뒤의 완전 귀속 학습 날짜를 선택한다.

    warm-up은 피처 계산에 보존되지만 fit 대상 impression에서 제외된다.
    이 함수는 학습/평가 입력 파일이나 원본 receipt를 수정하지 않는다.
    """
    dates = tuple(p.dt for p in inputs.manifest.history_partitions)
    if (type(start) is not date or type(end) is not date or not dates or start > end
            or (start-dates[0]).days < 30
            or end > inputs.manifest.complete_history_label_end_date
            or any(b-a != timedelta(days=1) for a, b in zip(dates, dates[1:]))):
        raise ValueError("temporal_training_window_invalid")
    selected = tuple(row for row in inputs.training_rows
                     if start <= row.event_timestamp.astimezone(_KST).date() <= end)
    if not selected:
        raise ValueError("temporal_training_rows_empty")
    ids = [row.source_event_id for row in selected]
    digest = sha256(json.dumps(ids, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return TemporalTrainingSelection(replace(inputs, training_rows=selected), {
        "contract_version": "temporal-training-selection-v1", "timezone": "Asia/Seoul",
        "history_start_date": str(dates[0]), "training_start_date": str(start),
        "training_end_date": str(end), "required_warmup_days": 30,
        "complete_history_label_end_date": str(inputs.manifest.complete_history_label_end_date),
        "input_manifest_sha256": inputs.manifest_sha256,
        "available_training_rows": len(inputs.training_rows), "selected_rows": len(selected),
        "excluded_warmup_rows": sum(row.event_timestamp.astimezone(_KST).date() < start
                                    for row in inputs.training_rows),
        "selected_source_event_ids_sha256": digest,
    })
