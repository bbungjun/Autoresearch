# PR Report Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** GitHub Pages 루트에 merge된 PR 이해 리포트만 최신순으로 모아 보여주고 PR 번호·제목·작성자로 검색할 수 있는 정적 아카이브를 배포한다.

**Architecture:** Python 생성기가 `gh-pages/pr/*/index.html`과 GitHub Pulls API를 결합해 검증된 `archive.json`과 `index.html`을 만든다. 별도 GitHub Actions 워크플로우가 PR merge, PR Report 완료, 수동 실행 때 전체 인덱스를 재생성하고 기존 PR 페이지를 보존한 채 Pages 루트만 갱신한다.

**Tech Stack:** Python 3.11+, 표준 라이브러리(`argparse`, `dataclasses`, `html.parser`, `json`, `pathlib`, `subprocess`), pytest, vanilla HTML/CSS/JavaScript, GitHub CLI, GitHub Actions, `peaceiris/actions-gh-pages@v4`

**Spec:** `docs/specs/2026-07-26-pr-report-archive-design.md` · **Issue:** #348 · **Branch:** `feat/348-pr-report-archive`

## Global Constraints

- 아카이브에는 GitHub API에서 `merged_at`이 있는 PR만 포함한다.
- open 또는 closed-unmerged PR 리포트 파일은 삭제하지 않고 목록에서만 제외한다.
- 리포트가 없는 merge PR은 빈 카드로 만들지 않는다.
- 카드에는 PR 번호, 제목, 작성자, 머지 날짜, 핵심 요약 정확히 3줄, 개별 리포트 링크를 표시한다.
- 목록은 `merged_at` 내림차순이며 PR 번호 기준 중복이 없어야 한다.
- 검색은 서버 요청 없이 PR 번호·제목·작성자 부분 일치로 동작하고 대소문자를 구분하지 않는다.
- report schema v1과 v2의 공통 `pr`·`summary_ko` 필드를 모두 지원한다.
- API·HTML·JSON·필수 필드 오류가 있으면 새 아카이브를 배포하지 않고 기존 아카이브를 유지한다.
- 기존 `gh-pages/pr/<번호>/index.html`은 수정하거나 삭제하지 않는다.
- PR Report publish와 아카이브 publish는 동일한 `pr-report-publish` concurrency 그룹을 사용한다.
- 권한이 높은 `workflow_run`에서는 PR checkout이나 artifact를 실행·다운로드하지 않고 `main`의 생성기와 정적 `gh-pages` 콘텐츠만 읽는다.
- 신규 런타임 의존성을 추가하지 않는다.

---

## File Structure

| 파일 | 책임 |
| --- | --- |
| `.github/pr-report/build_archive.py` | PR 페이지 탐색, GitHub API 메타데이터 조회, 내장 JSON 파싱, 필터·정렬·검증, 정적 산출물 생성 |
| `.github/pr-report/archive-template.html` | 아카이브 레이아웃, 카드 렌더링, 빈 상태 UI |
| `.github/pr-report/archive.js` | 브라우저 렌더링과 번호·제목·작성자 검색; Node에서도 가져올 수 있는 순수 검색 함수 제공 |
| `.github/workflows/pr-report-archive.yml` | merge/report 완료/수동 트리거, main·gh-pages checkout, 생성, 직렬화된 Pages 배포 |
| `tests/test_pr_report_archive.py` | Python 생성기 단위·통합 테스트 |
| `tests/test_pr_report_archive_search.py` | Node를 이용한 순수 JavaScript 검색 계약 테스트 |
| `tests/test_pr_report_archive_workflow.py` | workflow 트리거·권한·concurrency·보존 배포 계약 테스트 |
| `docs/specs/2026-07-24-pr-comprehension-report.md` | 기존 PR Report 운영 문서에 archive workflow와 Pages 루트 링크 추가 |
| `docs/specs/2026-07-26-pr-report-archive-design.md` | 구현 완료 상태와 실제 검증 결과 반영 |

---

### Task 1: 아카이브 데이터 수집기

**Files:**
- Create: `.github/pr-report/build_archive.py`
- Create: `tests/test_pr_report_archive.py`

