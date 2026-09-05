# 문서 인덱스

이 저장소의 문서는 아래 규칙으로 배치합니다.

| 위치 | 내용 | 수명 |
|---|---|---|
| `adr/` | 아키텍처 결정 기록 (ADR) | 영구 |
| `specs/` | 살아있는 계약·설계 spec (`YYYY-MM-DD-<slug>.md`) | 유효한 동안 |
| `plans/` | 진행 중 구현 계획 (`YYYY-MM-DD-<slug>.md`) | 구현 완료 시 archive로 |
| `guides/` | 운영·아키텍처 가이드 | 상시 갱신 |
| `runbooks/` | 운영 절차·트러블슈팅 기록 | 상시 갱신 |
| `reports/` | 팀 공유용 시각화 리포트 (HTML) | 참조되는 동안, 이후 archive/reports로 |
| `archive/` | 완료·과거 spec/plan/리포트 보존 | 영구 (수정하지 않음) |

새 spec/plan 작성 규칙은 [`CLAUDE.md`](../CLAUDE.md)의 *Spec / Plan First* 절을
따릅니다. spec/plan이 구현 완료되어 더 이상 계약으로 쓰이지 않으면
`archive/specs/`, `archive/plans/`로 옮깁니다. 코드 디렉토리 안에 문서를 두지
않습니다.

## 역할별 인덱스

문서는 유형(adr/specs/plans/guides)별로 배치되지만, 아래는 역할(도메인) 기준
모아본 것이다. 같은 문서가 여러 역할에 중복 등장할 수 있다.

### 📥 데이터 수집 (YouTube Collection)

- [ADR 0001 — YouTube 프록시의 목적](adr/0001-youtube-proxy-purpose.md)
- [Spec — GCS raw 데이터 BigQuery 적재](specs/2026-07-11-load-raw-to-bigquery.md)
- [가이드 — 데이터 레이크](guides/data-lake.md)

### 👤 가상 유저 (Virtual Users)

- (현재 전용 guide 없음 — `autoresearch/virtual_user_generation/` 코드 및
  `tests/virtual_user_generation/` 참조)

### 📝 Action Log

