# Sealed Judge — 변동 없는 guardrail의 σ 처리와 판정 정책 버전 고정

> 작성: 2026-10-20 | 개정: rev 3 (호환성·통합 검증·사전 차단 보완) | 상태: **1차 공통 경로 구현 완료 — 목표 모드·재실행 후속** | 추적: #148 / Draft PR #149
>
> 관련 근거: #142(slate 백분위 후보 invalid), #147(interest_peak_gap 후보 invalid), #144(learning 자동 검증)
>
> 현재 계약 코드: `autoresearch/research_harness/judge_decision.py`,
> `autoresearch/research_harness/report.py` `_decision_policy`,
> `autoresearch/research_harness/run_inputs.py`, `controller.py`, `domain.py`, `ledger.py`

## 0. 요약

현재 Sealed Judge는 7개 지표 **모두**의 baseline σ가 `1e-6`보다 커야 confirmation을
판정합니다. #147 profile에서는 baseline Recall@10이 calibration 5개 seed 모두 1.0이어서
σ=0이었고, 그래서 screening을 통과한 후보는 품질과 상관없이 `invalid /
insufficient_baseline_noise`로 끝났습니다. 이 spec은 다음을 제안합니다.

1. σ 유효성 조건(`σ > 1e-6`)은 **primary(NDCG@10)에만** 필수로 요구합니다.
2. σ가 `1e-6` 이하인 guardrail은 **관측 변동 없음(zero observed variance)**으로 분류하고,
   허용 폭 0의 비열등성(평균 Δ ≥ 0)으로 판정합니다. 지표가 상한·하한에 붙어 있는지는
   별도 표시(`at_bound`)로 기록합니다.
3. 판정 정책에 버전(`v1`/`v2`)을 두고 **실행 시작 시점에 고정**합니다. 판정, 재개,
   보고서 재생성 모두 그 고정 버전을 따르며, v1 판정 경로와 기존 입력 파일·SHA를 보존합니다.
4. 기존 profile은 수정하지 않습니다. #147 재검증은 v2에 결합된 **새 profile**로 사전 등록해
   실행합니다.
5. profile 준비 단계에서 변동 없는 지표를 진단합니다. 채택 판정이 불가능한 **새 성능 탐색
   실행은 시작 전에 차단**하고, 복구·진단 목적의 실행은 별도 조건으로 구분합니다.
6. (후속) σ 추정 방식과 변별력 있는 지표는 별도 spec에서 다룹니다.

## 1. 배경과 문제

### 1.1 현재 판정 규칙 (v1, 코드 기준)

| 단계 | 규칙 | 위치 |
|---|---|---|
| screening | seed 42 한 쌍에서 primary Δ > 0이면 confirmation으로 진행 | `screen_candidate` |
| 유효성 | 7개 지표 σ가 모두 유한하고 `> 1e-6`이어야 함. 아니면 `insufficient_baseline_noise`, decision 없음 | `_validated_sigmas` |
| primary | 5쌍 평균 Δ(NDCG@10) ≥ 2σ가 아니면 `discard / primary_threshold_not_met` | `compare_confirmation` |
| guardrail | 나머지 6개 중 하나라도 평균 Δ < −σ이면 `revise / guardrail_regression` | 같음 |
| 채택 | 그 외 `promote / promotion_threshold_met` | 같음 |

Δ는 개선이 양수가 되도록 방향을 맞춘 값입니다(LogLoss·Brier는 기준−후보). σ로 나누지
않습니다. **guardrail에서 σ의 역할은 "얼마까지 나빠져도 잡음으로 보느냐"를 정하는 허용 폭뿐**입니다.

### 1.2 관측된 사실

#147의 고정 `profile.json`에 기록된 baseline σ와 조건:

| 지표 | 역할 | σ |
|---|---|---:|
| NDCG@10 | primary | 0.021501 |
| NDCG@24 | guardrail | 0.021501 |
| Recall@10 | guardrail | **0.000000** |
| grouped ROC-AUC | guardrail | 0.004190 |
| PR-AUC | guardrail | 0.000821 |
| LogLoss | guardrail | 0.003167 |
| Brier | guardrail | 0.0000377 |

- 같은 profile에는 "공식 primary 2σ·guardrail −1σ·coverage·σ 유효성 정책을 유지한다 …
  epsilon·데이터·threshold를 바꾸지 않는다"는 고정 조건이 명시돼 있습니다
  (calibration receipt SHA256 `e36ebf5479e4e941326251ccc46dd0cbb089950eefe79fdb4cfda8bc61e3cd56`).
- validation은 160 slate × 24 후보이고 slate마다 클릭이 1개입니다. 합성 로그가 slate별
  click_propensity 1위만 클릭으로 정하기 때문입니다
  (`action_log_generation/pipeline.py` `select_clicks_per_slate`).
- calibration 5개 seed의 baseline Recall@10은 모두 1.0입니다. **#147에서는 σ=0이 곧 상한
  포화였습니다.** 다만 σ=0은 일반적으로 "관측한 seed 사이에서 값이 같았다"는 뜻일 뿐이고,
  상한 포화를 뜻하지는 않습니다(§3 D2).