**Interfaces:**
- Produces: `ReportSnapshot`, `PullRequestMetadata`, `ArchiveEntry` dataclasses
- Produces: `discover_report_pages(pages_root: Path) -> dict[int, Path]`
- Produces: `extract_report_snapshot(path: Path) -> ReportSnapshot`
- Produces: `fetch_merged_pull_requests(repository: str) -> dict[int, PullRequestMetadata]`
- Produces: `build_archive_entries(pages_root: Path, merged_prs: Mapping[int, PullRequestMetadata]) -> list[ArchiveEntry]`
- Consumes later: Task 2의 정적 렌더러와 Task 3의 workflow CLI가 이 인터페이스를 사용한다.

- [x] **Step 1: 모듈 로더와 HTML fixture를 포함한 실패 테스트 작성**

`tests/test_pr_report_archive.py`에 점이 포함된 `.github` 경로를 직접 import하는 로더와 v1·v2 fixture helper를 작성한다.

```python
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPOSITORY_ROOT / ".github" / "pr-report" / "build_archive.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_archive", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_report(
    root: Path,
    number: int,
    *,
    schema_version: int = 1,
    summary: list[str] | None = None,
) -> Path:
    report_dir = root / "pr" / str(number)
    report_dir.mkdir(parents=True)
    payload = {
        "schema_version": schema_version,
        "pr": {"number": number, "title": f"snapshot {number}", "author": "snapshot"},
        "summary_ko": summary or ["요약 1", "요약 2", "요약 3"],
    }
    path = report_dir / "index.html"
    path.write_text(
        '<script id="report-data" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script>",
        encoding="utf-8",
    )
    return path
```

다음 테스트를 추가한다.

```python
def test_discovers_only_numeric_pr_report_pages(tmp_path):
    module = _load_module()
    expected = _write_report(tmp_path, 345)
    (tmp_path / "pr" / "draft").mkdir(parents=True)
    (tmp_path / "pr" / "draft" / "index.html").write_text("ignored")

    assert module.discover_report_pages(tmp_path) == {345: expected}


@pytest.mark.parametrize("schema_version", [1, 2])
def test_extracts_common_fields_from_v1_and_v2(tmp_path, schema_version):
    module = _load_module()
    path = _write_report(tmp_path, 345, schema_version=schema_version)

    snapshot = module.extract_report_snapshot(path)

    assert snapshot.number == 345
    assert snapshot.summary_ko == ("요약 1", "요약 2", "요약 3")


def test_rejects_invalid_json_and_non_three_line_summary(tmp_path):
    module = _load_module()
    invalid = tmp_path / "invalid.html"
    invalid.write_text(
        '<script id="report-data" type="application/json">{</script>',
        encoding="utf-8",
    )
    short = _write_report(tmp_path, 346, summary=["한 줄"])

    with pytest.raises(module.ArchiveBuildError, match="JSON"):
        module.extract_report_snapshot(invalid)
    with pytest.raises(module.ArchiveBuildError, match="summary_ko"):
        module.extract_report_snapshot(short)
```

- [x] **Step 2: 수집기 테스트가 구현 부재로 실패하는지 확인**

Run:

```bash
uv run python -m pytest tests/test_pr_report_archive.py -v
```

Expected: FAIL because `.github/pr-report/build_archive.py` or its public interfaces do not exist.

- [x] **Step 3: dataclass, 경로 탐색, 안전한 내장 JSON 파서 구현**

`.github/pr-report/build_archive.py`에 다음 공개 타입과 핵심 검증을 구현한다.

```python
"""Merged PR report archive builder."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Mapping


class ArchiveBuildError(RuntimeError):
    """Prevent publishing an incomplete or corrupt archive."""


@dataclass(frozen=True)
class ReportSnapshot:
    number: int
    summary_ko: tuple[str, str, str]


@dataclass(frozen=True)
class PullRequestMetadata:
    number: int
    title: str
    author: str
    merged_at: str


@dataclass(frozen=True)
class ArchiveEntry:
    number: int
    title: str
    author: str
    merged_at: str
    summary_ko: tuple[str, str, str]
    report_url: str


class _ReportDataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._inside = False
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script" and values.get("id") == "report-data":
            self._inside = True

    def handle_endtag(self, tag):
        if tag == "script" and self._inside:
            self._inside = False

    def handle_data(self, data):
        if self._inside:
            self.parts.append(data)
```

