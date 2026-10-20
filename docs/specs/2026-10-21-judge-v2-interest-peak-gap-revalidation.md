# Judge v2 — 고정 interest_peak_gap 후보 재검증 사전 등록

> 2026-10-21 | 실행 전 사전 등록 | #150 | 선행 #149 병합 완료

## 목적과 근거 한계

#147에서 생성한 동일 후보를 새 Judge v2로 실제 재학습·평가한다. 새로운 Agent 가설 생성
실험은 아니며, 기존 목표 모드 전체를 병합하거나 기존 v1 기록을 재판정하지 않는다.
v2는 #147 결과를 본 뒤 설계됐다. 같은 validation의 재실행은 재현 근거이며 독립적인
일반화 근거로 주장하지 않는다. 미사용 final의 단일 평가 결과를 별도로 보고한다.

## 고정 조건

| 항목 | 사전 고정값 |
|---|---|
| trusted Judge | #149 merge `a05f3b61ee32fc45a29e9693213625e8974d8ab8`의 공통 Harness |
| 정책 | `judge_policy_version=v2`, primary 평균 Δ≥2σ, guardrail은 σ>1e-6일 때 Δ≥−σ, 0≤σ≤1e-6일 때 Δ≥0 |
| baseline 학습 코드 | `f06c9ab75461a04df4ccef65bc9ec7bcd90313fa` |
| candidate 학습 코드 | `db1cb70920cb3ff29c1b063d5d4e4ed733f1a46e` (동일 baseline의 직접 자식) |
| candidate patch SHA256 | `62d75dbe51857d8ff9bfa15999e9cbd73fc79f16b13f7996a3769533ee6d39b8` |
| 피처 | `interest_peak_gap = topic_similarity - mean_topic_similarity`; 둘 다 후보 영상에 종속된 기존 안전 입력 |
| 데이터 | #147의 동일 synthetic offline fixture, 원래 validation/final snapshot·metadata·원본 registry |
| calibration | 기존 5-seed receipt SHA256 `e36ebf5479e4e941326251ccc46dd0cbb089950eefe79fdb4cfda8bc61e3cd56` 재사용, 재학습·sigma 보정 없음 |
| 모델 | 고정 multilingual-e5-small revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`, binary LightGBM |
| 학습 설정 | learning_rate=0.05, n_estimators=200, num_leaves=31, sampling_rate=1.0, scale_pos_weight=auto |
| seeds | screening 42, confirmation 101~105, final 101~105 |
| 예산 | 후보1개, screening2fit + confirmation10fit + 조건부 final10fit, 최대22회 예측 학습 프로세스 시작 |
| timeout | 예측 학습 프로세스당300초, validation 시작 예산7200초 |
| Agent | 새 coding Agent·advisory LLM 호출0회. 과거 후보 생성의 Agent 기여는 #147에 귀속 |

Δ의 방향은 높을수록 좋은 지표에서 후보−기준, LogLoss/Brier에서 기준−후보다. 최종 비교는
반올림 전 원값으로 수행한다. threshold·seeds·후보·분할을 결과를 본 뒤 바꾸지 않는다.

## 준비·실행 절차

1. 새 profile에 위 조건과 드라이버·spec·trusted 소스의 SHA를 결합한다. typed prediction
   설정, 원본 calibration의 raw 값·표본 표준편차(ddof=1)·artifact·모델 파일·라이브러리·
   metadata·snapshot 결속을 검증한다. 원본 final marker가 있으면 시작하지 않는다.
2. 기존 #147 입력·ledger·보고서·calibration과 fixture의 파일 해시를 보존 목록으로 고정한다.
   final registry는 복제·초기화하지 않고 원본을 그대로 사용한다.
3. 고정 후보 adapter는 동일 Git commit과 patch만 새 prepare attempt에 연결한다. 과거
   candidate.json의 card·시간·Agent 비용을 새 실행의 값으로 복사하지 않는다.
4. 공통 LocalResearchTrialRunner·LocalRunner로 실제 baseline/candidate를 seed마다 별도
   학습한다. trusted Judge는 병합된 v2 코드에서 봉인된 예측을 독립 채점한다.
5. screening 탈락·confirmation discard/revise/invalid·실행 실패이면 추가 후보·seed 탐색·
   재시도 없이 종료한다. confirmation에서 v2·정확한 고정 candidate의 promote 기록이 있을
   때만 원래 Controller의 final 단일 소비 경로로 진입한다.
6. validation과 final은 따로 보고한다. 완료 뒤 terminal 재생에서 학습 호출0·기존 파일·
   marker bytes 보존을 검증한다. 보호 목록의 기존 증거는 실행 전후 동일해야 한다.

## 실험 전용 경계

공통 Controller는 정상 screening 후보만 있어도 final에 진입하므로, 이번 드라이버는
`_run_final` 진입 전에 **v2 confirmation promote + 정확한 candidate SHA**를 검사한다.
통과 시 기존 final 경로를 그대로 호출한다. 미통과 시 marker를 만들지 않고 별도 수치
receipt로 종료한다. 이를 제품 공통 정책 변경으로 주장하지 않는다.

원래 목표 모드의 D5/profile 연결은 여전히 후속이다. 이번 profile은 고정 후보 재검증용
별도 형식이며 목표 모드 profile로 가장하지 않는다. `at_bound`는 calibration 원값에서 별도
기록하며 공통 보고서의 미제공 값을 추정해 덮어쓰지 않는다.

수치 Judge는 실제 실행한다. 자연어 advisory는 요청하지 않으며, 표준 보고서에는 해당
미실행 상태가 unavailable로 남는다. 독립 Agent 리뷰는 준비·결과 감사에만 쓰고 원래
모델 학습·수치 판정 또는 새 연구 Agent 성과로 합산하지 않는다.

## 결과 수용

- validation promote만으로 성능 개선 완료를 선언하지 않는다.
- final promote이면 이 고정 synthetic offline holdout에서 v2 수용 조건 충족으로 보고한다.
- final discard/revise/invalid이면 그대로 보존하고 재소비하지 않는다.
- final 진입 이후 중단·오류가 나면 marker를 초기화하지 않으며 재시도하지 않는다.
- 실행 전 드라이버·profile·사전 등록을 독립 리뷰하고, 실행 후 학습 sidecar·pair·ledger의
  지표 방향·seed·판정 정책·fit 수·기존 증거 보존을 별도로 감사한다.