- 그 결과 #142①과 #147은 같은 사유로 invalid였습니다. #147은 확인 5쌍 모두 NDCG@10이
  개선됐습니다(평균 Δ +0.064002).

### 1.3 왜 문제인가

- **채택 경로가 구조적으로 닫혀 있습니다.** 현재 profile에서 가능한 결과는
  `discard`(screening 탈락)와 `invalid`(screening 통과) 둘뿐입니다.
- **invalid는 정보가 없습니다.** 후보의 좋고 나쁨을 말해 주지 않는데, confirmation
  10fit은 이미 사용한 뒤입니다.
- **모순이 있습니다.** #147에서 σ=0인 Recall@10의 후보 Δ는 0이었습니다. 지표가 전혀
  나빠지지 않았는데도 그 지표 때문에 판정이 막혔습니다.

## 2. 목표와 비목표

**목표**

- 변동 없는 guardrail이 있어도 primary 근거가 유효하면 판정할 수 있게 합니다.
- 변동 없는 지표에서도 "나빠지면 막는다"는 guardrail의 안전 기능은 유지합니다.
- 변동 없음과 상한·하한 포화를 구분해 결과·보고서에 남깁니다.
- 한 실행의 판정·재개·보고서가 항상 같은 정책 버전을 따르도록 합니다.
- 판정이 불가능한 새 성능 탐색 실행은 Agent 호출·학습 예산을 쓰기 전에 차단합니다.

**비목표**

- primary 판정 기준(2σ, 5쌍 평균)과 `1e-6` 유효성 경계값은 바꾸지 않습니다.
- calibration이 σ=0을 다른 값으로 바꾸지 않습니다
  (`test_calibration_does_not_replace_zero_sigma` 원칙 유지).
- 기존 profile, 과거 ledger, 과거 판정은 수정하지 않습니다.
- final의 데이터 접근·단일 소비 규칙은 바꾸지 않습니다. 성능 탐색의 validation·final은
  같은 고정 정책 버전으로 판정하며, 새 진단 실행은 final을 자동 소비하지 않습니다.
- 합성 데이터 생성기와 slate 구성은 바꾸지 않습니다(§9 후속).

## 3. 설계 결정

### D1. σ 유효성 조건은 primary에만 필수로 둡니다 (v2)

| 대상 | σ 상태 | v1 | v2 |
|---|---|---|---|
| primary | `σ > 1e-6` | 판정 | 판정 |
| primary | `0 ≤ σ ≤ 1e-6` | invalid | **invalid (변경 없음)** |
| guardrail | `σ > 1e-6` | 문턱 −σ | 문턱 −σ (변경 없음) |
| guardrail | `0 ≤ σ ≤ 1e-6` | invalid | **변동 없음 → 문턱 0.0 (D2)** |
| 모든 지표 | 음수·NaN·inf·비수치 | invalid / 입력 오류 | 변경 없음 |

경계는 v1과 같은 `1e-6`이며 비교 연산도 같습니다(`σ ≤ 1e-6`이면 유효 σ가 아님).
**v1과 v2의 판정이 같아지는 조건은 "7개 지표 σ가 모두 `1e-6` 초과"인 경우**입니다.
`0 < σ ≤ 1e-6`인 작은 양수 σ의 guardrail은 v1에서 invalid였고 v2에서는 판정이
진행되므로, 의도된 동작 차이입니다.

**이유:** primary의 σ는 채택 문턱(2σ)을 정합니다. σ가 0이면 "아무리 작은 개선도 채택"이
되어 위험하므로 그대로 막습니다. guardrail의 σ는 허용 폭일 뿐이라 0이어도 판정 의미가
무너지지 않습니다.

### D2. 변동 없는 guardrail은 허용 폭 0의 비열등성으로 판정하고, 포화는 따로 표시합니다

- 상태 이름은 `zero_observed_variance`입니다. 의미는 "calibration에서 관측한 seed 사이에
  유효한 변동이 없었다"입니다. 상한 포화를 뜻하지 않습니다.
- 통과 조건은 5쌍 평균 Δ ≥ 0.0입니다. 기존 `comparison_tolerance` 0.0과 같은 비교입니다.
  하나라도 미달이면 기존과 같이 `revise / guardrail_regression`입니다.
- 별도 표시 `at_bound`: calibration 원값이 모든 seed에서 지표의 이론적 경계(Recall·NDCG·
  ROC-AUC·PR-AUC는 1.0, LogLoss·Brier는 0.0)에 있으면 `true`입니다.
  - 값은 profile을 만들 때 calibration receipt의 seed별 원값으로 계산해 profile에
    저장합니다. Judge가 σ만 보고 추정하지 않습니다.
  - `at_bound`는 **판정에 영향을 주지 않는** 기록 전용 값입니다. 경계에 붙은 지표는
    퇴행만 감지할 수 있고 개선은 감지할 수 없다는 사실을 남겨, §9-1의 지표 교체 판단
    근거로 씁니다.