`discover_report_pages`는 `pages_root / "pr"` 바로 아래의 숫자 디렉터리만
허용한다. `extract_report_snapshot`은 마커 한 개, 객체형 JSON,
`pr.number`와 경로 PR 번호 일치, 비어 있지 않은 문자열 요약 정확히 3개를
검증한다.

- [x] **Step 4: merge 필터·정렬·오류 격리 실패 테스트 작성**

```python
def test_builds_only_merged_entries_and_sorts_newest_first(tmp_path):
    module = _load_module()
    _write_report(tmp_path, 344)
    _write_report(tmp_path, 345, schema_version=2)
    merged = {
        344: module.PullRequestMetadata(344, "JSON 전환", "alice", "2026-07-24T12:00:00Z"),
        345: module.PullRequestMetadata(345, "문서 갱신", "bob", "2026-07-25T12:00:00Z"),
    }

    entries = module.build_archive_entries(tmp_path, merged)

    assert [entry.number for entry in entries] == [345, 344]
    assert entries[0].title == "문서 갱신"
    assert entries[0].report_url == "pr/345/"


def test_does_not_parse_corrupt_unmerged_report(tmp_path):
    module = _load_module()
    corrupt = tmp_path / "pr" / "340"
    corrupt.mkdir(parents=True)
    (corrupt / "index.html").write_text("broken", encoding="utf-8")
    _write_report(tmp_path, 345)
    merged = {
        345: module.PullRequestMetadata(345, "정상", "bob", "2026-07-25T12:00:00Z")
    }

    assert [entry.number for entry in module.build_archive_entries(tmp_path, merged)] == [345]


def test_fails_when_merged_report_is_corrupt(tmp_path):
    module = _load_module()
    corrupt = tmp_path / "pr" / "345"
    corrupt.mkdir(parents=True)
    (corrupt / "index.html").write_text("broken", encoding="utf-8")
    merged = {
        345: module.PullRequestMetadata(345, "정상", "bob", "2026-07-25T12:00:00Z")
    }

    with pytest.raises(module.ArchiveBuildError, match="345"):
        module.build_archive_entries(tmp_path, merged)
```

- [x] **Step 5: GitHub Pulls API 조회와 entry 조립 구현**

`fetch_merged_pull_requests`는 다음 명령을 실행해 닫힌 PR 전체를 페이지별
중첩 배열로 받고, `merged_at is not None`인 항목만 반환한다.

```python
def fetch_merged_pull_requests(repository: str) -> dict[int, PullRequestMetadata]:
    raw = run(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repository}/pulls?state=closed&per_page=100",
        ]
    )
    pages = json.loads(raw)
    merged: dict[int, PullRequestMetadata] = {}
    for page in pages:
        for item in page:
            if item.get("merged_at") is None:
                continue
            number = int(item["number"])
            merged[number] = PullRequestMetadata(
                number=number,
                title=item["title"],
                author=item["user"]["login"],
                merged_at=item["merged_at"],
            )
    return merged
```

명령 실패, JSON 형태 오류, 필수 API 필드 누락은 `ArchiveBuildError`로
감싸 원인과 함께 종료한다. `build_archive_entries`는 먼저 PR 번호를
merge 메타데이터와 교집합한 뒤에만 HTML을 파싱하고, `merged_at`과
PR 번호 내림차순으로 안정 정렬한다.

- [x] **Step 6: Task 1 테스트와 lint 실행**

Run:

```bash
uv run python -m pytest tests/test_pr_report_archive.py -v
uv run --no-sync ruff check .github/pr-report/build_archive.py tests/test_pr_report_archive.py
```

Expected: all Task 1 tests PASS and ruff exits 0.

- [x] **Step 7: 데이터 수집기 커밋**

```bash
git add .github/pr-report/build_archive.py tests/test_pr_report_archive.py
git commit -m "feat: merge PR 리포트 아카이브 데이터 생성 (#348)"
```

---

