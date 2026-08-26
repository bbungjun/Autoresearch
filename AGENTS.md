# Coding Guidelines for AI Coding Agents

> Version: 1.3.0 | Last Updated: 2026-08-26

이 문서는 Claude Code 등 AI 코딩 에이전트가 이 저장소에서 작업할 때의 기본
진입점입니다. 여기에는 규칙·함정·근거만 남깁니다. 저장소 사실(디렉토리 구조,
배포 이미지, 저장소 책임 경계)는 정본 문서를 가리키고 여기에 복제하지 않습니다.

## Language Preference

에이전트 응답, PR 코멘트, 리뷰 요약, 구현 노트는 한국어 격식체를 사용합니다.
사용자가 명시적으로 요청하는 경우에만 다른 언어를 사용합니다.

## Rule Priority

규칙이 충돌하면 다음 순서로 적용합니다:

1. 사용자의 명시적 요청
2. `CLAUDE.md` 및 `AGENTS.md`
3. `.claude/docs/` 하위 가이드
4. `README.md`, 소스 주석, 기타 저장소 문서

같은 수준이면 더 구체적이고 더 최근에 갱신된 규칙을 우선합니다.

## Documentation Navigation

비자명한 변경을 하기 전에 가장 관련 있는 문서를 먼저 확인합니다:

| 요청 유형 | 먼저 볼 문서 | 다음 문서 |
| --- | --- | --- |
| 프로젝트 구조·저장소 경계·배포 이미지 | `README.md` | `.claude/docs/agent-project-reference.md` |
| 폴더별 책임·소유 경계 | `.claude/docs/agent-project-reference.md` | `.claude/docs/architecture-overview.md` |
| Python 스타일, 타이핑, 로깅 | `.claude/docs/agent-python-reference.md` | `.claude/docs/coding-conventions.md` |
| 워크플로우, spec, plan, 커밋, PR | `.claude/docs/agent-workflow-reference.md` | `.claude/docs/agent-prohibitions.md` |
| 문서 배치·수명 (specs/plans/archive) | `docs/README.md` | — |
| 보안, 시크릿, 외부 입력 | `.claude/docs/agent-security-guidelines.md` | `.claude/docs/agent-prohibitions.md` |
| 에러 처리 | `.claude/docs/agent-error-handling-reference.md` | 관련 소스 파일 |

## Project Context

Autoresearch는 YouTube 트렌딩 데이터 기반 CTR 모델링 프로젝트입니다.
수집 → 가상 유저 → action log → 학습 데이터셋 → 학습/평가 → 리랭킹 서빙 →
일일 추천·노출 시뮬레이션이 다시 action log로 돌아오는 **일일 폐루프**를
구성합니다. 구조 지도와 저장소 책임 경계는 `README.md`가 정본입니다.

## Repository Operating Mode

