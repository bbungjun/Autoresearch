# Recall@10 원인 후보 통제 실험 (#117)

## 문제와 해석 범위

#113은 최근 행동 15피처 대10피처의 Recall 하락을 관측했고 #115는 validation의
top10 이탈137/진입107을 확인했다. 이는 순위 변화의 설명이며 복잡도·목표·선호정보·표본수의
인과 원인 확인은 아니다. 본 실험은 rule-based 합성 분포에서 각 개입의 효과만 판정한다.
기존 final·예측·소비기록은 변경하거나 평가에 재사용하지 않는다.

## 실행 전 고정 계약

입력은 #109 world10701/10702/10703 bundle(9/2 학습), #111 cohort10901/10902/10903
기존 validation(9/4)이다. bundle hash는 tools.prepare_behavior_evaluation.BUNDLE_HASHES,
평가 summary hash는 tools.run_diverse_behavior.EVALUATION_SUMMARY_HASH를 검증한다.
개발용 평가의 결과는 후보 선택에만 쓴다. 기존 #113 저장 validation 피처를 해시 검증해 재사용한다.
E5 revision/identity를 유지하고 신규 평가 피처는 같은 로컬 모델로 조립한다.

모든 신규 arm은 seed401/402, 사용자 단위 hash 정렬 후 60% fit/20% calibration/나머지20%
reserve 분할을 공유한다. 사용자별 모든 slate를 한 분할에 두며 fit 행은 slate_id/video_id로
정렬한다. ranker의 group은 연속 slate별 행 수이고 합계가 fit행 수와 같아야 한다.
calibration과 fit 사용자·slate는 서로소다. vocabulary는 공통 기본 fit의 관측 범주만 사용한다.
표본 증가군도 같은 vocabulary를 쓰고 unknown은 결측 처리한다. 평가 사용자와 학습 사용자도 서로소다.

분할 변화와 class weighting이 목표 효과에 섞이지 않도록 모든 신규 binary는 sample weight=1,
scale_pos_weight=1로 통일한다. 따라서 이는 신규 protocol baseline이며 #113의 가중 binary와
직접 비교한 원인 추정이 아니다. 과거 결과는 역사적 참고로만 표시한다.

| arm | 가설 | 공통 baseline15 대비 유일한 변경 | 모델 fit | 보정 fit |
|---|---|---|---:|---:|
| baseline15 | 공통 대조 | 15피처 binary, leaves31 | 6 | 6 |
| reference10 | 최근 행동 대조 | 최근 행동5열 제거 | 6 | 6 |
| shallow | 복잡도 | num_leaves=7 (다른 설정 동일) | 6 | 6 |
| ranker | 학습 목표 | lambdarank, label_gain=[0,1], truncation=10 | 6 | 6 |
| preference | 영상별 선호 | 과거7일/30일 click의 후보영상 카테고리 비율2열 추가 | 6 | 6 |
| larger | 학습 표본 | fit에 reserve 사용자 추가(60→80%), calibration 고정 | 6 | 6 |
| 합계 | 가설당 후보1종 | 3world ×2seed ×6arm | 36 | 36 |

공통 LightGBM은 estimators200, learning_rate0.05, min_child_samples20, n_jobs1,
deterministic=true, force_col_wise=true, early stopping/튜닝/다운샘플링 없음이다.
prefererence 피처는 각 노출의 KST 당일 시작 이전 click만 사용한다. 후보영상 category와
click영상 category는 관측 가능한 시점 metadata에서 얻는다. 잠재 profile/미래 label 사용 금지.
분모는 해당 사용자 기간 내 모든 click 수이며 0이면 비율0이다.

각 모델은 같은 calibration 사용자들의 자연 노출 분포에 양의 기울기 sigmoid 보정을 1회 fit한다.
raw score로 ranking/AUC/AP를, 보정 확률로 LogLoss/Brier를 계산하고 둘을 별도 보존한다.
보정은 logaddexp logistic loss, slope bounds[1e-6,100], intercept[-100,100], 초기값(1,0),
L-BFGS-B maxiter500으로 고정한다. 실패/비유한값은 실험 중단이며 다른 보정법으로 교체하지 않는다.
단순 sigmoid를 학습된 확률로 간주하지 않는다. ranker 포함 모든 arm을 같은 보정 조건에서 비교한다.

