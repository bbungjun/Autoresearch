# Agent Workflow Reference

> Last Updated: 2026-08-15

GitHub 워크플로우 전체 가이드: Issue → Branch → Commit → PR → Review →
Merge. 모든 기능 작업의 운영 표준입니다. 사람용 요약은
`CONTRIBUTING.md`에 있으며, 두 문서의 규칙은 항상 일치해야 합니다.

## When To Use This Doc

- 새 기능이나 버그 수정을 시작하며 전체 워크플로우가 필요할 때
- 커밋 메시지나 PR 본문을 작성할 때
- PR이 워크플로우를 따르는지 검증할 때
- 브랜치 이름, 머지 방식, 옛 조직 Project 운영 기록이 필요할 때

## Workflow Overview

```
Issue 생성
    ↓
Branch 생성 (이슈의 Create a branch, feat/이슈번호-설명)
    ↓
Commit (<type>: 한국어 설명)
    ↓
PR 생성 (Draft 또는 Ready)
    ↓
독립 리뷰 worker 동료 리뷰 → 사람이 최종 판정 → Squash Merge
    ↓
Issue 자동 close
```

> **현재 비활성 — 옛 조직 GitHub Projects 자동 전환**: 개인 저장소에는
> Project가 없습니다. 옛 조직에서는 이슈 생성 시 `Todo`로 자동 추가하고,
> PR merge·이슈 close 시 `Done`으로 자동 전환했으며, 복구 근거로
> 과거 절차를 보존합니다.

## Issue Creation

**이슈를 만드는 경우:**
- 새 기능 또는 개선
- 버그 발견
- 실험 계획 또는 결과 기록
- 문서, 설정, 리팩터링 등 추적이 필요한 작업
- PR 리뷰 중 생긴 범위 밖 후속 작업

아주 작은 오타 수정은 바로 PR로 처리할 수 있습니다.

**Issue Forms** (`.github/ISSUE_TEMPLATE/`, 빈 이슈 생성 불가):

| Form | 제목 prefix | 자동 label | 필수 내용 |
|---|---|---|---|
| `feature.yml` | `[FEAT]` | `feature` | 목적, 작업 범위, 영향 컴포넌트, 완료 조건 |
| `bug.yml` | `[BUG]` | `bug` | 현상, 재현 방법, 기대 동작, 환경, 로그 |
| `experiment.yml` | `[EXP]` | `experiment` | 가설, 데이터셋, 모델, 피처, 평가지표, Champion 대비 결과, 결론 |
| `auto_research.yml` | `[AR]` | `auto-experiment` | 입력 필드 21개 중 18개를 `tools/auto_research_issue_branch.py`가 fail-closed로 파싱 (`선행 연구 참조`와 `보조 관측 지표`는 선택, `결과`는 에이전트가 사후 기입) |

GitHub는 `form 선택 → label 자동 적용` 방식으로 동작합니다. 옛 조직
Project의 `Add item`으로 제목만 추가하면 form을 우회했으며,
새 작업은 현재도 Issues 화면에서 생성합니다.

`[AR]` 이슈의 `auto-experiment` label은 Auto Research 분류와 promotion guard에
사용하며 **label 자체는 브랜치를 만들지 않습니다.** Form을 우회해 API로 발행하면
label이 자동 적용되지 않으므로 반드시 직접 부여합니다.

**문서 전용 이슈 (`[DOCS]`)**: Issue Form이 없습니다(`[CHORE]`, `[PERF]`
등 관례로 쓰이는 다른 prefix도 마찬가지입니다). 문서와 판단 기록만
산출물인 작업에 사용하며, Form이 없으므로 제목 prefix와 label을 직접
지정합니다.

```bash
gh issue create --title "[DOCS] ..." --label documentation --assignee @me
```

**에이전트로 이슈를 생성할 때 (`gh issue create`):**

`gh issue create`는 Issue Form을 우회하므로, 라벨과 담당자를 명시하지
않으면 빈 상태로 생성됩니다. 아래 플래그를 반드시 지정합니다.

- `--label`: 작업 성격에 맞는 라벨 1개 이상 (`feature`, `bug`,
  `experiment`, `enhancement`, `documentation` 중 택). Issue Form의
  자동 라벨과 동일한 기준을 적용합니다.
- `--assignee @me`: 이슈 작성자를 담당자로 자동 할당합니다.

