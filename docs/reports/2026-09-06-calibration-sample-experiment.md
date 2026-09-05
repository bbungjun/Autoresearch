# 고정 모델의 보정 표본 확대 실험 (#119)

상태: 구현·독립 코드 리뷰 완료, 실제 실험 실행 전.

## 문제

#117에서는 선호 피처가 추천 순위를 개선했으나 final Brier가 0.000100439
악화하여 채택되지 않았다. 기존 보정 표본의 클릭 정답은 모델별 9~14개였다.
작은 보정 표본이 확률 추정의 불안정에 영향을 주는지 별도 검증이 필요하다.
Brier는 판별력과 보정을 함께 반영하므로 이것만으로 원인을 단정하지 않는다.

## 해결과 검증 설계

[사전 등록 계약](../specs/2026-09-06-calibration-sample-experiment.md)에 따라
모델을 고정하고 보정 표본만 기존 20%에서 calibration+reserve 40%로 확대한다.
모델 fit은 0회, 보정 fit은 12회이며 기존 fit 사용자는 제외한다.
기본 모델과 선호 피처 모델 각각 기존/확대 보정을 비교한다.
개발 24관측과 신규 final 24관측으로 보정 효과와 기존 기준의 채택 여부를 분리한다.
보정법 변경·추가 후보·기준 완화·기존 final 재사용은 하지 않는다.

원본 모델/입력 pin, 같은 raw score, 8예측 봉인 후 정답 개봉, 신규 cohort 3회
단일 소비와 공통 Git claim을 구현했다. 독립 리뷰에서 과거 cohort manifest pin과
과거 평가 ID 제외 목록 누락을 발견하여 수정했다.

## 현재 검증과 남은 작업

관련 회귀 108 passed, 전체 Ruff·diff check 통과. 독립 리뷰에서 새 테스트
19 passed를 확인했으며 미해결 P1/P2는 없다. 실제 실험·결과 재집계·CI는 아직
수행하지 않았다. 실행 후 표본수, 지표, 비용, 원본 보존과 한계를 기록한다.

근거: [scikit-learn 확률 보정 문서](https://scikit-learn.org/stable/modules/calibration.html).

첫 CLI 기동은 supervisor의 keyword-only timeout을 위치 인자로 전달하여 TypeError로
종료했다. worker 생성 전이므로 모델/보정 fit·final claim·실험 출력 생성은 0이다.
호출을 수정하고 실제 supervisor 시그니처의 autospec으로 CLI 회귀를 추가했다.
이는 학습 실패 재시도가 아닌 학습 전 기동 오류 수정이며, 실패 기록을 보존한다.
