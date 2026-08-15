from __future__ import annotations

import os
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORM_PATH = PROJECT_ROOT / ".github/ISSUE_TEMPLATE/auto_research.yml"
PROMOTION_WORKFLOW = PROJECT_ROOT / ".github/workflows/auto-research-dev-promotion.yml"
RENDERED_FORM_FIXTURE = PROJECT_ROOT / "tests/fixtures/auto_research_issue_form_rendered.md"
_requires_active_dev_promotion_workflow = pytest.mark.skipif(
    not PROMOTION_WORKFLOW.is_file(),
    reason="조직 자동 실험 경로 부재로 비활성화된 auto-research-dev-promotion.yml 계약 테스트",
)
sys.path.insert(0, str(PROJECT_ROOT))

from tools import auto_research_issue_branch as issue_branch  # noqa: E402
from tools.auto_research_issue_branch import (  # noqa: E402
    MAX_COMPLETION_CANDIDATES,
    MAX_DECIMAL_DIGITS,
    MAX_DECIMAL_EXPONENT,
    MAX_DECIMAL_TEXT_LENGTH,
    MAX_RESULT_REFERENCE_LENGTH,
    IssueInput,
    branch_name_for,
    is_descendant,
    parse_issue_input,
    select_best_candidate,
)


def structured_body(
    *,
    primary_metric_name: str = "roc_auc",
    primary_metric_direction: str = "higher_is_better",
    minimum_primary_delta: str = "0.002",
    guardrail_metric_name: str = "없음",
    guardrail_metric_direction: str = "not_applicable",
    maximum_guardrail_regression: str = "없음",
    dataset_snapshot: str = "bq://autoresearch/train@2026-07-31",
    random_seeds: str = "42, 43, 44",
    split_seed: str = "20260731",
    test_size: str = "0.2",
    validation_size: str = "0.2",
    training_config_ref: str = "configs/train/lgbm-v1.yaml@abc1234",
    allowed_scope: str = "- [ ] prod 모델 계약(`autoresearch/feature_engineering/model_contract.py`) 수정을 허용한다",
) -> str:
    """GitHub가 구조화 Form 응답에서 생성하는 실제 heading 본문을 만듭니다."""
    return f"""### 연구 가설
비율 피처가 ROC-AUC를 높인다.

### 변경할 피처 · 모델
- 추가 피처: views_per_day = views / (days + 1)

### 주 지표 이름
{primary_metric_name}

### 주 지표 방향
{primary_metric_direction}

### 최소 주 지표 개선폭
{minimum_primary_delta}

### Guardrail 지표 이름
{guardrail_metric_name}

### Guardrail 지표 방향
{guardrail_metric_direction}

### 최대 Guardrail 악화폭
{maximum_guardrail_regression}

### 보조 관측 지표
pr_auc

### 비교 대상
동일 조건 baseline 재학습 (권장)

### 데이터셋 스냅샷
{dataset_snapshot}

### 랜덤 시드 목록
{random_seeds}

### Split 시드
{split_seed}

### Test 비율
{test_size}

### Validation 비율
{validation_size}

### 학습 설정 참조
{training_config_ref}

### 대상 데이터 · 기간
- 데이터셋 / 경로: data/train.csv
- 기간 (KST YYYY-MM-DD ~ YYYY-MM-DD): 2026-07-01 ~ 2026-07-31

### 스냅샷 재사용
허용 (진행하되 실제로 쓴 데이터를 결과에 명시)

### 허용 범위
{allowed_scope}

### 결과 (에이전트가 채웁니다)
- 판정 (지지/기각):
"""


