# #105 선행 이력과 학습 기간 분리

상태: 사전 등록. 기준 코드 `45c416a5ad1f4510ea6c690203fb9fd0662dc642`.
관련: #16, #103, #104. 결과 확인 전에 날짜·seed·판정을 고정한다.

## 문제와 선택

#103에서 최근 행동 5개는 학습 행 전체에서 0이고 실제 모델에서 미사용이었다.
현재 history 2일 중 완전 귀속 라벨을 갖는 첫날만 학습하므로 그 앞의 이력이 없다.
학습날 당일의 이벤트를 피처에 넣는 보정은 미래정보 유입이므로 하지 않는다.

30일 warm-up을 먼저 생성하고 학습 행을 그 뒤로 제한한다. 7일만 준비하면 기존
30일 affinity의 불완전성이 남으므로 30일을 선택한다. 날짜별 생성 순서를 보존한다.
새 opt-in fixture v2는 history 32일이고, 기존 v1의 기본 2일과 bytes/identity를 유지한다.
일일 producer의 노출·click 규칙은 변경하지 않는다.

## 고정 시간 계약 (KST)

| 역할 | 날짜 | 허용 용도 |
| --- | --- | --- |
| warm-up | 2026-08-02~08-31 (30일) | 과거 피처 조립만, fit 라벨에서 제외 |
| 학습 | 2026-09-01 | 4800개 학습 대상 impression, 기존 seed별 60/20/20 분할 |
| 학습 label scan tail | 2026-09-02 | 9/1 클릭 귀속 확인, 평가 피처의 과거 이력 |
| 평가 | 2026-09-03 | 고정 validation/final user split |
| 평가 label scan tail | 2026-09-04 | Judge의 평가 label 완전성 확인 |

- 요청 날짜 D의 최근 피처는 KST D-7~D-1, affinity는 D-30~D-1만 사용한다.
- 학습에는 9/1 이전 날짜 행동만 피처로 사용한다. 9/1~9/2 행동을 교란해도
  9/1 피처가 바뀌면 실패한다. label 귀속과 feature 이력 cutoff는 별개다.
- Candidate view에는 평가 전날까지 이력만 게시한다. 평가 label은 Sealed Judge만 읽는다.
- v2 user metadata는 warm-up 시작 시점에 생성한다. 날짜별 영상 metadata as-of 계약 유지.

## 고정 실험

- world seeds 10501/10502/10503; training seeds 301/302/303.
- 15피처(인기도 6개 제거) vs 10피처(추가로 최근 행동 5개 제거).
- LocalTrainingConfig 기본값, E5 small revision
  `614241f622f53c4eeff9890bdc4f31cfecc418b3`, CUDA adapter, cache 재사용.
- 3 world × 2 split × 3 seed × 2 arm = 36회 학습·평가.
- split별 9쌍과 world별 seed 평균 3개의 표준편차를 구분한다.
  같은 세계의 seed 반복을 독립 데이터라고 주장하지 않는다.
- 15−10 NDCG@10 평균 >0, 양수 ≥6/9, 양수 world 평균 ≥2/3이고
  Recall@10/NDCG@24/grouped ROC-AUC/PR-AUC 평균 변화 ≥0,
  LogLoss/Brier 평균 변화 ≤0, Judge coverage 유효가 두 split 모두 성립하면
  `supported`, 아니면 `not_supported`. 음성 결과도 완료다.
- validation 학습 입력에서 30일 완전성, warm-up 행 제외, 최근 피처 중 적어도 하나의
  변동을 확인한 뒤 final을 소비한다. 모두 상수이면 `uninformative`로 중단해 원인을 기록한다.
- #16/#103 ID와 중복이면 중단. 기존 결과 hash는 실행 전후 대조한다.
  저장소 공통 #105 단일 실행 claim, 각 world final 단일 소비·재소비 거절을 유지한다.
  실패 후 marker 초기화/다른 seed 대체/같은 final 재실행은 하지 않는다.

## 구현·검증 순서

1. RED: 32일 fixture 시간 계약/v1 유지, warm-up 학습 제외, KST 7/30일 경계와 당일·미래 무영향.
2. GREEN: opt-in fixture v2, 검증된 학습 입력의 날짜 선택, 실험 runner/집계/시간 감사 기록.
3. 독립 리뷰와 관련 좁은 회귀, 실행 코드 커밋 후 실제 36회 수행.
4. 피처 분포·importance·paired metric·시간 비용·한계를 보고하고 PR CI/머지.

## 종료 조건과 한계

날짜 순서/비유입을 증명하고 실제 피처 값·모델 사용·성능·비용과 다음 결정을 기록하면
완료다. 일부 count는 fixture 규칙상 모든 사용자에게 같을 수 있으며 억지로 변동을 만들지
않는다. Production 계약과 실제 CTR·LLM relevance·장기 폐루프는 이번 범위가 아니다.
