# 저장소 구조 재배치 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 최상위 폴더 이름만 보고 내용을 예상할 수 있도록 파이프라인 단계 축으로 재배치하되, 동작은 한 줄도 바꾸지 않는다.

**Architecture:** `src/`를 없애고 그 내용을 `autoresearch/` 아래 단계별 패키지로 흡수한다. 배포되는 서비스(서빙 API, 에이전트 플랫폼, proxy)는 `applications/` 층으로 분리한다. 순환 의존과 `feature_repo/`는 손대지 않는다.

**Tech Stack:** Python 3.11/3.12, uv, pytest, ruff, typer, Docker

**Spec:** [`docs/specs/2026-08-13-repository-structure-redesign.md`](../specs/2026-08-13-repository-structure-redesign.md)

**Issue:** #754

## Global Constraints

- **동작 변경 금지.** 이 계획의 모든 작업은 이동·리네임·임포트 치환뿐이다. 함수 시그니처, 로직, 로그 문구, 파일 포맷을 바꾸지 않는다. 리팩터링 충동이 들면 별도 이슈로 분리한다.
- **지연 import 위치 유지.** `autoresearch/jobs/action_log.py`의 함수 내부 지연 import 4곳(`from src.pipeline.rerank_api`, `build_training_dataset` ×2, `model_exposure_provider`)은 경로만 갱신하고 **모듈 최상단으로 올리지 않는다.** 이 계획서는 처음에 "올리면 순환 import로 실패한다"고 적었으나 Task 1에서 실측한 결과 **틀렸다** — 넷을 모두 올려도 import와 테스트가 통과한다(spec 1절 정정 참조). 올리지 않는 실제 이유는 무거운 선택적 의존(`google.cloud.bigquery`, `requests`)을 인자 검증 경로에서 떼어 놓기 위해서이며, 어느 쪽이든 이번 재배치의 범위가 아니다.
- **`feature_repo/` 이동 금지.** 최상위에 그대로 둔다. `feature_store.yaml`, `Dockerfile.feast`, `feast-apply.yml`의 `feature_repo` 경로를 건드리지 않는다.
- **`sys.path` 블록 유지 — 단, 깊이 보존은 `src/*` 계열에만 해당한다.** `pyproject.toml`의 `[tool.uv] package = false`는 그대로다. `src/cli.py`(2→2), `src/pipeline/*`(3→3), `src/serving/*`(3→3), `src/features|models|tracking|utils/*`(3→3)는 깊이가 보존되므로 `PROJECT_ROOT = os.path.dirname(...)` 줄을 **수정하지 않는다.** 반면 아래 네 그룹은 깊이가 바뀌므로 **개별 확인이 필요하다.**

  | 이동 | 깊이 |
  | --- | --- |
  | `autoresearch/experiments/*.py` → `autoresearch/model_evaluation/experiments/` | 3 → 4 |
  | `autoresearch/loadtest/*.py` → `applications/reranking_api/loadtest/` | 3 → 4 |
  | `agent_orchestration/<공유 6개>.py` → `applications/experiment_platform/shared/` | 2 → 4 |
  | `proxy/*.py` → `applications/youtube_api_proxy/` | 2 → 3 |

  각 Task에서 이동 직후 확인한다: `grep -rn "os.path.dirname\|Path(__file__)\|parents\[" <새 경로>`
- **슬래시 표기 경로를 잊지 않는다.** 임포트 치환 스크립트는 점 표기(`src.pipeline.train`)만 다룬다. `"src/features/model_contract.py"`, `"agent_orchestration"` 같은 **문자열 리터럴**은 별도 단계에서 손으로 고친다(spec 6-3-1). 확인 grep도 점 표기로 좁히지 않는다.
- **`docs/archive/` 갱신 금지.** `docs/README.md` 규칙상 아카이브 문서는 역사적 기록이다.
- **파일 이동과 임포트 치환은 별도 커밋.** git rename 감지를 살려야 리뷰가 가능하다.
- 커밋 메시지는 `<type>: 한국어 설명` + 본문 + `Refs #754` + `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## 착수 전제 (Task 0 이전에 확인)

- [ ] Model Training / Feast Features 코드 경계와 기존 계약 재확인 — 선행 plan(#149)의 기술적 경계를 보존한다
- [ ] `#535`(사람 PR, `degradation_eval.py`) 처리 순서 확정 — 먼저 머지하거나, 재배치 후 rebase한다

**실험 발행은 중단하지 않는다.** spec 9-1절에서 실측한 대로, 열린 `[AR]` PR
6건은 재배치를 막지 않는다:

- git rename 감지가 rebase·merge 양방향에서 충돌 없이 처리한다 (1,147행 규모로 실측)
- exp diff(`train.py` 884–933행)와 단계 1이 고치는 임포트 블록(55–118행)이 겹치지 않는다
- 진행 중 실험은 봉인된 `base_dev_sha` 트리를 쓰므로 `main`/`dev` 변경에 영향받지 않는다
- 승격 계보 검증은 `candidate_sha must be an ancestor of dev` 하나뿐이라 경로와 무관하다

대신 Task 1 끝에 `src/` 재생성을 막는 CI 가드를 넣는다(Task 1 Step 15-1).

## File Structure

| 새 위치 | 책임 |
| --- | --- |
| `autoresearch/cli.py` | 학습·평가·승격 typer CLI 진입점 |
| `autoresearch/jobs/` | Airflow가 소비하는 공개 batch CLI (변경 없음) |
| `autoresearch/data_collection/` | YouTube 트렌딩 수집 |
| `autoresearch/virtual_user_generation/` | 가상 유저 생성 + 파이프라인 어댑터 |
| `autoresearch/action_log_generation/` | action log 생성·shard·품질 계약 |
| `autoresearch/feature_engineering/` | 피처 조립·임베딩·Feast 조회 |
| `autoresearch/model_training/` | 모델 정의, 학습, 학습 데이터셋, provenance, 스냅샷, 아티팩트 I/O |
| `autoresearch/model_evaluation/` | 평가, 열화 측정, paired 비교, seed sweep, 승격 근거 |
| `autoresearch/recommendation/` | 일일 추천, 정책 라운드 시뮬, 노출 provider, 리랭킹 클라이언트 |
| `autoresearch/model_registry/` | MLflow tracking·registry·승격 |
| `autoresearch/reporting/` | HTML 리포트, 실험 결과 리포트 전송 |
| `applications/reranking_api/` | FastAPI 리랭킹 서빙 앱 + k6 부하 테스트 |
| `applications/experiment_platform/` | 실험 에이전트 (api/workbench/runner/launcher/executor/shared) |
| `applications/youtube_api_proxy/` | Cloud Run dumb forwarder |
| `deployment/` | 배포 산출물 + `Dockerfile.*` |

---

### Task 0: 루트 청소 — 완료 (PR #755, 커밋 `1470c12`)

착수 전제와 무관하게 먼저 수행했다. 아래는 **실제로 반영된 내용**이다 — 계획 단계에서 예상했던 것보다 범위가 작았다.

**Files:**
- Modified: `.gitignore` (+15)
- Deleted (로컬 전용): `dags/` — 안에 stale `__pycache__` 4개뿐이라 추적 파일이 없었고, 따라서 PR diff에는 나타나지 않는다

- [x] **Step 1: 기존 `.gitignore`와 대조**

계획을 처음 쓸 때 "미추적 = gitignore 누락"이라고 넘겨짚었으나 틀렸다. `artifacts/`, `asset/`, `mlruns/`, `output/`, `Nemotron-Personas-Korea/`, `.codex-tmp/`, `.omo/`는 **이미 있었다.** 실제로 빠진 것만 추가한다.

```bash
for d in artifacts asset mlruns output Nemotron-Personas-Korea .codex-tmp .omo; do
  printf "%-26s %s\n" "$d" "$(git check-ignore -q "$d" && echo IGNORED || echo not-ignored)"
done
```

- [x] **Step 2: 실제로 추가한 항목**

```gitignore
# Airflow DAG는 인접 저장소 SKYAHO/Autoresearch-airflow 소유다.
# 최상위 dags/ 는 #142 레거시 제거 후 되살아난 로컬 잔재이므로 추적하지 않는다 (#754)
/dags/

# 에이전트 도구 세션 스크래치 (#754)
.gjc/
.playwright-mcp/
.superpowers/

# 워크벤치 확인용 스크린샷 등 최상위 이미지 (#754)
/*.png

# 워크스페이스 전용 로컬 메모 — 파일 스스로 "Git에 커밋하지 않는다"고 선언한다 (#754)
/agent.md
```

`/data/`는 **넣지 않았다.** 기존 `data/generated/`·`data/raw/*`보다 넓어 의도치 않게 감출 위험이 있고, `git status`가 이미 아무것도 보고하지 않는다.

- [x] **Step 3: `agent.md`는 삭제하지 않고 ignore**

파일 머리말이 *"이 파일은 이 워크스페이스에서만 쓰는 로컬 전용 메모다. Git에 커밋하지 않는다"*라고 스스로 선언한다. 의도된 로컬 파일이므로 삭제가 아니라 ignore가 맞다. 최상위 PNG 3개도 같은 이유로 지우지 않고 `/*.png`로 덮었다.

- [x] **Step 4: `dags/` 삭제와 계약 확인**

```bash
find dags -type f    # __pycache__/*.pyc 4개뿐임을 확인한 뒤
rm -rf dags
uv run python -m pytest tests/test_release_workflow.py -q
```

`tests/test_release_workflow.py:221`이 `assert not list((REPOSITORY_ROOT / "dags").rglob("*.py"))`로 계약을 고정한다. 디렉토리가 없어도 `rglob`이 빈 결과를 내므로 통과한다 — 16 passed로 확인했다.

- [x] **Step 5: 검증**

