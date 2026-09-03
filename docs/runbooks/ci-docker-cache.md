# CI Docker 반복 빌드 개선 — 문제 해결 기록과 운영 절차

관련 이슈: [#81](https://github.com/bbungjun/Autoresearch/issues/81).
기록일: 2026-09-04. 적용 대상은 개인 저장소의
[CI 워크플로우](../../.github/workflows/ci.yml)이다.

이 문서는 포트폴리오 작성과 후속 운영 검증을 위한 문제 해결 기록이다.
구현·로컬 검증·독립 리뷰 기록과 원격 CI 검증 결과를 함께 보존한다.
구현은 PR #82로 main에 반영했으며, 후속 측정은 아래 원격 검증 절에 기록한다.

## 사례 요약

Autoresearch는 수집·학습·서빙과 실험 에이전트의 실행 환경을 여러 Docker
이미지로 관리한다. CI에서는 이 이미지들이 빌드되고 실제로 기동하는지 확인한다.
작은 수정도 main 반영 때마다 전체 이미지 검증으로 이어져, 변경되지 않은
환경을 반복해서 구성하는 비용이 발생했다.

이번 작업에서는 문제를 **불필요한 빌드 횟수**와 **필요한 빌드 안의 반복 설치**로
나누었다. 변경 경로에 따른 이미지 선택과 실행 간 레이어 캐시를 함께 적용하고,
이미지에 기록하는 커밋 정보가 설치 캐시를 깨지 않도록 Dockerfile 순서를 바꿨다.
동시에 현재 코드의 실행 검증을 유지하고, 선택 빌드에서 빠질 수 있는 입력 경로와
최초 push 예외를 독립 리뷰로 점검했다.

로컬 검증 후 원격 PR CI에서 9개 이미지의 빌드·실행 검증을 통과했다.
같은 커밋의 재실행에서는 빌드 단계 합계가 최초 1,188초에서 345초로 줄었다.
이는 단일 실행 쌍의 합산 빌드 시간 비교이며, 전체 CI 대기시간이나 비용의
절감률을 뜻하지 않는다.

## 문제와 근거

기존 CI는 PR에서는 경로별로 이미지를 선택했지만 main push에서는 필터를
우회해 9개 이미지를 모두 빌드했다. 문서만 머지해도 동일한 설치 작업을
반복했으며, 일반 `docker build`에는 실행 간 레이어 캐시 설정이 없었다.

[기준 실행](https://github.com/bbungjun/Autoresearch/actions/runs/33753267255)은
성공한 main CI이며, GitHub의 step 시작·종료 시각으로 다음을 확인했다.

| 이미지 | 빌드 단계 시간 |
| --- | ---: |
| Train | 240초 |
| Agent API·Runner·Workbench·Executor 합계 | 195초 |
| Serving | 86초 |
| Feast | 72초 |
| App | 71초 |
| MLflow | 40초 |
| 합계 | 704초 |

병렬 job 시간의 합계이므로 사용자의 실제 대기시간이나 개선율을 뜻하지 않는다.
초기 분석의 합계 690초는 산술 오류였으며, step 시작·종료 시각을 다시 합산해
704초(11분 44초)로 정정했다. 개별 이미지 시간은 동일하다.
App·Train은 builder의 uv 설치와 별개로 runtime 단계 앞에서 VCS_REF를
사용했고, Feast는 uv 설치보다 앞에서 사용했다. 커밋마다 바뀌는 메타데이터가
뒤쪽 설치·복사 레이어의 재사용을 방해할 수 있는 구조였다.

## 선택한 해결과 범위

1. PR은 기존 변경 파일 API, main push는 push 직전 커밋과 현재 커밋 사이의
   변경으로 이미지 필터를 적용한다. 최초 push의 0 SHA는 base/ref를 같은
   브랜치로 전달해 전체 파일을 검사한다. 수동 실행은 9개 모두 검증한다.
   COPY 입력을 대조해 기존 필터에 빠졌던 공통 applications 초기화 파일,
   Streamlit 설정, Executor의 tools 파일 두 개도 추가했다.
2. 새 커밋이 올라온 같은 PR의 이전 CI만 취소한다. main과 수동 실행은
   실행별 concurrency group을 사용한다. main은 각 push의 변경분을
   검증하므로 앞선 실행을 취소하거나 대기열에서 대체하면 검증이 누락될 수 있다.
3. Buildx로 이미지별 고정 GHA cache scope를 읽고 쓴다. 다단계 builder의
   설치 결과도 저장하도록 `mode=max`를 사용한다. `load: true`로 현재 runner에
   적재한 뒤 기존 smoke를 실행하며 레지스트리에는 게시하지 않는다.
4. App·Train·Feast의 VCS_REF 및 revision 환경 변수·라벨은 RUN/COPY 뒤로
   옮긴다. 이미지 커밋 정보와 코드 아카이브 주입 계약은 유지한다.
5. MLflow는 설치 레이어의 import 검증 외에 적재된 이미지에서도 import를
   실행한다. 캐시가 적중하면 Dockerfile의 RUN 자체는 다시 실행되지 않기 때문이다.

이미지별 scope는 서로 덮어쓰는 문제를 줄인다. 저장량과 export 시간이 늘 수
있는 `mode=max`를 선택한 이유는 App·Train builder 설치 레이어 재사용이다.
캐시 export는 5분 제한과 `ignore-error=true`를 두어 저장 장애가 성공한 빌드와
smoke를 실패로 바꾸지 않게 한다. export 경고와 캐시 적중 여부는 별도 관찰한다.

Agent 4개 이미지의 경로 분리(P2), 레지스트리에서 기존 이미지를 직접 가져오는
방식(P3)은 이번 범위에 포함하지 않는다. Python 테스트 job도 기존대로 실행한다.

## 대안과 선택 근거

| 대안 | 얻는 효과 | 제약과 이번 판단 |
| --- | --- | --- |
| 캐시만 적용 | 반복 설치 감소 | 문서 변경에도 9개 빌드를 시작하므로 선택 빌드와 함께 적용 |
| main 이미지 검증 전체 제거 | 머지 후 중복 실행 제거 | 머지 결과의 실행 검증을 유지하기 위해 관련 이미지만 선택하는 방식 채택 |
| 더 큰 runner 사용 | 일부 설치·압축 작업 가속 가능 | 미변경 환경을 다시 만드는 구조가 남고 별도 비용·측정이 필요해 도입하지 않음 |
| Agent 이미지별 경로 분리 | 4개 중 일부만 빌드 가능 | 공통 모듈의 영향 분석 범위를 늘리므로 P2 후속 작업으로 분리 |
| 레지스트리의 기존 런타임 이미지 사용 | 코드 변경에서 이미지 빌드 자체 생략 가능 | 이미지 보관·선택·버전 관리가 추가되므로 P3 후속 작업으로 분리 |

이번 변경의 기준은 검증 범위를 유지하면서 반복 작업을 줄이는 것이었다.
특히 캐시는 항상 존재하는 기반 상태가 아니라 없어도 빌드할 수 있는
최적화 수단으로 취급했다. 캐시 저장 오류를 허용한 것도 같은 이유이며,
실제 빌드 실패나 smoke 실패를 허용하도록 바꾼 것은 아니다.

## 구현 상세와 변경 전후

### 이벤트별로 어떤 이미지를 검증하는가

| 상황 | 변경 전 | 변경 후 |
| --- | --- | --- |
| 문서만 수정한 PR | 이미지 빌드 생략 | 동일 |
| 문서만 반영한 main push | 이미지 9개 빌드 | 이미지 빌드 생략, 집계 job은 실행 |
| Train Dockerfile만 수정한 main push | 이미지 9개 빌드 | Train 선택 |
| CI 워크플로우 또는 `.dockerignore` 변경 | 전체 이미지 선택 | 동일 |
| 수동 실행 | 전체 이미지 검증 | 동일 |
| 같은 PR의 새 커밋 | 이전 CI 취소 설정 없음 | 이전 실행 취소 |

이 표는 워크플로우의 선택 계약이다. 전체 이미지 원격 빌드와 캐시 재사용은
아래 실행 근거로 확인했으며, 문서 전용 main push의 생략 여부는 결과 문서만
변경하는 후속 이슈 #83에서 검증한다.
9개 이미지는 기존 6개 이미지 job에서 빌드한다. Agent job 하나가 API·Runner·
Workbench·Executor 4개를 계속 담당하며, 집계 job 이름 `Docker build`도 유지한다.

PR에서는 dorny/paths-filter의 변경 파일 API를 사용한다. main push에서는
`github.event.before`와 `github.sha`를 비교하여 한 번에 여러 커밋을 push한
경우도 마지막 커밋 하나로 축소하지 않는다. 최초 push는 이전 SHA가 0이므로
별도 분기로 전체 파일을 검사한다.

### 설치 결과를 다음 CI에 전달하는 방법

아래는 App 빌드의 실제 설정이다. 앞 단계에서
`docker/setup-buildx-action@v3`로 builder를 준비한다.

```yaml
uses: docker/build-push-action@v6
with:
  context: .
  file: deployment/Dockerfile.app
  tags: autoresearch:ci
  build-args: VCS_REF=${{ github.sha }}
  load: true
  push: false
  cache-from: type=gha,scope=ci-app
  cache-to: type=gha,scope=ci-app,mode=max,timeout=5m,ignore-error=true
```

scope를 커밋마다 바꾸지 않고 이미지별 고정 이름으로 둬 반복 실행에서
재사용하도록 했다. `load: true`는 결과를 runner의 Docker에 적재하므로 뒤의
`docker run`이 같은 빌드 결과를 검증할 수 있게 한다. 외부 레지스트리에
이미지를 게시하는 단계는 추가하지 않았다.

### 환경 재사용과 최신 코드 검증을 함께 유지하는 방법

App·Train·Feast는 코드를 이미지에 포함하지 않고 실행할 때 아카이브로
주입한다. CI가 체크아웃한 커밋의 코드를 `git archive`로 묶어 컨테이너에
전달하는 기존 흐름을 유지했다. 따라서 설치 레이어를 재사용해도 이번에
검증할 코드는 현재 체크아웃의 코드다.

커밋 메타데이터의 이동 효과는 이미지별로 구분한다.

| 이미지 | 기존 영향 범위 | 수정한 순서 |
| --- | --- | --- |
| App·Train | builder uv 설치는 이미 분리되어 있었지만 runtime의 apt·COPY 등이 revision 뒤에 위치 | runtime의 모든 RUN/COPY 이후 revision 기록 |
| Feast | 단일 stage의 apt·uv 설치 등이 revision 뒤에 위치 | 설치·COPY·소유권 설정 이후 revision 기록 |

이미지의 revision 라벨과 `AUTORESEARCH_REVISION`은 이미지 빌드 시점의
커밋을 뜻한다. 주입된 코드의 버전은 별도 부트스트랩 로그로 확인하는 기존
책임 구분을 유지했다. 이 작업을 App·Train의 builder 설치가 원래부터
커밋 정보 때문에 매번 무효화되던 문제를 고쳤다고 설명하면 부정확하다.

## 리뷰에서 발견하고 보완한 점

| 발견 | 문제가 되는 이유 | 보완 |
| --- | --- | --- |
| 기존 필터에 실제 COPY 입력 일부 누락 | 이전에는 main 전체 빌드가 뒤늦게 검증했지만 선택 빌드로 바꾸면 PR·main 모두 누락 가능 | Agent에 `.streamlit/**`, `applications/__init__.py`, tools 파일 2개 추가; Serving에 `applications/__init__.py` 추가 |
| 최초 push에서 base는 브랜치, ref는 SHA로 전달 | dorny v3의 초기 push 분기로 들어가지 않아 동일 커밋의 빈 diff가 될 수 있음 | 0 SHA에서는 base/ref를 모두 같은 브랜치로 전달 |

이 과정에서 단순히 main의 필터 우회를 제거하는 것만으로는 검증 범위를
보존할 수 없다는 점을 확인했다. 최적화가 없애는 중복 실행이 기존 누락을
보완하고 있었는지 함께 점검해야 했다.

새 회귀 검사에는 위 입력 경로의 이미지 선택, 최초 push 설정, 수동 전체 실행,
PR 전용 취소, 9개 캐시 scope의 분리, 빌드 결과의 실행 연결, revision 순서를
포함했다. 경로·표현식 검사는 설정에 대한 로컬 계약 검사이며 GitHub 이벤트와
캐시 서비스 전체를 실행한 통합 테스트는 아니다.

## 검증과 결과

2026-09-04 로컬 검증:

- 관련 pytest 5개 파일: Windows에서 42 passed, 15 skipped, 1 deselected.
  skip은 비활성 조직 workflow 계약이다. launcher COPY 검사 1개는 기존 코드도
  Windows의 경로 구분자 차이로 실패하여 제외했고, WSL에서 `--noconftest`로
  해당 정적 검사만 별도 실행해 1 passed를 확인했다.
- 변경 테스트의 Ruff, actionlint v1.7.12 (`-shellcheck=''`),
  `git diff --check` 통과. ShellCheck는 이번 actionlint 검증에 포함하지 않았다.
- App·Train·Feast 이미지는 각각 로컬 빌드 및 코드 아카이브 주입 기본 명령
  실행이 성공했다. VCS_REF만 바꾼 두 번째 빌드에서 각 이미지의 uv 설치·apt
  설치·COPY·소유권 설정이 모두 `CACHED`였으며 세 이미지의 revision 라벨과
  환경 변수는 새 값으로 변경되었다. 전체 CLI smoke 목록은 원격 CI에서 확인한다.
- 독립 리뷰에서 공유 COPY 경로 누락과 최초 push 예외를 발견하여 수정했다.
  회귀 검증을 추가했고 재리뷰에서 잔여 발견 사항이 없었다.

위 로컬 검증만으로 원격 CI 성능 개선을 단정하지 않았다. 이후 실제 원격
실행으로 확인한 결과를 다음 절에 기록했다. 구현은
[PR #82](https://github.com/bbungjun/Autoresearch/pull/82)를 squash merge했다.
원격 실행의 실제 결과는 아래 후속 기록으로 구분한다.

### 구현 및 검증 근거 위치

| 근거 | 확인할 내용 |
| --- | --- |
| [CI 워크플로우](../../.github/workflows/ci.yml) | 이벤트별 이미지 선택, Buildx 캐시, smoke, 집계 |
| [App Dockerfile](../../deployment/Dockerfile.app) · [Train Dockerfile](../../deployment/Dockerfile.train) · [Feast Dockerfile](../../deployment/Dockerfile.feast) | 설치 뒤 revision 기록 |
| [선택 빌드·캐시 계약 검사](../../tests/test_ci_docker_cache.py) | 공유 경로, push 경계, 캐시 scope, 적재·실행, 메타데이터 순서 |
| [Agent 컨테이너 검사](../../tests/applications/experiment_platform/test_agent_orchestration_container.py) · [Serving 배포 검사](../../tests/applications/reranking_api/test_serving_deployment.py) | 변경된 빌드 방식과 기존 실행 검증의 연결 |

로컬 검증 결과는 이 작업 중 관찰한 기록이며 전체 터미널 로그를 별도 파일로
보관하지는 않았다. 원격 측정은 아래 GitHub Actions 실행 링크의 job 시간과 BuildKit 로그를
근거로 하며, 장기 보존이 필요하면 Actions 로그 보존 기간 내 별도로 내보낸다.

## 반영 후 측정 절차

- 첫 CI에서 캐시를 채운 뒤, 의존성이 동일한 다음 실행의 Build 로그에서
  `uv sync`와 apt 레이어의 `CACHED` 여부를 확인한다.
- 각 이미지의 전체 Build step 시간, cache import/export 시간, 기존 smoke
  결과를 기록한다. Build step에는 이미지 load와 cache export 시간도 포함된다.
- 문서 전용 main push에서는 이미지 job 6개가 skipped이고 `Docker build`
  집계 job이 성공하는지 확인한다. 관련 코드 변경은 해당 이미지가 실행되어야 한다.
- 전체 검증은 Actions의 Python CI에서 Run workflow를 사용한다.
- `gh cache list --repo bbungjun/Autoresearch --limit 100`과 Actions의 cache
  화면으로 이미지 캐시·uv 캐시의 합산 저장량과 eviction을 관찰한다. 유료 용량
  확장 설정은 변경하지 않는다. PR 캐시는 ref별로 분리되므로 동일 scope라도
  저장 공간이 추가로 필요할 수 있다.
- 첫 실행이 느리거나 cache export가 timeout이면 경고를 확인한다. 반복 실행의
  적중률과 저장량을 확인한 뒤 scope별 저장 정책 조정을 별도 작업으로 판단한다.

`uv` cache mount의 내용은 GHA 레이어 캐시에 자동 보존되지 않는다. 이번 변경은
완료된 설치 레이어를 재사용하며, lock 변경 후 부분 다운로드 캐시 복구까지
보장하지 않는다. 캐시가 없거나 eviction되면 정상적인 새 빌드가 수행된다.

### 원격 검증 기록 — 2026-09-04

구현 PR은 [#82](https://github.com/bbungjun/Autoresearch/pull/82)이며,
main merge commit은 `6de4d0d70de0a79b0c9b3b36bdba1ff02ab3a71e`이다.
PR head commit은 `ee56a8a73a2c2061c7930ac07ab67b2f990389fc`이다.
실제 checkout과 이미지 VCS_REF는 `refs/pull/82/merge`의
`79663819b53f615e4c5f4b77a1714fbb8bb3da48`이며 두 attempt에서 동일하다.

| 실행 | 조건과 확인 범위 | 근거 |
| --- | --- | --- |
| 변경 전 기준 main | 기존 일반 docker build, 전체 CI 성공 | [33753267255](https://github.com/bbungjun/Autoresearch/actions/runs/33753267255) |
| PR 최초 실행 | 캐시 생성, Python 3.11·3.12·Feast·PostgreSQL·lock drift·9개 이미지 및 smoke 성공; 별도 Ruff 성공 후 merge | [33777667664 / attempt 1](https://github.com/bbungjun/Autoresearch/actions/runs/33777667664/attempts/1) |
| PR 동일 커밋 재실행 | 동일 이미지 9개의 저장된 캐시 재사용, 전체 CI 성공 | [33777667664 / attempt 2](https://github.com/bbungjun/Autoresearch/actions/runs/33777667664/attempts/2) |
| merge 이후 main | main push의 변경 경로 판정·9개 이미지·smoke·전체 테스트 성공 | [33778610655](https://github.com/bbungjun/Autoresearch/actions/runs/33778610655) |

PR 캐시는 PR ref에 속한다. main 최초 실행에서도 캐시를 새로 생성했으며,
빌드 단계 합계는 1,350초, 마지막 job까지의 전체 경과시간은 518초였다.
따라서 PR 재실행에서의 캐시 적중과 main에서의 캐시 생성은 구분해서 기록한다. 다음 표의 캐시 전후 비교는 같은 PR commit의
attempt 1과 attempt 2이며, 서로 다른 소스나 이미지 집합을 비교한 것이 아니다.

| 이미지 | 변경 전 기준 | PR 최초 실행 | PR 캐시 재사용 |
| --- | ---: | ---: | ---: |
| App | 71초 | 199초 | 34초 |
| Train | 240초 | 191초 | 47초 |
| Feast | 72초 | 124초 | 39초 |
| Serving | 86초 | 162초 | 59초 |
| MLflow | 40초 | 82초 | 26초 |
| Agent API | 묶음 측정 | 66초 | 27초 |
| Agent Runner | 묶음 측정 | 127초 | 24초 |
| Agent Workbench | 묶음 측정 | 83초 | 27초 |
| Agent Executor | 묶음 측정 | 154초 | 62초 |
| Agent 4개 소계 | 195초 | 430초 | 140초 |
| 전체 합계 (소계 중복 제외) | 704초 | 1,188초 | 345초 |

측정은 GitHub job의 각 Build step `completedAt - startedAt`을 합산했다.
새 Build step에는 캐시 import, 이미지 export/load, 캐시 export도 포함된다.
최초 실행이 기준 실행보다 느려졌다는 점도 결과에 포함한다. 캐시를 채우는
작업과 action 기반 이미지 적재·내보내기 비용이 추가되므로 첫 실행만으로
효과를 평가하면 안 된다.

동일 구현의 최초 실행 대비 재사용 실행은 `(1188 - 345) / 1188 ≈ 71.0%`,
변경 전 기준 실행 대비로는 `(704 - 345) / 704 ≈ 51.0%` 짧았다.
반복 측정의 평균·분산을 구한 결과가 아닌 **각 조건 1회 관측값**이다.

전체 workflow의 경과시간은 별도 지표다. workflow `startedAt`부터
마지막 job의 `completedAt`까지는 변경 전 513초, PR 최초 실행 504초였다.
캐시 재사용 실행은 502초였다. 즉 이 표본에서 전체 경과시간은 504초에서
502초로 거의 같았고, 빌드 단계 합계만 크게 줄었다. API의 `updatedAt`은
완료시각 전용 필드가 아니므로 경과시간 계산에 쓰지 않았다. 빌드 합계가 늘었어도
전체 경과시간이 그만큼 늘지 않은 이유를 이해하려면 병렬 실행과 마지막으로
끝나는 job을 함께 봐야 한다. PR 최초 실행에서 Python 3.12 job은 8분 19초,
Agent 이미지 job은 7분 46초였다. 빌드 합계의 감소율을 전체 CI 감소율로
대체하지 않는다.

캐시 로그에서 import manifest와 설치 단계의 `CACHED`를 확인했다.
재사용 실행의 로그에는 전체 `CACHED` 표기 117개가 있었고, 기준·최초 PR·
main 최초 실행의 로그에서는 각각 11개였다. 이는 로그 표기 개수이며
레이어 크기를 반영한 적중률을 뜻하지 않는다. 예를 들어
MLflow 재실행은 설치 레이어를 재사용했고 cache export가 0.5초였다.
최초 실행의 Feast export는 20.6초, Train export는 66.3초였다.
이 수치는 BuildKit의 `sending cache export ... done` 구간이며 Build step
전체 시간과 구분한다.

문서 전용 변경은 [후속 PR #84](https://github.com/bbungjun/Autoresearch/pull/84)
에서 검증한다. [최초 문서 PR 실행](https://github.com/bbungjun/Autoresearch/actions/runs/33779198508)에서
Docker 하위 job 6개가 모두 skipped이고 집계 job은 success인 것을 확인했다. 수동 `workflow_dispatch` 전체 실행과
새 커밋에 의한 이전 PR 실행 취소는 로컬 설정 검사 범위이며, 이 측정만으로
원격 실증까지 완료했다고 주장하지 않는다. 문서 PR의 새 커밋으로 이전
실행 취소를 확인한 경우에는 해당 취소 실행과 최종 성공 실행을 함께 확인한다.

## 포트폴리오 서술 초안

다음 문장은 로컬 검증과 원격 측정의 확인 범위로 작성했다. 반복 측정과
문서 전용 main 검증을 추가할 때도 조건과 실행 근거를 함께 갱신한다.

> 여러 런타임 이미지를 검증하는 ML 프로젝트에서, main CI가 변경 경로와
> 무관하게 9개 이미지를 재빌드하는 구조를 분석했습니다. 기준 실행의 빌드
> 단계 합계는 704초였으며, 이를 실제 대기시간과 구분해 기록했습니다.
> 변경 이미지 선택과 이미지별 빌드 캐시를 적용하고, 커밋 메타데이터를 설치
> 단계 뒤로 이동했습니다. 최적화 과정에서 공유 입력 경로 누락과 최초 push
> 예외를 리뷰로 찾아 보완하고, 현재 코드의 컨테이너 실행 검증을 유지했습니다.
> App·Train·Feast의 로컬 재빌드에서 설치 레이어 재사용과 revision 갱신을
> 확인했습니다. 원격에서는 9개 이미지와 실행 검증을 통과했고, 동일 커밋의
> 최초 실행과 캐시 재사용 실행의 빌드 단계 합계가 1,188초에서 345초로
> 줄었습니다. 단일 실행 비교라는 한계와 전체 CI 경과시간의 차이를 함께
> 기록해 성과를 과장하지 않았습니다.

이 사례에서 설명할 수 있는 역량은 실행 기록에 근거한 병목 분해, Docker
레이어 무효화 원인 분석, 변경 영향에 따른 검증 설계, 대안의 운영 비용 비교,
독립 리뷰를 통한 예외 보완이다. 합산 빌드 시간만으로 “CI 84% 단축”,
“대기시간 11분 30초 절감”, “비용 절감 달성”으로 표현하지 않는다.

근거: [Docker Actions 캐시](https://docs.docker.com/build/ci/github-actions/cache/),
[GHA scope·export 옵션](https://docs.docker.com/build/cache/backends/gha/),
[캐시 무효화](https://docs.docker.com/build/cache/invalidation/),
[paths-filter v3](https://github.com/dorny/paths-filter/tree/v3).
