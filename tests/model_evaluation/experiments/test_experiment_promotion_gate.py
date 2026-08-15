"""실험 metric 결과의 Draft PR 승격 게이트를 검증한다."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from autoresearch.model_evaluation.experiments.promotion_gate import _LABELS, evaluate, parse_criteria


PROJECT_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = PROJECT_ROOT / ".github/workflows/auto-research-promotion.yml"
DEV_PROMOTION_WORKFLOW = PROJECT_ROOT / ".github/workflows/auto-research-dev-promotion.yml"
ISSUE_FORM = PROJECT_ROOT / ".github/ISSUE_TEMPLATE/auto_research.yml"
RENDERED_FORM_FIXTURE = PROJECT_ROOT / "tests/fixtures/auto_research_issue_form_rendered.md"
_requires_active_promotion_workflow = pytest.mark.skipif(
    not WORKFLOW.is_file(),
    reason=(
        "조직 자동 실험 경로 부재로 비활성화된 "
        "auto-research-promotion.yml 계약 테스트"
    ),
)
_requires_active_dev_promotion_workflow = pytest.mark.skipif(
    not DEV_PROMOTION_WORKFLOW.is_file(),
    reason=(
        "조직 자동 실험 경로 부재로 비활성화된 "
        "auto-research-dev-promotion.yml 계약 테스트"
    ),
)


def _issue_body(**values: str) -> str:
    """Issue Form의 실제 label로 본문을 합성한다.

    기본값은 반드시 `.github/ISSUE_TEMPLATE/auto_research.yml`의 label과 일치해야 한다.
    합성 본문이 Form과 어긋나면 `parse_criteria`의 결함을 테스트가 덮어버린다(#495).
    """
    defaults = {
        "주 지표 이름": "val_roc_auc",
        "주 지표 방향": "higher_is_better",
        "최소 주 지표 개선폭": "0.002",
        "Guardrail 지표 이름": "없음",
        "Guardrail 지표 방향": "not_applicable",
        "최대 Guardrail 악화폭": "없음",
    }
    defaults.update(values)
    return "\n\n".join(f"### {key}\n\n{value}" for key, value in defaults.items())


def _issue_form_labels() -> set[str]:
    """Issue Form이 실제로 렌더하는 heading label 집합을 반환한다."""
    parsed = yaml.safe_load(ISSUE_FORM.read_text(encoding="utf-8"))
    return {
        item["attributes"]["label"]
        for item in parsed["body"]
        if item["type"] != "markdown"
    }


def test_parse_criteria_reads_body_rendered_from_actual_form() -> None:
    """정본 fixture를 그대로 파싱한다 — 합성 본문 헬퍼에 의존하지 않는다.

    이 테스트가 있었다면 #461 머지 시점에 즉시 실패했을 것이다. Issue Form과
    `_LABELS` 중 한쪽만 바뀌거나 한쪽만 머지되면 여기서 깨진다.
    """
    criteria = parse_criteria(RENDERED_FORM_FIXTURE.read_text(encoding="utf-8"))

    assert criteria.primary_name == "roc_auc"
    assert criteria.primary_direction == "higher_is_better"
    assert criteria.minimum_primary_delta == pytest.approx(0.002)
    assert criteria.guardrail_name is None
    assert criteria.guardrail_direction is None
    assert criteria.maximum_guardrail_regression is None


def test_promotion_gate_labels_exist_in_issue_form() -> None:
    """`_LABELS`의 모든 값이 Issue Form에 실재하는지 고정한다."""
    missing = sorted(set(_LABELS.values()) - _issue_form_labels())

    assert not missing, f"Issue Form에 없는 label: {missing}"


def test_issue_body_helper_uses_real_form_labels() -> None:
    """합성 본문 헬퍼가 Form에 없는 label을 쓰지 않도록 고정한다."""
    synthesized = {
        line.removeprefix("### ")
        for line in _issue_body().splitlines()
        if line.startswith("### ")
    }
    missing = sorted(synthesized - _issue_form_labels())

    assert not missing, f"헬퍼가 Form에 없는 label을 사용: {missing}"


def test_gate_passes_primary_metric_above_required_delta() -> None:
    criteria = parse_criteria(_issue_body())

    decision = evaluate(criteria, primary_candidate=0.781, primary_baseline=0.778)

    assert decision.passed is True
    assert decision.reason == "criteria_met"


def test_gate_rejects_guardrail_regression() -> None:
    criteria = parse_criteria(
        _issue_body(
            **{
                "Guardrail 지표 이름": "log_loss",
                "Guardrail 지표 방향": "lower_is_better",
                "최대 Guardrail 악화폭": "0.001",
            }
        )
    )

    decision = evaluate(
        criteria,
        primary_candidate=0.781,
        primary_baseline=0.778,
        guardrail_candidate=0.42,
        guardrail_baseline=0.41,
    )

    assert decision.passed is False
    assert decision.reason == "guardrail_regressed"


def test_gate_rejects_negative_primary_delta() -> None:
    try:
        parse_criteria(_issue_body(**{"최소 주 지표 개선폭": "-0.1"}))
    except ValueError as error:
        assert "minimum_primary_delta" in str(error)
    else:
        raise AssertionError("negative delta must be rejected")


def test_workflows_do_not_join_with_escaped_newline() -> None:
    """`].join('\\\\n')`는 리터럴 백슬래시+n을 본문에 남긴다(#495 버그 A).

    `github-script`의 `script: |`는 리터럴 블록 스칼라라 YAML이 이스케이프를 처리하지
    않는다. 따라서 JS가 받는 값이 `'\\\\n'`(백슬래시+n)이 되어 줄바꿈이 사라진다.
    """
    offenders = []
    for workflow in sorted(
        p
        for pattern in ("*.yml", "*.yaml")
        for p in (PROJECT_ROOT / ".github/workflows").glob(pattern)
    ):
        text = workflow.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if r".join('\\n')" in line or r'.join("\\n")' in line:
                offenders.append(f"{workflow.name}:{number}")

    assert not offenders, f"이스케이프된 개행으로 본문을 조립하는 위치: {offenders}"


def _experiment_id_patterns() -> dict[str, str]:
    """`experiment_id` 정규식이 정의된 모든 지점을 파일에서 추출한다."""
    sources = {
        "promotion_workflow": (
            WORKFLOW,
            r"/(\^\[a-z0-9\]\[[^/]*?\$)/\.test\(experimentId\)|"
            r"requireMatch\(experimentId, /(\^[^/]*?\$)/",
        ),
        "dev_promotion_workflow": (
            DEV_PROMOTION_WORKFLOW,
            r"/(\^\[a-z0-9\]\[[^/]*?\$)/\.test\(rawExperimentId\)|"
            r"requireMatch\(experimentId, /(\^[^/]*?\$)/",
        ),
        "tools_selector": (
            PROJECT_ROOT / "tools/auto_research_issue_branch.py",
            r"_EXPERIMENT_ID_PATTERN = re\.compile\(r\"(\^[^\"]+\$)\"\)",
        ),
        "paired_experiment": (
            PROJECT_ROOT / "autoresearch/model_evaluation/paired_experiment.py",
            r"_EXPERIMENT_ID_PATTERN = r\"(\^[^\"]+\$)\"",
        ),
        "experiment_context": (
            PROJECT_ROOT / "autoresearch/model_evaluation/experiments/context.py",
            r"_EXPERIMENT_ID = re\.compile\(r\"(\^[^\"]+\$)\"\)",
        ),
    }
    found: dict[str, str] = {}
    for name, (path, pattern) in sources.items():
        matches = [
            group
            for match in re.finditer(pattern, path.read_text(encoding="utf-8"))
            for group in match.groups()
            if group
        ]
        assert matches, f"{name}에서 experiment_id 정규식을 찾지 못했습니다"
        assert len(set(matches)) == 1, f"{name} 안에서 정규식이 갈라져 있습니다: {set(matches)}"
        found[name] = matches[0]
    return found


@_requires_active_promotion_workflow
@_requires_active_dev_promotion_workflow
def test_experiment_id_pattern_is_identical_everywhere() -> None:
    """정규식이 5개 정의 지점에서 동일해야 한다(#495 버그 B)."""
    patterns = _experiment_id_patterns()

    assert len(set(patterns.values())) == 1, f"정규식이 갈라져 있습니다: {patterns}"


@_requires_active_promotion_workflow
@_requires_active_dev_promotion_workflow
@pytest.mark.parametrize(
    "experiment_id",
    [
        "paired-offline-comparison-2026-08",  # 33자 — 좁은 쪽이 거부
        "feature_dropout",  # 밑줄
        "exp.v2",  # 점
        "ar:449",  # 콜론 — git ref 이름 불허 문자
    ],
)
def test_divergent_experiment_ids_are_judged_identically(experiment_id: str) -> None:
    """한쪽만 통과하던 값들이 모든 지점에서 같은 판정을 받아야 한다."""
    verdicts = {
        name: bool(re.fullmatch(pattern.strip("^$"), experiment_id))
        for name, pattern in _experiment_id_patterns().items()
    }

    assert len(set(verdicts.values())) == 1, f"{experiment_id} 판정 불일치: {verdicts}"


@_requires_active_promotion_workflow
def test_promotion_workflow_uses_dispatch_gate_and_draft_pr() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "repository_dispatch:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "issues: write" in workflow
    assert "pull-requests: write" in workflow
    assert "registry_uri" in workflow
    assert "run_id" in workflow
    assert "Validate experiment lineage" in workflow
    # 사유 코드는 `${kind}:${error.message}`로 조립된다(#495 D-3). kind 기본값이
    # lineage_invalid이며, 나머지 두 갈래는 전용 테스트가 고정한다.
    assert "error.kind || 'lineage_invalid'" in workflow
    assert "steps.lineage.outputs.valid != 'true'" in workflow
    assert "refs/heads/promote/" in workflow
    assert "draft: true" in workflow
    assert "compareCommits" in workflow
    assert "Comment failed or rejected experiment result on source issue" in workflow
    assert "github.rest.issues.createComment" in workflow
    assert "github.paginate(github.rest.issues.listComments" in workflow
    assert "existingRef.object.sha !== candidateSha" in workflow
    assert "github.rest.git.updateRef" not in workflow


# gate가 실패·취소로 끝났을 때도 실행되는 step만 순서 계약의 대상이다.
# Draft PR 생성 step은 gate 통과가 전제이므로 `steps.gate.outputs.reason` 단독으로 충분하다.
_GATE_FAILURE_AWARE_STEPS = (
    "Comment failed or rejected experiment result on source issue",
    "Write result summary",
)


def _gate_reason_expressions() -> dict[str, str]:
    """gate 실패 시에도 도는 step의 `GATE_REASON` 표현식을 YAML에서 파싱한다."""
    parsed = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = parsed["jobs"]["create-promotion-pr"]["steps"]
    found = {
        step["name"]: step["env"]["GATE_REASON"]
        for step in steps
        if step.get("name") in _GATE_FAILURE_AWARE_STEPS
        and isinstance(step.get("env"), dict)
        and "GATE_REASON" in step["env"]
    }
    missing = sorted(set(_GATE_FAILURE_AWARE_STEPS) - set(found))
    assert not missing, f"GATE_REASON을 쓰지 않는 step: {missing}"
    return found


@_requires_active_promotion_workflow
def test_gate_reason_prefers_gate_failure_over_lineage_success() -> None:
    """gate 실패 갈래가 lineage 사유보다 **먼저** 평가되어야 한다(#495).

    gate step은 `steps.lineage.outputs.valid == 'true'`일 때만 실행되므로, 그 경로에서
    `steps.lineage.outputs.reason`은 항상 `lineage_valid`(truthy)다. 순서가 뒤집히면
    gate가 예외로 죽어도 사유가 `lineage_valid`로 덮여 관측 경로가 다시 막힌다.

    문자열 존재만 보는 검사로는 이 순서 결함이 드러나지 않으므로, `||` 피연산자의
    **상대 순서**를 직접 고정한다.
    """
    for step_name, expression in _gate_reason_expressions().items():
        gate_failure = expression.find("gate_step_failed")
        lineage_reason = expression.find("steps.lineage.outputs.reason")

        assert gate_failure != -1, f"{step_name}: gate 실패 갈래가 없습니다"
        assert lineage_reason != -1, f"{step_name}: lineage 사유 갈래가 없습니다"
        assert gate_failure < lineage_reason, (
            f"{step_name}: gate 실패 갈래가 lineage 사유보다 뒤에 있어 "
            f"절대 선택되지 않습니다 — {expression}"
        )


@_requires_active_promotion_workflow
def test_gate_reason_covers_cancelled_outcome() -> None:
    """gate가 cancelled로 끝나도 사유가 남아야 한다(#495)."""
    for step_name, expression in _gate_reason_expressions().items():
        assert "steps.gate.outcome == 'cancelled'" in expression, step_name


@_requires_active_promotion_workflow
def test_promotion_workflow_records_result_when_gate_step_fails() -> None:
    """gate step이 실패·취소로 끝나도 소스 이슈에 코멘트가 남아야 한다(#495 C·D-1)."""
    parsed = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = parsed["jobs"]["create-promotion-pr"]["steps"]
    condition = next(
        step["if"]
        for step in steps
        if step.get("name", "").startswith("Comment failed or rejected")
    )

    assert "steps.gate.outcome == 'failure'" in condition
    assert "steps.gate.outcome == 'cancelled'" in condition


@_requires_active_promotion_workflow
def test_promotion_workflow_validates_metric_inputs_before_gate() -> None:
    """지표를 lineage에서 검증해 gate의 float() 폭발을 막는다(#495 D-1)."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "isFiniteDecimal" in workflow
    assert "must be a finite decimal" in workflow


@_requires_active_promotion_workflow
@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("0.7812", True),
        ("-0.0004", True),
        ("0", True),
        ("1e-05", True),  # producer가 작은 delta를 JSON 숫자로 실으면 이렇게 직렬화된다
        ("2E+3", True),
        ("0.7120000000000001", True),
        ("", False),
        ("abc", False),
        ("NaN", False),
        ("Infinity", False),
        ("01.5", False),  # 선행 0
        ("1.", False),
    ],
)
def test_metric_input_pattern_matches_previous_float_behaviour(value: str, accepted: bool) -> None:
    """지표 검증이 이전 `float()`이 받던 범위를 좁히지 않아야 한다(#495 D-1).

    좁히면 외부 producer(#492)가 보내던 정상 payload가 `input_invalid`로 거부된다.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"return /\^(.+?)\$/\.test\(value\)", workflow)
    assert match, "isFiniteDecimal 정규식을 찾지 못했습니다"

    matched = re.fullmatch(match.group(1), value) is not None
    is_finite = False
    if matched:
        try:
            is_finite = float(value) == float(value) and abs(float(value)) != float("inf")
        except ValueError:
            is_finite = False

    assert (matched and is_finite) is accepted, f"{value!r} 판정이 기대와 다릅니다"


@_requires_active_promotion_workflow
def test_promotion_workflow_separates_rejection_kinds() -> None:
    """입력 거부·계보 불일치·비교 기각을 다른 사유 코드로 구분한다(#495 D-3)."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "input_invalid" in workflow
    assert "comparison_rejected" in workflow
    assert "lineage_invalid" in workflow
    assert "RejectionError" in workflow


@_requires_active_promotion_workflow
def test_promotion_workflow_failure_comment_has_fallbacks() -> None:
    """실패 코멘트의 모든 항목이 빈 backtick으로 렌더되지 않는다(#495 D-2)."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "PRIMARY_CANDIDATE || '미제공'" in workflow
    assert "PRIMARY_BASELINE || '미제공'" in workflow


def _workflow_accepts_registry_uri(
    registry_uri: str,
    *,
    issue_number: int = 449,
    experiment_id: str = "primary",
    candidate_sha: str = "b" * 40,
) -> bool:
    """워크플로 소스에서 뽑은 suffix 템플릿으로 게이트 판정을 재현한다.

    JS를 실행하지는 못하므로, 워크플로가 실제로 쓰는 두 템플릿 문자열을 파일에서
    읽어 같은 규칙(gs:// anchoring + suffix 두 개 중 하나)을 파이썬으로 적용한다.
    템플릿이 바뀌면 이 재현도 함께 바뀌므로, 경로 규칙 변경이 테스트에 드러난다.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    suffixes = re.findall(
        r"`(/experiments/\$\{issueNumber\}/\$\{experimentId\}[^`]*registry\.db)`",
        workflow,
    )
    assert suffixes, "워크플로에서 registry suffix 템플릿을 찾지 못했습니다"
    resolved = [
        suffix.replace("${issueNumber}", str(issue_number))
        .replace("${experimentId}", experiment_id)
        .replace("${candidateSha}", candidate_sha)
        for suffix in suffixes
    ]
    if not registry_uri.startswith("gs://"):
        return False
    return any(registry_uri.endswith(suffix) for suffix in resolved)


@_requires_active_promotion_workflow
@pytest.mark.parametrize(
    ("registry_uri", "accepted"),
    [
        # 조건 격리 candidate 경로(#454)
        ("gs://registry/experiments/449/primary/candidate/" + "b" * 40 + "/registry.db", True),
        # 조건 구간이 없는 기존 경로(#450/#461)
        ("gs://registry/experiments/449/primary/" + "b" * 40 + "/registry.db", True),
        # baseline 조건 산출물은 승격 입력이 아니다
        ("gs://registry/experiments/449/primary/baseline/" + "b" * 40 + "/registry.db", False),
        # 다른 이슈·실험 좌표
        ("gs://registry/experiments/999/primary/candidate/" + "b" * 40 + "/registry.db", False),
        ("gs://registry/experiments/449/other/candidate/" + "b" * 40 + "/registry.db", False),
        # 다른 SHA
        ("gs://registry/experiments/449/primary/candidate/" + "c" * 40 + "/registry.db", False),
        # 스킴 위조
        ("https://evil.example/experiments/449/primary/candidate/" + "b" * 40 + "/registry.db", False),
    ],
)
def test_promotion_gate_registry_rule_accepts_only_candidate_coordinates(
    registry_uri: str, accepted: bool
) -> None:
    assert _workflow_accepts_registry_uri(registry_uri) is accepted


@_requires_active_promotion_workflow
def test_promotion_workflow_keeps_registry_rule_structure() -> None:
    """suffix 두 개를 OR로 받아들이는 구조 자체를 고정한다.

    조건을 `&&`에서 `||`로 잘못 바꾸면 두 경로 모두 거부되거나 모두 통과한다.
    문자열 존재만 보는 검사로는 그 변경이 드러나지 않는다.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert (
        "!registryUri.endsWith(isolatedRegistrySuffix) &&\n"
        "                !registryUri.endsWith(legacyRegistrySuffix)" in workflow
    )
    assert "if (!registryUri.startsWith('gs://'))" in workflow


@_requires_active_promotion_workflow
def test_promotion_workflow_rejects_non_passed_paired_outcome() -> None:
    """paired 비교가 실패·기각인데 승격 PR이 만들어지지 않도록 고정한다."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "outcome: {required: false, type: string}" in workflow
    assert "OUTCOME: ${{ inputs.outcome || github.event.client_payload.outcome }}" in workflow
    assert "if (outcome && outcome !== 'comparison_passed')" in workflow


# ---------------------------------------------------------------------------
# 하드 리밋 승격 조건 (#472 Task 1, spec §3.1·§4.1~§4.3)
#
# "성능과 무관하게 일정 기간이 지나면 교체한다"를 게이트에 배선한다. 게이트는
# `degradation_eval`을 import하지 않고 **원시값(일수)만** 받는다(spec §2) —
# 그 모듈이 lightgbm을 끌고 오고, `autoresearch/`는 `src/`를 import하지 않는다.
# ---------------------------------------------------------------------------


def test_existing_call_without_hard_limit_args_still_works() -> None:
    """하위호환 가드 — 현재 workflow(182-193행)는 이 인자들을 넘기지 않는다.

    기본값이 없으면 배선 전에 기존 승격 경로가 즉시 깨진다.
    """
    criteria = parse_criteria(_issue_body())

    decision = evaluate(criteria, primary_candidate=0.781, primary_baseline=0.778)

    assert decision.passed is True
    assert decision.reason == "criteria_met"


def test_metric_pass_reports_criteria_met_even_when_limit_reached() -> None:
    """지표로 통과했으면 기한 도달 여부와 무관하게 `criteria_met`이다(spec §4.1).

    이걸 뒤집어 `hard_retrain_limit_reached`로 기록하면, 나중에 승격 이력을 읽는
    사람이 "이 모델은 기한 때문에 올라갔다"로 읽어 **모델 품질을 과소평가**한다.
    """
    criteria = parse_criteria(_issue_body())

    decision = evaluate(
        criteria,
        primary_candidate=0.781,
        primary_baseline=0.778,
        hard_retrain_limit_days=5,
        days_since_last_promotion=9,
    )

    assert decision.passed is True
    assert decision.reason == "criteria_met"


def test_metric_below_delta_passes_when_hard_limit_reached() -> None:
    """#472의 핵심 — 지표는 미달인데 기한이 지나면 승격 후보가 된다."""
    criteria = parse_criteria(_issue_body())

    decision = evaluate(
        criteria,
        primary_candidate=0.778,
        primary_baseline=0.778,
        hard_retrain_limit_days=5,
        days_since_last_promotion=9,
    )

    assert decision.passed is True
    assert decision.reason == "hard_retrain_limit_reached"


def test_metric_below_delta_still_fails_when_limit_not_reached() -> None:
    criteria = parse_criteria(_issue_body())

    decision = evaluate(
        criteria,
        primary_candidate=0.778,
        primary_baseline=0.778,
        hard_retrain_limit_days=5,
        days_since_last_promotion=4,
    )

    assert decision.passed is False
    assert decision.reason == "primary_metric_below_delta"


def test_elapsed_equal_to_limit_counts_as_reached() -> None:
    """경계는 `>=`다 — "N일이 지나면"의 N일째가 도달이다."""
    criteria = parse_criteria(_issue_body())

    decision = evaluate(
        criteria,
        primary_candidate=0.778,
        primary_baseline=0.778,
        hard_retrain_limit_days=5,
        days_since_last_promotion=5,
    )

    assert decision.passed is True
    assert decision.reason == "hard_retrain_limit_reached"


def test_zero_limit_is_always_reached() -> None:
    """`limit_days=0`은 "이미 재학습 시점을 지났다"는 뜻이다.

    `#485` spec §4.2의 표에 없던 조합이다 — `safety_margin_days`가
    `degradation_point.elapsed_days`와 같으면 뺄셈이 음수가 아니라 정확히 0이라
    clamp 분기를 타지 않아 `reason=None`으로 나온다(spec §7). 게이트는 숫자만
    받으므로 두 경로의 판정이 같아야 한다.
    """
    criteria = parse_criteria(_issue_body())

    decision = evaluate(
        criteria,
        primary_candidate=0.778,
        primary_baseline=0.778,
        hard_retrain_limit_days=0,
        days_since_last_promotion=0,
    )

    assert decision.passed is True
    assert decision.reason == "hard_retrain_limit_reached"


@pytest.mark.parametrize(
    ("limit_days", "elapsed"),
    [(None, 9), (5, None), (None, None)],
)
def test_missing_hard_limit_inputs_are_not_treated_as_reached(limit_days, elapsed) -> None:
    """관측되지 않은 것을 "기한이 지났다"로도 "안 지났다"로도 바꾸지 않는다.

    `#485` spec §4.1과 같은 결. 특히 `hard_retrain_limit_days=None`은 hold가 걸려
    호출부가 값을 넘기지 않은 경우(spec §3.2)이므로, 그것으로 승격을 **늘리면**
    근거 없는 곡선이 승격을 만들어낸다.
    """
    criteria = parse_criteria(_issue_body())

    decision = evaluate(
        criteria,
        primary_candidate=0.778,
        primary_baseline=0.778,
        hard_retrain_limit_days=limit_days,
        days_since_last_promotion=elapsed,
    )

    assert decision.passed is False
    assert decision.reason == "primary_metric_below_delta"


# ---------------------------------------------------------------------------
# guardrail은 하드 리밋으로 우회되지 않는다 (#472 Task 2, spec §4.4)
#
# 하드 리밋의 취지는 "성능이 정체돼도 교체한다"이지 "망가진 모델도 올린다"가 아니다.
# guardrail은 안 망가졌다는 최소 보증이므로 그것까지 우회하면 게이트가 무력해진다.
# ---------------------------------------------------------------------------


def _guardrail_body() -> str:
    return _issue_body(
        **{
            "Guardrail 지표 이름": "log_loss",
            "Guardrail 지표 방향": "lower_is_better",
            "최대 Guardrail 악화폭": "0.001",
        }
    )


def test_hard_limit_does_not_bypass_guardrail_regression() -> None:
    criteria = parse_criteria(_guardrail_body())

    decision = evaluate(
        criteria,
        primary_candidate=0.778,
        primary_baseline=0.778,
        guardrail_candidate=0.310,  # baseline 대비 0.01 악화 — 허용치 0.001 초과
        guardrail_baseline=0.300,
        hard_retrain_limit_days=5,
        days_since_last_promotion=9,
    )

    assert decision.passed is False
    assert decision.reason == "guardrail_regressed"


def test_hard_limit_does_not_bypass_missing_guardrail_values() -> None:
    criteria = parse_criteria(_guardrail_body())

    decision = evaluate(
        criteria,
        primary_candidate=0.778,
        primary_baseline=0.778,
        hard_retrain_limit_days=5,
        days_since_last_promotion=9,
    )

    assert decision.passed is False
    assert decision.reason == "guardrail_metric_missing"


def test_hard_limit_passes_when_guardrail_is_within_budget() -> None:
    criteria = parse_criteria(_guardrail_body())

    decision = evaluate(
        criteria,
        primary_candidate=0.778,
        primary_baseline=0.778,
        guardrail_candidate=0.3005,  # 0.0005 악화 — 허용치 0.001 이내
        guardrail_baseline=0.300,
        hard_retrain_limit_days=5,
        days_since_last_promotion=9,
    )

    assert decision.passed is True
    assert decision.reason == "hard_retrain_limit_reached"


def test_metric_failure_without_hard_limit_keeps_existing_reason() -> None:
    """기존 동작 보존 — 하드 리밋이 성립하지 않으면 guardrail을 보지 않는다.

    현재 구현은 지표 미달이면 즉시 `primary_metric_below_delta`로 끝낸다. 하드 리밋
    경로를 더하면서 이 단축을 깨면, guardrail 값이 없는 기존 실행의 사유가
    `guardrail_metric_missing`으로 바뀌어 승격 이력의 의미가 달라진다.
    """
    criteria = parse_criteria(_guardrail_body())

    decision = evaluate(
        criteria,
        primary_candidate=0.778,
        primary_baseline=0.778,
    )

    assert decision.passed is False
    assert decision.reason == "primary_metric_below_delta"


# ---------------------------------------------------------------------------
# 게이트 정책 버전 (#472 Task 3, spec §5)
#
# 하드 리밋 값은 열화 재측정으로 바뀔 수 있다 — 같은 코드가 다른 날 다른 판정을 낼 수
# 있으므로, **어떤 정책으로 판정했는지**가 결과에 남아야 승격 이력을 해석할 수 있다.
# ---------------------------------------------------------------------------


def test_every_decision_carries_gate_policy_version() -> None:
    """통과·거부 모든 경로가 정책 버전을 싣는다 — 한 경로만 빠져도 이력이 끊긴다."""
    plain = parse_criteria(_issue_body())
    guarded = parse_criteria(_guardrail_body())

    decisions = [
        # 지표 통과
        evaluate(plain, primary_candidate=0.781, primary_baseline=0.778),
        # 지표 미달 + 기한 미도달
        evaluate(plain, primary_candidate=0.778, primary_baseline=0.778),
        # 지표 미달 + 기한 도달
        evaluate(
            plain,
            primary_candidate=0.778,
            primary_baseline=0.778,
            hard_retrain_limit_days=5,
            days_since_last_promotion=9,
        ),
        # guardrail 값 누락
        evaluate(
            guarded,
            primary_candidate=0.781,
            primary_baseline=0.778,
        ),
        # guardrail 악화
        evaluate(
            guarded,
            primary_candidate=0.781,
            primary_baseline=0.778,
            guardrail_candidate=0.310,
            guardrail_baseline=0.300,
        ),
    ]

    assert {decision.policy_version for decision in decisions} == {"gate-policy-v1"}


def test_gate_policy_version_is_distinct_from_promotion_policy_version() -> None:
    """`promotion-policy-v1`(통계 판정 정책)과 **다른 축**이다(spec §5).

    이름이 비슷해 같은 것으로 오독되면, 한쪽을 올리면서 다른 쪽도 올려야 한다고
    착각하게 된다.
    """
    from autoresearch.model_evaluation.promotion_evidence import PROMOTION_POLICY_VERSION

    decision = evaluate(
        parse_criteria(_issue_body()), primary_candidate=0.781, primary_baseline=0.778
    )

    assert decision.policy_version != PROMOTION_POLICY_VERSION


# ---------------------------------------------------------------------------
# workflow 배선 (#472 Task 4, spec §8.2)
#
# 게이트는 순수 함수라 값 조달은 전부 호출부 책임이다. 이 workflow는 MLflow에
# 접근하지 않고 모든 값을 입력으로 받으므로, 경과일도 입력이고 리밋만 정책 상수다.
# ---------------------------------------------------------------------------


def _promotion_workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@_requires_active_promotion_workflow
def test_workflow_accepts_days_since_last_promotion_as_optional_input() -> None:
    inputs = _promotion_workflow()[True]["workflow_dispatch"]["inputs"]

    assert "days_since_last_promotion" in inputs
    assert inputs["days_since_last_promotion"]["required"] is False


@_requires_active_promotion_workflow
def test_workflow_reads_elapsed_days_from_dispatch_payload_too() -> None:
    """`repository_dispatch`로 오는 producer도 같은 값을 실을 수 있어야 한다."""
    env = _promotion_workflow()["jobs"]["create-promotion-pr"]["env"]

    assert "client_payload.days_since_last_promotion" in env["DAYS_SINCE_LAST_PROMOTION"]


@_requires_active_promotion_workflow
def test_hard_retrain_limit_days_defaults_to_unset() -> None:
    """값이 확정되기 전에는 비워 둔다(spec §8.3).

    `#485` 실측은 단일 origin 관측 하나뿐이고 `safety_margin_days`가 미확정이라
    리밋도 잠정이다. 숫자를 박으면 **근거 없는 상수가 승격을 만들어낸다** — 비어
    있으면 게이트가 하드 리밋 조건을 평가하지 않아 기존 동작과 동일하다.
    """
    env = _promotion_workflow()["jobs"]["create-promotion-pr"]["env"]

    assert env["HARD_RETRAIN_LIMIT_DAYS"] == ""


@_requires_active_promotion_workflow
def test_gate_step_passes_hard_limit_arguments() -> None:
    steps = _promotion_workflow()["jobs"]["create-promotion-pr"]["steps"]
    gate = next(step for step in steps if step.get("id") == "gate")

    assert "hard_retrain_limit_days=" in gate["run"]
    assert "days_since_last_promotion=" in gate["run"]
    # 빈 값이면 None을 넘겨야 한다 — 관측되지 않은 것을 값으로 바꾸지 않는다.
    assert "else None" in gate["run"]


@_requires_active_promotion_workflow
def test_workflow_validates_elapsed_days_as_non_negative_integer() -> None:
    """gate step이 `int()`로 파싱하므로 형식 검증이 그 앞에 있어야 한다.

    없으면 잘못된 입력에서 gate step이 죽고 사유가 이슈에 남지 않는다(#495 D-1과
    같은 이유).
    """
    steps = _promotion_workflow()["jobs"]["create-promotion-pr"]["steps"]
    lineage = next(step for step in steps if step.get("id") == "lineage")
    script = lineage["with"]["script"]

    assert "days_since_last_promotion must be a non-negative integer" in script
    assert "input_invalid" in script


# ---------------------------------------------------------------------------
# Draft PR 본문에 승격 사유 표시 (#472 Task 5, spec §4.3·§6.1)
#
# 하드 리밋으로 올라온 후보는 **지표 기준을 통과하지 못했다.** 제목·본문이 그 사실을
# 드러내지 않으면 리뷰어가 "metric 통과 후보"를 믿고 지표가 개선된 줄로 읽는다.
# ---------------------------------------------------------------------------


def _draft_pr_script() -> str:
    steps = _promotion_workflow()["jobs"]["create-promotion-pr"]["steps"]
    step = next(
        s for s in steps if "Create immutable promotion branch" in (s.get("name") or "")
    )
    return step["with"]["script"]


@_requires_active_promotion_workflow
def test_draft_pr_title_distinguishes_hard_limit_promotion() -> None:
    script = _draft_pr_script()

    assert "하드 리밋 강제 교체 후보" in script
    # 기존 제목도 남아 있어야 한다 — 지표 통과 경로는 그대로다.
    assert "metric 통과 후보" in script


@_requires_active_promotion_workflow
def test_draft_pr_body_warns_that_metric_did_not_pass() -> None:
    script = _draft_pr_script()

    assert "지표 기준을 통과하지 못했습니다" in script


@_requires_active_promotion_workflow
def test_draft_pr_body_records_elapsed_days_is_an_approximation() -> None:
    """spec §6.1 — 근사는 값을 구하는 쪽의 성질이라 여기에 남긴다.

    `policy_version`에 넣지 않는 이유는 게이트가 값의 출처를 모르기 때문이다.
    """
    script = _draft_pr_script()

    assert "creation_timestamp" in script
    assert "근사치" in script


@_requires_active_promotion_workflow
def test_draft_pr_branches_on_gate_reason_not_on_passed_flag() -> None:
    """`passed`는 두 승격 경로에서 모두 true다 — 사유로 갈라야 구분된다."""
    script = _draft_pr_script()

    assert "GATE_REASON === 'hard_retrain_limit_reached'" in script


@_requires_active_promotion_workflow
def test_draft_pr_does_not_claim_metric_gate_pass_for_hard_limit() -> None:
    """"통과한 dev 후보 SHA"는 하드 리밋 경로에서 사실이 아니다."""
    script = _draft_pr_script()

    assert "강제 교체 대상으로 판정한 dev 후보 SHA" in script


# ---------------------------------------------------------------------------
# 주 지표 하한이 없다 — 현재 동작을 고정한다 (PR #540 리뷰 1, spec §4.4)
#
# 기존 하드 리밋 테스트는 전부 `primary_candidate == primary_baseline`(delta=0)이라
# **악화(delta < 0)가 어느 쪽으로 판정되는지 고정하는 테스트가 없었다.**
# 아래 두 건은 "지금 이렇게 동작한다"를 못박는 것이지 "이게 옳다"는 주장이 아니다 —
# 하한을 넣기로 결정하면 이 테스트가 빨간불이 되어 의도적 변경임을 드러낸다.
# ---------------------------------------------------------------------------


def test_severe_primary_regression_still_passes_on_hard_limit_today() -> None:
    """guardrail이 예산 안이면 주 지표가 대폭 악화돼도 통과한다(현재 동작)."""
    criteria = parse_criteria(_guardrail_body())

    decision = evaluate(
        criteria,
        primary_candidate=0.400,  # baseline 0.778 대비 -0.378
        primary_baseline=0.778,
        guardrail_candidate=0.3005,
        guardrail_baseline=0.300,
        hard_retrain_limit_days=5,
        days_since_last_promotion=9,
    )

    assert decision.passed is True
    assert decision.reason == "hard_retrain_limit_reached"


def test_no_guardrail_declared_means_no_floor_at_all_today() -> None:
    """guardrail을 `없음`으로 선언한 가설에는 **어떤 하한도 없다**(현재 동작).

    `_guardrail_failure`가 `criteria.guardrail_name is None`에서 즉시 `None`을
    돌려주므로, 하드 리밋이 켜지면 주 지표가 얼마나 나빠졌든 통과한다. spec §4.4가
    "정책을 켜기 전에 반드시 해결"로 지정한 조합이다.
    """
    criteria = parse_criteria(_issue_body())  # guardrail 없음

    decision = evaluate(
        criteria,
        primary_candidate=0.100,
        primary_baseline=0.778,
        hard_retrain_limit_days=5,
        days_since_last_promotion=9,
    )

    assert decision.passed is True
    assert decision.reason == "hard_retrain_limit_reached"


# ---------------------------------------------------------------------------
# 리뷰 2·3 — policy_version 방출, 정책 상수 검증
# ---------------------------------------------------------------------------


@_requires_active_promotion_workflow
def test_gate_step_emits_policy_version_to_github_output() -> None:
    """`policy_version`은 **결과에 남아야** 존재 이유가 성립한다(spec §5).

    GateDecision 안에만 있고 밖으로 안 나가면, 정책이 v2로 올라간 뒤 이미 머지된
    승격 PR을 보고 "이건 v1인가 v2인가"를 되짚을 방법이 없다.
    """
    steps = _promotion_workflow()["jobs"]["create-promotion-pr"]["steps"]
    gate = next(step for step in steps if step.get("id") == "gate")

    assert "policy_version={decision.policy_version}" in gate["run"]


@_requires_active_promotion_workflow
def test_draft_pr_body_records_gate_policy_version() -> None:
    script = _draft_pr_script()

    assert "GATE_POLICY_VERSION" in script


@_requires_active_promotion_workflow
def test_gate_step_validates_the_policy_constant_itself() -> None:
    """정책 상수는 lineage step의 입력 검증을 거치지 않는다 — 여기서 막아야 한다.

    `"30일"` 같은 오타 하나로 gate step이 죽으면 **모든 실험**이 `gate_step_failed`로
    떨어진다. 정책을 켜는 날 처음 겪는 실패라 원인 추적도 어렵다.
    """
    steps = _promotion_workflow()["jobs"]["create-promotion-pr"]["steps"]
    gate = next(step for step in steps if step.get("id") == "gate")

    assert "_optional_days" in gate["run"]
    assert "must be a non-negative integer or empty" in gate["run"]
    # 두 값 모두 같은 검증을 거친다.
    assert '_optional_days("HARD_RETRAIN_LIMIT_DAYS")' in gate["run"]
    assert '_optional_days("DAYS_SINCE_LAST_PROMOTION")' in gate["run"]