**이유:** σ가 0이라는 것은 baseline을 여러 번 학습해도 그 지표가 흔들리지 않았다는
뜻입니다. 관측된 하락을 잡음으로 설명할 근거가 없으므로, 가장 엄격한 허용 폭 0이 기존
규칙(−σ)을 그대로 연장한 해석입니다. 이 해석은 값이 경계에 있든 중간에 있든 같게
적용됩니다. 그래서 판정 규칙은 하나로 두고, 포화 여부는 기록으로만 구분합니다.

**검토한 대안**

| 대안 | 장점 | 기각 이유 |
|---|---|---|
| A. Recall@10을 guardrail에서 제거 | 가장 단순 | 클릭 영상을 상위 10위 밖으로 밀어내는 퇴행을 못 잡음 |
| B. σ 하한값(예: 1/160) 강제 | 기존 수식 유지 | 임의 상수이고, calibration 원칙과 충돌 |
| C. Recall@3·MRR 등으로 교체 | 변별력 증가 | 지표 계약·재calibration이 필요한 큰 변경. §9로 분리 |
| **D. 허용 폭 0 비열등성 (채택)** | 안전 기능 유지, 상수 없음, 변경 최소 | — |

### D3. 판정 정책 버전을 실행 시작 시 고정하고, v1 경로를 보존합니다

버전 기록만으로는 과거 판정이 보존되지 않습니다. 현재 `report._decision_policy()`는
보고서를 만들 때마다 **현재 코드 기준으로** 정책 설명을 생성합니다(`report.py`
`record["run"]["decision_policy"]`). 또 Judge가 하나의 경로만 가지면, v1으로 시작한 실행이
v2 코드에서 재개될 때 판정 자체가 바뀔 수 있습니다. 그래서 다음 계약을 둡니다.

1. **고정:** 실행 입력 계약(`run_inputs`의 contract, 목표 모드 실행 config)에
   `judge_policy_version: Literal["v1", "v2"]`를 둡니다. 실행을 시작할 때 값을 정해
   입력 SHA에 포함시키고, 이후에는 바꿀 수 없습니다.
2. **기존 실행:** 이 필드가 없는 기존 계약·ledger는 `v1`으로 해석합니다.
3. **판정:** `compare_confirmation`은 고정된 버전으로 분기합니다. v1 경로는 현재 코드와
   판정·reason code·Δ가 완전히 같아야 하며, v2 도입 이후에도 삭제하거나 수정하지 않습니다.
4. **재개:** 저장된 계약의 버전과 실행 요청의 버전이 다르면 재개를 거부하고 사유를
   남깁니다. 현재 코드의 기본 버전으로 대체하지 않습니다.
5. **보고서:** `_decision_policy(sigmas, version)`는 실행에 고정된 버전의 정책만
   설명합니다. 과거 실행의 보고서를 v2 코드에서 다시 만들어도 v1 설명이 나와야 합니다.
   이미 게시된 `research-record-v1`·`research-record-v2`의 기록·Markdown은 보존하고,
   새 정책 필드와 설명은 `research-record-v3`에 기록합니다. 기록 형식 버전과 Judge 정책
   버전은 별개입니다.
6. **기록:** validation confirmation과 final 판정 기록, 보고서에 `judge_policy_version`을
   남깁니다. 입력 계약·판정 반환값·저장 기록의 버전이 다르면 해당 결과를 채택하지 않고
   무결성 오류로 중단합니다.

**기존 파일·해시 호환성**

현재 `run_inputs._load()`는 계약 값뿐 아니라 재구성한 manifest와 저장된 원본 bytes도
대조합니다. 새 필드에 기본값을 추가해 다시 직렬화하면 기존 manifest와 SHA가 달라질 수
있으므로, 메모리상의 기본값 처리와 저장 형식 변경을 구분합니다.

- 기존 `run-inputs-v1`은 당시 형식으로 검증하고 읽습니다. 누락된 정책 버전은 **메모리에서만
  v1으로 해석**하며, 파일에 필드를 보충하거나 manifest를 다시 쓰지 않습니다.
- 새 실행은 `run-inputs-v2` 형식으로 정책 버전을 명시해 게시합니다. 입력 형식 버전과
  Judge 정책 버전은 별개이며, 새 형식에서도 v1 정책을 선택할 수 있습니다.
- 기존 입력을 재사용하는 freeze/load 경로는 기존 형식과 계약을 검증한 뒤 원본 manifest의
  SHA를 반환합니다. 새 형식으로 만든 manifest의 SHA를 기존 checkpoint와 비교하지 않습니다.
- 기존 ledger는 누락된 정책 버전을 v1으로 읽되, 과거 행을 다시 직렬화·수정하지 않습니다.
  새 판정 기록에는 버전을 명시하고, 기존 행의 재전송은 append 멱등성을 유지합니다.
- 입력 파일·metadata·기존 ledger 행·checkpoint·final 소비 marker의 bytes와 SHA를 보존합니다.
  재개로 새 결과가 생기면 ledger의 **새 행 추가만** 허용하며, 보고서 재생성은 기존 증거를
  수정하지 않습니다. 호환성 처리는 버전 누락만 허용하고, 다른 계약 변조 검증은 유지합니다.

