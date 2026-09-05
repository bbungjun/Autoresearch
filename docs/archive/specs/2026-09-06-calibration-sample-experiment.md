# #119 고정 모델의 보정 표본 확대 실험

## 질문과 고정 범위

#117의 선호 피처 후보는 ranking 개선에도 final Brier +0.000100439로 기각됐다.
현재 개발 진단에서 보정기는 272~368노출, 클릭 정답 9~14개로 학습됐다.
작은 보정 표본이 확률 추정에 영향을 주는지, 모델을 고정한 표본 확대 1종으로 검증한다.
Brier는 보정뿐 아니라 판별력도 반영하므로 Brier 악화를 보정 실패의 인과 증거로 단정하지 않는다.

기존 #117 결과 SHA256 `b40b80f0ce50494097cc74c903edcb86e63f33362b084e8ba3920f39465e1dfe`,
selection SHA256 `5c9fe0929c3ac29982576ced639dad27f855804907589e79639d7389e8004fd0`,
models-sealed SHA256 `21e921bc7ee924d260ce28447aca911614c67e794fe4baa3d7c3c8e1546fc009`를 pin한다.
selection의 prepared/model 해시로 읽는 모든 입력을 대조한다. #117 final 정답·예측은
무결성 hash 이외에는 읽지 않는다. 기존 개발 validation 10901/10902/10903만 진단에 쓴다.

## 설계

3 학습 world 10701/10702/10703 × seed401/402. 각 world/seed에서 #117의 baseline15와
preference 모델, vocabulary, raw score, 원래 보정기를 보존한다. 기존 group_split의
calibration 20%와 reserve 20%를 합쳐 동일 fit_calibration(양의 기울기 sigmoid,
raw margin 입력, logistic loss, 기존 bounds/optimizer)으로 새 보정기를 한 번 학습한다.
확대 표본은 해당 모델의 fit 사용자와 서로소여야 하며 두 모델군의 표본도 동일해야 한다.
reserve는 다른 #117 larger arm에서 쓰였지만 이번에 재사용하는 두 모델의 fit에는 쓰이지 않았다.

| arm | 모델 | 보정 표본 | 신규 fit |
|---|---|---|---:|
| baseline15 | 기존 baseline15 | 기존20% | 0 |
| baseline_expanded | 동일 baseline15 | calibration+reserve40% | 6 |
| preference | 기존 preference | 기존20% | 0 |
| preference_expanded | 동일 preference | calibration+reserve40% | 6 |

모델 fit0, 보정 fit12회 상한. expanded끼리의 비교와 baseline 확대 효과는 진단용이며,
채택 대조군을 결과를 본 뒤 바꾸지 않는다. raw score는 각 모델군의 original/expanded가
완전히 같아야 한다. ranking/AUC/AP는 raw, Brier/LogLoss는 확률로 채점한다.
개발24관측, 신규final24관측. #117 원래 개발12관측은 저장 예측을 재사용하고 기존 지표와 대조한다.

## 판정

보정 효과: preference_expanded 대 preference의 Brier 개선 평균>0, 개선≥4/6쌍,
개선 world평균≥2/3, LogLoss 평균 무악화. 동일 raw 및 모든 ranking/AUC/AP 동일성은 필수다.
본 채택: 위 보정 효과와 함께 preference_expanded 대 기존 baseline15에서 #117의
Recall 평균Δ≥0.005, 양수≥4/6쌍·world≥2/3, NDCG@10/24·grouped ROC-AUC·PR-AUC 무악화,
LogLoss/Brier 무악화를 요구한다. 개발과 신규final 각각 이 조건을 통과해야 supported다.
소수점 허용 오차로 실제 악화를 지우지 않는다. 보정 효과와 모델 채택은 따로 보고한다.
기존 score_predictions의 최소30개/20% coverage·양클래스·유한성 기준을 유지한다.
유효성 미달은 uninformative로 final 미수행을 명시한다. 유효한 부정 결과도 고정4arm의 final을 진행한다.

후보·12모델/기존보정/새보정·코드·입력·기준을 봉인한 뒤 cohort11901/11902/11903을
생성한다. 각 validation200명/final800명, 기존 행동 정책의 8/3 anchor, 9/4 평가, 9/5 귀속 tail을
유지한다. 신규 validation은 사용하지 않는다. 모든 알려진 기존 cohort 사용자·평가ID와
중복을 거절한다. cohort별8예측을 전부 봉인한 뒤 final 정답을 열어 채점하고 한 번만 claim한다.
총3claim, 모델/보정 추가fit0, 재선택0, 재채점0. 실제 미래 사용자 관측이 아닌 신규 합성 세계다.

## 진단·비용·운영

양성/음성 Brier의 전체평균 가산 기여와 조건부 평균을 구분한다. 확률 구간은
[0,.01,.025,.05,.1,.2,.5,1]로 고정하고 각 구간의 표본수/평균확률/관측률을 기록한다.
이는 기술 통계이며 bin 기반 calibration 지표를 새로운 채택 조건으로 쓰지 않는다.
기존·확대 표본의 사용자/양성수, 보정 slope/intercept, 6pair 및 world평균을 기록한다.
3world×2seed는 독립 사용자 실험6개가 아니다. 파라미터 분산 감소만으로 일반화 성공을 주장하지 않는다.

학습12시도, 전체 감독7200초, 실험용 유료API0. 실패 attempt는 자동재시도하지 않는다.
기존 데이터와 이전final marker를 변경하지 않고, 공통git claim으로 다른 output의 재실행을 막는다.
OS sandbox는 비활성인 로컬 신뢰코드 실행이다. 코드·원본·예측 hash와 단계 시간을 보존한다.
독립 코드/결과 리뷰·관련 회귀·CI·포트폴리오·squash 머지까지 완료한다.
Windows에서 알려진 변경 밖 전체회귀 실패는 이번 범위에서 수정하거나 전체 재실행하지 않는다.
배포/production 승격·새모델·보정법 교체·기준완화는 범위 밖이다.

참고: [확률 보정과 독립 표본](https://scikit-learn.org/stable/modules/calibration.html).
