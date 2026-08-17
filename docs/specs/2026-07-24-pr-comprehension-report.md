# PR 이해 리포트 자동 생성 (4단계 이해 체계)

> Status: 구현 (#313, #348) + 가독성 개선 설계 완료(이슈 미발행) | Last Updated: 2026-08-16

## 배경·목표

팀 전원이 에이전트(Claude/Codex CLI)로 코드를 생성하면서 PR 단위 코드
이해가 병목이 되었습니다. 모든 PR에 다음 4단계 이해 체계를 자동
제공합니다 (3→4단계 재구성 배경은 "가독성 개선" 절 참조).

1. **시각화** — 전체 파이프라인에서 이 PR의 위치(as-is → to-be)
2. **쉬운 설명** — 주제별 쉬운 설명 + 왜 필요했나 + 기대효과
   (비전문가 대상, 함수/변수명 지양)
3. **코드 기준 설명** — 함수/모듈 단위 중요도순 재구성, core >
   contract > config > test > docs 순 정렬 (기본 접힘)
4. **이해도 확인 Q&A** — 기존 `claude.yml` 리뷰 봇의 인라인
   "이해도 확인:" 질문(답변 후 스레드 resolve)

## 설계 결정

- **에이전트는 HTML을 작성하지 않습니다.** 시각화 품질을 고정하기 위해
  HTML/CSS 템플릿(`.github/pr-report/template.html`)을 저장소에 커밋해
  두고, 에이전트는 `report.json` 데이터만 출력합니다.
- **파이프라인 노드는 정본 카탈로그에서만 가져옵니다.**
  `.github/pr-report/pipeline-nodes.json`이 노드/에지의 단일 출처이며,
  에이전트는 각 노드의 status(unchanged/modified/added/removed)만
  판정합니다. 파이프라인 구조 변경 시 `docs/guides/pipeline-overview.md`
  갱신과 같은 PR에서 카탈로그도 갱신합니다.
- **배포는 gh-pages `pr/<번호>/`.** 저장소가 public이므로 GitHub Pages를
  사용합니다. 리포트 URL: `https://skyaho.github.io/Autoresearch/pr/<n>/`
- **자동 트리거는 opened / ready_for_review만.** 에이전트 PR은 push가
  잦아 synchronize 자동 재생성은 비용·소음이 큽니다. 갱신은 PR에
  `/claude-report` 코멘트로 수동 트리거합니다.
- **soft-fail.** 스키마 위반·생성 실패 시 안내 코멘트만 남기고 job은
  성공 종료합니다. required check로 등록하지 않으며 머지를 막지
  않습니다.

## 동작 계약

### report.json

계약 정본은 `.github/pr-report/report.schema.json`입니다
(`additionalProperties: false`). 요지:

| 키 | 계약 |
| --- | --- |
| `summary_ko` | 정확히 3줄, 각 120자 이하 |
| `plain_points_ko` | 2단계용 주제별 쉬운 설명 bullet, 3~6개·120자 이하 (schema_version 2, "가독성 개선" 절 참조) |
| `motivation_ko` / `expected_effects_ko` | as-is 문제 서술 / 기대효과 1~5개 |
| `pipeline.nodes[]` | 카탈로그 전체 복사 + status 판정, 변경 노드는 `as_is_ko`/`to_be_ko` |
| `pipeline.focus` | 중심 노드 id 1~3개 |
| `changes[]` | 20개 이하, rank·importance·module·symbol·file·explanation_ko 필수, 3단계(기본 접힘) |
| `changes[].importance` | `core` \| `contract` \| `config` \| `test` \| `docs` |
| `qa_note_ko` | 4단계(인라인 질문 답변·resolve) 안내 |

### 워크플로우 (`.github/workflows/pr-report.yml`)

- **analyze job** (PR별 concurrency, cancel-in-progress):
  `generate_report.py`가 PR 메타·diff·파이프라인 정본 문서를 결정적으로
  수집해 **OpenRouter chat completions API**(#319)로 `report.json` 생성
  (스키마 검증 실패 시 오류 피드백 1회 재시도) → `check-jsonschema` 검증 →
  `inject.py`로 `site/pr/<n>/index.html` 빌드 → artifact 업로드. 실패 시
  안내 코멘트. 모델은 워크플로 상단 `PR_REPORT_MODEL` 변수로 교체
  (기본 `google/gemini-3.7-flash`), 인증은 `OPENROUTER_API_KEY` 시크릿
  (팀 공용 종량제 — 개인 Claude 구독과 분리). diff는 생성 파일 제외,
  파일당 20K자·전체 150K자 제한.
- **publish job** (저장소 전역 concurrency — gh-pages push race 방지):
  `peaceiris/actions-gh-pages@v4` + `keep_files: true`로 배포 → 마커
  `<!-- pr-comprehension-report -->` 기반 sticky 코멘트 upsert (요약 3줄,
  쉬운 설명 미리보기, 리포트 링크, 4단계 안내 — 상세는 "가독성 개선"
  절 참조).

### 기존 리뷰 봇과의 관계

`claude.yml`(인라인 이해도 확인 질문)과 `pr-report.yml`은 별도
워크플로우로 병렬 실행됩니다. 리뷰 요약 코멘트가 리포트 URL을
안내합니다. 트리거 문구 `/claude-review`와 `/claude-report`는 substring
관계가 아니므로 충돌하지 않습니다. 인증도 분리되어 있습니다 —
`claude.yml`은 `CLAUDE_CODE_OAUTH_TOKEN`(Claude 구독), `pr-report.yml`은
`OPENROUTER_API_KEY`(종량제)를 사용합니다.

## 운영

- 1회 설정: 첫 배포로 `gh-pages` 브랜치가 생성된 뒤 Settings → Pages →
  "Deploy from a branch" → `gh-pages` / root 지정.
- fork PR에서는 시크릿이 주입되지 않으므로 리포트가 생성되지 않습니다
  (팀은 같은 저장소 브랜치로 작업하므로 영향 없음).
- 로컬 검증: 샘플 report.json으로
  `python .github/pr-report/inject.py .github/pr-report/template.html report.json > index.html`
  후 브라우저 확인.

### Merge 리포트 아카이브

merge된 PR 리포트는 Pages 루트
`https://skyaho.github.io/Autoresearch/`에서 최신 머지순으로 제공한다.
`pr-report-archive.yml`은 PR merge, PR Report 완료, 수동 실행 때
`gh-pages/pr/*`를 다시 스캔하며 기존 개별 리포트 파일은 보존한다.
최초 배포 또는 복구는 Actions의 **PR Report Archive → Run workflow**로
실행한다.

## 남은 작업 (phase 2)

- PR close 시 gh-pages의 `pr/<n>/` 정리 워크플로우
- 검증 후 `Autoresearch-infra`(카탈로그를 인프라 노드로 교체),
  `Autoresearch-airflow`(OAuth secret 등록 선행, DAG 관점 카탈로그)로
  동일 세트 롤아웃

## 가독성 개선 (4단계 재구성)

> Status: 설계 완료 (이슈 미발행, 구현 전) | Last Updated: 2026-07-24

### 배경

팀원 피드백: 1단계 파이프라인 시각화는 효과적이지만, 나머지 텍스트
(특히 옛 2단계 `changes[]`의 항목별 기술 설명)는 양이 많고 본인이
구현하지 않은 모듈의 용어라 읽는 데 시간이 오래 걸립니다. 목표는
"1단계 시각적 직관은 유지하되, 비전문가가 먼저 쉽게 이해할 수 있는
설명을 앞에 배치하고, 코드 레벨 상세는 원하는 사람만 펼쳐보게" 하는
것입니다.

### 새 페이지 구조 (4단계)

핵심요약(라벨 없음) → **1단계** 파이프라인 시각화(변경 없음) →
**2단계** 쉬운 설명(신규) → **3단계** 코드 기준 설명(옛 2단계
`changes[]`, 기본 접힘) → **4단계** 이해도 확인(옛 3단계 QA, 라벨만
이동).

2단계는 신규 필드 `plain_points_ko`(주제별 쉬운 설명 bullet) +
기존 `motivation_ko`(왜 필요했나) + `expected_effects_ko`(기대효과)를
한 카드 안에 소제목으로 구분해 통합합니다. `changes[]`·`pipeline`·
`qa_note_ko`의 구조는 이번 변경 범위 밖입니다(3단계 자체의 재설계는
필요성이 확인되면 별도 변경으로 다룹니다).

### report.json 계약 변경

- `schema_version`: `1` → `2` (계약 변경 표식. 소비자는 매 실행
  재생성되는 gh-pages 산출물뿐이라 하위호환 이슈 없음)
- 신규 필수 필드 `plain_points_ko`: 문자열 배열, 3~6개, 항목당 120자
  이하. "이 모듈을 모르는 팀원" 기준으로 함수/변수/클래스명 대신
  동작·데이터·영향 중심 서술. 주제 단위 bullet(예: "데이터가 이렇게
  바뀌어요").

### 프롬프트 rubric 추가

`generate_report.py`의 system 프롬프트에 `plain_points_ko` 작성 규칙
섹션을 추가하고, 스키마 필수 필드 안내("이 넷은 모두 필수") 문구를
"이 다섯"으로 갱신합니다.

**검증 스파이크(2026-07-24, 브레인스토밍 중 수행):** 위 rubric으로
실제 PR #334(calibration 2-모델 패키징) diff를 OpenRouter
(`google/gemini-3.6-flash`)에 넣어본 결과, 함수/변수명 없이 동작
중심으로 읽히는 4개 bullet을 얻었습니다. "서빙", "체이닝" 같은 일부
도메인 용어는 남았으나 옛 `changes[]` 설명보다 가독성이 뚜렷이
개선됨을 확인했습니다. 이 결과로 A안(schema에 `plain_points_ko`만
추가, `changes[]`는 그대로 3단계로 이동)을 최종 확정했습니다.

### template.html 변경

- 섹션 순서를 위 4단계로 재배치
- 2단계 카드는 `plain_points_ko` bullet · `motivation_ko` ·
  `expected_effects_ko`를 소제목으로 구분된 하위 블록으로 렌더링
  (기존 `#node-detail`의 라벨-블록 패턴 재사용)
- 3단계(`#changes`) 전체를 `<details>`로 감싸 기본 접힘,
  `<summary>`에 "코드 기준 상세 설명 펼치기 (N건)" 표시. 개별 항목의
  diff toggle(`details.diff-box`)은 기존 그대로 유지
- 4단계는 라벨 텍스트만 "3단계"→"4단계"로 교체
- SVG 파이프라인 렌더링, changes 카드 생성 로직 등 기존 JS는 변경
  없음

### pr-report.yml (publish job) 변경

sticky 코멘트 생성 파이썬(`Upsert sticky comment` 스텝)이 라벨과
미리보기를 하드코딩하고 있어 함께 갱신합니다.

- 라벨 텍스트: `"1·2단계 — ..."` / `"3단계 — 이해도 확인"` →
  `"1·2단계 — ..."` / `"4단계 — 이해도 확인"`(파이프라인+쉬운 설명
  링크, QA는 4단계로)로 번호만 갱신
- **미리보기 소스 교체**: 지금은 `changes[]`(3단계, symbol 단위
  기술 설명) top 3를 코멘트에 미리 보여주는데, 이 코멘트는 PR 화면에서
  가장 먼저 눈에 띄는 자리라 기술 용어가 그대로 노출되면 오늘 지적된
  문제가 재발합니다. `sorted(changes, key=rank)[:3]` 대신
  `plain_points_ko`(상위 2~3개)를 보여주도록 교체해, 코멘트 단계부터
  "쉬운 설명" 톤을 일관되게 유지합니다.

### 검증 계획

- `python .github/pr-report/inject.py .github/pr-report/template.html
  <샘플 report.json> > index.html` 후 브라우저로 4단계 레이아웃·접힘
  동작·다크모드 확인
- `check-jsonschema`로 `plain_points_ko` 포함 스키마 검증
- 위 스파이크로 만든 실제 PR #334 기반 report.json으로 렌더링 확인
- sticky 코멘트 파이썬 스니펫은 `report.json` 샘플로 로컬에서
  `python -c`로 직접 실행해 출력 미리보기 확인 (GitHub API 호출 없이)
- `uv run --no-sync ruff check .github/pr-report`