### Task 2: 정적 페이지 렌더링과 검색

**Files:**
- Create: `.github/pr-report/archive-template.html`
- Create: `.github/pr-report/archive.js`
- Modify: `.github/pr-report/build_archive.py`
- Modify: `tests/test_pr_report_archive.py`
- Create: `tests/test_pr_report_archive_search.py`

**Interfaces:**
- Consumes: Task 1의 `ArchiveEntry`
- Produces: `serialize_archive(entries: Sequence[ArchiveEntry], generated_at: str) -> dict`
- Produces: `render_archive(template_path: Path, payload: Mapping[str, object]) -> str`
- Produces: `write_archive(output_dir: Path, template_path: Path, javascript_path: Path, payload: Mapping[str, object]) -> None`
- Produces: JavaScript `matchesArchiveEntry(entry, rawQuery) -> boolean`
- Produces: `index.html`, `archive.json`, `archive.js`

- [x] **Step 1: 산출물·escaping 실패 테스트 작성**

`tests/test_pr_report_archive.py`에 다음을 추가한다.

```python
def test_writes_json_html_and_javascript_without_script_breakout(tmp_path):
    module = _load_module()
    template = tmp_path / "template.html"
    template.write_text(
        '<script id="archive-data" type="application/json">'
        "/*__ARCHIVE_DATA__*/</script>",
        encoding="utf-8",
    )
    javascript = tmp_path / "archive.js"
    javascript.write_text("window.archiveLoaded = true;", encoding="utf-8")
    entry = module.ArchiveEntry(
        number=345,
        title="안전성 </script><script>alert(1)</script>",
        author="bob",
        merged_at="2026-07-25T12:00:00Z",
        summary_ko=("요약 1", "요약 2", "요약 3"),
        report_url="pr/345/",
    )
    payload = module.serialize_archive([entry], "2026-07-26T00:00:00Z")

    module.write_archive(tmp_path / "out", template, javascript, payload)

    archive = json.loads((tmp_path / "out" / "archive.json").read_text(encoding="utf-8"))
    html = (tmp_path / "out" / "index.html").read_text(encoding="utf-8")
    assert archive["schema_version"] == 1
    assert archive["reports"][0]["number"] == 345
    assert "</script><script>alert(1)" not in html
    assert "<\\/script><script>alert(1)" in html
    assert (tmp_path / "out" / "archive.js").read_text() == "window.archiveLoaded = true;"


def test_refuses_template_without_archive_placeholder(tmp_path):
    module = _load_module()
    template = tmp_path / "template.html"
    template.write_text("<html></html>", encoding="utf-8")

    with pytest.raises(module.ArchiveBuildError, match="ARCHIVE_DATA"):
        module.render_archive(template, {"schema_version": 1, "reports": []})
```

- [x] **Step 2: 렌더링 테스트가 공개 함수 부재로 실패하는지 확인**

Run:

```bash
uv run python -m pytest tests/test_pr_report_archive.py -k "writes_json or refuses_template" -v
```

Expected: FAIL because serialization and rendering functions do not exist.

- [x] **Step 3: payload 직렬화와 완전 생성 후 쓰기 구현**

`.github/pr-report/build_archive.py`에 다음 계약을 구현한다.

```python
ARCHIVE_PLACEHOLDER = "/*__ARCHIVE_DATA__*/"


def serialize_archive(entries, generated_at):
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "reports": [
            {
                **asdict(entry),
                "summary_ko": list(entry.summary_ko),
            }
            for entry in entries
        ],
    }


def render_archive(template_path, payload):
    template = template_path.read_text(encoding="utf-8")
    if ARCHIVE_PLACEHOLDER not in template:
        raise ArchiveBuildError(f"{ARCHIVE_PLACEHOLDER} not found in {template_path}")
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return template.replace(ARCHIVE_PLACEHOLDER, data, 1)
```

`write_archive`는 모든 문자열을 메모리에서 생성·검증한 뒤 출력 디렉터리를
만들고 세 파일을 쓴다. 파싱이나 렌더링 중 실패하면 기존 출력 파일을
건드리지 않는다.

- [x] **Step 4: 순수 검색 함수의 실패 테스트 작성**

