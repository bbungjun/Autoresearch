# Judge v2 공통 판정 경로 구현 계획

> 2026-10-20 | 1차 공통 경로 구현 완료·검증 기록 | #148 / Draft PR #149

## 범위와 결정

정본: `../../specs/2026-10-20-judge-zero-variance-guardrail-policy-version.md` rev 3.
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

- [x] 기본 v1 및 기존 decision/reason/delta 보존
- [x] v2 σ 경계·guardrail Δ=0/음수·입력 오류 반환 버전
- [x] 원본 manifest·metadata·ledger prefix·checkpoint·marker bytes/SHA 보존
- [x] Controller → Domain → Judge의 validation/final 버전 및 저장 버전 대조
- [x] 완료 작업 재실행·final 재소비 없이 재개/보고서 재생성
- [x] 실측 명령·결과 및 미완료/환경 한계 기록
- [x] 독립 리뷰와 수정 반영

## 검증 기록

구현 커밋: `b849f3b241dff894b9b1322ad63469270430b7d2`.
기준선: `f06c9ab75461a04df4ccef65bc9ec7bcd90313fa`. Windows / Python 3.12.13.
기존 dev 환경을 `UV_PROJECT_ENVIRONMENT`로 지정하고 `uv run --no-sync`로 실행했으며
의존성·실제 모델·실험 기록은 변경하지 않았다.

- 관련 10개 파일의 pytest (`-n 4 --dist loadfile -q --disable-warnings`): 최종 **458 passed,
  2 warnings / 62.91초**. 파일은 Judge·Domain·run_inputs·ledger·Controller·Controller policy
  integration·local_runtime·report·report_context·report_policy다.
- `ruff check applications autoresearch tests tools`, `git diff --check`: 통과.
- 전체 pytest 최초 실행 (`-n 4 --dist loadfile --durations=25`): **92 failed / 4507 passed /
  137 skipped / 296.27초**. 새 보고서 테스트의 terminal fixture 누락 1개를 수정했고,
  수정 후 관련 전체 458개를 재검증했다. 나머지 실패의 기준선 비교 결과는 아래에 기록한다.
- 로컬 `docker build -f deployment/Dockerfile.app -t autoresearch:ci .`: Docker 엔진의
  named pipe가 없어 시작 불가. 이미지 검증은 PR의 CI 결과를 별도로 확인한다.
- 구현에 참여하지 않은 worker의 독립 리뷰: 런타임 결함 발견 없음. 독립 실행한 6개 파일
  **342 passed / 74.51초**. 추가 연속 흐름 테스트에서 발견한 replay planner fixture를
  최초와 같은 카드로 수정했고, 추가 리뷰에서 발견 사항이 없음을 확인했다.

### 이전 코드로 만든 실제 synthetic 증거

기준선 worktree의 원래 코드로 manifest·metadata·ledger·checkpoint·terminal·보고서를 만든
다음 새 코드에서 load → freeze 재사용 → checkpoint 결속 → terminal load → 보고서 재게시를
실행했다. 실제 Agent·학습·비공개 final은 사용하지 않았다.

- 원본 **22개 파일**의 bytes·SHA 모두 동일, 추가 Agent 호출 **0회**.
- manifest SHA256: `89f860e9cefb57070c63cebc91bdf3250b9205185cf097ff356c390e4af8d0cb`.
- 같은 입력의 v2 정책 요청과 runtime identity 변경은 모두 거부했다.
- 자동 회귀에도 기준선 serializer의 manifest template·ledger byte literal과 과거 보고서
  projection/prompt/render golden SHA를 보존했다. 원본 실행 산출물은 커밋하지 않았다.

### 전체 기준선·CI 비교

같은 환경에서 변경 전 기준선의 전체 pytest를 실행한 결과는 **91 failed / 4270 passed /
137 skipped / 390.44초**였다. 실패 node ID 집합은 후보 최초 실행의 실패 92개에서 수정한
새 보고서 fixture 1개를 뺀 **91개와 정확히 동일**했다. 이 비교는 실패 항목 재현 근거이며,
전체 테스트 통과나 모든 플랫폼에서의 무회귀를 뜻하지 않는다. Windows의 `/bin/sh`·권한·
경로 처리와 기존 loadtest 계약 등 변경 범위 밖 실패는 이번 PR에서 수정하지 않았다.

PR CI는 원격에서 실행 중이다. Lint·의존성 lock/export·Feast/Postgres 전용 pytest는 통과했고,
전체 Python 3.11/3.12 pytest와 이미지 검증은 진행 중인 상태에서 이 기록을 확정했다.
최신 원격 결과는 [PR #149](https://github.com/bbungjun/Autoresearch/pull/149)의 checks를 따른다.

## 후속 의존

목표 모드는 현재 main에 미병합이다. 해당 선행 변경이 병합된 뒤 profile의 `at_bound`,
실행 목적, D5 사전 차단·진단 예외를 별도 구현하고 #147 재검증은 새 profile로 사전 등록한다.
공통 경로 완료를 spec 전체 구현 완료로 표현하지 않는다.