```bash
gh issue create \
  --title "[FEAT] ..." \
  --label feature \
  --assignee @me \
  --body-file issue-body.md
```

## Branch Naming

**코드가 변경되는 작업은 반드시 이슈를 먼저 발행하고, 그 이슈에서 브랜치를
생성합니다.** GitHub 이슈 우측 `Development > Create a branch`를 사용하면
브랜치가 이슈에 자동 연결(`main` 기준 분기)됩니다. 로컬에서 임의로
분기하는 대신 이슈에서 만든 브랜치를 체크아웃해 작업합니다.

**형식:** `<type>/<이슈번호>-<간략한-설명>`

**Type:** `feat/`, `fix/`, `exp/`, `docs/`, `refactor/`, `chore/`

- 영어 소문자, 숫자, 하이픈만 사용합니다.
- 이슈 번호를 반드시 포함합니다.
- 한 브랜치에는 하나의 주요 목적만 담습니다.

**예외 — Auto Research 실험 브랜치:** `[AR]` 이슈의
`exp/<이슈번호>` 브랜치는 사람이 만들지 않습니다.

> **현재 비활성:** 아래 자동 생성 절차는 옛 조직 GitHub App과 GCP executor를
> 전제로 하며 개인 저장소에서는 동작하지 않습니다. 복구 근거를 위해 과거 절차를
> 삭제하지 않고 보존합니다.

