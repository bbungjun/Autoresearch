# 다양한 행동 조건의 15피처 대 10피처 비교 사전 등록

관련: #109, #107, #16. 상태: **준비만 승인, 학습/평가 미실행**.

## 질문과 arm

다양한 과거 행동을 갖는 합성 사용자에서 최근 행동 5개가 ranking 품질을 개선하는가?
`with_recent`는 기존 21피처에서 video popularity 6개를 제거한 15피처,
`without_recent`는 여기서 최근 행동 5개를 제거한 10피처다. 기존 canonical 열 순서를
보존하고 mean_topic_similarity 추가열은 제외한다. 10열은 15열의 정확한 부분집합이다.

## 데이터·날짜·분할 고정

학습 bundle은 #107의 원본 seed 10701/10702/10703을 그대로 사용한다.
각 world에서 8/3~9/1 warm-up, 9/2 학습 impression, 9/3 label scan tail이다.
원본 manifest SHA256은 순서대로 다음과 같다.

- `33f83133f73fc51ee09892d16e40555c2ced926f90ec0931a362fa011236bc79`
- `3ffc3ed5cd51084832aa4f11cc04009354c1c43273e3e6e2ad08482da69f9e14`
- `feb099b82b4a3962cbf36e4af80d3868a13e5e2ffcdd5519642c531e562866fc`

학습 seed는 401/402/403이다. 정답으로 stratify하여 먼저 test 20%, 남은 80% 중
validation 25%를 나눠 60/20/20을 만든다. 기존 local_training의 seed 알고리즘을 유지하며
arm 모두 같은 label/행 순서/split을 사용한다. Internal validation은 category vocabulary
조립에만 사용하고 early stopping/튜닝은 하지 않는다. Internal test로 최종 결론을 내리지 않는다.
같은 사용자의 여러 노출이 내부 split에 들어갈 수 있어 새 사용자 일반화 평가가 아니다.

후속 평가 cohort는 학습 world 10701→10901, 10702→10902, 10703→10903으로 1:1 고정한다.
학습 사용자와 다른 신규 seed 사용자이며 각 cohort의 기존 hash bucket 규칙으로
validation 800명/final 200명을 선택한다. 기존 선택기의 ID 후보 생성식을 그대로 사용해
bucket별 목표 수만 늘린다. 학습 데이터의 160/40 구성은 바꾸지 않는다.
평가 시점은 **2026-09-04**, scan tail은 **09-05**다.
잠재 상태의 anchor는 학습 데이터와 같은 08-03이며, 08-03~09-03 이력에서 피처를 만든다.
날짜별 활동/노출/클릭/관심 규칙은 diverse-behavior-v1과 동일하게 유지한다.
평가일에도 접속 사용자만 8/16/24개 노출을 가지며, 미접속 사용자에 가짜 노출을 채우지 않는다.
후속 executor는 같은 규칙으로 09-04/05까지 생성하는 확장을 구현하고,
평가 원본 생성 **전에** 코드/정책 hash를 봉인해야 한다. 이 확장은 이번 입력 조립에 포함되지 않는다.

평가 cohort의 raw label/latent manifest는 Judge만 열람한다. Candidate에는 평가 전날까지의
관측 이력·허용 metadata·label 없는 평가 slate만 제공한다. 과거 관측으로 피처를 만드는
것은 허용되지만 label/잠재 프로필의 candidate 전달은 금지한다.

## 학습 조건

LightGBM 기본 LocalTrainingConfig: n_estimators=200, learning_rate=0.05, num_leaves=31,
sampling_rate=1.0, scale_pos_weight=auto(각 train subset의 음성/양성 비율).
양 arm은 동일한 sampled train 행을 사용하며 추가 sampling/튜닝을 하지 않는다.
E5 small revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`과 기록된 실제 모델 파일/실행
identity를 사용한다. 학습/평가 metadata는 요청 시각 as-of로 선택한다.

3 학습 world × 3 seed × 2 arm = **18개 모델**을 fit한다. 각 모델을 대응 평가 cohort의
validation/final 모두에 예측하므로 **36개 평가 관측**, split별 paired 9쌍이다.
Final용으로 모델을 재학습하지 않는다. 사전에 고정된 6모델/world를 하나의 평가 묶음으로
봉인하며 최종 결과를 본 뒤 모델/arm/seed를 추가하지 않는다.

## 지표·판정 고정

Primary는 macro NDCG@10. Guardrail은 Recall@10, NDCG@24, grouped ROC-AUC,
PR-AUC, LogLoss, Brier이며 기존 Judge metric 정의를 그대로 사용한다.
슬레이트 크기가 K보다 작으면 기존 metric의 min(K, size) 처리를 따른다.
Zero-click slate는 ranking 평균에서 제외하고 제외 수/coverage를 함께 보고한다.
노출만 있고 클릭하지 않은 slate도 확률지표에서 제거하지 않는다.

양 split 각각 다음을 모두 만족해야 `supported`다.

- 15−10 NDCG@10 평균 >0, 양수 ≥6/9 paired, 양수 world별 seed 평균 ≥2/3.
- Recall/NDCG@24/grouped ROC-AUC/PR-AUC 평균 변화 ≥0.
- LogLoss/Brier 평균 변화 ≤0.
- 같은 평가 ID/행 수/row key, 양 arm coverage 동일, 기존 ablation coverage 검사 통과:
  각 ranking/grouped ROC-AUC의 유효 group 수≥max(30, ceil(전체 group 수×0.2)),
  음성·양성 label 모두 존재, metric은 전부 유한.

품질/coverage 미달은 `uninformative`로 종료하고 seed 교체나 기준 완화를 하지 않는다.
유효한 데이터에서 개선 조건 미달은 `not_supported`다. 9개의 반복은 독립 데이터 9개가
아니며 paired 표준편차와 3개 world 평균의 표준편차를 구분해 기록한다.
이 판정은 합성 ablation 가설의 채택이며 production 모델 자동 승격을 뜻하지 않는다.

## Final·중단·비용 정책

Validation을 먼저 완료한다. 원본/bundle/embedding hash, 행과 split 동일성, fit subset의
최근5개 비상수, 평가 coverage/지표 유한성 검증을 통과한 경우만 final을 소비한다.
Validation의 개선 여부로 선택적으로 final을 숨기지 않는다. 유효성 통과면 개선/비개선 모두
고정 묶음을 평가한다. Final은 cohort별 단일 claim, 총 최대 3회이며 재소비는 거절한다.
기존 #16/#103/#105 final 및 #107에서 관측한 개발 데이터를 final로 재사용하지 않는다.

하드 상한은 fit 18회, GPU E5의 고정 모델/기존 cache, 외부 유료 API 0회,
전체 30분이다. 초과/실패 시 추가 seed·모델 재학습을 하지 않고 산출물/실패를 기록한다.
손상되거나 부분 완료된 final 묶음의 marker를 초기화하지 않는다. 정확히 동일한 봉인
예측을 이용한 완료 판독/보고서 복구만 허용하며 새 채점은 금지한다.

## 이번 Goal 종료 경계

실제 E5 학습 feature/label/split bundle과 무결성 receipt, 이 사전 등록 문서 및 검증
기록을 완성하면 준비 완료다. 후속 executor·평가 생성 확장·snapshot 봉인·학습·채점은
다음 실행 작업이며 이번 Goal에서 성공했다고 주장하지 않는다.
