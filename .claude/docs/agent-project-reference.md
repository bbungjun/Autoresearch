# Agent Project Reference

> Last Updated: 2026-07-24

폴더별 책임과 저장소·모듈 경계를 찾기 위한 문서입니다. "새 코드를 어디에
두는가?", "Y는 어느 코드·저장소에서 담당하는가?" 질문에 답합니다. 디렉토리
구조 지도와 배포 이미지 목록의 정본은 `README.md`이며 여기에 복제하지 않습니다.

## When To Use This Doc

- 새 코드를 추가할 위치를 정해야 할 때
- 코드 영역과 책임 경계를 확인해야 할 때
- 폴더 간 책임 경계(무엇을 담당하지 않는지)를 확인해야 할 때

## Docs Layout

```
docs/
├── README.md                # 문서 인덱스·수명 규칙의 정본
├── adr/                     # Architecture Decision Records (영구)
├── specs/                   # 살아있는 계약·설계 spec (유효한 동안)
├── plans/                   # 진행 중 구현 계획 (완료 시 archive로)
├── guides/                  # 운영·아키텍처 가이드 (상시 갱신)
├── runbooks/                # 운영 절차·트러블슈팅 기록 (상시 갱신)
├── reports/                 # 공유용 시각화 리포트 (HTML)
└── archive/                 # 완료·과거 문서 보존 (수정하지 않음)
```

- 새 spec/plan은 `docs/specs/`, `docs/plans/`에 `YYYY-MM-DD-<slug>.md`로
  만들고, 구현이 완료되어 더 이상 계약으로 쓰이지 않으면 `docs/archive/`로
  옮깁니다.
- 코드 디렉토리 안에 문서를 두지 않습니다(모듈 사용법은 `docs/guides/`).

## Code Areas & Responsibilities

| 도메인 | 책임 | 주요 경로 |
|---|---|---|
| **Model Training** | 모델 구조, 학습 파이프라인, 평가 지표, MLflow 연동 | `autoresearch/model_training/`, `autoresearch/model_evaluation/`, `autoresearch/model_registry/` |
| **Feast Features** | 피처 정의, 피처 엔지니어링, 피처 스토어 연동 | `feature_repo/`, `autoresearch/feature_engineering/` |
| **YouTube Collection & Release** | YouTube 수집 파이프라인·복원력 레이어·프록시, release/배포 자동화 워크플로우 | `autoresearch/data_collection/`, `applications/youtube_api_proxy/`, `.github/workflows/` |
| **Airflow Orchestration** | DAG 정의, 스케줄링, 오케스트레이션 | `SKYAHO/Autoresearch-airflow` |
| **GCP Infrastructure** | 클라우드·Kubernetes 리소스, IAM, 시크릿 기반 | `SKYAHO/Autoresearch-infra` |
| **Agent Orchestration** | FastAPI 채팅 저장 API, Codex CLI/OpenAI 호출, PostgreSQL 저장 | `applications/experiment_platform/` |
| **Reranking Serving** | 리랭킹 API | `applications/reranking_api/` |
| **Recommendation** | 정책 라운드, 일일 추천 폐루프 | `autoresearch/recommendation/` |
| **Research Harness snapshot·fixture foundation (Stage B/C)** | action log 평가 snapshot 조립·게시, Stage C fixture/candidate handoff 계약과 결정적 입력 기반 | `autoresearch/research_harness/` |

## Responsibility Boundaries

### `autoresearch/data_collection/`
- **책임:** YouTube API 수집, 변환, GCS 적재, 백필. 복원력 레이어
  (`client.py`: 재시도/Key 롤링/IP밴 시그니처/프록시)를 포함합니다.
- **패턴:** fetch → transform → load 단계를 파일로 분리합니다. 데이터
  계약은 `schema.py`의 pydantic 모델로 정의합니다.

### `autoresearch/virtual_user_generation/`
- **책임:** 페르소나 원천 데이터 로드, LLM 기반 가상 유저 생성
- **패턴:** 외부 API 호출과 오케스트레이션(`pipeline.py`)을 분리합니다.

