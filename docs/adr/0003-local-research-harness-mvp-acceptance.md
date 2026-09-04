# ADR 0003: 로컬 Research Harness MVP 수용 경계

- **상태**: Accepted
- **날짜**: 2026-09-05
- **이슈**: #17

## 배경

로컬 Research Harness는 사람이 작성한 가설과 `ExperimentCard`에서 시작해 candidate 코드
수정, 반복 학습, 봉인된 validation/final 판정, 실패 복구·checkpoint 재개, ledger와 REPORT
생성까지 연결됐다. 남은 선택은 실측으로 확인한 판정 정책과 증거 수준을 MVP 완료에 충분한
것으로 수용할지, 더 넓은 제품·운영 요구를 같은 완료 조건에 포함할지였다.

## 결정

**로컬 Research Harness MVP**를 #17과 spec §12의 범위로 수용한다. `2σ` primary,
`-1σ` guardrail, `σ > 1e-6`과 지표별 coverage 정책을 유지한다. #60은 모든 final 지표가
개선됐어도 NDCG@10 개선폭이 고정 threshold보다 작아 기각했고, #69는 Recall@10의 σ=0에서
판정을 중단했으며, #71은 threshold를 충분히 넘은 새 피처만 승격했다. 이 세 결과는 정책이
근소한 개선·불충분한 baseline noise·명확한 개선을 서로 구분한다는 실용 근거다. 통계적
최적성이나 실제 사용자 효과를 뜻하지 않으며, 변경은 향후 실험 전에만 적용한다.

MVP 품질 근거는 버전이 고정된 합성 fixture의 offline 결과로 제한한다. 실행 비용 근거는
관측된 wall-clock과 token이고, provider가 가격을 제공하지 않은 달러 비용은 `null`을
유지한다. #69와 #71의 고정 실행 구간에서 관찰한 중간 승인·수동 코드 수정·수동 재시작
0건을 자율성 근거로 수용하되 자동 `human_interventions` 값은 `null`이며 범용 무인 실행으로
확대하지 않는다.

MVP의 실행 예산은 새 trial 수·trial 시작 시간과 subprocess timeout을 강제하는 현재 계약이다.
CPU/GPU/저장공간 hard scheduling, 자동 외부 개입 감지와 가격 환산은 후속 최적화다. #16의
다섯 비교군·개인화 ablation·여러 Judge, #90의 임의 장경로 candidate destination, 기존
executor 연결, 논문 발견·PaperCard·웹, production champion 전환과 온라인 A/B도 MVP 이후로
분리한다.

## 결과

#17은 문서·독립 리뷰·CI가 이 결정과 일치한 뒤 완료한다. 완료 처리에서 기존 final marker,
평가 기록과 판정을 수정하거나 재소비하지 않는다. 이 ADR의 수용은 후속 이슈를 완료했다는
뜻이 아니며, 새 운영·제품 근거가 생기면 별도 ADR로 범위를 확장한다.
