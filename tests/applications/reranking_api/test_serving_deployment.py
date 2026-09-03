"""학습 결과를 제공하는 서빙 이미지와 배포 전 CI 실행 계약을 검증한다.

의존성·소스 포함 및 캐시 이미지의 smoke 연결을 검사한다. 모델 학습과 실제
서빙 요청 처리는 autoresearch 및 applications.reranking_api가 담당한다.
"""

from pathlib import Path
import tomllib

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SERVING_DOCKERFILE = REPOSITORY_ROOT / "deployment" / "serving" / "Dockerfile"
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"


def test_feast_group_requires_sdk_compatible_pyarrow() -> None:
    with PYPROJECT.open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    feast_dependencies = pyproject["dependency-groups"]["feast"]
    assert "pyarrow>=21.0.0,<22" in feast_dependencies


def test_serving_image_installs_feast_compatible_group() -> None:
    dockerfile = SERVING_DOCKERFILE.read_text(encoding="utf-8")

    assert '"--no-dev", "--group", "feast"' in dockerfile
    assert '"--group", "serving"' not in dockerfile


def test_serving_runtime_installs_lightgbm_native_dependency() -> None:
    # Given: the production serving image definition.
    dockerfile = SERVING_DOCKERFILE.read_text(encoding="utf-8")

    # When: its runtime package installation is inspected.
    runtime_stage = dockerfile.split("FROM python:3.12-slim", maxsplit=1)[1]

    # Then: LightGBM's OpenMP library is installed before dropping privileges.
    assert "apt-get update" in runtime_stage
    assert "apt-get install --no-install-recommends -y libgomp1" in runtime_stage
    assert "rm -rf /var/lib/apt/lists/*" in runtime_stage
    assert runtime_stage.index("libgomp1") < runtime_stage.index("USER appuser")


def test_serving_image_copies_app_feature_repo_and_bootstrap_package() -> None:
    """서빙 앱 본체가 이미지에 없으면 컨테이너가 기동 즉시 죽는다.

    `applications/__init__.py`를 따로 단언하는 이유는 그것이 없으면 디렉터리만 복사되고
    패키지 import(`applications.reranking_api.app`)가 런타임에 실패하기 때문이다 — 빌드는
    통과하므로 배포된 뒤에야 드러난다 (#754).
    """
    dockerfile = SERVING_DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY autoresearch ./autoresearch" in dockerfile
    assert "COPY feature_repo ./feature_repo" in dockerfile
    assert "COPY applications/__init__.py ./applications/" in dockerfile
    assert "COPY applications/reranking_api ./applications/reranking_api" in dockerfile


def test_serving_image_embeds_source_revision_and_runs_non_root() -> None:
    dockerfile = SERVING_DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG VCS_REF=unknown" in dockerfile
    assert 'LABEL org.opencontainers.image.revision="${VCS_REF}"' in dockerfile
    assert "USER appuser" in dockerfile


def test_ci_builds_serving_image_and_runs_import_smoke() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    job = yaml.safe_load(workflow)["jobs"]["docker-build-serving"]
    build = next(
        step
        for step in job["steps"]
        if step.get("with", {}).get("file") == "deployment/serving/Dockerfile"
    )
    assert build["uses"].startswith("docker/build-push-action@")
    assert build["with"]["tags"] == "autoresearch-serving:ci"
    assert build["with"]["load"] is True
    assert (
        "import lightgbm, feast, fastapi, feature_repo.redis_iam, applications.reranking_api.app"
        in workflow
    )
    assert "tests/applications/reranking_api/test_serving_feast_reader.py" in workflow
    assert (
        "tests/applications/reranking_api/test_serving_feast_reader_feast.py"
        in workflow
    )
    assert "tests/applications/reranking_api/test_serving_api.py" in workflow
    assert "tests/applications/reranking_api/test_serving_deployment.py" in workflow


def test_ci_checks_serving_image_dependencies_and_feature_store_bootstrap() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "python -m pip check" in workflow
    assert "from feature_repo.bootstrap import load_feature_store" in workflow
    assert "load_feature_store('/app/feature_repo')" in workflow


def test_ci_smokes_serving_http_contract_while_unready_and_cleans_up() -> None:
    # Given: the production serving-image CI workflow.
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    # When: its detached-container smoke contract is inspected.
    # Then: request validation precedes readiness and health remains fail-closed.
    assert "Run serving image fail-closed HTTP contract smoke" in workflow
    assert "docker run --detach" in workflow
    assert "trap cleanup EXIT" in workflow
    assert "curl --request POST" in workflow
    assert "Content-Type: application/json" in workflow
    assert "--data '{}'" in workflow
    assert "/rerank" in workflow
    assert '"${rerank_status_code}" = "422"' in workflow
    assert "/healthcheck" in workflow
    assert '"${healthcheck_status_code}" = "503"' in workflow
