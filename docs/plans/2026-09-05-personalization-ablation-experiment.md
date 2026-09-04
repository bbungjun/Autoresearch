# #16 개인화 비교군·ablation 실험 계획

## 목적

고정된 합성 평가 세계에서 production 21피처 개인화 LightGBM이 단순 순서와
비개인화 모델보다 나은지 확인하고, 어떤 개인화 피처군이 차이를 만드는지 같은
snapshot·split·seed의 paired 비교로 설명한다. 결과는 합성 환경에만 한정한다.

## 사전 고정 계약

- 기준 코드: Issue #16 연결 브랜치가 시작한 `c4e0d07`
- 평가일: `2026-09-01`
- 평가 세계 seed: `1601`, `1602`, `1603`
- 학습 seed: `101`, `102`, `103`
- 평가 단위: world seed × split × training seed의 동일 사용자·동일 slate
- primary: NDCG@10
- guardrail: Recall@10, NDCG@24, grouped ROC-AUC, PR-AUC, LogLoss, Brier,
  ranking/probability coverage
- final: validation 결과를 본 뒤 arm을 바꾸지 않는다. 아래 전체 arm을 고정한 뒤
  world별 final registry를 한 번만 claim하고 하나의 실행에서 함께 평가한다.

## 비교군 5종

1. `trending`: 원래 Trending rank를 [0, 1] 역순 점수로 변환한다. 첫 실행 전 입력
   점검에서 canonical fixture의 `original_rank`·`candidate_source`가 모두 null이고
   snapshot이 행을 재정렬한다는 사실을 확인했다. 어떤 metric도 계산하기 전에, 이
   합성 fixture에 한해 candidate-safe `fixture-video-YYYYMMDD-NNNN`의 원천 행 번호를
   rank로 복원하도록 고정했다. 이 fallback은 production 데이터에는 적용하지 않는다.
2. `popularity`: 같은 slate 안의 as-of `view_count`를 [0, 1] min-max 점수로 변환한다.
   관측 전 영상은 production 피처 조립과 같은 cold-start 값 `0`을 사용한다.
3. `video_only_lgbm`: 영상 자체 9피처만 사용하는 LightGBM이다.
4. `personalized_lgbm`: production `MODEL_FEATURE_COLUMNS` 21개를 사용하는 LightGBM이다.
5. `oracle_upper_bound`: Judge만 봉인 label을 사용해 만드는 이진 relevance 상한이다.

`trending`, `popularity`, `oracle_upper_bound`는 학습 seed와 무관하므로 world별 한 번
계산하고 paired 집계에서는 같은 world의 각 학습 seed에 결합한다.

## Ablation 5종

각 arm은 `personalized_lgbm`에서 정확히 한 피처군만 제거한다.

| arm | 제거 피처 |
| --- | --- |
| `without_user_static` | `age_group`, `occupation`, `watch_time_band` |
| `without_recent_behavior` | 최근 7일 click/view/watch/like/total 5개 |
| `without_category_match` | `historical_category_affinity`, `preferred_category_match`, `historical_category_match` |
| `without_topic_similarity` | `topic_similarity` |
| `without_video_popularity` | `view_count`, `like_ratio`, `comment_ratio`, channel 규모 3개 |

## 구현 순서

1. `train_local_candidate()`에 검증된 feature projection을 추가한다. 기본 호출은 기존
   전체 열 동작을 유지하고, 실험 호출만 현재 batch의 중복 없는 부분집합을 허용한다.
2. Judge-side 실행기를 추가해 fixture 생성, validation/final candidate view 게시,
   예측 봉인·채점, final 단일 claim, 원자적 JSON 결과 기록을 수행한다.
3. arm 정의·점수 범위·feature projection·final 소비 계약을 회귀 테스트로 고정한다.
4. 3×3 실험을 실행하고 raw 결과 hash, paired delta 평균·표본 표준편차, coverage와
   판정을 포트폴리오 보고서에 기록한다.
5. 독립 리뷰와 GitHub CI를 통과한 PR만 merge한다.

## 판정

합성 rule-based 환경의 개인화 개선 근거는 다음을 모두 만족할 때만 `supported`다.

- validation과 final 모두에서 `personalized_lgbm - trending` 및
  `personalized_lgbm - video_only_lgbm` NDCG@10 평균이 양수다.
- 두 비교의 양수 방향이 전체 paired 반복의 2/3 이상에서 유지된다.
- Recall@10, NDCG@24, grouped ROC-AUC, PR-AUC, LogLoss, Brier의 방향 정규화 평균이
  video-only 대비 음수가 아니다.
- 하나 이상의 ablation에서 NDCG@10이 full model보다 반복적으로 낮아진다.
- 모든 ranking/probability coverage가 Judge 계약의 유효 기준을 통과한다.

하나라도 실패하면 개선을 주장하지 않고 `not_supported`로 기록한다. Oracle은 상한 확인용이며
판정 비교군에 포함하지 않는다.

## 증거 경계

현재 canonical fixture는 `generator=rule_based`이고 봉인 label은 binary `clicked`다.
따라서 이번 실행은 여러 fixture/world seed의 규칙 기반 Oracle 계층을 검증하지만 LLM relevance,
여러 LLM Judge, simulated watch time, 폐루프 장기 편향은 측정하지 않는다. 이 네 항목은 결과를
꾸며 채우지 않고 #16의 열린 후속 조건으로 남긴다. 기존 #60·#69·#71 final marker와 산출물은
읽거나 초기화하지 않는다.

## 종료 조건

- 동일 snapshot에서 5개 비교군과 5개 ablation 결과가 존재한다.
- 3개 world seed와 3개 training seed의 paired delta·분산이 존재한다.
- final marker는 신규 world마다 정확히 하나이고 재실행이 fail-closed 된다.
- raw JSON과 보고서 수치가 서로 일치하며 실제 사용자 성과로 표현하지 않는다.
- 코드·문서·테스트·독립 리뷰·CI가 같은 PR에서 완료된다.