**판정까지의 전달 경로**

- 실행 입력의 고정 버전을 `ControllerRunRequest`에 전달하고, Controller가 validation
  confirmation과 final 모두에서 `ResearchDomain.compare()`에 명시적으로 전달합니다.
- `ResearchDomain`의 confirmation 계약과 `YouTubeCTRDomain.compare()`가 그 값을
  `compare_confirmation()`에 전달합니다. 이 경로에서 현재 코드의 기본 버전으로 대체하지
  않습니다. screening의 primary Δ > 0 규칙은 유지합니다.
- σ·coverage·입력 오류로 decision이 없는 경우에도 `ConfirmationDecision`은 요청된 정책
  버전을 보존합니다. 오류 반환 경로가 기본값 v1으로 되돌아가면 안 됩니다.
- Controller는 판정 결과의 버전과 변동 없는 guardrail 목록을 validation·final의
  `TrialRecord`에 보존합니다. 보고서는 이를 고정 입력과 대조하고 해당 버전의 설명을 만듭니다.
- final은 validation에서 고정한 버전으로 판정합니다. final을 재소비하거나 정책 버전을
  바꾸어 같은 결과를 다시 채택하는 경로는 추가하지 않습니다.

**이유:** 판정 기준을 바꾸는 변경에서 가장 위험한 것은 과거 결과나 진행 중인 실행이
조용히 새 기준으로 다시 해석되는 일입니다. 버전을 입력 계약에 묶어야 재개와 보고서
재생성까지 같은 기준을 강제할 수 있습니다.

### D4. 변동 없음 상태를 판정 결과와 보고서에 기록합니다

- `ConfirmationDecision`에 다음 필드를 추가합니다. 기본값으로 기존 생성 코드와 호환됩니다.
  v1에서는 목록이 비어 있고 버전은 `v1`이며, v2에서는 decision 유무와 관계없이 `v2`입니다.
  - `zero_variance_guardrails: tuple[JudgeMetric, ...] = ()`
  - `judge_policy_version: str = "v1"`
- `TrialRecord`에도 두 필드를 보존합니다. ledger 파서는 두 필드를 선택 필드로 추가해 기존
  행을 읽고, 새 행에는 실제 판정값을 기록합니다. 기존 필수 필드·알 수 없는 필드 검증과
  멱등성 검사는 유지합니다.
- v2 보고서의 지표별 threshold에는 `status: "zero_observed_variance"`, `threshold: 0.0`,
  `basis: "zero_tolerance_non_inferiority"`, `at_bound: true|false`를 표시합니다.
  1차 공통 경로에는 calibration 원값을 전달하는 profile 계약이 없으므로 `at_bound`는
  `null`과 원값 미제공 사유를 기록합니다. 목표 모드 연결 후 원값에서 true/false를 계산하며,
  σ만 보고 포화 여부를 추정하지 않습니다.
- `JudgeReasonCode`는 추가하지 않습니다. 변동 없음은 decision의 사유가 아니라 지표 상태이며,
  ledger·Controller의 분기 계약은 그대로입니다.

**이유:** 변동 없는 지표를 조용히 통과시키면 "이 지표는 지금 아무것도 측정하지 못한다"는
사실이 가려집니다. 판정은 진행하되 그 사실은 드러나야 합니다.

### D5. 새 성능 탐색은 판정 가능성을 먼저 검사하고, 복구·진단 실행과 구분합니다

목표 모드의 새 실행 preflight에서 profile과 고정 정책 버전으로 판정 가능성을 검사합니다.
profile에 결합된 실행 config에 **새 필드**
`execution_purpose: Literal["model_selection", "diagnostic"]`를 제안합니다. 새 실행의
기본값은 `model_selection`이며, 목적도 실행 입력 identity에 포함해 시작 후 변경할 수 없습니다.

| σ 상태 | 새 성능 탐색 (`model_selection`) | 명시적 복구·진단 (`diagnostic`) |
|---|---|---|
| 누락·음수·NaN·inf·비수치 | 입력 오류로 차단 | 동일하게 차단; 예외 불가 |
| primary `0 ≤ σ ≤ 1e-6` (v1/v2) | 판정 불가로 차단 | 아래 조건을 충족하면 진단만 진행 |
| guardrail `0 ≤ σ ≤ 1e-6` (v1) | confirmation 무효가 확정되므로 차단 | 아래 조건을 충족하면 진단만 진행 |
| guardrail `0 ≤ σ ≤ 1e-6` (v2) | 변동 없음·`at_bound` 진단을 남기고 진행 | 같은 고정 v2 규칙으로 진단 |
| 모든 σ `> 1e-6` | 진행 | 진단 목적과 예산에 따라 진행 |

- 차단 시 대기 상태와 문제 지표·σ·정책 버전·사유를 남깁니다. Agent 호출, screening,
  confirmation, final을 시작하지 않으며 이 단계의 fit·Agent 호출 예산 사용은 0입니다.
  이미 완료한 calibration 비용은 별도 기록하며, 이를 예산 미사용으로 표현하지 않습니다.