def load_form() -> dict[str, object]:
    """Issue Form을 실제 YAML parser로 읽습니다."""
    parsed = yaml.safe_load(FORM_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def form_fields() -> dict[str, dict[str, object]]:
    """Markdown을 제외한 Form field를 ID로 반환합니다."""
    fields: dict[str, dict[str, object]] = {}
    for item in load_form()["body"]:
        if item["type"] == "markdown":
            continue
        fields[item["id"]] = item
    return fields


def workflow_trigger(workflow: dict[object, object]) -> dict[str, object]:
    """YAML 1.1 parser의 ``on`` boolean 호환성을 포함해 trigger를 반환합니다."""
    trigger = workflow.get("on", workflow.get(True))
    assert isinstance(trigger, dict)
    return trigger


def test_form_uses_machine_readable_metric_and_reproducibility_fields() -> None:
    fields = form_fields()
    structured_ids = {
        "primary_metric_name",
        "primary_metric_direction",
        "minimum_primary_delta",
        "guardrail_metric_name",
        "guardrail_metric_direction",
        "maximum_guardrail_regression",
        "dataset_snapshot",
        "random_seeds",
        "split_seed",
        "test_size",
        "validation_size",
        "training_config_ref",
    }

    assert fields.keys() >= structured_ids
    assert all(fields[field_id]["validations"]["required"] for field_id in structured_ids)
    assert "success_criteria" not in fields
    assert "reproducibility" not in fields
    assert fields.keys() >= {
        "hypothesis",
        "change",
        "secondary_metrics",
        "comparison",
        "dataset",
        "snapshot_reuse",
        "allowed_scope",
        "result",
    }
    assert fields["primary_metric_direction"]["attributes"]["options"] == [
        "higher_is_better",
        "lower_is_better",
    ]
    assert fields["guardrail_metric_direction"]["attributes"]["options"] == [
        "not_applicable",
        "higher_is_better",
        "lower_is_better",
    ]
    assert fields["guardrail_metric_name"]["attributes"]["value"] == "없음"
    assert fields["maximum_guardrail_regression"]["attributes"]["value"] == "없음"


def test_parse_issue_input_reads_body_rendered_from_actual_form() -> None:
    issue_input = parse_issue_input(
        449,
        "[AR] CTR ratio",
        RENDERED_FORM_FIXTURE.read_text(encoding="utf-8"),
    )

    assert issue_input.issue_branch == "exp/449"
    assert issue_input.primary_metric_name == "roc_auc"
    assert issue_input.primary_metric_direction == "higher_is_better"
    assert issue_input.minimum_primary_delta == Decimal("0.002")
    assert issue_input.guardrail_metric_name is None
    assert issue_input.guardrail_metric_direction == "not_applicable"
    assert issue_input.maximum_guardrail_regression is None
    assert issue_input.dataset_snapshot == "bq://autoresearch/train@2026-07-31"
    assert issue_input.random_seeds == (42, 43, 44)
    assert issue_input.split_seed == 20260731
    assert issue_input.test_size == 0.2
    assert issue_input.validation_size == 0.2
    assert issue_input.training_config_ref == "configs/train/lgbm-v1.yaml@abc1234"
    assert len(issue_input.criteria_id) == 64
    assert len(issue_input.reproducibility_id) == 64


def test_parse_issue_input_rejects_legacy_h2_issue_form_headings() -> None:
    legacy_body = structured_body().replace("### ", "## ")

    with pytest.raises(ValueError, match="issue_body must contain Issue Form headings"):
        parse_issue_input(449, "[AR] metric", legacy_body)


def test_parse_issue_input_reads_configured_guardrail() -> None:
    issue_input = parse_issue_input(
        449,
        "[AR] CTR ratio",
        structured_body(
            guardrail_metric_name="log_loss",
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression="0.01",
        ),
    )

    assert issue_input.guardrail_metric_name == "log_loss"
    assert issue_input.guardrail_metric_direction == "lower_is_better"
    assert issue_input.maximum_guardrail_regression == Decimal("0.01")


# 빈 값은 여기서 빠져 있다. 세 heading이 **모두** 비면 미선언이고(#570), 하나만 비는
# 부분 선언은 아래 `test_parse_issue_input_rejects_partially_declared_primary_metric`가
# 따로 고정한다.
@pytest.mark.parametrize("metric_name", ["1auc", "한글", "a b", "a/b", "a" * 65])
def test_parse_issue_input_rejects_invalid_primary_metric_name(metric_name: str) -> None:
    with pytest.raises(ValueError, match="primary_metric_name"):
        parse_issue_input(449, "[AR] metric", structured_body(primary_metric_name=metric_name))


@pytest.mark.parametrize("direction", ["not_applicable", "maximize"])
def test_parse_issue_input_rejects_invalid_primary_metric_direction(direction: str) -> None:
    with pytest.raises(ValueError, match="primary_metric_direction"):
        parse_issue_input(449, "[AR] metric", structured_body(primary_metric_direction=direction))


@pytest.mark.parametrize("delta", ["NaN", "Infinity", "-0.001", "not-a-number"])
def test_parse_issue_input_rejects_invalid_primary_delta(delta: str) -> None:
    with pytest.raises(ValueError, match="minimum_primary_delta"):
        parse_issue_input(449, "[AR] metric", structured_body(minimum_primary_delta=delta))


def test_parse_issue_input_accepts_zero_primary_delta() -> None:
    assert (
        parse_issue_input(449, "[AR] metric", structured_body(minimum_primary_delta="0"))
        .minimum_primary_delta
        == Decimal("0")
    )


def body_without(*headings: str, **overrides: str) -> str:
    """지정한 heading 구간을 통째로 지운 본문을 만듭니다.

    Streamlit 경로는 선언하지 않은 항목의 heading 자체를 생략하므로(#570), 그 본문을
    문자열 조작이 아니라 실제 발행 결과와 같은 모양으로 재현합니다.
    """
    body = structured_body(**overrides)
    for heading in headings:
        body = re.sub(
            rf"### {re.escape(heading)}\n.*?(?=### )",
            "",
            body,
            flags=re.DOTALL,
        )
    return body


_PRIMARY_METRIC_HEADINGS = ("주 지표 이름", "주 지표 방향", "최소 주 지표 개선폭")


def test_parse_issue_input_reads_a_body_without_primary_metric_sections() -> None:
    """주 지표 heading이 없으면 미선언으로 읽고 발행 경로를 막지 않습니다(#570)."""
    issue_input = parse_issue_input(
        449, "[AR] metric", body_without(*_PRIMARY_METRIC_HEADINGS)
    )

    assert issue_input.primary_metric_name is None
    assert issue_input.primary_metric_direction is None
    assert issue_input.minimum_primary_delta is None
    assert issue_input.issue_branch == "exp/449"
    # 봉인 자체는 계속 만들어져야 합니다 — 없으면 Actions output이 비어 후속 job이
    # 빈 문자열을 식별자로 들고 갑니다.
    assert len(issue_input.criteria_id) == 64
    assert len(issue_input.reproducibility_id) == 64


def test_parse_issue_input_seals_every_undeclared_primary_metric_alike() -> None:
    """미선언 표현이 두 가지면 같은 상태가 다른 `criteria_id`를 갖습니다."""
    first = parse_issue_input(449, "[AR] metric", body_without(*_PRIMARY_METRIC_HEADINGS))
    second = parse_issue_input(450, "[AR] other", body_without(*_PRIMARY_METRIC_HEADINGS))

    assert first.criteria_id == second.criteria_id


@pytest.mark.parametrize("omitted", _PRIMARY_METRIC_HEADINGS)
def test_parse_issue_input_rejects_partially_declared_primary_metric(omitted: str) -> None:
    """하나만 빠진 본문은 선언인지 미선언인지 읽을 수 없으므로 fail-closed입니다."""
    with pytest.raises(ValueError, match="primary metric sections must be declared together"):
        parse_issue_input(449, "[AR] metric", body_without(omitted))


def test_parse_issue_input_reads_a_body_without_a_change_section() -> None:
    """변경 내용은 가설 본문 안에 적으므로 별도 heading이 없을 수 있습니다(#570)."""
    issue_input = parse_issue_input(449, "[AR] metric", body_without("변경할 피처 · 모델"))

    assert issue_input.change == ""


def test_undeclared_primary_metric_never_qualifies_a_candidate() -> None:
    """통과 기준이 없는 실험은 어떤 후보도 승격시키지 않습니다(#570).

    여기서 끊지 않으면 `Decimal < None` 비교가 TypeError로 죽어, 판정 결과가 아니라
    워크플로 오류로 보입니다.
    """
    criteria = parse_issue_input(
        449, "[AR] metric", body_without(*_PRIMARY_METRIC_HEADINGS)
    )

    selection = select_best_candidate(
        criteria,
        "exp-449-20260801",
        "d" * 40,
        [completion_candidate("a" * 40, "0.99", criteria=criteria)],
    )

    assert selection.selection_reason == "no_qualified_candidate"
    assert selection.candidate_sha is None


def test_parse_issue_input_rejects_primary_delta_that_exceeds_decimal_input_bound() -> None:
    with pytest.raises(ValueError, match="minimum_primary_delta"):
        parse_issue_input(
            449,
            "[AR] metric",
            structured_body(minimum_primary_delta="1" + "0" * 400),
        )


@pytest.mark.parametrize(
    ("name", "direction", "maximum_regression"),
    [
        ("없음", "higher_is_better", "없음"),
        ("없음", "not_applicable", "0"),
        ("log_loss", "not_applicable", "0.01"),
        ("log_loss", "lower_is_better", "없음"),
        ("1invalid", "lower_is_better", "0.01"),
        ("log_loss", "lower_is_better", "-0.01"),
        ("log_loss", "lower_is_better", "NaN"),
    ],
)
def test_parse_issue_input_rejects_inconsistent_guardrail(
    name: str,
    direction: str,
    maximum_regression: str,
) -> None:
    with pytest.raises(ValueError, match="guardrail"):
        parse_issue_input(
            449,
            "[AR] metric",
            structured_body(
                guardrail_metric_name=name,
                guardrail_metric_direction=direction,
                maximum_guardrail_regression=maximum_regression,
            ),
        )


def test_parse_issue_input_rejects_guardrail_regression_that_exceeds_decimal_input_bound() -> None:
    with pytest.raises(ValueError, match="maximum_guardrail_regression"):
        parse_issue_input(
            449,
            "[AR] metric",
            structured_body(
                guardrail_metric_name="log_loss",
                guardrail_metric_direction="lower_is_better",
                maximum_guardrail_regression="1" + "0" * 400,
            ),
        )


@pytest.mark.parametrize("field_name", ["dataset_snapshot", "training_config_ref"])
# 빈 값과 공백만 있는 값은 미선언이라 거부 대상이 아니다(#570). 길이 상한만 남는다.
@pytest.mark.parametrize("value", ["x" * 257])
def test_parse_issue_input_rejects_invalid_text_reference(field_name: str, value: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        parse_issue_input(449, "[AR] metric", structured_body(**{field_name: value}))


@pytest.mark.parametrize("random_seeds", ["42, 42", "42, -1", "42, 1.5", "42,"])
def test_parse_issue_input_rejects_invalid_random_seeds(random_seeds: str) -> None:
    with pytest.raises(ValueError, match="random_seeds"):
        parse_issue_input(449, "[AR] metric", structured_body(random_seeds=random_seeds))


@pytest.mark.parametrize("split_seed", ["-1", "1.5", "seed"])
def test_parse_issue_input_rejects_invalid_split_seed(split_seed: str) -> None:
    with pytest.raises(ValueError, match="split_seed"):
        parse_issue_input(449, "[AR] metric", structured_body(split_seed=split_seed))


@pytest.mark.parametrize(
    ("test_size", "validation_size"),
    [
        ("0", "0.2"),
        ("0.2", "0"),
        ("1", "0.2"),
        ("0.7", "0.3"),
        ("NaN", "0.2"),
        ("0.2", "Infinity"),
        ("1e-400", "0.2"),
        ("0.2", "1e-400"),
        ("0.1", "0.89999999999999999"),
    ],
)
def test_parse_issue_input_rejects_invalid_split_sizes(
    test_size: str,
    validation_size: str,
) -> None:
    with pytest.raises(ValueError, match="test_size|validation_size"):
        parse_issue_input(
            449,
            "[AR] metric",
            structured_body(test_size=test_size, validation_size=validation_size),
        )


@pytest.mark.parametrize(
    ("changed_field", "changed_value", "identifier_name"),
    [
        ("primary_metric_name", "pr_auc", "criteria_id"),
        ("primary_metric_direction", "lower_is_better", "criteria_id"),
        ("minimum_primary_delta", "0.003", "criteria_id"),
        ("guardrail_metric_name", "log_loss", "criteria_id"),
        ("dataset_snapshot", "bq://autoresearch/train@2026-08-01", "reproducibility_id"),
        ("random_seeds", "45, 46, 47", "reproducibility_id"),
        ("split_seed", "7", "reproducibility_id"),
        ("test_size", "0.25", "reproducibility_id"),
        ("validation_size", "0.25", "reproducibility_id"),
        ("training_config_ref", "configs/train/lgbm-v2.yaml@def5678", "reproducibility_id"),
    ],
)
def test_identifiers_include_each_structured_field(
    changed_field: str,
    changed_value: str,
    identifier_name: str,
) -> None:
    original = parse_issue_input(449, "[AR] metric", structured_body())
    changes = {changed_field: changed_value}
    if changed_field == "guardrail_metric_name":
        changes.update(
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression="0.01",
        )
    changed = parse_issue_input(449, "[AR] metric", structured_body(**changes))

    assert getattr(changed, identifier_name) != getattr(original, identifier_name)
    other_identifier = "reproducibility_id" if identifier_name == "criteria_id" else "criteria_id"
    assert getattr(changed, other_identifier) == getattr(original, other_identifier)


def test_criteria_id_includes_guardrail_direction_and_maximum_regression() -> None:
    original = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(
            guardrail_metric_name="log_loss",
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression="0.01",
        ),
    )
    changed_direction = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(
            guardrail_metric_name="log_loss",
            guardrail_metric_direction="higher_is_better",
            maximum_guardrail_regression="0.01",
        ),
    )
    changed_maximum = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(
            guardrail_metric_name="log_loss",
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression="0.02",
        ),
    )

    assert changed_direction.criteria_id != original.criteria_id
    assert changed_maximum.criteria_id != original.criteria_id


