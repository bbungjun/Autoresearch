"""Agent Orchestration API·Runner 이미지 경계 계약."""

import ast
from pathlib import Path

import pytest
import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
API_DOCKERFILE = REPOSITORY_ROOT / "deployment" / "experiment_platform" / "api.Dockerfile"
RUNNER_DOCKERFILE = (
    REPOSITORY_ROOT / "deployment" / "experiment_platform" / "runner.Dockerfile"
)
LAUNCHER_DOCKERFILE = (
    REPOSITORY_ROOT / "deployment" / "experiment_platform" / "launcher.Dockerfile"
)
EXECUTOR_DOCKERFILE = (
    REPOSITORY_ROOT / "deployment" / "experiment_platform" / "executor.Dockerfile"
)
DOCKERIGNORE = REPOSITORY_ROOT / ".dockerignore"
RELEASE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
API_LLM_MODULE = REPOSITORY_ROOT / "applications" / "experiment_platform" / "api" / "llm.py"
_requires_active_release_workflow = pytest.mark.skipif(
    not RELEASE_WORKFLOW.is_file(),
    reason="조직 자원 부재로 비활성화된 release.yml 워크플로우 계약 테스트",
)


def test_api_image_excludes_codex_and_runner_image_pins_codex() -> None:
    """API에는 Codex 실행 표면이 없고 Runner만 검증된 CLI를 설치한다."""
    api_dockerfile = API_DOCKERFILE.read_text(encoding="utf-8")
    runner_dockerfile = RUNNER_DOCKERFILE.read_text(encoding="utf-8")

    assert "@openai/codex" not in api_dockerfile
    assert "node:" not in api_dockerfile
    assert "CODEX_HOME" not in api_dockerfile
    assert "@openai/codex@0.146.0" in runner_dockerfile
    assert "CODEX_HOME=/var/lib/codex" in runner_dockerfile
    assert "TMPDIR=/tmp" in runner_dockerfile
    assert "COPY --from=codex-cli /usr/local/bin/codex /usr/local/bin/codex" not in (
        runner_dockerfile
    )
    assert (
        "ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js "
        "/usr/local/bin/codex"
    ) in runner_dockerfile


def test_orchestration_images_install_only_runtime_group_and_run_as_fixed_user() -> None:
    """두 역할 이미지는 같은 최소 Python 의존성과 비루트 UID/GID를 사용한다."""
    for dockerfile_path in (API_DOCKERFILE, RUNNER_DOCKERFILE):
        dockerfile = dockerfile_path.read_text(encoding="utf-8")

        assert "FROM ghcr.io/astral-sh/uv:0.11.26 AS lock-export" in dockerfile
        assert '"--only-group", "orchestration"' in dockerfile
        assert '"--no-dev", "--group", "orchestration"' not in dockerfile
        assert "addgroup --gid 10001 appuser" in dockerfile
        assert "adduser --uid 10001 --gid 10001" in dockerfile
        assert "USER appuser" in dockerfile


def test_revision_label_preserves_runtime_dependency_cache() -> None:
    """소스 revision 라벨은 대용량 런타임 의존성 설치 이후에 설정한다."""
    label = 'LABEL org.opencontainers.image.revision="${VCS_REF}"'

    for dockerfile_path in (API_DOCKERFILE, RUNNER_DOCKERFILE):
        dockerfile = dockerfile_path.read_text(encoding="utf-8")

        assert dockerfile.index(label) > dockerfile.index(
            "RUN python -m pip install --no-cache-dir --no-deps -r requirements.lock"
        )


def test_orchestration_images_do_not_embed_runtime_secrets() -> None:
    """빌드 문맥의 인증·환경·DB 값을 이미지 명령으로 반입하지 않는다."""
    forbidden_values = ("auth.json", ".env", "DATABASE_URL", "ORCH_DATABASE_URL")

    for dockerfile_path in (API_DOCKERFILE, RUNNER_DOCKERFILE):
        dockerfile = dockerfile_path.read_text(encoding="utf-8")
        assert not any(value in dockerfile for value in forbidden_values)

    dockerignore = DOCKERIGNORE.read_text(encoding="utf-8")
    assert ".codex" in dockerignore
    assert ".env" in dockerignore
    assert "**/auth.json" in dockerignore