- `diagnostic`은 실행 전에 복구·진단 목적, 예상되는 판정 불가 사유(해당 시), 검증 항목과
  예산을 고정한 새 실행에만 적용합니다. Agent가 실행 중 목적을 바꾸어 차단을 우회할 수 없습니다.
- 새 진단 실행은 모델 채택·champion 교체·final 자동 소비를 하지 않습니다. 실제 Judge 결과는
  원래 정책대로 보존하되, 복구 성공이나 점수 변화를 성능 채택으로 해석하지 않습니다.
- 기존 실행의 재개는 새 실행이 아닙니다. 새 필드 누락을 이유로 과거 입력을 수정하거나
  기존 재개 동작을 바꾸지 않습니다. 저장된 정책을 유지하고, 알려진 판정 불가 상태를
  진단에 표시합니다. 보고서만 재생성하는 경우에는 실행 preflight를 수행하지 않습니다.

**이유:** 지금은 confirmation 10fit을 다 쓴 뒤에야 invalid를 알게 됩니다. 그 사실은
calibration 직후에 이미 정해져 있습니다. #147의 ResearchAgent는 공개된 σ=0을 원인으로
연결하지 못했으므로, 명시적 진단이 Agent 근거로도 필요합니다.

### D6. 기존 profile은 보존하고, #147 재검증은 v2 새 profile로 사전 등록합니다

#147 profile은 "σ 유효성 정책 유지·threshold 변경 금지"를 고정 조건으로 명시합니다. 같은
profile로 v2 판정을 하면 그 고정 조건을 위반하므로, 다음 순서를 따릅니다.

1. v2 규칙(D1~D5)을 이 spec으로 확정하고, 구현·테스트를 main에 병합합니다.
2. **새 profile**을 만듭니다.
   - `judge_policy_version: "v2"`와 v2 구현 코드 SHA에 결합합니다.
   - 데이터·임베딩·모델 설정은 #147 profile과 같게 둡니다.
   - σ는 기존 calibration receipt(SHA256 `e36ebf54…`)를 SHA로 참조해 재사용합니다. 새로
     fit하지 않습니다. `at_bound`는 그 receipt의 원값으로 계산합니다.
   - 고정 조건 문구는 v2 정책 기준으로 다시 씁니다.
3. 기존 #147 profile·ledger·보고서는 수정하지 않고, v1 판정 기록으로 보존합니다.
4. 새 profile로 #147 후보 재실행을 **새 trial로 사전 등록**합니다. 예산과 수용 기준도
   함께 고정합니다.
5. screening·confirmation을 실행합니다. v2에서 promote가 나오면 기존 단일 소비 경로로
   final을 1회 실행합니다.

**이유:** 이 규칙은 #147 결과를 본 뒤에 설계했습니다. §4처럼 v2를 #147에 적용하면
promote가 나옵니다. 결과를 보고 규칙을 바꿔 그 결과를 채택하면 "결과에 맞춰 기준을 고친
것"이 됩니다. 같은 데이터의 재실행은 대부분 같은 결과를 재현하므로 새 증거로서의 힘이
약합니다. **실질적인 독립 검증은 5단계의 final**이며, 보고서에 이 점을 명시합니다.

## 4. 설명용 시뮬레이션 — #147 수치에 v2를 적용하면

공식 재판정이 아닙니다. 저장된 5쌍 평균과 #147 profile σ로 계산한 설명용 값이며, D6 때문에
이 값으로 채택하지 않습니다.

| 지표 | 평균 Δ | v2 문턱 | 상태 | 결과 |
|---|---:|---:|---|---|
| NDCG@10 (primary) | +0.064002 | ≥ 2σ = 0.043002 | 유효 σ | 통과 |
| Recall@10 | 0.000000 | ≥ 0.0 | zero_observed_variance, at_bound=true | 통과 |
| NDCG@24 | +0.064002 | ≥ −0.021501 | 유효 σ | 통과 |
| grouped ROC-AUC | +0.010489 | ≥ −0.004190 | 유효 σ | 통과 |
| PR-AUC | +0.002377 | ≥ −0.000821 | 유효 σ | 통과 |
| LogLoss | +0.017726 | ≥ −0.003167 | 유효 σ | 통과 |
| Brier | +0.000439 | ≥ −0.0000377 | 유효 σ | 통과 |

→ v2에서는 `promote / promotion_threshold_met`이 됩니다. #142①은 confirmation guardrail
평균을 이번에 확인하지 않았으므로 제외했습니다.

## 5. 동작 계약 변경

