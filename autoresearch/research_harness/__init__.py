"""재현 가능한 평가 snapshot 패키지.

[파이프라인] action log 일일 파티션과 P0-2 Sealed Judge 사이에서 평가용
label-free slate와 Judge 전용 label artifact를 조립하는 경계를 담당한다.

[기능] Stage B의 공개 요청·receipt·error·source seam과 snapshot builder를 제공한다.

[비책임] action log 생성(autoresearch.action_log_generation), 후보 학습·실행과
metric/Judge 판정(P0-2 이후)을 담당하지 않는다.
"""

from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.evaluation_snapshot_models import (
    EvaluationSnapshotReceipt,
    EvaluationSnapshotRequest,
)
from autoresearch.research_harness.evaluation_source import ActionLogSource
from autoresearch.research_harness.slate import build_evaluation_snapshot


__all__ = [
    "ActionLogSource",
    "EvaluationSnapshotError",
    "EvaluationSnapshotReceipt",
    "EvaluationSnapshotRequest",
    "SnapshotErrorCode",
    "build_evaluation_snapshot",
]
