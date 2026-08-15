"""feast-apply 워크플로우의 prod/dev 배선 검증(#399, #548, #561).

bare ``feast`` CLI 는 ``feature_repo/bootstrap.py`` 를 거치지 않으므로, apply 스텝은
``FEAST_ONLINE_FULL_SCAN_FOR_DELETION`` 을 워크플로우가 직접 주입받는다. 그 파생
규칙(prod=true, dev=false)이 워크플로우 bash 에 **손으로 복제**돼 있어
``feature_repo/env.py`` 와 조용히 어긋날 수 있다. 여기서는 워크플로우의 실제
bash 조각을 꺼내 실행해 env.py 와 같은 값을 내는지 대조한다.

같은 이유로 #548 의 회귀도 여기서 고정한다 — 잡이 뜨는 GitHub Environment 이름이
워크플로우 텍스트를 꺼내 실행해야만 잡히는 실패라서다. #561 이후 apply는 GKE Job
이 아니라 셀프 호스티드 러너에서 직접 실행되므로, 코드 아카이브 대기·Job 렌더 관련
검증은 더 이상 존재하지 않는다.

feast 의존이 없어 기본 pytest 그룹에서 실행된다.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

import pytest
import yaml

from feature_repo.env import ENV_DEV, ENV_PROD, online_full_scan_for_deletion

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APPLY_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "feast-apply.yml"
CODE_ARCHIVE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "code-archive.yml"
pytestmark = pytest.mark.skipif(
    not APPLY_WORKFLOW.is_file() or not CODE_ARCHIVE_WORKFLOW.is_file(),
    reason="조직 자원 부재로 비활성화된 Feast·code archive 워크플로우 계약 테스트",
)

_DERIVATION_START = 'if [[ "$AUTORESEARCH_ENV" == "dev" ]]'
_APPLY_STEP = "Fetch Redis CA and run feast apply"

# `environment:` 키는 env 컨텍스트를 읽지 못해 같은 식을 잡 수준 env 와 runs-on
# 에도 각각 한 번 더 적어야 한다(세 사본). 어긋나면 Job 매니페스트의
# AUTORESEARCH_ENV 나 실제로 뜬 러너 스케일셋이 Environment 와 달라지므로, 문자열
# 동일성을 계약으로 고정한다.
ENVIRONMENT_EXPRESSION = (
    "${{ github.event_name == 'workflow_dispatch' && inputs.environment "
    "|| (github.ref_name == 'main' && 'prod' || 'dev') }}"
)


def _workflow_text() -> str:
    return APPLY_WORKFLOW.read_text(encoding="utf-8")


def _apply_job() -> dict:
    workflow = yaml.safe_load(_workflow_text())
    return workflow["jobs"]["feast-apply"]


def _step_script(step_name: str) -> str:
    for step in _apply_job()["steps"]:
        if step.get("name") == step_name:
            return step["run"]
    raise AssertionError(f"step not found: {step_name}")


def _triggers() -> dict:
    # YAML 1.1 은 따옴표 없는 `on:` 을 boolean True 로 읽는다.
    return yaml.safe_load(_workflow_text())[True]


def _dispatch_environment_options() -> set[str]:
    """수동 dispatch 가 고를 수 있는 환경 이름 집합."""
    return set(_triggers()["workflow_dispatch"]["inputs"]["environment"]["options"])


def _push_branches() -> list[str]:
    """push 트리거가 받는 브랜치 목록."""
    return list(_triggers()["push"]["branches"])


def _extract_derivation_snippet(workflow: str) -> str:
    """렌더 스텝의 full_scan 파생 bash 블록만 잘라내 들여쓰기를 제거한다."""
    lines = workflow.splitlines()
    start = next(
        index for index, line in enumerate(lines) if _DERIVATION_START in line
    )
    end = next(
        index
        for index, line in enumerate(lines[start:], start=start)
        if line.strip() == "fi"
    )
    return "\n".join(line.strip() for line in lines[start : end + 1])


@pytest.mark.parametrize("environment", ["prod", "dev"])
def test_workflow_full_scan_derivation_matches_env_module(environment: str) -> None:
    # Given: 워크플로우가 Job 에 주입할 full_scan 값을 계산하는 실제 bash 블록.
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable")
    snippet = _extract_derivation_snippet(_workflow_text())

    # When: env.py 와 동일한 환경값으로 그 블록을 실행한다.
    completed = subprocess.run(
        [bash, "-c", f'{snippet}\nprintf "%s" "$FEAST_ONLINE_FULL_SCAN_FOR_DELETION"'],
        env={"AUTORESEARCH_ENV": environment, "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=True,
    )

    # Then: 두 구현이 같은 결론에 도달해야 한다(드리프트 가드).
    expected = online_full_scan_for_deletion({"AUTORESEARCH_ENV": environment})
    assert completed.stdout == ("true" if expected else "false")


def test_workflow_routes_each_push_branch_to_its_own_environment() -> None:
    # main/dev push는 수동 입력이 없으므로 ref 에서 대상 환경을 골라야 한다. 다만
    # 고를 것은 브랜치 이름이 아니라 **Environment 이름**이다 — main 이라는
    # Environment 는 비어 있어서 좌표가 전부 저장소 수준으로 폴백된다(#548).
    workflow = _workflow_text()

    assert "branches: [main, dev]" in workflow
    assert ENVIRONMENT_EXPRESSION in workflow
    assert "|| github.ref_name }}" not in workflow


def test_environment_expression_only_yields_known_environment_names() -> None:
    # 표현식이 내놓을 수 있는 값은 수동 dispatch 입력과 ref 분기의 두 리터럴뿐이다.
    # 브랜치 이름이 그대로 새어 나가면 GitHub Environment 도, env.py 의
    # resolve_environment 도 함께 깨진다. 리터럴을 통째로 긁어 제외 목록을 빼는
    # 대신, ref 분기만 정확히 짚어 읽는다 — 비교 피연산자가 늘어도 오탐이 없다.
    ref_branch, matched_environment, fallback_environment = re.search(
        r"github\.ref_name == '([^']+)' && '([^']+)' \|\| '([^']+)'",
        ENVIRONMENT_EXPRESSION,
    ).groups()
    push_branches = _push_branches()

    # 삼항 하나로 갈라지므로 push 브랜치가 정확히 둘일 때만 전수 대응이 성립한다.
    assert set(push_branches) == {ref_branch, ENV_DEV}
    assert {matched_environment, fallback_environment} == {ENV_PROD, ENV_DEV}
    assert _dispatch_environment_options() == {ENV_PROD, ENV_DEV}


def test_job_environment_and_autoresearch_env_stay_in_sync() -> None:
    # `environment:` 는 env 컨텍스트를 읽지 못해 같은 식이 두 벌 존재한다. 한쪽만
    # 고치면 잡이 뜬 Environment 와 Job 에 주입되는 AUTORESEARCH_ENV 가 어긋난다.
    job = _apply_job()

    assert job["environment"] == ENVIRONMENT_EXPRESSION
    assert job["env"]["AUTORESEARCH_ENV"] == ENVIRONMENT_EXPRESSION


def test_runs_on_targets_the_scale_set_of_the_same_environment() -> None:
    # `runs-on` 은 env 컨텍스트를 읽지 못해 같은 식의 세 번째 사본이다. 여기만
    # 어긋나면 prod Environment 로 뜬 잡이 dev 스케일셋에서 돌거나, 매칭되는
    # 러너가 없어 큐에서 무한 대기한다.
    assert _apply_job()["runs-on"] == f"feast-apply-{ENVIRONMENT_EXPRESSION}"


def test_code_archive_uploads_dev_commit_for_dev_feast_apply() -> None:
    # feast-apply는 더 이상 이 아카이브를 기다리지 않지만(#561), GKE Job 롤백
    # 경로(deployment/feast/apply-job.yaml, deployment/Dockerfile.feast)는 여전히 이 아카이브를
    # 소비하므로 dev push도 계속 archive를 만들어야 한다.
    archive_workflow = CODE_ARCHIVE_WORKFLOW.read_text(encoding="utf-8")

    assert "branches: [main, dev]" in archive_workflow


def test_workflow_selects_environment_scoped_coordinates() -> None:
    # GitHub Environment 를 선택해야 같은 이름의 repo-level vars 보다
    # prod/dev Environment 좌표가 우선하며, 임시 dev 차단 가드는 더 이상 필요 없다.
    workflow = _workflow_text()

    assert f"    environment: {ENVIRONMENT_EXPRESSION}" in workflow
    assert "Guard against dev dispatch before dev coordinates are wired" not in workflow


def _run_validate_configuration(
    environment: str, wif_provider_id: str
) -> subprocess.CompletedProcess[str]:
    """설정 검증 스텝을 실행한다. 이름 검사 외 변수는 모두 채워 둔다."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable")

    step_env = {
        name: "filled"
        for name in (
            "GCP_PROJECT_ID",
            "GCP_REGION",
            "BQ_DATASET",
            "GCS_REGISTRY_PATH",
            "GCS_STAGING_LOCATION",
            "REDIS_HOST",
            "REDIS_PORT",
            "REDIS_CA_SECRET_ID",
        )
    }
    step_env["WIF_PROVIDER_ID"] = wif_provider_id
    step_env["AUTORESEARCH_ENV"] = environment
    step_env["PATH"] = os.environ.get("PATH", "")

    return subprocess.run(
        [bash, "-e", "-o", "pipefail", "-c", _step_script("Validate required configuration")],
        env=step_env,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("environment", [ENV_PROD, ENV_DEV])
def test_validate_configuration_accepts_the_matching_wif_provider(
    environment: str,
) -> None:
    completed = _run_validate_configuration(
        environment, f"projects/1/…/providers/github-feast-{environment}"
    )

    assert completed.returncode == 0


def test_validate_configuration_rejects_the_repository_level_fallback_provider() -> None:
    # #548 에서 실제로 온 값. 비어 있지 않으므로 존재 검사만으로는 통과해 버린다.
    completed = _run_validate_configuration(
        ENV_PROD, "projects/1/locations/global/workloadIdentityPools/p/providers/github"
    )

    assert completed.returncode == 1
    assert "does not match the prod environment" in completed.stdout


def test_credentials_are_verified_before_the_apply_step() -> None:
    # 자격 확인을 apply 뒤에 두면 #548 류의 오인 보고가 apply 가 끝난 뒤에야 드러난다.
    step_names = [step.get("name") for step in _apply_job()["steps"]]

    assert step_names.index(
        "Verify the environment credentials"
    ) < step_names.index(_APPLY_STEP)


def test_credential_check_never_prints_the_access_token(tmp_path: Path) -> None:
    # 토큰이 로그로 새면 이 스텝 자체가 사고가 된다.
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable")
    token = "ya29.SECRET-ACCESS-TOKEN"
    gcloud_stub = tmp_path / "gcloud"
    gcloud_stub.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' {shlex.quote(token)}\nexit 0\n",
        encoding="utf-8",
    )
    gcloud_stub.chmod(0o755)

    completed = subprocess.run(
        [
            bash,
            "-e",
            "-o",
            "pipefail",
            "-c",
            _step_script("Verify the environment credentials"),
        ],
        env={
            "AUTORESEARCH_ENV": ENV_PROD,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert token not in completed.stdout
    assert token not in completed.stderr