`tests/test_pr_report_archive_search.py`에서 Node로 `archive.js`를 가져와
번호·제목·작성자 검색을 검증한다.

```python
import json
import subprocess
from pathlib import Path

ARCHIVE_JS = (
    Path(__file__).resolve().parents[1] / ".github" / "pr-report" / "archive.js"
)


def _matches(query: str) -> bool:
    entry = {
        "number": 345,
        "title": "ONNX 배포 문서 갱신",
        "author": "Waieiches",
    }
    script = (
        f"const a=require({json.dumps(str(ARCHIVE_JS))});"
        f"process.stdout.write(String(a.matchesArchiveEntry("
        f"{json.dumps(entry, ensure_ascii=False)},{json.dumps(query, ensure_ascii=False)})));"
    )
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return result.stdout == "true"


def test_search_matches_number_title_and_author_case_insensitively():
    assert _matches("345")
    assert _matches("onnx")
    assert _matches("WAIEICHES")
    assert not _matches("redis")
```

- [x] **Step 5: archive.js와 반응형 템플릿 구현**

`.github/pr-report/archive.js`는 브라우저 전역과 CommonJS에 같은 순수
함수를 노출한다.

```javascript
"use strict";

function matchesArchiveEntry(entry, rawQuery) {
  var query = String(rawQuery || "").trim().toLocaleLowerCase("ko-KR");
  if (!query) return true;
  var haystack = [String(entry.number), entry.title, entry.author]
    .join(" ")
    .toLocaleLowerCase("ko-KR");
  return haystack.indexOf(query) !== -1;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { matchesArchiveEntry: matchesArchiveEntry };
}
if (typeof window !== "undefined") {
  window.matchesArchiveEntry = matchesArchiveEntry;
}
```

브라우저 초기화 코드는 `DOMContentLoaded` 후 `archive-data` JSON을
파싱하고 `textContent`, `createElement`, `setAttribute`만 사용해 카드를
만든다. 검색 `input` 이벤트마다 `matchesArchiveEntry`로 필터링하며,
등록 수와 빈 결과 문구를 갱신한다.

`.github/pr-report/archive-template.html`에는 다음 필수 요소를 둔다.

```html
<main class="archive-shell">
  <header>
    <p class="eyebrow">Autoresearch</p>
    <h1>PR Report Archive</h1>
    <p><strong id="report-count">0</strong>개의 merge 리포트</p>
  </header>
  <label for="archive-search">PR 번호·제목·작성자 검색</label>
  <input id="archive-search" type="search" autocomplete="off">
  <p id="empty-state" hidden>검색 결과가 없습니다.</p>
  <section id="report-list" aria-live="polite"></section>
</main>
<script id="archive-data" type="application/json">/*__ARCHIVE_DATA__*/</script>
<script src="./archive.js"></script>
```

기존 PR Report의 배경색, 카드 테두리, 본문색, 포인트색을 CSS custom
properties로 옮겨 재사용하고 `max-width`, `clamp`, 한 열 카드 목록,
모바일 여백을 정의한다.

- [x] **Step 6: Task 2 테스트와 lint 실행**

Run:

```bash
uv run python -m pytest tests/test_pr_report_archive.py tests/test_pr_report_archive_search.py -v
uv run --no-sync ruff check .github/pr-report/build_archive.py tests/test_pr_report_archive.py tests/test_pr_report_archive_search.py
```

Expected: all Task 1–2 tests PASS and ruff exits 0.

- [x] **Step 7: 정적 페이지와 검색 커밋**

```bash
git add .github/pr-report/archive-template.html .github/pr-report/archive.js \
  .github/pr-report/build_archive.py tests/test_pr_report_archive.py \
  tests/test_pr_report_archive_search.py
git commit -m "feat: PR 리포트 아카이브 화면과 검색 추가 (#348)"
```

---

### Task 3: CLI와 GitHub Pages 갱신 workflow

**Files:**
- Modify: `.github/pr-report/build_archive.py`
- Create: `.github/workflows/pr-report-archive.yml`
- Modify: `tests/test_pr_report_archive.py`
- Create: `tests/test_pr_report_archive_workflow.py`

