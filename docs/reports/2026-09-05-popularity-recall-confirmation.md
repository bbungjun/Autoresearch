# #103 인기도 피처 제거와 Recall 확인 실험

## 문제

#16 탐색에서는 인기도 피처 6개 제거가 full 21피처보다 NDCG@10이 높았지만,
개인화 모델 자체의 video-only 대비 Recall 손실이 남았다. 같은 final로 피처 구성을
반복 선택하면 holdout이 탐색 데이터가 되므로 평가일과 seed를 새로 고정했다.

## 해결과 실험 설계

평가일 2026-09-02, world seed 10301/10302/10303, training seed 201/202/203을
결과 관측 전에 커밋 `c4ea827`에서 등록했다. 구현 및 독립 리뷰 반영 커밋은 `9de25ae`다.
3 world × validation/final × 3 training seed × full/removal/video-only의 54회 비교다.
동일한 LocalTrainingConfig와 E5 revision으로 피처 projection만 달리한다.
학습/평가 구현은 기존 local training과 Sealed Judge를 사용한다.

새 실행기는 각 모델의 prediction, 봉인 사본, 모델·학습 receipt, metric과 importance를
보존한다. 실제 학습 split과 같은 행 선택으로 진단 통계를 계산하고, split별 paired
delta와 world 평균의 표준편차를 구분한다. training seed 9개 반복을 독립 데이터 9개로
해석하지 않는다. 통계적 유의성이나 실제 사용자 일반화를 주장하지 않는다.

독립 리뷰에서 출력 폴더를 바꾸면 final registry도 새로 생기는 문제가 발견됐다.
저장소의 모든 worktree가 공유하는 고정 실행 claim을 최종 파일에 직접 exclusive-create하고
fsync하도록 바꾸었다. 동시 실행 8개 중 하나만 성공하는 회귀를 추가했다. 중단·부분 쓰기도
claim을 보존한다. 별도 clone이나 수동 파일 삭제를 막는 보안 경계는 아니다.

## 결과

사전 등록 판정은 **supported**다. 54회 학습·평가를 174.062초에 완료했다.
Validation과 final 각각 9/9 paired NDCG@10 개선, world 평균도 각각 3/3 개선했다.
모든 guardrail의 split 평균은 악화되지 않았다. PR-AUC/Brier의 개별 seed 악화는 있으므로
모든 지표가 모든 반복에서 좋아졌다고 해석하지 않는다. 판정 단위는 사전 등록한 split 평균이다.

| split | 모델 | NDCG@10 평균 | Recall@10 평균 |
| --- | --- | ---: | ---: |
| validation | personalized_lgbm | 0.764484 | 1.000000 |
| validation | without_video_popularity | 0.813117 | 1.000000 |
| validation | video_only_lgbm | 0.643405 | 1.000000 |
| final_holdout | personalized_lgbm | 0.778034 | 0.997222 |
| final_holdout | without_video_popularity | 0.831625 | 1.000000 |
| final_holdout | video_only_lgbm | 0.629289 | 1.000000 |

### 제거−full paired 변화

Loss/Brier는 full−제거로 방향을 보정하므로 양수는 개선이다. 표준편차는 기술 통계다.

| split | 지표 | 평균 변화 | 9쌍 표준편차 | 3 world 평균 표준편차 | 양수 쌍 |
| --- | --- | ---: | ---: | ---: | ---: |
| validation | brier | +0.000560 | 0.000686 | 0.000093 | 7/9 |
| validation | grouped_roc_auc | +0.014040 | 0.005143 | 0.005033 | 9/9 |
| validation | log_loss | +0.014815 | 0.003815 | 0.003008 | 9/9 |
| validation | ndcg_at_10 | +0.048633 | 0.017197 | 0.014865 | 9/9 |
| validation | ndcg_at_24 | +0.048633 | 0.017197 | 0.014865 | 9/9 |
| validation | pr_auc | +0.010041 | 0.019590 | 0.002659 | 6/9 |
| validation | recall_at_10 | +0.000000 | 0.000000 | 0.000000 | 0/9 |
| final_holdout | brier | +0.000419 | 0.000768 | 0.000076 | 7/9 |
| final_holdout | grouped_roc_auc | +0.014976 | 0.005694 | 0.004588 | 9/9 |
| final_holdout | log_loss | +0.014337 | 0.004905 | 0.002753 | 9/9 |
| final_holdout | ndcg_at_10 | +0.053591 | 0.023826 | 0.021910 | 9/9 |
| final_holdout | ndcg_at_24 | +0.052840 | 0.023310 | 0.021270 | 9/9 |
| final_holdout | pr_auc | +0.013012 | 0.023054 | 0.010387 | 5/9 |
| final_holdout | recall_at_10 | +0.002778 | 0.008333 | 0.004811 | 1/9 |

### Recall cutoff 해석

Validation은 세 모델 모두 Recall@10=1.0이며, final의 full은 0.997222, 제거 모델과
video-only는 1.0이다. Final의 9개 비교 중 Recall 개선은 1개, 동률은 8개다.
각 slate가 양성 1개를 갖는 이 fixture에서는 Recall@10이 정답의 top-10 포함 여부이고
NDCG@10은 그 안에서 정답을 더 높은 순위에 놓는지도 반영한다. 이번에는 제거 모델이
누락을 없애면서 상위 순위 품질을 높였다. #16의 Recall 손실이 이 새 날짜에서도 크게
반복된 것은 아니므로 일반적인 Recall trade-off 해결을 입증했다고 확대하지 않는다.

### 인기도 피처 분포와 사용 여부