| 대상 | 변경 | 비고 |
|---|---|---|
| `run_inputs` contract / 목표 모드 실행 config | 새 실행에 `judge_policy_version` 명시, 입력 SHA에 포함 | 기존 `run-inputs-v1` 원본 보존, 새 실행은 `run-inputs-v2` |
| `ControllerRunRequest` / Controller | 고정 버전을 validation confirmation·final의 Domain 호출에 전달하고 반환 버전 대조 | 새 판정 기록에도 보존 |
| `ResearchDomain.compare` / `YouTubeCTRDomain.compare` | confirmation 경로에 정책 버전을 전달 | 오류 반환도 요청 버전 유지, screening 규칙 유지 |
| `judge_decision._validated_sigmas` | 버전 인자 추가. v1은 현행 그대로, v2는 D1 표대로 분류 | v1 경로 동치 보장 |
| `judge_decision.compare_confirmation` | 고정 버전으로 분기. v2는 변동 없는 guardrail 문턱 0.0 | 판정 순서(primary → guardrail) 유지 |
| `ConfirmationDecision` | `zero_variance_guardrails`, `judge_policy_version` 추가 | 기본값으로 하위 호환 |
| `TrialRecord` / ledger 파서 | 버전·변동 없는 guardrail 목록 저장, 기존 행은 선택 필드로 읽기 | 과거 행 bytes·멱등성 보존 |
| `report._decision_policy` | 버전 인자를 받아 그 버전의 정책만 설명. v2에 상태·`at_bound` 표시 | 설명 전용 원칙 유지 |
| 재개·freeze/load 경로 | 저장된 입력 형식으로 검증, 버전 불일치 거부 | 원본 manifest SHA·checkpoint 참조 보존 |
| profile 모델 | `at_bound` 진단값, `judge_policy_version` 결합 | **목표 모드 코드는 아직 main에 미병합**(§8) |
| 목표 모드 실행 config / preflight | 실행 목적 고정, 새 성능 탐색의 판정 불가 차단·진단 예외 | D5, 기존 재개·보고서 재생성과 구분 |
| screening | 변경 없음 | — |

## 6. 테스트 계획

**v1 보존**

- 기존 `judge_decision` 테스트 전체가 `v1` 명시 실행에서 그대로 통과합니다.
- 버전 필드가 없는 기존 계약은 `v1`으로 판정됩니다.
- **7개 지표 σ가 모두 `1e-6` 초과**인 입력에서 v1과 v2의 decision·reason code·Δ가
  완전히 같습니다(동치 테스트).

**σ 경계값** (v1, v2 각각, primary와 guardrail 각각)

- `σ = 0.0`
- `σ = math.nextafter(1e-6, 0.0)` (경계 바로 아래)
- `σ = 1e-6` (경계값, 유효 σ 아님)
- `σ = math.nextafter(1e-6, math.inf)` (경계 바로 위, 유효 σ)
- 기대값: primary는 두 버전 모두 앞의 세 경우 invalid, 마지막은 판정. guardrail은 v1에서
  앞의 세 경우 invalid, v2에서는 `zero_observed_variance`로 판정 진행, 마지막은 −σ 문턱.
- 음수·NaN·inf·bool σ는 두 버전 모두 기존 invalid 사유를 유지합니다.

**v2 판정**

- 변동 없는 guardrail, 평균 Δ = 0 → 판정 진행, `zero_variance_guardrails`에 포함.
- 변동 없는 guardrail, 평균 Δ < 0 → `revise / guardrail_regression`.
- `at_bound` true/false 각각에서 판정 결과가 같습니다(기록 전용 확인).

**버전 고정**

- v1으로 시작해 중단한 실행을 v2 기본 코드에서 재개 → v1으로 판정합니다. 요청 버전이
  다르면 재개를 거부합니다.
- v1 실행의 보고서를 v2 코드에서 재생성 → `decision_policy`가 v1 설명과 같습니다.
- 버전 값이 바뀌면 입력 SHA가 바뀝니다.

**기존 파일·해시 호환성**

- 변경 전 코드가 생성한 버전 필드 없는 manifest·metadata·ledger·checkpoint를 고정
  synthetic fixture로 보존합니다. 새 직렬화기로 다시 생성한 파일로 이 검증을 대체하지 않습니다.
- 새 코드의 load/freeze에서 원본 파일 bytes·SHA와 checkpoint의 입력 SHA가 그대로 유지됩니다.
  누락된 버전은 v1으로 해석되고, v2 요청은 기존 파일 수정 없이 거부됩니다.
- 중단 fixture를 재개하면 완료한 학습·판정을 반복하지 않고 남은 작업만 수행합니다. 기존
  ledger prefix는 동일하며 새 행만 추가되고, 같은 기록의 재전송은 중복 append되지 않습니다.
- 완료 fixture의 보고서를 별도 출력 위치에 재생성하면 입력·ledger·checkpoint·소비 marker의
  bytes·SHA는 변하지 않고 정책 설명은 v1입니다. 학습·Agent·final 추가 호출은 0입니다.
- 정책 버전 누락과 무관한 계약·metadata 변조는 기존처럼 거부합니다.

**전체 호출 경로 통합 검증**

- Controller 진입점에서 고정 v2를 입력하고 실제 Domain·Judge 경로를 통과시킵니다. primary
  문턱을 넘고 guardrail σ=0·Δ=0인 paired fixture로 validation confirmation과 final 모두
  v2 판정이 나며, ledger·보고서에도 같은 버전과 guardrail 목록이 남는지 확인합니다.
