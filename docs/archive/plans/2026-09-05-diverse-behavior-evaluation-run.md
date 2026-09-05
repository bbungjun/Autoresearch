# #113 다양한 행동 ablation 실행 계획

상태: 구현·실제 학습/평가·독립 코드 및 결과 리뷰 완료. CI와 머지 상태는 #113의 연결 PR에서 추적한다. #109에서 동결한 사전 등록의
수치·seed·날짜·판정은 변경하지 않는다. 준비 문서의 당시 미승인 상태는 이번 사용자의
평가 실행 요청으로 해소됐으며, 동결 복사본 bytes는 유지한다.

## 고정 입력과 완료 기준

- 학습: #109의 3개 bundle hash(기존 prepare 도구 BUNDLE_HASHES 계약), seed401/402/403.
- 평가: #111 summary SHA256 `459c938eaf3a0d00b56fe46327a023745acddaae06e6afb904358f103b2c7d89`.
  각 world의 raw/snapshot/candidate manifest hash를 summary와 대조한다. 원본 재생성 없음.
- 모델: 기존 behavior_execution의 고정 15/10피처 API. 총18개 모델을 먼저 fit/봉인하고
  동일 모델을 validation/final에 재사용하고, 각 split의 feature batch를 두 arm이 공유한다.
- validation 18개 관측의 행 key·평가 ID·coverage·유한성 검증 후만 final claim을 한다.
  개선 여부는 final 실행 gate가 아니다. final은 cohort별6모델 묶음, 최대3claim이다.
  Final 예측6개를 전부 CSV로 봉인한 뒤 채점하고 seed/model/arm을 추가하지 않는다.
- 고정36관측이면 supported/not_supported/표본부족 uninformative를 판정하고,
  실패/시간초과에는 현재 산출물·호출 수·실패 단계를 남긴다. 자동 재학습/재채점 없음.

## 실행 설계

1. 사전 입력 검증과 관련 회귀, 독립 리뷰를 완료한 깨끗한 commit을 고정한다.
2. Git 공통 경로에 평가 summary identity 기준 실행 claim을 원자 생성해 다른 출력/worktree의
   반복 실행을 차단한다. 18모델 receipt와 파일 hash를 모델 묶음으로 봉인한다.
3. Candidate v2 입력만 읽는 피처 조립으로 실제 CUDA E5 identity를 확인한다.
   snapshot 정답과 raw latent는 Judge측만 읽고, 모델 함수에는 피처·허용 학습 label만 전달한다.
4. 모델·피처 batch·예측 CSV hash를 기록한다. validation 전체유효성 후 기존 registry에서
   cohort별 final을 claim하고 동일 모델을 재사용한다. 기존 marker는 변경/삭제하지 않는다.
5. 감독 프로세스가 실행 프로세스를30분에 종료한다. fit18/유료API0을 코드와 receipt로 확인한다.
   실측·독립 결과 검토·문서·CI·squash 머지를 완료한다.

## 검증

- 가짜 domain/모델로 실행 순서, 모든 validation 후 final, 18fit 재사용, 실패/coverage미달 시
  final미소비, 6개예측봉인후final채점, hash변조·중복claim·시간초과 거절을 검증한다.
- 통계는 등록36그리드, 동일평가ID/row key/coverage, guardrail방향, 최소30/20% coverage,
  seed9쌍과world평균3개의 표준편차를 구분한다. 표본부족은 not_supported와 구분한다.
- 실제실행은 한 번만 한다. 결과 복구는 봉인된 원시지표의 재집계만 허용하고 final 재채점은 하지 않는다.

## 환경과 범위

Codex 샌드박스는 비활성인 로컬 신뢰코드 실행이다. 별도 worktree와 Judge/candidate 파일
경계를 사용하지만 OS 수준 격리를 주장하지 않는다. Serving 배포와 production champion
승격은 범위 밖이다. 실제 사용자 일반화·장기 폐루프 효과·LLM 자율 피처 탐색의 증거로 확대하지 않는다.

## 완료 근거

- 실행 commit `ad2db3d99e28ea7ba5c5f2471ede85f13173298a`에서 18모델·36평가·3final 소비를 완료했다.
- 실행부 155.321초, coverage 유효, 고정 판정 `not_supported`다. Final NDCG 평균은 상승했지만 개선 5/9쌍과 Recall 감소로 기준에 미달했다.
- 관련 회귀 33개와 Ruff가 통과했고, 독립 코드·실제 결과·최종 문서 리뷰에서 P1/P2 발견 사항은 없었다.
- 원본 해시와 final marker를 보존한다. 재학습·재채점 없이 저장 지표만 독립 재집계했다.
- 상세 수치·한계는 [실측 보고서](../../reports/2026-09-05-diverse-behavior-ablation.md)에 보존한다.
