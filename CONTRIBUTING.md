# 기여 가이드 (Contributing Guide)

AutoResearch 프로젝트에 기여해 주셔서 감사합니다.
원활한 협업을 위해 아래 규칙을 따라 주세요.

- 기준 저장소: `bbungjun/Autoresearch`
- 기준 Project: 현재 없음

---

## 워크플로우

```
이슈 생성 → 브랜치 생성 → 작업 → PR 생성 → 리뷰 → Squash Merge
```

1. **이슈 생성**: 코드가 변경되는 작업은 반드시 이슈를 먼저 발행합니다.
   `Issues > New issue`에서 Issue Form(Feature / Bug / Experiment)을 선택해 작성해 주세요. Form을 선택하면 제목 prefix와 label이 자동으로 적용됩니다. 빈 이슈 생성은 비활성화되어 있습니다.

2. **브랜치 생성**: 브랜치는 **해당 이슈에서 생성**합니다. 이슈 우측 `Development > Create a branch`를 사용하면 브랜치가 이슈에 자동 연결되고, `main` 기준으로 분기됩니다. 브랜치 네이밍 규칙은 아래를 따릅니다.

   > **현재 비활성**: 아래 Auto Research 자동 브랜치 절차는 옛 조직 GitHub App과
   > GCP executor를 전제로 하며 개인 저장소에서는 동작하지 않습니다. 복구 근거를
   > 위해 삭제하지 않고 과거 절차로 보존합니다.
   >
   > **예외 — Auto Research 실험 브랜치**: `[AR]` 이슈의 `exp/<이슈번호>` 브랜치는 사람이 만들지 않습니다. API가 이슈 발행 전에 **`dev` tip을 DB의 `base_dev_sha`로 봉인**하고, launcher가 만든 executor Pod가 나중에 그 SHA에서만 브랜치를 생성합니다. 자세한 내용은 [브랜치 보호 규칙](#브랜치-보호-규칙)을 참조하세요.

3. **작업 및 커밋**: 커밋 컨벤션에 따라 커밋 메시지를 작성합니다.

4. **PR 생성**: PR 템플릿을 채우고, 본문에 `Closes #이슈번호`를 포함합니다.

5. **코드 리뷰**: 코디네이터 에이전트가 계획하고 Codex worker가 구현한 뒤,
   구현에 참여하지 않은 독립 리뷰 worker가 동료 리뷰를 수행합니다. 사람이 리뷰
   결과와 검증 근거를 확인하고 최종 머지 여부를 판정합니다.

6. **Squash Merge**: 머지는 항상 **Squash and merge** 방식으로 합니다.
   머지 커밋 제목은 `<type>: <설명> (#PR번호)` 형식으로 작성합니다.

---

## Issue 작성 규칙

`.github/ISSUE_TEMPLATE`의 Issue Form을 사용합니다.

| Form | 제목 prefix | 자동 label | 사용 상황 |
|------|------|------|------|
| `feature.yml` | `[FEAT]` | `feature` | 새 기능, 기능 개선 |
| `bug.yml` | `[BUG]` | `bug` | 오류, 장애, 기대와 다른 동작 |
| `experiment.yml` | `[EXP]` | `experiment` | 모델, 데이터, 지표, 방법론 실험 |
| `auto_research.yml` | `[AR]` | `auto-experiment` | 에이전트가 읽고 직접 수행할 실험 가설 |

`[AR]` 이슈의 `auto-experiment` label은 Auto Research 분류와 promotion guard에
사용합니다. **label 자체는 브랜치를 만들지 않습니다.** Form을 우회해 API로
발행하면 label이 자동 적용되지 않으므로 직접 부여해야 합니다.

**문서 전용 이슈** (`[DOCS]`): Issue Form이 없습니다(`[CHORE]`, `[PERF]` 등 관례로 쓰이는 다른 prefix도 마찬가지입니다). 문서·판단 기록만 산출물인 작업은 `gh issue create --title "[DOCS] ..." --label documentation`으로 만듭니다. Form이 없으므로 제목 prefix와 label을 직접 지정해야 합니다.

**이슈를 만드는 경우**: 새 기능, 버그, 실험 계획·결과 기록, 문서·설정·리팩터링처럼 추적이 필요한 작업, PR 리뷰 중 생긴 범위 밖 후속 작업. 아주 작은 오타 수정은 바로 PR로 처리할 수 있습니다.

**Form별 최소 작성 내용**:

- Feature: 목적, 작업 범위, 영향받는 컴포넌트, 완료 조건
- Bug: 현상, 재현 방법, 기대 동작, 실행 환경, 로그 또는 에러 메시지
- Experiment: 가설, 데이터셋, 모델, 피처, 평가지표, Champion 대비 결과, 결론

---

## 브랜치 네이밍 규칙

브랜치는 **해당 이슈의 `Create a branch`로 생성**합니다 (이슈-브랜치 자동 연결,
`main` 기준 분기). 생성한 브랜치명을 로컬에서 체크아웃해 작업합니다.

```bash
# 이슈에서 Create a branch로 브랜치 생성(예: feat/42-add-feature-store-schema) 후
git fetch origin
git switch feat/42-add-feature-store-schema
```

| 유형 | 패턴 | 예시 |
|------|------|------|
| 기능 개발 | `feat/이슈번호-간략한-설명` | `feat/42-add-feature-store-schema` |
| 버그 수정 | `fix/이슈번호-간략한-설명` | `fix/57-training-oom-error` |
| 실험 | `exp/이슈번호-간략한-설명` | `exp/61-lgbm-baseline` |
| 문서 | `docs/이슈번호-간략한-설명` | `docs/30-update-readme` |
| 리팩터링 | `refactor/이슈번호-간략한-설명` | `refactor/48-serving-cleanup` |
| 기타 | `chore/이슈번호-간략한-설명` | `chore/10-setup-ci` |

- 영어 소문자, 숫자, 하이픈(`-`)만 사용합니다.
- 이슈 번호를 반드시 포함합니다.
- 한 브랜치에는 하나의 주요 목적만 담습니다.
- 다음 `exp/*` 자동 생성 설명은 **현재 비활성인 옛 조직 절차**입니다. `exp/`는
  사람이 만드는 실험 브랜치와 Auto Research executor가 생성하는 실험 브랜치가
  **같은 prefix를 공유**합니다. 자동 생성 브랜치는 이슈 번호만으로 이름이 정해져
  설명 slug가 붙지 않습니다(`exp/589`). 자동 생성 브랜치는 DB에 봉인된
  `base_dev_sha`에서 만들어지므로 **삭제하거나 force-push하지 마십시오** — 옛
  ruleset이 막아 주지 않았으며 삭제하면 launcher가 자동 복구하지 않고, 다른 tip으로
  바꾸면 executor 재시도와 승격 계보 검증이 fail-closed됩니다. [브랜치 보호
  규칙](#브랜치-보호-규칙) 참조.

---

## 커밋 컨벤션

```
<type>: <설명>
```

### Type 목록

| type | 사용 상황 |
|------|-----------|
| `feat` | 새로운 기능 추가 |
| `fix` | 버그 수정 |
| `refactor` | 기능 변경 없는 코드 개선 |
| `docs` | 문서 추가·수정 |
| `chore` | 빌드, 패키지, CI 설정 등 |
| `exp` | 실험 코드 추가·수정 |
| `test` | 테스트 코드 추가·수정 |

### 예시

```
feat: Feature Store에 스키마 버전 관리 기능 추가
fix: Training 파이프라인 OOM 오류 수정
exp: LightGBM 베이스라인 실험 추가
docs: CONTRIBUTING.md 초안 작성
test: math utils 테스트 추가
```

- 설명은 한국어로 작성합니다.
- 제목은 현재형 동사로 시작합니다 (추가, 수정, 삭제, ...).
- 제목은 50자 이내로 작성합니다.
- 한 커밋은 하나의 논리적 변경만 담고, 포맷 변경과 기능 변경을 섞지 않습니다.

---

## PR 규칙

좋은 PR의 조건:

- 하나의 이슈를 해결합니다.
- 제목만 봐도 변경 목적이 드러납니다 (커밋 컨벤션과 동일한 형식 권장).
- 본문에 `Closes #이슈번호`가 있습니다.
- 변경 사항이 bullet list로 정리되어 있습니다.
- 테스트 또는 검증 명령이 적혀 있습니다.
- 아직 리뷰 준비가 안 되었으면 Draft PR로 둡니다.

PR은 작게 유지합니다. 무관한 리팩터링과 기능 변경을 섞지 말고, 리뷰 중 발견된 별도 작업은 새 이슈로 분리합니다.

**PR 생성 전 체크**:

- [ ] 로컬 테스트 통과: `uv run python -m pytest -n 4 --dist loadfile --durations=25`
- [ ] 불필요한 파일, 캐시, 시크릿(`.env` 등)이 없는지 확인
- [ ] 커밋 메시지가 컨벤션을 따르는지 확인

---

## 리뷰와 머지

현재 저장소의 동료 리뷰와 최종 판정 흐름은 다음과 같습니다.

```
코디네이터 에이전트 계획 → Codex worker 구현 → 독립 리뷰 worker 동료 리뷰
→ 발견 사항 반영·검증 → 사람이 최종 머지 판정
```

독립 리뷰 worker는 구현 worker와 분리합니다. 팀원 GitHub Approve는 현재 개인
저장소의 머지 조건이 아니며, 사람이 독립 리뷰 결과와 CI·로컬 검증 근거를 확인한
뒤 최종 판정합니다.

**리뷰어 확인 사항**:

- 이슈의 목적과 PR 변경이 일치하는가
- 변경 범위가 너무 크지 않은가
- 테스트 또는 검증 방법이 충분한가
- `Closes #이슈번호`가 있는가
- 불필요한 파일, 캐시, 시크릿이 포함되지 않았는가

**현재 비활성 — Claude 자동 리뷰·PR 이해 리포트**: 옛 조직 저장소에서는 PR이
처음 열리거나 Ready for review로 전환되면 Claude 리뷰가 자동 실행됐습니다. 현재는
`claude.yml`, `pr-report.yml`, `pr-report-archive.yml`을
`.github/workflows-disabled/`로 옮겼으므로 `/claude-review`와
`/claude-report`도 동작하지 않습니다. Claude 리뷰는 개인 계정의
`CLAUDE_CODE_OAUTH_TOKEN`을 등록하면 되살릴 수 있으며, 복구 조건 전체는
`.github/workflows-disabled/README.md`에 보존합니다.

**머지 후 자동 흐름**:

```
PR merge → Closes #issue로 이슈 close
```

> **현재 비활성 — 옛 조직 GitHub Projects 자동 전환**: 개인 저장소에는
> Project가 없어 아래 전환은 동작하지 않습니다. 복구 근거로 과거 흐름을
> 보존합니다: `PR merge → PR Status: Done → Closes #issue로 이슈 close
> → Issue Status: Done`. 당시에는 항목이 사라진 것처럼 보이면 `Done`
> 컬럼을 확인했습니다.

---

## GitHub Projects 운영

> **현재 비활성**: 아래는 `SKYAHO / Autoresearch` 조직 Project 운영
> 기록입니다. 개인 저장소에는 Project가 없어 자동 추가·상태 전환·
> 이슈 close 자동화가 동작하지 않습니다. 복구 근거로 과거 절차를 보존합니다.

옛 조직 Project는 작업 상태를 보여주는 보드로 사용했습니다.

| 상태 | 의미 | 전환 |
|------|------|------|
| `Todo` | 시작 전 | 이슈/PR 생성 시 자동 추가 |
| `In Progress` | 작업 중 | 브랜치를 따고 작업을 시작하면 직접 이동 |
| `Done` | 완료 | merge/close 시 자동 전환 |

당시 켜져 있던 자동화: open 이슈/PR 자동 추가(`is:issue,pr is:open`), 추가 시 `Todo` 설정, close/merge 시 `Done` 설정, Project에서 `Done`으로 옮기면 이슈 자동 close.

옛 조직 Project의 `Add item`으로 제목만 추가하면 Issue Form을 우회했으며, 새 작업은 현재도 Issues 화면에서 생성합니다.

---

## Label 컨벤션

Issue Form과 자동화를 단순하게 유지하기 위해 `feature`, `bug`, `experiment`를 우선 사용합니다. 보조 label: `documentation`, `good first issue`, `help wanted`, `question`. `enhancement`는 `feature`와 겹치면 `feature`를 우선합니다.

**Auto Research 분류 label — 임의로 제거하지 마십시오.**

| label | 역할 |
|---|---|
| `auto-experiment` | `[AR]` 이슈 분류와 현재 비활성인 `auto-research-promotion.yml`의 입력 guard |

이 label은 Issue Form과 API 발행 경로가 붙이며, executor Pod의 branch 생성 트리거는
아닙니다. 비활성 보관된
`.github/workflows-disabled/auto-research-promotion.yml`은 같은 label을 요구하므로,
나중에 옛 승격 절차를 복구할 가능성을 위해 label을 유지합니다. 이 일치 조건의
워크플로우 단언은 비활성 기간에 skip되고 파일을 활성 경로로 되돌리면 자동으로
다시 실행됩니다.

`auto-research`는 트리거가 **아닙니다.** Auto Research 주제를 가리키는 분류 label이며, `[AR]` 이슈에는 붙지 않습니다.

---

## CI

`.github/workflows/ci.yml`과 `.github/workflows/lint.yml`이 PR과 `main` push, 수동 실행(`workflow_dispatch`)에서 자동 실행됩니다.

- `ci.yml`: Python 3.11 / 3.12에서 `python -m pytest -n 4 --dist loadfile --durations=25`, feast·postgres 그룹 테스트, `uv lock & proxy export drift`, 이미지 빌드와 import smoke check
- `lint.yml`: `Ruff`
- `release-drafter.yml`: GitHub가 자동 제공하는 `GITHUB_TOKEN`만 사용하는 release note 초안 갱신

이 세 파일만 `.github/workflows/`에 남아 있습니다. 조직 secrets, GCP 자원,
`gh-pages`, 조직 GitHub App 또는 옛 executor를 요구하는 나머지 워크플로우는
`.github/workflows-disabled/`로 옮겼으며 복구 조건은 그 디렉터리의 `README.md`를
따릅니다.

두 파일 모두 `pull_request:` 트리거에 `branches:` 필터가 없어 base 브랜치를 가리지 않지만, `push:`는 `main` 전용입니다.

옛 조직 저장소의 `main-protection`이 요구하던 status check 6개는 `ci.yml`
5개(`pytest (Python 3.11)`, `pytest (Python 3.12)`, `pytest (feast group)`,
`uv lock & proxy export drift`, `Docker build`)와 `lint.yml` 1개(`Ruff`)였습니다.
개인 저장소에는 해당 ruleset이 없으므로 현재 required check는 아니지만, CI job 이름은
복구 근거로 유지합니다. `pytest (postgres group)`과 이미지별 `Docker build (...)`
서브잡은 실행되지만 옛 required 컨텍스트에는 포함되지 않았습니다.

---

## 브랜치 보호 규칙

> **현재 유효하지 않은 옛 조직 설정**: 아래 ruleset과 ID는
> `SKYAHO/Autoresearch`의 `main-protection`(`18360502`)과
> `dev-protection`(`20261204`) 기록입니다. 개인 저장소에는 이 ruleset이 존재하지
> 않으므로 현재 머지 조건이나 브랜치 보호 상태를 설명하지 않습니다. 나중에 보호
> 규칙을 재설계할 때 근거로 쓰기 위해 삭제하지 않습니다.

옛 조직 저장소의 보호는 저장소 파일이 아니라 **GitHub ruleset**으로 적용됐습니다.
당시 설정을 대조할 때는 아래 명령을 사용했습니다.

```bash
gh api repos/SKYAHO/Autoresearch/rulesets
gh api repos/SKYAHO/Autoresearch/rulesets/18360502   # main-protection
gh api repos/SKYAHO/Autoresearch/rulesets/20261204   # dev-protection
```

### `main` — ruleset `main-protection` (id `18360502`)

조건은 `refs/heads/main`이 아니라 `~DEFAULT_BRANCH`입니다(기본 브랜치가 `main`이므로 지금은 효과가 같습니다). read-back 대조 시 유의합니다.

> **기본 브랜치를 바꾸려면 ruleset을 먼저 고정하십시오.** 기본 브랜치를 `dev`로 바꾸는 순간 `main-protection`이 `main`을 떠나 `dev`를 따라갑니다. 그러면 `main`은 보호가 전부 풀리고, `dev`에는 `pull_request` rule이 붙어 아래에서 설명하는 실험 자동 병합이 즉시 중단됩니다. 기본 브랜치를 옮기기 전에 `main-protection`의 조건을 `refs/heads/main`으로 명시 고정해야 합니다.

- **직접 push 금지**(`pull_request`): 모든 변경은 PR을 통해서만 반영됩니다.
- **리뷰 승인 필수**: 최소 1명의 팀원 Approve가 있어야 머지할 수 있습니다 (`required_approving_review_count: 1`). approve 후 push하면 approve가 초기화되고(`dismiss_stale_reviews_on_push`), 마지막 push에 대한 승인이 필요합니다(`require_last_push_approval`).
- **CI 통과 필수**(`required_status_checks`, `strict_required_status_checks_policy: true`): 아래 6개 컨텍스트가 모두 통과해야 합니다. `Ruff`, `pytest (Python 3.11)`, `pytest (Python 3.12)`, `pytest (feast group)`, `uv lock & proxy export drift`, `Docker build`
- **force-push 금지**(`non_fast_forward`), **삭제 금지**(`deletion`).
- **머지 방식**: Squash and merge만 허용합니다 (`allowed_merge_methods: ["squash"]`).
- **우회 불가**(`bypass_actors: []`, `current_user_can_bypass: never`): classic branch protection의 "include administrators" 토글과 달리, ruleset은 bypass actor를 명시하지 않으면 **관리자도 우회하지 못합니다.**

저장소 Merge 설정: squash만 허용, merge commit·rebase merge 비활성, 머지 후 head 브랜치 자동 삭제.

### `dev` — ruleset `dev-protection` (id `20261204`)

- **삭제 금지**(`deletion`)
- **force-push 금지**(`non_fast_forward`)
- **우회 불가**(`bypass_actors: []`, `current_user_can_bypass: never`) — 팀 전원에게 예외 없이 적용됩니다.
- **PR 필수·required status check는 걸지 않습니다** — 아래 "제외한 이유" 참조.

> **`dev`가 오염되면 되감기가 아니라 revert-forward만 가능합니다.** 우회가 불가능하므로, 잘못된 candidate가 `dev`에 병합됐을 때 force-push로 되돌릴 수 없습니다. revert 커밋으로 전진 복구해야 합니다. 다행히 이 방식은 진행 중인 실험을 깨지 않습니다 — 기존 `base_dev_sha`가 여전히 `dev`의 조상으로 남아 lineage 검증이 그대로 통과합니다.

`dev`는 단순한 통합 브랜치가 아니라 **Auto Research 모든 실험의 기준선**입니다.
Agent Orchestration API의 Contents read 전용 GitHub App이 이슈 발행 전에
`heads/dev` tip을 한 번 읽어 `Experiment.base_dev_sha`에 저장합니다. launcher와
executor는 Job 시작 시 최신 `dev`나 `main`을 다시 읽지 않고, executor는 저장된 SHA에
`exp/<이슈번호>` ref를 생성합니다. 이후 계보 검증도 이 SHA를 기준점으로
삼으므로 `dev`가 force-push되거나 삭제되어 커밋이 사라지면 다음이 깨집니다.

| 깨지는 것 | 근거 | 시점 |
|---|---|---|
| 신규 이슈의 기준 SHA 봉인 | Agent Orchestration API가 `heads/dev`를 읽어 `base_dev_sha`를 DB에 먼저 저장합니다 | 이슈 발행 시 |
| exp 브랜치 생성 | executor Pod가 DB에서 전달받은 `base_dev_sha`에 ref를 만듭니다 | Job 실행 시 |
| dev 병합 | `.github/workflows-disabled/auto-research-dev-promotion.yml:367-372`가 `base: 'dev'`로 머지합니다 | 즉시 |
| main Draft PR의 lineage 검사 | `.github/workflows-disabled/auto-research-promotion.yml:188-193`이 `head: 'dev'`로 비교합니다 | 즉시 |
| 진행 중 이슈의 후보 검증 | `.github/workflows-disabled/auto-research-dev-promotion.yml:224-242`가 `base_dev_sha`를 base로 `compareCommits`를 호출합니다 | 후보 제출 시 |

#### `dev`에서 PR 필수·required status check를 의도적으로 제외한 이유

`.github/workflows-disabled/auto-research-dev-promotion.yml:367-372`의 `github.rest.repos.merge({ base: 'dev', head: selectedCandidateSha })`는 **PR을 거치지 않고 `dev` ref를 직접 갱신**합니다.

- `pull_request` rule을 걸면 이 호출 자체가 거부되어 실험 자동 병합이 즉시 멈춥니다.
- `required_status_checks`를 걸어도 마찬가지입니다. `ci.yml:6-8`과 `lint.yml:6-8`의 `push` 트리거가 `main` 전용이고, 게다가 `GITHUB_TOKEN`으로 만든 커밋은 workflow를 재귀 트리거하지 않습니다. 두 이유 각각으로 **`repos.merge`가 만든 dev 커밋에는 check run이 하나도 생성되지 않아** 영구히 통과할 수 없습니다. 후자는 `docs/archive/specs/2026-08-01-auto-research-dev-issue-branch.md:112`에 이미 기록돼 있습니다.

> "`dev`에는 지정할 컨텍스트가 없다"는 서술은 **틀립니다.** `ci.yml`과 `lint.yml` 모두 `pull_request:` 트리거에 `branches:` 필터가 없으므로 base가 `dev`인 PR에서는 위 6개 컨텍스트가 정상 생성되고, ruleset에 컨텍스트 이름을 직접 입력할 수도 있습니다. 성립하는 사실은 **"`repos.merge`로 만든 dev 커밋에는 CI가 돌지 않는다"** 입니다.

### `exp/*`, `promote/*` — ruleset 없음 (2026-08-03 판단)

두 네임스페이스에는 ruleset을 적용하지 않습니다. executor와 승격 workflow의
fail-closed 검사만 있으며, **생성 이후의 force-push·삭제 자체는 막지 못합니다.**

- `exp/*` 생성 시: executor는 ref가 없을 때만 DB에 봉인된 `base_dev_sha`로
  생성합니다. 같은 SHA의 기존 ref만 멱등 성공이고 다른 tip은 update·reset·force 없이
  `branch_ref_conflict`로 실패합니다.
- `exp/*` 삭제 후: launcher가 Kubernetes Job 존재 확인 시각을 저장한 뒤에는 TTL로 Job이
  사라져도 자동 재생성하지 않습니다. branch를 지우면 Phase 1이 자동 복구하지 않으며
  후속 승격 입력도 잃습니다.
- `exp/*` force-push: executor 재시도는 다른 tip을 거부하고,
  `.github/workflows-disabled/auto-research-dev-promotion.yml`의 후보 계보 검사도
  거부합니다. 안전하게 실패하지만 작업 결과는 소실됩니다.
- **marker 경계:** Phase 1 executor는 기존 GitHub Actions bot marker를 새로 쓰지
  않습니다. 따라서 새 marker 없는 branch는 현재 promotion workflow의 입력이 아니며,
  marker 작성 주체·서명·`base_dev_sha` 검증 재설계가 실제 실험 실행 전 다음 gate입니다.
- `promote/*`: `.github/workflows-disabled/auto-research-promotion.yml:275-288`이 이미 존재하는 promote 브랜치를 **다른 SHA로 재사용**하는 것만 거부합니다.

**`exp/*`에 보호를 걸지 않은 이유**: `exp/`는 자동화 전용 네임스페이스가 아닙니다. 위 [브랜치 네이밍 규칙](#브랜치-네이밍-규칙)이 `exp/`를 사람이 쓰는 브랜치 type으로 규정하고 있고, 사람이 만든 `exp/116-openrouter-provider-ab`와 `exp/396-views-per-day`가 실제로 원격에 존재합니다. 사람 브랜치와 자동화 브랜치가 `exp/<이슈번호>-<설명>`이라는 **같은 형식**을 쓰므로 ruleset의 ref 패턴으로 구분할 수 없고, `non_fast_forward`를 걸면 사람의 rebase·force-push가, `deletion`을 걸면 작업 후 브랜치 정리가 함께 막힙니다. 보호를 걸려면 **자동화 전용 네임스페이스 분리가 선행**되어야 하며, 이는 브랜치명 생성 규칙과 marker 신뢰 계약을 바꾸는 동작 변경이라 별도 이슈에서 다룹니다.

---

## 문제 해결

**PR이 merge되지 않을 때**: Draft 상태인지, 독립 리뷰 worker의 발견 사항과
conversation이 해결됐는지, 충돌이나 CI 실패가 있는지 확인합니다. 최종 머지 여부는
사람이 판정합니다.

**현재 비활성 — 옛 조직 Project에 항목이 안 보일 때**: `Done` 컬럼과 view filter를 확인했습니다. 이미 closed/merged된 항목은 자동 추가 필터(`is:issue,pr is:open`)에 걸리지 않을 수 있었습니다.

**이슈가 자동으로 닫히지 않을 때**: PR 본문에 `Closes #이슈번호`가 있는지, PR이 `main`으로 merge되었는지 확인합니다.

---

## 참고 링크

- Repository: https://github.com/bbungjun/Autoresearch
- Project Board (**현재 비활성인 옛 조직 보드**): https://github.com/orgs/SKYAHO/projects/3/views/2
- Issue Forms: `.github/ISSUE_TEMPLATE/*.yml`
- PR template: `.github/PULL_REQUEST_TEMPLATE.md`