def test_identifiers_canonicalize_equivalent_decimal_spellings() -> None:
    original = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(
            minimum_primary_delta="0.20",
            guardrail_metric_name="log_loss",
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression="0.010",
            test_size="0.20",
            validation_size="0.20",
        ),
    )
    equivalent = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(
            minimum_primary_delta="0.2",
            guardrail_metric_name="log_loss",
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression="0.01",
            test_size="0.2",
            validation_size="0.2",
        ),
    )

    assert equivalent.criteria_id == original.criteria_id
    assert equivalent.reproducibility_id == original.reproducibility_id


def test_parse_issue_input_rejects_legacy_unstructured_headings() -> None:
    legacy_body = structured_body().replace(
        "### 주 지표 이름\nroc_auc\n\n",
        "### 성공 기준 — 주 지표 1개와 수치 임계\nROC-AUC +0.002\n\n",
    )

    with pytest.raises(ValueError, match="unknown Issue Form heading"):
        parse_issue_input(449, "[AR] metric", legacy_body)


def test_parse_issue_input_rejects_a_body_without_the_hypothesis() -> None:
    """`연구 가설`은 유일하게 남은 필수 heading입니다(#570)."""
    body = body_without("연구 가설")

    with pytest.raises(ValueError, match="hypothesis"):
        parse_issue_input(449, "[AR] metric", body)


def test_parse_issue_input_reads_a_body_without_execution_settings() -> None:
    """Streamlit 경로가 발행한 본문은 실행 설정 heading을 갖지 않습니다(#570)."""
    issue_input = parse_issue_input(
        449,
        "[AR] metric",
        body_without(
            "변경할 피처 · 모델",
            "주 지표 이름",
            "주 지표 방향",
            "최소 주 지표 개선폭",
            "Guardrail 지표 이름",
            "Guardrail 지표 방향",
            "최대 Guardrail 악화폭",
            "보조 관측 지표",
            "비교 대상",
            "데이터셋 스냅샷",
            "랜덤 시드 목록",
            "Split 시드",
            "Test 비율",
            "Validation 비율",
            "학습 설정 참조",
            "대상 데이터 · 기간",
            "스냅샷 재사용",
            "허용 범위",
        ),
    )

    assert issue_input.issue_branch == "exp/449"
    assert issue_input.comparison is None
    assert issue_input.dataset_snapshot is None
    assert issue_input.random_seeds == ()
    assert issue_input.split_seed is None
    assert issue_input.test_size is None
    assert issue_input.validation_size is None
    assert issue_input.training_config_ref is None
    assert issue_input.snapshot_reuse is None
    # 부재가 권한을 넓히는 방향으로 읽히면 실행기가 잠긴 경로를 건드릴 수 있습니다.
    assert issue_input.allowed_scope == ()
    assert len(issue_input.criteria_id) == 64
    assert len(issue_input.reproducibility_id) == 64


@pytest.mark.parametrize("omitted", ["Test 비율", "Validation 비율"])
def test_parse_issue_input_rejects_partially_declared_split_sizes(omitted: str) -> None:
    """하나만 있으면 나머지를 지어내야 하는데 그 값은 이슈에 적혀 있지 않습니다."""
    with pytest.raises(ValueError, match="declared together"):
        parse_issue_input(449, "[AR] metric", body_without(omitted))