## 지표·선택·채택

기존 ranking_metrics의 score 내림차순/video_id 오름차순 tie break, slate macro 평균,
zero-click 제외 규칙을 사용한다. 확률 지표는 zero-click 행도 포함한다.
Recall@10 primary, NDCG@10/24·grouped ROC-AUC·global average precision(PR-AUC 명칭)·
보정 LogLoss/Brier가 guardrail이다. 추가 진단으로 slate크기8/16/24별 Recall/NDCG를 기록한다.
각 cohort에서 ranking 유효 slate 최소30 및20%, AUC 양클래스 그룹 최소30 및20%,
전체 확률 양클래스·유한값을 요구한다. coverage 실패는 uninformative이며 후보 선택/신규 final 중단이다.

3world ×2seed의 6쌍을 동일 가중한다(독립 사용자6회로 해석 금지).
개발 통과는 Recall 평균Δ≥0.005, 양수≥4/6쌍, 양수 world평균≥2/3,
모든 guardrail의 방향성 평균Δ≥0이다. 통과 후보 중 RecallΔ 최대, NDCGΔ 다음,
arm 이름 오름차순으로 한 후보를 선택한다. 통과 후보가 없으면 같은 정렬의 최고 후보를
진단용으로만 고정하고 채택 자격은 false로 유지한다. reference10은 선택 후보가 아니다.

선택한 후보·baseline15·reference10의 기존 모델/보정기, 코드 tree, 전처리, 입력 해시,
본 spec과 평가 기준을 selection.json으로 먼저 봉인한다. 이후 신규 cohort11701/11702/11703,
각 validation200/final800 사용자, 기존 behavior-evaluation-policy-v1 규칙과 8/3 anchor,
9/4 평가·9/5귀속 tail로 생성한다. 신규 데이터의 validation은 사용하지 않는다.
새 사용자·평가ID가 모든 기존 학습/개발/과거 final ID와 겹치면 중단한다.
신규 final은 cohort당1claim, 전체3claim에서 위3arm×2seed=6예측을 먼저 모두 봉인 후 채점한다.
추가 학습0, 재선택0, 재채점0. 채택은 개발 통과와 신규 final의 동일 기준 통과 모두 필요하다.
그 외는 not_supported(유효 부정 결과) 또는 uninformative(유효성 실패)다.

## 예산·중단·증거

72학습시도≤90, 가설당1후보≤2, 유료API0. 여유18회는 새 후보/재시도에 사용하지 않는다.
계산 상한은 입력 준비900초+model/calibration1800초+개발평가600초+
신규 생성/봉인2400초+최종평가900초+검증/집계600초=7200초다.
단계 실측 wall time을 합산하고 전체 감독 프로세스가 남은 예산 초과 시 프로세스 트리를 종료한다.
코드 작성/리뷰/CI 대기시간은 실험 계산이 아니며 별도 기록한다. 실제 실험의 실패 시도도 포함한다.
fit 시작 전에 attempt를 독점 생성하고 끝난 모델/보정/예측/점수 receipt를 hash로 재사용한다.
재개는 완료 단계의 해시 확인만 허용하며 중간 실패한 학습은 재시도하지 않는다.
공통 git registry의 #117 run claim으로 다른 output 이름의 실험 재실행도 금지한다.
단계별 입력/코드/모델/예측/결과 해시와 초·fit시도·API0을 보존한다.
원본 파일과 기존 소비 marker는 시작/종료 SHA256 목록으로 보존 확인하며 내용은 평가에 사용하지 않는다.
미완료/오류/예산 소진은 명시하며 전체 완료로 보고하지 않는다. 배포와 production 승격은 범위 밖이다.

## 근거

- [#113](https://github.com/bbungjun/Autoresearch/issues/113), [#115](https://github.com/bbungjun/Autoresearch/issues/115)
- [LightGBM group 계약](https://lightgbm.readthedocs.io/en/v4.4.0/pythonapi/lightgbm.LGBMRanker.html)
- [분리된 calibration 데이터](https://scikit-learn.org/stable/modules/calibration.html)