Agent Orchestration API가 이슈 발행 전에 **`dev` tip을 DB의
`base_dev_sha`로 봉인**하고, launcher가 만든 executor Pod가 나중에 그 SHA에서만
브랜치를 생성합니다. `exp/`는 사람과 자동화가 **같은 prefix를 공유**하며, 자동 생성 브랜치는 이슈
번호만으로 이름이 정해져 설명 slug가 붙지 않습니다(`exp/589`). 자동 생성된
exp 브랜치를 삭제하거나 force-push하지 않습니다 — ruleset이 막아 주지 않으며,
삭제하면 launcher가 자동 복구하지 않고 다른 tip으로 바꾸면 executor 재시도와 승격
계보 검증이 fail-closed됩니다. 아래 [Branch protection](#branch-protection) 참조.

```bash
# 이슈에서 Create a branch로 생성(예: feat/45-docs-system-phase1) 후
git fetch origin
git switch feat/45-docs-system-phase1
```

## Commit Messages

**형식:** `<type>: <한국어 설명>`

| type | 의미 |
|---|---|
| `feat` | 새 기능 |
| `fix` | 버그 수정 |
| `exp` | 실험 코드 또는 실험 설정 |
| `docs` | 문서 |
| `refactor` | 기능 변화 없는 구조 개선 |
| `test` | 테스트 추가 또는 수정 |
| `chore` | 빌드, 설정, 패키지, CI 등 |

**규칙:**
1. 한 커밋에는 하나의 논리적 변경만 담습니다.
2. 포맷 변경과 기능 변경을 섞지 않습니다.
3. 제목은 현재형 동사로 50자 이내로 씁니다.

```text
feat: CLAUDE.md 라우팅 표 추가
test: config 로딩 단위 테스트 추가
docs: 아키텍처 개요 갱신
```

## PR Creation

**PR 생성 전 체크:**
- [ ] 테스트 통과: `uv run python -m pytest -n 4 --dist loadfile --durations=25`
- [ ] 시크릿, `.env`, 데이터 파일이 포함되지 않았다
- [ ] 커밋 메시지가 컨벤션을 따른다
- [ ] PR 라벨을 1개 이상 부착했다 (아래 매핑 참조)
- [ ] 담당자를 작성자 본인으로 지정했다

**PR 담당자 (assignee):**

PR을 생성하는 에이전트(또는 사용자)가 해당 작업의 담당자이므로, 작성자를
담당자로 지정합니다. `gh pr create` 사용 시 `--assignee @me` 플래그를
추가합니다.

```bash
gh pr create \
  --title "..." \
  --label feature \
  --assignee @me \
  --body-file pr-body.md
```

**PR 라벨 (Release Drafter 연동):**

Release Drafter가 라벨 기반으로 release note 분류와 semantic version을
자동 계산합니다. 라벨이 없으면 `Other Changes`(patch)로 분류되므로, 변경
성격에 맞는 라벨을 반드시 부착합니다.

| 라벨 | 분류 | 버전 영향 |
|---|---|---|
| `feature`, `enhancement` | Features | minor |
| `bug` | Bug Fixes | patch |
| `breaking` | Breaking Changes | major |
| `documentation` | Documentation | patch (default) |
| `experiment` | Experiments | patch (default) |

**PR 본문** (`.github/PULL_REQUEST_TEMPLATE.md` 사용):

```markdown
## 작업 내용
변경 요약

## 변경 사항
- 항목 1
- 항목 2

## 관련 이슈
Closes #45

## 리뷰어 참고사항
검증 명령과 결과
```

**좋은 PR의 조건:**
- 하나의 이슈를 해결합니다.
- 제목만 봐도 변경 목적이 드러납니다 (커밋 컨벤션과 동일 형식).
- 변경 사항이 bullet list로 정리되어 있습니다.
- 무관한 리팩터링과 기능 변경을 섞지 않습니다.
- 리뷰 중 발견된 별도 작업은 새 이슈로 분리합니다.

**Draft vs Ready:**
- Draft: 작업 중이거나 이른 피드백이 필요할 때
- Ready: 정식 리뷰를 요청할 때

## Review & Approval

**머지 조건:**
- 코디네이터 에이전트가 계획하고 Codex worker가 구현한다
- 구현에 참여하지 않은 독립 리뷰 worker가 동료 리뷰를 수행한다
- 리뷰 발견 사항을 반영하고 필요한 검증을 다시 수행한다
- 모든 conversation resolved
- CI status check 통과
- Ready for review 상태
- 사람이 리뷰 결과와 검증 근거를 확인하고 최종 머지 여부를 판정한다

팀원 GitHub Approve는 현재 개인 저장소의 머지 조건이 아닙니다. 독립 리뷰
worker의 결과가 동료 리뷰 근거이고, 자동화가 사람의 최종 판정을 대신하지 않습니다.

**리뷰어 확인 사항:**
- 이슈의 목적과 PR 변경이 일치하는가
- 변경 범위가 너무 크지 않은가
- 테스트 또는 검증 방법이 충분한가
- `Closes #이슈번호`가 있는가
- 불필요한 파일, 캐시, 시크릿이 포함되지 않았는가

**현재 비활성 — Claude 자동 리뷰·PR 이해 리포트:** 옛 조직 저장소에서는 PR
open/Ready 전환과 `/claude-review`, `/claude-report` 댓글로 자동 리뷰와 리포트를
실행했습니다. 현재는 `claude.yml`, `pr-report.yml`, `pr-report-archive.yml`을
`.github/workflows-disabled/`로 옮겨 이 trigger가 동작하지 않습니다. Claude 리뷰는
개인 계정의 `CLAUDE_CODE_OAUTH_TOKEN`을 등록하면 되살릴 수 있으며, 전체 복구 조건은
`.github/workflows-disabled/README.md`를 따릅니다.

## Branch protection

> **현재 유효하지 않은 옛 조직 설정:** 아래 ruleset과 ID는
> `SKYAHO/Autoresearch`의 `main-protection`(`18360502`)과
> `dev-protection`(`20261204`) 기록입니다. 개인 저장소에는 이 ruleset이 존재하지
> 않아 현재 머지 조건이나 브랜치 보호 상태를 설명하지 않습니다. 복구·재설계의
> 근거로 쓰기 위해 삭제하지 않습니다.

옛 조직 저장소의 보호는 저장소 파일이 아니라 **GitHub ruleset**으로 적용됐습니다.
당시 설정은 다음 명령으로 read-back했습니다:

```bash
gh api repos/SKYAHO/Autoresearch/rulesets
gh api repos/SKYAHO/Autoresearch/rulesets/18360502   # main-protection
gh api repos/SKYAHO/Autoresearch/rulesets/20261204   # dev-protection
```

### `main` — `main-protection` (id `18360502`)

조건은 `refs/heads/main`이 아니라 `~DEFAULT_BRANCH`입니다(기본 브랜치가
`main`이므로 지금은 효과가 같습니다). read-back 대조 시 유의합니다.

**기본 브랜치를 바꾸려면 ruleset을 먼저 고정합니다.** 기본 브랜치를 `dev`로
바꾸는 순간 `main-protection`이 `main`을 떠나 `dev`를 따라갑니다. `main`은
보호가 전부 풀리고, `dev`에는 `pull_request` rule이 붙어 아래에서 설명하는
실험 자동 병합이 즉시 중단됩니다. 옮기기 전에 `main-protection`의 조건을
`refs/heads/main`으로 명시 고정해야 합니다.

- 직접 push 금지, PR을 통한 변경만 허용 (`pull_request`)
- approve 1명 필수(`required_approving_review_count: 1`). approve 후 새
  커밋이 push되면 approve가 초기화되고
  (`dismiss_stale_reviews_on_push`), 마지막 push에 대한 승인이 필요합니다
  (`require_last_push_approval`).
- required status check 6개 (`required_status_checks`,
  `strict_required_status_checks_policy: true`): `Ruff`,
  `pytest (Python 3.11)`, `pytest (Python 3.12)`,
  `pytest (feast group)`, `uv lock & proxy export drift`, `Docker build`
- force-push 금지 (`non_fast_forward`), 삭제 금지 (`deletion`)
- squash merge만 허용 (`allowed_merge_methods: ["squash"]`)
- 우회 불가 (`bypass_actors: []`, `current_user_can_bypass: never`) —
  classic branch protection의 "include administrators" 토글과 달리 ruleset은
  bypass actor를 명시하지 않으면 **관리자도 우회하지 못합니다.**

### `dev` — `dev-protection` (id `20261204`)

- 삭제 금지 (`deletion`)
- force-push 금지 (`non_fast_forward`)
- 우회 불가 (`bypass_actors: []`, `current_user_can_bypass: never`) — 팀
  전원에게 예외 없이 적용됩니다.
- **PR 필수·required status check는 걸지 않습니다** (아래 이유 참조)

**`dev`가 오염되면 revert-forward만 가능합니다.** 우회가 불가능하므로 잘못된
candidate가 병합됐을 때 force-push로 되돌릴 수 없고 revert 커밋으로 전진
복구해야 합니다. 이 방식은 진행 중인 실험을 깨지 않습니다 — 기존
`base_dev_sha`가 여전히 `dev`의 조상으로 남아 lineage 검증이 통과합니다.

`dev`는 단순한 통합 브랜치가 아니라 **Auto Research 모든 실험의 기준선**입니다.
Agent Orchestration API의 Contents read 전용 GitHub App이 이슈 발행 전에
`heads/dev` tip을 한 번 읽어 `Experiment.base_dev_sha`에 저장합니다. launcher와
executor는 Job 시작 시 최신 `dev`나 `main`을 다시 읽지 않고, executor는 저장된 SHA에
`exp/<이슈번호>` ref를 생성합니다. 이후 계보 검증도 이 SHA를 기준점으로
삼으므로 `dev`가 force-push·삭제되어 커밋이 사라지면 다음이 깨집니다.

| 깨지는 것 | 근거 | 시점 |
|---|---|---|
| 신규 이슈의 기준 SHA 봉인 | Agent Orchestration API가 `heads/dev`를 읽어 DB에 저장 | 이슈 발행 시 |
| exp 브랜치 생성 | executor Pod가 전달받은 `base_dev_sha`에 ref 생성 | Job 실행 시 |
| dev 병합 | `.github/workflows-disabled/auto-research-dev-promotion.yml:367-372` (`base: 'dev'`) | 즉시 |
| main Draft PR의 lineage 검사 | `.github/workflows-disabled/auto-research-promotion.yml:188-193` (`head: 'dev'`) | 즉시 |
| 진행 중 이슈의 후보 검증 | `.github/workflows-disabled/auto-research-dev-promotion.yml:224-242` | 후보 제출 시 |

**PR 필수·required status check를 `dev`에서 제외한 이유:**
`.github/workflows-disabled/auto-research-dev-promotion.yml:367-372`의
`github.rest.repos.merge({ base: 'dev', head: selectedCandidateSha })`는
**PR을 거치지 않고 `dev` ref를 직접 갱신**합니다.

- `pull_request` rule을 걸면 이 호출이 거부되어 자동 병합이 즉시 멈춥니다.
- `required_status_checks`를 걸어도 마찬가지입니다. `ci.yml:6-8`과
  `lint.yml:6-8`의 `push` 트리거가 `main` 전용이고, 게다가
  `GITHUB_TOKEN`으로 만든 커밋은 workflow를 재귀 트리거하지 않습니다.
  두 이유 각각으로 **`repos.merge`가 만든 dev 커밋에는 check run이
  하나도 생성되지 않아** 영구히 통과할 수 없습니다. 후자는
  `docs/archive/specs/2026-08-01-auto-research-dev-issue-branch.md:112`에
  이미 기록돼 있습니다.

> "`dev`에는 지정할 컨텍스트가 없다"는 서술은 **틀립니다.** `ci.yml`과
> `lint.yml` 모두 `pull_request:` 트리거에 `branches:` 필터가 없어 base가
> `dev`인 PR에서는 6개 컨텍스트가 정상 생성되고, ruleset에 컨텍스트
> 이름을 직접 입력할 수도 있습니다. 성립하는 사실은 **"`repos.merge`로
> 만든 dev 커밋에는 CI가 돌지 않는다"** 입니다.

### `exp/*`, `promote/*` — ruleset 없음 (2026-08-03 판단)

executor와 승격 workflow의 fail-closed 검사만 있으며 **생성 이후의
force-push·삭제 자체는 막지 못합니다.**

- `exp/*` 생성 시: executor는 ref가 없을 때만 DB에 봉인된 `base_dev_sha`로
  생성합니다. 같은 SHA의 기존 ref만 멱등 성공이고 다른 tip은 update·reset·force 없이
  `branch_ref_conflict`로 실패합니다.
- `exp/*` 삭제 후: launcher가 Job 존재 확인 시각을 저장한 뒤에는 TTL로 Job이
  사라져도 자동 재생성하지 않습니다. branch를 지우면 Phase 1이 자동 복구하지 않으며
  후속 승격 입력도 잃습니다.
- `exp/*` force-push: executor 재시도와
  `.github/workflows-disabled/auto-research-dev-promotion.yml`의 후보 계보 검사가
  다른 tip을 거부합니다. 안전하게 실패하지만 작업 결과는 소실됩니다.
- **marker 경계:** Phase 1 executor는 기존 GitHub Actions bot marker를 새로 쓰지
  않습니다. 따라서 새 marker 없는 branch는 현재 promotion workflow 입력이 아니며,
  marker 작성 주체·서명·`base_dev_sha` 검증 재설계가 실제 실험 실행 전 다음 gate입니다.
- `promote/*`: `.github/workflows-disabled/auto-research-promotion.yml:275-288`이 이미 존재하는
  promote 브랜치를 **다른 SHA로 재사용**하는 것만 거부합니다.

**보호를 걸지 않은 이유:** `exp/`는 자동화 전용 네임스페이스가 아닙니다.
위 [Branch Naming](#branch-naming)이 `exp/`를 사람이 쓰는 type으로
규정하고, 사람이 만든 `exp/116-openrouter-provider-ab`와
`exp/396-views-per-day`가 원격에 존재합니다. 양쪽이
`exp/<이슈번호>-<설명>`이라는 **같은 형식**을 쓰므로 ruleset의 ref
패턴으로 구분할 수 없고, `non_fast_forward`는 사람의 rebase를,
`deletion`은 작업 후 브랜치 정리를 함께 막습니다. 보호하려면 **자동화
전용 네임스페이스 분리가 선행**되어야 하며, 이는 브랜치명 생성 규칙과
marker 신뢰 계약을 바꾸는 동작 변경이므로 별도 이슈에서 다룹니다.

## Merging

**Squash and merge만 사용합니다.** 저장소 설정에서 merge commit과
rebase merge는 비활성화되어 있습니다.

1. "Squash and merge" 클릭
2. 머지 커밋 제목을 `<type>: <설명> (#PR번호)` 형식으로 확인
3. Confirm

**결과:**
- 커밋이 하나로 squash됩니다.
- `Closes #이슈번호`로 연결된 이슈가 자동 close됩니다.
- 브랜치가 자동 삭제됩니다.

## GitHub Projects

> **현재 비활성**: 아래는 `SKYAHO / Autoresearch` 조직 Project 운영
> 기록입니다. 개인 저장소에는 Project가 없어 자동 추가·상태 전환·
> 이슈 close 자동화가 동작하지 않습니다. 복구 근거로 과거 절차를 보존합니다.

옛 조직 Project는 작업 상태를 보여주는 보드로 사용했습니다.

| 상태 | 의미 | 전환 |
|---|---|---|
| `Todo` | 시작 전 | 이슈/PR 생성 시 자동 추가 |
| `In Progress` | 작업 중 | 작업 시작 시 직접 이동 |
| `Done` | 완료 | merge/close 시 자동 전환 |

**당시 켜져 있던 자동화:**
- Auto-add to project: open 이슈/PR 자동 추가 (`is:issue,pr is:open`)
- Item added → `Todo` 설정
- Item closed / PR merged → `Done` 설정
- Project에서 `Done`으로 옮기면 이슈 자동 close

## Labels

Issue Form과 자동화를 단순하게 유지하기 위해 `feature`, `bug`,
`experiment`를 우선 사용합니다. 보조: `documentation`,
`good first issue`, `help wanted`, `question`. `enhancement`는
`feature`와 겹치면 `feature`를 우선합니다.

**Auto Research 분류 label — 임의로 제거하지 않습니다.** `auto-experiment`는
Issue Form과 API 발행 경로가 붙이는 분류값이며 executor Pod의 branch 생성 트리거가
아닙니다. 비활성 보관된
`.github/workflows-disabled/auto-research-promotion.yml`도 같은 label을 요구하므로
나중에 옛 승격 절차를 복구할 가능성을 위해 유지합니다. 워크플로우 단언은 비활성
기간에 skip되고 파일을 활성 경로로 되돌리면 자동으로 다시 실행됩니다.

`auto-research`는 트리거가 **아닙니다.** Auto Research 주제를 가리키는 분류
label이며 `[AR]` 이슈에는 붙지 않습니다.

## CI

`.github/workflows/ci.yml`과 `.github/workflows/lint.yml`이 PR과 `main`
push, 수동 실행(`workflow_dispatch`)에서 동작합니다. 두 파일 모두
`pull_request:` 트리거에 `branches:` 필터가 없어 base 브랜치를 가리지
않지만, `push:`는 `main` 전용입니다.

- `ci.yml`: Python 3.11 / 3.12에서 `python -m pytest -n 4 --dist loadfile --durations=25`, feast·postgres 그룹
  테스트, `uv lock & proxy export drift`, 이미지 빌드와 import smoke check
- `lint.yml`: `Ruff`
- `release-drafter.yml`: GitHub가 자동 제공하는 `GITHUB_TOKEN`만 사용하는 release note 초안 갱신

이 세 파일만 `.github/workflows/`에 남아 있습니다. 조직 secrets, GCP 자원,
`gh-pages`, 조직 GitHub App 또는 옛 executor를 요구하는 나머지 워크플로우는
`.github/workflows-disabled/`로 옮겼으며 복구 조건은 그 디렉터리의 `README.md`를
따릅니다.

옛 조직 저장소의 `main-protection`이 요구하던 status check 6개는 `ci.yml` 5개
(`pytest (Python 3.11)`, `pytest (Python 3.12)`, `pytest (feast group)`,
`uv lock & proxy export drift`, `Docker build`)와 `lint.yml` 1개
(`Ruff`)였습니다. 개인 저장소에는 해당 ruleset이 없어 현재 required check는
아니지만 job 이름은 복구 근거로 유지합니다. `pytest (postgres group)`과 이미지별
`Docker build (...)` 서브잡은 실행되지만 옛 required 컨텍스트에는 포함되지 않았습니다.
`Docker build`는 이미지별 서브잡을 모으는 fan-in 집계 잡의 이름입니다. 아래
옛 ruleset 기록이 그 이름을 요구하므로 복구 근거를 위해 유지합니다
(`ci.yml:526-530` 주석).

## Special Cases

### main과 충돌

```bash
git fetch origin main
git rebase origin/main
git push --force-with-lease origin feat/45-...
```

리뷰어의 재리뷰가 필요합니다.

### 리뷰에서 수정 요청

1. 새 커밋으로 수정합니다 (amend 금지).
2. push 후 재리뷰를 요청합니다.

### PR 분리

새 이슈를 만들고 커밋을 새 브랜치로 cherry-pick한 뒤 별도 PR을
생성합니다. 양쪽 PR 본문에 서로 링크를 남깁니다.

## Troubleshooting

- **PR이 merge되지 않을 때:** Draft 상태, 독립 리뷰 worker의 발견 사항과
  conversation 해결 여부, 충돌, CI 실패를 확인합니다. 최종 머지 여부는 사람이
  판정합니다.
- **현재 비활성 — 옛 조직 Project에 항목이 안 보일 때:** `Done`
  컬럼과 view filter를 확인했습니다. 이미 closed/merged된 항목은
  자동 추가 필터에 걸리지 않을 수 있었습니다.
- **이슈가 자동으로 닫히지 않을 때:** PR 본문의 `Closes #이슈번호`,
  `main`으로의 merge 여부, 이슈 번호가 같은 저장소의 번호인지
  확인합니다.