def test_api_and_runner_images_copy_only_their_runtime_modules() -> None:
    """각 이미지는 상대 역할의 애플리케이션 모듈을 포함하지 않는다."""
    api_dockerfile = API_DOCKERFILE.read_text(encoding="utf-8")
    runner_dockerfile = RUNNER_DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY applications/experiment_platform/api ./applications/experiment_platform/api" in api_dockerfile
    assert "COPY applications/experiment_platform/shared/contracts.py ./applications/experiment_platform/shared/" in api_dockerfile
    assert "COPY applications/experiment_platform/shared/bootstrap_secrets.py ./applications/experiment_platform/shared/" in api_dockerfile
    assert "COPY applications/experiment_platform/entrypoint.sh ./applications/experiment_platform/" in api_dockerfile
    assert "COPY applications/experiment_platform/runner" not in api_dockerfile
    assert "COPY applications/experiment_platform/shared/codex.py" not in api_dockerfile

    assert "COPY applications/experiment_platform/runner ./applications/experiment_platform/runner" in runner_dockerfile
    assert "COPY applications/experiment_platform/shared/codex.py ./applications/experiment_platform/shared/" in runner_dockerfile
    assert "COPY applications/experiment_platform/shared/contracts.py ./applications/experiment_platform/shared/" in runner_dockerfile
    assert "COPY applications/experiment_platform/runner_entrypoint.sh ./applications/experiment_platform/" in runner_dockerfile
    assert "COPY applications/experiment_platform/api" not in runner_dockerfile


