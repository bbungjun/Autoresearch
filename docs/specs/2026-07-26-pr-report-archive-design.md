# 머지된 PR 리포트 아카이브

> Status: 구현 완료 (#348) | Issue: #348 | Last Updated: 2026-07-26

## 배경과 목표

PR 이해 리포트는 GitHub Pages의 `pr/<PR 번호>/index.html`에 누적되지만,
현재 Pages 루트에는 이 리포트를 탐색할 인덱스가 없다. 기존 리포트 파일을
그대로 보존하면서 merge된 PR의 리포트만 모아 보여주는 정적 아카이브를
`https://skyaho.github.io/Autoresearch/`에 제공한다.

아카이브는 다음 정보를 최신 머지순으로 표시한다.

- PR 번호와 제목
- 작성자
- 머지 날짜
- 리포트의 핵심 요약 3줄
- 개별 PR 리포트 링크

사용자는 PR 번호, 제목, 작성자로 목록을 즉시 검색할 수 있다.

## 범위

### 포함

- 기존 `gh-pages/pr/*/index.html`을 대상으로 한 최초 소급 등록
- GitHub API의 PR 상태를 기준으로 merge된 PR만 노출
- 정적 `index.html`과 `archive.json` 생성
- PR merge, PR Report 완료, 수동 실행 시 아카이브 재생성
- 기존 report schema v1과 v2의 공통 필드 지원
- 브라우저 내 번호·제목·작성자 검색
- 모바일과 데스크톱에서 읽을 수 있는 반응형 목록

### 제외

- 미머지 PR 리포트 파일 삭제
- merge되지 않고 닫힌 PR의 직접 URL 차단
- 서버 측 검색, 데이터베이스, 별도 애플리케이션 서버
- 페이지네이션과 고급 필터
- 리포트가 생성되지 않은 merge PR의 빈 카드 생성

## 설계 결정

### 정적 재생성

방문 시 GitHub API를 호출하는 대신 GitHub Actions에서 아카이브를 미리
생성한다. 방문자는 GitHub Pages의 정적 파일만 받으므로 API rate limit,
GitHub API 장애, 느린 다중 요청의 영향을 받지 않는다.

증분 manifest를 직접 수정하지 않고 매 실행마다 현재 `gh-pages/pr/*`와
GitHub의 PR 상태를 기준으로 전체 인덱스를 재생성한다. PR 번호별 상태가
정본에서 다시 계산되므로 누락, 중복, 동시 수정으로 인한 drift를 줄인다.

### 기존 리포트 보존

아카이브 배포는 `gh-pages` 루트의 `index.html`과 `archive.json`만
갱신한다. 기존 `pr/<PR 번호>/index.html`은 수정하거나 삭제하지 않는다.
목록에 나타나지 않는 open/closed-unmerged PR도 기존 직접 URL로 계속
접근할 수 있다.

## 구성 요소

### `.github/pr-report/build_archive.py`

아카이브 데이터 수집과 정적 산출물 생성을 담당한다.

입력:

- 로컬로 체크아웃한 `gh-pages` 디렉터리
- 저장소 식별자
- `GH_TOKEN`
- 아카이브 HTML 템플릿

처리:

1. `pr/<숫자>/index.html` 경로에서 PR 번호를 수집한다.
2. GitHub API로 각 PR의 상태, 제목, 작성자, `mergedAt`을 조회한다.
3. `mergedAt`이 있는 PR만 남긴다.
4. merge된 PR의 HTML에서
   `<script id="report-data" type="application/json">` 내용을 읽는다.
5. `summary_ko` 3줄과 PR 메타데이터를 정규화한다.
6. PR 번호 기준 중복을 제거하고 `mergedAt` 내림차순으로 정렬한다.
7. `archive.json`과 완성된 `index.html`을 임시 출력 디렉터리에 쓴다.

HTML 내장 JSON은 실행하지 않고 JSON 텍스트로만 파싱한다. schema v1과
v2 모두에 존재하는 `pr`과 `summary_ko`만 아카이브 계약으로 사용한다.
제목과 작성자, merge 상태와 날짜는 오래된 리포트의 스냅샷보다 GitHub API
응답을 우선한다.

### `.github/pr-report/archive-template.html`

아카이브의 고정 레이아웃과 스타일, 클라이언트 검색을 담당한다.

- 기존 PR Report의 색상과 타이포그래피를 재사용한다.
- 상단에 제목, 등록된 리포트 수, 검색 입력을 표시한다.
- 각 카드에 PR 번호, 제목, 작성자, 머지 날짜, 요약 3줄, 링크를 표시한다.
- 검색어를 소문자로 정규화한 뒤 PR 번호·제목·작성자에 대한 부분 일치로
  필터링한다.
- 검색 결과가 없으면 빈 상태 메시지를 표시한다.
- 사용자 또는 PR에서 온 문자열은 `innerHTML`이 아닌 텍스트 노드로
  렌더링한다.

### `.github/workflows/pr-report-archive.yml`

아카이브를 생성하고 GitHub Pages에 배포한다.

트리거:

- `pull_request`의 `closed`: `merged == true`일 때만 실행
- `workflow_run`: `PR Comprehension Report`가 끝났을 때 실행
- `workflow_dispatch`: 최초 백필과 운영상 수동 복구

`workflow_run`은 merge 뒤에 리포트를 재생성한 경우도 최종적으로
아카이브에 반영하기 위한 트리거다. PR Report가 실패한 경우에도 생성기는
현재 정본에서 전체 목록을 다시 계산하므로 기존 아카이브를 훼손하지 않는다.

기존 PR Report publish job과 동일한 `pr-report-publish` concurrency
그룹을 사용하고 `cancel-in-progress: false`로 설정한다. 두 워크플로우의
`gh-pages` push를 직렬화해 경합을 막는다.

권한은 `contents: write`와 PR 메타데이터 조회에 필요한 최소 읽기 권한만
부여한다. PR 본문이나 diff, OpenRouter 시크릿은 사용하지 않는다.

## 데이터 계약

`archive.json`은 다음 구조를 사용한다.

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-26T00:00:00Z",
  "reports": [
    {
      "number": 345,
      "title": "docs: 배포 아티팩트 표를 실제 배포 상태로 갱신",
      "author": "example-user",
      "merged_at": "2026-07-25T19:21:16Z",
      "summary_ko": ["요약 1", "요약 2", "요약 3"],
      "report_url": "pr/345/"
    }
  ]
}
```

`reports`는 `merged_at` 내림차순이며 같은 PR 번호가 두 번 나타나지 않는다.
`report_url`은 Pages 저장소 루트 기준 상대 경로다.

## 오류 처리

아카이브는 새 산출물이 완전히 생성된 경우에만 배포한다.

- GitHub API 조회 실패: workflow 실패, 기존 아카이브 유지
- merge된 PR의 HTML 또는 내장 JSON 손상: workflow 실패, 기존 아카이브 유지
- 필수 필드 누락 또는 요약 3줄 미충족: workflow 실패, 기존 아카이브 유지
- open 또는 closed-unmerged PR: 정상 제외
- 리포트 파일이 없는 merge PR: 스캔 대상이 아니므로 정상 제외
- 중복 PR 디렉터리: PR 번호를 키로 하나만 유지

오류가 있는 merge 리포트를 조용히 누락하지 않는다. 배포 전에 전체 생성을
실패시켜 마지막으로 검증된 아카이브를 계속 제공한다.

## 배포 흐름

```text
pull_request closed(merged) / PR Report workflow_run / workflow_dispatch
  → main에서 아카이브 생성기와 템플릿 checkout
  → gh-pages를 별도 경로에 checkout
  → PR 번호 수집 및 GitHub API 상태 조회
  → merge 리포트 JSON 추출·정렬
  → 임시 디렉터리에 index.html + archive.json 생성
  → gh-pages 루트에 배포(keep_files: true)