- 같은 paired 입력을 v1으로 실행하면 기존 `insufficient_baseline_noise`가 유지됩니다.
- 유효성 오류로 decision이 없는 v2 결과도 끝까지 v2로 기록됩니다. 입력 계약·판정 반환값·
  저장 기록 중 버전이 다른 경우는 거부하고 champion을 교체하지 않습니다.
- v2 중단·재개에서도 같은 버전이 유지되고, final 소비 marker를 초기화하거나 다시 소비하지
  않습니다. 호출 횟수와 기존 marker 보존을 확인합니다. 실제 비공개 final은 사용하지 않습니다.

**실행 전 차단과 진단 예외**

- D5 표의 primary/guardrail·v1/v2·실행 목적 조합을 검증합니다. 차단되는 새 실행은 Agent·
  screening·confirmation·final 호출이 모두 0이고 대기 사유가 기록됩니다.
- 목적이 없으면 새 실행은 성능 탐색으로 처리됩니다. 사전 등록되지 않은 진단 예외, 실행 중
  목적 변경, 잘못된 σ 입력으로 진단 예외를 요청하는 경우는 거부합니다.
- 명시적 진단은 고정 예산 내에서만 진행하며, 실제 Judge 결과와 별개로 champion 교체·
  final 자동 소비가 없습니다. 기존 실행 재개와 보고서 재생성은 D3의 호환성을 유지합니다.

**기존 원칙**

- `test_calibration_does_not_replace_zero_sigma` 통과를 유지합니다.
- calibration 원값과 비용은 보존하고, preflight 이후의 예산 사용과 구분합니다.

검증 명령은 `CLAUDE.md` Verification을 따릅니다(관련 테스트 → 전체 pytest → ruff).

## 7. 위험과 완화

| 위험 | 완화 |
|---|---|
| 판정이 느슨해져 잘못된 promote 증가 | primary 2σ 유지. 변동 없는 guardrail은 허용 폭 0으로 가장 엄격. final 단일 검증 유지 |
| 결과를 본 뒤 규칙 변경(사후 선택) | D6: 기존 profile 보존, 새 profile 사전 등록, final을 독립 근거로 명시 |
| 과거·진행 중 실행이 새 규칙으로 재해석됨 | D3: 실행 시작 시 버전 고정, v1 경로 보존, 재개 불일치 거부, 보고서도 고정 버전 |
| 기본값 추가로 기존 manifest SHA가 바뀌어 재개 실패 | 기존 형식·원본 bytes 보존, 새 입력 형식 분리, 변경 전 fixture로 호환성 검증 |
| Controller·Domain에서 버전이 누락되거나 final만 v1으로 판정 | 두 판정 경로에 명시 전달, 반환·저장 버전 대조, Controller 진입점의 통합 검증 |
| 판정 불가가 확정된 profile로 성능 탐색 예산 낭비 | 새 성능 탐색은 preflight 차단, 사전 고정한 진단 목적만 예외 허용 |
| 평균 Δ의 부동소수 반올림으로 생긴 극소 음수가 문턱 0.0을 넘지 못함 | 기존 `comparison_tolerance` 0.0과 같은 비교를 유지하고, 경계 사례를 테스트로 고정. 허용 오차 도입 여부는 §10 질문 |
| 변동 없는 지표가 계속 아무것도 측정하지 않음 | D4·D5로 드러내고, `at_bound`를 근거로 §9-1에서 지표 교체 결정 |
| 목표 모드 코드가 main에 없음 | `run_inputs`·Controller·Domain·Judge·ledger·report를 먼저 변경. D5와 profile 변경은 목표 모드 병합 뒤 적용 |
| seed σ가 실제 불확실성을 과소추정 | §9-2 진단 후 결정. 이 spec에서는 기존 seed σ 유지 |

## 8. 적용 순서

1. 이슈 발행 → 이 spec 확정.
2. main: 기존 입력 형식 호환성을 먼저 확보하고 `run_inputs`의 새 입력 형식·버전 필드,
   Controller·Domain·Judge의 버전 전달/분기(v1 보존), ledger 저장, report 설명을 연결합니다.
   변경 전 fixture의 재개·해시 보존과 validation·final 통합 검증을 수용 조건으로 둡니다(§6).
3. 목표 모드 병합 후: profile `at_bound`·버전 결합, 실행 목적 고정과 preflight 차단·진단
   예외(D5)를 적용합니다.
4. D6 절차로 #147 재검증 사전 등록·실행.

### 8.1 #148에서 확정한 1차 범위와 호환성 경계

- 저장소 소유자의 구현 지시에 따라 §8의 2단계를 진행합니다. v2의 변동 없는 guardrail
  허용 폭은 0.0, primary 2σ와 기존 비교 허용 오차는 유지합니다. 새 실행의 기본 정책은
  v1이며, v2는 명시적으로 선택합니다.
- 목표 모드는 main에 미병합이므로 D5·profile 연결과 #147 재실행은 후속입니다. 해당
  코드를 이번 브랜치에 임의로 병합하거나 실제 final을 소비하지 않습니다.
