# #103 video-popularity 제거와 Recall 재검증

상태: 실행·분석 완료, `supported`. 관련: #103, #16, #102.
사전 등록 커밋 `c4ea827`, 실행 코드 `9de25ae`. 상세 결과는
[확인 실험 보고서](../../reports/2026-09-05-popularity-recall-confirmation.md)에 보존한다.

## 문제와 가설

#16에서 인기도 6개 제거가 NDCG@10을 개선했지만 개인화 모델은 video-only보다
Recall@10이 낮았다. 새로운 합성 snapshot에서 제거 효과와 Recall 손실을 확인한다.
최근 행동 피처 ablation의 완전 동률은 피처 분포와 모델 사용 여부로 진단한다.

## 고정 조건

- 기준 코드: `399fbbb7d221130beb3dc061bbffe0e95c2e44cb`.
- 평가일 2026-09-02, world seeds 10301/10302/10303, training seeds 201/202/203.
- full 21개, without_video_popularity 15개, video_only 9개. 제거 열은
  view_count, like_ratio, comment_ratio, channel_subscriber_count,
  channel_view_count, channel_video_count이다. 추가 실험 피처 mean_topic_similarity는 제외한다.
- 기존 LocalTrainingConfig 기본값(200 trees, learning_rate .05, num_leaves 31,
  sampling_rate 1, scale_pos_weight auto)과 기존 split/학습 계약을 유지한다.
- E5 small revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`, CUDA, batch 8.
- 3 worlds × 2 splits × 3 training seeds × 3 arms = 54회 학습·평가.
- #16 원시 결과 SHA256 `e504042bded46fa385b6164c3d45136f041a15cbe6d8b9f896965feefc24d7cc`.
  해당 결과의 snapshot/evaluation ID와 신규 ID가 겹치면 중단한다.
  신규 final은 world마다 한 번 소비하며 두 번째 claim 거절을 확인한다.
  소비 후 실패해도 marker를 초기화하거나 새 seed로 대체하지 않는다.
  실행 전 저장소 git common directory에 #103 단일 실행 claim을 원자적으로 기록한다.
  출력 폴더/worktree 변경 재실행도 차단하며 실패 시 claim을 유지한다.
  별도 clone이나 수동 삭제까지 막는 보안 경계로 주장하지 않는다.

## 결과 전 판정 고정

각 split의 9개 paired 관측을 별도로 요약한다. training seed 반복을 독립 표본으로
주장하지 않으며 world별 3개 seed 평균의 분산도 함께 기록한다.

- 제거−full NDCG@10 평균 > 0, 9쌍 중 6쌍 이상 양수, 3 world 평균 중 2개 이상 양수.
- 제거−full의 Recall@10, NDCG@24, grouped ROC-AUC, PR-AUC는 평균 ≥ 0.
  LogLoss와 Brier는 평균 ≤ 0. 모든 Judge coverage 유효.
- 위 조건이 validation/final 모두 성립하면 `supported`, 아니면 `not_supported`.
- Recall 회복 여부는 별도 명시한다. 제거−video-only Recall 평균 ≥ 0이면 완전 회복,
  제거−full Recall > 0이나 여전히 video-only 미만이면 부분 회복이다.
- 음성 결과도 완료다. final을 본 뒤 임계값·피처·목적함수를 바꾸어 재실행하지 않는다.
  합성 결과만으로 production 모델/피처 계약을 변경하지 않는다.

## 진단과 완료 조건

1. 실제 학습 입력과 예측 입력의 인기도 6개·최근 행동 5개 피처 분포
   (min/max/mean/std/unique/null/zero), 메타데이터 누락과 history coverage를 기록한다.
2. full 모델의 각 피처 gain/split importance를 저장한다. 상수·미사용 여부는 직접
   측정하며 관찰만으로 label mismatch/과적합의 인과성을 단정하지 않는다.
3. 세 모델의 metric 평균·paired delta·표준편차, Recall trade-off와 판정 보고서를 작성한다.
4. 좁은 회귀 테스트 → 독립 리뷰 → 실제 54회 → 결과 검토 → PR CI → 머지 순서로 완료한다.
   실행 코드와 사전 등록 계획을 먼저 커밋한다. 원시 데이터는 커밋하지 않는다.

## 한계

새 날짜와 seed도 같은 rule-based fixture 생성 규칙을 공유한다. 실제 사용자 CTR,
LLM relevance, watch time, 장기 폐루프 일반화의 증거는 아니다.

## 완료 기록

54회 학습·평가 완료(174.062초). NDCG@10 제거−full 변화는 validation +0.048633,
final +0.053591이며 각각 9/9 양수다. Final Recall은 0.997222→1.0이다.
모든 split 평균 guardrail과 coverage가 사전 기준을 통과했다. 신규 final marker 3개와
재소비 거절을 확인했다. 최근 행동 5개는 실제 fit 입력에서 모두 0이고 full 모델에서
미사용임을 확인했다. Production 피처 계약은 유지하고 합성 비교 기준으로 15피처를 권고한다.