def test_api_llm_module_defers_codex_execution_import() -> None:
    """Runner 전용 실행 모듈 없이 API 앱을 import할 수 있어야 한다."""
    module = ast.parse(API_LLM_MODULE.read_text(encoding="utf-8"))
    top_level_imports = {
        node.module
        for node in module.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert "applications.experiment_platform.shared.codex" not in top_level_imports


@_requires_active_release_workflow
def test_release_workflow_publishes_api_and_runner_digests() -> None:
    """Release는 동일 source SHA의 API·Runner immutable digest를 각각 발행한다."""
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    api_dockerfile = API_DOCKERFILE.read_text(encoding="utf-8")
    runner_dockerfile = RUNNER_DOCKERFILE.read_text(encoding="utf-8")

    assert "publish-agent-orchestration-api-image:" in workflow
    assert "publish-agent-orchestration-runner-image:" in workflow
    assert "file: deployment/experiment_platform/api.Dockerfile" in workflow
    assert "file: deployment/experiment_platform/runner.Dockerfile" in workflow
    assert "autoresearch-agent-orchestration-api" in workflow
    assert "autoresearch-agent-orchestration-runner" in workflow
    assert workflow.count("needs: publish-application-image") >= 3
    assert workflow.count("org.opencontainers.image.revision") >= 4
    assert workflow.count("digest_ref=$digest_ref") >= 4

    for dockerfile in (api_dockerfile, runner_dockerfile):
        assert "ARG VCS_REF=unknown" in dockerfile
        assert 'org.opencontainers.image.revision="${VCS_REF}"' in dockerfile


def test_launcher_image_is_a_locked_non_root_runtime_image() -> None:
    """Launcher는 Phase 1의 최소 runtime과 역할별 command를 유지한다."""
    dockerfile = LAUNCHER_DOCKERFILE.read_text(encoding="utf-8")

    assert "FROM ghcr.io/astral-sh/uv:0.11.26 AS lock-export" in dockerfile
    assert '"--only-group", "orchestration"' in dockerfile
    assert "addgroup --gid 10001 appuser" in dockerfile
    assert "adduser --uid 10001 --gid 10001" in dockerfile
    assert "USER appuser" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "COPY applications/experiment_platform/launcher ./applications/experiment_platform/launcher" in dockerfile
    assert 'CMD ["python", "-m", "applications.experiment_platform.launcher.main"]' in dockerfile
    assert "ARG VCS_REF=unknown" in dockerfile
    assert 'org.opencontainers.image.revision="${VCS_REF}"' in dockerfile
    assert "@openai/codex" not in dockerfile
    assert "node:" not in dockerfile


def test_executor_image_seals_the_phase2_runtime_contract() -> None:
    """Executor는 clone source와 독립된 Git·uv·Codex 검증 runtime을 제공한다.

    이 테스트가 잡는 변경: executor가 dev 검증 도구, Codex 또는 image-봉인 issue
    parser 없이 빌드되어 Stage 6의 어느 container라도 실행하지 못하는 회귀.
    """
    dockerfile = EXECUTOR_DOCKERFILE.read_text(encoding="utf-8")

    assert "FROM ghcr.io/astral-sh/uv:0.11.26 AS lock-export" in dockerfile
    assert '"--group", "dev"' in dockerfile
    assert '"--no-group", "feast"' in dockerfile
    assert "FROM node:22.16.0-slim AS codex-cli" in dockerfile
    assert "@openai/codex@0.146.0" in dockerfile
    # libgomp1은 lightgbm이 runtime에 dlopen하는 OpenMP다. python:3.12-slim에
    # 없으므로 빠지면 Phase 2 학습이 import 시점에 OSError로 죽는다.
    assert "apt-get install --yes --no-install-recommends git libgomp1" in dockerfile
    assert "COPY --from=lock-export /uv /usr/local/bin/uv" in dockerfile
    assert "UV_PROJECT_ENVIRONMENT=/opt/autoresearch-venv" in dockerfile
    assert "PATH=/opt/autoresearch-venv/bin:${PATH}" in dockerfile
    assert "uv venv /opt/autoresearch-venv" in dockerfile
    assert "uv pip install --python /opt/autoresearch-venv/bin/python" in dockerfile
    assert "COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node" in dockerfile
    assert "COPY --from=codex-cli /usr/local/lib/node_modules /usr/local/lib/node_modules" in dockerfile
    assert "COPY tools/__init__.py ./tools/" in dockerfile
    assert "COPY tools/auto_research_issue_branch.py ./tools/" in dockerfile
    assert "COPY applications/experiment_platform/executor ./applications/experiment_platform/executor" in dockerfile
    assert "COPY . ." not in dockerfile
    assert "COPY autoresearch" not in dockerfile
    assert "COPY src" not in dockerfile
    assert ".env" not in dockerfile
    assert "auth.json" not in dockerfile
    assert "addgroup --gid 10001 appuser" in dockerfile
    assert "adduser --uid 10001 --gid 10001" in dockerfile
    assert "USER appuser" in dockerfile


@_requires_active_release_workflow
def test_executor_release_verification_runs_phase2_toolchain_and_entrypoints() -> None:
    """Release가 immutable executor digest에서 실제 Stage 6 runtime을 점검한다.

    이 테스트가 잡는 변경: digest는 발행하지만 Git·uv·Node·Codex 또는 Stage 6
    module import를 검증하지 않아 배포 후에만 executor 실패가 드러나는 회귀.
    """
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index("  publish-agent-orchestration-executor-image:")
    end = workflow.index("  promote-airflow-digest:", start)
    executor_job = workflow[start:end]

    for command in ("git --version", "uv --version", "node --version", "codex --version"):
        assert command in executor_job
    for module in (
        "applications.experiment_platform.executor.main",
        "applications.experiment_platform.executor.token_minter",
        "applications.experiment_platform.executor.workspace",
        "applications.experiment_platform.executor.codex_worker",
        "applications.experiment_platform.executor.verifier",
        "applications.experiment_platform.executor.finalizer",
        "applications.experiment_platform.executor.phase2",
    ):
        assert module in executor_job


def _load_ci_workflow() -> dict[str, object]:
    parsed = yaml.load(CI_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(parsed, dict)
    return parsed


def test_pr_ci_builds_and_smokes_the_executor_image_contract() -> None:
    """PR CI가 release 전에 executor runtime·sealed parser를 실제로 검증한다.

    이 테스트가 잡는 변경: agent orchestration 경로의 PR에서 executor Dockerfile이
    build되지 않거나 runtime clone의 tools mount가 image 봉인 parser를 가리는 회귀.
    """
    workflow = _load_ci_workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["docker-build-agent-orchestration"]
    assert isinstance(job, dict)
    assert job["needs"] == "changes"
    assert job["if"] == "needs.changes.outputs.experiment_platform == 'true'"

    steps = job["steps"]
    assert isinstance(steps, list)
    build_step = next(step for step in steps if step["name"].startswith("Build Agent"))
    smoke_step = next(step for step in steps if step["name"].startswith("Run Agent"))
    build_script = build_step["run"]
    smoke_script = smoke_step["run"]
    assert isinstance(build_script, str)
    assert isinstance(smoke_script, str)

    assert "deployment/experiment_platform/executor.Dockerfile" in build_script
    assert "autoresearch-agent-orchestration-executor:ci" in build_script
    assert "--read-only" in smoke_script
    assert 'test "$(id -u)" = "10001"' in smoke_script
    assert 'test "$(id -g)" = "10001"' in smoke_script
    assert 'test "$UV_PROJECT_ENVIRONMENT" = "/opt/autoresearch-venv"' in smoke_script
    for command in ("git --version", "uv --version", "node --version", "codex --version"):
        assert command in smoke_script
    for module in (
        "applications.experiment_platform.executor.main",
        "applications.experiment_platform.executor.token_minter",
        "applications.experiment_platform.executor.workspace",
        "applications.experiment_platform.executor.codex_worker",
        "applications.experiment_platform.executor.verifier",
        "applications.experiment_platform.executor.finalizer",
        "applications.experiment_platform.executor.phase2",
    ):
        assert module in smoke_script
    assert "/tmp/executor-runtime-clone/tools" in smoke_script
    assert "/workspace/repository/tools:ro" in smoke_script
    assert 'tools.__file__ == \\"/app/tools/__init__.py\\"' in smoke_script


def _load_release_workflow() -> dict[str, object]:
    parsed = yaml.load(
        RELEASE_WORKFLOW.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(parsed, dict)
    return parsed


@_requires_active_release_workflow
@pytest.mark.parametrize(
    ("job_name", "dockerfile", "image_name", "import_modules"),
    (
        (
            "publish-agent-orchestration-launcher-image",
            "deployment/experiment_platform/launcher.Dockerfile",
            "autoresearch-agent-orchestration-launcher",
            ("applications.experiment_platform.launcher.main",),
        ),
        (
            "publish-agent-orchestration-executor-image",
            "deployment/experiment_platform/executor.Dockerfile",
            "autoresearch-agent-orchestration-executor",
            (
                "applications.experiment_platform.executor.main",
                "applications.experiment_platform.executor.token_minter",
            ),
        ),
    ),
)
def test_release_workflow_publishes_branch_job_runtime_digests(
    job_name: str,
    dockerfile: str,
    image_name: str,
    import_modules: tuple[str, ...],
) -> None:
    """Release의 역할별 job이 독립 image를 push하고 digest·module을 검증한다."""
    workflow = _load_release_workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert job_name in jobs

    job = jobs[job_name]
    assert isinstance(job, dict)
    assert job["needs"] == "publish-application-image"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert job["outputs"] == {
        "digest_ref": "${{ steps.verify.outputs.digest_ref }}",
        "source_sha": "${{ steps.source.outputs.sha }}",
    }

    steps = job["steps"]
    assert isinstance(steps, list)
    checkout = next(step for step in steps if step.get("uses") == "actions/checkout@v6")
    assert checkout["with"]["ref"] == (
        "${{ needs.publish-application-image.outputs.source_sha }}"
    )

    image_step = next(step for step in steps if step.get("id") == "image")
    image_script = image_step["run"]
    assert isinstance(image_script, str)
    assert f"/{image_name}" in image_script
    assert 'sha_ref="${image_uri}:sha-${SOURCE_SHA}"' in image_script

    build_step = next(
        step for step in steps if step.get("uses") == "docker/build-push-action@v6"
    )
    assert build_step["with"] == {
        "context": ".",
        "file": dockerfile,
        "push": "true",
        "build-args": "VCS_REF=${{ steps.source.outputs.sha }}\n",
        "tags": "${{ steps.image.outputs.tags }}",
    }

    verify_step = next(step for step in steps if step.get("id") == "verify")
    assert verify_step["env"] == {
        "DIGEST": "${{ steps.build.outputs.digest }}",
        "IMAGE_URI": "${{ steps.image.outputs.uri }}",
        "SOURCE_SHA": "${{ steps.source.outputs.sha }}",
    }
    verify_script = verify_step["run"]
    assert isinstance(verify_script, str)
    assert "^sha256:[0-9a-f]{64}$" in verify_script
    assert 'digest_ref="${IMAGE_URI}@${DIGEST}"' in verify_script
    assert "org.opencontainers.image.revision" in verify_script
    assert "image_user" in verify_script
    assert 'echo "digest_ref=$digest_ref" >> "$GITHUB_OUTPUT"' in verify_script
    for module in import_modules:
        assert module in verify_script


LAUNCHER_ENTRYPOINTS = (
    "applications.experiment_platform.launcher.main",
    "applications.experiment_platform.launcher.log_collector",
    "applications.experiment_platform.launcher.pull_request",
)


def _module_source(module: str) -> Path | None:
    """모듈 이름을 저장소 안의 파일 경로로 바꾼다. 저장소 밖이면 `None`."""
    relative = Path(module.replace(".", "/"))
    for candidate in (
        REPOSITORY_ROOT / relative.with_suffix(".py"),
        REPOSITORY_ROOT / relative / "__init__.py",
    ):
        if candidate.is_file():
            return candidate
    return None


def _imported_repository_modules(source: Path) -> set[str]:
    """한 파일이 import하는 `applications.experiment_platform.*` 모듈.

    함수 안 import도 센다 — `PullRequestSettings.from_environment`처럼 지연 import한
    모듈도 실행 시점에는 이미지 안에 있어야 한다. `ast.walk`가 중첩을 함께 훑는다.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            # `from a.b import c`의 `c`는 모듈일 수도 이름일 수도 있다. 둘 다 후보로
            # 넣고 파일이 있는 것만 남긴다.
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return {
        module
        for module in modules
        if module.startswith("applications.experiment_platform") and _module_source(module) is not None
    }


def _reachable_repository_modules(entrypoints: tuple[str, ...]) -> set[str]:
    """entrypoint에서 전이적으로 도달하는 저장소 모듈 전체."""
    reached: set[str] = set()
    pending = list(entrypoints)
    while pending:
        module = pending.pop()
        if module in reached:
            continue
        reached.add(module)
        source = _module_source(module)
        if source is not None:
            pending.extend(_imported_repository_modules(source))
    return reached


def _copied_sources(dockerfile: str) -> set[str]:
    """Dockerfile이 이미지로 복사하는 저장소 경로."""
    copied: set[str] = set()
    for line in dockerfile.splitlines():
        stripped = line.strip()
        # `applications/` 로 넓게 잡는다 — `COPY applications/__init__.py` 를 놓치면
        # applications 패키지 자체가 수집되지 않아 가드가 헐거워진다 (#754).
        if not stripped.startswith("COPY applications/"):
            continue
        copied.add(stripped.split()[1])
    return copied


def test_launcher_image_copies_every_module_its_entrypoints_import() -> None:
    """launcher image의 entrypoint 셋이 import하는 모듈이 모두 이미지에 있어야 한다.

    이 목록은 경로를 **열거**해 복사하므로 `applications/experiment_platform/`에 파일을 두는 것만으로는
    이미지에 들어가지 않는다. 그래서 빠뜨려도 빌드는 통과하고, 그 모듈을 import하는
    entrypoint의 컨테이너만 기동 즉시 ModuleNotFoundError로 죽는다 — 릴리스가 배포된
    뒤에야 드러난다. 같은 누락이 두 번 났다(`bootstrap_secrets.py`,
    `github_pull_requests.py` #700).
    """
    copied = _copied_sources(LAUNCHER_DOCKERFILE.read_text(encoding="utf-8"))

    def is_covered(module: str) -> bool:
        relative = Path(module.replace(".", "/"))
        candidates = {f"{relative}.py", f"{relative}/__init__.py"}
        # 디렉토리 통째로 복사한 줄(`COPY applications/experiment_platform/launcher ...`)이 덮는다.
        candidates.update(str(parent) for parent in relative.parents)
        return bool(candidates & copied)

    missing = sorted(
        module
        for module in _reachable_repository_modules(LAUNCHER_ENTRYPOINTS)
        if not is_covered(module)
    )

    assert missing == [], (
        "launcher.Dockerfile의 COPY 목록에 없는 모듈을 entrypoint가 import한다: "
        f"{missing}"
    )