- [사전 등록 — 다양한 행동 15/10피처 비교](specs/2026-09-05-diverse-behavior-ablation.md) — 학습 bundle·신규 평가 cohort·seed·판정·final 단일 소비 조건 (#109, 평가 미실행)
- [Spec — 다양한 합성 행동 이력](specs/2026-09-05-diverse-behavior-data.md) — 날짜순 30일 warm-up, 사용자별 활동·관심 변화와 데이터 품질 감사 (#107)
- [실측 — 행동 다양성 데이터 생성](reports/2026-09-05-diverse-behavior-data.md) — 32일×3 seed, 168,548개 이벤트와 최근 피처 5개 분산 검증 (#107)
- [가이드 — action log 모듈 사용법](guides/action-log.md)
- [가이드 — Agent Simulator 명세 (action log SSOT)](guides/agent-simulator-spec.md)
- [Spec — Research Harness P0-1 평가 snapshot](specs/2026-08-31-research-harness-evaluation-snapshot.md) — 원천 `slate_id`, 시간·click 귀속, validation/final split, label 봉인과 write-once manifest 계약 (#17)

### 🎯 Feature Engineering

- [가이드 — 피처 스토어](guides/feature-store.md)
- [가이드 — Feast GCP 설정](guides/feast-gcp-setup.md)
- `feature_repo/` 디렉토리 (Feast 규격 — `feature_definitions.py`, `feature_store.yaml`)

### 🏋️ 학습 파이프라인 (Training)

- [가이드 — 학습 데이터셋](guides/training-dataset.md)
- [가이드 — CTR 모델 명세](guides/ctr-model-specification.md)
- [가이드 — 학습 실험 provenance 애플리케이션 설계](guides/training-experiment-provenance.md)
- [Spec — 모델 승격 구조화 결과 계약](specs/2026-07-29-model-promotion-structured-outcome.md)
- [Spec — CTR 모델 배포 패키지](archive/specs/2026-08-01-ctr-model-deployment-package.md) (구현 완료·아카이브)
- [Plan — CTR 모델 배포 패키지 구현](archive/plans/2026-08-01-ctr-model-deployment-package.md) (구현 완료·아카이브)
- [Spec — 학습 윈도우 spine 커버리지 가드](specs/2026-08-01-training-window-coverage-guard.md) — 기준값 근거·lineage 계약·가드의 한계 (#464)
- [Spec — paired offline 실험 배치·비교 결과 계약](specs/2026-08-03-paired-offline-experiment-comparison.md) — 조건 격리 좌표·피처 보존·결과 payload (#454)
- [Plan — paired offline 실험 배치·비교 결과 구현](plans/2026-08-03-paired-offline-experiment-comparison.md) (#454)
- [Plan — #16 개인화 비교군·ablation 실험](archive/plans/2026-09-05-personalization-ablation-experiment.md) — rule-based 합성 평가 세계 3개에서 비교군 5종·피처군 ablation 5종을 paired 검증해 `not_supported`로 완료했다
- [Report — #103 인기도 제거·Recall 확인](reports/2026-09-05-popularity-recall-confirmation.md) — 신규 합성 snapshot 3개, 54회 비교에서 `supported`; production 계약은 유지한다
- [Plan — #103 사전 등록·완료 기록](archive/plans/2026-09-05-popularity-recall-confirmation.md)
- [Spec — 선행 이력과 학습 날짜 선택](specs/2026-09-05-temporal-training-fixture.md) — 기존 v1 유지, opt-in 32일 history와 KST 학습 기간 분리
- [Plan — #105 최근 행동 피처 검증](archive/plans/2026-09-05-temporal-warmup-experiment.md)
- [Report — #105 날짜순 이력과 최근 행동 실측](reports/2026-09-05-temporal-warmup-experiment.md) — 비영 피처 학습을 확인했지만 ranking 지표 동률로 `not_supported`
- [Spec — 모델 성능 열화 시점 측정(rolling-origin 평가)](specs/2026-08-03-model-degradation-rolling-origin-evaluation.md) — 단일 cutoff 기반 forward degradation evaluation, 날짜 구간·평가일 상태 계약, video staleness (#471)
- [Plan — 모델 성능 열화 시점 측정 구현](plans/2026-08-03-model-degradation-rolling-origin-evaluation.md) (#471)
- [Spec — temporal signal 승격 판정 연결](specs/2026-08-04-temporal-signal-promotion-integration.md) — baseline 재정의, hard retrain limit 산출 절차, #425 다중 신호 연결, fail-closed hold (#485 잔여 범위)
- [Plan — temporal signal 승격 판정 연결 구현](plans/2026-08-04-temporal-signal-promotion-integration.md) (#485, Task 4는 #493 대기)
- [Spec — 학습 데이터셋 스냅샷 GCS 게시·재사용 계약](specs/2026-08-04-training-dataset-snapshot-store.md) — content-addressed 스냅샷 레이아웃·write-once·by-date 포인터, `--dataset-uri` 재사용 학습 (#530)
- [Plan — 학습 데이터셋 스냅샷 게시·재사용 구현](archive/plans/2026-08-04-training-dataset-snapshot-store.md) (#530 구현 완료·아카이브)
- [Spec — 실험별 Feast Registry·offline 실행 격리](specs/2026-07-31-experiment-isolated-offline-run.md) (#454 실행 context)
- [Spec — 저장소 구조 재배치](specs/2026-08-13-repository-structure-redesign.md) — 파이프라인 단계 축 재배치, 전환 기간 계약 (#754)
- [Plan — 저장소 구조 재배치 구현](plans/2026-08-13-repository-structure-redesign.md) (#754)
- `autoresearch/model_training/`, `autoresearch/model_evaluation/`, `autoresearch/feature_engineering/` (CTR 학습·평가 코드)

### 🚀 서빙 (Serving)

- [Spec — YouTube 리랭킹 서빙 API](specs/2026-07-16-reranking-serving-api.md)
- [Spec — Rerank Serving 성능·비용·안정성 벤치마크](specs/2026-07-31-rerank-serving-performance-benchmark.md)
- [Plan — Rerank Serving 성능 벤치마크 구현](plans/2026-08-01-rerank-serving-performance.md)
- [Runbook — 리랭킹 서빙 부하측정 운영 절차](runbooks/rerank-loadtest.md)
- [Plan — Reranking Serving API 구현](archive/plans/2026-07-16-reranking-serving-api.md) (완료·아카이브)
- [시각화 — Serving Feature Build: 무엇이 바뀌었나](reports/2026-07-22-serving-feature-build-overview.html) — 비개발 팀원용 변경 흐름·운영 경계 안내
- `applications/reranking_api/` (FastAPI 추론 서버), `deployment/serving/` (이미지 정의)

### 🤖 오케스트레이션 (Experiment API)

- [Spec — Agent Orchestration 채팅 저장 스켈레톤](archive/specs/2026-07-30-agent-orchestration-chat-postgres-skeleton.md) (구현 완료)
- [Plan — Agent Orchestration 1단계 구현 계획](archive/plans/2026-07-30-agent-orchestration-chat-postgres-skeleton.md) (구현 완료)
- [Plan — Agent Orchestration PR 사전 병합 강화](archive/plans/2026-07-31-agent-orchestration-premerge-hardening.md) (구현 완료)
- [Spec — Agent Orchestration 실험 워크벤치 v0](archive/specs/2026-08-01-agent-orchestration-experiment-workbench-v0.md) (구현 완료)
- [Plan — Agent Orchestration 실험 워크벤치 v0](archive/plans/2026-08-01-agent-orchestration-experiment-workbench-v0.md) (구현 완료)
- [Spec — 실험 Step 추적 v0](specs/2026-08-04-experiment-step-tracking-v0.md) — 에이전트 진행 상황 실시간 관찰 계약 (#518 구현 완료, 계약은 유효)
- [Spec — 실험 Job 기준 커밋 고정 계약](specs/2026-08-05-experiment-job-baseline-freeze.md) — 대기열·동시 실행 중에도 `base_dev_sha`를 고정하고 executor Pod가 그 SHA에서만 exp branch를 생성하는 Phase 1 계약. marker 없는 Phase 1 branch는 promotion 입력이 아님 (#546)
- [Spec — 실험 executor Phase 2](specs/2026-08-06-experiment-executor-phase2.md) — 봉인 exp 브랜치의 Codex 코드 수정과 executor 소유 candidate commit·push 계약 (#557)
- [Plan — 실험 executor Phase 2](plans/2026-08-06-experiment-executor-phase2.md) — Candidate API부터 branch-creator·Codex·verifier·commit/push·8-container Job·배포 검증까지 7단계 구현 순서 (#557, Stage review 종료 전 archive 보류)
- [Plan — Executor raw 이슈 입력](archive/plans/2026-08-07-executor-raw-issue-input.md) (#592 구현 완료·아카이브)
- [Plan — 실험 브랜치 Bootstrap Kubernetes Job Phase 1](plans/2026-08-05-experiment-branch-bootstrap-k8s-job-phase1.md) — GitHub App installation token, launcher 선점·동시 상한, launcher/executor digest 게시, executor ref 생성과 infra 적용 순서 (#546)
- [Plan — 실험 Step 추적 v0](archive/plans/2026-08-04-experiment-step-tracking-v0.md) (구현 완료·아카이브)
- [Spec — Agent Orchestration `/chat` API 계약](specs/2026-08-01-agent-orchestration-chat-api-contract.md) — 내부 호출 서비스의 요청·응답·오류·저장 의미 정본
- [Spec — 가설 수신부터 `[AR]` 이슈 발행까지](specs/2026-08-04-hypothesis-to-auto-research-issue.md) — 필드 소유권 3분할, 시드 고정, `gh` 발행 경계, 멱등성 (#516)
- [Plan — 가설 수신부터 `[AR]` 이슈 발행까지 구현](plans/2026-08-04-hypothesis-to-auto-research-issue.md) (#516)
- [Spec — 자율 ML 연구 Harness 기반 MVP와 논문 로드맵](specs/2026-08-14-paper-grounded-autonomous-ml-research-harness.md) — 저장소 전체 수정·외부 Sealed Judge·σ 기반 판정·local-first 반복 연구 계약과 §12의 구현/회귀·실측·수용 판단 구분 (#17; 최초 설계는 이전 조직 #769). 현재 유효 계약이므로 유지한다.
- [Plan — 로컬 Research Harness MVP](archive/plans/2026-08-15-local-research-harness-mvp.md) — Task 1~6 구현, Task 7의 feedback·재개·복구·단일 피처 실증과 ADR 0003 수용 결정을 완료해 archive했다 (#17, 2026-09-05).
- [실측·포트폴리오 기록 — 로컬 자율 ML 실험 E2E](reports/2026-09-03-local-autonomous-experiment-e2e.md) — #60 feedback·재개·엄격한 기각, #69 실패 코드 수정·σ=0 판정 불가, #71 새 22번째 피처의 offline promote, §23 MVP 정책 수용, §24 장경로 안정화와 §25 개인화 비교군·ablation 실측을 기록한다.
- [Spec — Research Harness P0-1 평가 snapshot](specs/2026-08-31-research-harness-evaluation-snapshot.md) — 완료된 P0-1 구현의 현재 유효 계약: `slate_id`, 평가 기간·click 귀속, label 봉인, split·fingerprint, 독립 재생성과 candidate-safe data view (#17, #22). 후속 Judge/Controller가 계속 소비하는 정본이므로 archive하지 않는다.
- `applications/experiment_platform/` (FastAPI + Codex CLI/OpenAI + PostgreSQL 실험 API)
- [Spec — Agent Orchestration GKE 내부 배포](specs/2026-07-30-agent-orchestration-gke-internal-deployment.md)
- [Plan — Agent Orchestration GKE 내부 배포](plans/2026-07-30-agent-orchestration-gke-internal-deployment.md)

### 🌬️ 오케스트레이션 (Airflow)

- [Spec — Autoresearch-airflow 경계 컷오버](specs/2026-07-13-autoresearch-airflow-boundary-cutover.md) (Phase 1~5 완료, Phase 6 대기)
- [Spec — 공개 batch 실행 계약 batch-contract-v1](specs/2026-07-13-public-batch-execution-contract.md)
- 본 저장소 `dags/`는 비어있습니다. DAG는 이전 조직 환경에서
  [`Autoresearch-airflow`](https://github.com/SKYAHO/Autoresearch-airflow)가
  담당했으며, 현재 개인 저장소에서는 해당 연동을 전제하지 않습니다.

### ☁️ 인프라 (Infrastructure)

- [Spec — MLflow 배포 전략](specs/2026-07-14-mlflow-deployment-strategy.md)
- [Spec — 배치 이미지 GCS 코드 부트스트랩 전환](specs/2026-08-12-batch-image-source-decoupling.md) — `Dockerfile.app` 소스 분리, 아카이브 규약·digest 정본·반영 시점 3개 결정
- [가이드 — 데이터 웨어하우스 (BigQuery)](guides/data-warehouse.md)
- `deployment/mlflow/`, `applications/youtube_api_proxy/` (Cloud Run forwarder), `deployment/Dockerfile.app`

### 📚 저장소 메타 (Repository Meta)

- [문제 해결 기록 — CI Docker 반복 빌드 개선](runbooks/ci-docker-cache.md) — 병목 근거, 대안·트레이드오프, 구현·리뷰·검증, 포트폴리오 초안과 후속 측정 (#81)
- [문제 해결 기록 — CI pytest 병렬 실행](runbooks/ci-pytest-parallel.md) — 직렬 실행 병목 근거, xdist 선택과 로컬 비교, PR·main 원격 최종 실측 (#85, #87)
- [ADR 0002 — 저장소 책임 경계](adr/0002-repository-responsibility-boundaries.md)
- [ADR 0003 — 로컬 Research Harness MVP 수용 경계](adr/0003-local-research-harness-mvp-acceptance.md)
- [Spec — 저장소 구조 재정리](specs/2026-07-15-repo-restructure.md)
- [Spec — 머지된 PR 리포트 아카이브](specs/2026-07-26-pr-report-archive-design.md)

## ADR

- [0001 — YouTube 프록시의 목적](adr/0001-youtube-proxy-purpose.md)
- [0002 — 저장소 책임 경계](adr/0002-repository-responsibility-boundaries.md)
- [0003 — 로컬 Research Harness MVP 수용 경계](adr/0003-local-research-harness-mvp-acceptance.md)

## 유효한 Spec (살아있는 계약)

- [공개 batch 실행 계약](specs/2026-07-13-public-batch-execution-contract.md) —
  Airflow가 소비하는 공개 CLI·인자 계약
- [Autoresearch-airflow 경계 컷오버](specs/2026-07-13-autoresearch-airflow-boundary-cutover.md)
- [MLflow 배포 전략](specs/2026-07-14-mlflow-deployment-strategy.md)
- [GCS raw 데이터 BigQuery 적재](specs/2026-07-11-load-raw-to-bigquery.md)
- [오프라인 feature build 배치](specs/2026-07-22-feature-store-build-batch.md)
- [저장소 구조 재정리](specs/2026-07-15-repo-restructure.md) — 이 문서 구조의 근거.
  결정 3(`src/` 통합 목표 구조)은 #754로 대체됐습니다
- [저장소 구조 재배치](specs/2026-08-13-repository-structure-redesign.md) — 파이프라인
  단계 축 재배치의 근거·실측·전환 기간 계약 (#754)
- [머지된 PR 리포트 아카이브](specs/2026-07-26-pr-report-archive-design.md) —
  GitHub Pages에 누적된 merge PR 리포트의 정적 검색 인덱스
- [모델 승격 구조화 결과 계약](specs/2026-07-29-model-promotion-structured-outcome.md) —
  승격·게이트 미달·후보 없음·실행 오류의 기계 판독 결과와 Airflow 인계 계약
- [paired offline 실험 배치·비교 결과 계약](specs/2026-08-03-paired-offline-experiment-comparison.md) —
  baseline/candidate 조건 격리, 실험 피처 보존, `comparison_passed`/`rejected`/`failed` 결과 payload
- [모델 성능 열화 시점 측정(rolling-origin 평가)](specs/2026-08-03-model-degradation-rolling-origin-evaluation.md) —
  단일 cutoff 학습 → 하루 단위 순차 평가로 ROC-AUC 열화 곡선·열화 지점 산출, 데이터 가용성 제약(`A-D`)
- [학습 데이터셋 스냅샷 GCS 게시·재사용 계약](specs/2026-08-04-training-dataset-snapshot-store.md) —
  content-addressed 불변 스냅샷 주소 체계, write-once 의미론, by-date 포인터 갱신 규칙,
  `--dataset-uri` 재사용 학습의 재검증 계약
- [Rerank Serving 성능·비용·안정성 벤치마크](specs/2026-07-31-rerank-serving-performance-benchmark.md)
- [Agent Orchestration `/chat` API 계약](specs/2026-08-01-agent-orchestration-chat-api-contract.md) —
  내부 호출 서비스의 요청·응답·오류·저장 의미 정본
- [실험 Job 기준 커밋 고정 계약](specs/2026-08-05-experiment-job-baseline-freeze.md) —
  Job 대기열과 동시 실행 중 기준 SHA·비교 집합 기준선을 고정하는 #546 Phase 1 계약
- [실험 executor Phase 2](specs/2026-08-06-experiment-executor-phase2.md) —
  봉인 exp 브랜치의 Codex 코드 수정과 executor 소유 candidate commit·push 계약
- [실험 executor Phase 2 구현 계획](plans/2026-08-06-experiment-executor-phase2.md) —
  Candidate API부터 branch-creator·Codex·verifier·commit/push·8-container Job·배포 검증까지 7단계 구현 순서 (Stage review 종료 전 archive 보류)
- [실험 브랜치 Bootstrap Kubernetes Job Phase 1 구현 계획](plans/2026-08-05-experiment-branch-bootstrap-k8s-job-phase1.md) —
  기준 SHA 봉인, GitHub App token, 독립 launcher·executor digest, infra companion 변경의 fail-first 구현 순서
- [가설 수신부터 `[AR]` 이슈 발행까지](specs/2026-08-04-hypothesis-to-auto-research-issue.md) —
  Issue Form 18필드의 LLM/사용자/서버 소유권 분할, `POLICY_SEEDS` 고정, `gh` 발행 경계와 멱등성 3중 방어
- [자율 ML 연구 Harness 기반 MVP와 논문 로드맵](specs/2026-08-14-paper-grounded-autonomous-ml-research-harness.md) —
  저장소 전체 수정, 예측 점수 artifact 기반 Sealed Judge, baseline seed 노이즈 상대 판정,
  local-first 반복 연구 계약과 §12의 증거 수준별 현황 (#17; 이전 조직 #769의 후속)
- [Research Harness P0-1 평가 snapshot](specs/2026-08-31-research-harness-evaluation-snapshot.md) —
  원천 `slate_id`, multi-day click 귀속, validation/final 분리, label 봉인과
  content-addressed write-once snapshot, 독립 두 root 재생성과 candidate-safe data view 계약 (#17, #22; P0-1 완료, 현재 유효 계약)
## 가이드

- [전체 파이프라인 개요](guides/pipeline-overview.md) — 배치·서빙·시뮬레이션 폐루프 mermaid 다이어그램
- [데이터 레이크](guides/data-lake.md) · [데이터 웨어하우스](guides/data-warehouse.md)
- [학습 데이터셋](guides/training-dataset.md)
- [피처 스토어](guides/feature-store.md) · [Feast GCP 설정](guides/feast-gcp-setup.md)
- [CTR 모델 명세](guides/ctr-model-specification.md)
- [학습 실험 provenance 애플리케이션 설계](guides/training-experiment-provenance.md)
- [Agent Simulator 명세 (action log SSOT)](guides/agent-simulator-spec.md)
- [action log 모듈 사용법](guides/action-log.md)
- [Release & 배포 파이프라인](guides/release-pipeline.md) — 이전 조직 환경의 CI/CD·GAR push·digest 승격·GKE 배포 설계(현재 비활성)
- [CTR 학습 이미지](guides/training-image.md) — `Dockerfile.train`, MLflow tracking URI 연동
- [YouTube 트렌딩 수집 파이프라인](guides/youtube-collection.md) — API 수집·정규화·GCS parquet 적재

## 아카이브

완료된 spec/plan과 과거 리포트(중간발표, QA·실증 테스트 리포트)는
[`archive/`](archive/)에 있습니다. 역사적 기록이므로 갱신하지 않습니다.
