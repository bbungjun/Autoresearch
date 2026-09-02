"""재현 가능한 평가 snapshot과 validation Judge scoring 패키지.

[파이프라인] action log 일일 파티션과 P0-2 Sealed Judge 사이에서 평가용
label-free slate와 Judge 전용 label artifact를 조립하는 경계를 담당한다.

[기능] Stage B의 공개 요청·receipt·error·source seam과 snapshot builder, Stage C의
fixture/candidate handoff typed contract, canonical identity helper, P0-2A ranking metric,
P0-2B scoring, P0-2C 봉인 ingestion·판정, P0-2D domain interface, final 소비 registry와
append-only Trial Ledger, 고정 candidate 명령을 실행·회수하는 LocalRunner를 제공한다.

[비책임] action log 생성(autoresearch.action_log_generation), candidate 코드 변경·학습 구현,
실제 baseline sigma 측정과 반복 Controller를 담당하지 않는다.
"""

from autoresearch.research_harness.evaluation_errors import (
    EvaluationSnapshotError,
    SnapshotErrorCode,
)
from autoresearch.research_harness.domain import (
    DomainError,
    DomainErrorCode,
    ResearchDomain,
    YouTubeCTRDomain,
)
from autoresearch.research_harness.evaluation_snapshot_models import (
    EvaluationSnapshotReceipt,
    EvaluationSnapshotRequest,
)
from autoresearch.research_harness.evaluation_source import ActionLogSource
from autoresearch.research_harness.candidate_data_view import (
    materialize_candidate_data_view,
)
from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode
from autoresearch.research_harness.fixture_inputs import (
    canonical_fixture_dates,
    descriptor_sha256,
    select_fixture_user_ids,
)
from autoresearch.research_harness.fixture_models import (
    CandidateDataManifest,
    CandidateDataViewReceipt,
    CandidateDataViewRequest,
    CandidateHistoryReceipt,
    FixtureDescriptor,
    FixtureInputReceipt,
    FixturePartitionReceipt,
    JudgeSnapshotHandoff,
    LocalEvaluationFixtureReceipt,
    LocalEvaluationFixtureRequest,
)
from autoresearch.research_harness.local_evaluation_fixture import (
    build_local_evaluation_fixture,
)
from autoresearch.research_harness.consumption_registry import (
    ConsumptionRegistryError,
    ConsumptionRegistryErrorCode,
    FinalConsumptionEvidence,
    FinalConsumptionGrant,
    FinalConsumptionRequest,
    claim_final_consumption,
)
from autoresearch.research_harness.ledger import (
    CheckpointRecord,
    LedgerAppendReceipt,
    LedgerArtifactEvidence,
    LedgerError,
    LedgerErrorCode,
    LedgerMetric,
    TrialLedger,
    TrialLedgerState,
    TrialRecord,
    open_trial_ledger,
)
from autoresearch.research_harness.judge import (
    JudgeError,
    JudgeErrorCode,
    JudgeScoringResult,
    build_final_target,
    build_validation_target,
    score_predictions,
)
from autoresearch.research_harness.judge_decision import (
    ConfirmationDecision,
    JudgeDecision,
    JudgeMetric,
    JudgeReasonCode,
    MetricDelta,
    PairedJudgeResult,
    ScreeningResult,
    compare_confirmation,
    screen_candidate,
)
from autoresearch.research_harness.prediction_ingestion import (
    SealedPredictionReceipt,
    seal_prediction_copy,
)
from autoresearch.research_harness.ranking_metrics import (
    RankingMetricError,
    RankingMetricErrorCode,
    RankingMetricResult,
    ndcg_at_k,
    recall_at_k,
)
from autoresearch.research_harness.slate import build_evaluation_snapshot
from autoresearch.research_harness.workspace import (
    CandidateChangeReceipt,
    CandidateProcessContext,
    CandidateWorkspace,
    CandidateWorkspaceRequest,
    WorkspaceError,
    WorkspaceErrorCode,
    open_candidate_workspace,
)
from autoresearch.research_harness.runner import (
    LocalRunReceipt,
    LocalRunRequest,
    LocalRunner,
    RunnerError,
    RunnerErrorCode,
)


__all__ = [
    "ActionLogSource",
    "CandidateDataManifest",
    "CandidateDataViewReceipt",
    "CandidateDataViewRequest",
    "CandidateHistoryReceipt",
    "CandidateChangeReceipt",
    "CandidateProcessContext",
    "CandidateWorkspace",
    "CandidateWorkspaceRequest",
    "CheckpointRecord",
    "ConfirmationDecision",
    "ConsumptionRegistryError",
    "ConsumptionRegistryErrorCode",
    "DomainError",
    "DomainErrorCode",
    "EvaluationSnapshotError",
    "EvaluationSnapshotReceipt",
    "EvaluationSnapshotRequest",
    "FixtureDescriptor",
    "FixtureInputReceipt",
    "FixturePartitionReceipt",
    "FinalConsumptionEvidence",
    "FinalConsumptionGrant",
    "FinalConsumptionRequest",
    "JudgeSnapshotHandoff",
    "JudgeError",
    "JudgeErrorCode",
    "JudgeDecision",
    "JudgeMetric",
    "JudgeReasonCode",
    "JudgeScoringResult",
    "LedgerAppendReceipt",
    "LedgerArtifactEvidence",
    "LedgerError",
    "LedgerErrorCode",
    "LedgerMetric",
    "LocalEvaluationFixtureReceipt",
    "LocalEvaluationFixtureRequest",
    "LocalRunReceipt",
    "LocalRunRequest",
    "LocalRunner",
    "MetricDelta",
    "PairedJudgeResult",
    "RankingMetricError",
    "RankingMetricErrorCode",
    "RankingMetricResult",
    "ResearchDomain",
    "RunnerError",
    "RunnerErrorCode",
    "ScreeningResult",
    "SealedPredictionReceipt",
    "SnapshotErrorCode",
    "StageCError",
    "StageCErrorCode",
    "TrialLedger",
    "TrialLedgerState",
    "TrialRecord",
    "YouTubeCTRDomain",
    "WorkspaceError",
    "WorkspaceErrorCode",
    "build_evaluation_snapshot",
    "build_final_target",
    "build_local_evaluation_fixture",
    "build_validation_target",
    "materialize_candidate_data_view",
    "canonical_fixture_dates",
    "claim_final_consumption",
    "compare_confirmation",
    "descriptor_sha256",
    "ndcg_at_k",
    "open_candidate_workspace",
    "open_trial_ledger",
    "recall_at_k",
    "screen_candidate",
    "seal_prediction_copy",
    "score_predictions",
    "select_fixture_user_ids",
]
