# Judge v2 공통 판정 경로 구현 계획

> 2026-10-20 | 진행 중 | #148

## 범위와 결정

정본: `../specs/2026-10-20-judge-zero-variance-guardrail-policy-version.md` rev 3.
이번 범위는 §8의 2단계 공통 경로이며, 목표 모드 연결·D5 preflight·#147 재실행·실제
final 소비·배포·PR 병합은 포함하지 않는다. 기본 정책 v1, 명시 선택 v2, v2의 변동 없는
guardrail 허용 폭 0.0과 primary 2σ·기존 허용 오차를 유지한다.

## 구현 순서

1. 기존 코드로 synthetic 입력·ledger 증거를 확보하고 v1 회귀 기준을 고정한다.
2. Judge·Domain에 정책 버전을 추가하고 v1/v2 분기와 오류 반환 버전 보존을 구현한다.
3. run_inputs의 형식을 분리하고 기존 원본 bytes·SHA와 ledger의 멱등성을 보존한다.
4. Controller와 로컬 config에서 두 비교 경로에 버전을 전달하고 기록과 보고서를 연결한다.
5. 좁은 단위 테스트 → 공통 경로 통합·중단/재개·보고서 호환성 → 전체 pytest·ruff·이미지
   빌드를 검증한다. 기존 환경 실패는 변경 전 기준선과 비교한다.
6. 구현에 참여하지 않은 worker의 독립 리뷰를 반영하고 커밋·push·Draft PR을 만든다.

## 수용 체크리스트

- [ ] 기본 v1 및 기존 decision/reason/delta 보존
- [ ] v2 σ 경계·guardrail Δ=0/음수·입력 오류 반환 버전
- [ ] 원본 manifest·metadata·ledger prefix·checkpoint·marker bytes/SHA 보존
- [ ] Controller → Domain → Judge의 validation/final 버전 및 저장 버전 대조
- [ ] 완료 작업 재실행·final 재소비 없이 재개/보고서 재생성
- [ ] 실측 명령·결과 및 미완료/환경 한계 기록
- [ ] 독립 리뷰와 수정 반영

## 후속 의존

목표 모드는 현재 main에 미병합이다. 해당 선행 변경이 병합된 뒤 profile의 `at_bound`,
실행 목적, D5 사전 차단·진단 예외를 별도 구현하고 #147 재검증은 새 profile로 사전 등록한다.
공통 경로 완료를 spec 전체 구현 완료로 표현하지 않는다.