**Interfaces:**
- Consumes: Task 1–2의 수집·직렬화·쓰기 함수
- Produces: CLI `python .github/pr-report/build_archive.py --pages-root PATH --template PATH --javascript PATH --output-dir PATH --repository OWNER/REPO`
- Produces: GitHub Actions workflow `PR Report Archive`

- [x] **Step 1: CLI 성공·실패 원자성 테스트 작성**

`tests/test_pr_report_archive.py`에 `main(argv, merged_prs=None)` 주입점을
사용하는 테스트를 추가한다.

```python
def test_cli_writes_complete_archive(tmp_path):
    module = _load_module()
    _write_report(tmp_path / "pages", 345, schema_version=2)
    template = tmp_path / "template.html"
    template.write_text("/*__ARCHIVE_DATA__*/", encoding="utf-8")
    javascript = tmp_path / "archive.js"
    javascript.write_text('"use strict";', encoding="utf-8")
    merged = {
        345: module.PullRequestMetadata(345, "완료", "bob", "2026-07-25T12:00:00Z")
    }

    exit_code = module.main(
        [
            "--pages-root", str(tmp_path / "pages"),
            "--template", str(template),
            "--javascript", str(javascript),
            "--output-dir", str(tmp_path / "site"),
            "--repository", "SKYAHO/Autoresearch",
        ],
        merged_prs=merged,
    )

    assert exit_code == 0
    assert (tmp_path / "site" / "index.html").exists()
    assert (tmp_path / "site" / "archive.json").exists()
    assert (tmp_path / "site" / "archive.js").exists()


def test_cli_keeps_existing_output_when_generation_fails(tmp_path):
    module = _load_module()
    site = tmp_path / "site"
    site.mkdir()
    existing = site / "index.html"
    existing.write_text("last-known-good", encoding="utf-8")

    exit_code = module.main(
        [
            "--pages-root", str(tmp_path / "missing"),
            "--template", str(tmp_path / "missing-template"),
            "--javascript", str(tmp_path / "missing-js"),
            "--output-dir", str(site),
            "--repository", "SKYAHO/Autoresearch",
        ],
        merged_prs={},
    )

    assert exit_code == 1
    assert existing.read_text(encoding="utf-8") == "last-known-good"
```

- [x] **Step 2: argparse CLI와 임시 디렉터리 기반 교체 구현**

`main`은 UTC `generated_at`을 만들고, `output_dir`의 형제 임시
디렉터리에서 세 산출물을 완성한 뒤 `Path.replace`로 개별 파일을
교체한다. 예외는 stderr에 `[pr-report-archive]` 접두사로 출력하고 1을
반환한다. 테스트에서 API를 우회할 수 있도록 `merged_prs`가 `None`일
때만 `fetch_merged_pull_requests`를 호출한다.

- [x] **Step 3: workflow 계약의 실패 테스트 작성**

`tests/test_pr_report_archive_workflow.py`를 추가한다.

```python
from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "pr-report-archive.yml"
)


def _load_workflow():
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_archive_workflow_has_all_rebuild_triggers():
    workflow = _load_workflow()
    triggers = workflow["on"]
    assert triggers["pull_request"]["types"] == ["closed"]
    assert triggers["workflow_run"]["workflows"] == ["PR Comprehension Report"]
    assert triggers["workflow_run"]["types"] == ["completed"]
    assert "workflow_dispatch" in triggers


def test_archive_workflow_serializes_pages_push_and_preserves_reports():
    workflow = _load_workflow()
    job = workflow["jobs"]["publish-archive"]
    assert job["concurrency"] == {
        "group": "pr-report-publish",
        "cancel-in-progress": "false",
    }
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "github.event.pull_request.merged == true" in text
    assert "ref: gh-pages" in text
    assert "path: pages" in text
    assert "keep_files: true" in text
    assert "build_archive.py" in text
```

- [x] **Step 4: PR Report Archive workflow 구현**

`.github/workflows/pr-report-archive.yml`의 핵심 계약은 다음과 같다.

