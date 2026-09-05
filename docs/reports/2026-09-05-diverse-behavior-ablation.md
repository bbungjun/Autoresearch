# #113 다양한 행동 데이터의 최근 행동 피처 ablation

## 문제

#105에서는 과거 이력을 날짜순으로 만들었으나 행동 분산이 제한적이어서 최근 행동 피처의 학습 효과를 충분히 검증하지 못했다. #107에서 다양한 행동 이력을 만들고 #109에서15/10피처 학습 bundle을 고정했으며 #111에서 신규 평가 cohort를 봉인했다. 이번 작업은 준비한 입력이 실제 모델 비교에서 어떤 결과를 내는지 검증한다.

## 고정한 실험과 구현

사전 등록 문서의 복사본 SHA256 `a1490bca5ebbe8114f6a3619dca6f3684b9eac4cecbcb18eb95af6abd0f624aa`를 유지한다. 3개 학습 world10701/10702/10703의9/2 학습 입력, seed401/402/403, 15피처(with_recent) 대10피처(without_recent)와 기본 LightGBM을 사용한다. E5 small의 실제 모델/실행 identity를 학습 bundle과 대조한다. 평가 cohort10901/10902/10903과9/4 평가·9/5 귀속 tail은 #111의 봉인 산출물을 그대로 소비한다.

모델18개를 전부 학습해 receipt를 봉인한 뒤 validation 전체18관측을 계산한다. Primary 개선 여부와 관계없이 행key/평가ID/coverage/유한성이 유효한 경우에만 final로 진행한다. Final은 원래 snapshot registry에서 cohort별 한 번, 최대3회 claim한다. 각 cohort의6개 예측 CSV를 모두 봉인한 뒤 채점하며 같은 저장 모델을 재사용한다. 모델 재학습이나 데이터 재생성을 하지 않는다.

실행기와 분석 코드를 분리해 고정 그리드·동일 표본·guardrail 방향·표본 부족을 검증한다. 유효한 개선 실패는 not_supported, coverage 미달/비유한 지표는 uninformative로 구분한다. 9개의 seed pair와3개 world 평균의 표본 표준편차를 별도로 기록하며 독립 사용자 실험9개로 해석하지 않는다.

Git 공통 경로의 평가 묶음 hash 기반 실행 claim으로 다른 출력/worktree의 반복 실행을 거절한다. 감독 프로세스는30분에 실행 프로세스 트리를 회수하고 실패·시간초과의 산출물을 보존한다. Candidate/Judge 파일 역할은 나누지만 Codex 및 실행 프로세스에 OS sandbox는 적용하지 않은 로컬 신뢰코드 실행이다. 외부 유료 API와 serving 배포는 사용하지 않는다.

## 사전 검증

실행/분석 회귀33 passed(0.88초), 독립 실행33 passed(0.89초), Ruff 통과. 테스트는 같은 관측을 중복 집계하지 않는다. 전체18fit 선행, validation 전체 검사 후final, 유효한 부정 결과도final 진행, final6예측 선행봉인, 잘못된hash/중복claim/timeout 중단을 확인했다. 독립 코드 리뷰에서 P1/P2 발견 사항은 없었다.

실물 입력은3개 cohort의 원본·metadata·snapshot·학습bundle hash를 읽기 검증했으며, 이 단계에서는 학습·채점·finalclaim을 하지 않았다.

## 실행 결과

실행 전 기록이다. 결과·호출 수·시간·판정과 독립 검증은 단일 실행 후 기록한다.

## 한계와 후속

이는 rule-based 합성 사용자에서의 고정 ablation이며, 실제 CTR·LLM 평가·장기 폐루프 효과·production champion 승격을 입증하지 않는다. 기존 #16/#103/#105 결과와 final을 초기화하지 않는다. 결과가 불리해도 seed나판정기준을 바꾸지 않는다.
