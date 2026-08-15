"""브랜치 보호 문서가 근거로 삼는 유지 중인 워크플로 사실을 CI에서 고정한다.

전체 파이프라인 기준으로 이 모듈은 **실험 실행이나 판정에 관여하지 않는다.**
`CONTRIBUTING.md`와 `.claude/docs/agent-workflow-reference.md`의 "브랜치 보호
규칙" 절이 단언하는 워크플로 사실만 검증하는 문서-코드 정합성 테스트다.
브랜치 생성은 executor Pod가, 후보 판정은 `tools/auto_research_issue_branch.py`가
담당하며 여기서 다루지 않는다.

두 문서는 `dev`에 PR 필수와 required status check를 걸지 않은 이유를 워크플로
동작으로 설명한다. 그 설명이 조용히 낡는 것을 막기 위해 다음을 고정한다.

- dev 병합이 PR을 거치지 않고 ref를 직접 갱신한다는 사실.
- `ci.yml`/`lint.yml`의 `push`가 `main` 전용이고 `pull_request`에는 branch
  필터가 없다는 사실. 두 가지가 함께 "`repos.merge`로 만든 dev 커밋에는 CI가
  돌지 않는다"의 근거다.
- 문서가 인용하는 승격 fail-closed 메시지 문자열의 실재.
- main-protection의 required status check 컨텍스트 6개가 실제 job 이름으로
  존재한다는 사실.
"""

from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"

DEV_PROMOTION_WORKFLOW = WORKFLOWS / "auto-research-dev-promotion.yml"
PROMOTION_WORKFLOW = WORKFLOWS / "auto-research-promotion.yml"
CI_WORKFLOW = WORKFLOWS / "ci.yml"
LINT_WORKFLOW = WORKFLOWS / "lint.yml"
_requires_active_promotion_workflows = pytest.mark.skipif(
    not DEV_PROMOTION_WORKFLOW.is_file() or not PROMOTION_WORKFLOW.is_file(),
    reason="조직 자동 실험 경로 부재로 비활성화된 promotion 워크플로우 계약 테스트",
)

# main-protection(ruleset 18360502)의 required status check 컨텍스트.
REQUIRED_STATUS_CHECK_CONTEXTS = (
    "Ruff",
    "pytest (Python 3.11)",
    "pytest (Python 3.12)",
    "pytest (feast group)",
    "uv lock & proxy export drift",
    "Docker build",
)


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


@_requires_active_promotion_workflows
def test_dev_promotion_merges_dev_ref_without_pull_request() -> None:
    """dev 병합이 PR을 거치지 않고 ref를 직접 갱신함을 고정한다.

    이 호출이 `dev`에 `pull_request` rule을 걸 수 없는 직접 근거다.
    """
    workflow_text = DEV_PROMOTION_WORKFLOW.read_text(encoding="utf-8")

    assert "github.rest.repos.merge({" in workflow_text
    assert "base: 'dev'," in workflow_text


def test_ci_and_lint_push_triggers_are_main_only() -> None:
    """`repos.merge`가 만든 dev 커밋에 check run이 생기지 않음을 고정한다.

    `push`가 `main` 전용이라는 사실과, `pull_request`에 branch 필터가 없어
    base가 `dev`인 PR에서는 컨텍스트가 정상 생성된다는 사실을 함께 고정한다.
    후자가 빠지면 "dev에는 지정할 컨텍스트가 없다"는 틀린 서술이 되살아난다.
    """
    for path in (CI_WORKFLOW, LINT_WORKFLOW):
        triggers = _load(path)["on"]

        assert triggers["push"]["branches"] == ["main"], path.name
        assert "branches" not in triggers["pull_request"], path.name


@_requires_active_promotion_workflows
def test_documented_promotion_fail_closed_messages_exist() -> None:
    """문서가 인용하는 승격 fail-closed 메시지가 실재함을 고정한다.

    line 번호 대신 grep 가능한 앵커로 문서와 코드를 잇는다.
    """
    promotion_text = PROMOTION_WORKFLOW.read_text(encoding="utf-8")

    assert "refusing to reuse it for" in promotion_text
    assert "candidate_sha must be an ancestor of dev" in promotion_text


def test_required_status_check_contexts_exist_as_job_names() -> None:
    """required status check 6개가 실제 job 이름으로 존재함을 고정한다.

    ruleset은 컨텍스트를 이름 문자열로 요구하므로, job 이름이 바뀌면 `main`
    머지가 영구히 막힌다. `Ruff`만 `lint.yml` 소유다.
    """
    job_names = set()
    for path in (CI_WORKFLOW, LINT_WORKFLOW):
        for job in _load(path)["jobs"].values():
            name = job.get("name")
            if name is None:
                continue
            if "${{ matrix.python-version }}" in name:
                job_names.update(
                    name.replace("${{ matrix.python-version }}", version)
                    for version in ("3.11", "3.12")
                )
            else:
                job_names.add(name)

    missing = [c for c in REQUIRED_STATUS_CHECK_CONTEXTS if c not in job_names]
    assert not missing, f"required status check 컨텍스트가 사라졌습니다: {missing}"