```yaml
name: PR Report Archive

on:
  pull_request:
    types: [closed]
  workflow_run:
    workflows: ["PR Comprehension Report"]
    types: [completed]
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: read

jobs:
  publish-archive:
    if: github.event_name != 'pull_request' || github.event.pull_request.merged == true
    runs-on: ubuntu-latest
    concurrency:
      group: pr-report-publish
      cancel-in-progress: false
    steps:
      - name: Checkout main
        uses: actions/checkout@v6
        with:
          ref: main
          fetch-depth: 1

      - name: Checkout current Pages content
        uses: actions/checkout@v6
        with:
          ref: gh-pages
          path: pages
          fetch-depth: 1

      - name: Build merged PR report archive
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          python .github/pr-report/build_archive.py \
            --pages-root pages \
            --template .github/pr-report/archive-template.html \
            --javascript .github/pr-report/archive.js \
            --output-dir site \
            --repository "$GITHUB_REPOSITORY"

      - name: Deploy archive to gh-pages
        uses: peaceiris/actions-gh-pages@v4
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: ./site
          keep_files: true
          commit_message: "pr-report-archive: rebuild"
```

- [x] **Step 5: Task 3 테스트와 workflow 정적 검사 실행**

Run:

```bash
uv run python -m pytest tests/test_pr_report_archive.py tests/test_pr_report_archive_workflow.py -v
uv run --no-sync ruff check .github/pr-report/build_archive.py tests/test_pr_report_archive.py tests/test_pr_report_archive_workflow.py
git diff --check
```

If `actionlint` is installed, also run:

```bash
actionlint .github/workflows/pr-report-archive.yml
```

Expected: tests PASS, ruff and `git diff --check` exit 0, actionlint exits 0 when available.

- [x] **Step 6: CLI와 workflow 커밋**

```bash
git add .github/pr-report/build_archive.py .github/workflows/pr-report-archive.yml \
  tests/test_pr_report_archive.py tests/test_pr_report_archive_workflow.py
git commit -m "ci: merge PR 리포트 아카이브 자동 갱신 (#348)"
```

---

### Task 4: 과거 데이터 실측, 브라우저 QA, 운영 문서

**Files:**
- Modify: `docs/specs/2026-07-24-pr-comprehension-report.md`
- Modify: `docs/specs/2026-07-26-pr-report-archive-design.md`
- Modify: `docs/plans/2026-07-26-pr-report-archive.md`

**Interfaces:**
- Consumes: Task 3의 CLI
- Produces: 실제 `origin/gh-pages` 18개 리포트 기반 로컬 아카이브와 QA 증거
- Produces: merge 후 `workflow_dispatch` 최초 백필 운영 절차

- [x] **Step 1: 실제 gh-pages를 임시 worktree로 체크아웃**

PowerShell에서 저장소 내부가 아닌 임시 경로를 명시적으로 만들고 대상이
임시 디렉터리인지 확인한 뒤 사용한다.

```powershell
$archiveQaRoot = Join-Path ([System.IO.Path]::GetTempPath()) "autoresearch-pr-archive-qa"
if (Test-Path -LiteralPath $archiveQaRoot) {
    throw "QA 경로가 이미 존재합니다: $archiveQaRoot"
}
git worktree add --detach $archiveQaRoot origin/gh-pages
```

- [x] **Step 2: 실제 merge 상태로 정적 아카이브 생성**

```powershell
uv run python .github/pr-report/build_archive.py `
  --pages-root $archiveQaRoot `
  --template .github/pr-report/archive-template.html `
  --javascript .github/pr-report/archive.js `
  --output-dir .tmp/pr-report-archive-site `
  --repository SKYAHO/Autoresearch
```

Expected: exit 0, `.tmp/pr-report-archive-site/index.html`,
`archive.json`, `archive.js` 생성. `archive.json`의 모든 PR은 GitHub
API에서 merge 상태이고 최신 머지순이다.

- [x] **Step 3: 로컬 서버와 실제 브라우저로 화면·검색 QA**

```powershell
uv run python -m http.server 8765 --directory .tmp/pr-report-archive-site
```

브라우저에서 `http://127.0.0.1:8765/`를 열고 다음을 확인한다.