def test_parse_issue_input_rejects_duplicate_structured_heading() -> None:
    body = structured_body().replace(
        "### Split 시드\n20260731",
        "### Split 시드\n20260731\n\n### Split 시드\n7",
    )

    with pytest.raises(ValueError, match="duplicate Issue Form heading"):
        parse_issue_input(449, "[AR] metric", body)


def test_parse_issue_input_rejects_unknown_selected_scope() -> None:
    with pytest.raises(ValueError, match="allowed_scope"):
        parse_issue_input(
            449,
            "[AR] metric",
            structured_body(allowed_scope="- [x] 배포 워크플로우를 수정한다"),
        )


def test_identifiers_ignore_human_oriented_free_text() -> None:
    original = parse_issue_input(449, "[AR] metric", structured_body())
    changed_body = structured_body().replace(
        "비율 피처가 ROC-AUC를 높인다.",
        "사람이 읽는 가설 설명을 보완한다.",
    ).replace(
        "- 데이터셋 / 경로: data/train.csv",
        "- 데이터셋 / 경로: 설명용 별칭",
    )
    changed = parse_issue_input(449, "[AR] metric", changed_body)

    assert changed.criteria_id == original.criteria_id
    assert changed.reproducibility_id == original.reproducibility_id


def test_identifiers_ignore_fields_outside_fixed_structured_contracts() -> None:
    original = parse_issue_input(449, "[AR] metric", structured_body())
    changed_issue_number = parse_issue_input(450, "[AR] metric", structured_body())
    changed_comparison = parse_issue_input(
        449,
        "[AR] metric",
        structured_body().replace("동일 조건 baseline 재학습 (권장)", "둘 다"),
    )
    changed_snapshot_reuse = parse_issue_input(
        449,
        "[AR] metric",
        structured_body().replace(
            "허용 (진행하되 실제로 쓴 데이터를 결과에 명시)",
            "불허 (정규 조립 경로 실패 시 중단)",
        ),
    )
    changed_scope = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(
            allowed_scope="- [x] prod 모델 계약(`autoresearch/feature_engineering/model_contract.py`) 수정을 허용한다"
        ),
    )

    for changed in (
        changed_issue_number,
        changed_comparison,
        changed_scope,
        changed_snapshot_reuse,
    ):
        assert changed.criteria_id == original.criteria_id
        assert changed.reproducibility_id == original.reproducibility_id


def test_branch_name_is_single_issue_coordinate() -> None:
    assert branch_name_for(449) == "exp/449"


def test_branch_name_rejects_a_non_positive_issue_number() -> None:
    """이슈 번호가 유일한 좌표이므로 그것이 무효면 브랜치 이름도 만들 수 없다."""
    with pytest.raises(ValueError):
        branch_name_for(0)


@pytest.mark.parametrize(
    ("compare_status", "expected"),
    [("ahead", True), ("identical", True), ("behind", False), ("diverged", False)],
)
def test_is_descendant_accepts_only_ancestor_safe_statuses(
    compare_status: str,
    expected: bool,
) -> None:
    assert is_descendant(compare_status) is expected


def run_cli(tmp_path: Path, body: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    """실제 CLI를 임시 이슈 본문과 GitHub output 경로로 실행합니다."""
    body_path = tmp_path / "issue.md"
    output_path = tmp_path / "github-output.txt"
    body_path.write_text(body, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "tools/auto_research_issue_branch.py",
            "--issue-number",
            "449",
            "--issue-title",
            "[AR] CTR ratio",
            "--issue-body-file",
            str(body_path),
        ],
        check=False,
        capture_output=True,
        cwd=PROJECT_ROOT,
        env={**os.environ, "GITHUB_OUTPUT": str(output_path)},
        text=True,
    )
    return result, output_path


def test_cli_writes_only_issue_contract_outputs(tmp_path: Path) -> None:
    result, output_path = run_cli(tmp_path, structured_body())
    issue_input = parse_issue_input(449, "[AR] CTR ratio", structured_body())

    assert result.returncode == 0, result.stderr
    assert output_path.read_text(encoding="utf-8").splitlines() == [
        "issue_branch=exp/449",
        "criteria_id=" + issue_input.criteria_id,
        "reproducibility_id=" + issue_input.reproducibility_id,
    ]


def test_cli_fails_closed_without_outputs_for_invalid_form_body(tmp_path: Path) -> None:
    result, output_path = run_cli(tmp_path, structured_body(random_seeds="42, 42"))

    assert result.returncode != 0
    assert "random_seeds" in result.stderr
    assert not output_path.exists()


def completion_candidate(
    candidate_sha: str,
    primary_candidate_metric: str,
    *,
    candidate_id: str | None = None,
    criteria_id: str | None = None,
    reproducibility_id: str | None = None,
    primary_baseline_metric: str = "0.75",
    artifact_uri: str = "gs://autoresearch/experiments/artifact.json",
    log_uri: str = "https://logs.example.test/run/449",
    guardrail_candidate_metric: str | None = None,
    guardrail_baseline_metric: str | None = None,
    criteria: IssueInput | None = None,
) -> dict[str, object]:
    """완료 event producer가 전달해야 하는 완전한 후보 payload를 만듭니다."""
    issue_criteria = criteria or parse_issue_input(449, "[AR] metric", structured_body())
    payload: dict[str, object] = {
        "schema_version": 1,
        "issue_number": 449,
        "experiment_id": "exp-449-20260801",
        "base_dev_sha": "d" * 40,
        "candidate_id": candidate_id or f"candidate-{candidate_sha[:12]}",
        "candidate_sha": candidate_sha,
        "criteria_id": criteria_id or issue_criteria.criteria_id,
        "reproducibility_id": reproducibility_id or issue_criteria.reproducibility_id,
        "primary_candidate_metric": primary_candidate_metric,
        "primary_baseline_metric": primary_baseline_metric,
        "artifact_uri": artifact_uri,
        "log_uri": log_uri,
    }
    if guardrail_candidate_metric is not None and guardrail_baseline_metric is not None:
        payload["guardrail_candidate_metric"] = guardrail_candidate_metric
        payload["guardrail_baseline_metric"] = guardrail_baseline_metric
    return payload


def test_select_best_candidate_uses_primary_direction_then_sha() -> None:
    criteria = parse_issue_input(449, "[AR] metric", structured_body(minimum_primary_delta="0"))

    selection = select_best_candidate(
        criteria,
        "exp-449-20260801",
        "d" * 40,
        [
            completion_candidate("b" * 40, "0.79", criteria=criteria),
            completion_candidate("a" * 40, "0.79", criteria=criteria),
        ],
    )

    assert selection.candidate_sha == "a" * 40
    assert selection.selection_reason == "qualified_candidate"