`git status` 미추적이 12건 → 7건. 남은 7건은 전부 다른 브랜치(#753 baseline 캐싱)의 진행 중 작업이라 손대지 않았다.

- [x] **Step 6: 커밋** — `1470c12 chore: 최상위 로컬 잔재를 gitignore로 걷어낸다`

---

### Task 1: `src/` → `autoresearch/` 이동 — 완료 (커밋 `a524cf0`·`a05b421`)

가장 큰 Task다. 이동 커밋과 치환 커밋을 분리한다.

**계획과 달랐던 것.** 아래 다섯 가지는 이 계획서가 예상하지 못했고, 전부 Task 1 시점에
이미 깨지는 것이라 뒤 Task로 미룰 수 없었다.

| 발견 | 왜 미룰 수 없나 |
| --- | --- |
| `train.py:315`·`evaluate.py:277`의 `get_project_root()`가 최상위 `src` 디렉터리 존재 여부를 **센티널**로 쓴다 | Task 2에서 `src/`가 사라지면 `RuntimeError("프로젝트 루트를 찾을 수 없습니다")`. 센티널을 `autoresearch`로 바꿨다 |
| `ci.yml`의 `train` 이미지 paths 필터에 `autoresearch/**`가 없다 | 학습 코드가 옮겨간 순간부터 train 이미지 빌드가 **조용히 스킵**된다. 5-2절이 경고한 바로 그 실패 방식이다 |
| `ci.yml`·`release.yml`의 실행 명령과 이슈 템플릿 `build-features` 안내 | Task 4로 미루면 그 사이 CI와 새 실험이 없는 모듈을 부른다 |
| `feature_repo/feature_definitions.py:35`의 ODFV 헬퍼 import | 아래 별항 |
| `scripts/verify_registry_portability.py:59`의 `ALLOWED_FIRST_PARTY`와 테스트 2개의 `_PROJECT_TOP_LEVEL` | 레지스트리 이식성 게이트가 `src/` 밑만 1st-party로 인정한다. 안 고치면 apply 후 검증이 새 경로를 외부 참조로 오판한다 |

**Feast 레지스트리 — 이 계획서의 가장 큰 사각지대였다.**

`feature_repo/feature_definitions.py`는 `feature_repo/` 이동 제외 대상이라 안전하다고
넘겼으나, 그 파일이 `src.features.feature_builder`를 import한다. 배포된 레지스트리의
ODFV UDF는 그 이름을 **dill by-reference**로 물고 있다.

문제는 import 줄만 고쳐서는 레지스트리가 따라오지 않는다는 것이다. feast의 변경 감지는
`PandasTransformation.__eq__ = (udf_string, 바이트코드)` 둘뿐이라 헬퍼의 소속 모듈만 바뀐
변경을 못 본다 — `feast apply`가 "변경 없음"으로 판정해 옛 dill body를 그대로 둔다. 그
상태로 `src/`가 사라지면 학습·서빙이 레지스트리를 읽는 순간 `ModuleNotFoundError`로
죽는다. 이 함정은 **파일 자신이 216-225행 주석으로 문서화하고 있었다**(#409가 같은 것을
겪었다).

그래서 UDF 함수 본문 주석까지 고쳐 `udf_string`을 바꿨다 — #409가 쓴 것과 같은 수단이다.

**남는 위험 — 순서 문제가 아니라 교착이었다(2026-08-14 정정).** 아래 "순서를 맞추면
된다"는 서술은 **틀렸다.** `feast apply`는 새 정의를 쓰기 전에 기존 레지스트리를 읽어
diff를 내는데, 그 레지스트리의 dill body가 옛 이름을 물고 있어 **읽는 것 자체가**
`ModuleNotFoundError`로 실패한다. 재배치 커밋이 main에 들어간 뒤 feast-apply 워크플로가
3연속 실패한 것이 그 증거다. 어느 순서로도 풀리지 않으며, apply 한 번을 통과시킬
전환용 shim이 필요하다 — 상세는 `fix/754-feast-registry-cutover`.

원래 서술: apply 후 레지스트리는 새 이름을 참조하므로,
재배치 **이전** 이미지로 도는 파드는 그때부터 레지스트리를 읽지 못한다. `feast apply`와
이미지 롤아웃 순서를 맞춰야 한다. Task 6(인접 저장소) 시점에 함께 확인한다.

**Step 15-1(`src/` 재생성 방지 가드)은 Task 2로 옮겼다.** Task 1 종료 시점에도
`src/serving/`이 남아 있어 `[ -d src ]` 가드가 즉시 실패한다.

**기준선.** `2833 tests collected`, `2814 passed / 2 failed / 25 skipped`. 실패 2건은
`test_spawn_failure_logs_why_the_process_could_not_start`(measurement·training)로, WSL2에서
없는 명령이 `FileNotFoundError` 대신 `PermissionError`를 내는 로컬 환경 특성이다 — CI는
통과한다. Task 1 종료 시점 `2815 passed / 2 failed / 25 skipped`(계약 테스트 parametrize로
+1). Docker는 이 개발 환경에서 사용 불가라 이미지 빌드는 CI에 맡겼다.

**Files:**
- Move: `src/` 전체 50개 → `autoresearch/` 아래 (단 `src/serving/`은 Task 2에서 처리하므로 **여기서는 건드리지 않는다**)
- Create: `autoresearch/{feature_engineering,model_training,model_evaluation,recommendation,reporting}/__init__.py`
- Modify: 임포트를 가진 전 파일

**Interfaces:**
- Produces: 아래 모듈 경로. Task 2 이후의 모든 Task가 이 경로에 의존한다.

```
autoresearch.cli
autoresearch.feature_engineering.{assembly,category_reference,embeddings,feast_retrieval,feature_builder,model_contract}
autoresearch.model_training.{base,calibration,downsampling,lgbm_model,model_utils,train,build_training_dataset,training_provenance,training_snapshot_store}
autoresearch.model_evaluation.{evaluate,degradation_eval,experiment_evaluation,training_comparison,paired_experiment,seed_sweep,promotion_evidence}
autoresearch.model_evaluation.experiments.{context,promotion_gate}
autoresearch.recommendation.{daily_recommendations,simulate_policy_round,model_exposure_provider,policy_selector,rerank_api}
autoresearch.virtual_user_generation.adapter
autoresearch.reporting.{report_html,experiment_result_report}
autoresearch.model_registry.{client,logger,model_package,namespace,promote,promotion_result,registry}
autoresearch.data_collection.{backfill,client,fetch,load,schema,transform}
autoresearch.action_log_generation.{calibration,candidate,daily,llm_generator,observability,pipeline,schema,video_source}
```

- [x] **Step 1: 기준선 기록**

치환 후 비교할 값을 먼저 남긴다.

```bash
uv run python -m pytest --collect-only -q 2>/dev/null | tail -1 | tee /tmp/baseline-collect.txt
uv run python -m pytest -q 2>&1 | tail -3 | tee /tmp/baseline-result.txt
```

기대: 전부 통과. 실패가 있으면 **여기서 멈춘다** — 재배치 전에 이미 깨진 것이므로 원인을 먼저 분리한다.

- [x] **Step 2: 새 패키지 디렉토리와 `__init__.py` 생성**

```bash
cd /home/yjlee/Autoresearch
for p in feature_engineering model_training model_evaluation recommendation reporting; do
  mkdir -p "autoresearch/$p"
  touch "autoresearch/$p/__init__.py"
done
mkdir -p autoresearch/model_evaluation/experiments
```

`src/models/__init__.py`와 `src/utils/__init__.py`는 **둘 다 빈 파일**이므로 옮기지 않고 지운다(Step 3). 여기서 만든 빈 `__init__.py`가 그 자리를 대신한다. 반면 `src/tracking/__init__.py`는 14줄짜리 re-export가 있으므로 디렉토리째 옮겨 내용을 보존한다.

- [x] **Step 3: `git mv`로 파일 이동**

```bash
cd /home/yjlee/Autoresearch

# 진입점
git mv src/cli.py autoresearch/cli.py

# feature_engineering ← src/features
for f in assembly category_reference embeddings feast_retrieval feature_builder model_contract; do
  git mv "src/features/$f.py" "autoresearch/feature_engineering/$f.py"
done

# model_training ← src/models + src/utils + 학습 4파일 + config.yaml
for f in base calibration downsampling lgbm_model; do
  git mv "src/models/$f.py" "autoresearch/model_training/$f.py"
done
git rm -q src/models/__init__.py          # 빈 파일 — Step 2의 새 __init__.py 로 대체
git mv src/utils/model_utils.py autoresearch/model_training/model_utils.py
git rm -q src/utils/__init__.py           # 빈 파일
for f in train build_training_dataset training_provenance training_snapshot_store; do
  git mv "src/pipeline/$f.py" "autoresearch/model_training/$f.py"
done
git mv src/pipeline/config.yaml autoresearch/model_training/config.yaml

# model_evaluation
for f in evaluate degradation_eval experiment_evaluation training_comparison \
         paired_experiment seed_sweep promotion_evidence; do
  git mv "src/pipeline/$f.py" "autoresearch/model_evaluation/$f.py"
done
git mv autoresearch/experiments/context.py       autoresearch/model_evaluation/experiments/context.py
git mv autoresearch/experiments/promotion_gate.py autoresearch/model_evaluation/experiments/promotion_gate.py
git mv autoresearch/experiments/__init__.py      autoresearch/model_evaluation/experiments/__init__.py

# recommendation
for f in daily_recommendations simulate_policy_round model_exposure_provider policy_selector rerank_api; do
  git mv "src/pipeline/$f.py" "autoresearch/recommendation/$f.py"
done

# virtual_user_generation ← autoresearch/virtual_users + adapter
git mv autoresearch/virtual_users autoresearch/virtual_user_generation
git mv src/pipeline/virtual_user_adapter.py autoresearch/virtual_user_generation/adapter.py

# reporting
git mv src/pipeline/report_html.py             autoresearch/reporting/report_html.py
git mv src/pipeline/experiment_result_report.py autoresearch/reporting/experiment_result_report.py

# model_registry ← src/tracking
git mv src/tracking autoresearch/model_registry

# 단순 리네임
git mv autoresearch/youtube_collection autoresearch/data_collection
git mv autoresearch/action_logs        autoresearch/action_log_generation
```

`src/`에는 `serving/`만 남아야 한다. 확인:

```bash
ls src/     # serving  (그리고 __pycache__)
```

- [x] **Step 4: 이동만 커밋 (rename 감지 확보)**

이 시점에는 테스트가 **깨져 있는 것이 정상**이다. 임포트가 아직 옛 경로를 가리킨다.

```bash
git add -A
git status --short | head -20      # R (rename) 로 표시되는지 확인
git commit -m "$(cat <<'EOF'
refactor: src 트리를 autoresearch 단계별 패키지로 옮긴다

파일 이동만 수행한다. 임포트 치환은 다음 커밋에서 한다 — 두 변경을
한 커밋에 섞으면 git이 rename을 감지하지 못해 리뷰가 불가능해진다.
이 커밋 시점에는 테스트가 실패하는 것이 정상이다.

Refs #754

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

- [x] **Step 5: 갈라지는 임포트 1곳을 손으로 고친다**

`autoresearch/cli.py:47`의 다중행 임포트는 6개 모듈이 2개 패키지로 갈라지므로 스크립트로 처리할 수 없다. 아래로 교체한다.

기존:

```python
from src.pipeline import (  # noqa: E402
    build_training_dataset,
    degradation_eval,
    evaluate,
    paired_experiment,
    train,
    training_comparison,
)
```

교체 후:

```python
from autoresearch.model_training import (  # noqa: E402
    build_training_dataset,
    train,
)
from autoresearch.model_evaluation import (  # noqa: E402
    degradation_eval,
    evaluate,
    paired_experiment,
    training_comparison,
)
```

- [x] **Step 6: 나머지 임포트를 스크립트로 치환**

아래를 스크래치패드에 저장해 실행한다. **커밋하지 않는 일회성 도구다.**

```python
# /tmp/rewrite_imports.py
import pathlib
import re

# 순서가 중요하다. 더 긴 경로를 먼저 치환해야 접두사 충돌이 없다.
DOTTED = [
    # src/pipeline → 4개 패키지로 분할
    ("src.pipeline.build_training_dataset",   "autoresearch.model_training.build_training_dataset"),
    ("src.pipeline.training_snapshot_store",  "autoresearch.model_training.training_snapshot_store"),
    ("src.pipeline.training_provenance",      "autoresearch.model_training.training_provenance"),
    ("src.pipeline.train",                    "autoresearch.model_training.train"),
    ("src.pipeline.experiment_result_report", "autoresearch.reporting.experiment_result_report"),
    ("src.pipeline.experiment_evaluation",    "autoresearch.model_evaluation.experiment_evaluation"),
    ("src.pipeline.training_comparison",      "autoresearch.model_evaluation.training_comparison"),
    ("src.pipeline.promotion_evidence",       "autoresearch.model_evaluation.promotion_evidence"),
    ("src.pipeline.paired_experiment",        "autoresearch.model_evaluation.paired_experiment"),
    ("src.pipeline.degradation_eval",         "autoresearch.model_evaluation.degradation_eval"),
    ("src.pipeline.seed_sweep",               "autoresearch.model_evaluation.seed_sweep"),
    ("src.pipeline.evaluate",                 "autoresearch.model_evaluation.evaluate"),
    ("src.pipeline.model_exposure_provider",  "autoresearch.recommendation.model_exposure_provider"),
    ("src.pipeline.simulate_policy_round",    "autoresearch.recommendation.simulate_policy_round"),
    ("src.pipeline.daily_recommendations",    "autoresearch.recommendation.daily_recommendations"),
    ("src.pipeline.policy_selector",          "autoresearch.recommendation.policy_selector"),
    ("src.pipeline.rerank_api",               "autoresearch.recommendation.rerank_api"),
    ("src.pipeline.virtual_user_adapter",     "autoresearch.virtual_user_generation.adapter"),
    ("src.pipeline.report_html",              "autoresearch.reporting.report_html"),
    # 단순 리네임 — model_utils 를 utils 보다 먼저
    ("src.utils.model_utils",                 "autoresearch.model_training.model_utils"),
    ("src.utils",                             "autoresearch.model_training"),
    ("src.features",                          "autoresearch.feature_engineering"),
    ("src.models",                            "autoresearch.model_training"),
    ("src.tracking",                          "autoresearch.model_registry"),
    ("src.cli",                               "autoresearch.cli"),
    ("autoresearch.youtube_collection",       "autoresearch.data_collection"),
    ("autoresearch.action_logs",              "autoresearch.action_log_generation"),
    ("autoresearch.virtual_users",            "autoresearch.virtual_user_generation"),
    ("autoresearch.experiments",              "autoresearch.model_evaluation.experiments"),
]

# `from <pkg> import <이름>` 형태. 값은 (심볼 → 새 패키지).
FROM_PKG = {
    "src.pipeline": {
        "build_training_dataset":  "autoresearch.model_training",
        "train":                   "autoresearch.model_training",
        "training_snapshot_store": "autoresearch.model_training",
        "evaluate":                "autoresearch.model_evaluation",
        "degradation_eval":        "autoresearch.model_evaluation",
        "paired_experiment":       "autoresearch.model_evaluation",
        "training_comparison":     "autoresearch.model_evaluation",
        "experiment_evaluation":   "autoresearch.model_evaluation",
        "simulate_policy_round":   "autoresearch.recommendation",
    },
    "src.features": {"*": "autoresearch.feature_engineering"},
    "src.tracking": {"*": "autoresearch.model_registry"},
    "src":          {"cli": "autoresearch"},
}

ROOTS = ["autoresearch", "applications", "src", "tests", "scripts", "examples",
         "tools", "feature_repo", "agent_orchestration", "proxy"]


def rewrite_from_pkg(text: str) -> str:
    def repl(m: re.Match) -> str:
        pkg, names = m.group(1), m.group(2)
        table = FROM_PKG.get(pkg)
        if table is None:
            return m.group(0)
        symbols = [n.strip() for n in names.split(",")]
        targets = {table.get(s.split(" as ")[0], table.get("*")) for s in symbols}
        if len(targets) != 1 or None in targets:
            raise SystemExit(
                f"수동 처리 필요 — 한 줄이 여러 패키지로 갈라진다: {m.group(0)!r}"
            )
        return f"from {targets.pop()} import {names}"

    return re.sub(
        r"from (src(?:\.[a-z_]+)*) import ([a-zA-Z_][\w, ]*)", repl, text
    )


changed = 0
for root in ROOTS:
    for path in pathlib.Path(root).rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        original = path.read_text(encoding="utf-8")
        text = rewrite_from_pkg(original)
        for old, new in DOTTED:
            text = re.sub(rf"(?<![\w.]){re.escape(old)}(?![\w])", new, text)
        if text != original:
            path.write_text(text, encoding="utf-8")
            changed += 1
print(f"{changed} files rewritten")
```

```bash
uv run python /tmp/rewrite_imports.py
```

스크립트가 "수동 처리 필요"로 멈추면 그 줄을 손으로 고치고 다시 실행한다. Step 5를 먼저 했다면 멈추지 않아야 한다.

- [x] **Step 6-1: 실험 에이전트 executor의 슬래시 표기 경로를 고친다 (최우선)**

치환 스크립트는 점 표기만 다루므로 아래는 잡히지 않는다. **빠뜨리면 정책 게이트가 조용히 사라진다** — spec 6-3-1절.

**(a) `prod_model_contract` 게이트 — `agent_orchestration/executor/verifier.py:357`**

```python
# 기존
    if path == "src/features/model_contract.py":
        return "prod_model_contract" in policy.allowed_scope
    if path.startswith("src/"):
        return True
    if path.startswith(_BASE_ALLOWED_PREFIXES):
        return True

# 교체 — 전환 기간 동안 두 경로를 모두 게이트한다
    if path in _MODEL_CONTRACT_PATHS:
        return "prod_model_contract" in policy.allowed_scope
    if path.startswith("src/"):
        # 전환 기간 허용. 봉인된 옛 base_dev_sha 트리에서 만들어진 실험은
        # 워크스페이스에 여전히 src/ 를 가진다. 아래 "전환 기간" 절 참조.
        return True
    if path.startswith(_BASE_ALLOWED_PREFIXES):
        return True
```

```python
_MODEL_CONTRACT_PATHS: Final = frozenset({
    "src/features/model_contract.py",                          # 봉인된 옛 트리
    "autoresearch/feature_engineering/model_contract.py",      # 재배치 후
})
```

정확 매칭이 `src/`·`_BASE_ALLOWED_PREFIXES` 접두사 검사보다 **위에** 있어야 한다. 순서가 바뀌면 게이트가 무력해진다.

**왜 `src/` 허용을 남기는가 — executor 이미지와 봉인된 트리의 버전 어긋남**

`_validate_path_files`는 워크스페이스의 diff 경로를 `_path_is_allowed`에 넣는다(`verifier.py:464-470`). 그런데 워크스페이스는 DB에 봉인된 `base_dev_sha`에서 만든 `exp/*` 브랜치이고, executor 이미지는 릴리스된 digest다. **둘의 버전이 다를 수 있다.**

재배치 후 빌드된 executor 이미지가 재배치 **전** 봉인 SHA 실험을 검증하면:

```
워크스페이스 트리: src/pipeline/train.py 를 수정
새 verifier:      src/ 접두사 허용이 없음
결과:             CandidateVerificationError("forbidden_path") → candidate 거부
```

`src/` 허용을 남기면 옛 트리 실험이 계속 통과하고, `_MODEL_CONTRACT_PATHS`가 두 경로를 모두 잡으므로 **어느 트리에서도 게이트가 살아 있다.**

**전환 기간 종료 조건** — 봉인 SHA가 전부 재배치 이후인 실험만 남으면(즉 진행 중 실험이 모두 종료·머지된 뒤) `src/` 허용 줄과 `_MODEL_CONTRACT_PATHS`의 옛 경로를 제거한다. Task 1 Step 15-1의 `src/` 부활 방지 CI 가드는 저장소 트리를 보는 것이고 이쪽은 워크스페이스를 보는 것이라, 둘은 서로 다른 층이다. 정리 작업은 별도 이슈로 남긴다.

**(b) 블로킹 ruff 인자 — `verifier.py:582`**

```python
# 기존: "ruff", "check", "agent_orchestration", "autoresearch", "tests", "tools",
# 교체: "ruff", "check", "autoresearch", "tests", "tools",
```

Task 1 시점에는 `agent_orchestration/`이 아직 있으므로 인자를 지우면 그 디렉토리를 lint하지 않게 된다. **Task 2에서 `applications`를 추가**해 `"autoresearch", "applications", "tests", "tools"`로 만든다. Task 1에서는 그대로 두고 Task 2에서 한 번에 바꿔도 된다 — 어느 쪽이든 Task 2 종료 시점에 `applications`가 들어가 있어야 한다.

**(c) 피처 정의 변경 감지 — `executor/training.py:70`**

```python
# 기존
_FEATURE_DEFINITION_PATHS: Final = (
    "feature_repo",
    "src/pipeline/build_training_dataset.py",
)
# 교체
_FEATURE_DEFINITION_PATHS: Final = (
    "feature_repo",
    "autoresearch/model_training/build_training_dataset.py",
)
```

같은 파일 74행의 `_SEED_PROBE` 문자열 안 `from src.pipeline.experiment_evaluation import POLICY_SEEDS`는 점 표기라 스크립트가 처리한다. 처리됐는지 확인한다.

**(d) Codex 안내문 — `executor/prompt.py`**

| 행 | 기존 | 교체 |
| --- | --- | --- |
| 85 | `"src/** (src/features/model_contract.py 제외)"` | `"autoresearch/** (autoresearch/feature_engineering/model_contract.py 제외)"` |
| 91 | `"prod_model_contract": "src/features/model_contract.py"` | `"prod_model_contract": "autoresearch/feature_engineering/model_contract.py"` |
| 105 | `"uv run --no-sync ruff check agent_orchestration autoresearch tests tools"` | Task 2 종료 시점 기준으로 `applications`를 반영 |
| 362 | 채점 경로 `src/pipeline/evaluate.py` | `autoresearch/model_evaluation/evaluate.py` |

**(e) 게이트 계약 테스트를 새 경로로 고친다 — `tests/test_experiment_candidate_verifier.py:151-162`**

이 테스트는 tmp 저장소에 경로를 **직접 만들어** 검증하므로, 고치지 않으면 실제 게이트가 죽어도 계속 통과한다.

```python
    target = repository / "autoresearch" / "feature_engineering" / "model_contract.py"
    ...
    assert _verify(repository, base_sha, allowed_scope=("prod_model_contract",)) == (
        "autoresearch/feature_engineering/model_contract.py",
    )
```

**옛 경로 케이스를 지우지 말고 둘 다 남긴다.** 전환 기간 동안 봉인된 옛 트리 실험도 이 게이트를 통과해야 하므로, `src/features/model_contract.py`와 새 경로 양쪽에 대해 각각 검증한다. `pytest.mark.parametrize`로 묶는 것이 자연스럽다.

scope **없이 거부되는** 음성 케이스가 있는지 확인하고, 없으면 두 경로 모두에 추가한다 — 게이트의 존재 이유가 그쪽이고, 이번 회귀가 조용했던 이유도 음성 케이스 부재다.

```bash
uv run python -m pytest tests/test_experiment_candidate_verifier.py -v 2>&1 | tail -5
```

- [x] **Step 7: 잔여 참조 확인 — 점 표기와 슬래시 표기 둘 다**

```bash
# 점 표기 (임포트)
grep -rn "from src\.\|import src\.\|from src import" --include=*.py . | grep -v '\.venv\|\.worktrees'
# 슬래시 표기 (문자열 리터럴) — 이쪽을 빠뜨려서 executor 4파일을 놓쳤다
grep -rn '"src/\|'"'"'src/\|src/pipeline\|src/features\|src/models\|src/tracking\|src/utils' \
  --include=*.py --include=*.yml --include=*.yaml --include=Dockerfile* . \
  | grep -v '\.venv\|\.worktrees\|docs/archive'
```

기대: `src/serving/` 관련 참조만 남는다 (Task 2에서 처리). 그 외 0건.

- [x] **Step 8: 하드코딩 config 경로 수정**

`autoresearch/model_training/train.py:590`, `autoresearch/model_evaluation/evaluate.py:311`:

```python
# 기존
config_path = os.path.join(project_root, "src", "pipeline", "config.yaml")
# 교체
config_path = os.path.join(project_root, "autoresearch", "model_training", "config.yaml")
```

`autoresearch/cli.py` 292·404·455·1219행의 typer help 문자열:

```python
# 기존
help="config.yaml 경로 (기본: src/pipeline/config.yaml)"
# 교체
help="config.yaml 경로 (기본: autoresearch/model_training/config.yaml)"
```

`src/serving/model_loader.py:40` 주석은 Task 2에서 처리한다.

```bash
grep -rn '"src"\|src/pipeline/config.yaml' --include=*.py autoresearch
```

기대: 0건.

- [x] **Step 9: docstring 경로 참조 수정**

이동한 모듈의 최상단 docstring이 옛 경로를 서술한다. CLAUDE.md 규칙상 기능을 옮기는 같은 커밋에서 갱신한다.

```bash
grep -rn "src/pipeline\|src/features\|src/models\|src/tracking\|src/utils\|src\.pipeline\|src\.features" --include=*.py autoresearch
```

나오는 곳을 새 경로로 고친다. 특히 `autoresearch/feature_engineering/feature_builder.py:9-15`의 feast ODFV 계약 서술은 `feature_repo` 부분을 **그대로 두고** `src.features` 표기만 바꾼다.

CLAUDE.md 규칙상 모듈 docstring은 "전체 파이프라인 기준으로 어느 구간을 담당하는지"를 서술한다. `[비책임]` 절이 옛 경로로 인접 모듈을 가리키는 곳도 함께 고친다. 예: `autoresearch/loadtest/__init__.py`의 *"HTTP 리랭킹 요청은 src/serving/이 담당한다"* → Task 2에서 `applications/reranking_api/`로 갱신.

- [x] **Step 9-1: 지연 import에 사유 주석을 남긴다**

spec 7절의 남는 부채다. `autoresearch/jobs/action_log.py`의 함수 내부 지연 import 4곳(209, 233, 239, 265행)에 아래 취지의 주석을 각 블록 위에 붙인다.

```python
# 함수 안에 두는 이유는 순환이 아니라 **비용**이다. 이 배치 진입점은 인자 검증만
# 하고 끝나는 경로가 있는데, 이 모듈들은 최상단에서 google.cloud.bigquery 등
# 무거운 의존을 끌어온다. --exposure-source 가 고르는 경로에서만 필요하다.
```

주석만 추가하고 **import 위치를 바꾸지 않는다.**

- [x] **Step 10: `examples/` 갱신**

```bash
grep -rn "src\." examples
```

- `examples/ctr_pipeline_scaffold/01_generate_mock_raw_data.py:24` → `autoresearch.feature_engineering.category_reference`
- `examples/ctr_pipeline_scaffold/02_generate_event_log.py:11,30,92` → `autoresearch.feature_engineering.feature_builder`
- `examples/ctr_pipeline_scaffold/sync_mock_data_to_pipeline.py:108` → `python -m autoresearch.cli build-features`
- `examples/ctr_pipeline_scaffold/README.md:99,123` → `autoresearch.feature_engineering.feature_builder`

- [x] **Step 11: `Dockerfile.train` CMD 갱신**

```dockerfile
# 기존 (54행)
CMD ["python", "-m", "src.cli", "--help"]
# 교체
CMD ["python", "-m", "autoresearch.cli", "--help"]
```

- [x] **Step 12: 테스트 실행**

```bash
uv run python -m pytest -q 2>&1 | tail -5
```

기대: Step 1의 `/tmp/baseline-result.txt`와 **동일한 통과 수**. `src/serving/` 관련 테스트가 아직 옛 경로를 쓰므로 통과해야 한다 (Task 1에서 serving을 안 건드렸으므로).

- [x] **Step 13: lint**

```bash
uv run --no-sync ruff check autoresearch src tests tools scripts
```

- [x] **Step 14: CLI 동작 확인**

```bash
uv run python -m autoresearch.cli --help
uv run python -m autoresearch.recommendation.daily_recommendations --help
```

기대: 정상 출력.

- [ ] **Step 15-1: `src/` 재생성 방지 CI 가드 추가**

exp 브랜치가 봉인된 옛 SHA에서 갈라져 나오므로, 그 브랜치가 `src/` 아래 **새 파일**을 추가하면 merge가 `src/`를 되살린다. 실험은 기존 하이퍼파라미터만 고치므로 실제로 일어나진 않지만, 되살아나면 조용히 넘어가는 것이 더 나쁘다.

`.github/workflows/lint.yml`의 Ruff job에 아래 step을 추가한다.

```yaml
      - name: src 디렉토리가 되살아나지 않았는지 확인
        run: |
          if [ -d src ]; then
            echo "::error::src/ 가 다시 생겼습니다 — #754 재배치 이후 이 경로는 사용하지 않습니다" >&2
            git ls-files src | head -20
            exit 1
          fi
```

로컬 확인:

```bash
[ -d src ] && echo "실패: src/ 가 아직 있다" || echo "통과"
```

- [x] **Step 15: 커밋**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor: src 임포트를 autoresearch 새 경로로 치환한다

앞 커밋의 파일 이동에 맞춰 임포트 101곳과 하드코딩된 config 경로를
갱신한다. cli.py의 다중행 임포트는 6개 모듈이 model_training과
model_evaluation 둘로 갈라지므로 두 문장으로 나눴다.

train.py·evaluate.py가 문자열로 조립하던 기본 config 경로와 cli.py의
typer help 문자열 4곳도 함께 고친다 — 임포트 치환으로는 잡히지 않는다.

examples 스캐폴드와 Dockerfile.train CMD도 새 경로를 가리키게 한다.

sys.path 블록은 건드리지 않았다. 이동 대상이 전부 깊이를 보존해
PROJECT_ROOT 계산이 그대로 유효하다.

Refs #754

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `applications/` 층 신설 — 완료 (커밋 `9bae803`·`e68bc2b`)

**계획과 달랐던 것.** 셋 다 Task 2 시점에 즉시 깨진다.

| 발견 | 왜 미룰 수 없나 |
| --- | --- |
| `verifier.py`의 블로킹 ruff 인자 | ruff는 **없는 경로를 인자로 받으면 exit 1**이다. 이 명령은 봉인된 워크스페이스에서 도는데 옛 트리에는 `applications/`가, 새 트리에는 `agent_orchestration/`이 없다. 고정 목록이면 한쪽 세대가 통째로 `ruff_failed`로 거부된다 — `_ruff_targets()`가 트리 모양을 보고 고른다 |
| `alembic.ini`의 `prepend_sys_path = %(here)s/..` | 이 파일이 한 단계 깊어져 `..`가 저장소 루트가 아니라 `applications/`를 가리킨다. `../..`로 고치지 않으면 migration이 `api.database`를 import하지 못한다 |
| `proxy/requirements.txt` 경로 | CI의 `uv lock & proxy export drift` job이 없는 경로에 export해 실패한다. 산출물도 새 경로로 재생성했다 |

**Step 15-1(Task 1에서 옮겨온 `src/` 부활 방지 가드)을 여기서 넣었다.** 대상을 셋으로
넓혔다 — `src`·`proxy`·`agent_orchestration` 전부 이번에 사라진다.

**임포트 치환 스크립트의 함정.** 점 표기 매핑에 bare `agent_orchestration`을 넣으면
`agent_orchestration/app` 같은 **슬래시 경로**와 `ruff check agent_orchestration` 같은
**CLI 인자**까지 점 표기로 바꿔버린다(`applications.experiment_platform/app`). 실제로
11개 파일이 이 형태로 망가져 손으로 되돌렸다. 다음 Task에서는 슬래시 규칙을 먼저
적용하거나 bare 매핑을 빼야 한다.

**Dockerfile COPY는 스크립트로 처리할 수 없다.** 원본 경로와 목적지 경로가 서로 다르게
바뀌기 때문이다(`COPY .../api ./.../app`처럼 어긋난다). 5개 모두 손으로 고쳤고,
`applications/__init__.py`와 `shared/__init__.py` COPY를 새로 넣었다.

**검증.** `2837 tests collected` — Task 2 착수 시점과 동일. `2818 passed / 2 failed(WSL2
환경) / 25 skipped`. Docker는 이 개발 환경에서 사용 불가라 이미지 빌드는 CI에 맡겼다.

**Files:**
- Create: `applications/__init__.py`, `applications/experiment_platform/shared/__init__.py`
- Move: `src/serving/` → `applications/reranking_api/`, `agent_orchestration/` → `applications/experiment_platform/`, `proxy/` → `applications/youtube_api_proxy/`, `loadtest/` + `autoresearch/loadtest/` → `applications/reranking_api/loadtest/`
- Modify: `deploy/serving/Dockerfile`, `deploy/agent_orchestration/*.Dockerfile`, `agent_orchestration/alembic.ini`, `docker-compose.yml`, 진입점 스크립트
- Modify: **`.github/workflows/lint.yml:33`** — `ruff check agent_orchestration autoresearch tests tools` → `ruff check autoresearch applications tests tools`. **이 Task와 같은 PR에 넣어야 한다.** Task 4로 미루면 이 PR 자신이 Lint 실패로 머지 불가가 된다
- Modify: **`applications/experiment_platform/executor/{verifier,prompt}.py`** — Task 1 Step 6-1에서 남겨둔 ruff 인자에 `applications`를 반영
- Modify: **경로를 문자열로 단언하는 테스트 8개** — `test_agent_orchestration_container.py`, `test_serving_deployment.py`, `test_experiment_models.py`, `test_ui_submission_app.py`, `test_ui_visual_contract.py`, `test_experiment_branch_migration.py`, `test_experiment_issue_migration.py`, `test_experiment_candidate_verifier.py`. 갱신하지 않으면 Step 7의 "기준선과 동일한 통과 수" 기대가 성립하지 않는다

- [x] **Step 1: 디렉토리 생성과 이동**

```bash
cd /home/yjlee/Autoresearch
mkdir -p applications && touch applications/__init__.py

git mv src/serving applications/reranking_api
rmdir src/features src/models src/pipeline src/utils 2>/dev/null
rm -rf src/__pycache__ src/*/__pycache__ 2>/dev/null
rmdir src 2>/dev/null || ls src   # 비어야 한다

git mv proxy applications/youtube_api_proxy

git mv agent_orchestration applications/experiment_platform
cd applications/experiment_platform
git mv app api
git mv ui workbench
mkdir -p shared && touch shared/__init__.py
for f in codex contracts github_app github_pull_requests github_refs bootstrap_secrets; do
  git mv "$f.py" "shared/$f.py"
done
cd /home/yjlee/Autoresearch

mkdir -p applications/reranking_api/loadtest
git mv loadtest/rerank.js  applications/reranking_api/loadtest/rerank.js
git mv loadtest/README.md  applications/reranking_api/loadtest/README.md
rmdir loadtest 2>/dev/null
git mv autoresearch/loadtest/rerank_fixture.py applications/reranking_api/loadtest/rerank_fixture.py
git mv autoresearch/loadtest/__init__.py       applications/reranking_api/loadtest/__init__.py
rmdir autoresearch/loadtest 2>/dev/null
```

- [x] **Step 2: 이동만 커밋**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor: 배포되는 서비스를 applications 층으로 옮긴다

파일 이동만 수행한다. 임포트 치환은 다음 커밋에서 한다.

Refs #754

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

- [x] **Step 3: 임포트 치환**

Task 1의 스크립트를 재사용하되 `DOTTED`를 아래로 바꾼다.

```python
DOTTED = [
    ("src.serving",                    "applications.reranking_api"),
    ("autoresearch.loadtest",          "applications.reranking_api.loadtest"),
    ("agent_orchestration.app",        "applications.experiment_platform.api"),
    ("agent_orchestration.ui",         "applications.experiment_platform.workbench"),
    ("agent_orchestration.runner",     "applications.experiment_platform.runner"),
    ("agent_orchestration.launcher",   "applications.experiment_platform.launcher"),
    ("agent_orchestration.executor",   "applications.experiment_platform.executor"),
    ("agent_orchestration.codex",      "applications.experiment_platform.shared.codex"),
    ("agent_orchestration.contracts",  "applications.experiment_platform.shared.contracts"),
    ("agent_orchestration.github_app", "applications.experiment_platform.shared.github_app"),
    ("agent_orchestration.github_pull_requests", "applications.experiment_platform.shared.github_pull_requests"),
    ("agent_orchestration.github_refs","applications.experiment_platform.shared.github_refs"),
    ("agent_orchestration.bootstrap_secrets", "applications.experiment_platform.shared.bootstrap_secrets"),
]
FROM_PKG = {"src.serving": {"*": "applications.reranking_api"}}
```

```bash
uv run python /tmp/rewrite_imports.py
grep -rn "src\.serving\|agent_orchestration\.\|autoresearch\.loadtest" --include=*.py . | grep -v '\.venv\|\.worktrees'
```

기대: 0건.

- [x] **Step 4: `src/serving/model_loader.py:40` 주석 수정**

```python
# 기존
# 학습 config(src/pipeline/config.yaml artifacts.*) 파일명이 바뀌면 함께 갱신한다.
# 교체
# 학습 config(autoresearch/model_training/config.yaml artifacts.*) 파일명이 바뀌면 함께 갱신한다.
```

- [x] **Step 5: 배포 파일 경로 갱신**

```bash
grep -rn "agent_orchestration\|src/serving\|src\.serving\|^COPY proxy" deploy/ applications/experiment_platform/docker-compose.yml applications/experiment_platform/alembic.ini applications/experiment_platform/*.sh
```

- `deploy/serving/Dockerfile:36` → `CMD ["uvicorn", "applications.reranking_api.app:app", "--host", "0.0.0.0", "--port", "8000"]`
- `deploy/agent_orchestration/{api,runner,ui,launcher,executor}.Dockerfile` — 아래 Step 5-1 참조
- `applications/experiment_platform/alembic.ini` — `script_location` 이 상대 경로면 그대로, 절대/패키지 경로면 갱신
- `applications/experiment_platform/{entrypoint.sh,runner_entrypoint.sh}` — `python -m agent_orchestration.*` 갱신
- `applications/experiment_platform/docker-compose.yml` — build context·volume 경로 갱신

- [x] **Step 5-1: 에이전트 이미지 5개의 COPY 허용 목록 갱신**

이 다섯 Dockerfile은 디렉토리 통째가 아니라 **모듈을 하나씩 골라 COPY하는 허용 목록** 방식이다(#701이 launcher에 `github_pull_requests`를 넣어 맞춘 그 목록). 공유 모듈이 `shared/`로 내려가므로 전부 바뀐다.

경로 접두사는 모든 파일에서 동일하게 바뀐다:

```
agent_orchestration/                    → applications/experiment_platform/
agent_orchestration/app                 → applications/experiment_platform/api
agent_orchestration/ui                  → applications/experiment_platform/workbench
agent_orchestration/<공유모듈>.py        → applications/experiment_platform/shared/<공유모듈>.py
```

**추가로 각 이미지에 `applications/__init__.py`와 `shared/__init__.py` COPY를 넣어야 한다.** 빠뜨리면 패키지 import가 런타임에 실패한다 — 이것이 이 Step의 주된 실패 모드다.

| 파일 | 바꿀 행 |
| --- | --- |
| `api.Dockerfile` | 42-48, 52, 53, 63 (`CMD`) |
| `executor.Dockerfile` | 46, 49, 50, 51, 62 (`CMD`) |
| `launcher.Dockerfile` | 23, 24, 27, 28, 29, 34, 41 (`CMD`) |
| `runner.Dockerfile` | 37, 38, 39, 40, 41, 51 (`CMD`) |
| `ui.Dockerfile` | 25, 28-32, 45 (`CMD`) |

예 — `api.Dockerfile`:

```dockerfile
COPY applications/__init__.py ./applications/
COPY applications/experiment_platform/__init__.py ./applications/experiment_platform/
COPY applications/experiment_platform/api ./applications/experiment_platform/api
COPY applications/experiment_platform/shared/__init__.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/shared/contracts.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/shared/bootstrap_secrets.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/shared/github_app.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/shared/github_refs.py ./applications/experiment_platform/shared/
COPY applications/experiment_platform/entrypoint.sh ./applications/experiment_platform/
COPY applications/experiment_platform/alembic.ini ./applications/experiment_platform/
COPY applications/experiment_platform/migrations ./applications/experiment_platform/migrations
...
CMD ["./applications/experiment_platform/entrypoint.sh"]
```

`CMD` 변경:

```dockerfile
# executor
CMD ["python", "-m", "applications.experiment_platform.executor.main"]
# launcher
CMD ["python", "-m", "applications.experiment_platform.launcher.main"]
# runner
CMD ["./applications/experiment_platform/runner_entrypoint.sh"]
# ui
CMD ["streamlit", "run", "applications/experiment_platform/workbench/app.py", \
     "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true", \
     "--browser.gatherUsageStats=false", "--server.fileWatcherType=none"]
```

`ui.Dockerfile`은 `app/experiments/models.py`를 선별 COPY하므로 `api/experiments/models.py`로 바꾼다. `.streamlit` COPY는 그대로 둔다.

**정적 가드를 먼저 고친다 — `docker run`보다 싸다.**

`tests/test_agent_orchestration_container.py:418-453`이 이미 이 실패 모드를 잡는 가드다. entrypoint가 import하는 모듈이 Dockerfile COPY 목록에 있는지 정적으로 검사한다. `bootstrap_secrets.py`와 `github_pull_requests.py`(#700)에서 같은 누락이 두 번 났기 때문에 만들어졌다.

`_copied_sources`가 `COPY agent_orchestration/`로 시작하는 줄만 수집하므로, 접두사를 바꾸지 않으면 **수집 결과가 빈 집합이 되어 가드가 무력해진다.**

```python
# 기존
        if not stripped.startswith("COPY agent_orchestration/"):
# 교체
        if not stripped.startswith("COPY applications/"):
```

같은 파일에서 `LAUNCHER_DOCKERFILE` 등 경로 상수와 모듈명 매핑(`module.replace(".", "/")`)이 새 구조를 반영하는지 함께 확인한다.

```bash
uv run python -m pytest tests/test_agent_orchestration_container.py -v 2>&1 | tail -5
```

그 다음 빌드와 런타임 import로 재확인한다:

```bash
docker build -f deploy/agent_orchestration/executor.Dockerfile -t ao-executor:ci .
docker run --rm ao-executor:ci python -c "import applications.experiment_platform.executor.main"
```

- [x] **Step 6: alembic migration 경로 확인**

```bash
grep -n "script_location\|prepend_sys_path\|version_locations" applications/experiment_platform/alembic.ini
grep -rn "agent_orchestration" applications/experiment_platform/migrations/env.py
```

`env.py`가 모델을 import 하면 경로를 갱신한다. **revision 파일 자체는 수정하지 않는다** — 이미 적용된 마이그레이션 이력이다.

- [x] **Step 7: 테스트·lint**

```bash
uv run python -m pytest -q 2>&1 | tail -5
uv run --no-sync ruff check autoresearch applications tests tools scripts
```

기대: Step 1 기준선과 동일한 통과 수.

- [x] **Step 8: 이미지 빌드 검증**

```bash
docker build -f Dockerfile.app   -t autoresearch:ci .
docker build -f Dockerfile.train -t autoresearch-train:ci .
docker build -f deploy/serving/Dockerfile -t autoresearch-serving:ci .
for img in api runner ui launcher executor; do
  docker build -f "deploy/agent_orchestration/$img.Dockerfile" -t "ao-$img:ci" . || break
done
```

이미지 빌드 성공만으로는 부족하다. COPY 허용 목록이 빠진 모듈은 **런타임에야** 드러나므로 import까지 확인한다:

```bash
docker run --rm ao-api:ci      python -c "import applications.experiment_platform.api.main"
docker run --rm ao-executor:ci python -c "import applications.experiment_platform.executor.main"
docker run --rm ao-launcher:ci python -c "import applications.experiment_platform.launcher.main"
docker run --rm ao-runner:ci   python -c "import applications.experiment_platform.runner.app"
docker run --rm ao-ui:ci       python -c "import applications.experiment_platform.workbench.app"
```

- [x] **Step 9: 커밋**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor: applications 층 임포트와 배포 경로를 갱신한다

서빙 API를 applications/reranking_api로, 에이전트 플랫폼을
applications/experiment_platform으로, proxy를 youtube_api_proxy로
옮긴 데 맞춰 임포트와 Dockerfile·compose·entrypoint 경로를 고친다.

experiment_platform 최상단에 흩어져 있던 공유 모듈 6개(866줄)를
shared/로 모은다 — api/workbench/runner/launcher/executor 다섯이
공유하는 것들이라 최상단에 두면 어느 서비스 소유인지 읽히지 않는다.

이름이 겹쳐 혼란을 주던 최상위 loadtest(k6)와 autoresearch/loadtest
(픽스처)를 reranking_api/loadtest 한 곳으로 합친다.

alembic revision 파일은 적용 이력이므로 수정하지 않았다.

Refs #754

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `tests/` 소스 구조 미러링 — 완료 (커밋 `f1a489b`·`e3ab015`)

**계획서 매핑은 정확했다.** 163개 중 162개를 그대로 덮었고(나머지는 기존
`tests/__init__.py`), 중복·누락이 없었다. 다만 `<확인>` 표시 2건 중 하나가 틀렸다 —
`test_action_logs_schema_policy.py`는 `action_log_generation.schema`만 import하므로
`recommendation`이 아니라 `action_log_generation`이다. `tests/action_logs/` 디렉터리는
이미 없어서 리네임 단계는 건너뛰었다.

**계획서가 놓친 것 — 깊이에 의존하는 경로 계산.**

`Path(__file__).resolve().parents[1]`이 저장소 루트를 가리키는 테스트가 **37개**였다.
한 단계(또는 `applications/<svc>/`는 두 단계) 깊어지면 이 값이 `tests/`를 가리킨다.
`tests/fixtures`를 형제로 참조하던 2곳도 같다. 이동 커밋과 분리해 고쳤다.

**계획서가 놓친 것 — ci.yml이 파일 이름을 열거한다.**

`pytest (feast group)` job이 테스트 12개를, `pytest (postgres group)`이 1개를 경로로
나열한다. 어긋나면 job이 실패한다. `test_serving_deployment.py`가 그 목록을 계약으로
고정하므로 함께 갱신했다.

**드러난 기존 결함 — `.env`가 프로세스 환경으로 샌다.**

이동으로 실행 순서가 바뀌자 `tests/jobs/test_feast_materialize.py` 3건이 깨졌다. 원인은
`test_rerank_loadtest_fixture.py`가 부르는 `provisioner.main()` 안의 `load_dotenv()`다.
이것은 monkeypatch가 아니라 **프로세스 환경을 직접** 바꾸므로, 로컬 `.env`의
`AUTORESEARCH_ENV=dev`가 세션 끝까지 남아 이후 테스트가 dev 환경으로 오인된다.

이동이 만든 버그가 아니라 순서가 가려주던 기존 결함이다. CI에는 `.env`가 없어 드러나지
않는다 — 로컬에서만 나는 종류다. 해당 모듈에 autouse fixture로 `load_dotenv`를 막았다.

**검증.** `2838 tests collected` — 이동 전과 정확히 동일. `2819 passed / 2 failed(WSL2
환경) / 25 skipped`.

**Files:**
- Move: `tests/test_*.py` 다수 → `tests/<패키지명>/`
- Modify: `tests/conftest.py` (필요 시)

- [x] **Step 1: 기준선 재확인**

```bash
uv run python -m pytest --collect-only -q 2>/dev/null | tail -1
```

이 숫자를 Task 3 종료 시 비교한다.

- [x] **Step 2: 디렉토리 생성**

```bash
cd /home/yjlee/Autoresearch/tests
for p in feature_engineering model_training model_evaluation recommendation \
         model_registry reporting data_collection virtual_user_generation \
         jobs cli applications; do
  mkdir -p "$p" && touch "$p/__init__.py"
done
mkdir -p applications/reranking_api applications/experiment_platform
touch applications/reranking_api/__init__.py applications/experiment_platform/__init__.py
```

기존 `tests/action_logs/`는 `tests/action_log_generation/`으로 리네임한다.

```bash
git mv action_logs action_log_generation
```

- [x] **Step 3: 테스트 파일을 대상 모듈 기준으로 이동**

각 테스트가 무엇을 import 하는지로 목적지를 정한다.

```bash
cd /home/yjlee/Autoresearch
for f in tests/test_*.py; do
  target=$(grep -ohE "(from|import) (autoresearch|applications)\.[a-z_]+(\.[a-z_]+)?" "$f" \
           | awk '{print $2}' | cut -d. -f1-2 | sort | uniq -c | sort -rn | head -1 | awk '{print $2}')
  echo "$f -> $target"
done
```

출력을 보고 이동한다. 예:

아래는 위 명령으로 산출한 결과를 목적지별로 묶은 것이다. **`<확인>` 표시는 휴리스틱이 애매한 파일**이므로 파일을 열어 실제 대상 모듈을 확인하고 확정한다.

**`tests/model_training/`**

```
test_pipeline_train.py → test_train.py
test_build_training_dataset.py
test_build_training_dataset_feast_path.py
test_build_training_dataset_env_check_feast.py
test_training_provenance.py
test_training_snapshot_store.py
test_spine_coverage_guard.py
test_downsampling.py
test_models_contract.py
test_model_utils.py
test_calibration.py            <확인> src/models/calibration 인지 action_logs/calibration 인지
```

**`tests/model_evaluation/`**

```
test_pipeline_evaluate.py → test_evaluate.py
test_degradation_eval.py
test_degradation_eval_hold.py
test_degradation_eval_dates.py
test_degradation_eval_detection.py
test_degradation_eval_staleness.py
test_paired_experiment.py
test_pipeline_seed_sweep.py → test_seed_sweep.py
test_training_comparison.py
test_pipeline_experiment_evaluation.py → test_experiment_evaluation.py
test_experiment_evaluation_temporal_signal.py
test_pipeline_promotion_evidence.py → test_promotion_evidence.py
```

**`tests/model_evaluation/experiments/`**

```
test_experiment_context.py
test_experiment_promotion_gate.py
```

**`tests/recommendation/`**

```
test_daily_recommendations.py
test_simulate_policy_round.py
test_model_exposure_provider.py
test_rerank_api.py
test_policy_selector.py
test_action_logs_schema_policy.py   <확인> action_log_generation 과 걸침
```

**`tests/feature_engineering/`**

```
test_embeddings.py
test_feature_builder.py
test_features_assembly.py
test_model_feature_contract.py
test_odfv_category_match_feast.py
test_build_pool_feature_frame_feast.py
test_feast_retrieval_integration_feast.py
test_verify_registry_portability_feast.py
```

**`tests/model_registry/`**

```
test_model_package.py
test_tracking_promote.py
test_tracking_registry.py
test_tracking_namespace.py
test_tracking_promotion_result.py
```

**`tests/reporting/`**

```
test_experiment_result_report.py
```

**`tests/data_collection/`**

```
test_youtube_client.py
test_youtube_collection_load.py
test_youtube_collection_fetch.py
test_youtube_collection_schema.py
test_youtube_collection_backfill.py
test_youtube_collection_transform.py
```

**`tests/virtual_user_generation/`**

```
test_virtual_users_schema.py
test_virtual_users_pipeline.py
test_virtual_users_categories.py
test_virtual_users_glm_generator.py
test_virtual_users_persona_source.py
test_virtual_user_adapter.py → test_adapter.py
```

**`tests/action_log_generation/`** (기존 `tests/action_logs/`를 리네임한 곳)

```
test_action_logs_daily.py
test_action_logs_pipeline.py
test_action_logs_llm_generator.py
test_action_logs_observability.py
test_click_threshold_calibration.py
test_click_threshold_calibrate_job.py
```

**`tests/jobs/`**

```
test_action_log_job.py
test_action_log_job_telemetry.py
test_action_log_quality_job.py
test_feast_materialize.py
test_feature_store_build.py
test_youtube_backfill_job.py
test_youtube_trending_job.py
```

**`tests/cli/`**

```
test_cli.py
```

**`tests/applications/reranking_api/`**

```
test_serving_api.py
test_serving_onnx.py
test_serving_schemas.py
test_serving_feast_reader.py
test_serving_feast_reader_feast.py
test_serving_online_features.py
test_serving_model_registry.py
test_serving_deployment.py
test_rerank_loadtest_fixture.py
```

**`tests/applications/experiment_platform/`**

```
test_agent_orchestration.py                  test_experiment_models.py
test_agent_orchestration_runner.py           test_experiment_router.py
test_agent_orchestration_bootstrap.py        test_experiment_service.py
test_agent_orchestration_container.py        test_experiment_postgres.py
test_agent_orchestration_ui_cost.py          test_experiment_cost_api.py
test_agent_orchestration_ui_step.py          test_experiment_report_api.py
test_agent_orchestration_ui_time.py          test_experiment_step_router.py
test_agent_orchestration_ui_write.py         test_experiment_step_service.py
test_agent_orchestration_ui_report.py        test_experiment_candidate_api.py
test_ui_board.py                             test_experiment_issue_endpoint.py
test_ui_submission_app.py                    test_experiment_issue_publication.py
test_ui_submission_form.py                   test_experiment_issue_migration.py
test_ui_visual_contract.py                   test_experiment_transition_service.py
test_github_app.py                           test_experiment_branch_baseline.py
test_github_refs.py                          test_experiment_branch_migration.py
test_github_issues.py                        test_experiment_candidate_migration.py
test_github_pull_requests.py                 test_experiment_executor.py
test_issue_authoring.py                      test_experiment_executor_router.py
test_executor_report.py                      test_experiment_executor_integration.py
test_executor_training.py                    test_experiment_workspace.py
test_executor_measurement.py                 test_experiment_codex_worker.py
test_executor_results_store.py               test_experiment_candidate_verifier.py
test_executor_command_output.py              test_experiment_candidate_finalizer.py
test_launcher_resident.py                    test_experiment_launcher.py
test_launcher_log_collector.py               test_experiment_pull_request.py
test_launcher_job_resources.py               test_experiment_pull_request_run.py
test_launcher_training_environment.py        test_experiment_pull_request_adapters.py
test_harness_resource_budget.py              test_auto_experiment_trigger_label.py
```

**`tests/applications/youtube_api_proxy/`**

```
test_proxy_app.py
test_proxy_docker.py
```

**`tests/` 루트에 남기는 것** — 여러 패키지에 걸치거나 저장소 자체를 검사하는 테스트다. 무리해서 나누지 않는다.

```
conftest.py                             test_logging_json.py
paired_experiment_fixtures.py           test_redis_iam.py
test_release_workflow.py                test_feature_repo_env.py
test_branch_protection_contract.py      test_feast_apply_workflow.py
test_auto_research_issue_branch.py      test_offline_retrieval_smoke_feast.py
test_build_static_features.py           test_odfv_registry_portability_feast.py
test_load_raw_to_bigquery.py            test_verify_serving_e2e.py
test_generate_action_logs_scale.py      test_degradation_curve_plot.py
test_rewrite_action_log_event_ids.py
test_pr_report_archive.py               test_pr_report_archive_rail.py
test_pr_report_archive_merge.py         test_pr_report_archive_search.py
test_pr_report_archive_category.py      test_pr_report_archive_workflow.py
test_pr_report_archive_card_isolation.py
```

이동은 `git mv`로 한다. 예:

```bash
git mv tests/test_pipeline_train.py tests/model_training/test_train.py
```

- [x] **Step 4: 수집 개수 비교**

```bash
uv run python -m pytest --collect-only -q 2>/dev/null | tail -1
```

기대: Step 1과 **정확히 같은 숫자**. 다르면 파일이 유실됐거나 `__init__.py`가 빠져 수집이 안 되는 것이다.

- [x] **Step 5: 이름 충돌 확인**

같은 basename의 테스트 파일이 서로 다른 디렉토리에 있으면 `__init__.py`가 없을 때 pytest가 충돌을 낸다. Step 2에서 전부 만들었는지 확인한다.

```bash
find tests -type d -not -path '*__pycache__*' -exec sh -c '[ -f "$1/__init__.py" ] || echo "missing __init__: $1"' _ {} \;
```

- [x] **Step 6: 전체 실행**

```bash
uv run python -m pytest -q 2>&1 | tail -5
```

- [x] **Step 7: CI 테스트 목록 갱신**

`.github/workflows/ci.yml`의 feast·postgres 그룹 job이 테스트 경로를 명시적으로 나열한다. 이동한 경로로 갱신한다.

```bash
grep -n "tests/" .github/workflows/ci.yml
```

- [x] **Step 8: 커밋**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor: 테스트를 소스 구조에 맞춰 재배치한다

소스가 12개 패키지로 나뉘었는데 tests가 플랫이면 "이 모듈 테스트가
어디에 있나"가 새 문제가 된다. 소스와 같은 모양으로 미러링한다.

여러 패키지에 걸친 통합 테스트는 루트에 남겼다. 디렉토리마다
__init__.py를 넣어 같은 basename의 테스트 파일이 충돌하지 않게 한다.

수집 테스트 수가 재배치 전후로 동일함을 확인했다.

Refs #754

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `deployment/` 이동과 CI 경로 갱신 — 완료 (커밋 `d655e58`·`231261d`)

**계획서보다 넓게 했다 — 하위 디렉터리 이름까지.** `deploy/agent_orchestration/`은 그
패키지가 #757로 사라졌으므로 `deployment/experiment_platform/`으로, `ui.Dockerfile`은 그것이
만드는 `workbench` 패키지에 맞춰 `workbench.Dockerfile`로 바꿨다. 이름과 내용이 어긋난
채로 두면 이번 재배치가 지우려던 드리프트가 그대로 남는다.

**게시되는 이미지 이름(`autoresearch-agent-orchestration-*`)은 건드리지 않았다** — 인접
저장소 `Autoresearch-infra`의 K8s 매니페스트와 동시에 바꿔야 하는 배포 계약이다. 별도
과제로 남긴다.

**계획서가 놓친 것 — `Path` 컴포넌트 표기.**

`REPOSITORY_ROOT / "deploy" / "serving" / "Dockerfile"`처럼 경로를 **컴포넌트로 나눠
인용**한 곳은 슬래시 치환 규칙(`deploy/` → `deployment/`)이 잡지 못한다. 테스트 3개
파일에서 나왔고 전부 손으로 고쳤다. #757에서도 같은 부류를 한 번 놓쳤으므로, 다음
작업자는 `"<이동대상>"` 형태를 따로 grep한다.

**추가한 가드.** 워크플로가 `-f`/`file:`로 가리키는 빌드 파일이 실제로 있는지 검사한다.
Dockerfile 경로가 틀리면 `docker build`가 실패하지만, 그 job이 paths 필터에 걸려 **돌지
않으면** 아예 드러나지 않는다. 경로를 일부러 틀리게 만들어 잡히는 것을 확인했다.

**검증.** `2821 passed / 2 failed(WSL2 환경) / 25 skipped`, 수집 2839.

**Files:**
- Move: `deploy/` → `deployment/`, `Dockerfile.{app,train,feast}` → `deployment/`
- Modify: `.github/workflows/{ci,release,feast-apply,lint}.yml`, `.github/ISSUE_TEMPLATE/auto_research.yml`, `pyproject.toml`, `.dockerignore`

- [x] **Step 1: 이동**

```bash
cd /home/yjlee/Autoresearch
git mv deploy deployment
git mv Dockerfile.app   deployment/Dockerfile.app
git mv Dockerfile.train deployment/Dockerfile.train
git mv Dockerfile.feast deployment/Dockerfile.feast
```

- [x] **Step 2: 워크플로우 경로 갱신**

```bash
grep -rn "deploy/\|Dockerfile\.\|src/\*\*\|src\.cli\|src\.pipeline\|src\.serving\|agent_orchestration\|proxy/\|loadtest/" .github/workflows/
```

`agent_orchestration`·`proxy`·`loadtest`를 패턴에 반드시 포함한다. 이들은 매칭에 실패해도 **에러가 아니라 job이 안 도는** 방식으로 조용히 망가진다.

고칠 것:

| 위치 | 변경 |
| --- | --- |
| `ci.yml:48,56,69,79` | `paths` 필터 `'src/**'` → `'autoresearch/**'`, `'applications/**'` |
| `ci.yml:82-84` | `agent_orchestration` 필터의 `'agent_orchestration/**'` → `'applications/experiment_platform/**'`, `'deploy/agent_orchestration/**'` → `'deployment/agent_orchestration/**'`. **매칭 실패 시 이미지 5개 빌드가 조용히 스킵되고, Task 2의 COPY 허용 목록 안전망까지 함께 꺼진다** |
| `ci.yml:266`, `release.yml:215` | `src.pipeline.daily_recommendations` → `autoresearch.recommendation.daily_recommendations` |
| `ci.yml:311,312,313,378` | `python -m src.cli` → `python -m autoresearch.cli` |
| `ci.yml:404`, `release.yml:401` | `import ... src.serving.app` → `applications.reranking_api.app` |
| 전 워크플로우 | `-f Dockerfile.app` → `-f deployment/Dockerfile.app` (train·feast 동일) |
| 전 워크플로우 | `deploy/serving/Dockerfile` → `deployment/serving/Dockerfile` (agent_orchestration 이미지 5개 동일) |

`feature_repo` 관련 경로(`feast-apply.yml`의 path 필터, `ci.yml:391`의 `load_feature_store('/app/feature_repo')`)는 **건드리지 않는다.**

- [x] **Step 3: 이슈 템플릿의 `build-features` 안내** — Task 1에서 처리했다

`.github/ISSUE_TEMPLATE/auto_research.yml:212`의 `python -m src.cli build-features` → `python -m autoresearch.cli build-features`. Task 4까지 미루면 그 사이 발행된 실험이 없는 모듈을 부른다.

이 문구는 실험 에이전트가 읽는 가설 템플릿이다. 갱신 직후부터 새 실험이 새 경로를 쓴다.

- [x] **Step 3-1: scope 라벨 문자열 — 전환 기간 동안 두 문자열을 모두 받는다**

같은 파일 254행의 라벨

```
- label: prod 모델 계약(`src/features/model_contract.py`) 수정을 허용한다
```

은 단순 안내문이 아니라 **정확 일치 계약**이다. `tools/auto_research_issue_branch.py:79`의
`_SCOPE_LABELS`가 이 문자열을 키로 써서 `prod_model_contract` scope로 매핑한다.

> **정정 (2026-08-14, 구현 중 실측)** — 처음에는 "한쪽만 고치면 scope가 **조용히
> 사라진다**"고 적었으나 **틀렸다.** `_parse_allowed_scope`는 알 수 없는 라벨을 만나면
> `ValueError("allowed_scope contains an unknown guardrail")`로 **fail-closed** 거부한다.
> 조용한 실패가 아니라 그 이슈가 통째로 반려된다.
>
> 증상은 다르지만 대응은 같다 — 오히려 더 급하다. 조용히 좁아지는 것이 아니라 **새 실험이
> 하나도 발행되지 않기** 때문이다.

| 고치는 것 | 안 고치면 |
| --- | --- |
| 템플릿 라벨만 | 새 이슈의 라벨이 매핑에 없어 **모든 신규 실험이 반려**된다 |
| `_SCOPE_LABELS` 키만 | 봉인된 진행 중 실험의 이슈 본문이 매핑되지 않아 같은 증상 |
| 둘 다 동시에 | 봉인된 옛 본문이 남아 있는 동안 같은 증상 |

따라서 `_SCOPE_LABELS`가 **두 문자열을 모두 받아야** 한다 — verifier의 `src/` 허용과 같은
전환 기간 조치다.

**열린 이슈가 아니라 봉인된 본문이 기준이다.** 구현 시점에 열린 `[AR]` 이슈 7건 중 옛
문자열을 본문에 가진 것은 0건이었지만, DB에 봉인된 진행 중 실험의 `issue_body`는 그와
별개다. 제거 시점은 `experiments.base_dev_sha` 기준 미종료 실험 0건으로 판단한다.

**추가한 가드.** Issue Form 의 checkbox 라벨이 전부 `_SCOPE_LABELS` 에 있는지 검사한다
(`test_issue_form_labels_are_all_known_scopes`). 템플릿만 고치고 매핑을 빠뜨리면 그
순간부터 신규 실험이 전부 반려되는데, 증상이 "이슈 발행은 되는데 실험이 시작되지
않는다"라 원인을 찾기 어렵다. 템플릿을 일부러 어긋나게 만들어 잡히는 것을 확인했다.

- [x] **Step 4: `pyproject.toml` 주석 갱신**

121-123행:

```toml
# 기존
# Phase 1(이슈 #80): 의존성 관리만 uv 로 전환하고 패키지 배치 방식(sys.path)은
# 유지한다. Phase 2 에서 src 레이아웃 전환 시 package 빌드로 변경 예정.
# 교체
# 의존성 관리만 uv 로 전환하고 패키지 배치 방식(sys.path)은 유지한다(이슈 #80).
# #754의 디렉토리 재배치는 sys.path 방식을 그대로 두었다 — 설치형 패키지
# (build-system + [project.scripts]) 전환은 여전히 별도 과제다.
```

- [x] **Step 5: `.dockerignore` 확인**

```bash
cat .dockerignore
```

`src/`나 `deploy/`를 명시하면 갱신한다.

- [x] **Step 6: 로컬 이미지 빌드**

```bash
docker build -f deployment/Dockerfile.app   -t autoresearch:ci .
docker build -f deployment/Dockerfile.train -t autoresearch-train:ci .
docker build -f deployment/Dockerfile.feast -t autoresearch-feast:ci .
docker build -f deployment/serving/Dockerfile -t autoresearch-serving:ci .
```

- [x] **Step 7: 워크플로우 문법 검사**

```bash
git diff --check
command -v actionlint >/dev/null && actionlint || echo "actionlint 없음 — 건너뜀"
```

- [x] **Step 8: 커밋**

```bash
git add -A
git commit -m "$(cat <<'EOF'
chore: 배포 산출물을 deployment로 모으고 CI 경로를 갱신한다

Dockerfile 3개가 최상위에 흩어져 있던 것을 deploy/와 함께
deployment/로 모은다. CI·release 워크플로우의 실행 경로와 paths
필터, 이슈 템플릿의 build-features 명령을 새 모듈 경로로 고친다.

이슈 템플릿 문구는 실험 에이전트가 읽는 가설 계약이라, 이 커밋
시점부터 새 실험이 autoresearch.cli 경로를 쓴다.

pyproject의 "Phase 2 src 레이아웃 전환" 주석은 이번 재배치가 그
전환이 아님을 명시하도록 고쳤다 — 설치형 패키지 전환은 별도 과제다.

feature_repo 경로는 건드리지 않았다.

Refs #754

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: 문서 갱신 — 완료 (커밋 `feed427`)

**범위를 셋으로 나눴다.** 문서 전체에서 옛 경로를 기계적으로 치환하면 역사적 기록까지
바뀌므로, 무엇을 고칠지 기준을 먼저 세웠다.

| 층 | 대상 | 처리 |
| --- | --- | --- |
| 정본 | `README.md`, `CLAUDE.md`/`AGENTS.md`, `.claude/docs/*`, `docs/README.md` | 구조 절·코드 영역 표·폴더 책임을 다시 씀 |
| 상시 갱신 | `docs/guides/`, `docs/runbooks/`, `docs/adr/` | 경로 전면 갱신 |
| 살아있는 계약 | `docs/specs/` | 실행 가능한 참조와 계약 경로만 갱신 |
| 역사 기록 | `docs/plans/`, `docs/archive/` | **손대지 않음** |

`docs/plans/` 아래 완료된 구현 계획의 코드 스니펫은 그 시점의 기록이다. `docs/README.md`
규칙상 `archive/`로 옮겨야 할 것들이라, 경로만 고치면 오히려 어중간해진다. 아카이브
정리는 별도 과제로 남긴다.

**치환 우선순위는 "복사해 실행하면 실패하는 것"이다.** `python -m src.cli`,
`docker build -f Dockerfile.*`, import 문 순이다. 산문 속 경로 언급은 그 다음이다.

**실패한 시도 하나를 기록한다.** `src/pipeline/` → 단일 문자열 치환 규칙을 넣었더니
25개 spec 에 `autoresearch/ 단계 패키지에 config.yaml` 같은 문구가 생겼다. `src/pipeline`은
네 패키지로 갈라졌으므로 **디렉터리 단위 대응이 없다.** 되돌리고 파일 단위 매핑
(`src/pipeline/train.py` → `autoresearch/model_training/train.py` …)으로 다시 했다.
Task 1의 임포트 치환에서 이미 겪은 것과 같은 함정인데 문서에서 반복했다.

**추가한 검증.** 마크다운 상대 링크의 타깃이 실제로 존재하는지 전수 확인했다 —
아카이브로 옮긴 plan 을 가리키던 링크 1건이 잡혔다.

`tests/test_release_workflow.py`의 code archive 계약 테스트는 **주석이 사실과 달라져**
함께 고쳤다. "두 패키지가 모두 아카이브에 들어가야 한다"는 근거가, 재배치로 한 패키지
안의 두 단계가 되면서 성립하지 않는다. 단언 자체는 그대로다.

**Files:**
- Modify: `README.md`, `CLAUDE.md`, `AGENTS.md`, `.claude/docs/agent-project-reference.md`, `.claude/docs/architecture-overview.md`, `docs/README.md`, 살아있는 `docs/specs/`·`docs/guides/`
- Modify: `docs/specs/2026-07-15-repo-restructure.md` (대체 표기)
- Move or rewrite: `docs/plans/2026-07-15-src-package-merge.md`

- [x] **Step 1: 갱신 대상 목록 만들기**

```bash
grep -rln "src/\|src\.\|agent_orchestration\|^deploy/" \
  README.md CLAUDE.md AGENTS.md .claude/docs docs/README.md docs/specs docs/guides \
  2>/dev/null | sort
```

`docs/archive/`는 목록에 넣지 않는다.

- [x] **Step 2: `README.md` 저장소 구조 절 교체**

26-52행의 구조 블록을 spec 4절의 최종 구조로 교체한다. 213행의 미해결 표기

> `src/serving/`(리랭킹 API)과 정책 라운드·일일 추천 폐루프의 코드 경계는 저장소 구조 논의(#149)에서 정리 예정입니다.

는 `applications/reranking_api/` 기준의 코드 책임으로 다시 쓴다.

54-67행 배포 이미지 표의 `Dockerfile.*` 경로를 `deployment/` 기준으로 고친다.

- [x] **Step 3: `CLAUDE.md`·`AGENTS.md` 갱신**

두 파일은 내용이 동일하다(같은 크기·날짜). 한쪽을 고치고 복사한다.

"저장소 경계" 절의 경로 표기와 "Local Development"의 ruff 명령

```bash
uv run --no-sync ruff check agent_orchestration autoresearch tests tools
```

을 새 경로로 고친다:

```bash
uv run --no-sync ruff check autoresearch applications tests tools
```

```bash
cp CLAUDE.md AGENTS.md   # 갱신 후 동기화
diff CLAUDE.md AGENTS.md && echo "동일"
```

- [x] **Step 4: `.claude/docs/` 갱신**

`agent-project-reference.md`의 폴더 책임 경계 표, `architecture-overview.md`의 경로를 새 구조로 고친다.

- [x] **Step 5: 선행 문서에 대체 표기 추가**

`docs/specs/2026-07-15-repo-restructure.md`의 "결정 3" 절 머리에 추가:

```markdown
> **대체됨 (2026-08-13)** — 이 결정은
> [`docs/specs/2026-08-13-repository-structure-redesign.md`](2026-08-13-repository-structure-redesign.md)로
> 대체되었습니다. 아래 목표 구조는 채택되지 않았습니다.
```

`docs/plans/2026-07-15-src-package-merge.md`는 `docs/archive/plans/`로 옮긴다.

```bash
git mv docs/plans/2026-07-15-src-package-merge.md docs/archive/plans/
```

- [x] **Step 6: `docs/README.md` 인덱스 갱신**

새 spec·plan을 인덱스에 추가하고, 아카이브로 옮긴 plan의 항목을 옮긴다.

- [x] **Step 7: 최종 전수 확인**

```bash
grep -rn "from src\.\|import src\.\|python -m src\." --include=*.py --include=*.yml \
  --include=*.yaml --include=*.md --include=Dockerfile* . \
  | grep -v '\.venv\|\.worktrees\|docs/archive'
```

기대: **0건.**

- [x] **Step 8: 전체 검증**

```bash
uv run python -m pytest -v 2>&1 | tail -5
uv run --no-sync ruff check autoresearch applications tests tools scripts
git diff --check
```

feast 그룹:

```bash
uv sync --only-group feast
# .github/workflows/ci.yml 의 pytest (feast group) job 테스트 목록을 실행
uv sync   # dev 환경 복구
```

- [x] **Step 9: 커밋**

```bash
git add -A
git commit -m "$(cat <<'EOF'
docs: 새 디렉토리 구조를 문서에 반영한다

README 구조 절과 배포 이미지 표, CLAUDE.md·AGENTS.md의 저장소 경계와
ruff 명령, .claude/docs의 폴더 책임 표를 새 경로로 갱신한다.

#149가 남긴 2026-07-15 spec의 결정 3에 대체 표기를 넣고, 짝이 되는
plan은 아카이브로 옮긴다. docs/archive/ 이하는 역사적 기록이므로
경로를 갱신하지 않았다.

Refs #754

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: 인접 저장소 갱신 — 부분 완료

**`Autoresearch-airflow`는 끝났다** — `SKYAHO/Autoresearch-airflow#324`(이슈 #323).
실측으로 확인한 범위였다.

| 위치 | 처리 |
| --- | --- |
| `dags/ctr_training/dag.py`, `dags/ctr_model_promote/dag.py` | `module="src.cli"` → `"autoresearch.cli"` |
| DAG parse 계약 테스트 3곳 | 함께 갱신 |
| `Dockerfile.*` 표기 | `deployment/` 반영 |
| `autoresearch.jobs.*` 6곳 | **변경 없음** — 공개 batch CLI 계약이라 재배치 대상이 아니었다 |
| `values.yaml` digest | 미변경 — 승격 PR이 소유한다 |

**순서 제약이 이 작업의 핵심이었다.** 배치 이미지는 소스를 담지 않고 GCS 코드
아카이브에서 부트스트랩하며(#752) 아카이브는 릴리스 digest에 묶인다. 배포된 digest의
아카이브에는 아직 `src/`가 있어 당장은 돌아가지만, **다음 릴리스의 digest 승격 PR이
머지되는 순간** 두 DAG가 동시에 `ModuleNotFoundError`로 죽는다. 그래서 이 PR이 승격보다
먼저 들어가야 했다.

**남은 것 — feast 레지스트리 전환.** Task 1이 ODFV 헬퍼의 import 경로를 바꿨다.
처음에는 "apply와 이미지 롤아웃 순서를 맞추면 된다"고 적었으나 **실측 결과 교착이었다** —
apply가 기존 레지스트리를 읽는 단계에서 실패해 어느 순서로도 진행되지 않는다. 전환용
shim으로 apply 한 번을 통과시켜야 하고, 그 뒤에 이미지 롤아웃 순서 문제가 남는다.

| 레지스트리 | 이미지 코드 | 결과 |
| --- | --- | --- |
| 옛 (`src.features…`) | 옛 | 정상 — 현재 운영 상태 |
| 옛 | 새 | `ModuleNotFoundError` |
| 새 (`autoresearch…`) | 옛 | `ModuleNotFoundError` |
| 새 | 새 | 정상 — 목표 |

두 혼합 상태가 모두 깨지므로 apply와 digest 승격 사이 창을 일일 배치가 돌지 않는
시간대로 잡아야 한다.

**남은 것 — 게시 이미지 이름.** `autoresearch-agent-orchestration-*`는
`Autoresearch-infra`의 K8s 매니페스트와 동시에 바꿔야 한다.

이 저장소의 PR이 머지되고 이미지가 배포된 **이후** 수행한다.

- [x] **Step 1: `Autoresearch-airflow`에서 호출 경로 확인**

```bash
gh api repos/SKYAHO/Autoresearch-airflow/contents --jq '.[].name'
# 로컬 클론이 있으면
grep -rn "src\.cli\|src\.pipeline\|src\.serving\|python -m src" <airflow-repo-path>
```

- [x] **Step 2: 이슈 발행 후 브랜치에서 경로 갱신, PR 생성**

`autoresearch.jobs.*` 호출은 변경 없음을 함께 확인한다.

- [x] **Step 3: 실험 발행 재개**

Task 4에서 이슈 템플릿을 갱신했으므로, 재개 후 첫 실험이 완주하는지 확인한다.

---

## 검증 요약

| 시점 | 명령 | 기대 |
| --- | --- | --- |
| Task 1 이전 | `pytest --collect-only -q \| tail -1` | 기준선 기록 |
| 각 Task 끝 | `uv run python -m pytest -q` | 기준선과 동일한 통과 수 |
| Task 3 끝 | `pytest --collect-only -q \| tail -1` | Task 1 이전과 **정확히 동일** |
| Task 2·4 끝 | `docker build` 4종 | 성공 |
| Task 5 끝 | `grep -rn "from src\.\|import src\.\|python -m src\."` (archive 제외) | 0건 |
| Task 5 끝 | `uv run --no-sync ruff check autoresearch applications tests tools scripts` | 통과 |

## PR 전략

Task마다 별도 PR을 올린다. `main` 기준, `Closes #754`는 마지막 PR에만 넣고 나머지는 `Refs #754`를 쓴다.

| PR | Task | 성격 | 상태 |
| --- | --- | --- | --- |
| [#755](https://github.com/SKYAHO/Autoresearch/pull/755) | spec·plan + Task 0 | 설계 확정 + 잔재 정리 | **머지** (main `247644e`) |
| [#756](https://github.com/SKYAHO/Autoresearch/pull/756) | Task 1 | 최대 diff — 이동/치환 2커밋 + executor 슬래시 경로 + feast 레지스트리 | **머지** — 아래 사유로 #755 squash에 포함 |
| [#757](https://github.com/SKYAHO/Autoresearch/pull/757) | Task 2 | applications 층 + `lint.yml` + ruff 대상·alembic·proxy export | **머지** |
| [#758](https://github.com/SKYAHO/Autoresearch/pull/758) | Task 3 | 테스트 재배치 | **머지** |
| [#761](https://github.com/SKYAHO/Autoresearch/pull/761) | Task 4 | 배포·CI paths 필터 + `deployment/experiment_platform/` 리네임 | 리뷰 대기 |
| 7 | Task 5 | 문서 | 구현 완료 |

**머지 순서가 어긋나 Task 1이 #755의 squash에 삼켜졌다.** #756이 중간 브랜치
`chore/754-repo-structure-redesign`으로 먼저 머지되고, 그 브랜치를 담은 #755가 main으로
squash 머지됐다. 그래서 main의 `247644e`는 제목이 `docs: ...`인데 **191개 파일,
+2948/-583**을 담는다. 트리 자체는 정확하다(main == Task 1 브랜치 tip, diff 없음).
main 히스토리는 재작성하지 않았다 — 진행 중 exp 브랜치의 base와 다른 클론이 어긋난다.

**여기서 얻은 규칙:** 스택 PR은 **아래에서 위로** 머지한다. 중간 브랜치를 base로 삼은
PR을 먼저 머지하면 그 내용이 아래 PR의 squash에 흡수되어 커밋 제목이 내용을 잃는다.
`Closes #754`가 #755에 들어 있던 것도 같은 종류의 사고였다 — 이슈가 Task 1 시점에
자동 종료됐다(다시 열었다). **`Closes`는 마지막 PR에만 넣는다.**

PR 2~6은 순서 의존이므로 앞 PR이 머지된 뒤 rebase해 올린다.

**PR 3에 반드시 함께 들어가야 하는 것** — `.github/workflows/lint.yml:33`의 ruff 대상. Task 2가 `agent_orchestration/`을 없애므로, 이 줄을 뒤 PR로 미루면 **PR 3 자신이 Lint 실패로 머지 불가**가 된다.
