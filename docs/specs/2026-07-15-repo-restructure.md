# 저장소 구조 재정리 Spec

- 날짜: 2026-07-15
- 이슈: #149
- 상태: 1단계(문서 통합·잔재 정리) 구현 완료, 2단계(`src/` 패키지 통합)는 기술적 전제 확인 대기

## 배경

과거 팀 운영 시점에 기능(도메인)별로 병렬 작업하면서 두 가지 구조 문제가 누적되었다.

1. **문서 분산** — 문서가 6곳에 흩어져 있었다: `docs/` 루트 평면 나열,
   과거 체계(`docs/superpowers/{specs,plans}`)와 현행 체계(`docs/{specs,plans}`)의
   병렬 공존, 코드 디렉토리 내부 문서(`autoresearch/action_log_generation/docs/`,
   `autoresearch/virtual_user_generation/docs/`).
2. **소스 트리 이원화** — 수집·생성 계열은 `autoresearch/` 패키지, CTR 학습
   계열은 `src/` 디렉토리로 나뉘어 import 경로 규칙이 두 개가 되었고, 신규
   코드의 배치 기준이 모호해졌다.

## 결정 1 — 문서: 유형 기반 구조 + archive (구현 완료)

문서 분류 축으로 세 가지를 검토했다.

| 대안 | 판단 |
|---|---|
| **유형 기반 유지 + `archive/`·`guides/` 신설 (채택)** | `adr/`·`specs/`·`plans/` 체계가 이미 CLAUDE.md에 규정되어 있어 변경 최소 |
| 도메인 기반 재편 (`docs/training/` 등) | spec/plan 규칙과 교차축 발생, CLAUDE.md 규칙 개정 필요, 이동량 과다 |
| Diátaxis 4분류 | 소규모 내부 프로젝트에 과함 |

확정된 구조와 수명 규칙은 [`docs/README.md`](../README.md)가 단일 출처다.
핵심 규칙:

- `specs/`에는 **살아있는 계약**만 남긴다. 구현 완료된 spec/plan은
  `archive/specs/`, `archive/plans/`로 옮긴다.
- 일회성 리포트(QA, 실증 테스트, 발표 자료)는 `archive/reports/`에 둔다.
- 코드 디렉토리 안에 문서를 두지 않는다. 모듈 사용법은 `docs/guides/`에 둔다.
- 아카이브 문서는 역사적 기록으로, 내용을 갱신하지 않는다.

## 결정 2 — 잔재 정리 (구현 완료)

- `autoresearch/math_utils.py` + `tests/test_math_utils.py` 삭제 — 온보딩 데모
  잔재(커밋 "feat: 덧셈 함수 추가")로 실사용처가 없었다.
- `dags/` 로컬 잔재 삭제 — #142에서 레거시 DAG 표면 제거 후 `__pycache__`만
  남아 있었다.
- `scratchpad/`를 `.gitignore`에 추가.
- `README.md`를 실제 진입점(구조 지도, 코드 영역별 책임, 시작 명령, 문서 인덱스
  링크)으로 재작성.

## 결정 3 — `src/`를 `autoresearch/` 패키지로 통합 (기술적 전제 확인 후 실행)

> **대체됨 (2026-08-13)** — 이 결정은
> [`docs/specs/2026-08-13-repository-structure-redesign.md`](2026-08-13-repository-structure-redesign.md)로
> 대체되었습니다(#754). 아래 목표 구조는 **채택되지 않았습니다** — `src/pipeline`을
> `autoresearch/training/` 한 폴더로 합치는 안이었고, `applications/` 층과 서빙·proxy
> 이동은 범위 밖이었습니다. 실제로는 파이프라인 단계 축으로 나눴습니다.

### 목표 구조

```
autoresearch/
├── youtube_collection/   # 그대로
├── virtual_users/        # 그대로
├── action_logs/          # 그대로
├── jobs/                 # 그대로 (+ src/cli.py 진입점 흡수 검토)
├── features/             # ← src/features
├── models/               # ← src/models
├── training/             # ← src/pipeline (train, evaluate, build_training_dataset, config.yaml)
├── tracking/             # ← src/tracking
└── utils/                # ← src/utils
```

### 근거

- 단일 패키지 네임스페이스(`autoresearch.*`)로 import 규칙이 하나가 된다.
- `src.pipeline`은 이름이 역할(모델 학습)을 드러내지 못하므로 `training`으로
  개명한다. 데이터 파이프라인(`action_logs.pipeline` 등)과의 혼동도 없앤다.
- `Dockerfile.app`·CI가 이미 `autoresearch` 패키지를 기준으로 동작하므로 배포
  표면 변화가 없다.

### 유지하는 최상위 디렉토리

`feature_repo/`(Feast 규격), `proxy/`(별도 Cloud Run 배포 단위),
`deploy/`(배포 산출물), `examples/`(비패키지 스캐폴드), `scripts/`(비패키지
스크립트)는 각자 독립 배포·규격상의 이유가 있으므로 최상위에 유지한다.

### 실행 조건

- Model Training / Feast Features의 코드 경계와 기존 계약을 보존한다.
- 진행 중인 학습 파이프라인 브랜치가 머지된 직후 실행해 충돌을 최소화한다.
- 상세 체크리스트: [`docs/plans/2026-07-15-src-package-merge.md`](../archive/plans/2026-07-15-src-package-merge.md)

## 범위 제외

- `src/` 코드의 동작 변경(이동은 순수 구조 변경으로만 진행)
- `feature_repo/`, `proxy/`, `deploy/`, `examples/` 구조 변경
- `.claude/docs/` 에이전트 가이드 체계 개편