- 입력 형식 호환성은 **동일하게 고정한 runtime identity**에서 보장합니다. 현재
  `local_runtime._runtime_identity()`는 설정·모델·라이브러리·trusted Harness 소스 해시를
  입력에 결합하므로, 실제 코드 해시가 달라진 로컬 실행 재개는 기존처럼 거부합니다.
  누락된 정책 필드의 해석을 이유로 이 무결성 검증을 완화하지 않습니다.
- 기존 pinned runtime으로의 재개와 새 코드에서의 형식 호환성 검증을 구분합니다. 다른
  trusted 코드로의 실행 이관은 별도 migration 설계가 필요하며, #148이 이를 보장한다고
  보고하지 않습니다. 기존 원본 입력·ledger·checkpoint·소비 증거는 수정하지 않습니다.

## 9. 후속 (별도 spec)

1. **변별력 있는 지표:** 클릭이 slate당 1개인 구조에서는 Recall@1·@3, MRR이 순위 변화를
   더 잘 드러냅니다. 지표를 추가하거나 교체하려면 재calibration과 Judge 계약 변경이 필요합니다.
2. **σ 추정 방식:** #147에서 후보 NDCG는 5개 seed에서 두 값뿐이었습니다. 기준도 seed
   103=104, seed 105=screening 42로 같았습니다. `sampling=1.0`과 데이터 고정 때문에 seed σ는
   평가 데이터의 표본 변동을 반영하지 않습니다. slate bootstrap σ를 진단값으로 먼저
   기록하고 비교한 뒤 채택 여부를 정합니다.
3. **테스트베드:** validation slate 수를 늘리고, 탐색 라운드 간 validation 재사용에서
   생기는 선택 편향을 완화하는 방안을 검토합니다.

## 10. 확정한 결정과 후속 판단

1. **확정:** 변동 없는 guardrail 허용 폭 0.0, 기존 부동소수 비교 허용 오차 유지.
2. **설계 채택·구현 후속:** D5의 새 성능 탐색 차단과 명시적 진단 예외. 기존 재개는 보존.
3. **후속:** #147 재검증 시점과 §9-1 지표 개선의 선후 관계는 공통 경로 검증 이후 결정.
4. **확정:** 이번에는 기본 `judge_policy_version`을 v1으로 유지하고 v2는 명시 선택.
   기본값을 v2로 바꾸는 시점은 후속 판단.

## 11. 제안 이유 요약

- **지표와 데이터를 유지하면서 막힌 경로를 엽니다.** σ 검증뿐 아니라 입력 호환성,
  Controller·Domain·Judge의 버전 전달, ledger·보고서 기록과 실행 전 차단까지 함께 변경합니다.
- **안전성을 낮추지 않습니다.** primary 기준은 그대로이고, 변동 없는 guardrail은 기존보다
  엄격한 "조금도 나빠지면 안 됨"이 됩니다.
- **과거와 진행 중인 실행을 보호합니다.** 정책 버전을 입력 계약에 고정해 판정, 재개,
  보고서가 모두 같은 기준을 따르게 하고, v1 경로와 기존 파일·SHA를 보존합니다.
- **정직성 원칙과 맞습니다.** σ=0을 다른 값으로 바꾸지 않고, 변동 없음과 포화를 구분해
  기록하고, 결과를 본 뒤 만든 규칙을 기존 profile에 소급하지 않습니다.
- **판정 불가가 확정된 새 성능 탐색을 시작 전에 막습니다.** 복구·진단 실행은 별도 목적과
  예산으로 기록해, 실행 성공과 성능 채택을 구분합니다.

## 변경 이력

- rev 1 (2026-10-20): 초안.
- rev 2 (2026-10-20): 리뷰 반영.
  ① 같은 profile 재실행을 기존 profile 보존·v2 새 profile 사전 등록으로 변경(D6).
  ② 정책 버전을 실행 시작 시 고정하고 판정·재개·보고서 재생성이 따르도록 계약화,
     v1 판정 경로 보존(D3).
  ③ v1/v2 동치 조건을 "모든 σ > 1e-6"으로 정정하고 σ 경계값 테스트 추가(D1, §6).
  ④ σ=0을 "관측 변동 없음"으로 좁히고 상한·하한 포화는 `at_bound` 기록으로 분리,
     부동소수 오차가 없다는 서술 삭제(D2).
  추가로 calibration receipt SHA 참조 재사용을 명시했습니다(D6).
- rev 3 (2026-10-20): 추가 리뷰 반영.
  ① 기존 manifest·metadata·ledger·checkpoint·final marker의 원본 bytes·SHA 보존과
     새 입력 형식 분리, 변경 전 synthetic fixture의 재개·보고서 재생성 검증 명시(D3, §6).
  ② Controller → Domain → Judge → ledger·보고서의 버전 전달과 validation·final 통합
     검증, 오류 반환의 버전 보존·불일치 거부 추가(D3·D4, §5~§6).
  ③ 새 성능 탐색의 preflight 차단과 사전 고정한 복구·진단 예외, 기존 재개 보존 및
     calibration 비용 구분 추가(D5). 적용 순서·위험·소유자 질문도 같은 계약으로 갱신.