- 데스크톱과 모바일 폭에서 텍스트 겹침이나 가로 스크롤이 없다.
- 카드에 번호·제목·작성자·머지 날짜·요약 3줄이 보인다.
- `345`, `ONNX`, `example-user` 검색이 해당 카드만 남긴다.
- 존재하지 않는 검색어에서 빈 결과 문구가 보인다.
- `리포트 보기`가 `/pr/<번호>/` 상대 경로를 가리킨다.

- [x] **Step 4: 운영 문서와 검증 결과 갱신**

`docs/specs/2026-07-24-pr-comprehension-report.md`의 운영 섹션에 다음을
추가한다.

```markdown
### Merge 리포트 아카이브

merge된 PR 리포트는 Pages 루트
`https://skyaho.github.io/Autoresearch/`에서 최신 머지순으로 제공한다.
`pr-report-archive.yml`은 PR merge, PR Report 완료, 수동 실행 때
`gh-pages/pr/*`를 다시 스캔하며 기존 개별 리포트 파일은 보존한다.
최초 배포 또는 복구는 Actions의 **PR Report Archive → Run workflow**로
실행한다.
```

`docs/specs/2026-07-26-pr-report-archive-design.md`의 Status를
`구현 완료 (#348)`로 바꾸고 실제 백필 대상 수, Python 테스트 수,
브라우저 QA 결과를 기록한다. 이 계획의 완료된 체크박스를 `[x]`로 갱신한다.

- [x] **Step 5: 전체 관련 테스트와 저장소 검증 실행**

Run:

```bash
uv run python -m pytest tests/test_pr_report_archive.py \
  tests/test_pr_report_archive_search.py \
  tests/test_pr_report_archive_workflow.py -v
uv run --no-sync ruff check .github/pr-report/build_archive.py \
  tests/test_pr_report_archive.py \
  tests/test_pr_report_archive_search.py \
  tests/test_pr_report_archive_workflow.py
git diff --check
```

Expected: all archive tests PASS, ruff and `git diff --check` exit 0.

- [x] **Step 6: 임시 QA 자원 정리**

서버를 종료한 뒤 경로를 다시 확인하고 git worktree만 제거한다.

```powershell
$resolvedQaRoot = (Resolve-Path -LiteralPath $archiveQaRoot).Path
if ($resolvedQaRoot -ne $archiveQaRoot) {
    throw "예상하지 않은 QA 경로입니다: $resolvedQaRoot"
}
git worktree remove $archiveQaRoot
```

`.tmp/pr-report-archive-site`는 추적하지 않는다. 필요하면 경로가 저장소의
`.tmp` 아래인지 확인한 후 PowerShell `Remove-Item -Recurse -LiteralPath`로
삭제한다.

- [x] **Step 7: 문서와 최종 검증 커밋**

```bash
git add docs/specs/2026-07-24-pr-comprehension-report.md \
  docs/specs/2026-07-26-pr-report-archive-design.md \
  docs/plans/2026-07-26-pr-report-archive.md
git commit -m "docs: PR 리포트 아카이브 운영 절차 기록 (#348)"
```

---

## Merge 후 운영

PR이 merge되어 workflow 파일이 `main`에 들어간 뒤 Actions의
**PR Report Archive → Run workflow**를 한 번 실행한다. 생성된 Pages
루트에서 기존 merge 리포트가 소급 등록됐는지 확인한다. 이후에는
`pull_request.closed(merged)`와 `PR Comprehension Report`의
`workflow_run`이 자동으로 전체 인덱스를 재생성한다.

## 최종 완료 기준

- [x] 관련 Python·Node 계약 테스트가 모두 통과한다.
- [x] ruff, `git diff --check`, 가능한 경우 actionlint가 통과한다.
- [x] 실제 기존 v1 13개와 v2 5개 리포트를 오류 없이 읽는다.
- [x] 실제 GitHub merge 상태를 기준으로 미머지 리포트가 제외된다.
- [x] 로컬 브라우저에서 검색, 빈 상태, 반응형 화면, 링크를 확인한다.
- [x] 배포 workflow가 기존 `pr/` 파일을 보존하고 shared concurrency를 사용한다.
- [x] merge 후 최초 수동 백필 절차가 운영 문서에 기록된다.
