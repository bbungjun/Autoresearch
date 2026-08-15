"""학습 opt-in 환경이 어느 container에 붙는지를 계약으로 고정한다(#605).

학습은 baseline(Codex 실행 **전**)과 candidate(push **후**) 두 지점에서 돈다. 그 두
container에만 데이터셋 좌표가 붙어야 하고, credential이 없는 codex-worker·verifier에는
붙지 않아야 한다. URI가 비어 있으면 아무것도 붙지 않아 executor가 기존 경로만 돈다.
"""

from __future__ import annotations

from pathlib import Path
import sys
import uuid

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from applications.experiment_platform.launcher.config import (  # noqa: E402
    LauncherConfigError,
    LauncherSettings,
)
from applications.experiment_platform.launcher.jobs import build_executor_job  # noqa: E402
from applications.experiment_platform.launcher.repository import ClaimedExperiment  # noqa: E402


_EXPERIMENT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
_DIGEST = "d3d273e66324042cd8e547068c194231cf1812d53cb68236edba56b067055293"
_DATASET_URI = f"gs://experiment-results/training-snapshots/by-hash/{_DIGEST}/"
_TRAINING_KEYS = frozenset(
    {
        "ORCH_TRAINING_DATASET_URI",
        "ORCH_TRAINING_TIMEOUT_SEC",
        "ORCH_TRAINING_DOWNLOAD_TIMEOUT_SEC",
        "ORCH_UV_SYNC_TIMEOUT_SEC",
    }
)


_MLFLOW_URI = "http://mlflow.mlflow.svc.cluster.local:5000"


def _settings(
    *,
    dataset_uri: str = "",
    mlflow_tracking_uri: str = "",
    experiment_results_root: str = "",
) -> LauncherSettings:
    return LauncherSettings(
        mlflow_tracking_uri=mlflow_tracking_uri,
        experiment_results_root=experiment_results_root,
        database_url="postgresql://launcher:password@db/orchestration",
        job_namespace="agent-orchestration",
        executor_image=(
            "asia-northeast3-docker.pkg.dev/example/executor@sha256:" + "b" * 64
        ),
        executor_service_account="experiment-executor",
        executor_node_pool="batch-od",
        github_app_secret_name="experiment-app",
        github_app_id=123,
        github_app_installation_id=456,
        github_repository="SKYAHO/Autoresearch",
        max_concurrent_experiments=2,
        executor_api_url="http://agent-orchestration-api",
        executor_api_token_secret_name="executor-api-token",
        codex_home_secret_name="codex-auth",
        workspace_size_limit="8Gi",
        codex_timeout_sec=900,
        active_deadline_sec=2700,
        training_dataset_uri=dataset_uri,
    )


def _claim() -> ClaimedExperiment:
    return ClaimedExperiment(
        experiment_id=_EXPERIMENT_ID,
        issue_number=605,
        issue_branch="exp/605",
        base_dev_sha="a" * 40,
        job_name="ar-exec-1234567812345678123456781234",
    )


def _environment(container) -> dict[str, str]:
    return {variable.name: variable.value for variable in (container.env or [])}


def _containers(settings: LauncherSettings) -> dict[str, object]:
    job = build_executor_job(_claim(), settings)
    spec = job.spec.template.spec
    return {
        container.name: container
        for container in [*(spec.init_containers or []), *(spec.containers or [])]
    }


def test_training_environment_is_absent_when_the_dataset_uri_is_unset() -> None:
    """URI가 없으면 어느 container에도 붙지 않는다 — 학습을 켜지 않은 배포."""
    for name, container in _containers(_settings()).items():
        present = _TRAINING_KEYS & set(_environment(container))
        assert not present, f"{name}에 학습 환경이 붙었다: {sorted(present)}"


def test_training_environment_is_limited_to_the_two_training_containers() -> None:
    """baseline은 workspace-preparer, candidate는 candidate-finalizer에서 돈다."""
    containers = _containers(_settings(dataset_uri=_DATASET_URI))
    expected = {"workspace-preparer", "candidate-finalizer"}
    for name, container in containers.items():
        environment = _environment(container)
        if name in expected:
            assert _TRAINING_KEYS <= set(environment), f"{name}에 학습 환경이 부족하다"
            assert environment["ORCH_TRAINING_DATASET_URI"] == _DATASET_URI
        else:
            present = _TRAINING_KEYS & set(environment)
            assert not present, f"{name}에 학습 환경이 붙었다: {sorted(present)}"


def test_mlflow_tracking_uri_is_exported_without_the_orch_prefix() -> None:
    """`train.py`가 `os.getenv("MLFLOW_TRACKING_URI")`로 읽는다 — 접두사를 붙이면 무효다.

    launcher가 **받는** 이름은 `ORCH_MLFLOW_TRACKING_URI`이고 executor에 **내보내는**
    이름은 `MLFLOW_TRACKING_URI`다. 두 이름이 다르다.
    """
    containers = _containers(
        _settings(dataset_uri=_DATASET_URI, mlflow_tracking_uri=_MLFLOW_URI)
    )
    for name in ("workspace-preparer", "candidate-finalizer"):
        environment = _environment(containers[name])
        assert environment["MLFLOW_TRACKING_URI"] == _MLFLOW_URI
        assert "ORCH_MLFLOW_TRACKING_URI" not in environment


