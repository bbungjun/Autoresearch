# #117 실행 계획

1. #113/#115, 저장소 지침·모델/평가 계약 확인 및 독립 설계 검토.
2. spec과 72fit/7200초/API0 예산을 커밋해 고정한다. 기존 산출물 위치·hash와 런타임을 읽기 검증한다.
3. 별도 worker가 group split·6arm·보정·선호피처와 집중 테스트를 구현한다.
   coordinator는 단계별 증거 실행기, 예산·claim·선택·신규 final 봉인을 구현한다.
4. 독립 구현 리뷰와 관련 회귀/Ruff를 통과한 코드에서만 실험을 시작한다.
5. 기존 bundle/validation으로 36model+36calibration만 학습하고 저장 prediction으로 개발 비교한다.
6. 단일 후보·모델·코드·전처리·평가기준을 봉인하고 새 cohort를 생성·봉인 평가한다.
7. 저장 결과를 독립 재집계하고 원본 hash·비용·재실행 방지를 검증한다. 모델/평가를 재실행하지 않는다.
8. 결과·한계·문제/해결/검증 근거를 보고서와 기존 포트폴리오에 기록한다.
9. 이슈 #117에 PR #118을 연결하고 전체 테스트 및 CI, 독립 리뷰 발견 사항 해결 후 squash merge한다.
   사용자가 머지를 명시적으로 요청했으므로 별도 승인 대기는 만들지 않는다.

실행 전 검증: 관련 회귀119개(79+40) 및 전체 Ruff 통과. 독립 리뷰에서 입력 pin,
미수행 표시, 실패 비용 기록을 보완했고 재리뷰에서 추가 P1/P2 없음.
독립 집중 회귀89개 통과.

실험 완료 기록: 36model+36calibration=72fit, 개발36관측과 신규final18관측,
신규claim3회, 유료API0회. 감독 프로세스235.290초. 후보 preference의 신규final
Recall 개선에도 Brier 무악화 조건이 미달하여 not_supported로 판정했다.
단계1~8을 수행하고 독립 결과 재집계의 일치를 확인했다. 실행 commit8262d0e의
Python3.11/3.12 각4339passed/139skipped와 모든 대상 CI를 통과했다.
결과·미수행·한계는 [실측 보고서](../../reports/2026-09-06-recall-controlled-experiment.md),
최종 CI·squash merge 상태는 [PR #118](https://github.com/bbungjun/Autoresearch/pull/118)이 정본이다.