아래 분포는 world별 전체 학습 입력 4,800행을 3개 world에 걸쳐 집계했다. 동일 학습 입력의
validation/final 중복은 제외했다. importance는 validation full 모델 9개의 평균이다.
원시 진단에는 각 seed의 실제 fit 2,880행 및 예측 입력의 분포도 별도로 보존했다.

| 피처 | min–max | 평균 | world별 표준편차 평균 | 0 비율 | null 비율 | gain 평균 | split 평균 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| view_count | 0–1e+06 | 587728 | 481926 | 40.194% | 0% | 1765.790 | 750.667 |
| like_ratio | 0–0.05 | 0.0293712 | 0.0240844 | 40.194% | 0% | 0.000 | 0.000 |
| comment_ratio | 0–0.005 | 0.00293712 | 0.00240844 | 40.194% | 0% | 0.000 | 0.000 |
| channel_subscriber_count | 0–100011 | 59808.6 | 49029.9 | 40.194% | 0% | 95.354 | 248.444 |
| channel_view_count | 0–1e+07 | 5.98056e+06 | 4.90274e+06 | 40.194% | 0% | 0.000 | 0.000 |
| channel_video_count | 0–111 | 62.8769 | 51.61 | 40.194% | 0% | 0.000 | 0.000 |

학습 입력의 video metadata 미관측은 40.194%이며 피처의 0 대체와 일치한다.
이는 null 비율 0%와 다른 의미다. 수집 시각은 UTC 00:00(KST 09:00)이므로 그보다 이른
요청에서 as-of metadata가 없다. Validation 예측의 video metadata 미관측 비율은
40.061%다.
view_count와 channel_subscriber_count만 실제 분기에 사용됐고 나머지 4개는 gain/split=0이다.
합성 수치들이 서로 연관되므로 6개가 각각 독립적인 유해 신호였다고 주장하지 않는다.

### 최근 행동 피처 동률 원인

3 world의 전체 학습 입력 및 9개 실제 fit subset에서 최근 행동 5개는 모두 min=max=0,
unique=1이다. 18개 full 모델(validation/final 포함)의 해당 피처 gain/split도 모두 0이다.
7일/30일 history 완전성 flag는 학습·예측 모두 false다. 예측에서는 값이 생겨도 학습에서
분기를 만들지 않았으므로 모델이 사용하지 못한다. 따라서 #16의 동률은 최근 행동의
일반적 무효성이 아니라 현재 fixture가 해당 신호를 학습할 조건을 제공하지 못한다는
설명과 일치한다. 과거 모델의 importance를 추가 확인해 동률 기록과 직접 대조했다.

### 검증 및 재현 증거

- 신규 evaluation ID 6개와 snapshot 3개가 #16과 겹치지 않음을 실행 전에 검사했다.
- 신규 final marker 3개, 두 번째 claim은 모두 already_consumed로 거절됐다.
- 54개 학습 receipt의 피처 열, 모델 파일 hash, paired split 행 hash와 설정을 대조했다.
- 결과 JSON에서 집계를 다시 계산해 원본 summary와 완전히 일치했다.
- E5 cache hit 1,080, miss 0, 새 inference 0회다. 기존 임베딩을 재사용했으며
  GPU 신규 inference 성능을 측정한 실험은 아니다.
- 회귀 24 passed, Ruff 및 git diff --check 통과. 독립 리뷰 P1 수정 후 추가 P1/P2 없음.
- 원시 결과 SHA256: `b70172bbcebe0cd7dfc47d243e0741b8e7f48def3a86f53c765f9d2fd648f5f5`.
- #16 원시 결과 SHA256은 실행 전후
  `e504042bded46fa385b6164c3d45136f041a15cbe6d8b9f896965feefc24d7cc`로 유지됐다.
- 원시 데이터/모델은 커밋하지 않는다. 실행 도구는 `python -m tools.run_popularity_recall`이며
  경로 인자는 `--output`, `--model-dir`, `--cache-dir`, `--previous-result`다.
  이미 소비한 #103 실험은 재실행하지 않는다. 집계 함수 `summarize`로 결과만 재검토한다.


## 해석의 경계와 다음 결정

RuleBasedActionLogGenerator의 propensity는 사용자 키워드와 영상 텍스트 겹침 및
video ID hash jitter로 정해진다. 인기도 수치는 직접 사용하지 않는다. fixture의
view/like/comment 수는 영상 index의 함수이고 channel 수치는 channel index의 함수다.
따라서 인기도 피처가 합성 ID/순서의 대리 변수가 될 가능성은 있으나, 중요도와 ablation
관측만으로 개별 피처의 과적합 인과성을 확정할 수는 없다.

최근 행동 피처는 이전 날짜만 읽는다. 현재 fixture의 완전 귀속 학습 행은 history 첫날에
위치하므로 이전 날 이력이 없다. 실제 fit 행의 상수값과 모델의 split/gain을 대조해 원인을
검증한다. label 생성 규칙이나 history 길이 수정은 새로운 평가 계약으로 분리해야 한다.

**다음 결정:** 후속 합성 실험에서는 인기도를 제거한 15피처 모델을 유력 비교 기준으로
사용한다. Production 21피처 계약은 유지한다. 즉시 목적함수를 바꾸기보다 학습 전에
충분한 history가 있는 fixture를 먼저 설계하고 최근 행동 ablation을 새 snapshot에서
검증한다. label 다양화 및 날짜가 바뀔 때의 ID jitter 의존도 검증은 #16의 후속 범위다.
이번 실험은 production 모델/피처 계약을 바꾸지 않는다. 실제 CTR, LLM relevance,
watch time 및 장기 폐루프 편향은 #16의 남은 범위다.