def test_select_best_candidate_returns_normal_no_qualified_result() -> None:
    criteria = parse_issue_input(449, "[AR] metric", structured_body(minimum_primary_delta="0.05"))

    selection = select_best_candidate(
        criteria,
        "exp-449-20260801",
        "d" * 40,
        [completion_candidate("a" * 40, "0.79", criteria=criteria)],
    )

    assert selection.candidate_sha is None
    assert selection.selection_reason == "no_qualified_candidate"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("criteria_id", "0" * 64),
        ("reproducibility_id", "0" * 64),
        ("issue_number", 450),
        ("experiment_id", "another-experiment"),
        ("base_dev_sha", "a" * 40),
    ],
)
def test_select_best_candidate_rejects_mismatched_event_coordinates(
    field: str,
    value: object,
) -> None:
    criteria = parse_issue_input(449, "[AR] metric", structured_body())
    candidate = completion_candidate("a" * 40, "0.79")
    candidate[field] = value

    with pytest.raises(ValueError, match=field):
        select_best_candidate(
            criteria,
            "exp-449-20260801",
            "d" * 40,
            [candidate],
        )


@pytest.mark.parametrize("metric", [0.79, True, "NaN", "Infinity", " 0.79", "0.79 "])
def test_select_best_candidate_rejects_noncanonical_or_nonfinite_metrics(metric: object) -> None:
    criteria = parse_issue_input(449, "[AR] metric", structured_body())
    candidate = completion_candidate("a" * 40, "0.79")
    candidate["primary_candidate_metric"] = metric

    with pytest.raises(ValueError, match="primary_candidate_metric"):
        select_best_candidate(
            criteria,
            "exp-449-20260801",
            "d" * 40,
            [candidate],
        )


def test_select_best_candidate_rejects_unknown_keys_duplicate_sha_and_baseline_mismatch() -> None:
    criteria = parse_issue_input(449, "[AR] metric", structured_body())
    unknown = completion_candidate("a" * 40, "0.79")
    unknown["untrusted"] = "value"
    with pytest.raises(ValueError, match="unknown candidate keys"):
        select_best_candidate(criteria, "exp-449-20260801", "d" * 40, [unknown])

    duplicate_sha = [
        completion_candidate("a" * 40, "0.79"),
        completion_candidate("a" * 40, "0.80", candidate_id="candidate-other"),
    ]
    with pytest.raises(ValueError, match="candidate_sha must be unique"):
        select_best_candidate(criteria, "exp-449-20260801", "d" * 40, duplicate_sha)

    mismatched_baseline = [
        completion_candidate("a" * 40, "0.79", primary_baseline_metric="0.75"),
        completion_candidate("b" * 40, "0.80", primary_baseline_metric="0.76"),
    ]
    with pytest.raises(ValueError, match="primary_baseline_metric"):
        select_best_candidate(criteria, "exp-449-20260801", "d" * 40, mismatched_baseline)


def test_select_best_candidate_rejects_non_object_candidate_as_schema_error() -> None:
    criteria = parse_issue_input(449, "[AR] metric", structured_body())

    with pytest.raises(ValueError, match="candidate must be an object"):
        select_best_candidate(criteria, "exp-449-20260801", "d" * 40, ["not-an-object"])  # type: ignore[list-item]


def test_select_best_candidate_enforces_guardrail_direction_and_result_set_order_independence() -> None:
    criteria = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(
            minimum_primary_delta="0",
            guardrail_metric_name="log_loss",
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression="0.01",
        ),
    )
    qualified = completion_candidate(
        "a" * 40,
        "0.80",
        guardrail_candidate_metric="0.31",
        guardrail_baseline_metric="0.30",
        criteria=criteria,
    )
    rejected = completion_candidate(
        "b" * 40,
        "0.90",
        guardrail_candidate_metric="0.32",
        guardrail_baseline_metric="0.30",
        criteria=criteria,
    )

    forward = select_best_candidate(criteria, "exp-449-20260801", "d" * 40, [qualified, rejected])
    reverse = select_best_candidate(criteria, "exp-449-20260801", "d" * 40, [rejected, qualified])

    assert forward.candidate_sha == "a" * 40
    assert forward.result_set_id == reverse.result_set_id


def load_promotion_workflow() -> dict[object, object]:
    """완료 event의 dev promotion workflow를 실제 YAML parser로 읽습니다."""
    parsed = yaml.safe_load(PROMOTION_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


@_requires_active_dev_promotion_workflow
def test_promotion_workflow_has_only_completion_triggers_and_minimum_permissions() -> None:
    workflow = load_promotion_workflow()

    assert "permissions" not in workflow
    validation_job = workflow["jobs"]["validate-coordinate"]
    promotion_job = workflow["jobs"]["promote-completed-experiment"]
    assert isinstance(validation_job, dict)
    assert isinstance(promotion_job, dict)
    assert validation_job["permissions"] == {}
    assert promotion_job["permissions"] == {"contents": "write", "issues": "write"}
    assert workflow_trigger(workflow) == {
        "repository_dispatch": {"types": ["auto-research-experiment-completed"]},
        "workflow_dispatch": {
            "inputs": {
                "issue_number": {"required": True, "type": "string"},
                "experiment_id": {"required": True, "type": "string"},
                "candidates_json": {"required": True, "type": "string"},
            }
        },
    }


@_requires_active_dev_promotion_workflow
def test_promotion_workflow_uses_all_candidate_lineage_before_selector_and_dev_merge_only() -> None:
    workflow = load_promotion_workflow()
    job = workflow["jobs"]["promote-completed-experiment"]
    assert isinstance(job, dict)
    assert job["needs"] == "validate-coordinate"
    assert job["concurrency"] == {
        "group": "auto-research-dev-promotion",
        "queue": "max",
        "cancel-in-progress": False,
    }
    steps = job["steps"]
    assert isinstance(steps, list)
    scripts = [step["with"]["script"] for step in steps if "with" in step and "script" in step["with"]]
    assert len(scripts) == 1
    script = scripts[0]
    assert isinstance(script, str)

    candidate_loop = re.search(
        r"for \(const candidate of candidates\) \{(?P<body>.*?)\n\s*\}\n\n\s*const bodyPath",
        script,
        flags=re.DOTALL,
    )
    assert candidate_loop is not None
    loop_body = candidate_loop.group("body")
    assert "base: recorded.baseDevSha" in loop_body
    assert "head: candidate.candidate_sha" in loop_body
    assert "base: candidate.candidate_sha" in loop_body
    assert "head: recorded.issueBranch" in loop_body
    assert loop_body.index("base: recorded.baseDevSha") < loop_body.index("base: candidate.candidate_sha")
    assert script.index("for (const candidate of candidates)") < script.index("selectorOutput = execFileSync")
    assert "selector.criteria_id !== recorded.criteriaId" in script
    assert "selector.reproducibility_id !== recorded.reproducibilityId" in script
    assert script.index("selector.criteria_id !== recorded.criteriaId") < script.index("github.rest.repos.merge")

    merge_call = re.search(
        r"await github\.rest\.repos\.merge\(\{(?P<arguments>.*?)\}\);",
        script,
        flags=re.DOTALL,
    )
    assert merge_call is not None
    merge_arguments = merge_call.group("arguments")
    assert "base: 'dev'" in merge_arguments
    assert "head: selectedCandidateSha" in merge_arguments
    assert "main" not in merge_arguments
    assert "updateRef" not in script
    assert "createRef" not in script
    assert "pulls." not in script


@_requires_active_dev_promotion_workflow
def test_promotion_workflow_marker_idempotency_blocks_changed_result_sets_before_merge() -> None:
    workflow = load_promotion_workflow()
    steps = workflow["jobs"]["promote-completed-experiment"]["steps"]
    assert isinstance(steps, list)
    script = next(step["with"]["script"] for step in steps if "with" in step and "script" in step["with"])
    assert isinstance(script, str)

    marker_path = re.search(
        r"const markerComments = .*?\n(?P<idempotency>.*?)\n\s*if \(selector\.selection_reason",
        script,
        flags=re.DOTALL,
    )
    assert marker_path is not None
    idempotency = marker_path.group("idempotency")
    assert "github-actions[bot]" in idempotency
    assert "matchingResultMarkers" in idempotency
    assert "recordedResult.experimentId === experimentId" in idempotency
    assert "recordedResult.resultSetId !== selector.result_set_id" in idempotency
    assert "return;" in idempotency
    assert idempotency.index("recordedResult.resultSetId !== selector.result_set_id") < idempotency.index("return;")
    assert script.index("recordedResult.resultSetId !== selector.result_set_id") < script.index("github.rest.repos.merge")


def test_selection_keeps_subnormal_and_precise_thresholds_as_decimal() -> None:
    subnormal_criteria = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(minimum_primary_delta="1e-400"),
    )
    assert subnormal_criteria.minimum_primary_delta == Decimal("1e-400")

    precise_delta = "0.1234567890123456789012345678901234567890"
    precise_criteria = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(minimum_primary_delta=precise_delta),
    )
    selection = select_best_candidate(
        precise_criteria,
        "exp-449-20260801",
        "d" * 40,
        [
            completion_candidate(
                "a" * 40,
                precise_delta,
                primary_baseline_metric="0",
                criteria=precise_criteria,
            )
        ],
    )

    assert precise_criteria.minimum_primary_delta == Decimal(precise_delta)
    assert selection.candidate_sha == "a" * 40


