# 저장소 구조 재설계 — 파이프라인 단계 축 재배치

- 작성일: 2026-08-13
- 상태: 설계 승인 완료, 구현 계획 작성 대기
- 관련: 당시 `README.md:213`의 서빙·추천 코드 경계 미정 기록(저장소 구조 논의
  #149에서 정리 예정)

## 0. 선행 문서와의 관계

이 문서는 #149가 남긴 두 문서를 **대체**합니다.

| 선행 문서 | 처리 |
| --- | --- |
| `docs/specs/2026-07-15-repo-restructure.md` | 결정 1·2(문서 통합, 잔재 정리)는 구현 완료된 역사적 기록으로 유지. **결정 3(`src/` 통합)은 이 문서로 대체** — 해당 절에 대체 표기를 추가한다 |
| `docs/plans/2026-07-15-src-package-merge.md` | 이 문서 기준으로 재작성하거나 `docs/archive/plans/`로 이동 |

선행 설계와 달라진 점:

| 항목 | 2026-07-15 설계 | 이 문서 |
| --- | --- | --- |
| `src/pipeline` | `autoresearch/training/` 한 폴더 | `model_training`·`model_evaluation`·`recommendation`·`reporting` 4분할 |
| 폴더 명명 | 기존 이름 유지(`features`, `models`, `tracking`, `utils`) | 파이프라인 단계 축으로 개명 |
| `proxy`·`agent_orchestration`·서빙 | 최상위 유지 (범위 제외) | `applications/` 층 신설 후 이동 |
| 학습 CLI | `autoresearch/training/cli.py` | `autoresearch/cli.py` |

선행 plan이 규정한 **기술적 실행 조건은 그대로 유효**합니다: Model Training / Feast
Features의 코드 경계를 보존하고, `src/`를 건드리는 열린 PR·브랜치의 충돌 가능성을
착수 전에 확인합니다.

## 1. 배경 — 왜 지금 구조에서 "어느 폴더에 뭐가 있는지" 예상이 안 되는가

원인이 네 겹으로 쌓여 있습니다.

### 1-1. 최상위 파이썬 패키지 3개가 서로 다른 명명 축을 씁니다

| 폴더 | 이름의 축 | 실제 내용 |
| --- | --- | --- |
| `autoresearch/` | 제품명 (pyproject `name`) | 수집·가상유저·action log·공개 batch CLI |
| `src/` | 관례어 (아무 정보 없음) | CTR 학습·서빙 파이프라인 |
| `agent_orchestration/` | 기능명 | 실험 에이전트 서비스 |

같은 층에 있는데 축이 달라 "어느 폴더를 열지"가 이름에서 나오지 않습니다.

### 1-2. `src`가 파이썬 관례를 배신합니다

파이썬에서 `src/`는 통상 import 경로에 나타나지 않는 소스 루트입니다. 그러나 이
저장소에서는 `from src.pipeline.train import ...` 형태로 **패키지 이름 자체**로
쓰입니다(`from src.` 93개 파일, `import src.` 8개 파일).

게다가 `pyproject.toml`에 `build-system`도 `[tool.setuptools] packages`도 없습니다.
즉 설치된 패키지가 아니라 **리포 루트가 `sys.path`에 얹혀서** 동작합니다.
`src/`, `src/features/`, `src/pipeline/`에는 `__init__.py`조차 없는 암묵적
namespace 패키지입니다(`__init__.py`가 있는 곳은 `models/`, `serving/`,
`tracking/`, `utils/` 4개뿐).

### 1-3. 눈에 보이는 최상위 디렉토리의 절반 이상이 git에 없습니다

`ls`로 보이는 디렉토리는 34개인데 추적되는 것은 14개입니다. 미추적 잔재:
`dags/`, `data/`, `artifacts/`, `asset/`, `output/`, `mlruns/`,
`Nemotron-Personas-Korea/`, `.codex-tmp/`, `.omo/`, `.gjc/`, `.playwright-mcp/`.
루트의 PNG 3개(`before-submit.png`, `after-submit.png`, `hypotheses-filled.png`)와
`agent.md`(`CLAUDE.md`/`AGENTS.md`와 별개인 11KB)도 같은 성격의 노이즈입니다.

### 1-4. 실제 경계는 3개가 아니라 2개입니다

의존 그래프 실측 결과:

```
agent_orchestration  ──▶ (파이프라인 코드를 한 줄도 import 하지 않음)
                     ◀── src/cli.py:854 1곳 (함수 내부 지연 import)

src ──8──▶ autoresearch          ┐
autoresearch ──4──▶ src          │  순환
src ──1──▶ feature_repo          │
feature_repo ──1──▶ src          ┘
```

- `agent_orchestration`은 이미 독립 애플리케이션입니다. 자체
  `docker-compose.yml`, `alembic.ini` + `migrations/`, `README.md`, 배포 이미지
  5개(api/runner/ui/launcher/executor), 테스트 215개를 가집니다.
- 반대로 `src`·`autoresearch`·`feature_repo`는 **패키지 수준에서 서로를 참조**합니다.
  즉 현재의 3분할은 설계가 아니라 사후 봉합입니다.

  > **정정 (2026-08-13, Task 1 구현 중 실측)** — 처음에는
  > `autoresearch/jobs/action_log.py`의 함수 내부 지연 import 4곳이 "최상단에 두면 순환
  > import로 실패하기 때문"이라고 적었으나 **틀렸습니다.** 네 개를 모두 모듈 최상단으로
  > 올려도 import와 관련 테스트 36개가 통과합니다. `action_log_generation`은 자기 패키지
  > 밖의 `autoresearch.*`를 하나도 import하지 않고, `recommendation`·`model_training` 어느
  > 쪽도 `jobs.action_log`를 참조하지 않습니다 — 실제 방향은
  > `jobs.action_log → recommendation → action_log_generation` DAG입니다.
  > 지연 import의 실제 이유는 **비용**입니다: `model_exposure_provider`가 최상단에서
  > `google.cloud.bigquery`를, `rerank_api`가 `requests`를 끌어오는데, 둘 다
  > `--exposure-source`가 고르는 경로에서만 필요합니다.

따라서 실제 덩어리는 `폐루프 파이프라인(src + autoresearch + feature_repo)`과
`실험 에이전트(agent_orchestration)` 2개이며, 현재 구조는 이 2덩어리를 3개
폴더로 잘못 잘라놓은 상태입니다.

## 2. 승인된 결정

| 결정 항목 | 채택안 |
| --- | --- |
| 정리 범위 | 경계까지 재설계 |
| 에이전트 배치 | 같은 저장소, `applications/` 아래 |
| 폐루프 순환 의존 | **유지** — 이름·배치만 정리 (`contracts/` 신설 안 함) |
| 서빙 위치 | `applications/reranking_api/` 로 통째 (`model_serving/` 신설 안 함) |
| `feature_repo/` | 이번 범위에서 **제자리 유지** |
| 학습 CLI | `src/cli.py` → `autoresearch/cli.py` |
| 테스트 | 소스 구조 미러링 |

## 3. 목표 / 비목표

### 목표

- 최상위에서 이름만 보고 "어느 폴더에 무엇이 있는지" 예상 가능하게 만든다.
- `src`라는 무의미한 이름을 없앤다.
- 파이프라인(라이브러리)과 애플리케이션(배포되는 서비스)을 층으로 분리한다.
- 최상위 미추적 잔재를 정리해 구조를 읽을 때의 노이즈를 없앤다.

### 비목표 (이번 범위 밖)

- **동작 변경 없음.** 순수 이동·리네임·임포트 치환이다.
- 순환 의존 해소 없음. `recommendation ↔ action_log_generation` 순환은 남는다.
- `feature_repo/` 이동 없음.
- 패키징 방식 변경 없음 (`build-system` 도입, 설치형 패키지 전환은 별도 과제).
- 모듈 내부 로직·API 변경 없음.

## 4. 최종 구조

```
autoresearch/                        # 폐루프 파이프라인 (단일 배포 패키지)
├── cli.py                           # 학습·평가·승격 CLI (typer)
├── logging_json.py                  # jobs + reranking_api 공용
├── jobs/                            # 공개 batch CLI — Airflow 소비 계약
├── data_collection/
├── virtual_user_generation/
├── action_log_generation/
├── feature_engineering/
├── model_training/
├── model_evaluation/
│   └── experiments/
├── recommendation/
├── model_registry/
└── reporting/

applications/                        # 배포되는 서비스
├── experiment_platform/
│   ├── shared/
│   ├── api/
│   ├── workbench/
│   ├── runner/
│   ├── launcher/
│   ├── executor/
│   ├── migrations/
│   └── alembic.ini
├── reranking_api/
│   └── loadtest/
└── youtube_api_proxy/

feature_repo/                        # 제자리 유지 (feast 규약 디렉토리)
deployment/                          # ← deploy/  (+ Dockerfile.* 3개)
scripts/
tests/                               # 소스 구조 미러링
docs/
```

## 5. 파일 매핑

모든 추적 파일에 자리가 있음을 검산했습니다: `src/` 50개 → 50개,
`autoresearch/` 38개 → 38개, 누락 없음.

`src/` 내역: `cli.py` 1, `features/` 6, `models/` 5, `pipeline/` 20(`.py` 19 +
`config.yaml`), `serving/` 8, `tracking/` 8, `utils/` 2.
`autoresearch/` 내역: `__init__.py` 1, `logging_json.py` 1, `action_logs/` 9,
`experiments/` 3, `jobs/` 9, `loadtest/` 2, `virtual_users/` 6,
`youtube_collection/` 7.

### 5-1. `src/` → `autoresearch/`

| 현재 | 이동 후 |
| --- | --- |
| `src/cli.py` | `autoresearch/cli.py` |
| `src/features/` (6) | `autoresearch/feature_engineering/` |
| `src/models/` (5) | `autoresearch/model_training/` |
| `src/utils/model_utils.py`, `__init__.py` | `autoresearch/model_training/` |
| `src/pipeline/train.py` | `autoresearch/model_training/` |
| `src/pipeline/build_training_dataset.py` | `autoresearch/model_training/` |
| `src/pipeline/training_provenance.py` | `autoresearch/model_training/` |
| `src/pipeline/training_snapshot_store.py` | `autoresearch/model_training/` |
| `src/pipeline/config.yaml` | `autoresearch/model_training/config.yaml` |
| `src/pipeline/evaluate.py` | `autoresearch/model_evaluation/` |
| `src/pipeline/degradation_eval.py` | `autoresearch/model_evaluation/` |
| `src/pipeline/experiment_evaluation.py` | `autoresearch/model_evaluation/` |
| `src/pipeline/training_comparison.py` | `autoresearch/model_evaluation/` |
| `src/pipeline/paired_experiment.py` | `autoresearch/model_evaluation/` |
| `src/pipeline/seed_sweep.py` | `autoresearch/model_evaluation/` |
| `src/pipeline/promotion_evidence.py` | `autoresearch/model_evaluation/` |
| `src/pipeline/daily_recommendations.py` | `autoresearch/recommendation/` |
| `src/pipeline/simulate_policy_round.py` | `autoresearch/recommendation/` |
| `src/pipeline/model_exposure_provider.py` | `autoresearch/recommendation/` |
| `src/pipeline/policy_selector.py` | `autoresearch/recommendation/` |
| `src/pipeline/rerank_api.py` | `autoresearch/recommendation/` |
| `src/pipeline/virtual_user_adapter.py` | `autoresearch/virtual_user_generation/adapter.py` |
| `src/pipeline/report_html.py` | `autoresearch/reporting/` |
| `src/pipeline/experiment_result_report.py` | `autoresearch/reporting/` |
| `src/tracking/` (8) | `autoresearch/model_registry/` |
| `src/serving/` (8) | `applications/reranking_api/` |

### 5-2. `autoresearch/` 내부 리네임

| 현재 | 이동 후 |
| --- | --- |
| `autoresearch/youtube_collection/` (7) | `autoresearch/data_collection/` |
| `autoresearch/virtual_users/` (6) | `autoresearch/virtual_user_generation/` |
| `autoresearch/action_logs/` (9) | `autoresearch/action_log_generation/` |
| `autoresearch/experiments/` (3) | `autoresearch/model_evaluation/experiments/` |
| `autoresearch/loadtest/` (2) | `applications/reranking_api/loadtest/` |
| `autoresearch/jobs/` (9) | 유지 |
| `autoresearch/logging_json.py` | 유지 |

### 5-3. 애플리케이션

| 현재 | 이동 후 |
| --- | --- |
| `agent_orchestration/app/` | `applications/experiment_platform/api/` |
| `agent_orchestration/ui/` | `applications/experiment_platform/workbench/` |
| `agent_orchestration/runner/` | `applications/experiment_platform/runner/` |
| `agent_orchestration/launcher/` | `applications/experiment_platform/launcher/` |
| `agent_orchestration/executor/` | `applications/experiment_platform/executor/` |
| `agent_orchestration/{codex,contracts,github_app,github_pull_requests,github_refs,bootstrap_secrets}.py` | `applications/experiment_platform/shared/` |
| `agent_orchestration/migrations/`, `alembic.ini` | `applications/experiment_platform/` |
| `agent_orchestration/{docker-compose.yml,entrypoint.sh,runner_entrypoint.sh,README.md}` | `applications/experiment_platform/` |
| `proxy/` (4) | `applications/youtube_api_proxy/` |
| `loadtest/` (k6 `rerank.js`, `README.md`) | `applications/reranking_api/loadtest/` |
| `deploy/` (19) | `deployment/` |
| `Dockerfile.app`, `Dockerfile.train`, `Dockerfile.feast` | `deployment/` |

### 5-4. 배치 판단이 필요했던 파일

이름만으로 정해지지 않아 **실제 임포트 관계를 근거로** 배치했습니다.

| 파일 | 배치 | 근거 |
| --- | --- | --- |
| `virtual_user_adapter.py` | `virtual_user_generation/adapter.py` | 소비자가 `build_training_dataset`(학습)와 `daily_recommendations`(추천) 양쪽이라 어느 단계에도 안 속함. 가상 유저를 변환하는 어댑터이므로 생산자 쪽에 둔다 |
| `utils/model_utils.py` | `model_training/model_utils.py` | 저장은 `train.py`·`lgbm_model.py`, 읽기는 `evaluate.py`·`degradation_eval.py`. 아티팩트 형식의 소유자가 학습이므로 학습에 두고 평가 → 학습 단방향으로 만든다 |
| `rerank_api.py` | `recommendation/` | 서빙 API 호출 클라이언트이며 유일 소비자가 `jobs/action_log.py` |
| `experiments/{context,promotion_gate}.py` | `model_evaluation/experiments/` | 소비자가 `paired_experiment.py`(평가) 하나뿐 |
| `autoresearch/loadtest/` | `applications/reranking_api/loadtest/` | 최상위 `loadtest/`(k6)와 이름이 겹쳐 혼란을 유발하므로 리랭킹 API 아래로 합쳐 중복을 없앤다 |

## 6. 계약 영향

### 6-1. 유지되는 계약 (변경 없음)

- `python -m autoresearch.jobs.*` — Airflow가 소비하는 공개 batch CLI.
  `docs/specs/2026-07-13-public-batch-execution-contract.md` 그대로.
- `feature_repo` 경로 전체 — `feature_store.yaml:31`의
  `type: feature_repo.redis_iam.IAMRedisOnlineStore`, `ci.yml:391`의
  `load_feature_store('/app/feature_repo')`, `Dockerfile.feast:48`,
  `.github/workflows/feast-apply.yml`의 path 필터, ODFV UDF의 cwd 기준 bare
  import 계약(#409).
- 환경 변수(`.env.example`), PostgreSQL 스키마, alembic revision 이력.

### 6-2. 변경되는 실행 계약

```
python -m src.cli <sub>                       → python -m autoresearch.cli <sub>
python -m src.pipeline.daily_recommendations  → python -m autoresearch.recommendation.daily_recommendations
uvicorn src.serving.app:app                   → uvicorn applications.reranking_api.app:app
import src.serving.app                        → import applications.reranking_api.app
```

소비처 전수:

| 위치 | 내용 |
| --- | --- |
| `Dockerfile.train:54` | `CMD ["python", "-m", "src.cli", "--help"]` |
| `deploy/serving/Dockerfile:36` | `CMD ["uvicorn", "src.serving.app:app", ...]` |
| `.github/workflows/ci.yml` | 266, 311, 312, 313, 378, 404행 (실행 경로) |
| `.github/workflows/release.yml` | 215, 401행 |
| `.github/ISSUE_TEMPLATE/auto_research.yml:212` | 에이전트가 읽는 가설 템플릿 본문 |
| `scripts/` | `validate_feast_assembly.py`, `verify_registry_portability.py`, `bench/compare_seed_sweeps.py`, `bench/daily_as_of_probe.py`, `bench/window_holdout_eval.py`, `bench/degradation_curve_plot.py` |
| `examples/ctr_pipeline_scaffold/` | `01_generate_mock_raw_data.py`(`src.features.category_reference`), `02_generate_event_log.py`(`src.features.feature_builder`), `sync_mock_data_to_pipeline.py:108`(`python -m src.cli build-features`), `README.md` 2곳 |
| `.github/workflows/ci.yml` | 48, 56, 69, 79행의 `paths` 필터 `'src/**'` |
| `pyproject.toml:121-123` | `[tool.uv]` 주석 "Phase 2 에서 src 레이아웃 전환 시 package 빌드로 변경 예정" — 이 재배치가 그 Phase 2가 아님을 명시하도록 갱신(설치형 전환은 여전히 별도 과제) |
| `agent_orchestration/executor/` | **슬래시 표기 하드코딩 — 6-3-1절 참조.** `verifier.py`(357, 582행), `training.py`(70행), `prompt.py`(85, 91, 105, 362행) |
| `.github/workflows/lint.yml:33` | `ruff check agent_orchestration autoresearch tests tools` — 단계 2에서 `agent_orchestration/`이 사라지므로 **같은 PR에서** 고쳐야 Lint가 통과한다 |
| `.github/workflows/ci.yml:82-84` | `agent_orchestration` paths 필터(`agent_orchestration/**`, `deploy/agent_orchestration/**`) — 매칭되지 않으면 실패가 아니라 에이전트 이미지 5개 빌드가 **조용히 스킵**된다 |
| `tests/` 8개 파일 | 경로를 문자열로 단언한다. 6-3-2절 참조 |
| `SKYAHO/Autoresearch-airflow` | **별도 저장소** — 호출 경로 갱신 PR 필요 |

### 6-3. 하드코딩된 경로 문자열

문자열로 조립되어 임포트 치환으로는 잡히지 않습니다. 수동 확인 필요:

- `src/pipeline/train.py:590` — `os.path.join(project_root, "src", "pipeline", "config.yaml")`
- `src/pipeline/evaluate.py:311` — 동일 패턴
- `src/cli.py` 292, 404, 455, 1219행 — typer help 문자열 `"config.yaml 경로 (기본: src/pipeline/config.yaml)"`
- `src/serving/model_loader.py:40` — 주석의 `src/pipeline/config.yaml` 참조
- `src/features/feature_builder.py:9-15` — 모듈 docstring의 feast ODFV 계약 서술

전수 확인 명령: `grep -rn '"src"\|src/\|src\.' --include=*.py --include=*.yml
--include=*.yaml --include=Dockerfile* . | grep -v '\.venv\|\.worktrees'`

**점 표기만 확인하면 놓칩니다.** 임포트 치환은 `src.pipeline.train` 같은 점
표기를 다루지만, 아래 값들은 **슬래시 표기 문자열**이라 임포트 문법이 아닙니다.
확인 grep을 점 표기로만 좁히면 통째로 빠집니다.

### 6-3-1. 실험 에이전트 executor의 하드코딩 경로 — 최우선

| 위치 | 현재 값 | 갱신하지 않으면 |
| --- | --- | --- |
| `executor/verifier.py:357` | `if path == "src/features/model_contract.py"` | **게이트 소멸 (조용한 정책 회귀)** — 아래 상술 |
| `executor/verifier.py:582` | 블로킹 ruff 인자 `"agent_orchestration"` | 경로 부재 → `CandidateVerificationError("ruff_failed")` → **모든 candidate 거부** |
| `executor/training.py:70` | `_FEATURE_DEFINITION_PATHS = ("feature_repo", "src/pipeline/build_training_dataset.py")` | 매칭 없음 → 피처 정의 변경 감지가 조용히 멈춤 |
| `executor/prompt.py:85, 91` | `"src/** (src/features/model_contract.py 제외)"`, scope 설명 | Codex에게 없는 경로를 계속 안내 |
| `executor/prompt.py:105` | `"uv run --no-sync ruff check agent_orchestration autoresearch tests tools"` | 실패하는 명령을 안내 |
| `executor/prompt.py:362` | 채점 경로 `src/pipeline/evaluate.py` | 부정행위 금지 안내가 없는 경로를 가리킴 |

**`prod_model_contract` 게이트가 왜 조용히 사라지는가:**

```python
# verifier.py:_path_is_allowed
if path == "src/features/model_contract.py":
    return "prod_model_contract" in policy.allowed_scope   # ← scope 없으면 거부
if path.startswith("src/"):
    return True
if path.startswith(_BASE_ALLOWED_PREFIXES):                # ("autoresearch/", "tests/", "tools/")
    return True
```

단계 1이 이 파일을 `autoresearch/feature_engineering/model_contract.py`로 옮기면
정확 매칭이 다시는 걸리지 않고, `autoresearch/` 접두사 검사가 무조건 `True`를
돌려줍니다. 즉 **실험 에이전트가 프로덕션 모델 계약 파일을 scope 없이 편집할 수
있게 됩니다.** 실패가 아니라 권한이 넓어지는 방향이라 CI가 잡지 못합니다.

더 나쁜 것은, 이 계약을 고정하는
`tests/test_experiment_candidate_verifier.py:151`가 tmp 저장소에
`src/features/model_contract.py`를 **직접 만들어** 검증한다는 점입니다. 실제
저장소에 그 경로가 없어져도 테스트는 계속 통과합니다. **테스트가 초록인 채로
게이트만 죽습니다.**

따라서 단계 1에서 게이트를 **두 경로 모두**에 걸도록 고칩니다.

```python
_MODEL_CONTRACT_PATHS: Final = frozenset({
    "src/features/model_contract.py",                       # 봉인된 옛 트리
    "autoresearch/feature_engineering/model_contract.py",   # 재배치 후
})
```

### 6-3-3. executor 이미지와 봉인된 트리의 버전 어긋남

`src/` 접두사 허용을 **바로 지우면 안 됩니다.**

`_validate_path_files`(`verifier.py:464-470`)는 워크스페이스의 diff 경로를
검사합니다. 그런데 워크스페이스는 DB에 봉인된 `base_dev_sha`에서 만든 `exp/*`
브랜치이고, executor는 릴리스된 이미지 digest로 돕니다. **둘의 버전이 다를 수
있습니다.**

재배치 후 빌드된 executor 이미지가 재배치 **전** 봉인 SHA 실험을 검증하면:

```
워크스페이스 트리: src/pipeline/train.py 를 수정
새 verifier:      src/ 접두사 허용 없음
결과:             CandidateVerificationError("forbidden_path") → candidate 거부
```

9-1절에서 "실험 발행을 중단할 필요가 없다"고 결론지었는데, 그 실측은 **git
머지 동작**만 다뤘고 이 verifier allowlist 차원을 보지 못했습니다. 다만 결론이
뒤집히지는 않습니다 — 전환 기간 동안 `src/` 허용을 남기면 해소되기 때문입니다.

**전환 기간 종료 조건:** 진행 중 실험이 모두 종료·머지되어 봉인 SHA가 전부
재배치 이후가 되면, `src/` 허용 줄과 `_MODEL_CONTRACT_PATHS`의 옛 경로를
제거합니다. 별도 이슈로 남깁니다.

### 6-3-2. 경로를 문자열로 단언하는 테스트

`tests/` 8개 파일이 경로 문자열을 계약으로 들고 있습니다:
`test_agent_orchestration_container.py`, `test_serving_deployment.py`,
`test_experiment_models.py`, `test_ui_submission_app.py`,
`test_ui_visual_contract.py`, `test_experiment_branch_migration.py`,
`test_experiment_issue_migration.py`, `test_experiment_candidate_verifier.py`.

특히 `test_agent_orchestration_container.py:418-453`은 **entrypoint가 import하는
모듈이 Dockerfile COPY 목록에 있는지 정적으로 검사하는 기존 가드**입니다
(`_copied_sources`가 `COPY agent_orchestration/`로 시작하는 줄만 수집).
`bootstrap_secrets.py`와 `github_pull_requests.py`(#700)에서 같은 누락이 두 번
났기 때문에 만들어진 가드로, 단계 2의 COPY 허용 목록 누락을 `docker run`보다
훨씬 싸게 잡아 줍니다. 반드시 새 접두사로 갱신합니다.

### 6-4. `sys.path` 조작 블록

`src/`가 설치형 패키지가 아니라 `sys.path` 의존이었기 때문에, 다음 파일들이
상단에 `sys.path` 조작 블록(`# noqa: E402` 동반)을 가지고 있습니다:
`src/cli.py`, `src/pipeline/{train,evaluate,build_training_dataset}.py`,
`scripts/{verify_registry_portability,fetch_redis_ca,provision_rerank_loadtest_fixture,build_static_features}.py`,
`scripts/bench/{daily_as_of_probe,window_holdout_eval}.py`.

이동 후에도 `package = false`는 유지되므로 이 블록들은 **제거하지 않고 경로만
갱신**합니다. 제거 가능 여부는 설치형 전환 과제에서 다룹니다.

### 6-5. 문서 참조

`docs/` 이하 82개 마크다운이 `src/` 경로를 언급합니다. 전부 고치지 않습니다:

- **갱신 대상**: `README.md`(4곳), `.claude/docs/agent-project-reference.md`,
  `.claude/docs/architecture-overview.md`, `docs/specs/`의 살아있는 계약 문서,
  `docs/guides/`, `docs/README.md`.
- **갱신 제외**: `docs/archive/` 이하 전부. `docs/README.md` 규칙상 아카이브
  문서는 역사적 기록이므로 내용을 갱신하지 않습니다.

### 6-6. 패키지 초기화

`src/`, `src/features/`, `src/pipeline/`은 `__init__.py`가 없는 암묵적 namespace
패키지입니다. `autoresearch/`는 정식 패키지이므로, 새로 만드는 하위 패키지
(`feature_engineering`, `model_training`, `model_evaluation`, `recommendation`,
`reporting`)마다 `__init__.py`를 추가합니다. `applications/`와
`applications/experiment_platform/shared/`도 동일합니다.

## 7. 남는 부채 (이번 범위 밖, 기록만)

- **`recommendation` → `action_log_generation` 방향 의존.** 폐루프 프로젝트에서
  파이프라인 단계 축으로 자르면 마지막 단계가 첫 단계를 참조하게 됩니다. 다만 1절의
  정정대로 이것이 **import 순환을 만들지는 않습니다** — 현재 그래프는 DAG입니다.
  `jobs/action_log.py`의 지연 import 4곳은 순환 회피가 아니라 무거운 선택적 의존
  (`google.cloud.bigquery`, `requests`)을 인자 검증 경로에서 떼어 놓기 위한 것이며,
  주석도 그렇게 적습니다. 최상단으로 올리는 것 자체는 지금도 가능하므로, 올릴지 말지는
  진입점 import 비용 문제로 별도 판단합니다.
- **`feature_repo/` 이동.** feast registry 재생성과 ODFV UDF 계약(#409) 검증이
  필요하므로 별도 이슈로 분리합니다.
- **설치형 패키지 전환.** `build-system` + `[project.scripts]` 도입은 별도 과제입니다.

## 8. 단계 분해와 검증

동작 변경 0을 유지하기 위해 단계마다 전체 테스트 통과를 게이트로 둡니다.

| 단계 | 내용 | 검증 |
| --- | --- | --- |
| 0 | 루트 청소 — 미추적 잔재를 `.gitignore`에 추가, 루트 PNG 3개·`agent.md` 정리 | `git status --porcelain` 결과가 비어 있음 |
| 1 | `src/*` → `autoresearch/*` 이동, `__init__.py` 추가, 임포트 101곳 치환, 하드코딩 경로 6곳 + `sys.path` 블록 갱신, `examples/` 7곳, `Dockerfile.app:41` | `uv run python -m pytest`, `ruff check`, `docker build -f Dockerfile.app` |
| 2 | `src/serving` → `applications/reranking_api`, `proxy`·`agent_orchestration` 이동, `shared/` 신설, `loadtest/` 통합 | `pytest`, `ruff`, `docker build` 3종 |
| 3 | `tests/` 소스 구조 미러링 재배치 | `pytest` 수집 테스트 수가 이전과 동일 |
| 4 | `deploy` → `deployment`, `Dockerfile.*` 이동, CI·release·이슈 템플릿 경로 갱신, `pyproject.toml` 주석 갱신 | CI 전체 통과 |
| 5 | 문서 갱신 — `README.md`, `.claude/docs/*`, `docs/README.md`, 살아있는 spec/guide (아카이브 제외), 선행 문서 2건 대체 표기 | `git diff --check` |
| 6 | `SKYAHO/Autoresearch-airflow` 호출 경로 갱신 | 별도 저장소 PR |

1단계가 가장 큽니다(임포트 101곳). 여기서 초록을 확인하면 이후는 기계적입니다.

각 단계는 별도 PR로 올립니다. 단계 1·2 안에서는 파일 이동(`git mv`) 커밋과
임포트 치환 커밋을 분리해 git의 rename 감지를 살립니다.

### 검증 명령

```bash
uv run python -m pytest -v
uv run --no-sync ruff check autoresearch applications tests tools scripts
docker build -f deployment/Dockerfile.app -t autoresearch:ci .
git diff --check
```

feast 계열은 `uv sync --only-group feast` 환경에서 `.github/workflows/ci.yml`의
`pytest (feast group)` job 테스트 목록을 실행합니다.

## 9. 리스크

| 리스크 | 완화 |
| --- | --- |
| **`prod_model_contract` 게이트가 조용히 사라짐** | 6-3-1 참조. 권한이 넓어지는 방향이라 CI가 못 잡고, 계약 테스트도 tmp 저장소에 옛 경로를 스스로 만들어 통과한다. 단계 1의 **같은 커밋**에서 `verifier.py`와 테스트를 함께 고친다 |
| 문자열 하드코딩 경로 누락 | 6-3에 전수 목록. 점 표기만 보면 executor의 슬래시 표기를 놓친다. 단계 1 완료 후 `grep -rn "src/\|agent_orchestration" --include=*.py --include=*.yml --include=Dockerfile*`로 잔여 확인 |
| 에이전트 이미지 빌드가 조용히 스킵됨 | `ci.yml`의 `agent_orchestration` paths 필터가 새 경로를 못 맞추면 실패가 아니라 job이 안 돈다. 단계 4에서 필터를 `applications/**`로 갱신 |
| 인접 저장소 Airflow 호출 중단 | 단계 5를 이 저장소 배포 **이후**에 수행하거나, 배포 순서를 사전에 확정 |
| `tests/` 재배치 중 테스트 유실 | 단계 3에서 재배치 전후 `pytest --collect-only -q \| wc -l` 비교 |
| 이슈 템플릿 경로 변경으로 진행 중 실험 실패 | 해당 없음 — 실험은 `base_dev_sha`로 봉인된 트리를 체크아웃하므로 이슈 본문의 명령과 트리가 항상 짝이 맞는다. 9-1 참조 |
| 단계 1의 diff가 커서 리뷰 불가 | 파일 이동(`git mv`)과 임포트 치환을 **별도 커밋**으로 분리해 rename 감지를 살림 |
| 열린 PR이 `src/`를 건드려 merge 충돌 | 9-1에서 실측 — rename 감지가 처리한다. 예외는 `#535`(사람 PR) 한 건의 일반 rebase뿐 |
| exp 브랜치가 `src/` 아래 새 파일을 추가해 `src/`가 되살아남 | 단계 1 이후 CI 가드 추가 (9-1 참조) |
| 코드 경계 검증 없이 착수 | 선행 plan의 Model Training / Feast Features 경계 보존 조건이 유효. 착수 전 관련 계약과 영향 범위 확인 필요 |
| 배치 이미지가 코드 경로를 굽고 있을 가능성 | 해당 없음 — #752 이후 `Dockerfile.app`은 소스를 COPY하지 않는다. `scripts/upload_code_archive.sh`가 **추적 파일 전체**를 `git archive`로 말아 `gcs_code_bootstrap.sh`가 `/app`에 풀고 `PYTHONPATH=/app`으로 실행하므로 경로에 무관하다. 두 스크립트 수정 불필요 |

### 9-1. 실험 에이전트와의 충돌 — 실측 결과 막지 않는다

2026-08-13 기준 열린 PR 11건 중 7건이 `src/`를 건드리며, 그중 6건이 실험
에이전트가 자동 생성한 `[AR]` PR입니다.

| PR | 브랜치 | base | 건드리는 `src/` 파일 |
| --- | --- | --- | --- |
| #739, #738, #737 | `exp/733`, `exp/734`, `exp/732` | `dev` | `models/lgbm_model.py`, `pipeline/config.yaml`, `pipeline/train.py` |
| #736, #735, #751 | `exp/731`, `exp/730`, `exp/749` | `dev` | `pipeline/config.yaml` |
| #535 | `docs/514-temporal-paired-evaluation-spec` | `main` | `pipeline/degradation_eval.py` |

이 파일들은 단계 1에서 이동하는 파일과 겹칩니다. 그래서 처음에는 착수를 막는
제약으로 판단했으나, **실측 결과 막지 않습니다.**

**실측 1 — git rename 감지가 처리한다.** 1,147행 규모 파일을 모사해 재배치 후
exp 변경을 rebase·merge 양방향으로 시도했습니다.

```
rebase: Successfully rebased and updated refs/heads/exp/999.
merge:  Auto-merging autoresearch/model_training/train.py
```

두 방향 모두 충돌 없이 새 경로 파일에 값이 반영됩니다. 파일이 작을 때는 임포트
1~2줄 변경만으로 유사도가 임계값 아래로 떨어져 `modify/delete` 충돌이 나지만,
실제 파일 크기에서는 유사도가 99%대라 감지가 성공합니다.

**실측 2 — exp diff가 단계 1이 고치는 영역과 겹치지 않는다.**

| 파일 | 단계 1이 고치는 곳 | exp PR이 고치는 곳 |
| --- | --- | --- |
| `pipeline/train.py` | 임포트 55–118행 | 884–933행 |
| `models/lgbm_model.py` | 임포트 21–22행 | 31–53행 |
| `pipeline/config.yaml` | 이동만, 내용 변경 없음 | 하이퍼파라미터 값 |

**실측 3 — 진행 중 실험은 애초에 영향받지 않는다.** executor는 DB에 봉인된
`base_dev_sha` 트리에서 `exp/*` 브랜치를 만듭니다. 그 트리에는 `src/`가 그대로
있고, 같은 시점에 발행된 이슈 본문의 `python -m src.cli build-features`와 짝이
맞습니다. `main`/`dev`가 재배치돼도 봉인된 SHA는 바뀌지 않습니다.

**실측 4 — 승격 계보 검증은 경로와 무관하다.**
`.github/workflows/auto-research-promotion.yml:192`의 유일한 계보 조건은
`candidate_sha must be an ancestor of dev`입니다.

**따라서 실험 발행을 중단할 필요가 없습니다.** 남는 조치는 두 가지뿐입니다.

1. `#535`는 사람이 올린 PR이므로 일반적인 rebase 한 번이 필요합니다.
2. exp 브랜치가 `src/` 아래 **새 파일을 추가**하면 merge가 `src/`를 되살립니다.
   실험은 기존 하이퍼파라미터만 수정하므로 실제로 일어나진 않지만, 단계 1
   이후 CI에 가드를 둡니다.

```yaml
- name: src 디렉토리가 되살아나지 않았는지 확인
  run: |
    if [ -d src ]; then
      echo "src/ 가 다시 생겼습니다 — #754 재배치 이후 이 경로는 사용하지 않습니다" >&2
      exit 1
    fi
```

`exp/*` 브랜치를 삭제하거나 force-push하지 않는다는 기존 규칙은 그대로
지킵니다(CONTRIBUTING 브랜치 보호). merge 방향이 동작하므로 rebase가 필요
없습니다.

## 10. 후속 문서

- `README.md` 저장소 구조 절, 배포 이미지 표 갱신
- `.claude/docs/agent-project-reference.md` 폴더 책임 경계 갱신
- `.claude/docs/architecture-overview.md` 경로 갱신
- `CLAUDE.md` 저장소 경계 절의 경로 표기 갱신
- `docs/specs/2026-07-15-repo-restructure.md` 결정 3에 대체 표기 추가
- `docs/plans/2026-07-15-src-package-merge.md` 재작성 또는 아카이브 이동