def test_mlflow_tracking_uri_never_leaks_outside_the_two_training_containers() -> None:
    """학습 stage 밖으로 새면 안 된다 — 특히 codex-worker.

    Codex는 이슈 본문을 입력으로 저장소 코드를 고치는 LLM 실행이고, 그 container에는
    token volume이 붙지 않는다(credential 없는 stage). 이 PR과 짝을 이루는 infra 변경이
    executor egress에 MLflow(5000)를 여는데 같은 Pod의 container가 그 egress를
    공유하므로, 주소까지 주면 공용 tracking 서버가 LLM 실행의 사정거리에 들어온다.

    `_TRAINING_KEYS`에 합칠 수 없다 — 그 4개는 dataset URI만 있으면 **항상** 방출되지만
    이 값은 tracking URI가 **따로** 주어졌을 때만 방출되므로, 합치면
    `test_training_environment_is_limited_to_the_two_training_containers`의 부분집합
    단언이 깨진다.
    """
    containers = _containers(
        _settings(dataset_uri=_DATASET_URI, mlflow_tracking_uri=_MLFLOW_URI)
    )
    allowed = {"workspace-preparer", "candidate-finalizer"}
    for name, container in containers.items():
        if name in allowed:
            continue
        assert "MLFLOW_TRACKING_URI" not in _environment(container), (
            f"{name}에 tracking 좌표가 샜다"
        )
    # 회귀가 났을 때 어느 container인지 바로 보이도록 명시적으로 한 번 더 짚는다.
    assert "MLFLOW_TRACKING_URI" not in _environment(containers["codex-worker"])


def test_codex_worker_never_receives_the_dataset_coordinate() -> None:
    """Codex container는 데이터 좌표를 알 필요가 없다 — 최소 노출 원칙."""
    containers = _containers(_settings(dataset_uri=_DATASET_URI))
    assert "ORCH_TRAINING_DATASET_URI" not in _environment(containers["codex-worker"])


@pytest.mark.parametrize(
    "uri",
    [
        f"gs://bucket/prefix/{_DIGEST}/",
        f"gs://bucket/by-hash/{_DIGEST[:-1]}/",
        f"https://bucket/by-hash/{_DIGEST}/",
        "gs://bucket/by-hash/NOTAHASH/",
    ],
)
def test_malformed_dataset_uri_is_rejected_at_the_launcher(uri: str) -> None:
    """형식 오류를 Pod까지 끌고 가지 않는다 — 8 container를 띄운 뒤 죽으면 원인이 묻힌다."""
    with pytest.raises(LauncherConfigError, match="invalid training_dataset_uri"):
        _settings(dataset_uri=uri)


_RESULTS_ROOT = "gs://autoresearch-505505-autoresearch-dev-experiment-results"


def test_results_root_reaches_only_the_publishing_container() -> None:
    """게시 좌표는 채점·게시가 도는 container에만 붙는다 — 최소 노출 원칙.

    특히 Codex container에는 가면 안 된다. 게시 경로를 아는 것만으로 위험하지는
    않지만, 노출 목록이 넓어지는 것을 기본값으로 두면 다음 좌표도 따라 넓어진다.
    """
    containers = _containers(
        _settings(dataset_uri=_DATASET_URI, experiment_results_root=_RESULTS_ROOT)
    )

    assert (
        _environment(containers["candidate-finalizer"])["ORCH_EXPERIMENT_RESULTS_ROOT"]
        == _RESULTS_ROOT
    )
    for name, container in containers.items():
        if name == "candidate-finalizer":
            continue
        assert "ORCH_EXPERIMENT_RESULTS_ROOT" not in _environment(container), (
            f"{name}에 게시 좌표가 샜다"
        )


def test_results_root_is_absent_when_not_configured() -> None:
    """설정하지 않으면 아무것도 붙지 않는다 — executor가 "게시하지 않는 배포"로 읽는다."""
    containers = _containers(_settings(dataset_uri=_DATASET_URI))

    assert "ORCH_EXPERIMENT_RESULTS_ROOT" not in _environment(
        containers["candidate-finalizer"]
    )


@pytest.mark.parametrize(
    "root",
    ["s3://bucket", "bucket/prefix", "gs://", "gs:///prefix", "http://bucket"],
)
def test_malformed_results_root_is_rejected_at_the_launcher(root: str) -> None:
    """형식 오류를 Pod까지 끌고 가지 않는다.

    게시는 실험의 **마지막** 단계라, 여기서 막지 않으면 30분을 다 쓴 뒤에 실패하고
    그 실행의 산출물은 이미 사라진 뒤다.
    """
    with pytest.raises(LauncherConfigError, match="invalid experiment_results_root"):
        _settings(experiment_results_root=root)
