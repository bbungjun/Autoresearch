"""재현 가능한 평가 snapshot과 validation Judge scoring 패키지.

[파이프라인] action log 일일 파티션과 P0-2 Sealed Judge 사이에서 평가용
label-free slate와 Judge 전용 label artifact를 조립하는 경계를 담당한다.

[기능] Stage B의 공개 요청·receipt·error·source seam과 snapshot builder, Stage C의
fixture/candidate handoff typed contract, canonical identity helper, P0-2A ranking metric,
P0-2B scoring, P0-2C 봉인 ingestion·판정, P0-2D domain interface, final 소비 registry와
append-only Trial Ledger, LocalRunner 및 반복 ResearchController를 제공한다.
Validation metadata 준비·v2 게시를 opt-in으로 제공하며 v1 소비 경로는 유지한다.

[비책임] action log 생성(autoresearch.action_log_generation), candidate 코드 변경·학습 구현,
실제 baseline sigma 측정, candidate coding adapter와 REPORT를 담당하지 않는다.
"""

from autoresearch.research_harness.controller import (
    ControllerConclusion,
    ControllerError,
    ControllerErrorCode,
    ControllerRunRequest,
    ControllerRunResult,
    FinalPairRequest,
    PairedRunReceipt,
    PrepareCandidateRequest,
    PreparedCandidate,
    ResearchBudget,
    ResearchController,
    ResearchPlanner,
    ResearchTrialRunner,
    TrialExecutionError,
    ValidationPairRequest,
)

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
    materialize_candidate_data_view_v2,
    prepare_candidate_metadata,
)
from autoresearch.research_harness.fixture_errors import StageCError, StageCErrorCode
from autoresearch.research_harness.feedback import (
    ExperimentCard,
    FeedbackFailure,
    FeedbackMetric,
    FeedbackPayload,
    TrialFeedbackSummary,
    build_feedback,
)
from autoresearch.research_harness.fixture_inputs import (
    canonical_fixture_dates,
    descriptor_sha256,
    select_fixture_user_ids,
)
from autoresearch.research_harness.fixture_models import (
    CandidateDataManifest,
    CandidateDataManifestV2,
    CandidateDataViewReceipt,
    CandidateDataViewRequest,
    PreparedCandidateMetadata,
    PreparedMetadataArtifact,
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
    "CandidateDataManifestV2",
    "PreparedCandidateMetadata",
    "PreparedMetadataArtifact",
    "CandidateDataViewReceipt",
    "CandidateDataViewRequest",
    "CandidateHistoryReceipt",
    "CandidateChangeReceipt",
    "CandidateProcessContext",
    "CandidateWorkspace",
    "CandidateWorkspaceRequest",
    "CheckpointRecord",
    "ConfirmationDecision",
    "ControllerConclusion",
    "ControllerError",
    "ControllerErrorCode",
    "ControllerRunRequest",
    "ControllerRunResult",
    "ConsumptionRegistryError",
    "ConsumptionRegistryErrorCode",
    "DomainError",
    "DomainErrorCode",
    "EvaluationSnapshotError",
    "EvaluationSnapshotReceipt",
    "EvaluationSnapshotRequest",
    "ExperimentCard",
    "FeedbackFailure",
    "FeedbackMetric",
    "FeedbackPayload",
    "FixtureDescriptor",
    "FixtureInputReceipt",
    "FixturePartitionReceipt",
    "FinalConsumptionEvidence",
    "FinalConsumptionGrant",
    "FinalConsumptionRequest",
    "FinalPairRequest",
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
    "PairedRunReceipt",
    "PrepareCandidateRequest",
    "PreparedCandidate",
    "RankingMetricError",
    "RankingMetricErrorCode",
    "RankingMetricResult",
    "ResearchDomain",
    "ResearchBudget",
    "ResearchController",
    "ResearchPlanner",
    "ResearchTrialRunner",
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
    "TrialExecutionError",
    "TrialFeedbackSummary",
    "YouTubeCTRDomain",
    "WorkspaceError",
    "WorkspaceErrorCode",
    "ValidationPairRequest",
    "build_evaluation_snapshot",
    "build_feedback",
    "build_final_target",
    "build_local_evaluation_fixture",
    "build_validation_target",
    "materialize_candidate_data_view",
    "materialize_candidate_data_view_v2",
    "prepare_candidate_metadata",
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
