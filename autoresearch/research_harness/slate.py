"""일일 action log에서 게시 가능한 평가 snapshot을 조립하는 공개 파사드.

[파이프라인] 검증된 일일 action log 원천과 P0-2 Sealed Judge 사이에서 Stage B의
source·slate·attribution·split·artifact·publisher 단계를 순서대로 연결한다.

[기능] typed 요청과 선택적 source adapter를 받아 cutover 이후 partition의 canonical
slate identity를 검증하고 content-addressed local snapshot receipt를 반환한다. Stage C의
결정적 fixture에 한해 생성 시각을 주입하는 내부 조립 helper도 제공한다.

[비책임] 각 단계의 검증·귀속·분할·identity·게시 알고리즘과 Stage C fixture/Judge
handoff는 인접 모듈 및 후속 단계가 담당한다.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from autoresearch.research_harness.click_attribution import attribute_clicks
from autoresearch.research_harness.evaluation_artifacts import write_snapshot_artifacts
from autoresearch.research_harness.evaluation_snapshot_models import (
    EvaluationSnapshotReceipt,
    EvaluationSnapshotRequest,
    EvaluationWindow,
    SnapshotArtifactInput,
)
from autoresearch.research_harness.evaluation_source import (
    ActionLogSource,
    load_required_partitions,
)
from autoresearch.research_harness.evaluation_split import split_evaluation_rows
from autoresearch.research_harness.slate_validation import validate_slate_identities
from autoresearch.research_harness.snapshot_publisher import publish_snapshot


def build_evaluation_snapshot(
    request: EvaluationSnapshotRequest,
    *,
    source: ActionLogSource | None = None,
) -> EvaluationSnapshotReceipt:
    return _build_evaluation_snapshot(
        request,
        source=source,
        created_at=datetime.now(UTC),
    )


def _build_evaluation_snapshot(
    request: EvaluationSnapshotRequest,
    *,
    source: ActionLogSource | None,
    created_at: datetime,
) -> EvaluationSnapshotReceipt:
    partitions = load_required_partitions(request, source)
    validate_slate_identities(
        tuple(
            partition
            for partition in partitions
            if partition.receipt.dt >= request.slate_id_cutover_date
        )
    )
    receipts = tuple(partition.receipt for partition in partitions)
    window = EvaluationWindow(
        history_start_date=request.history_start_date,
        evaluation_start_date=request.evaluation_start_date,
        evaluation_end_date=request.evaluation_end_date,
        label_scan_end_date=request.evaluation_end_date + timedelta(days=1),
        complete_history_label_end_date=request.evaluation_start_date
        - timedelta(days=2),
        candidate_history_partitions=tuple(
            receipt
            for receipt in receipts
            if receipt.dt < request.evaluation_start_date
        ),
    )
    rows = attribute_clicks(partitions, window)
    validation, final_holdout = split_evaluation_rows(rows)
    snapshot_root = request.output_root / "evaluation-snapshots"
    snapshot_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".staging-", dir=snapshot_root) as staging_name:
        staging_dir = Path(staging_name)
        manifest = write_snapshot_artifacts(
            staging_dir,
            SnapshotArtifactInput(
                request=request,
                window=window,
                partitions=receipts,
                validation=validation,
                final_holdout=final_holdout,
                created_at=created_at,
            ),
        )
        return publish_snapshot(staging_dir, snapshot_root / "by-hash", manifest)
