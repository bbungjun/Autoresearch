"""Auto Research 분류·승격 label 문자열이 다섯 곳에서 어긋나지 않게 고정한다.

전체 파이프라인 기준으로 이 모듈은 **실험 실행이나 판정에 관여하지 않는다.**
가설 이슈를 Auto Research로 분류하고 승격 단계에서 확인하는 label 계약만 검증한다.
본문 파싱과 executor Pod의 브랜치 생성은 각각
`tools/auto_research_issue_branch.py`와 `applications.experiment_platform.executor`가,
승격 판정은 `autoresearch/experiments/promotion_gate.py`가 담당하며 여기서 다루지 않는다.

label 문자열은 Issue Form·API 발행 경로·승격 워크플로·문서 2개에 흩어져 있다.
`auto-research-promotion.yml`의 가드가 어긋나면 실행 전까지 드러나지 않는다 — #495에서
`promotion_gate._LABELS`가 실제 Issue Form에 없는 label을 가리킨 채 도입 이래 한 번도
동작하지 않았던 것이 같은 실패 유형이다.

[기능] Issue Form의 `labels:`를 정본으로 삼아 나머지 네 곳이 같은 label을
말하는지 검사한다.

- Form이 정확히 하나의 분류 label만 부여한다는 사실
- 승격 워크플로의 이슈 가드가 같은 label을 요구한다는 사실
- 두 문서의 Issue Form 표가 Form의 `labels:`와 정확히 같은 집합을 싣는다는 사실

[비책임] label이 GitHub 저장소에 실제로 존재하는지는 검사하지 않는다(네트워크
접근 없음). label 생성은 저장소 밖 조치이며 `CONTRIBUTING.md`가 안내한다.
"""

import re
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

ISSUE_FORM = REPOSITORY_ROOT / ".github" / "ISSUE_TEMPLATE" / "auto_research.yml"
PROMOTION_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "auto-research-promotion.yml"
)
CONTRIBUTING = REPOSITORY_ROOT / "CONTRIBUTING.md"
WORKFLOW_REFERENCE = (
    REPOSITORY_ROOT / ".claude" / "docs" / "agent-workflow-reference.md"
)
_requires_active_promotion_workflow = pytest.mark.skipif(
    not PROMOTION_WORKFLOW.is_file(),
    reason="조직 자동 실험 경로 부재로 비활성화된 auto-research-promotion.yml 계약 테스트",
)

DOCUMENTS_WITH_ISSUE_FORM_TABLE = (CONTRIBUTING, WORKFLOW_REFERENCE)

# 두 문서 모두 `| `auto_research.yml` | `[AR]` | <자동 label> | <설명> |` 형식의
# Issue Form 표를 가진다. 표에서 자동 label 칸만 뽑아 Form과 대조한다.
_FORM_TABLE_ROW = re.compile(
    r"^\|\s*`auto_research\.yml`\s*\|(?P<rest>.*)$", re.MULTILINE
)
_BACKTICKED = re.compile(r"`([^`]+)`")


def _form_labels() -> list[str]:
    """Issue Form이 자동 부여하는 label 목록 — 이 계약의 정본."""
    form = yaml.safe_load(ISSUE_FORM.read_text(encoding="utf-8"))
    labels = form["labels"]
    assert isinstance(labels, list)
    return labels


def _documented_labels(path: Path) -> list[str]:
    """문서의 Issue Form 표에서 `auto_research.yml` 행의 자동 label 칸을 읽는다."""
    matches = _FORM_TABLE_ROW.findall(path.read_text(encoding="utf-8"))
    assert len(matches) == 1, (
        f"{path.name}에 auto_research.yml 표 행이 {len(matches)}개 있습니다 (1개여야 함)"
    )
    # rest = " `[AR]` | `auto-experiment` | 설명 |" — 두 번째 칸이 자동 label이다.
    cells = matches[0].split("|")
    assert len(cells) >= 3, f"{path.name}의 표 행 칸 수가 부족합니다: {matches[0]!r}"
    return _BACKTICKED.findall(cells[1])


def test_issue_form_applies_exactly_one_classification_label() -> None:
    """Form이 label 하나만 부여함을 고정한다(#507).

    Form과 API 발행 경로가 같은 Auto Research 분류 좌표를 유지해야 한다.
    """
    assert _form_labels() == ["auto-experiment"]

@_requires_active_promotion_workflow
def test_promotion_workflow_guard_requires_the_form_label() -> None:
    """승격 워크플로의 이슈 가드가 같은 label을 요구함을 고정한다.

    여기가 두 번째 게이트다. Form만 바꾸고 이 가드를 두면 발행된 `[AR]` 이슈는
    옛 label을 갖지 않으므로 승격 단계에서 항상 throw한다.
    """
    (trigger_label,) = _form_labels()
    workflow_text = PROMOTION_WORKFLOW.read_text(encoding="utf-8")

    assert f"label.name === '{trigger_label}'" in workflow_text
    assert f"issue must have the {trigger_label} label" in workflow_text


def test_documents_list_the_same_labels_as_the_issue_form() -> None:
    """두 문서의 Issue Form 표가 Form의 `labels:`와 같은 집합을 실음을 고정한다."""
    form_labels = _form_labels()
    for path in DOCUMENTS_WITH_ISSUE_FORM_TABLE:
        assert _documented_labels(path) == form_labels, (
            f"{path.name}의 자동 label 표기가 Issue Form과 다릅니다"
        )


def test_service_publishes_issues_with_the_form_label() -> None:
    """발행 endpoint가 붙이는 label이 Form의 트리거 label과 같음을 고정한다.

    `service.TRIGGER_LABEL`은 이 문자열의 복제본이다 — 여기가 어긋나면 서버가 발행한
    이슈를 승격 workflow가 Auto Research 입력으로 인정하지 않는다.
    """
    from applications.experiment_platform.api.experiments.service import TRIGGER_LABEL

    (trigger_label,) = _form_labels()
    assert TRIGGER_LABEL == trigger_label


def test_documents_name_the_label_in_the_auto_research_section() -> None:
    """두 문서의 'Auto Research 분류 label' 절이 실제 label을 지목함을 고정한다.

    표만 고치고 본문 설명을 두면 사람이 읽는 정본과 워크플로가 어긋난다.
    """
    (trigger_label,) = _form_labels()
    for path in DOCUMENTS_WITH_ISSUE_FORM_TABLE:
        text = path.read_text(encoding="utf-8")
        head, separator, tail = text.partition("Auto Research 분류 label")
        assert separator, f"{path.name}에 'Auto Research 분류 label' 절이 없습니다"
        assert trigger_label in tail, (
            f"{path.name}의 'Auto Research 분류 label' 절이 {trigger_label}을 언급하지 않습니다"
        )
