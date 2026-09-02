#!/usr/bin/env python3
"""LightGBM 학습 파이프라인 Typer CLI.

[파이프라인] 피처 조립 → 학습 → 평가 → comparison → champion 승격 구간의
진입점(배선)을 담당한다: `python -m autoresearch.cli create-experiment-plan /
build-features / train-model / evaluate-model / run-pipeline / verify-comparison /
compare-paired-experiment / promote-model / sweep-seeds / measure-degradation`.

[기능] 각 단계 모듈에 인자를 전달하고 단계 순서를 정한다. #466의
create-experiment-plan은 write-once GCS plan receipt를 만들고, 학습·comparison
명령은 그 receipt를 전달하거나 검증할 evidence store를 생성한다. run-pipeline은
build-features → train-model → evaluate-model 순서로 실행하며, registered model 버전
생성은 평가가 통과한 뒤에 수행한다(#421) — 평가가 실패하면 지표를 신뢰할 수 없는
후보 버전이 registry에 남지 않는다.
학습 CLI의 `split_seed`·`model_seed`·`sampler_seed`는 각각 데이터 분할·모델 초기화·
negative downsampling 난수를 분리하며, `run-pipeline`은 검증된 snapshot sidecar를
요구한다(#423). `sweep-seeds`는 기존 `random_state` 호환 경로를 유지한다(#407).
`build-features`/`run-pipeline`의 `--feature-service`·`--extra-features`는 조립 단계까지
전달되어 실험 피처가 학습 CSV에 보존되게 한다(#454). `compare-paired-experiment`는
조건별 실행이 끝난 뒤 seed별 baseline/candidate run을 짝지어 판정하고
`comparison_passed`/`comparison_rejected`/`comparison_failed` 결과 payload를 남긴다(#454).
`measure-degradation`은 단일 cutoff로 학습한 모델을 이후 날짜에 하루씩 순차 적용해
ROC-AUC 열화 곡선과 열화 지점을 낸다(#471) — 승격 판정이 아니라 측정 도구다.
`harness-predict`는 별도 candidate-safe 로컬 입력으로 seed별 새 학습과 CSV 예측을
수행한다(#48). 이 경로는 운영 MLflow 등록이나 원격 데이터 게시를 호출하지 않는다.
`harness-run`은 Judge-only 로컬 설정으로 실제 coding agent·Controller를 연결하고
불변 입력과 ledger를 대조하여 재개한다(#52). 모델 준비와 calibration은 별도다.

[비책임] 실제 조립·학습·평가·승격 로직은 각 모듈(`autoresearch/model_training/`,
`autoresearch/model_evaluation/`, `autoresearch/model_registry/promote.py`)이 소유한다. DAG·스케줄·재시도는 인접 저장소
Autoresearch-airflow 소유다.
"""

import json
import math
import os
import sys
import traceback
import uuid
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

import typer
from pydantic import ValidationError

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from autoresearch.model_training import (  # noqa: E402
    build_training_dataset,
    train,
)
from autoresearch.model_evaluation import (  # noqa: E402
    degradation_eval,
    evaluate,
    paired_experiment,
    training_comparison,
)
from autoresearch.reporting.experiment_result_report import (  # noqa: E402
    LauncherOwnedExperimentError,
    ResultReportError,
    TerminalStatusConflictError,
    build_log_content,
    build_log_idempotency_key,
    build_metric_snapshot,
    build_reason,
    plan_transitions,
    target_status,
)
from autoresearch.model_evaluation.seed_sweep import run_seed_sweep, validate_seeds  # noqa: E402
from autoresearch.model_evaluation.promotion_evidence import (  # noqa: E402
    PromotionEvidenceStore,
    PromotionEvidenceValidationError,
    create_experiment_plan,
)
from autoresearch.model_training.training_provenance import write_manifest_atomic  # noqa: E402
from autoresearch.model_registry import promote  # noqa: E402
from autoresearch.model_registry.promotion_result import (  # noqa: E402
    MODEL_PROMOTION_RESULT_CONTRACT,
    ModelPromotionResult,
    PromotionExecutionError,
    PromotionOutcome,
    PromotionReasonCode,
    write_result_file,
)

app = typer.Typer()


@app.command("harness-predict")
def harness_predict(
    slate: Path = typer.Option(..., "--slate", help="candidate v2 slate.parquet 경로"),
    out: Path = typer.Option(..., "--out", help="새 prediction CSV 경로"),
    seed: int = typer.Option(..., "--seed", min=0, max=2**32 - 1, help="재학습 seed"),
    config: Path = typer.Option(Path("harness_config.json"), "--config", help="로컬 임베딩/학습 설정 JSON"),
) -> None:
    """후보 입력으로 새 LightGBM을 학습하고 예측·모델·receipt를 보존한다."""
    from autoresearch.feature_engineering.model_contract import FeatureContractError
    from autoresearch.research_harness.prediction import run_harness_prediction

    try:
        run_harness_prediction(slate=slate, out=out, seed=seed, config_path=config)
    except FeatureContractError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from None