def test_selection_preserves_64_digit_primary_delta_threshold_beyond_default_context() -> None:
    baseline = f"1.{'0' * 63}"
    minimum_delta = f"0.1234567890123456789012345678{'1'}{'0' * 34}"
    criteria = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(minimum_primary_delta=minimum_delta),
    )
    selection = select_best_candidate(
        criteria,
        "exp-449-20260801",
        "d" * 40,
        [
            completion_candidate(
                "a" * 40,
                f"1.{minimum_delta[2:]}",
                primary_baseline_metric=baseline,
                criteria=criteria,
            )
        ],
    )

    assert criteria.minimum_primary_delta == Decimal(minimum_delta)
    assert selection.candidate_sha == "a" * 40


def test_selection_preserves_threshold_boundary_across_permitted_decimal_exponents() -> None:
    criteria = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(minimum_primary_delta="1e1000"),
    )
    selection = select_best_candidate(
        criteria,
        "exp-449-20260801",
        "d" * 40,
        [
            completion_candidate(
                "a" * 40,
                "1e1000",
                primary_baseline_metric="1e-1000",
                criteria=criteria,
            )
        ],
    )

    assert selection.selection_reason == "no_qualified_candidate"


def test_selection_rejects_guardrail_regression_beyond_default_decimal_context() -> None:
    baseline = f"1.{'0' * 63}"
    maximum_regression = "0.1234567890123456789012345678"
    actual_regression = f"{maximum_regression}1{'0' * 34}"
    criteria = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(
            minimum_primary_delta="0",
            guardrail_metric_name="log_loss",
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression=maximum_regression,
        ),
    )
    selection = select_best_candidate(
        criteria,
        "exp-449-20260801",
        "d" * 40,
        [
            completion_candidate(
                "a" * 40,
                baseline,
                primary_baseline_metric=baseline,
                guardrail_candidate_metric=f"1.{actual_regression[2:]}",
                guardrail_baseline_metric=baseline,
                criteria=criteria,
            )
        ],
    )

    assert selection.selection_reason == "no_qualified_candidate"


def test_selection_ranks_64_digit_primary_metrics_beyond_default_decimal_context() -> None:
    baseline = f"1.{'0' * 63}"
    criteria = parse_issue_input(449, "[AR] metric", structured_body(minimum_primary_delta="0"))
    selection = select_best_candidate(
        criteria,
        "exp-449-20260801",
        "d" * 40,
        [
            completion_candidate(
                "a" * 40,
                f"1.{'0' * 62}1",
                primary_baseline_metric=baseline,
                criteria=criteria,
            ),
            completion_candidate(
                "b" * 40,
                f"1.{'0' * 62}2",
                primary_baseline_metric=baseline,
                criteria=criteria,
            ),
        ],
    )

    assert selection.candidate_sha == "b" * 40


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("issue_number", True),
        ("issue_number", 449.0),
    ],
)
def test_select_best_candidate_rejects_non_integer_schema_coordinates(
    field: str,
    value: object,
) -> None:
    criteria = parse_issue_input(449, "[AR] metric", structured_body())
    candidate = completion_candidate("a" * 40, "0.79")
    candidate[field] = value

    with pytest.raises(ValueError, match=field):
        select_best_candidate(criteria, "exp-449-20260801", "d" * 40, [candidate])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("primary_candidate_metric", "1" * (MAX_DECIMAL_DIGITS + 1)),
        ("primary_candidate_metric", f"1e-{MAX_DECIMAL_EXPONENT + 1}"),
        ("primary_candidate_metric", "1" * (MAX_DECIMAL_TEXT_LENGTH + 1)),
        ("artifact_uri", " \t "),
        ("log_uri", "line-one\nline-two"),
        ("artifact_uri", "a" * (MAX_RESULT_REFERENCE_LENGTH + 1)),
    ],
)
def test_select_best_candidate_rejects_resource_exhaustion_and_invalid_references(
    field: str,
    value: str,
) -> None:
    criteria = parse_issue_input(449, "[AR] metric", structured_body())
    candidate = completion_candidate("a" * 40, "0.79")
    candidate[field] = value

    with pytest.raises(ValueError, match=field):
        select_best_candidate(criteria, "exp-449-20260801", "d" * 40, [candidate])


def test_select_best_candidate_rejects_candidate_count_above_contract_limit() -> None:
    criteria = parse_issue_input(449, "[AR] metric", structured_body())
    candidate = completion_candidate("a" * 40, "0.79")

    with pytest.raises(ValueError, match="candidates must contain at most"):
        select_best_candidate(
            criteria,
            "exp-449-20260801",
            "d" * 40,
            [candidate] * (MAX_COMPLETION_CANDIDATES + 1),
        )