### `autoresearch/action_log_generation/`와 `autoresearch/jobs/`
- **책임:** action log 도메인 로직과 Airflow 비종속 공개 batch 계약을
  소유합니다. `autoresearch/action_log_generation/`은 BigQuery 비의존 순수 모듈로
  유지합니다(BQ 리더는 `autoresearch/recommendation/`).
- **경계:** `jobs/`는 입력을 검증하고 도메인 모듈을 호출하지만 schedule,
  retry, timeout, Pool과 KubernetesPodOperator 설정은 소유하지 않습니다.

### `autoresearch/research_harness/` (Stage B + Stage C foundation)
- **책임:** 검증된 action log 파티션에서 source 검증 → slate identity 검증 → 다일
  click attribution → user split/구조 coverage → artifact/manifest → local publisher를
  조립합니다. Stage C foundation은 fixture/candidate handoff의 frozen typed contract,
  안전한 실패 code, canonical 날짜·user split·descriptor identity helper를 제공하고,
  production consumer schema와 호환되는 versioned virtual-user/YouTube 입력을 생성합니다.
- **공개 facade:** Stage B의 `ActionLogSource`, `EvaluationSnapshotError`,
  `EvaluationSnapshotReceipt`, `EvaluationSnapshotRequest`, `SnapshotErrorCode`,
  `build_evaluation_snapshot`과 Stage C의 `LocalEvaluationFixtureRequest`,
  `FixtureInputReceipt`, `FixturePartitionReceipt`, `FixtureDescriptor`,
  `JudgeSnapshotHandoff`, `LocalEvaluationFixtureReceipt`, `CandidateHistoryReceipt`,
  `CandidateDataManifest`, `CandidateDataViewRequest`, `CandidateDataViewReceipt`,
  `StageCError`, `StageCErrorCode`, `canonical_fixture_dates`,
  `select_fixture_user_ids`, `descriptor_sha256`를 재수출합니다. Stage B builder의 typed signature는
  `build_evaluation_snapshot(request: EvaluationSnapshotRequest, *, source: ActionLogSource | None = None) -> EvaluationSnapshotReceipt`입니다.
- **데이터 계약:** click은 같은 `(user_id, video_id)`의 엄격히 앞선 30분 안 최근
  impression에만 귀속합니다. 유저는 `user-hash-80-20-v1`의 SHA-256 고정 salt/bucket으로
  validation과 final holdout에 배타적으로 나뉩니다. 네 artifact 상대 경로는
  `validation/slate.parquet`, `validation/labels.parquet`,
  `final_holdout/slate.parquet`, `final_holdout/labels.parquet`입니다. slate는 label-free이고
  label 파일은 봉인됩니다.
- **게시 경계:** local content-addressed target은 같은 lock protocol을 따르는 cooperating
  publisher에게만 write-once 의미를 보장합니다. 완성·동일 target만 재사용하며 부분 target이나
  manifest/artifact digest 불일치는 `snapshot_write_conflict`로 거부하고 덮어쓰지 않습니다.
- **비책임:** Stage C foundation은 일일 action log producer 실행, fixture/snapshot
  write-once orchestration, canonical source adapter, Judge handoff artifact 재검증,
  candidate view 게시를 아직 수행하지 않습니다. candidate workspace 주입 검사와 Sealed
  Judge·지표·승격도 Task 3 또는 P0-2 이후의 명시적 후속 범위입니다.

### `autoresearch/`의 학습·평가 단계 패키지
- **책임:** 피처 조립(`feature_engineering/`), 모델 정의·학습·학습 데이터셋·
  provenance·스냅샷(`model_training/`), 평가·열화 측정·paired 비교·승격 근거
  (`model_evaluation/`), 일일 추천·정책 시뮬레이션·노출 provider
  (`recommendation/`), MLflow tracking/registry(`model_registry/`),
  리포트 생성·전송(`reporting/`).
- **경계:** 온라인 추론은 `applications/reranking_api/`가 담당하며 배치
  파이프라인을 import하지 않습니다. 피처 온라인 조회는 Feast(`feature_repo/`) 경유.
