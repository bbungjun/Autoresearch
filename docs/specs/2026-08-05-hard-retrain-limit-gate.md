# 하드 리밋 승격 게이트 배선 — 성능과 무관한 강제 재학습 (#472)

- **상태**: Proposed
- **날짜**: 2026-08-05
- **이슈**: #472
- **선행 계약**:
  - `docs/specs/2026-08-04-temporal-signal-promotion-integration.md` (#485) §4.1·§4.2 —
    `hard_retrain_limit` **값 산출 절차**를 소유한다. 이 spec은 그 값을 **배선**만 한다.
  - `docs/specs/2026-08-03-model-degradation-rolling-origin-evaluation.md` (#471/#510) —
    측정 하네스.
  - `.github/workflows/auto-research-promotion.yml` — 게이트의 유일한 호출부.

## 목적

`#485`가 산출하는 `hard_retrain_limit_days`를 근거로 **"성능과 무관하게 일정 기간이
지나면 교체한다"**를 승격 게이트에 실제로 동작시킨다. 지금 게이트는 지표만 보고
판정하므로, 모델이 조용히 늙어도 지표가 기준을 넘지 못하면 영원히 교체되지 않는다.

## 비목적

- **`hard_retrain_limit_days` 값 산출** — `#485` §4.1 소유. 이 spec은 값을 받기만 한다.
- **열화 측정 실행** — `#485`/`#514` 소유.
- **모델 alias 이동·prod 배포** — 게이트 모듈의 비책임(모듈 docstring). 호출자
  workflow와 외부 오케스트레이터가 소유한다.
- **Issue Form 필드 추가** — §3.3에서 다루듯 하드 리밋은 **가설별 기준이 아니라 운영
  정책**이라 Issue Form에 넣지 않는다.

## 1. 지금 게이트의 실제 모양 (착수 전 실측)

**`#472` 본문이 지목한 "`#461` 승격 게이트"는 `autoresearch/experiments/promotion_gate.py`다**
(126줄). 착수 전 이 파일을 읽고 확인한 사실:

| 항목 | 실제 |
| --- | --- |
| 성격 | **순수 함수 모듈** — I/O 없음, GitHub·GCP·MLflow 접근 없음 |
| 입력 | `parse_criteria(issue_body)` + `evaluate(criteria, *, primary_candidate, primary_baseline, guardrail_*)` |
| 출력 | `GateDecision(passed: bool, reason: str)` |
| 사유 | `criteria_met` / `primary_metric_below_delta` / `guardrail_metric_missing` / `guardrail_regressed` |
| **시간 개념** | **전혀 없다** — 날짜·경과일·마지막 승격 시각 어느 것도 받지 않는다 |
| 호출부 | `.github/workflows/auto-research-promotion.yml:182-193` **한 곳뿐** |

호출부는 env 변수로 값을 넘기고 `passed`/`reason`을 `GITHUB_OUTPUT`에 쓴 뒤,
`passed == 'true'`일 때만 Draft PR을 만든다(200-205행).

> **문서 정정**: `#514` plan의 "건드리지 않는 파일" 목록에 `promotion_gate.py`를
> `src/pipeline/` 아래인 것처럼 적었으나 그 경로에는 파일이 없다. 실제 경로는
> `autoresearch/model_evaluation/experiments/promotion_gate.py`다(#754 재배치 반영). `#514`가 그 파일을 건드리지 않는다는
> 사실 자체는 그대로다.

## 2. 의존 방향 — 게이트는 `degradation_eval`을 import하지 않는다

`derive_hard_retrain_limit`은 `autoresearch/model_evaluation/degradation_eval.py`에 있고, 그 모듈은
`train`(→ lightgbm)을 끌고 온다. 게이트가 그것을 import하면 **판정 경로에 ML 의존이
들어온다** — `#485` §5.3에서 같은 이유로 원시값 전달을 택한 것과 동일한 문제다.

여기서는 근거가 하나 더 있다: **`autoresearch/`는 `src/`를 import하지 않는다**
(`grep -rn "^from src|^import src" autoresearch/` → 0건). 패키지 경계가 이미 서 있고,
이 spec은 그것을 깨지 않는다.

```text
게이트가 받는 것: 원시값 (일수·불리언)
게이트가 하지 않는 것: 측정 실행, 값 산출, 시각 조회
값을 구해 넘기는 책임: 호출부 workflow
```

## 3. 입력 계약

### 3.1 게이트가 새로 받는 값

```python
def evaluate(
    criteria: PromotionCriteria,
    *,
    primary_candidate: float,
    primary_baseline: float,
    guardrail_candidate: float | None = None,
    guardrail_baseline: float | None = None,
    # 신규 (#472)
    hard_retrain_limit_days: int | None = None,
    days_since_last_promotion: int | None = None,
) -> GateDecision:
```

**둘 다 기본값 `None`이다.** 지금 호출부는 이 인자를 넘기지 않으므로, 기본값이 없으면
기존 워크플로우가 즉시 깨진다. `None`이면 하드 리밋 조건을 **평가하지 않는다**(§4.2).

### 3.2 `temporal_hold`는 게이트가 검사하지 않는다 — 호출부가 먼저 거른다

`#485` spec §4.2가 소비자 계약으로 고정한 순서가 있다:

```text
1. evaluate_temporal_hold(result)를 먼저 부른다.
2. hold 사유가 있으면 limit_days는 무시한다 — 근거 없는 곡선에서 나온 값이다.
3. hold가 None일 때만 derive_hard_retrain_limit 결과를 게이트에 쓴다.
```

`derive_hard_retrain_limit`은 hold를 참조하지 않으므로, 오염된 곡선에서도 **숫자 모양이
정상인 `limit_days`가 나온다.** 게이트는 그 숫자만 받으므로 스스로 구분할 수 없다.

**따라서 hold 확인은 값을 구하는 쪽(호출부)의 책임이다.** hold가 있으면 호출부가
`hard_retrain_limit_days=None`으로 넘긴다 — 게이트는 "리밋 정보 없음"으로 취급하고
지표 조건만 본다. 이것이 fail-closed 방향이다: 근거 없는 곡선으로 **승격을 늘리지**
않는다.

### 3.3 하드 리밋은 Issue Form에 넣지 않는다

`PromotionCriteria`는 Issue Form 본문에서 파싱된다(`parse_criteria`). 그런데 하드
리밋은 **가설마다 다른 기준이 아니라 운영 정책**이다 — "이 실험은 3일, 저 실험은 7일"이
아니라 "이 모델군은 N일이 지나면 교체한다"이다.

Issue Form에 넣으면 실험 작성자가 매번 값을 적어야 하고, 값이 실측으로 갱신될 때마다
(§6) 과거 이슈 본문과 어긋난다. 그래서 **호출부가 정책값으로 주입**한다.

## 4. 판정 규칙

### 4.1 OR 조건

`#472` 작업 범위: *"지표 통과 조건 외에 마지막 승격 이후 N일 경과 조건을 추가 — 둘 중
하나만 만족해도 승격 후보가 되도록"*.

```text
passed = (지표 기준 통과) OR (하드 리밋 도달)
```

**순서가 중요하다.** 지표 조건을 **먼저** 평가한다. 둘 다 만족하면 사유는
`criteria_met`이다 — "지표로 통과했는데 마침 기한도 지났다"를 "기한 때문에 통과했다"로
기록하면 나중에 승격 이력을 읽을 때 모델 품질을 과소평가하게 된다.

### 4.2 하드 리밋 조건이 성립하는 경우

```text
hard_retrain_limit_days is not None
  AND days_since_last_promotion is not None
  AND days_since_last_promotion >= hard_retrain_limit_days
```

**둘 중 하나라도 `None`이면 조건을 평가하지 않는다.** 관측되지 않은 것을 "기한이 안
지났다"로도 "지났다"로도 바꾸지 않는다(`#485` §4.1과 같은 결).

### 4.3 사유 코드 — 승격 경로를 구분한다

`#472` 완료 조건: *"승격 사유(지표 통과 vs 하드 리밋)가 결과에 구분되어 기록됩니다."*

| `passed` | `reason` | 의미 |
| --- | --- | --- |
| `True` | `criteria_met` | 지표 기준 통과(기한 도달 여부와 무관) |
| `True` | **`hard_retrain_limit_reached`** | **지표는 미달인데 기한이 지나 강제 후보** |
| `False` | `primary_metric_below_delta` | 지표 미달 + 기한 미도달 |
| `False` | `guardrail_metric_missing` / `guardrail_regressed` | 기존 그대로 |

`hard_retrain_limit_reached`로 통과한 후보는 **지표상 개선이 없다.** Draft PR 본문에
그 사실이 드러나야 리뷰어가 "왜 이게 올라왔지"를 되묻지 않는다 — 호출부가
`GATE_REASON`을 이미 PR 생성 단계에 넘기고 있으므로(workflow 199-205행) 그 값으로
분기하면 된다.

### 4.4 하한이 없다 — **정책을 켜기 전에 반드시 해결해야 한다** (PR #540 리뷰)

§4.2의 조건은 `primary_delta < minimum_primary_delta`이기만 하면 성립한다. 즉
**"정체"(delta≈0)와 "대폭 악화"(예: 0.778 → 0.400)가 구분되지 않는다.** 기한만 지나면
주 지표가 얼마나 나빠졌든 `hard_retrain_limit_reached`로 통과한다.

`#472` 본문은 *"성능이 좋든 안 좋든 그냥 나가야 함"*이라고 적었고, 시간 기반 재학습의
논리 자체가 "옛 test set 점수가 낮아도 최신 데이터로 학습한 모델이 지금 트래픽에서는
낫다"이므로 **악화 후보를 올리는 것 자체는 의도된 방향이다.** 다만 그 논리는 **하한이
있을 때만** 성립한다.

**하한이 실제로 없는 조합이 있다.** Issue Form에서 guardrail을 `없음`으로 선언하면
`_guardrail_failure`가 즉시 `None`을 돌려주므로(`criteria.guardrail_name is None`),
그 가설에서는 하드 리밋이 켜진 순간 **아무 하한도 없이** 승격된다. §4.5가 말하는
"guardrail이 방어선"이 그 경우엔 존재하지 않는다.

**이 spec은 하한을 지금 확정하지 않는다.** 어떤 형태여야 하는지가 정책 판단이기
때문이다 — 후보는 최소 셋이다.

| 안 | 내용 | 문제 |
| --- | --- | --- |
| 하한 없음(현재) | 기한만 보면 통과 | guardrail 미선언 가설에서 무제한 |
| `primary_delta >= 0` 요구 | 악화는 교체하지 않음 | "성능과 무관하게"라는 취지를 좁힌다 |
| 별도 최대 악화폭 선언 | Issue Form 또는 정책 상수에 추가 | 입력 계약이 늘어난다 |

**정책값(`HARD_RETRAIN_LIMIT_DAYS`)이 비어 있는 동안 이 경로는 실행되지 않으므로
현재 위험은 잠재적이다.** 값을 넣어 정책을 켜기 전에 위 셋 중 하나를 확정한다 —
`docs/guides/retraining-policy.md`의 활성화 절차에 선행 조건으로 넣었다.

현재 동작(하한 없음)은 **테스트로 고정**해 뒀다. 나중에 하한을 넣을 때 그 테스트가
빨간불이 되어 "의도적으로 바꾸는 중"임을 드러낸다.

### 4.5 guardrail은 하드 리밋으로 우회되지 않는다

**하드 리밋이 도달해도 guardrail 악화는 통과시키지 않는다.** 하드 리밋의 취지는 "성능이
정체돼도 교체한다"이지 "망가진 모델도 올린다"가 아니다. guardrail은 **안 망가졌다는
최소 보증**이므로, 그것까지 우회하면 게이트가 사실상 무력해진다.

```text
guardrail 위반 → 하드 리밋 도달 여부와 무관하게 passed=False
```

## 5. 정책 버전

`#472` 작업 범위: *"열화 시점 재측정 시 하드 리밋 값이 바뀔 수 있으므로 정책에 버전
필드 부여"*.

`GateDecision`에 `policy_version: str`을 더한다. 값이 왜 바뀌는지는 §6이 다룬다 —
같은 코드가 다른 날 다른 판정을 낼 수 있으므로, **어떤 정책으로 판정했는지**가 결과에
남아야 승격 이력을 나중에 해석할 수 있다.

기존 `promotion_evidence.PROMOTION_POLICY_VERSION`("promotion-policy-v1")과는 **다른
축**이다 — 그쪽은 통계 판정 정책(`experiment_evaluation`)이고 이쪽은 게이트 정책이다.
이름이 겹치지 않게 `gate-policy-v1`로 시작한다.

## 6. "마지막 승격 시각"의 출처와 한계 — **미해결**

`days_since_last_promotion`을 구하려면 "마지막으로 승격된 시각"이 필요한데,
**그것을 직접 기록하는 곳이 없다.**

- `set_model_alias(model_name, alias, version)`(`autoresearch/model_registry/registry.py:121`)은
  **언제 붙였는지 남기지 않는다.**
- 사용 가능한 근사치는 champion alias가 가리키는 **버전의 `creation_timestamp`**
  (`registry.py:81,90`)뿐이다.

**근사치의 방향**: 버전 생성은 alias 부여보다 **이르거나 같다**. 따라서 이 값으로 계산한
경과일은 실제보다 **크거나 같고**, 하드 리밋이 **더 일찍** 발동한다. 재학습을 덜
하는 게 아니라 더 하는 쪽이므로 모델 신선도 관점에서는 안전한 방향이지만, **정확한
값은 아니다.**

**확정: 안 A(버전 `creation_timestamp` 근사)** — 2026-08-05.

- **안 A(채택)**: 버전 `creation_timestamp`를 쓴다. 추가 구현 없음. 위 오차를 감수한다.
- **안 B(보류)**: alias 부여 시각을 기록하는 경로를 만든다. `set_model_alias` 호출부
  또는 registry 태그에 남긴다. **`autoresearch/model_registry/` 계약까지 변경 범위가 확장된다.** → §8.4

**채택 사유**: 안 A의 오차는 "여러 버전을 미리 등록해두고 나중에 alias만 옮기는" 운영
관행이 있을 때 커진다. 그런데 Auto Research 승격 루프는 이제 막 동작하기 시작했고
(`#448` 최근 close, `#461` 게이트도 최근 착지), **그런 관행이 쌓일 시간 자체가 없었다.**
따라서 발표 시점까지 creation과 alias 부여의 시차가 며칠씩 벌어질 구조가 아니다.

안 B를 지금 하면 `autoresearch/model_registry/registry.py`의 모델 레지스트리 계약까지
변경하는데, 그 비용을 치를 만큼 지금 오차가 크지 않다.

### 6.1 근사라는 사실을 어디에 남기는가 — 게이트가 아니다

**`policy_version`에 `-approx` 같은 suffix를 붙이지 않는다.** 게이트는
`days_since_last_promotion`을 **그냥 정수로 받고 그 값이 어떻게 구해졌는지 모른다**
(§2의 의존 방향). 근사 여부를 게이트 정책 버전에 박으면 **게이트가 알 수 없는 것을
단언**하게 되고, 나중에 안 B로 바꿔도 게이트 코드는 바뀔 게 없는데 버전만 바뀌는
모순이 생긴다.

근사는 **값을 구하는 쪽의 성질**이므로 거기에 남긴다:

- 호출부(workflow)가 산출 근거를 함께 남긴다.
- `hard_retrain_limit_reached`로 통과한 Draft PR 본문에 **"경과일은 champion 버전
  생성 시각 기준 근사치"**를 명시한다(§4.3의 사유 표시와 같은 자리).

이렇게 하면 나중에 이 값을 신뢰도 100%로 오해하는 사람이 없고, 안 B로 전환할 때
바뀌는 것이 호출부 한 곳으로 국한된다.

## 7. `#485` §4.2 표의 빈칸 — 이 spec이 메운다

`#485` 실측(2026-08-04)에서 발견한 항목이다. `derive_hard_retrain_limit`은
`safety_margin_days == degradation_point.elapsed_days`일 때 뺄셈이 음수가 아니라 정확히
0이라 clamp 분기를 타지 않는다(`degradation_eval.py:593`). 그래서 나오는 조합:

```text
limit_days = 0,  reason = None
```

`#485` spec §4.2의 표는 마지막 행이 "**양수** | `None` | 정상 산출"이라 이 상태가 없다.
표대로 구현한 소비자에게는 미처리 케이스다.

**이 spec의 처리**: `limit_days=0`은 `days_since_last_promotion >= 0`이 항상 참이므로
**즉시 하드 리밋 도달**로 판정된다. 이는 의미상 맞다("이미 재학습 시점을 지났다").
`reason`이 `None`이든 `safety_margin_exceeds_degradation_point`든 게이트 판정은 같다 —
게이트는 `limit_days` 숫자만 받기 때문이다(§3.1).

**따라서 §4.2 표는 "0 이상 | `None` | 정상 산출"로 넓히면 해소된다.** 이 spec이 그
문서를 고치지는 않는다(`#485`는 닫혔다) — 여기 기록해 두고, 값 산출 쪽을 다시 열게 되면
그때 반영한다.

## 8. 미해결 항목

### 8.1 `#461` 게이트 변경 확인 — **2026-08-05 완료**

`autoresearch/experiments/promotion_gate.py`는 `#461`에서 도입된 코드다. `#472`의
assignee 정보만으로 이 코드의 변경 승인 여부를 판단하지 않고, `#493`에서 발생했던
같은 혼동을 피하기 위해 계약 변경을 별도로 확인했다.

확인한 항목:
1. `evaluate()`에 OR 조건과 신규 인자 2개를 더하는 방향이 맞는지.
2. `GateDecision`에 `policy_version`을 더해도 되는지 — 호출부 workflow가 그 필드를
   읽지 않으므로 하위호환이지만, 계약 변경은 별도 판단이 필요했다.
3. §6의 안 A/안 B 중 어느 쪽인지 — 안 B는 `autoresearch/model_registry/registry.py`를 건드린다.

**확인 결과(2026-08-05)**: 승인이 기록됐다 —
`pull/461#issuecomment-5186975872`에 `#472` 관련 변경 승인이 남아 있다.

1·2번은 예/아니오라 이 코멘트로 답이 된다. **3번은 이 spec이 §6에서 안 A로 확정했다** —
추가 확인하지 않는다. 근거는 §6에 적었고, 요지는 "안 A의 오차를 키우는 운영
관행이 쌓일 시간 자체가 없었다"이다. 안 B는 §8.4로 백로그화했으므로 되돌아올 길이
열려 있다.

> 구두 확인은 근거로 적지 않는다(`#485` §7.1). 위 링크는 GitHub에 남은 기록이라 그
> 조건을 만족한다. 다만 **무엇이 승인됐는지**까지는 담고 있지 않으므로, 이 spec이
> 스스로 정한 §6의 결정은 그 코멘트가 아니라 **§6에 적힌 사유**를 근거로 삼는다.

### 8.4 안 B 백로그 — alias 부여 시각 기록

§6이 안 A(근사)를 택한 것은 **지금 오차가 작기 때문**이지 근사가 옳기 때문이 아니다.
다음 중 하나라도 관측되면 안 B로 전환한다:

- 버전을 여러 개 미리 등록해두고 나중에 골라 alias를 옮기는 운영이 자리잡을 때
- `hard_retrain_limit_reached`가 **거의 항상** 발동해 지표 조건이 사실상 무력해질 때

전환 내용: `set_model_alias`(`autoresearch/model_registry/registry.py:121`) 경로에서 alias 부여 시각을
기록한다(registry 태그 또는 별도 레코드). **모델 레지스트리 계약이 함께 바뀌므로 별도
이슈에서 영향 범위를 검증한다.** 전환해도 게이트 코드는 바뀌지 않는다 — 값을 구하는
쪽만 바뀐다(§6.1).

### 8.2 호출부가 값을 어디서 구하는가 — plan에서 확정

게이트는 원시값만 받으므로(§2), workflow가 다음을 구해야 한다:

- `hard_retrain_limit_days` — `#485` 측정 결과 JSON에서. 그 JSON을 CI가 어떻게 얻는지
  (아티팩트? GCS? 정책 상수로 고정?)는 **아직 정하지 않았다.**
- `days_since_last_promotion` — §6의 안 A/안 B에 따라 달라진다.

**값 조달 경로가 이 이슈에서 가장 불확실한 부분이다.** 최악의 경우 "정책 상수로 고정하고
실측으로 주기적으로 갱신"이 현실적인 1차 형태일 수 있다 — 그 판단은 plan에서 한다.

### 8.3 하드 리밋 값 자체는 아직 미확정

`#485` §7.3이 `safety_margin_days`를 확정하지 않았고, 실측(2026-08-04)은 단일 origin
관측 하나뿐이다:

```text
degradation_point = elapsed_days 7 (2026-08-03 실측)
margin=2 → limit_days=5
```

**이 값을 정책 상수로 굳히려면 다중 origin 관측이 필요하다**(`#485` §4.1). 이 spec은
게이트 **배선**을 정의하고, 값 확정은 여전히 `#485` 계열 후속이다. 배선이 먼저 있어도
값이 `None`이면 게이트는 기존과 동일하게 동작하므로(§4.2) 순서상 문제가 없다.

## 9. 구현 순서 (plan에서 상세화)

1. `evaluate()`에 인자 2개 추가 + OR 조건 + `hard_retrain_limit_reached` 사유
   (기본값 `None`으로 기존 호출부 무변경)
2. guardrail 우회 금지 규칙(§4.5) 고정
3. `GateDecision.policy_version`
4. workflow 배선 — §8.2 확정 후
5. Draft PR 본문에 승격 사유 표시(§4.3)
6. 운영 정책 문서에 하드 리밋 정책 명시(`#472` 작업 범위)
   → **`docs/guides/retraining-policy.md`**로 착지. `#472` 본문은 "스킬 문서"를
   지목했으나 이 저장소에 그런 문서가 없다(`.claude/`에는 `docs/`만 있고
   `.claude/skills/`도, 그 이름의 파일도 없다). 저장소 밖 문서를 가리킨 표현으로
   보이며, 운영 계약은 `docs/guides/`에 두는 것이 이 저장소 관례다
   (`training-experiment-provenance.md`가 같은 성격).

1~3은 순수 함수 변경이라 테스트만으로 검증된다. 4~6은 CI·문서 영역이며 §8.1 확인이
선행돼야 한다.