@_requires_active_dev_promotion_workflow
def test_promotion_workflow_validates_coordinate_before_global_queue_and_uses_trusted_checkout() -> None:
    workflow = load_promotion_workflow()
    validation_job = workflow["jobs"]["validate-coordinate"]
    job = workflow["jobs"]["promote-completed-experiment"]
    assert isinstance(validation_job, dict)
    assert isinstance(job, dict)
    assert job["concurrency"] == {
        "group": "auto-research-dev-promotion",
        "queue": "max",
        "cancel-in-progress": False,
    }
    validation_steps = validation_job["steps"]
    assert isinstance(validation_steps, list)
    validation_script = next(
        step["with"]["script"]
        for step in validation_steps
        if "with" in step and "script" in step["with"]
    )
    assert isinstance(validation_script, str)
    assert "typeof rawIssueNumber !== 'string'" in validation_script
    assert "/^[1-9][0-9]*$/" in validation_script
    assert "typeof rawExperimentId !== 'string'" in validation_script
    # 런타임 계약(`context.py`, `paired_experiment.py`)과 동일해야 한다 — 넓게 두면
    # dev 병합을 통과한 값이 main 승격에서 거부된다(#495 버그 B).
    assert "/^[a-z0-9][a-z0-9-]{0,31}$/" in validation_script
    assert "core.setOutput('issue_number', rawIssueNumber);" in validation_script
    assert "core.setOutput('experiment_id', rawExperimentId);" in validation_script
    assert "actions/checkout" not in str(validation_steps)
    steps = job["steps"]
    assert isinstance(steps, list)
    checkout_step = next(step for step in steps if step["name"] == "Checkout trusted selector")
    assert checkout_step["with"] == {
        "ref": "${{ github.workflow_sha }}",
        "fetch-depth": 1,
        "persist-credentials": False,
    }
    script = next(step["with"]["script"] for step in steps if "with" in step and "script" in step["with"])
    assert isinstance(script, str)

    selector_env = re.search(
        r"const selectorEnvironment = \{(?P<entries>.*?)\n\s*\};",
        script,
        flags=re.DOTALL,
    )
    assert selector_env is not None
    env_names = re.findall(r"^\s*([A-Z0-9_]+):", selector_env.group("entries"), flags=re.MULTILINE)
    assert env_names == ["PATH", "LANG", "PYTHONUTF8", "GITHUB_OUTPUT"]
    exec_call = re.search(
        r"selectorOutput = execFileSync\(.*?\{ encoding: 'utf8', env: selectorEnvironment \},",
        script,
        flags=re.DOTALL,
    )
    assert exec_call is not None
    assert "...process.env" not in exec_call.group(0)

    promotion_script_step = next(
        step for step in steps if "with" in step and "script" in step["with"]
    )
    assert promotion_script_step["env"] == {
        "ISSUE_NUMBER": "${{ needs.validate-coordinate.outputs.issue_number }}",
        "EXPERIMENT_ID": "${{ needs.validate-coordinate.outputs.experiment_id }}",
    }


@_requires_active_dev_promotion_workflow
def test_promotion_workflow_caps_candidates_before_lineage_or_compare_requests() -> None:
    workflow = load_promotion_workflow()
    steps = workflow["jobs"]["promote-completed-experiment"]["steps"]
    assert isinstance(steps, list)
    script = next(step["with"]["script"] for step in steps if "with" in step and "script" in step["with"])
    assert isinstance(script, str)

    cap_check = "if (!Array.isArray(candidates) || candidates.length === 0 || candidates.length > _MAX_COMPLETION_CANDIDATES)"
    assert "const _MAX_COMPLETION_CANDIDATES = 50;" in script
    assert cap_check in script
    assert script.index(cap_check) < script.index("const issue = await github.rest.issues.get")
    assert script.index(cap_check) < script.index("for (const candidate of candidates)")
    assert script.index(cap_check) < script.index("github.rest.repos.compareCommits")


@_requires_active_dev_promotion_workflow
def test_promotion_workflow_claims_before_merge_and_recovers_pending_state() -> None:
    workflow = load_promotion_workflow()
    steps = workflow["jobs"]["promote-completed-experiment"]["steps"]
    assert isinstance(steps, list)
    script = next(step["with"]["script"] for step in steps if "with" in step and "script" in step["with"])
    assert isinstance(script, str)

    pending_create = re.search(
        r"const pendingComment = await github\.rest\.issues\.createComment\(\{(?P<arguments>.*?)\}\);",
        script,
        flags=re.DOTALL,
    )
    assert pending_create is not None
    assert "issue_number: issueNumber" in pending_create.group("arguments")
    assert "'pending'" in pending_create.group("arguments")
    merge_call = re.search(
        r"mergeResponse = await github\.rest\.repos\.merge\(\{(?P<arguments>.*?)\}\);",
        script,
        flags=re.DOTALL,
    )
    assert merge_call is not None
    assert "base: 'dev'" in merge_call.group("arguments")
    assert "head: selectedCandidateSha" in merge_call.group("arguments")
    assert pending_create.start() < merge_call.start()
    assert "mergeResponse.status === 201 || mergeResponse.status === 204" in script
    assert "error.status === 409" in script

    reconciliation = re.search(
        r"const reconciliation = await github\.rest\.repos\.compareCommits\(\{(?P<arguments>.*?)\}\);",
        script,
        flags=re.DOTALL,
    )
    assert reconciliation is not None
    assert [line.strip() for line in reconciliation.group("arguments").splitlines() if line.strip()] == [
        "owner,",
        "repo,",
        "base: selectedCandidateSha,",
        "head: 'dev',",
    ]
    assert reconciliation.start() < merge_call.start()
    marker_update = re.search(
        r"await github\.rest\.issues\.updateComment\(\{(?P<arguments>.*?)\}\);",
        script,
        flags=re.DOTALL,
    )
    assert marker_update is not None
    assert "comment_id: pendingCommentId" in marker_update.group("arguments")
    assert merge_call.start() < marker_update.start()


@_requires_active_dev_promotion_workflow
def test_promotion_workflow_confirms_selector_sha_was_lineage_verified_before_merge() -> None:
    workflow = load_promotion_workflow()
    steps = workflow["jobs"]["promote-completed-experiment"]["steps"]
    assert isinstance(steps, list)
    script = next(step["with"]["script"] for step in steps if "with" in step and "script" in step["with"])
    assert isinstance(script, str)

    lineage_loop = script.index("for (const candidate of candidates)")
    add_verified_sha = script.index("verifiedCandidateShas.add(candidate.candidate_sha)")
    membership_check = script.index("if (!verifiedCandidateShas.has(selectedCandidateSha))")
    merge_call = script.index("github.rest.repos.merge")
    assert lineage_loop < add_verified_sha < membership_check < merge_call
    assert "pending|merged|no_qualified|merge_conflict|merge_api_failed" in script


@_requires_active_dev_promotion_workflow
def test_promotion_workflow_leaves_pending_marker_when_state_update_fails() -> None:
    workflow = load_promotion_workflow()
    steps = workflow["jobs"]["promote-completed-experiment"]["steps"]
    assert isinstance(steps, list)
    script = next(step["with"]["script"] for step in steps if "with" in step and "script" in step["with"])
    assert isinstance(script, str)

    state_machine = re.search(
        r"async function mergePendingCandidate\(pendingCommentId\) \{(?P<body>.*?)\n\s*\}\n\n\s*async function updateResultMarker",
        script,
        flags=re.DOTALL,
    )
    assert state_machine is not None
    body = state_machine.group("body")
    assert "let mergeResponse;" in body
    assert "await updateResultMarker(pendingCommentId, finalState);" in body
    assert body.index("await updateResultMarker(pendingCommentId, finalState);") < body.index(
        "if (finalState !== 'merged')"
    )


