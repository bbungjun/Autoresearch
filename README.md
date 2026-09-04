# Autoresearch

YouTube 트렌딩 데이터 기반 CTR(Click-Through Rate) 모델링 프로젝트입니다.
YouTube 트렌딩 영상을 수집하고, LLM으로 가상 유저와 action log를 생성해
CTR 모델을 학습·서빙하며, 모델 노출 결과가 다시 학습 데이터로 돌아오는
일일 폐루프를 구현합니다.

## 현재 저장소 운영 상태

- 기준 저장소는 개인 저장소
  [`bbungjun/Autoresearch`](https://github.com/bbungjun/Autoresearch)입니다.
- 이 저장소는 이전 조직 저장소 `SKYAHO/Autoresearch`에서 개인 저장소로
  이전되었습니다. 문서와 코드에 남아 있는 `SKYAHO/*` 표기는 이전 조직 환경의
  아키텍처 계보 또는 복구 참고 자료일 수 있습니다.
- 현재 활성 GitHub Actions는 CI(`ci.yml`), Ruff(`lint.yml`), Release Drafter
  (`release-drafter.yml`)입니다. 조직 Secret, GitHub App, GCP 자원과 인접
  저장소 쓰기 권한을 요구하는 워크플로우는 `.github/workflows-disabled/`에
  보관되어 있으며 현재 동작하지 않습니다.
- 팀원 Approve와 조직 Project 자동화는 현재 머지 조건이 아닙니다. 독립적인
  에이전트 리뷰와 로컬·CI 검증 결과를 사람이 확인해 최종 반영 여부를 결정합니다.
- 현재 기여·브랜치·PR 절차는 [`CONTRIBUTING.md`](CONTRIBUTING.md)가 정본입니다.

## 비전

Autoresearch의 최종 목표는 ML 리서처·엔지니어를 위한 **자율 실험 에이전트
서비스**입니다. 사용자가 가설 한 줄(예: 추천 알고리즘 논문)을 입력하면,
에이전트가 raw 데이터로 피처를 재조립·가공하고, 모델·임베딩 방식을 선택해
학습한 뒤, origin(champion) 모델과의 비교·A/B 테스트까지 스스로 판단해
수행합니다. 구현된 일일 폐루프는 이 에이전트가 실험을 돌리기 위한
기반 테스트베드입니다. 사람이 준비한 가설·데이터·예산으로 동작하는 **로컬 Research
Harness MVP는 완료했습니다.** 사전 주입 오류 한 건의 실제 agent 수정·재평가와
`mean_topic_similarity` 피처의 실제 22열 학습·offline final promote까지 실증했습니다.
근거는 고정 합성 fixture에 한정하며 실제 사용자 품질·범용 무인 실행·비용 절감을
뜻하지 않습니다. 논문 자동 발견·가설 변환·웹 제품 연결은 MVP 이후 로드맵입니다.
최신 상태와 근거는 [MVP 완료 조건](docs/specs/2026-08-14-paper-grounded-autonomous-ml-research-harness.md#12-mvp-완료-조건)과
[실측·포트폴리오 기록](docs/reports/2026-09-03-local-autonomous-experiment-e2e.md)을 따릅니다.

전체 파이프라인 (일일 폐루프):

```
YouTube 수집 → 가상 유저 생성 → action log 생성 → CTR 학습 데이터셋 → 모델 학습/평가
                    ↑                                                        ↓
            노출·클릭 시뮬레이션 ← 일일 추천 ← 리랭킹 서빙 API ← 모델 배포
```

위 그림은 논리적 폐루프입니다. GKE·Airflow·GCP를 포함한 이전 조직 배포 배선은
현재 개인 저장소에서 비활성입니다.

## 저장소 구조

폴더는 **파이프라인 단계** 축으로 나눕니다(#754). 최상위 이름만 보고 그 안에 무엇이
있는지 예상할 수 있어야 하고, 배포되는 서비스는 파이프라인 코드와 섞이지 않습니다.

```
autoresearch/        # 폐루프 파이프라인 — 단계마다 한 패키지
├── cli.py                # 학습·평가·승격 typer 진입점
├── jobs/                 # Airflow 비종속 공개 batch CLI
├── data_collection/      # YouTube 트렌딩 수집 (fetch/transform/load/backfill + 복원력 레이어)
├── virtual_user_generation/  # LLM 기반 가상 유저(페르소나) 생성 + 파이프라인 어댑터
├── action_log_generation/    # action log 생성·shard·merge·품질 계약
├── research_harness/  # 로컬 자율 실험: snapshot·Sealed Judge·재학습·Controller·ledger·REPORT
├── feature_engineering/  # 피처 조립·임베딩·Feast 조회
├── model_training/       # 모델 정의, 학습, 학습 데이터셋, provenance, 스냅샷
├── model_evaluation/     # 평가, 열화 측정, paired 비교, seed sweep, 승격 근거
├── recommendation/       # 일일 추천, 정책 라운드 시뮬, 노출 provider, 리랭킹 클라이언트
├── model_registry/       # MLflow tracking·registry·승격
└── reporting/            # HTML 리포트, 실험 결과 리포트 전송
applications/        # 배포되는 서비스 — 파이프라인을 소비하지만 그 일부가 아니다
├── reranking_api/        # FastAPI 리랭킹 추론 서버 + k6 부하 테스트
├── experiment_platform/  # 실험 에이전트 (api/workbench/runner/launcher/executor/shared)
└── youtube_api_proxy/    # Cloud Run dumb forwarder (YouTube API IP밴 대응)
deployment/          # 배포 산출물 (Dockerfile.*, mlflow/ Tracking Server,
                     #             serving/ 추론 이미지, experiment_platform/ 역할별
                     #             runtime 이미지, feast/ apply GKE Job 매니페스트)
feature_repo/        # Feast 피처 스토어 정의 (BigQuery offline / Redis online)
examples/            # CTR 파이프라인 예제 스캐폴드
scripts/             # 검증·일회성 스크립트
tests/               # 소스 구조를 그대로 미러링 (tests/model_training/ …)
docs/                # 문서 — docs/README.md 인덱스 참조
.streamlit/          # Streamlit Experiment Workbench 테마 정본 (config.toml)
```

`feature_repo/`는 Feast가 요구하는 규격이라 최상위에 그대로 둡니다. 파이프라인 단계
축으로 자르면 마지막 단계가 첫 단계를 참조하게 되는데(`recommendation` →
`action_log_generation`), 폐루프 구조상 자연스러운 방향이며 import 순환은 아닙니다.

### Research Harness 평가 snapshot (Stage B)

`autoresearch/research_harness/`는 검증된 action log 일일 파티션에서 재현 가능한
평가 snapshot을 조립하는 파이프라인 경계입니다. Stage B의 주요 Python API는
`ActionLogSource`, `EvaluationSnapshotError`, `EvaluationSnapshotReceipt`,
`EvaluationSnapshotRequest`, `SnapshotErrorCode`,
`build_evaluation_snapshot` 여섯 항목입니다. 마지막 함수의 계약은
`build_evaluation_snapshot(request: EvaluationSnapshotRequest, *, source: ActionLogSource | None = None) -> EvaluationSnapshotReceipt`입니다.

평가 출력은 `validation/slate.parquet`, `validation/labels.parquet`,
`final_holdout/slate.parquet`, `final_holdout/labels.parquet`의 네 artifact와
`manifest.json`으로 구성합니다. slate에는 label을 넣지 않으며 labels와 final holdout은
후속 Judge 경계의 입력입니다. click은 같은 `(user_id, video_id)`에서 직전 30분 안의
전역 최근 impression 한 건에만 귀속하고, 유저는 고정 SHA-256 bucket의 80/20
validation/final holdout split으로 나눕니다. local publisher는 같은 lock protocol을 따르는
cooperating publisher에 한해 동일한 완성 target을 재사용하고, 불완전하거나 digest가 다른
target은 덮어쓰지 않고 실패합니다.

이후 RuleBased fixture, candidate workspace, Sealed Judge, ledger·Controller,
metadata v2, 로컬 피처/임베딩과 seed별 재학습 CLI를 구현했습니다. final용 metadata·workspace는
별도 interface에서 기존 소비 grant 검증 후 전달합니다. 실제 agent 실행과 종료 REPORT는
아래 로컬 실행 경로에서 연결하며, 5-seed calibration·실측 완주는 Task 7 범위입니다. snapshot 계약 정본은
[`Research Harness P0-1 평가 snapshot`](docs/specs/2026-08-31-research-harness-evaluation-snapshot.md)입니다.

### Research Harness 로컬 임베딩 준비

`research_harness.local_embedding`은 준비된 모델을 사용하는 `TextEmbedder` adapter입니다.
기본 dev·배포 환경에는 GPU 의존성을 넣지 않으며 `local-embedding` 선택 그룹을 사용합니다.
Windows RTX 3070 Ti baseline은 CUDA 12.8 PyTorch wheel이며 시스템 드라이버를 바꾸지 않습니다.
다른 기존 가상환경을 유지하려면 별도 worktree의 `.venv`에서 아래 명령을 실행하십시오.

```bash
uv sync --locked --no-default-groups --group local-embedding
uv run --no-sync python -m scripts.research_harness.embedding_smoke --model-dir artifacts/models/multilingual-e5-small --cache-dir artifacts/embedding-cache-smoke --out artifacts/embedding-smoke.json --download
```

`--download`가 있을 때만 공개 고정 revision 모델을 준비합니다. 이후에는 같은 명령에서
이 옵션을 빼면 로컬 파일만 사용합니다. JSON에는 모델 identity·파일 hash·라이브러리 버전,
GPU 장치·할당 peak·처리 시간·cache hit 검증을 남깁니다. 새 캐시 디렉터리에서 실행해야
첫 추론과 재사용 시간을 구분할 수 있습니다. 생성 모델/캐시/JSON은 커밋하지 않습니다.
이 smoke는 모델 품질 실험이나 전체 agent loop의 완료 증거가 아닙니다. 설정·캐시·오류
계약과 실제 검증 결과는 [Harness spec §4.7](docs/specs/2026-08-14-paper-grounded-autonomous-ml-research-harness.md)와
[구현 plan](docs/archive/plans/2026-08-15-local-research-harness-mvp.md)을 따릅니다.

### 로컬 seed별 재학습

준비된 candidate v2 view(`candidate-view.json`, slate/history/metadata)에서만 학습합니다.
`harness_config.json`을 로컬에 작성합니다. 아래 모델·캐시 경로는 **설정 파일 디렉터리
기준**이며, 모델은 앞 단계에서 미리 준비해야 합니다. 로컬 설정 파일은 커밋하지 않습니다.

```json
{
  "embedding": {
    "model_id": "intfloat/multilingual-e5-small",
    "revision": "614241f622f53c4eeff9890bdc4f31cfecc418b3",
    "model_dir": "artifacts/models/multilingual-e5-small",
    "cache_dir": "artifacts/embedding-cache",
    "device": "cuda",
    "batch_size": 8
  }
}
```

```bash
uv run --no-sync python -m autoresearch.cli harness-predict --slate <candidate-view>/slate.parquet --out artifacts/trial-42/predictions.csv --seed 42 --config harness_config.json
```

`--seed`는 필수입니다. 매 호출 60/20/20 stratified split과 새로운 CPU LightGBM fit을
수행하며, 동일 입력의 사전학습 임베딩만 캐시에서 재사용합니다. 완전 라벨은 평가 시작일
`T-2`까지이며 `T-1` 로그는 자정 click 귀속 완결에 씁니다. `predictions.csv`,
`predictions.model.txt`, `predictions.training.json`을 생성하고 기존 출력은 덮어쓰지 않습니다.
일부 게시 실패 후에는 새 출력 경로로 재시도합니다. receipt의 입력·split·모델·embedding
identity와 진단·시간은 재현 근거이며, native 모델을 별도로 사용할 때는 receipt의 sampling
실현값에 따른 확률 보정도 적용해야 합니다. 이 명령은 모델 다운로드, MLflow 등록,
평가 정답 조회 또는 승격 판정을 하지 않습니다. 자세한 계약은
[Harness spec §4.8](docs/specs/2026-08-14-paper-grounded-autonomous-ml-research-harness.md)을 따릅니다.

### 실제 자율 실험 실행과 재개

`python -m autoresearch.cli harness-run --config <local-run.json>`은 준비된 fixture와
모델, 기존 Codex CLI 로그인을 사용합니다. `local_runtime.HarnessRunConfig`가 설정 정본이며
repository/workspace/run 절대 경로, Judge handoff와 fixture descriptor digest,
baseline/champion SHA, 초기 card, budget, screening seed와 서로 다른 confirmation seed
5개, 실측 baseline sigma, prediction·agent 설정을 요구합니다. 최대 두 validation trial로
한 번의 feedback revision을 허용합니다. sigma는 이 명령이 임의로 채우지 않습니다.

모델/캐시·run root·별도 workspace parent를 먼저 준비하고 fixture의 소비 registry도
기존 계약에 따라 준비해야 합니다. 이 명령은 registry를 생성하거나 초기화하지 않습니다.
Codex 모델과 reasoning effort는 명시하며 새 API key나 유료 클라우드 자원을 요구하지 않습니다.
agent 호출은 개인 config를 읽지 않고 승인 정책 `never`와 요청 sandbox 범위를 명시합니다.
native Windows는 기존에 설치된 `elevated` sandbox를 선택하며 전체 접근으로 우회하지 않습니다.
Windows coding prepare에서는 검증된 candidate 입력에 한정한 추가 읽기 권한과
등록된 `harness_out/.agent-tmp`의 creator-side 회수를 구현했습니다
([#54](https://github.com/bbungjun/Autoresearch/issues/54), PR #65·#66).
후보 commit·patch·기록을 먼저 보존하고 같은 sandbox 주체의 helper 회수, host의 비어 있음
검사와 기존 worktree 회수까지 성공해야 후보를 반환합니다. 등록 경로 밖의 private 산출물과
host 강제 종료까지 자동 회수하지 않으며 기존 실패 폴더의 소유권·권한도 바꾸지 않습니다.
agent의 코드·테스트 실행과 Harness의 공식 재학습·수치 판정은 계속 분리합니다.
run 설정과 산출물은 Judge-owned 로컬 파일이며 저장소에 커밋하거나 candidate에 주지 않습니다.

같은 명령은 `run-inputs`의 고정 metadata·설정·모델 파일·trusted Harness 코드와 ledger를
대조해 재개합니다. 입력이 달라지면 실패하며 완료 trial을 재실행하지 않습니다. 중단된
validation attempt는 재시작할 수 있어 LLM 호출 exactly-once는 보장하지 않습니다.
최종 결과는 `controller-result.json`, 상세 증거는 ledger와 `attempts/`에 남습니다.
결과는 `controller-result-binding.json`으로 입력·ledger와 연결합니다. 결속된 종료 결과가
있으면 같은 명령을 다시 실행해도 Controller·학습·final claim 없이 REPORT만 복구합니다.
입력이나 기록이 달라졌거나 결속 파일이 사라졌다면 재실행으로 덮지 않고 실패합니다.

종료 시 `research-record.json`, `research-judge.json`, `research-report.md`와
`research-report-manifest.json`을 게시합니다. 보고서는 실제 변경·agent 주장·관측 지표를
구분하고 대표 수치는 final holdout의 완전한 비교를 사용합니다. validation champion을
최종 채택 모델로 간주하지 않으며 final 실패를 validation 최고값으로 대체하지 않습니다.
이전 대화와 candidate 코드가 없는 새 read-only Judge가 구조화 기록을 한 번 검토하지만,
의견은 advisory이며 수치 판정·champion·feedback을 바꾸지 않습니다. 호출 intent 이후
실패는 자동 재호출하지 않고 검토 unavailable로 기록합니다. 시간·token은 관측 coverage와
함께 표시하고, 달러 비용·사람 개입 횟수는 측정되지 않았다면 null입니다.

final은 기존 단일 소비 계약을 유지합니다. 별도 baseline 5-seed calibration과 실제
2-trial feedback·checkpoint 재개·final·REPORT·새 문맥 Judge 검토를 실측했습니다.
해당 합성 실험에서는 validation 승격 후 final의 사전 채택 기준에 미달해 baseline을
유지했습니다. 시간·토큰은 관측했지만 달러 비용·사람 개입 횟수는 미측정입니다.
별도 #69 실행에서는 #62/#54 변경을 반영해 사전 주입 오류 → 실제 agent 수정 한 번 →
재학습·final·REPORT·새 문맥 Judge까지 완주했습니다. Recall@10의 σ=0을 유지했으므로
최종 품질 결론은 `inconclusive / insufficient_baseline_noise`이며 baseline을 유지했습니다.
실행 구간의 중간 개입 0은 운영자 수동 관측이지 자동 계측값이 아닙니다. 구조화 기록
Judge는 원본 편집 과정의 가시성 한계를 지적했습니다. 일반적인 복구 능력이나 품질
개선을 입증한 것은 아닙니다. 후속 #71 seed 7104에서는 `mean_topic_similarity`를 실제
22번째 피처로 학습했고 final NDCG@10이 0.8325499381에서 0.8821625508로 올라
`promote / promotion_threshold_met`을 받았습니다. ADR 0003은 #60의 근소한 기각,
#69의 σ=0 판정 불가와 이 승격을 근거로 기존 판정 정책과 합성 offline·시간·token·수동
관찰의 MVP 증거 경계를 수용했습니다. 달러 비용과 자동 사람 개입 값은 계속 `null`이며
확대 실증은 후속 범위입니다.
자세한 계약은 [Harness spec §4.9·§10.1.1](docs/specs/2026-08-14-paper-grounded-autonomous-ml-research-harness.md)를 따릅니다.

사전 수동 측정 도구는 `python -m scripts.research_harness.calibrate_baseline --help`와
`python -m scripts.research_harness.benchmark_parser --help`로 확인합니다. 새 절대 출력
디렉터리만 허용하며 원본 결과를 덮어쓰거나 자동 재학습하지 않습니다. 전자는 고정 baseline의
validation 5회 새 학습, 후자는 합성 parser 자원 측정이며 agent/final 실행 도구가 아닙니다.
calibration의 선택 `--baseline-sha`는 full commit SHA를 받아 저장소와 대조하며, 생략하면
기존 기본 baseline을 사용합니다. 고정 seed와 출력 재사용 금지는 그대로 유지합니다.
실제 입력 준비와 결과·한계는 [Task 7 기록](docs/archive/plans/2026-08-15-local-research-harness-mvp.md#첫-실측-pr--57)을 참조합니다.

실행 전후 증거 관측은 `python -m scripts.research_harness.measure_e2e --help`로 확인합니다.
기존 run 설정과 별도의 새 측정 출력이 필요하며, 선택한 첫 validation checkpoint 직후의
주입 중단·동일 설정 재개·종료 재호출을 구분해 기록합니다. registry를 초기화하거나 실패한
호출을 자동으로 재시도하지 않습니다. 정상 runtime 반환은 모델 개선을 의미하지 않습니다. 실측 계약과
현재 진행 상태는 [spec §4.11](docs/specs/2026-08-14-paper-grounded-autonomous-ml-research-harness.md#411-실제-controller-e2e-측정-60)을 따릅니다.
실제 두 trial의 피드백 수정·중단 복구·단일 final·종료 재호출 결과와 한계는
[실측·포트폴리오 기록](docs/reports/2026-09-03-local-autonomous-experiment-e2e.md)에 정리했습니다.

## 배포 이미지

| 이미지 | 용도 |
|---|---|
| `deployment/Dockerfile.app` | 공개 batch CLI 실행 (이전 Airflow KPO가 소비하도록 설계된 canonical application image) |
| `deployment/Dockerfile.train` | feast 불필요 학습 서브커맨드 — `promote-model`(alias 승격), `train-model`/`evaluate-model`/`sweep-seeds`(다중 시드 반복 학습·유의성 판정 근거, #407), `compare-paired-experiment`(baseline/candidate paired 비교·판정, #454), `measure-degradation`(단일 cutoff 기반 모델 열화 시점 측정, #471/#485). `train-model --dataset-uri`(게시된 학습 데이터셋 스냅샷 재사용, #530)는 GCS 다운로드만 필요해 이 이미지로 실행 가능하다. GCS code archive 부트스트랩, MLflow 연동 |
| `deployment/Dockerfile.feast` | Feast apply/materialize + feast 필요 학습 조립 — `build-features`/`run-pipeline`이 offline PIT로 피처를 조립하므로(#359 C2) 이 이미지로 실행. `--snapshot-root`(또는 `TRAINING_SNAPSHOT_ROOT`)로 조립 결과를 GCS에 content-addressed 게시할 수 있다(#530, `docs/guides/training-dataset.md`) |
| `deployment/serving/Dockerfile` | 리랭킹 서빙 API (GKE) |
| `deployment/mlflow/Dockerfile` | MLflow Tracking Server |
| `deployment/experiment_platform/api.Dockerfile` | Agent Orchestration FastAPI·PostgreSQL 저장 API (GKE 내부) |
| `deployment/experiment_platform/runner.Dockerfile` | API 전용 Codex Runner (GKE 내부, OAuth PVC 분리) |
| `deployment/experiment_platform/workbench.Dockerfile` | Streamlit Experiment Workbench (GKE 내부, API 토큰 서버 환경 주입) |
| `deployment/experiment_platform/launcher.Dockerfile` | 봉인 좌표를 선점해 branch-bootstrap Kubernetes Job을 생성하는 1회 launcher runtime (CronJob용) |
| `deployment/experiment_platform/executor.Dockerfile` | Phase 2 GitHub App token-minter, 봉인 issue/workspace, Codex, verifier, candidate finalizer를 같은 digest로 실행하는 executor runtime |

이전 조직 환경의 `release.yml`은 launcher와 executor를 각각
`autoresearch-agent-orchestration-launcher`,
`autoresearch-agent-orchestration-executor`로 build/push합니다. 배포 인프라는 tag가
아니라 release가 검증한 `@sha256:<64자리 digest>`를 소비하도록 설계되었습니다.
현재 이 워크플로우는 `.github/workflows-disabled/release.yml`에 보관되어 있으며
개인 저장소에서는 비활성입니다.

아키텍처상 DAG·스케줄·Airflow 배포는
[`SKYAHO/Autoresearch-airflow`](https://github.com/SKYAHO/Autoresearch-airflow),
GCP 인프라는
[`SKYAHO/Autoresearch-infra`](https://github.com/SKYAHO/Autoresearch-infra)가
담당했습니다. 현재 개인 저장소에서는 두 조직 저장소의 접근 권한이나 자동 연동을
전제하지 않으며, 관련 표기는 책임 경계와 과거 배포 계약을 이해하기 위한
참조입니다.

### Agent Orchestration 이슈 발행 환경 변수 (#516, 현재 조직 연동 비활성)

가설을 `[AR]` Auto Research 이슈로 발행하는 경로가 쓰는 필수 환경 변수입니다
(전체 기본값·형식은 `.env.example`이 정본).
아래 계약은 이전 조직 배포를 복구하거나 개인 저장소용 연동을 새로 구성할 때의
참조이며, 현재 활성 GitHub Actions만으로는 실행되지 않습니다.

| 변수 | 용도 |
|---|---|
| `ORCH_GITHUB_TOKEN` | 이슈 발행 전용 `issues: write` GitHub 토큰 |
| `ORCH_GITHUB_REPOSITORY` | 발행 대상 저장소(`owner/repo`), 발행 결과 URL과 대조해 오발행을 막음 |
| `ORCH_BASELINE_GITHUB_APP_ID` | 이슈 발행 전에 `heads/dev`를 읽는 Contents read 전용 GitHub App ID |
| `ORCH_BASELINE_GITHUB_APP_INSTALLATION_ID` | baseline reader App installation ID |
| `ORCH_BASELINE_GITHUB_APP_PRIVATE_KEY_PATH` | API Pod에 read-only mount한 baseline reader private key 파일 경로 |
| `ORCH_GH_TIMEOUT_SEC` | `gh` 서브프로세스 실행 상한(초) |
| `ORCH_ISSUE_DAILY_LIMIT` | 일일 발행 상한, 초과 시 429 반환 |
| `ORCH_EXPERIMENT_DATASET_SOURCE` | 서버가 Issue Form에 채우는 학습 데이터 출처 좌표. 기간은 발행 시점에 서버가 계산해 붙임(`dt BETWEEN P-30 AND P-1`, 어제까지 30일) |
| `ORCH_EXPERIMENT_TRAINING_CONFIG_REF` | 서버가 Issue Form에 채우는 학습 설정 참조 |

### Agent Orchestration 실험 executor Job handoff (#557, 현재 조직 연동 비활성)

이전 release 계약은 launcher/executor/API를 각각 `@sha256:<64자리 digest>`로
게시하고, Infra가 그 digest만 배포 입력으로 사용하도록 설계되었습니다. launcher는
DB에서 `CREATED` Experiment를 선점해
`RUNNING`으로 전이한 뒤, 아래 정확한 값과 volume 경로를 executor Job에 전달합니다.
값·기본값의 단일 출처는 `.env.example`입니다.
이 절은 이전 조직 환경의 배포 계약을 보존한 것으로, 개인 저장소의 현재 실행 상태를
뜻하지 않습니다.

| 역할 | 변수 | 용도 |
|---|---|---|
| launcher | `ORCH_DATABASE_URL` | Experiment 선점·생성 확인을 기록할 PostgreSQL 연결 |
| launcher | `ORCH_JOB_NAMESPACE` | executor Job 생성 namespace |
| launcher | `ORCH_EXECUTOR_IMAGE` | release가 게시한 executor `@sha256:` digest reference |
| launcher | `ORCH_EXECUTOR_SERVICE_ACCOUNT` | Kubernetes API 권한이 없는 executor KSA |
| launcher | `ORCH_EXECUTOR_NODE_POOL` | executor Job의 nodeSelector·toleration 좌표 |
| launcher | `ORCH_GITHUB_APP_SECRET_NAME` | token-minter에만 mount할 branch-writer App Secret 이름 |
| launcher/token-minter | `ORCH_GITHUB_APP_ID`, `ORCH_GITHUB_APP_INSTALLATION_ID` | Contents write 전용 branch-writer App 공개 좌표 |
| launcher | `ORCH_MAX_CONCURRENT_EXPERIMENTS` | namespace의 executor Job 동시 실행 상한 |
| launcher | `ORCH_CODEX_HOME_SECRET_NAME` | Infra가 생성·이름을 소유하는 executor 전용 Codex 인증 Secret 이름 (`auth.json` key 제공, launcher가 volume `defaultMode=0440` 지정) |
| launcher | `ORCH_ACTIVE_DEADLINE_SEC` | 8-container Job 전체 실행 상한 (`60000`초) |
| launcher | `ORCH_TTL_AFTER_FINISHED_SEC` | 완료 Job 보존 시간(기본·최소 `120`초, 장애 smoke에서만 상향) |
| launcher | `ORCH_MLFLOW_TRACKING_URI` | 학습이 MLflow run을 기록할 tracking server 좌표. executor에는 접두사 없는 `MLFLOW_TRACKING_URI`로 내보낸다 |
| 로그 수집기 | `ORCH_LOG_COLLECT_INTERVAL_SEC` | Pod 로그 폴링 주기(기본 `5`초). 워크벤치 폴링 5초와 합쳐 최악 지연이 정해진다 |
| PR 생성기 | `ORCH_PULL_REQUEST_INTERVAL_SEC` | 완주한 실험을 훑어 `exp` → `dev` PR을 여는 주기(기본 `60`초, #689) |
| PR 생성기 | `ORCH_GITHUB_APP_PRIVATE_KEY_FILE` | branch-writer App private key 경로(기본 `/var/run/github-app/key.pem`). 값은 mount하며 애플리케이션이 읽어 변수에 담지 않는다 |
| launcher | `ORCH_EXPERIMENT_RESULTS_ROOT` | 실험 산출물을 남길 GCS 루트(`gs://bucket[/prefix]`). 비어 있으면 게시하지 않는다 — Pod의 workspace는 emptyDir이라 측정 결과가 사라진다 |
| executor | `ORCH_EXPERIMENT_ID`, `ORCH_ISSUE_NUMBER`, `ORCH_ISSUE_BRANCH`, `ORCH_BASE_DEV_SHA` | launcher가 DB에서 복사해 전달하는 불변 branch 좌표 |
| token-minter | `ORCH_GITHUB_APP_PRIVATE_KEY_FILE` | branch/clone/push token-minter에만 보이는 private key mount 경로 |
| token-minter/각 consumer | `ORCH_GITHUB_TOKEN_FILE` | purpose별 memory volume의 mode 0400 installation token 파일 경로 (`/var/run/{branch,clone,push}-token/token`) |
| candidate-finalizer | `ORCH_EXECUTOR_API_URL`, `ORCH_EXECUTOR_API_TOKEN_FILE` | internal Candidate API URL과 `ORCH_EXECUTOR_API_TOKEN` Secret을 mount한 `/var/run/executor-api-token/token` 경로 |
| codex-worker/candidate-finalizer | `ORCH_CODEX_HOME`, `ORCH_CODEX_TIMEOUT_SEC` | read-only Codex auth source와 Job 전체 상한보다 작은 Codex 실행 상한 (`6000`초). Codex는 두 번 돕니다 — 코드 수정(5)과 `report.md` 작성(8) |

동일 executor digest는 아래 8개 container가 순서대로 사용합니다. GitHub App private key는
1·3·7의 token-minter에만, executor 전용 Codex 인증 Secret의 `CODEX_HOME`은 5·8에만,
`ORCH_EXECUTOR_API_TOKEN`은 8에만 mount합니다. 5·6에는 GitHub/API credential volume을
mount하지 않습니다. **8은 push token·API token과 Codex 인증을 함께 들고 있습니다** —
`report.md`를 쓰는 Codex가 도는 곳이 결과가 나오는 곳이기 때문이며, Codex의 자격 증명
접근 금지는 코드가 아니라 하네스 지침이 담당합니다. 컨테이너를 갈라 이 공존을 없애는
것은 후속 재구성(8 → 4/5)의 몫입니다.

1. `branch-token-minter`: private key → branch token memory volume
2. `branch-creator`: branch token → 봉인 `base_dev_sha`의 exp ref 관찰/생성
3. `clone-token-minter`: private key → clone token memory volume
4. `workspace-preparer`: clone token + issue 번호·branch·기준 SHA → raw issue 조회·workspace/state
5. `codex-worker`: workspace + read-only `.git` + state + read-only auth source `CODEX_HOME` →
   `/tmp` 아래 mode 0700 per-run writable scratch `CODEX_HOME`에 regular `auth.json`만 mode 0400으로
   복사 → clone 루트 `AGENTS.md`를 executor 전용 하네스 지침으로 교체 →
   `codex exec --ephemeral --json`으로 working tree 수정 → **하네스 파일을 원본으로 복원**.
   `--json`은 turn마다 토큰 사용량을 실어, 두 Codex stage가 input·cached·output 분해를
   로그 한 줄로 남기게 한다(#742). 사람이 읽는 stdout은 총량 한 줄만 실어 캐시 적중분을
   분리할 수 없었다.
   config·plugin 등 다른 source 파일은 복사하지 않음
6. `candidate-verifier`: workspace + read-only `.git` + state → 고정 Ruff/pytest 검증 결과
7. `push-token-minter`: private key → push token memory volume
8. `candidate-finalizer`: workspace + push token + verifier 결과 + API token + read-only auth
   source `CODEX_HOME` → candidate commit/push, `candidate_sha` 저장, `RUNNING → EVALUATING`,
   candidate 학습·채점 → **`metrics.json` GCS 게시** → `metrics.json`과 candidate diff로
   Codex가 `report.md` 작성 → `report.md` 게시 → 지표 요약 보고

하네스 지침 교체는 반드시 되돌립니다. verifier가 `git status`와 `ls-files --others`로 변경
파일을 수집하므로, 교체한 채로 두면 하네스 파일이 candidate 변경으로 잡혀 commit·push
됩니다. 저장소 원본 `AGENTS.md`는 사람과 로컬 에이전트를 위한 기여 가이드라 "이슈를 먼저
발행"·"`docs/specs/`에 계획 작성"처럼 executor가 수행할 수 없는 절차를 요구하고, 그대로
두면 Codex가 "규칙상 못 하겠다"며 아무것도 하지 않아 실제 실패와 구분되지 않습니다.

**숫자는 리포트보다 먼저 게시합니다.** 한 번에 올리면 Codex 실행 시간(최대
`ORCH_CODEX_TIMEOUT_SEC`)만큼 숫자를 잃을 수 있는 창이 열립니다 — 그 사이
`activeDeadlineSeconds`나 OOM으로 container가 죽으면 push와 `RUNNING → EVALUATING`은 이미
끝난 뒤라 실험은 ERROR로 회수되고 측정한 숫자는 어디에도 남지 않습니다. `report.md`
작성이 실패해도 게시와 API 보고가 그대로 일어나는 것은 예외 경로만이 아니라 **container가
죽는 경로에서도** 성립해야 합니다.

버킷 IAM이 `objectCreator`(교체 불가)라 먼저 게시된 `metrics.json`은 뒤이어 도는 Codex가
로컬 파일을 고치더라도 그대로 남습니다.

executor image에는 Git CLI, uv, `/opt/autoresearch-venv`의 lock 기반 기본+`dev`
의존성(Feast 제외), Node.js, `@openai/codex@0.146.0`과 `UV_PROJECT_ENVIRONMENT`가
고정됩니다. workspace-preparer는 runtime clone의 Issue Form parser를 실행하지 않고 GitHub의
현재 이슈 본문을 raw 입력으로 전달합니다. repository 소스 전체, `.env`, `auth.json`, Codex
인증은 image에 포함하지 않으며 Codex 인증은 runtime mount로만 제공합니다.

Infra companion PR에는 다음을 확인 항목으로 옮깁니다. 실제 Secret/PVC/resource/
NetworkPolicy 이름·값은 `Autoresearch-infra` 소유이므로 이 저장소에서 단정하지 않습니다.

- GitHub App private key를 branch/clone/push token-minter에만 mount
- executor 전용 Codex 인증 Secret의 `auth.json` key를 launcher가 mode 0440의 read-only
  `subPath` 파일로 codex-worker·candidate-finalizer에만 mount하고, writable `executor-tmp`의
  `/tmp`를 per-run scratch에 제공
- `ORCH_EXECUTOR_API_TOKEN`을 candidate-finalizer에만 mount
- workspace/token `emptyDir` size limit과 GitHub·OpenAI·internal API 최소 egress
- immutable launcher/executor/API digest, non-root/seccomp/capability drop/
  `automountServiceAccountToken=false`

**Job의 container·volume·mount 구성을 바꾸는 PR은 같은 PR에서
`autoresearch-experiment-job-contract` ValidatingAdmissionPolicy의 변경 필요 여부를
확인합니다.** 이 정책이 위 계약을 admission 수준에서 강제하며 `Autoresearch-infra`
소유라, 이 저장소의 릴리스만으로는 반영되지 않습니다. 어긋나면 Job 생성이 422로
거부되고 실험은 `RUNNING`인 채 launcher tick마다 재시도됩니다 — 2026-08-09에
`candidate-finalizer`의 `codex-home` mount를 추가하면서 실제로 겪었습니다.

`auto-experiment`는 `[AR]` 이슈의 분류와 promotion guard에 남지만 branch 생성
트리거가 아닙니다. Phase 1 executor는 기존 GitHub Actions bot marker를 새로 쓰지
않으므로, 새 marker 없는 exp branch는 promotion workflow 입력이 아닙니다. marker
신뢰 계약 재설계는 실제 실험 실행 전 후속 gate입니다.

action log 데이터 레이크는 **일일 슬라이스 파티션**(`dt=D` = KST D일
하루치, 파티션 간 서로소)으로 적재되며, 피처·학습 소비자는 `dt BETWEEN`
프루닝으로 30일 히스토리를 조립합니다. 계약 상세:
[`docs/specs/2026-07-24-action-log-slice-semantics.md`](docs/specs/2026-07-24-action-log-slice-semantics.md)

## 코드 영역과 주요 경로

| 영역 | 주요 경로 |
|---|---|
| Model Training | `autoresearch/model_training/`, `autoresearch/model_evaluation/`, `autoresearch/model_registry/` |
| Feast Features | `feature_repo/`, `autoresearch/feature_engineering/` |
| YouTube Collection & Release | `autoresearch/data_collection/`, `applications/youtube_api_proxy/`; 과거 release·배포 트리거는 `.github/workflows-disabled/` |
| Airflow Orchestration | 과거 인접 `Autoresearch-airflow` 저장소 계약(현재 연동 비활성) |
| GCP Infrastructure | 과거 인접 `Autoresearch-infra` 저장소 계약(현재 연동 비활성) |
| Reranking Serving | `applications/reranking_api/` |
| Recommendation | `autoresearch/recommendation/` |

## 시작하기

```bash
uv sync                                    # .venv 생성 + 의존성 설치 (uv.lock 기준)
uv run python -m pytest -n 4 --dist loadfile --durations=25 # 테스트 실행 (CI와 동일)
uv run --no-sync ruff check autoresearch tests tools   # lint (CI와 동일)
```

- Python 3.12 (`.python-version`), 의존성 단일 출처는 `pyproject.toml` + `uv.lock`
- 필수 환경 변수는 `.env.example` 참조
- Feast 작업은 격리 그룹 사용: `uv sync --only-group feast`

## 문서

- 문서 인덱스: [`docs/README.md`](docs/README.md)
- 기여 규칙(브랜치·이슈·PR 전략): [`CONTRIBUTING.md`](CONTRIBUTING.md)
- AI 코딩 에이전트 가이드: [`CLAUDE.md`](CLAUDE.md)