```

최초 배포는 `workflow_dispatch`로 실행한다. 이 실행이 현재 존재하는 모든
`pr/*/index.html`을 스캔하므로 별도의 일회성 백필 스크립트는 만들지 않는다.

## 검증

자동 테스트는 다음 시나리오를 포함한다.

- merge된 PR만 포함하고 open 및 closed-unmerged PR은 제외
- `merged_at` 최신순 정렬
- PR 번호 기준 중복 제거
- schema v1·v2 HTML의 내장 JSON 추출
- 손상된 HTML, 잘못된 JSON, 필수 필드 누락 시 실패
- 번호·제목·작성자 검색의 부분 일치와 대소문자 무시
- 사용자 입력을 HTML로 해석하지 않는 렌더링
- 생성 산출물의 JSON 계약과 링크 경로

워크플로우 변경에는 `git diff --check`와 가능한 경우 `actionlint`를
실행한다. 샘플 fixture로 로컬 `index.html`을 생성해 데스크톱과 모바일
레이아웃, 검색, 빈 결과 상태, 개별 리포트 링크를 브라우저에서 확인한다.

## 완료 조건

- Pages 루트에서 merge된 PR 리포트만 최신 머지순으로 표시된다.
- 기존 리포트가 최초 수동 실행으로 소급 등록된다.
- 각 카드에 번호, 제목, 작성자, 머지 날짜, 요약 3줄이 표시된다.
- 번호, 제목, 작성자 검색이 서버 요청 없이 동작한다.
- 기존 `pr/<번호>/index.html` 파일은 모두 보존된다.
- 실패한 생성은 기존 아카이브를 덮어쓰지 않는다.
- 자동 테스트와 정적 검증이 통과한다.

## 구현 검증 결과

- `origin/gh-pages`의 기존 리포트 18개(v1 13개, v2 5개)를 모두 읽고
  GitHub API의 merge 상태와 교차 검증해 18개 아카이브 항목을 생성했다.
- Python·Node·workflow 계약 테스트 19개가 통과했다.
- 전체 테스트는 786개가 통과했고, 구현 전과 동일한 Windows 장경로 관련
  `test_action_logs_daily.py` 실패 16개만 남았다.
- Ruff와 `git diff --check`가 통과했다. 로컬에 `actionlint`가 없어
  workflow 구조는 Python 계약 테스트와 YAML 파싱으로 검증했다.
- 데스크톱 1280px와 모바일 390px에서 가로 넘침 없이 카드가 표시됐다.
- 실제 브라우저에서 PR 번호(`345`), 제목(`ONNX`), 작성자(`bbungjun`)
  검색과 빈 결과 상태를 확인했다.
- 카드의 상대 링크가 기존 개별 리포트 경로(`pr/<번호>/`)를 가리키며
  브라우저 콘솔 오류가 없음을 확인했다.