@_requires_active_dev_promotion_workflow
def test_promotion_workflow_keeps_malformed_201_response_outside_merge_api_catch() -> None:
    workflow = load_promotion_workflow()
    steps = workflow["jobs"]["promote-completed-experiment"]["steps"]
    assert isinstance(steps, list)
    script = next(step["with"]["script"] for step in steps if "with" in step and "script" in step["with"])
    assert isinstance(script, str)

    state_machine = re.search(
        r"async function mergePendingCandidate\(pendingCommentId\) \{(?P<body>.*?)\n\s*\}\n\n\s*async function updateResultMarker",
        script,
        flags=re.DOTALL,
    )
    assert state_machine is not None
    body = state_machine.group("body")
    transport_boundary = re.search(
        r"let mergeResponse;\n\s*try \{(?P<transport>.*?)\n\s*\} catch \(error\) \{(?P<failure>.*?)\n\s*\}\n\n\s*if \(mergeResponse\.status === 201\)",
        body,
        flags=re.DOTALL,
    )
    assert transport_boundary is not None
    assert "github.rest.repos.merge" in transport_boundary.group("transport")
    assert "requireMatch(mergeResponse.data.sha" not in transport_boundary.group("failure")
    assert "requireMatch(mergeResponse.data.sha" in body
    assert body.index("} catch (error) {") < body.index("requireMatch(mergeResponse.data.sha")


@_requires_active_dev_promotion_workflow
def test_promotion_workflow_fails_terminal_merge_failure_without_reconciliation_or_remerge() -> None:
    workflow = load_promotion_workflow()
    steps = workflow["jobs"]["promote-completed-experiment"]["steps"]
    assert isinstance(steps, list)
    script = next(step["with"]["script"] for step in steps if "with" in step and "script" in step["with"])
    assert isinstance(script, str)

    terminal_state = re.search(
        r"if \(recordedResult\.state !== 'pending'\) \{(?P<body>.*?)\n\s*\}\n\s*const pendingCommentId",
        script,
        flags=re.DOTALL,
    )
    assert terminal_state is not None
    body = terminal_state.group("body")
    assert "['merge_conflict', 'merge_api_failed'].includes(recordedResult.state)" in body
    assert "failClosed(" in body
    assert terminal_state.start() < script.index("const reconciliation")
    assert terminal_state.start() < script.index("github.rest.repos.merge")


@_requires_active_dev_promotion_workflow
def test_promotion_workflow_fails_after_recording_new_merge_failure_state() -> None:
    workflow = load_promotion_workflow()
    steps = workflow["jobs"]["promote-completed-experiment"]["steps"]
    assert isinstance(steps, list)
    script = next(step["with"]["script"] for step in steps if "with" in step and "script" in step["with"])
    assert isinstance(script, str)

    state_machine = re.search(
        r"async function mergePendingCandidate\(pendingCommentId\) \{(?P<body>.*?)\n\s*\}\n\n\s*async function updateResultMarker",
        script,
        flags=re.DOTALL,
    )
    assert state_machine is not None
    body = state_machine.group("body")
    update_failure = re.search(
        r"await updateResultMarker\(pendingCommentId, (?P<state>\w+)\);\n\s*failClosed\(",
        body,
    )
    assert update_failure is not None


@pytest.mark.parametrize(
    "label",
    (
        "prod 모델 계약(`autoresearch/feature_engineering/model_contract.py`) 수정을 허용한다",
        "prod 모델 계약(`src/features/model_contract.py`) 수정을 허용한다",
    ),
)
def test_prod_model_contract_scope_accepts_both_path_spellings(label: str) -> None:
    """#754 재배치 전후 라벨 문자열을 모두 같은 scope 로 해석해야 한다.

    알 수 없는 라벨은 fail-closed 로 거부된다 — 한쪽만 받으면 그 이슈가 통째로 반려된다.
    DB에 봉인된 진행 중 실험의 이슈 본문은 여전히 옛 문자열을 담고 있으므로, 템플릿만
    바꾸고 여기를 안 바꾸면(또는 그 반대면) 실험이 죽는다.
    """
    parsed = parse_issue_input(
        451, "[AR] metric", structured_body(allowed_scope=f"- [x] {label}")
    )

    assert parsed.allowed_scope == ("prod_model_contract",)


def test_issue_form_labels_are_all_known_scopes() -> None:
    """Issue Form 의 checkbox 문구가 `_SCOPE_LABELS` 에 전부 있어야 한다(#754).

    이 둘은 **정확 일치 계약**이다. 알 수 없는 라벨은 fail-closed 로 거부되므로, 템플릿
    문구를 고치면서 매핑을 빠뜨리면 그 순간부터 새 실험이 통째로 반려된다. 증상이
    "이슈 발행은 되는데 실험이 시작되지 않는다"라 원인을 찾기 어렵다.

    반대 방향(매핑에만 있고 템플릿에 없는 항목)은 검사하지 않는다 — 전환 기간 동안
    봉인된 옛 이슈 본문을 받기 위해 일부러 남기기 때문이다.
    """
    repository_root = Path(__file__).resolve().parents[1]
    template = repository_root / ".github" / "ISSUE_TEMPLATE" / "auto_research.yml"
    form = yaml.safe_load(template.read_text(encoding="utf-8"))

    # 파일 전체에서 `- label:` 을 긁으면 나중에 "제출 전 확인" 같은 checkboxes 필드가
    # 하나만 추가돼도 그 라벨까지 scope 매핑을 요구하게 된다. allowed_scope 필드만 본다.
    fields = [
        field
        for field in form["body"]
        if field.get("type") == "checkboxes" and field.get("id") == "allowed_scope"
    ]
    assert len(fields) == 1, "allowed_scope checkboxes 필드를 정확히 하나 찾지 못했다"
    labels = [option["label"] for option in fields[0]["attributes"]["options"]]

    assert labels, "allowed_scope 에서 checkbox 라벨을 하나도 찾지 못했다 — 형식이 바뀌었는지 확인하라"
    unknown = [label for label in labels if label.strip() not in issue_branch._SCOPE_LABELS]
    assert unknown == [], f"Issue Form 라벨이 _SCOPE_LABELS 에 없다: {unknown}"


def test_duplicate_scope_labels_collapse_to_one_scope() -> None:
    """전환 기간의 두 라벨이 모두 체크돼도 scope 는 하나여야 한다(#754).

    verifier 는 중복 scope 를 `allowed_scope_invalid` 로 **거부**한다
    (`executor/verifier.py:_validate_input`). 접지 않으면 두 라벨이 모두 체크된 이슈의
    candidate 가 검증 단계에서 통째로 반려된다.
    """
    body = structured_body(
        allowed_scope=(
            "- [x] prod 모델 계약(`autoresearch/feature_engineering/model_contract.py`) 수정을 허용한다\n"
            "- [x] prod 모델 계약(`src/features/model_contract.py`) 수정을 허용한다"
        )
    )

    parsed = parse_issue_input(452, "[AR] metric", body)

    assert parsed.allowed_scope == ("prod_model_contract",)