@app.command("harness-run")
def harness_run(
    config: Path = typer.Option(..., "--config", help="Judge-only 실제 실험 설정 JSON"),
) -> None:
    """준비된 로컬 모델·로그인으로 자율 실험을 실행하거나 같은 run을 재개한다."""
    from autoresearch.feature_engineering.model_contract import FeatureContractError
    from autoresearch.research_harness.controller import ControllerError
    from autoresearch.research_harness.fixture_errors import StageCError
    from autoresearch.research_harness.ledger import LedgerError
    from autoresearch.research_harness.local_runtime import (
        LocalRuntimeError, load_run_config, run_local_research,
    )

    try:
        result = run_local_research(load_run_config(config))
    except (LocalRuntimeError, ControllerError, StageCError, LedgerError, FeatureContractError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from None
    except (OSError, ValueError, RuntimeError):
        typer.echo("harness_runtime_failed: stage=local_io", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps({
        "conclusion": result.conclusion.value,
        "validation_trials": result.validation_trials,
        "final_reason_code": result.final_reason_code,
    }, sort_keys=True))


@app.command()
def build_features(
    output_path: Optional[str] = typer.Option(
        None, help="출력 CSV 경로 (기본: data/processed/training_dataset.csv)"
    ),
    events_start_date: Optional[str] = typer.Option(
        None, help="학습 기간 시작일 KST YYYY-MM-DD (spine=training_entity 조회)"
    ),
    events_end_date: Optional[str] = typer.Option(
        None, help="학습 기간 종료일 KST YYYY-MM-DD (포함)"
    ),
    min_coverage_days: Optional[int] = typer.Option(
        None,
        "--min-coverage-days",
        help=(
            "학습에 쓸 수 있는 최소 날짜 수(#464). 요청 기간에 데이터 없는 날이 섞여 "
            "이 값 미만이면 조립을 실패시킵니다. 백필처럼 의도적으로 좁은 구간을 쓸 때는 "
            "0으로 우회합니다. 일별 행수 하한은 실행 단위로 조정할 수 없습니다 "
            "(전역 CTR_TRAINING_MIN_ROWS_PER_DAY만)."
        ),
    ),
    feature_service: Optional[str] = typer.Option(
        None,
        "--feature-service",
        help=(
            "조회할 Feast FeatureService 이름(기본 ctr_training_v1, #454). 실험용 파생 "
            "피처를 가진 서비스를 지정하면 그 서비스로 PIT 조회하며, 실제로 쓴 이름이 "
            "snapshot manifest에 기록됩니다."
        ),
    ),
    extra_features: Optional[str] = typer.Option(
        None,
        "--extra-features",
        help=(
            "학습 CSV에 함께 보존할 실험 피처(쉼표 구분, #454). prod 계약 컬럼 뒤·라벨 앞에 "
            "덧붙여 저장하며, 조회 결과에 없으면 CSV를 쓰기 전에 실패합니다. 물리 스키마가 "
            "prod 데이터셋과 달라지므로 prod와 같은 출력 경로를 재사용하지 마십시오."
        ),
    ),
    snapshot_root: Optional[str] = typer.Option(
        None,
        "--snapshot-root",
        help=(
            "조립한 데이터셋을 게시할 gs://bucket/prefix (미지정 시 "
            "TRAINING_SNAPSHOT_ROOT, 둘 다 없으면 게시하지 않습니다). "
            "prod 재학습 경로에만 지정하십시오 — 실험·dev 파이프라인이 켜면 "
            "by-date 포인터가 경합합니다(#530)."
        ),
    ),
) -> None:
    """training_dataset.csv 생성 (offline feature store PIT 조회, #359 C2로 feast-only)."""
    snapshot_root_kwargs = _snapshot_root_kwargs(snapshot_root)
    if not snapshot_root_kwargs:
        # 게시 게이팅은 main()이 하지만, "이번 실행은 게시하지 않는다"는 사실은
        # 호출자(CLI)가 이미 알고 있다 — main()이 매 호출마다 이 안내를 찍으면
        # degradation_eval처럼 반복 호출하는 경로에서 같은 줄이 호출 수만큼
        # 중복된다(#530 PR 리뷰). 그래서 여기서 한 번만 남긴다.
        typer.echo("[게시 없음] snapshot root 미지정 — 로컬에만 저장")
    build_training_dataset.main(
        output_path=output_path,
        events_start_date=events_start_date,
        events_end_date=events_end_date,
        **_coverage_kwargs(min_coverage_days),
        **_assembly_feature_kwargs(
            feature_service=feature_service,
            extra_features=_parse_extra_features(extra_features),
        ),
        **snapshot_root_kwargs,
    )


def _requested_min_coverage_days(value: object) -> Optional[int]:
    """사용자가 실제로 지정한 `--min-coverage-days` 값만 정수로 돌려준다(#464).

    미지정이면 None이다. Typer가 붙인 함수를 테스트가 **직접 호출**하면 기본값 자리에
    `OptionInfo` 객체가 들어오므로(CLI 경유일 때만 None으로 치환됨), 정수인지로 판정한다.
    `bool`은 파이썬에서 `int`의 하위 타입이라 명시적으로 배제한다.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _coverage_kwargs(min_coverage_days: Optional[int]) -> dict:
    """`--min-coverage-days` 미지정 시 모듈 기본값을 그대로 쓰게 한다(#464).

    None을 그대로 넘기면 기본값이 덮여 검증이 꺼지므로, 지정된 경우에만 키를 만든다.
    `0`(명시적 우회)과 None(미지정)을 참/거짓으로 뭉개면 우회구가 조용히 무시된다.
    """
    resolved = _requested_min_coverage_days(min_coverage_days)
    return {} if resolved is None else {"min_coverage_days": resolved}


def _parse_extra_features(value: Optional[str]) -> Optional[list[str]]:
    """`--extra-features` 쉼표 목록을 파싱한다(#405).

    미지정·빈 문자열이면 None을 돌려 prod 경로(계약 그대로)를 유지한다.
    """
    if not value:
        return None
    names = [name.strip() for name in value.split(",") if name.strip()]
    return names or None


def _optional_cli_string(value: object) -> Optional[str]:
    """직접 함수 호출의 Typer OptionInfo 기본값을 미지정(None)으로 정규화한다."""
    return value if isinstance(value, str) else None


def _assembly_feature_kwargs(
    *, feature_service: object, extra_features: Optional[list[str]]
) -> dict:
    """지정된 조립 피처 옵션만 build-features 인자로 만든다(#454).

    `_coverage_kwargs`와 같은 규칙이다 — 미지정 옵션을 None으로 넘겨 모듈 기본값을
    덮지 않고, 아무것도 지정하지 않은 실행의 조립 인자를 기존과 완전히 동일하게 둔다.
    """
    kwargs: dict = {}
    service = _optional_cli_string(feature_service)
    if service is not None:
        kwargs["feature_service"] = service
    if extra_features:
        kwargs["extra_features"] = extra_features
    return kwargs


def _snapshot_root_kwargs(snapshot_root: object) -> dict:
    """스냅샷 게시 루트를 옵션 → 환경변수 순으로 해석한다(#530).

    환경변수를 `build_training_dataset.main()`이 아니라 여기서만 읽는 것이 계약이다 —
    `degradation_eval`의 horizon 평가 루프는 `main()`을 평가일 수만큼 부르므로,
    `main()`이 환경변수를 직접 읽으면 그 루프가 by-date 포인터를 평가일마다 덮어쓴다.
    루트를 명시적으로 넘기지 않는 호출은 게시 경로에 들어갈 방법이 없어야 한다.
    """
    resolved = _optional_cli_string(snapshot_root) or os.environ.get(
        "TRAINING_SNAPSHOT_ROOT"
    )
    return {} if not resolved else {"snapshot_root": resolved}


def _promotion_evidence_kwargs(
    *,
    experiment_plan_receipt: object,
    promotion_evidence_root: object,
) -> dict[str, str]:
    """두 promotion evidence 옵션을 함께 받았을 때만 train 인자로 만든다."""
    receipt = _optional_cli_string(experiment_plan_receipt)
    root = _optional_cli_string(promotion_evidence_root)
    if (receipt is None) != (root is None):
        typer.echo(
            "[인자 오류] --experiment-plan-receipt와 --promotion-evidence-root는 "
            "함께 지정해야 합니다.",
            err=True,
        )
        raise typer.Exit(code=2)
    if receipt is None:
        return {}
    return {
        "experiment_plan_receipt_path": receipt,
        "promotion_evidence_root": root,
    }


@app.command("create-experiment-plan")
def create_experiment_plan_command(
    hypothesis_id: str = typer.Option(..., "--hypothesis-id", help="가설 식별자"),
    control_id: str = typer.Option(..., "--control-id", help="대조군 식별자"),
    candidate_id: str = typer.Option(..., "--candidate-id", help="후보 식별자"),
    promotion_evidence_root: str = typer.Option(
        ...,
        "--promotion-evidence-root",
        help="write-once plan을 기록할 gs://bucket/prefix root",
    ),
    output: Path = typer.Option(..., "--output", help="published plan receipt JSON 경로"),
) -> None:
    """학습 전에 immutable experiment plan을 publish하고 receipt를 원자 저장한다."""
    try:
        plan = create_experiment_plan(
            hypothesis_id=hypothesis_id,
            control_id=control_id,
            candidate_ids=(candidate_id,),
        )
        receipt = PromotionEvidenceStore(promotion_evidence_root).publish_plan(plan)
    except PromotionEvidenceValidationError as error:
        typer.echo(
            f"[실험 계획 publish 실패] {type(error).__name__}: "
            "plan receipt를 만들지 않았습니다.",
            err=True,
        )
        raise typer.Exit(code=1) from error
    try:
        write_manifest_atomic(receipt, output)
    except OSError as error:
        typer.echo(
            f"[실험 계획 receipt 저장 실패] {type(error).__name__}: "
            "GCS plan은 이미 publish되었습니다. 아래 receipt를 안전한 경로에 저장한 뒤 "
            "학습에 사용해 주세요.",
            err=True,
        )
        typer.echo(receipt.model_dump_json(), err=True)
        raise typer.Exit(code=1) from error
    typer.echo(receipt.model_dump_json())


@app.command()
def train_model(
    config_path: Optional[str] = typer.Option(None, help="config.yaml 경로 (기본: autoresearch/model_training/config.yaml)"),
    data_path: Optional[str] = typer.Option(None, help="training dataset 경로 (config override)"),
    model_output: Optional[str] = typer.Option(None, help="모델 저장 경로 (config override)"),
    test_set_output: Optional[str] = typer.Option(
        None, help="Held-out test set 저장 경로 (config override, 병렬 실험 시 실험별로 분리 필요)"
    ),
    feature_columns_output: Optional[str] = typer.Option(None, help="Feature 목록 저장 경로 (config override)"),
    categorical_columns_output: Optional[str] = typer.Option(None, help="Categorical 카테고리 저장 경로 (config override)"),
    test_size: Optional[float] = typer.Option(None, help="Test set 비율 (config override)"),
    val_size: Optional[float] = typer.Option(None, help="Val set 비율 (config override)"),
    random_state: Optional[int] = typer.Option(
        None, help="기존 호환용 random state (세 effective seed에 동일 적용)"
    ),
    split_seed: Optional[int] = typer.Option(
        None, "--split-seed", help="Train/validation/test 분할에 사용할 seed"
    ),
    model_seed: Optional[int] = typer.Option(
        None, "--model-seed", help="LightGBM 모델 초기화에 사용할 seed"
    ),
    sampler_seed: Optional[int] = typer.Option(
        None, "--sampler-seed", help="Train split negative downsampling에 사용할 seed"
    ),
    experiment: Optional[str] = typer.Option(
        None,
        "--experiment",
        help=(
            "실험 이름. 지정하면 prod와 분리된 MLflow experiment·registry 이름"
            "(<model>-exp-<slug>)으로 기록되고, 트래킹 URI 미설정 시 로컬 파일 스토어를 "
            "기본값으로 씁니다(#406). 이 모델은 champion 승격 대상이 아닙니다."
        ),
    ),
    extra_features: Optional[str] = typer.Option(
        None,
        "--extra-features",
        help=(
            "실험 피처(쉼표 구분). prod 모델 계약을 수정하지 않고 그 뒤에 덧붙여 학습합니다(#405). "
            "데이터셋에 이미 있는 컬럼만 지정할 수 있으며, 이 모델은 champion 승격이 차단됩니다."
        ),
    ),
    experiment_plan_receipt: Optional[str] = typer.Option(
        None,
        "--experiment-plan-receipt",
        help="학습 전에 publish한 ExperimentPlanReceipt JSON 경로",
    ),
    promotion_evidence_root: Optional[str] = typer.Option(
        None,
        "--promotion-evidence-root",
        help="plan/held-out metric receipt를 검증·기록할 gs://bucket/prefix root",
    ),
    dataset_uri: Optional[str] = typer.Option(
        None,
        "--dataset-uri",
        help=(
            "게시된 스냅샷 gs://<root>/by-hash/<sha>/ 를 재조립 없이 학습 입력으로 씁니다(#530). "
            "내려받은 뒤 sha·schema·row_count를 재검증하며, 불일치하면 학습 전에 중단합니다."
        ),
    ),
    min_coverage_days: Optional[int] = typer.Option(
        None,
        "--min-coverage-days",
        help=(
            "재사용 스냅샷(--dataset-uri)이 만족해야 할 최소 spine 사용 가능 일수(#530). "
            "미지정이면 조립 경로와 같은 기본값을 적용하며, 0으로 명시하면 우회합니다. "
            "--dataset-uri 없이 학습할 때는 아무 영향이 없습니다."
        ),
    ),
) -> None:
    """LightGBM 모델 훈련 (train/val/test 3-way split, test는 완전 held-out).

    `--dataset-uri`를 주면 게시된 스냅샷(#530)을 재조립 없이 학습 입력으로 쓴다 —
    `--data-path`와는 함께 지정할 수 없다(스냅샷이 학습 입력을 이미 확정했다).
    `--min-coverage-days`는 이 재사용 경로에만 적용되는 커버리지 게이트로,
    `run-pipeline --dataset-uri`와 같은 기본값(미지정 시 모듈 기본값, 0이면 우회)을 쓴다.
    """
    promotion_evidence_kwargs = _promotion_evidence_kwargs(
        experiment_plan_receipt=experiment_plan_receipt,
        promotion_evidence_root=promotion_evidence_root,
    )
    resolved_dataset_uri = _optional_cli_string(dataset_uri)
    if resolved_dataset_uri is not None and _optional_cli_string(data_path) is not None:
        raise typer.BadParameter(
            "--dataset-uri는 --data-path와 함께 쓸 수 없습니다 — "
            "스냅샷이 학습 입력을 이미 확정했습니다"
        )
    requested_min_coverage_days = _requested_min_coverage_days(min_coverage_days)
    train.main(
        config_path=config_path,
        data_path=data_path,
        model_output=model_output,
        test_set_output=test_set_output,
        feature_columns_output=feature_columns_output,
        categorical_columns_output=categorical_columns_output,
        test_size=test_size,
        val_size=val_size,
        random_state=random_state,
        split_seed=split_seed,
        model_seed=model_seed,
        sampler_seed=sampler_seed,
        extra_features=_parse_extra_features(extra_features),
        experiment=experiment,
        dataset_uri=resolved_dataset_uri,
        min_coverage_days=(
            build_training_dataset.DEFAULT_MIN_COVERAGE_DAYS
            if requested_min_coverage_days is None
            else requested_min_coverage_days
        ),
        **promotion_evidence_kwargs,
    )


@app.command()
def evaluate_model(
    config_path: Optional[str] = typer.Option(None, help="config.yaml 경로 (기본: autoresearch/model_training/config.yaml)"),
    data_path: Optional[str] = typer.Option(None, help="평가용 데이터 경로 (config override, 기본: held-out test set)"),
    model_path: Optional[str] = typer.Option(None, help="모델 로드 경로 (config override)"),
    feature_columns_path: Optional[str] = typer.Option(None, help="Feature 목록 경로 (config override)"),
    extra_features: Optional[str] = typer.Option(
        None,
        "--extra-features",
        help=(
            "실험 피처(쉼표 구분). 학습이 --extra-features로 만든 모델을 단독으로 "
            "재평가할 때 학습과 **같은 목록**을 주십시오(#405). 다르면 계약 검증이 막습니다."
        ),
    ),
    metrics_output: Optional[str] = typer.Option(
        None,
        "--metrics-output",
        help=(
            "held-out 지표를 기록할 JSON 경로. 지정하면 stdout과 **같은 값**을 "
            "`held-out-metrics-v1` 형식으로 남깁니다 — 호출자가 출력을 파싱하지 "
            "않게 하는 용도입니다."
        ),
    ),
) -> None:
    """저장된 모델을 held-out test set으로 평가."""
    evaluate.main(
        config_path=config_path,
        data_path=data_path,
        model_path=model_path,
        feature_columns_path=feature_columns_path,
        extra_features=_parse_extra_features(extra_features),
        metrics_output=metrics_output,
    )


@app.command()
def run_pipeline(
    dataset_path: Optional[str] = typer.Option(None, help="Training dataset 경로 (기본: data/processed/training_dataset.csv)"),
    events_start_date: Optional[str] = typer.Option(
        None, help="학습 기간 시작일 KST YYYY-MM-DD (spine=training_entity 조회)"
    ),
    events_end_date: Optional[str] = typer.Option(
        None, help="학습 기간 종료일 KST YYYY-MM-DD (포함)"
    ),
    min_coverage_days: Optional[int] = typer.Option(
        None,
        "--min-coverage-days",
        help=(
            "학습에 쓸 수 있는 최소 날짜 수(#464). 미달이면 조립 단계에서 실패합니다. "
            "실측 커버리지와 이 값은 MLflow lineage에 기록됩니다. "
            "백필 등 의도적으로 좁은 구간을 쓸 때는 0으로 우회합니다."
        ),
    ),
    config_path: Optional[str] = typer.Option(None, help="config.yaml 경로 (기본: autoresearch/model_training/config.yaml)"),
    model_output: Optional[str] = typer.Option(None, help="모델 저장 경로 (config override)"),
    test_set_output: Optional[str] = typer.Option(
        None, help="Held-out test set 저장 경로 (config override, 병렬 실험 시 실험별로 분리 필요)"
    ),
    feature_columns_output: Optional[str] = typer.Option(None, help="Feature 목록 저장 경로 (config override)"),
    categorical_columns_output: Optional[str] = typer.Option(None, help="Categorical 카테고리 저장 경로 (config override)"),
    test_size: Optional[float] = typer.Option(None, help="Test set 비율 (config override)"),
    val_size: Optional[float] = typer.Option(None, help="Val set 비율 (config override)"),
    random_state: Optional[int] = typer.Option(
        None, help="기존 호환용 random state (세 effective seed에 동일 적용)"
    ),
    split_seed: Optional[int] = typer.Option(
        None, "--split-seed", help="Train/validation/test 분할에 사용할 seed"
    ),
    model_seed: Optional[int] = typer.Option(
        None, "--model-seed", help="LightGBM 모델 초기화에 사용할 seed"
    ),
    sampler_seed: Optional[int] = typer.Option(
        None, "--sampler-seed", help="Train split negative downsampling에 사용할 seed"
    ),
    experiment: Optional[str] = typer.Option(
        None,
        "--experiment",
        help=(
            "실험 이름. 지정하면 prod와 분리된 MLflow experiment·registry 이름"
            "(<model>-exp-<slug>)으로 기록되고, 트래킹 URI 미설정 시 로컬 파일 스토어를 "
            "기본값으로 씁니다(#406). 이 모델은 champion 승격 대상이 아닙니다."
        ),
    ),
    extra_features: Optional[str] = typer.Option(
        None,
        "--extra-features",
        help=(
            "실험 피처(쉼표 구분). prod 모델 계약을 수정하지 않고 그 뒤에 덧붙여 학습·평가합니다(#405). "
            "조립 단계가 같은 목록을 CSV에 보존하므로(#454) FeatureService가 제공하는 파생 피처를 "
            "그대로 쓸 수 있으며, 이 모델은 champion 승격이 차단됩니다."
        ),
    ),
    feature_service: Optional[str] = typer.Option(
        None,
        "--feature-service",
        help=(
            "조립이 조회할 Feast FeatureService 이름(기본 ctr_training_v1, #454). "
            "실제로 쓴 이름이 snapshot manifest와 MLflow lineage에 기록됩니다."
        ),
    ),
    experiment_plan_receipt: Optional[str] = typer.Option(
        None,
        "--experiment-plan-receipt",
        help="학습 전에 publish한 ExperimentPlanReceipt JSON 경로",
    ),
    promotion_evidence_root: Optional[str] = typer.Option(
        None,
        "--promotion-evidence-root",
        help="plan/held-out metric receipt를 검증·기록할 gs://bucket/prefix root",
    ),
    snapshot_root: Optional[str] = typer.Option(
        None,
        "--snapshot-root",
        help=(
            "조립한 데이터셋을 게시할 gs://bucket/prefix (미지정 시 "
            "TRAINING_SNAPSHOT_ROOT, 둘 다 없으면 게시하지 않습니다). "
            "prod 재학습 경로에만 지정하십시오 — 실험·dev 파이프라인이 켜면 "
            "by-date 포인터가 경합합니다(#530)."
        ),
    ),
    dataset_uri: Optional[str] = typer.Option(
        None,
        "--dataset-uri",
        help=(
            "게시된 스냅샷 gs://<root>/by-hash/<sha>/ 를 재조립 없이 학습 입력으로 씁니다(#530). "
            "내려받은 뒤 sha·schema·row_count를 재검증하며, 불일치하면 학습 전에 중단합니다."
        ),
    ),
) -> None:
    """전체 파이프라인 실행: build-features -> train-model -> evaluate-model -> 등록.

    등록(Model Registry 버전 생성)은 평가 통과 뒤에만 수행하는 별도 단계다(#421).
    조립 경로는 #359 C2로 feast-only다. `--extra-features`를 주면 prod 모델 계약을
    건드리지 않고 실험 피처를 덧붙여 학습하며, 조립·학습·평가가 같은 목록을 공유한다
    (#405, 조립 보존은 #454). `--feature-service`로 조회할 FeatureService를 바꿀 수 있고,
    실제로 쓴 이름이 MLflow lineage에 남는다.
    `--split-seed`, `--model-seed`, `--sampler-seed`는 verified comparison용으로
    분리해 전달하며, snapshot sidecar가 없거나 검증에 실패하면 학습을 시작하지 않는다.
    `--dataset-uri`를 주면 build-features를 완전히 생략하고 게시된 스냅샷을
    재사용한다(#530) — `--dataset-path`·`--events-start-date`·`--events-end-date`와는
    함께 쓸 수 없다(스냅샷이 그 값들을 이미 확정했다). `--feature-service`·
    `--snapshot-root`도 함께 쓸 수 없다 — 재사용 경로는 조립 분기를 건너뛰어 두
    옵션이 전달될 곳이 없고, 조용히 무시되면 오퍼레이터가 지정한 값이 아무 효과가
    없다는 사실을 알 방법이 없다.
    """
    experiment_features = _parse_extra_features(extra_features)
    promotion_evidence_kwargs = _promotion_evidence_kwargs(
        experiment_plan_receipt=experiment_plan_receipt,
        promotion_evidence_root=promotion_evidence_root,
    )

    resolved_dataset_uri = _optional_cli_string(dataset_uri)
    # 충돌 검사만 정규화하고 하위 호출에는 raw 값을 넘기던 비대칭을 없앤다(#537).
    # CLI 경유로는 둘 다 None이라 지금은 차이가 없지만, 이 함수를 직접 부르며
    # dataset_path를 생략하면 Typer의 OptionInfo 객체가 그대로 build-features와
    # train.main까지 흘러가 "경로가 아닌 값"으로 뒤늦게 터진다.
    resolved_dataset_path = _optional_cli_string(dataset_path)
    if resolved_dataset_uri is not None:
        conflicting = {
            "--dataset-path": resolved_dataset_path,
            "--events-start-date": _optional_cli_string(events_start_date),
            "--events-end-date": _optional_cli_string(events_end_date),
            # 재사용 경로는 _assembly_feature_kwargs·_snapshot_root_kwargs를 부르는
            # 조립 분기 자체를 건너뛰므로, 이 둘을 줘도 아무 데도 전달되지 않고
            # 조용히 무시된다 — MLflow에 남는 feature_service는 다운로드한
            # manifest의 것이고, 게시도 일어나지 않는다. 반드시 거부한다(#530 PR 리뷰).
            "--feature-service": _optional_cli_string(feature_service),
            "--snapshot-root": _optional_cli_string(snapshot_root),
        }
        named = [name for name, value in conflicting.items() if value is not None]
        if named:
            raise typer.BadParameter(
                f"--dataset-uri는 {', '.join(named)}와 함께 쓸 수 없습니다 — "
                "스냅샷이 학습 구간과 입력을 이미 확정했습니다"
            )

    typer.echo("=" * 70)
    typer.echo("전체 파이프라인 실행")
    typer.echo("=" * 70)

    # 조립 경로(lineage 기록)와 재사용 경로(train.main 게이트)가 같은 값을 보도록
    # 한 번만 계산해 두 곳에서 재사용한다 — 이름을 두 개로 나누면 나중에 한쪽만
    # 고치는 사고가 생긴다.
    requested_min_coverage_days = _requested_min_coverage_days(min_coverage_days)
    snapshot_uri: Optional[str] = resolved_dataset_uri
    if resolved_dataset_uri is None:
        typer.echo("\n[1/4] build-features 실행...")
        snapshot_root_kwargs = _snapshot_root_kwargs(snapshot_root)
        if not snapshot_root_kwargs:
            # main()이 매 호출마다 이 안내를 찍으면 반복 호출 호출부(degradation_eval)에서
            # 중복되므로 호출자인 여기서 한 번만 남긴다(#530 PR 리뷰).
            typer.echo("[게시 없음] snapshot root 미지정 — 로컬에만 저장")
        # 실험 피처는 학습·평가만이 아니라 **조립에도** 넘긴다(#454) — 조립이 보존하지 않으면
        # 학습의 --extra-features가 승격할 컬럼 자체가 CSV에 없어 실행이 성립하지 않는다.
        assembly = build_training_dataset.main(
            output_path=resolved_dataset_path,
            events_start_date=events_start_date,
            events_end_date=events_end_date,
            **_coverage_kwargs(min_coverage_days),
            **_assembly_feature_kwargs(
                feature_service=feature_service, extra_features=experiment_features
            ),
            **snapshot_root_kwargs,
        )
        coverage = assembly.coverage
        snapshot_uri = assembly.snapshot_uri
    else:
        typer.echo(f"\n[1/4] build-features 생략 — 스냅샷 재사용: {resolved_dataset_uri}")

    # 어떤 기간·소스로 학습했는지 MLflow run에 lineage로 남긴다(#359). C2로 조립 경로는
    # feast(offline store PIT)가 유일하므로 FeatureService·registry·기간을 기록한다.
    from autoresearch.feature_engineering.feast_retrieval import DEFAULT_SERVICE

    if resolved_dataset_uri is None:
        # run-pipeline은 C2로 feast-only다. 위 build-features(_assemble_via_feast)가
        # events 기간을 필수로 검증하고 GCS_REGISTRY_PATH를 필수로 읽으므로(미설정이면
        # 여기 도달 전에 멈춤), 이 시점엔 셋 다 항상 존재한다. 따라서 조립이 필수로 읽는
        # 값을 lineage는 "있으면 기록"으로 두던 비대칭을 없애고 무조건 기록해, registry나
        # 기간이 빠진 재현 불가 run이 남지 않게 한다(#359 C2 리뷰).
        # FeatureService는 하드코딩하지 않고 조립이 실제로 쓴 이름을 남긴다(#454) —
        # 실험 서비스로 조회한 run이 prod 서비스로 조회된 것처럼 기록되면 비교가 성립하지 않는다.
        data_source_params = {
            "assembly_source": "feast",
            "feature_service": _optional_cli_string(feature_service) or DEFAULT_SERVICE,
            "events_start_date": events_start_date,
            "events_end_date": events_end_date,
            "feast_registry_path": os.environ["GCS_REGISTRY_PATH"],
        }
        # 요청 구간만 남기면 v12 사고의 비대칭("요청 7일 ≠ 실제 2일")이 그대로 남는다.
        # 실측 커버리지와 적용된 기준(우회 여부 포함)을 함께 남겨, 나중에 champion 후보를
        # 볼 때 run 파라미터만으로 판별할 수 있게 한다(#464 리뷰).
        data_source_params.update(
            coverage.as_lineage_params(
                min_days=(
                    build_training_dataset.DEFAULT_MIN_COVERAGE_DAYS
                    if requested_min_coverage_days is None
                    else requested_min_coverage_days
                )
            )
        )
    else:
        # 재조립을 하지 않아 조립 반환값이 없다 — 나머지 lineage(events_*,
        # feature_service, registry, spine_usable_days)는 train.main이 다운로드한
        # manifest에서 직접 채운다(#530) — manifest를 실제로 읽는 주체가 그 함수뿐이다.
        data_source_params = {"assembly_source": "snapshot_reuse"}

    # 게시하지 않은 실행에는 이 키를 아예 넣지 않는다 — 빈 문자열로 남기면
    # "게시했는데 URI가 비었다"와 구별되지 않는다(#530 §10-7).
    if snapshot_uri is not None:
        data_source_params["training_snapshot_uri"] = snapshot_uri

    typer.echo("\n[2/4] train-model 실행...")
    # train.main은 실현 sampling_rate(#300)를 담은 TrainingOutcome을 반환한다 —
    # evaluate가 오프라인 지표(LogLoss/calibration)를 원분포 기준으로 재도록 넘긴다.
    # defer_registration=True: registered model 버전 생성만 평가 뒤로 미룬다(#421).
    # run 로깅(파라미터·메트릭·아티팩트)은 학습 시점에 그대로 남는다.
    outcome = train.main(
        config_path=config_path,
        data_path=resolved_dataset_path,
        model_output=model_output,
        test_set_output=test_set_output,
        feature_columns_output=feature_columns_output,
        categorical_columns_output=categorical_columns_output,
        test_size=test_size,
        val_size=val_size,
        random_state=random_state,
        split_seed=split_seed,
        model_seed=model_seed,
        sampler_seed=sampler_seed,
        extra_params=data_source_params,
        defer_registration=True,
        extra_features=experiment_features,
        experiment=experiment,
        require_snapshot=True,
        dataset_uri=resolved_dataset_uri,
        min_coverage_days=(
            build_training_dataset.DEFAULT_MIN_COVERAGE_DAYS
            if requested_min_coverage_days is None
            else requested_min_coverage_days
        ),
        **promotion_evidence_kwargs,
    )

    # dataset_path(방금 만든 train+val+test 전체)는 넘기지 않는다: evaluate는
    # train-model이 분리해 저장한 held-out test set으로만 채점해야 하며, 그대로
    # 넘기면 data leakage가 재발한다. 대신 test_set_output/feature_columns_output을
    # 그대로 전달해서, 병렬로 여러 run-pipeline을 돌릴 때도(각자 다른 경로를 줬다면)
    # 자기 자신이 만든 test set/feature 목록으로 채점되도록 짝을 맞춘다.
    typer.echo("\n[3/4] evaluate-model 실행...")
    evaluate.main(
        config_path=config_path,
        data_path=test_set_output,
        model_path=model_output,
        feature_columns_path=feature_columns_output,
        sampling_rate=outcome.sampling_rate,
        # 학습이 쓴 것과 같은 목록을 넘겨야 계약 검증이 어긋나지 않는다(#405).
        extra_features=experiment_features,
    )

    # 평가가 통과한 뒤에야 registered model 버전을 만든다(#421). 평가가 실패하면
    # evaluate.main의 예외가 여기까지 오지 않으므로, 지표를 신뢰할 수 없는 후보
    # 버전이 registry에 쌓이지 않는다. 학습 run과 아티팩트는 이미 남아 있어,
    # 데이터를 고친 뒤 재학습하면 정상 후보가 다시 만들어진다.
    if outcome.pending_registration is not None:
        typer.echo("\n[등록] 평가 통과 — Model Registry 등록...")
        train.register_pending_model(outcome.pending_registration)

    typer.echo("\n" + "=" * 70)
    typer.echo("파이프라인 완료")
    typer.echo("=" * 70)


@app.command()
def verify_comparison(
    baseline_run_id: str = typer.Option(
        ..., "--baseline-run-id", help="비교 기준(baseline) MLflow run ID"
    ),
    challenger_run_id: str = typer.Option(
        ..., "--challenger-run-id", help="비교 대상(challenger) MLflow run ID"
    ),
    output: Path = typer.Option(
        ..., "--output", help="검증된 comparison manifest를 저장할 로컬 JSON 경로"
    ),
    promotion_evidence_root: Optional[str] = typer.Option(
        None,
        "--promotion-evidence-root",
        help="plan/held-out metric receipt를 재검증할 gs://bucket/prefix root",
    ),
) -> None:
    """두 MLflow run의 공정성과 선택적 promotion evidence를 검증한다(#423, #466)."""
    comparison_kwargs: dict[str, object] = {
        "baseline_run_id": baseline_run_id,
        "challenger_run_id": challenger_run_id,
        "output_path": output,
    }
    try:
        root = _optional_cli_string(promotion_evidence_root)
        if root is not None:
            comparison_kwargs["promotion_evidence_store"] = PromotionEvidenceStore(root)
        result = training_comparison.verify_training_comparison(**comparison_kwargs)
    except (PromotionEvidenceValidationError, training_comparison.ComparisonValidationError) as error:
        # 예외 원문은 backend credential이나 signed URL을 포함할 수 있으므로 CLI에는
        # type과 고정된 안전 진단만 출력한다.
        typer.echo(
            f"[비교 검증 실패] {type(error).__name__}: "
            "verified comparison manifest를 만들지 않았습니다.",
            err=True,
        )
        raise typer.Exit(code=1) from error

    typer.echo(result.model_dump_json(indent=2))


@app.command()
def compare_paired_experiment(
    request: Path = typer.Option(
        ..., "--request", help="paired-offline-experiment-v1 요청 JSON 경로"
    ),
    promotion_evidence_root: str = typer.Option(
        ...,
        "--promotion-evidence-root",
        help="plan/held-out metric receipt를 재검증할 gs://bucket/prefix root",
    ),
    output: Path = typer.Option(
        ..., "--output", help="비교 결과 payload를 게시할 로컬 JSON 경로"
    ),
    workspace: Optional[Path] = typer.Option(
        None,
        "--workspace",
        help="seed별 verified comparison manifest를 둘 디렉터리(기본: 임시 디렉터리)",
    ),
) -> None:
    """baseline/candidate paired 실행 결과를 비교·판정한다(#454).

    조건별 학습은 서로 다른 이미지에서 끝난 뒤이므로, 이 명령은 실행이 아니라
    **집계·판정**만 한다. 요청 검증이나 comparison 재검증이 실패하면 판정 엔진을
    부르지 않고 `comparison_failed`를 남긴다 — 판정할 수 없는 상태를 통과로
    해석하지 않기 위해서다.

    exit code: 통과·기각은 0(정상 판정), 실패는 1, 인자·요청 계약 오류는 2.
    """
    try:
        payload = json.loads(Path(request).read_text(encoding="utf-8"))
        parsed = paired_experiment.PairedExperimentRequest.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        # 요청 payload 원문에는 URI·식별자가 섞여 있으므로 CLI에는 오류 종류만 남긴다.
        typer.echo(
            f"[요청 검증 실패] {type(error).__name__}: "
            "paired-offline-experiment-v1 요청을 읽지 못했습니다.",
            err=True,
        )
        raise typer.Exit(code=2) from error

    try:
        store = PromotionEvidenceStore(promotion_evidence_root)
    except PromotionEvidenceValidationError as error:
        typer.echo(
            f"[요청 검증 실패] {type(error).__name__}: "
            "promotion evidence root가 올바르지 않습니다.",
            err=True,
        )
        raise typer.Exit(code=2) from error

    with ExitStack() as stack:
        if workspace is None:
            # workspace를 명시하지 않으면 seed별 comparison manifest는 실행 동안만
            # 필요하다. 지정한 경우에는 재사용·보존이 목적이므로 지우지 않는다.
            resolved_workspace = Path(
                stack.enter_context(TemporaryDirectory(prefix="paired_experiment_"))
            )
        else:
            resolved_workspace = Path(workspace)
        try:
            result = paired_experiment.evaluate_paired_experiment(
                parsed,
                promotion_evidence_store=store,
                workspace=resolved_workspace,
            )
        except OSError as error:
            # workspace를 만들지 못하는 등 판정 자체를 시작할 수 없는 경우다.
            # traceback을 그대로 흘리지 않고 안전한 진단만 남긴다.
            typer.echo(
                f"[비교 실행 실패] {type(error).__name__}: "
                "paired 비교를 시작하지 못했습니다.",
                err=True,
            )
            raise typer.Exit(code=1) from error
        try:
            paired_experiment.write_result(result, Path(output))
        except OSError as error:
            typer.echo(
                f"[결과 게시 실패] {type(error).__name__}: "
                "비교 결과 파일을 남기지 못했습니다.",
                err=True,
            )
            raise typer.Exit(code=1) from error

    typer.echo(result.model_dump_json())
    if result.outcome == paired_experiment.OUTCOME_FAILED:
        raise typer.Exit(code=1)


def _experiment_client_module():
    """Experiment API client 모듈을 **지연** import한다.

    `applications.experiment_platform.workbench.client`는 `ui.models`를 거쳐
    `applications.experiment_platform.api.experiments.models`를 끌어오고, 그 모듈이 SQLAlchemy를
    요구한다. 학습 이미지는 `uv sync --locked --no-dev`로 빌드되어 SQLAlchemy가 없으므로
    top-level import면 `autoresearch.cli` 전체가 뜨지 않는다 — `train-model --help`조차 죽는다.

    이 명령을 실제로 실행할 때만 필요한 의존이므로 여기서만 가져온다.
    """
    from applications.experiment_platform.workbench import client

    return client


_DEFAULT_REPORT_FAILURE = "판정 결과를 Experiment API에 반영하지 못했습니다."

# 종료 코드 1이 나오는 경우들은 운영 대응이 서로 다르다. 실패 로그만 보고 무엇을 해야
# 하는지 알 수 있도록 사유별 고정 진단을 붙인다(payload는 싣지 않는다).
_REPORT_FAILURE_DIAGNOSTICS = {
    LauncherOwnedExperimentError: (
        "CREATED 실험은 launcher가 RUNNING으로 선점합니다 — 선점된 뒤 다시 실행해 "
        "주세요."
    ),
    TerminalStatusConflictError: (
        "이미 결론이 난 실험이라 덮어쓰지 않았습니다 — --experiment-id가 맞는지 "
        "확인해 주세요."
    ),
}


def _report_stop_point(reached: Optional[str]) -> None:
    """실패 시 실험이 어느 상태로 남았는지와 재개 방법을 알린다.

    중간 상태를 터미널로 내리지 않으므로(아래 근거) 재실행이 남은 전이부터 이어간다.
    전이를 하나도 밟지 못했으면 실험은 손대지 않은 그대로다.
    """
    if reached is None:
        return
    typer.echo(
        f"[결과 반영 실패] 실험은 {reached} 상태로 남았습니다 — 원인을 고친 뒤 같은 "
        "명령을 재실행하면 남은 전이부터 재개합니다.",
        err=True,
    )


@app.command("report-experiment-result")
def report_experiment_result(
    result: Path = typer.Option(
        ..., "--result", help="compare-paired-experiment가 게시한 결과 JSON 경로"
    ),
    experiment_id: str = typer.Option(
        ..., "--experiment-id", help="Experiment API의 실험 UUID"
    ),
    log_uri: Optional[str] = typer.Option(
        None, "--log-uri", help="포인터 로그에 함께 남길 실행 로그 위치"
    ),
) -> None:
    """paired 판정 결과를 Experiment API에 반영한다(#550).

    판정도 실행도 하지 않는다. 현재 상태를 먼저 읽어 남은 전이만 밟으며, 중간에
    실패하면 실험을 **그 상태 그대로 두고** 끝낸다 — 재실행이 남은 전이부터 재개한다.

    `CREATED` 실험은 다루지 않는다 — launcher가 선점할 대기 행이므로 종료 코드 1로
    거부한다(#547).

    exit code: 반영 성공 0, API·전이 실패 1, 인자·결과 계약 오류 2.
    """
    try:
        # 서버 라우트가 `experiment_id: uuid.UUID`이므로(`router.py:110`) 오타는 422 →
        # 종료 코드 1로 나가 API 실패와 구분되지 않는다. 인자 오류는 인자 오류로
        # 끊는다 — #454 `experiment_id`(UUID가 아니다)와 뒤바꾼 사고도 여기서 걸린다.
        uuid.UUID(experiment_id)
    except ValueError as error:
        typer.echo(
            f"[결과 반영 실패] {type(error).__name__}: "
            "--experiment-id가 UUID 형식이 아닙니다.",
            err=True,
        )
        raise typer.Exit(code=2) from error

    try:
        payload = json.loads(Path(result).read_text(encoding="utf-8"))
        parsed = paired_experiment.PairedExperimentResult.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        # 결과 payload에는 URI·식별자가 섞여 있으므로 오류 종류만 남긴다.
        typer.echo(
            f"[결과 반영 실패] {type(error).__name__}: "
            "paired-offline-experiment-result-v1 결과를 읽지 못했습니다.",
            err=True,
        )
        raise typer.Exit(code=2) from error

    client_module = _experiment_client_module()
    try:
        # 빈 토큰·base_url 검사는 client 생성자가 이미 한다. 여기서 다시 만들지 않고
        # 그 예외를 종료 코드로 옮기기만 한다.
        client = client_module.ExperimentClient.from_environment()
    except client_module.ExperimentApiError as error:
        typer.echo(
            f"[결과 반영 실패] {type(error).__name__}: "
            "Experiment API 연결 설정이 올바르지 않습니다.",
            err=True,
        )
        raise typer.Exit(code=2) from error

    reached: Optional[str] = None
    try:
        target = target_status(parsed)
        current = client.get_experiment(experiment_id).status
        transitions = plan_transitions(current, target)
        reason = build_reason(parsed)
        for status in transitions:
            is_terminal = status == target
            client.patch_status(
                experiment_id,
                status,
                reason=reason,
                metric_snapshot=build_metric_snapshot(parsed) if is_terminal else None,
            )
            reached = status
        client.post_log(
            experiment_id,
            idempotency_key=build_log_idempotency_key(experiment_id, parsed),
            content=build_log_content(parsed, log_uri=log_uri),
        )
    except (client_module.ExperimentApiError, ResultReportError) as error:
        typer.echo(
            f"[결과 반영 실패] {type(error).__name__}: "
            f"{_REPORT_FAILURE_DIAGNOSTICS.get(type(error), _DEFAULT_REPORT_FAILURE)}",
            err=True,
        )
        _report_stop_point(reached)
        raise typer.Exit(code=1) from error

    typer.echo(f"{experiment_id} -> {target}")


@app.command()
def promote_model(
    model_name: str = typer.Option("ctr-model", help="Registry에 등록된 main 모델 이름"),
    champion_alias: str = typer.Option("champion", help="승격 대상 alias"),
    calibration_model_name: str = typer.Option(
        "ctr-calibration-model",
        help="[DEPRECATED · 무시됨] #390에서 calibration은 main run에 종속돼 별도 등록하지 않습니다. "
        "호출 계약 하위호환을 위해 인자만 남겨두며 값은 사용하지 않습니다.",
    ),
    result_contract: Optional[str] = typer.Option(
        None,
        "--result-contract",
        help="구조화 결과 계약. --result-path와 함께 model-promotion-result-v1만 허용합니다.",
    ),
    result_path: Optional[Path] = typer.Option(
        None,
        "--result-path",
        help="구조화 결과 JSON 파일 경로. --result-contract와 함께 지정합니다.",
    ),
) -> None:
    """게이트(지표 비교 + downsampling calibration 아티팩트 존재) 통과 시 신규 후보를 champion으로 승격.

    calibration_model_name은 #390에서 무시된다(deprecated). Airflow DAG(Autoresearch-airflow#137)가
    아직 이 플래그를 넘기더라도 기동이 깨지지 않도록 인자 표면만 유지하며, DAG에서 플래그를 제거한
    뒤 후속 PR로 이 인자를 걷어낸다.
    """
    structured_mode_requested = result_contract is not None or result_path is not None
    structured_mode_valid = (
        result_contract == MODEL_PROMOTION_RESULT_CONTRACT
        and result_path is not None
    )
    if structured_mode_requested and not structured_mode_valid:
        typer.echo(
            "[인자 오류] --result-contract와 --result-path를 함께 지정하고 "
            f"--result-contract={MODEL_PROMOTION_RESULT_CONTRACT}을 사용해 주세요.",
            err=True,
        )
        raise typer.Exit(code=2)

    # 기본값과 다른 값이 명시적으로 넘어오면 stderr에 deprecation 경고를 남긴다 — DAG가
    # 기본값과 같은 문자열을 넘기면 감지 못하지만(한계), 다른 값이면 "아직 호출부가 이 플래그를
    # 쓰고 있다"는 신호를 로그로 남겨 언제 걷어내도 되는지 추적하게 한다(#395 리뷰).
    if calibration_model_name != "ctr-calibration-model":
        typer.echo(
            "[경고] --calibration-model-name은 #390에서 무시됩니다(deprecated). "
            "호출부(DAG)에서 이 플래그를 제거해 주세요.",
            err=True,
        )
    if structured_mode_valid:
        _run_structured_promotion(
            model_name=model_name,
            champion_alias=champion_alias,
            result_path=result_path,
        )
        return

    _run_legacy_promotion(
        model_name=model_name,
        champion_alias=champion_alias,
    )


def _error_result(
    *,
    model_name: str,
    champion_alias: str,
    reason_code: PromotionReasonCode,
    candidate_version: Optional[str] = None,
    champion_version: Optional[str] = None,
    candidate_metric: Optional[float] = None,
    champion_metric: Optional[float] = None,
) -> ModelPromotionResult:
    """외부 예외 내용을 포함하지 않는 구조화 오류 결과를 만든다."""
    return ModelPromotionResult(
        outcome=PromotionOutcome.ERROR,
        model_name=model_name,
        champion_alias=champion_alias,
        candidate_version=candidate_version,
        champion_version=champion_version,
        candidate_metric=candidate_metric,
        champion_metric=champion_metric,
        reason_code=reason_code,
    )


def _emit_structured_error_diagnostic(
    *,
    reason_code: PromotionReasonCode,
    error: BaseException,
    include_stack: bool,
) -> None:
    """비밀 가능성이 있는 예외 메시지 없이 안전한 stderr 진단을 출력한다."""
    typer.echo(
        f"[구조화 결과 오류] reason_code={reason_code.value} "
        f"error_type={type(error).__name__}",
        err=True,
    )
    if include_stack and error.__traceback__ is not None:
        for frame in traceback.extract_tb(error.__traceback__):
            typer.echo(
                f"  at {frame.filename}:{frame.lineno} in {frame.name}",
                err=True,
            )


def _run_structured_promotion(
    *,
    model_name: str,
    champion_alias: str,
    result_path: Path,
) -> None:
    """구조화 결과를 파일과 stdout에 기록하고 오류에만 non-zero로 종료한다."""
    try:
        result = promote.main(
            model_name=model_name,
            champion_alias=champion_alias,
        )
    except PromotionExecutionError as exc:
        _emit_structured_error_diagnostic(
            reason_code=exc.reason_code,
            error=exc,
            include_stack=False,
        )
        result = _error_result(
            model_name=model_name,
            champion_alias=champion_alias,
            reason_code=exc.reason_code,
            candidate_version=exc.candidate_version,
            champion_version=exc.champion_version,
            candidate_metric=exc.candidate_metric,
            champion_metric=exc.champion_metric,
        )
    except Exception as exc:
        _emit_structured_error_diagnostic(
            reason_code=PromotionReasonCode.UNEXPECTED_ERROR,
            error=exc,
            include_stack=True,
        )
        result = _error_result(
            model_name=model_name,
            champion_alias=champion_alias,
            reason_code=PromotionReasonCode.UNEXPECTED_ERROR,
        )

    try:
        write_result_file(result, result_path)
    except Exception as exc:
        _emit_structured_error_diagnostic(
            reason_code=PromotionReasonCode.RESULT_WRITE_FAILED,
            error=exc,
            include_stack=False,
        )
        result = _error_result(
            model_name=model_name,
            champion_alias=champion_alias,
            reason_code=PromotionReasonCode.RESULT_WRITE_FAILED,
            candidate_version=result.candidate_version,
            champion_version=result.champion_version,
            candidate_metric=result.candidate_metric,
            champion_metric=result.champion_metric,
        )

    typer.echo(result.model_dump_json())
    if result.outcome is PromotionOutcome.ERROR:
        raise typer.Exit(code=1)


def _run_legacy_promotion(
    *,
    model_name: str,
    champion_alias: str,
) -> None:
    """구조화 opt-in 전 호출부의 메시지와 exit code 계약을 보존한다."""
    try:
        result = promote.main(
            model_name=model_name,
            champion_alias=champion_alias,
        )
    except promote.GateRejectedError as exc:
        typer.echo(f"[게이트 미달] {exc}", err=True)
        raise typer.Exit(code=1)
    except Exception as exc:
        typer.echo(f"[에러] promote-model 실행 중 오류: {exc}", err=True)
        raise typer.Exit(code=1)

    if result.outcome is PromotionOutcome.REJECTED:
        if result.legacy_message is not None:
            detail = result.legacy_message
        elif result.reason_code is PromotionReasonCode.METRIC_BELOW_CHAMPION:
            detail = (
                f"게이트1 미달: 후보 {result.model_name} "
                f"v{result.candidate_version} "
                f"val_roc_auc={result.candidate_metric:.4f} < "
                f"champion({result.champion_alias}) "
                f"val_roc_auc={result.champion_metric:.4f}"
            )
        elif result.reason_code is PromotionReasonCode.CALIBRATION_ARTIFACT_MISSING:
            detail = (
                f"게이트2 미달: 후보 {result.model_name} "
                f"v{result.candidate_version}에 필요한 calibration "
                "아티팩트가 없습니다."
            )
        elif result.reason_code is PromotionReasonCode.SERVING_CALIBRATION_NOT_READY:
            detail = (
                f"후보 {result.model_name} v{result.candidate_version}: "
                "서빙 calibration 준비가 완료되지 않았습니다."
            )
        else:
            # 새 reason code가 생겼는데 분기를 안 만든 경우. 엉뚱한 진단을 찍는 대신
            # 코드 자체를 드러낸다(#405 리뷰 — EXPERIMENT_MODEL이 calibration 문제로
            # 잘못 보고되던 문제).
            detail = (
                f"후보 {result.model_name} v{result.candidate_version}: "
                f"{result.reason_code.value}"
            )
        typer.echo(f"[게이트 미달] {detail}", err=True)
        raise typer.Exit(code=1)
    if result.outcome is PromotionOutcome.NO_CANDIDATE:
        typer.echo(f"{model_name}: 평가할 신규 후보 버전 없음 — no-op")
        return
    if result.outcome is PromotionOutcome.ERROR:
        typer.echo(f"[에러] promote-model 실행 실패: {result.reason_code.value}", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"[OK] {model_name} v{result.candidate_version} "
        f"-> @{champion_alias} 승격 완료"
    )


@app.command()
def sweep_seeds(
    seeds: str = typer.Option(
        "42,43,44",
        "--seeds",
        help="반복할 시드 목록(쉼표 구분). 기본값은 이슈 템플릿의 재현 조건과 같은 3개입니다.",
    ),
    config_path: Optional[str] = typer.Option(None, help="config.yaml 경로 (기본: autoresearch/model_training/config.yaml)"),
    data_path: Optional[str] = typer.Option(None, help="training dataset 경로 (config override)"),
    output_dir: Optional[str] = typer.Option(
        None, help="시드별 아티팩트 저장 디렉토리 (기본: data/processed/seed_sweep)"
    ),
    test_size: Optional[float] = typer.Option(None, help="Test set 비율 (config override)"),
    val_size: Optional[float] = typer.Option(None, help="Val set 비율 (config override)"),
    experiment: Optional[str] = typer.Option(
        None, "--experiment", help="실험 이름. prod와 분리된 네임스페이스로 기록합니다(#406)."
    ),
    extra_features: Optional[str] = typer.Option(
        None, "--extra-features", help="실험 피처(쉼표 구분). prod 계약을 수정하지 않습니다(#405)."
    ),
    result_path: Optional[str] = typer.Option(
        None, "--result-path", help="요약 JSON 저장 경로. 미지정이면 표준출력에만 남깁니다."
    ),
) -> None:
    """같은 조건을 여러 시드로 반복 학습하고 지표 평균·편차를 요약한다 (#407).

    시드 1개로 1회만 돌리면 지표 차이가 진짜 개선인지 분할이 흔들려 나온 노이즈인지
    판정할 수 없다. 이 명령은 판정을 대신하지 않고 **판정 근거**만 만든다 —
    채택/기각은 가설의 성공 기준이 정한다.

    시드마다 아티팩트가 덮어써지지 않도록 `--output-dir` 아래에 시드별로 저장한다.
    """
    try:
        seed_list = [int(s.strip()) for s in seeds.split(",") if s.strip()]
    except ValueError as error:
        typer.echo(
            f"[에러] --seeds는 쉼표로 구분된 정수 목록이어야 합니다: {seeds!r}", err=True
        )
        raise typer.Exit(code=2) from error

    # 부수효과(디렉토리 생성) 전에 시드 목록을 검증한다(#407 리뷰 5).
    validate_seeds(seed_list)
    experiment_features = _parse_extra_features(extra_features)
    base_dir = output_dir or os.path.join("data", "processed", "seed_sweep")

    def _train_once(*, random_state: int) -> float:
        typer.echo(f"\n[시드 {random_state}] 학습 시작...")
        outcome = train.main(
            # 스윕의 산출물은 승격 후보가 아니라 요약이다. 시드마다 등록하면
            # registry에 버전이 시드 수만큼 쌓이고, 마지막 시드의 모델이 후보
            # 자리에 앉는다(#407 리뷰 1). 등록 없이 지표만 받는다.
            defer_registration=True,
            config_path=config_path,
            data_path=data_path,
            model_output=os.path.join(base_dir, f"model_seed{random_state}.joblib"),
            test_set_output=os.path.join(base_dir, f"test_set_seed{random_state}.csv"),
            feature_columns_output=os.path.join(
                base_dir, f"feature_columns_seed{random_state}.json"
            ),
            categorical_columns_output=os.path.join(
                base_dir, f"categorical_columns_seed{random_state}.json"
            ),
            test_size=test_size,
            val_size=val_size,
            random_state=random_state,
            extra_features=experiment_features,
            experiment=experiment,
        )
        return outcome.val_roc_auc

    # 시드 검증(빈 목록·중복)이 끝난 뒤에 디렉토리를 만든다 — 실패 경로에서
    # 빈 디렉토리가 남지 않도록(#407 리뷰 5).
    os.makedirs(base_dir, exist_ok=True)
    result = run_seed_sweep(seed_list, train_once=_train_once)
    payload = result.to_dict()

    typer.echo("\n" + "=" * 70)
    typer.echo("시드 스윕 요약")
    typer.echo("=" * 70)
    for seed, metric in zip(result.seeds, result.metrics):
        typer.echo(f"  seed {seed}: {result.metric_name}={metric:.4f}")
    summary = result.summary
    std_text = "판정 불가(시드 1개)" if math.isnan(summary.std) else f"{summary.std:.4f}"
    typer.echo(
        f"\n  n={summary.n} mean={summary.mean:.4f} std={std_text} "
        f"min={summary.minimum:.4f} max={summary.maximum:.4f}"
    )
    typer.echo(
        "\n  baseline과 비교하려면 두 조건의 요약을 compare_to_baseline에 넘기십시오 "
        "— 이 명령은 한 조건의 편차만 잽니다."
    )

    if result_path:
        with open(result_path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        typer.echo(f"\n[저장] 요약 JSON: {result_path}")


@app.command("measure-degradation")
def measure_degradation(
    cutoff_date: str = typer.Option(
        ..., "--cutoff-date", help="학습 경계 날짜 KST YYYY-MM-DD (당일은 평가 첫날, 학습에 미포함)"
    ),
    window_days: int = typer.Option(..., "--window-days", help="cutoff 이전 학습 기간(일)"),
    horizon_days: int = typer.Option(..., "--horizon-days", help="cutoff 이후 하루 단위 평가 기간(일)"),
    run_root: str = typer.Option(
        ..., "--run-root", help="학습·평가일별 산출물을 격리해 저장할 디렉터리"
    ),
    min_rows_per_day: int = typer.Option(
        ..., "--min-rows-per-day", help="평가일을 유효로 판정할 최소 행수"
    ),
    min_auc_drop: float = typer.Option(
        ...,
        "--min-auc-drop",
        help="열화 판정 절대 하락폭. sweep-seeds 등으로 미리 calibration한 값을 넘깁니다.",
    ),
    recent_window_days: int = typer.Option(
        3,
        "--recent-window-days",
        help=(
            "'최근 성능' 평균에 쓸 최근 유효일 수(#485 §3). 실측 후 재조정 대상이라 "
            "결과에도 함께 기록됩니다. 1 이상이어야 합니다."
        ),
    ),
    min_coverage_days: Optional[int] = typer.Option(
        None, "--min-coverage-days", help="학습 spine 커버리지 가드 최소 일수(기본: 모듈 기본값)"
    ),
    seed: Optional[int] = typer.Option(None, "--seed", help="학습 random_state"),
    feature_service: Optional[str] = typer.Option(None, "--feature-service"),
    extra_features: Optional[str] = typer.Option(
        None, "--extra-features", help="쉼표 구분 실험 피처(#454)"
    ),
    experiment: Optional[str] = typer.Option(
        None, "--experiment", help="실험 이름. prod와 분리된 네임스페이스로 기록합니다(#406)."
    ),
    best_effort: bool = typer.Option(
        False,
        "--best-effort",
        help="평가일 조립 실패를 evaluation_failed로 기록하고 계속 진행합니다"
        "(기본: 즉시 중단 — 비싼 cutoff 학습을 버리지 않으려면 켜십시오).",
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="run-root가 이미 채워져 있어도 재사용합니다."
    ),
    output: Path = typer.Option(..., "--output", help="RollingOriginResult JSON을 게시할 경로"),
) -> None:
    """단일 cutoff 기반 모델 성능 열화 시점을 측정한다(#471).

    cutoff 이전 데이터로 모델을 1회 학습하고, cutoff부터 하루씩 순회 평가해 ROC-AUC
    곡선과 열화 지점을 낸다. 승격 판정이 아니라 측정·리포트 도구다 — 이 명령의
    산출물은 champion 후보로 등록되지 않는다.

    exit code: 성공 0(열화 탐지 여부와 무관 — 둘 다 유효한 측정 결과다),
    run-root 재사용 오류(``--overwrite`` 필요) 2, 그 외 실행 실패 1.
    """
    try:
        result = degradation_eval.run_rolling_origin(
            cutoff_date,
            window_days=window_days,
            horizon_days=horizon_days,
            run_root=run_root,
            min_rows_per_day=min_rows_per_day,
            min_auc_drop=min_auc_drop,
            recent_window_days=recent_window_days,
            min_coverage_days=min_coverage_days,
            seed=seed,
            feature_service=feature_service,
            extra_features=_parse_extra_features(extra_features),
            experiment=experiment,
            best_effort=best_effort,
            overwrite=overwrite,
        )
    except degradation_eval.RunRootExistsError as error:
        typer.echo(f"[에러] {error}", err=True)
        raise typer.Exit(code=2) from error
    except Exception as error:
        # 예외 원문에 BigQuery/GCS 자격증명이나 경로가 섞일 수 있으므로(#454
        # comparison_verification_failed와 같은 이유) 종류만 남기고 원문은 감춘다.
        typer.echo(
            f"[측정 실패] {type(error).__name__}: cutoff 학습 또는 평가일 조립 중 "
            "오류가 발생했습니다.",
            err=True,
        )
        raise typer.Exit(code=1) from error

    write_manifest_atomic(result, Path(output))
    typer.echo(result.model_dump_json())
    if result.degradation_point.elapsed_days is not None:
        typer.echo(
            f"\n[열화 탐지] elapsed_days={result.degradation_point.elapsed_days} "
            f"date={result.degradation_point.date}"
        )
    else:
        typer.echo(f"\n[열화 미탐지] 사유={result.degradation_point.reason}")


if __name__ == "__main__":
    app()