- **배치 근거(#754):** 폴더는 파이프라인 **단계** 축으로 나눕니다. 새 학습·평가
  코드는 그 단계 패키지에 둡니다 — 어느 단계인지 애매하면 `README.md`의 구조 절과
  `docs/specs/2026-08-13-repository-structure-redesign.md`를 먼저 봅니다.
- **학습 데이터셋 스냅샷(#530):** `autoresearch/model_training/training_snapshot_store.py`가
  content-addressed GCS 게시(`gs://<root>/by-hash/<sha>/`)·by-date 포인터
  갱신·다운로드를 소유하고, `autoresearch/model_training/training_provenance.py`가
  `TrainingSnapshotManifest`/`TrainingSnapshotPointer` 스키마를 소유합니다.
  게시는 opt-in 환경변수 `TRAINING_SNAPSHOT_ROOT`(CLI `--snapshot-root`)로만
  켜지며 **prod 재학습 경로에만** 설정해야 합니다 — 실험·dev 파이프라인이
  켜면 by-date 포인터가 경합합니다. 정본은
  `docs/specs/2026-08-04-training-dataset-snapshot-store.md`,
  사용자 안내는 `docs/guides/training-dataset.md`입니다.

### `applications/experiment_platform/`
- **책임:** 실험형 오케스트레이션 API와 비공개 Codex Runner. `/chat`의
  프롬프트 처리·PostgreSQL 영속화, API→Runner 내부 토큰 계약을 제공한다.
- **배포 경계:** `deployment/experiment_platform/api.Dockerfile`은 DB·API만,
  `runner.Dockerfile`은 Codex CLI·OAuth PVC만, `workbench.Dockerfile`은 Streamlit UI와
  Experiment API 표시 모델만 소유한다. `launcher.Dockerfile`은 DB 선점과 Kubernetes
  Job 생성만 소유한다. `executor.Dockerfile`은 Phase 2의 GitHub App token-minter,
  봉인 issue/workspace, Codex, verifier, candidate finalizer를 동일 digest에서 command
  override로 제공하며 Git·uv·`/opt/autoresearch-venv` dev/test 의존성·Node.js·고정
  `@openai/codex@0.146.0`를 포함한다. 이미지에는 repository source 전체, `.env`,
  `auth.json`, Codex 인증을 넣지 않고 issue parser `tools/`만 image copy로 봉인한다.
  KSA/GSA·PVC·Secret
  mount·RBAC·NetworkPolicy는 `SKYAHO/Autoresearch-infra` 소유이다.
- **비책임:** 사용자 OAuth, 세션/사용자 히스토리, 정책 라우팅은 후속 단계다.
- **패턴:** 파이프라인 코드(`autoresearch/`)와 패키지 경계를 분리해 배포 단위를
  `applications/` 아래 별도로 둔다.
- **Workbench 테마 정본(#657):** 색·모서리·테두리는 최상위 `.streamlit/config.toml`의
  `[theme]`이 소유하고, `applications/experiment_platform/workbench/styles.py`는 테마로 표현할 수 없는
  타이포그래피·레이아웃 CSS만 남긴다. Streamlit 1.60은 `--background-color` 같은 전역
  CSS 커스텀 속성을 노출하지 않으므로 CSS에서 `var(--*)`로 테마 값을 참조하면 그
  선언은 오류 없이 통째로 무시된다. `.streamlit/`은 `workbench.Dockerfile`이 명시적으로
  `COPY`해야 이미지에 실린다 — 그 Dockerfile은 경로를 열거해 복사한다.
- **이슈 발행 환경 변수(#516):** 가설을 `[AR]` 이슈로 발행하는 경로가 쓰는
  필수 환경 변수. 전체 기본값·형식은 `.env.example`이 정본.
  - `ORCH_GITHUB_TOKEN`: 이슈 발행 전용 `issues: write` GitHub 토큰.
  - `ORCH_GITHUB_REPOSITORY`: 발행 대상 저장소(`owner/repo`), 발행 결과 URL과
    대조해 오발행을 막음.
  - `ORCH_BASELINE_GITHUB_APP_ID`,
    `ORCH_BASELINE_GITHUB_APP_INSTALLATION_ID`: 이슈 발행 전 `heads/dev`를 한 번
    읽는 Contents read 전용 App 좌표.
  - `ORCH_BASELINE_GITHUB_APP_PRIVATE_KEY_PATH`: API Pod에 read-only mount한
    baseline reader App private key 파일 경로.
  - `ORCH_GH_TIMEOUT_SEC`: `gh` 서브프로세스 실행 상한(초).
  - `ORCH_ISSUE_DAILY_LIMIT`: 일일 발행 상한, 초과 시 429 반환.
  - `ORCH_EXPERIMENT_DATASET_SOURCE`: 서버가 Issue Form에 채우는 학습 데이터
    출처 좌표. 기간은 발행 시점에 서버가 계산해 붙이므로(`dt BETWEEN P-30
    AND P-1`, 어제까지 30일) 여기에 날짜를 넣지 않음.
  - `ORCH_EXPERIMENT_TRAINING_CONFIG_REF`: 서버가 Issue Form에 채우는 학습
    설정 참조.
- **실험 executor Job 환경 변수(#557):** release는 launcher/executor/API를 독립
  image로 게시하고 Infra는 tag가 아닌 검증된 digest를 소비한다. producer인 launcher는
  DB의 `ORCH_EXPERIMENT_ID`, `ORCH_ISSUE_NUMBER`, `ORCH_ISSUE_BRANCH`,
  `ORCH_BASE_DEV_SHA`를 exact handoff value로 Pod consumer에 전달한다.
  candidate-finalizer는 `ORCH_EXECUTOR_API_TOKEN` Secret의 file mount
  `/var/run/executor-api-token/token`을 `ORCH_EXECUTOR_API_TOKEN_FILE`로 받고,
  candidate 저장과 `RUNNING → EVALUATING`를 Candidate API에서 검증한다. 전체 기본값·경로는
  `.env.example`이 정본이다.
  - launcher 설정: `ORCH_DATABASE_URL`, `ORCH_JOB_NAMESPACE`, digest-only
    `ORCH_EXECUTOR_IMAGE`, `ORCH_EXECUTOR_SERVICE_ACCOUNT`,
    `ORCH_EXECUTOR_NODE_POOL`, `ORCH_GITHUB_APP_SECRET_NAME`,
    `ORCH_GITHUB_APP_ID`, `ORCH_GITHUB_APP_INSTALLATION_ID`,
    `ORCH_GITHUB_REPOSITORY`, `ORCH_MAX_CONCURRENT_EXPERIMENTS`,
    `ORCH_CODEX_HOME_SECRET_NAME`, `ORCH_ACTIVE_DEADLINE_SEC`,
    `ORCH_CODEX_TIMEOUT_SEC`, 선택 `ORCH_TTL_AFTER_FINISHED_SEC`. Codex Secret 이름은
    Infra가 생성·소유하며, 완료 Job TTL은 기본·최소 120초이고 장애 smoke에서만 상향한다.
    Job 전체 상한은
    60000초, 그 안의 Codex 실행 상한은 6000초다. launcher는 Codex 상한이 Job 상한 이상이면
    기동 전에 거부한다.
  - 8-container 순서: branch-token-minter → branch-creator → clone-token-minter →
    workspace-preparer → codex-worker → candidate-verifier → push-token-minter →
    candidate-finalizer. branch/clone/push token-minter만 GitHub App private key를,
    codex-worker와 candidate-finalizer만 executor 전용 Codex 인증 Secret의 read-only
    `CODEX_HOME`을, candidate-finalizer만 executor API token을 mount한다. Secret은
    `auth.json` key 하나를 제공하고 launcher는 이를 `defaultMode=0440`의 read-only
    `subPath` 파일로 mount한다. 두 container 모두 source의 regular `auth.json`만 mode
    0400으로 `/tmp` 아래 mode 0700 per-run writable scratch `CODEX_HOME`에 복사하고,
    config·plugin 등 다른 source 파일은 복사하지 않은 채 `codex exec --ephemeral`을
    실행한다.
    - **Codex는 두 번 돈다(#639).** codex-worker가 이슈를 읽고 코드를 고치고,
      candidate-finalizer가 채점이 끝난 뒤 `metrics.json`과 candidate diff를 읽어
      `report.md`를 쓴다. 리포트는 git 커밋 대상이 아니라 GCS 게시 산출물이라 push 뒤에
      와도 되고(계약 결정 5), 그래서 컨테이너 재구성 없이 finalizer 안에 들어간다.
      세션 유지(`codex exec resume`)는 MVP 범위 밖이다 — 두 번째 호출은 채점 결과와
      diff를 입력으로 받는다.
    - codex-worker는 Codex 실행 **직전에** clone 루트 `AGENTS.md`를 executor 전용 하네스
      지침으로 교체하고 `finally`로 **반드시 원본을 복원한다.** verifier가
      `git status`·`ls-files --others`로 변경을 수집하므로 복원하지 않으면 하네스 파일이
      candidate 변경으로 잡혀 commit·push된다. 지침 본문은 `executor/prompt.py`가
      소유하며 ONNX 재귀 제약(#633) 같은 실험 공간의 숨은 제약도 여기서 알린다.
    - candidate-finalizer는 push token·API token과 Codex 인증을 함께 들고 있다. 코드로
      막지 않고 하네스 지침이 담당한다는 결정이며(spec 결정 3과 같은 논리), 컨테이너를
      갈라 없애는 것은 8 → 4/5 재구성의 몫이다.
  - **단일 파드 학습(#574, 데모 스코프):** 20차 회의 지침으로 파이프라인을 파드 4개에서
    **파드 1개**로 바꿨다. 컨테이너를 새로 만들지 않고 위 8-container 중 두 곳에 학습을
    얹는다 — **baseline은 `workspace-preparer` 끝**(Codex 실행 전, dev 코드·dev 의존성),
    **candidate는 `candidate-finalizer` 끝**(push 후, candidate 코드·candidate 의존성).
    컨테이너 개수가 8 그대로라 어드미션 계약은 바뀌지 않는다.
    - **순서가 계약이다.** 뒤집히면 baseline이 candidate의 의존성 버전으로 학습돼 두
      조건의 차이가 "코드 변경"만이 아니게 되고 paired 대조의 전제가 깨진다. 재시도로
      순서가 어긋나는 경로까지 막기 위해 executor-state volume의
      `baseline_training_complete` marker로 강제하며, marker가 없으면 candidate 학습은
      **시작 자체를 거부**한다(`executor/training.py`).
    - seed 목록은 상수를 복제하지 않고 **workspace 코드에게 직접 묻는다**
      (`from autoresearch.model_evaluation.experiment_evaluation import POLICY_SEEDS`). executor 이미지에
      파이프라인 코드가 없어 import가 불가능하고, 복제하면 `issue_authoring.py`에 이어 세 번째
      사본이 된다. 조건별로 다른 값이 나오는 것이 정상이다 — candidate가 seed 정책을
      바꾸는 실험이면 candidate 학습은 바뀐 값으로 돌아야 한다.
    - `uv sync`는 `pyproject.toml`·`uv.lock`이 `base_dev_sha` 이후 바뀐 경우에만 돈다.
    - **학습 코드는 이미지가 아니라 workspace의 clone에서 온다. 이미지에 파이프라인 코드를 굽지
      말 것** — Codex가 수정한 candidate 코드가 아니라 빌드 시점의 낡은 코드로 학습하게
      되어 candidate 실험 자체가 무의미해진다.
    - **조립(`build-features`)은 이 Pod에서 하지 않는다.** feast group이 executor
      이미지에 없고 `pyproject.toml`이 feast와 dev를 `conflicts`로 선언해 재빌드로도 넣을
      수 없다. 조립은 파드 밖에서 돌려 스냅샷으로 게시하고 URI만 주입한다.
    - `ORCH_TRAINING_DATASET_URI`가 **on/off 스위치**다. 비어 있으면 학습을 건너뛰고
      기존 Phase 2 경로만 돈다. 값은 `by-hash/<sha256>/` prefix이며, 스냅샷을 읽으려면
      `experiment-job` GSA에 `roles/storage.objectViewer`가 필요하다.
    - **MLflow 좌표는 이름이 갈린다(#624).** launcher가 받는 이름은
      `ORCH_MLFLOW_TRACKING_URI`이지만 executor container에 내보내는 이름은 접두사 없는
      `MLFLOW_TRACKING_URI`다 — `autoresearch/model_training/train.py`가 표준 이름으로 읽기 때문이며,
      접두사를 붙여 내보내면 값이 전달돼도 학습은 Pod 로컬 file store에 기록한다.
      비어 있으면 아무것도 붙지 않는다(`ORCH_TRAINING_DATASET_URI`와 같은 opt-in 규약).
      이 좌표가 없으면 run이 Pod과 함께 사라져 `training_comparison.py`가
      `runs:/<run_id>/reproducibility/split/...`로 내려받을 artifact를 잃는다.
    - **산출물 게시 루트는 `ORCH_EXPERIMENT_RESULTS_ROOT`다.** `gs://bucket[/prefix]`
      형식이며 비어 있으면 게시하지 않는다(같은 opt-in 규약). executor는 그 아래
      `experiments/<이슈번호>/<experiment_id>/`로 쓴다. **Pod의 `/workspace`는
      emptyDir이라 TTL 후 사라지므로, 비워 두면 측정한 것이 아무것도 남지 않는다.**
      대상 버킷의 `experiment-job` GSA 권한은 `objectCreator`+`objectViewer`이며
      `objectCreator`는 **기존 객체 교체를 허용하지 않는다** — 게시된 결과는 같은
      Pod에서 도는 에이전트도 덮어쓸 수 없다.
    - **다운로드는 workspace 코드가, 검증은 executor 이미지가 한다(#605).** 받은 CSV의
      SHA-256을 URI에 박힌 값과 대조한다. 다운로드 경로(`autoresearch/**`)는 Codex의 허용
      범위라 candidate가 바꿀 수 있는데, 학습과 달리 **데이터 조달은 두 조건이 같아야**
      paired 대조가 성립한다. 검증만 이미지에 봉인해 우회를 막는다. 받아둔 파일은
      candidate 단계와 Job 재시도가 재사용한다.
  - **로그 수집기는 executor 밖에 있다(#559).** 상주 Deployment가 `pods/log`로 executor
    Pod의 컨테이너 로그를 읽어 `experiment_logs`에 적재한다. executor 컨테이너는 한 줄도
    건드리지 않으므로 credential 경계가 유지된다 — `codex-worker`에 API 토큰을 주는 방식은
    Codex가 `--sandbox danger-full-access`로 도는 컨테이너에 쓰기 자격증명을 놓는 것이라
    기각했다. HTTP API가 아니라 `create_experiment_log`를 직접 부르므로 API 토큰·egress도
    필요 없다(launcher 이미지가 `app` 패키지를 포함한다). 수집 대상은 K8s Job 목록에서
    얻는다 — DB의 `RUNNING`으로 거르면 `EVALUATING` 전환 뒤에도 같은 Job이 계속 도는
    구간을 놓친다. 정본: `docs/specs/2026-08-09-experiment-log-collector.md`
  - **PR 생성도 executor 밖에 있다(#689).** 상주 프로세스가 `PASSED` 실험을 훑어 `exp`
    브랜치를 `dev`로 향하는 PR로 연다. 같은 이유다 — `candidate-finalizer`에는 이미 push
    token과 API token이 있고 Codex가 그 안에서 도는데, 거기에 `Pull requests: write`까지
    얹지 않는다. token은 그 권한 하나만 발급받아 이 프로세스가 코드를 push할 수 없다.
    **지표로 거르지 않는다** — `PASSED`는 "가설이 맞았다"가 아니라 "완주했다"이고
    (`2026-08-09-agent-authored-experiment-report.md` §결정 6), 여기서 결과로 걸러내면
    승격 관문에서 제거한 통계 게이트를 되살리는 것이 된다. 머지와 `PROMOTED`는 사람이
    한다. 관측 대상은 수집기와 반대로 **DB**다 — 찾는 것이 살아 있는 프로세스가 아니라
    확정된 상태이고, Job은 TTL로 사라져 K8s에는 그 사실이 없다. 정본:
    `docs/specs/2026-08-11-passed-experiment-pull-request.md`
  - executor 봉인 좌표: launcher가 `ORCH_EXPERIMENT_ID`, `ORCH_ISSUE_NUMBER`,
    `ORCH_ISSUE_BRANCH`, `ORCH_BASE_DEV_SHA`를 DB에서 복사해 Pod에 주입한다.
    workspace-preparer는 GitHub의 현재 이슈 본문을 raw 입력으로 읽고 해당 branch를
    checkout한 뒤 HEAD와 원격 tip의 일치만 검증한다.
  - token 파일 좌표: token-minter에만 `ORCH_GITHUB_APP_PRIVATE_KEY_FILE`을
    주입하고, 각 minter와 단일 consumer는 memory volume의 purpose별
    `ORCH_GITHUB_TOKEN_FILE`(`/var/run/{branch,clone,push}-token/token`)만 공유한다.
  - Codex worker·verifier에는 GitHub/API token을 mount하지 않는다. 모든 container는
    non-root UID/GID 10001, seccomp, capability drop, `automountServiceAccountToken=false`,
    workspace/token volume size limit을 준수해야 하며, 실제 Secret/PVC/resource/
    NetworkPolicy 이름과 값은 Infra가 정한다.
  - `auto-experiment`는 이슈 분류와 promotion guard일 뿐 branch 생성 트리거가
    아니다. Phase 1 executor는 기존 GitHub Actions bot marker를 쓰지 않으므로 새
    marker 없는 branch는 promotion 입력이 아니며, marker 재설계가 다음 단계 gate다.

### 외부 오케스트레이션 경계
- DAG와 Airflow 배포는 `Autoresearch-airflow`에만 둡니다.
- Airflow는 배포 이미지의 immutable digest와 `autoresearch.jobs.*` 공개
  명령만 소비하며 내부 Python API를 직접 import하지 않습니다.
- 공개 batch 명령·인자 계약:
  `docs/specs/2026-07-13-public-batch-execution-contract.md`
- 모델 승격 판정과 `model-promotion-result-v1` schema는 이 저장소가
  소유합니다. Airflow는 `--result-path` 파일을 XCom으로 운반하고 알림으로
  렌더링하지만 `autoresearch.model_registry` 내부 API를 import하거나 outcome을 다시
  판정하지 않습니다. 결과 정본:
  `docs/specs/2026-07-29-model-promotion-structured-outcome.md`
- paired offline 실험(#454)의 비교·판정도 이 저장소가 소유합니다. Airflow는
  조건별(baseline|candidate) 학습 Job을 실행하고
  `python -m autoresearch.cli compare-paired-experiment`의 결과 파일을 운반할 뿐,
  `comparison_passed`/`comparison_rejected`/`comparison_failed` 판정을 다시
  계산하지 않습니다. 실행 좌표는 `autoresearch/model_evaluation/experiments/context.py`가,
  결과 계약은 `autoresearch/model_evaluation/paired_experiment.py`가 소유하며 정본은
  `docs/specs/2026-08-03-paired-offline-experiment-comparison.md`입니다.
- 학습 데이터셋 스냅샷 재사용(#530)의 CLI 인자 이름(`--snapshot-root`,
  `--dataset-uri`, `--min-coverage-days`)은 `Autoresearch-airflow#236` 배선이
  참조하도록 확정됐습니다. `build-features`/`train-model`/`run-pipeline`
  자체는 아직 `공개 batch 실행 계약`의 v1 명령 목록에 없으며, 인자 상세와
  상호배타 규칙의 정본은 `docs/specs/2026-08-04-training-dataset-snapshot-store.md`
  입니다.

### `tests/`
- **책임:** 모듈별 단위 테스트. **소스 구조를 그대로 미러링**합니다(#754) —
  `autoresearch/model_training/train.py`의 테스트는
  `tests/model_training/test_train.py`입니다. 새 모듈에는 대응하는 테스트 파일을
  같은 자리에 만듭니다.
- 여러 패키지에 걸치거나 저장소 자체(워크플로·릴리스 계약)를 검사하는 테스트는
  `tests/` 루트에 둡니다. 무리해서 나누지 않습니다.
- 저장소 루트를 `Path(__file__).resolve().parents[N]`으로 찾을 때 `N`은 파일 깊이에
  따라 다릅니다 — 옮길 때 함께 고쳐야 합니다(통합 과제 #760).
- feast 계열 테스트는 dev 환경에서 `pytest.importorskip("feast")`로 skip되고
  CI `pytest (feast group)` job이 별도 실행합니다.

## Technical Stack

- **언어:** Python 3.12 (`.python-version`), CI는 3.11/3.12 매트릭스
- **의존성:** uv + `pyproject.toml`/`uv.lock`(단일 출처).
  `proxy/requirements.txt`는 `uv export` 전핀 산출물,
  `deployment/mlflow/runtime`은 자체 lock — CI가 drift를 검사합니다.
- **주요 라이브러리:** pydantic v2, pyarrow, pandas, DuckDB, LightGBM,
  scikit-learn, typer, mlflow-skinny, google-cloud-bigquery/storage/aiplatform,
  openai(OpenRouter 호출)
- **데이터 저장:** GCS 데이터 레이크(parquet), BigQuery(피처·학습 데이터셋
  운영 중)
- **피처 스토어:** Feast 0.64 (`feature_repo/`, BigQuery offline / Redis
  online) — dev와 의존성 충돌로 격리 그룹(`uv sync --only-group feast`).
  prod/dev 환경은 `AUTORESEARCH_ENV`(기본 prod)로 선택한다. dev는 오프라인 전용
  (apply + BigQuery PIT)이라 registry(`GCS_REGISTRY_PATH`)·offline(`BQ_DATASET`)만
  분리하고 online 서빙·materialize는 prod만 한다. dev apply는
  `full_scan_for_deletion=false`(`feature_repo/env.py`)로 Redis에 접속하지 않는다
  (#399, `docs/specs/2026-07-29-feature-store-prod-dev-environment.md`)
- **모델·추적:** LightGBM + MLflow (Tracking Server는 `deployment/mlflow/`)
- **서빙:** FastAPI (`applications/reranking_api/`), GKE 배포(`deployment/serving/`)
- **오케스트레이션:** 외부 `Autoresearch-airflow`가 배포 이미지의 공개
  CLI를 KubernetesPodOperator로 실행

## Key Extension Rules

1. **저장소 책임 경계 확인:** 애플리케이션·ML은 이 저장소, Airflow와 GCP
   인프라는 각각 전용 저장소에서 변경합니다.
2. **올바른 위치에 배치:** 위 책임 경계를 따르고 도메인 간 결합을
   피합니다.
3. **데이터 계약 갱신:** 스키마가 바뀌면 해당 모듈의 `schema.py`
   pydantic 모델과 테스트를 함께 수정합니다.
4. **테스트 작성:** `tests/test_<module>.py`에 단위 테스트를 추가합니다.
5. **설계 결정 기록:** 아키텍처에 영향이 있으면 `docs/specs/`에 spec을
   남기거나 관련 `.claude/docs/` 가이드를 갱신합니다.
6. **구조 사실 갱신:** 새 최상위 디렉토리·`Dockerfile.*`·공개 CLI·필수 환경
   변수를 도입하면 같은 PR에서 `README.md`와 이 문서를 갱신합니다.

## Verification Checklist

- [ ] 코드가 영역별 책임에 맞는 폴더에 있다.
- [ ] 공개 CLI에 schedule·retry·KPO 같은 Airflow 정책이 들어가지 않았다.
- [ ] 스키마 변경 시 pydantic 모델과 테스트를 함께 수정했다.
- [ ] 새 기능에 테스트가 있다.
- [ ] 동작·설정이 바뀌었으면 문서를 갱신했다(구조 사실은 README와 이 문서).