- 현재 기준 저장소와 `origin`은 개인 저장소
  [`bbungjun/Autoresearch`](https://github.com/bbungjun/Autoresearch)입니다.
- 팀원 GitHub Approve, 조직 Project, 조직 GitHub App, GCP 배포 자동화는 현재
  개인 저장소의 운영 전제가 아닙니다. 사람이 독립 리뷰 결과와 검증 근거를
  확인해 최종 반영 여부를 결정합니다.
- 현재 활성 GitHub Actions는 `ci.yml`, `lint.yml`, `release-drafter.yml`입니다.
  `.github/workflows-disabled/`의 파일과 문서에 남은 조직 자동화 설명은 복구
  근거를 위한 역사적 기록이며, 활성 워크플로우로 간주하지 않습니다.
- `SKYAHO/Autoresearch`, `SKYAHO/Autoresearch-airflow`,
  `SKYAHO/Autoresearch-infra` 참조는 이전 조직 저장소 또는 인접 시스템의
  아키텍처 계보를 나타냅니다. 접근 가능성이나 연동 상태를 가정하지 말고,
  사용자의 명시적 요청 없이 해당 저장소나 외부 인프라를 변경하지 않습니다.
- 현재 GitHub 작업 절차의 정본은 `CONTRIBUTING.md`와
  `.claude/docs/agent-workflow-reference.md`입니다. 두 문서에서 "현재 비활성"으로
  표시한 절차를 실행하지 않습니다.

**저장소 경계 (여기서 자주 틀립니다):**

- 아키텍처상 DAG·schedule·retry·timeout·Pool·KubernetesPodOperator·Airflow
  배포는 인접 저장소 `SKYAHO/Autoresearch-airflow`의 책임이었으며, 이
  저장소는 배포 이미지와 `autoresearch.jobs.*` 공개 CLI만 제공합니다.
  현재 개인 저장소에서는 해당 조직 연동이 비활성입니다.
- GCP 인프라(IAM, K8s 리소스, 시크릿 기반)는 인접 저장소
  `SKYAHO/Autoresearch-infra`의 책임이었으며, 현재 개인 저장소에서 접근이나
  배포 연동을 전제하지 않습니다.
- 공개 batch 명령·인자 계약은
  `docs/specs/2026-07-13-public-batch-execution-contract.md`를 따릅니다.
- action log 데이터 레이크 파티션 계약(#295 A안): `dt=D`는 KST D일 하루치
  슬라이스(파티션 간 서로소)이며, 소비자는 `dt BETWEEN P-30 AND P-1` +
  timestamp 윈도우로 30일 히스토리를 조립합니다. event_id는
  `{prefix}_{YYYYMMDD}_{seq:08d}` 전역 고유 형식입니다. 정본:
  `docs/specs/2026-07-24-action-log-slice-semantics.md`

## Project Vision & Phase

Autoresearch의 최종 목표는 **ML 리서처·엔지니어를 위한 자율 실험 에이전트
서비스**입니다. 사용자가 가설 한 줄(예: 추천 알고리즘 논문)을 입력하면,
에이전트가 raw 데이터로 피처를 재조립·가공하고, 모델·임베딩 방식을 선택해
학습한 뒤, origin(champion) 모델과의 비교·A/B 테스트까지 스스로 판단해
수행합니다. 일일 폐루프는 이 에이전트가 실험을 돌리기 위한 **기반
테스트베드**이지 그 자체가 최종 목표가 아닙니다.

현재 단계는 **MVP — 기반 폐루프 완주**이며, 이후 최적화 → 기술 고도화 →
에이전트 자율 실험 순서로 나아갑니다.

- 아키텍처·인프라·디렉토리 구조는 **유동적**입니다. 문서의 구조 서술은
  현황 스냅샷이지 제약이 아닙니다.
- 문서와 코드가 다르면 **코드가 사실**입니다. 문서를 근거로 코드를
  되돌리지 말고, 문서 갱신을 제안합니다.
- 폐루프 완주를 앞당기는 구조 변경 제안을 주저하지 않습니다. 단, 실행은
  기존 워크플로우(이슈·spec·저장소 소유자의 결정)를 따릅니다.

## Core Rules

- GitHub에서 이슈·브랜치·PR을 생성·수정·삭제하기 전에는 `CONTRIBUTING.md`와
  `.claude/docs/agent-workflow-reference.md`를 모두 읽고, 작업 유형과 Issue Form 또는
  CLI 요구사항(제목 prefix, label, assignee)을 확인합니다. 별도 지시가 없으면
  대상 저장소는 `bbungjun/Autoresearch`입니다.
- 코드가 변경되는 작업은 반드시 이슈를 먼저 발행하고, 그 이슈의 `Create a
  branch`로 브랜치를 생성합니다(이슈-브랜치 자동 연결). 상세는
  `.claude/docs/agent-workflow-reference.md` 참조.
- 모듈 최상단 docstring에 ① 이 모듈이 **전체 파이프라인 기준으로 어느
  구간을 담당하는지**(담당하지 않는 인접 책임 포함), ② 모듈이 제공하는
  기능을 모듈 단위로 서술합니다. 새 모듈은 생성 시, 기존 모듈은 기능을
  추가·변경하는 같은 커밋에서 docstring을 갱신합니다. 형식은
  `.claude/docs/agent-python-reference.md`의 Module Responsibility 참조.
- 새 추상화보다 기존 저장소 패턴을 우선합니다.
- 구조 변경과 동작 변경은 분리합니다.
- Python 함수의 타입 힌트(반환 타입 포함)를 유지합니다.
- 요청된 변경에 필요하지 않은 광범위한 리팩터링은 피합니다.
- 시크릿, 로컬 데이터 경로, 생성된 데이터 파일, `.env`를 커밋하지 않습니다.
- 새 최상위 디렉토리, `Dockerfile.*`, 공개 batch CLI, 필수 환경 변수를
  도입하는 PR은 **같은 PR에서** `README.md`와
  `.claude/docs/agent-project-reference.md`를 갱신합니다.
- 문서에 구체적 예시(요청/응답 JSON, 필드명, 타입, 스키마)를 적을 때는
  해당 계약 정본(pydantic 모델, spec)을 열어 대조한 뒤 커밋합니다. 검증
  대상은 의심되는 것이 아니라 **단언하는 모든 것**입니다.
- 구현이 완료된 spec/plan은 `docs/archive/`로 옮깁니다. 문서 배치·수명
  규칙은 `docs/README.md`가 정본입니다.

## Local Development

```bash
uv sync                                    # .venv 생성 + 런타임/dev 의존성 (uv.lock 기준)
uv run python -m pytest                    # CI pytest job과 동일
uv run --no-sync ruff check applications autoresearch tests tools   # CI lint job과 동일
```

- 의존성 변경은 `pyproject.toml` 수정 → `uv lock` → 산출물 갱신 순서로
  진행합니다. `applications/youtube_api_proxy/requirements.txt`는 파일 헤더의 `uv export` 명령으로
  재생성하고, `deployment/mlflow/runtime`은 자체 lock을 가집니다 — CI
  `uv lock & proxy export drift` job이 둘 다 검사합니다.
- Feast는 dev 그룹과 의존성 충돌(feast 0.64의 starlette>=1.0 ↔ dev/proxy의
  fastapi<0.129)로 **격리 그룹**입니다: `uv sync --only-group feast`.
  feast 계열 테스트는 dev 환경에서 `pytest.importorskip`으로 skip되며, CI
  `pytest (feast group)` job이 전용 테스트 목록을 별도 실행합니다.
- 환경 변수의 단일 출처는 `.env.example`입니다. 여기에 나열하지 않습니다.

## Spec / Plan First

비자명한 변경(광범위한 동작 변경, 마이그레이션, 모듈 간 계약, 공개 API, 대규모
다중 파일 수정)은 구현 전에 계획을 작성합니다.

- 요구사항, 설계 결정, 동작 계약, 아키텍처 노트 →
  `docs/specs/YYYY-MM-DD-<slug>.md`
- 구현 순서, 작업 분해, 검증 체크리스트 →
  `docs/plans/YYYY-MM-DD-<slug>.md`
- 새로 만들기보다 기존 관련 spec/plan 갱신을 우선합니다.

문서만 수정하거나 범위가 좁은 워크플로우 변경은 스레드 내 짧은 계획으로
진행할 수 있습니다.

## Verification

변경을 증명하는 가장 좁은 검증부터 수행하고, 공유 동작이나 사용자
워크플로우에 영향이 있으면 범위를 넓힙니다.

```bash
uv run python -m pytest -v                          # CI와 동일
uv run --no-sync ruff check applications autoresearch tests tools # CI lint와 동일
docker build -f deployment/Dockerfile.app -t autoresearch:ci . # CI 이미지 빌드 검증
```

- feast 계열 변경 시: `uv sync --only-group feast` 환경에서 CI
  `pytest (feast group)` job의 테스트 목록(`.github/workflows/ci.yml`)을
  실행합니다.
- GitHub 워크플로우·문서 변경 시 추가로 `git diff --check`.
  `actionlint`가 로컬에 있으면 함께 사용합니다.

## Review Guidance

PR 리뷰 시 심각도 순으로 구체적 발견 사항을 먼저 제시합니다. 중점 사항:

- 정확성 버그와 기존 동작의 의도치 않은 변경
- 시크릿·자격 증명 처리 위험
- 데이터 스키마/계약(pydantic 모델, parquet 스키마) 변경 위험
- 변경된 동작에 대한 테스트 누락·부실
- 타입 안정성 문제
- 전제 조건과 영향이 명확한 성능 문제
- 새 디렉토리·이미지·공개 CLI·필수 환경 변수를 도입하면서 `README.md`와
  `agent-project-reference.md`를 갱신하지 않은 문서 드리프트

구체적 코드 이슈는 인라인 코멘트로, 요약 코멘트는 짧게 유지합니다.
