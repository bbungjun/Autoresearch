# 비활성화한 GitHub Actions 워크플로우

이 디렉터리의 워크플로우는 팀 조직 `SKYAHO`에서 개인 저장소로 이관할 때
GitHub Actions secrets와 GCP 자원이 이전되지 않아 2026-08-15에 비활성화했습니다.
파일은 복구 근거를 보존하기 위해 삭제하지 않았으며, 필요한 자원을 다시 갖춘 뒤
`.github/workflows/`로 옮기면 해당 계약 테스트도 자동으로 다시 실행됩니다.

## 비활성화 목록

| 워크플로우 | 비활성화 사유 | 되살리기 위한 조건 |
| --- | --- | --- |
| `auto-research-dev-promotion.yml` | 옛 executor가 보내는 `repository_dispatch` 완료 이벤트를 입력으로 받고(`auto-research-dev-promotion.yml:3-5`), `github-actions[bot]`이 남긴 자동 생성 브랜치 marker를 요구합니다(`auto-research-dev-promotion.yml:203-211`). | 실험 launcher/executor와 `exp/*` 자동 브랜치 생성 경로를 복구하고, marker의 작성 주체·신뢰 계약을 다시 검증해야 합니다. |
| `auto-research-promotion.yml` | 옛 실험 producer의 `repository_dispatch` 결과를 입력으로 받고(`auto-research-promotion.yml:3-5`), GCS Registry URI와 자동 `promote/*` 브랜치를 전제합니다(`auto-research-promotion.yml:166-169`, `auto-research-promotion.yml:260-278`). | 실험 결과 producer, GCS Registry, 자동 승격 브랜치 계약을 함께 복구해야 합니다. |
| `claude.yml` | `CLAUDE_CODE_OAUTH_TOKEN`이 없어 Claude 리뷰 액션을 실행할 수 없습니다(`claude.yml:42-45`). | 개인 계정의 `CLAUDE_CODE_OAUTH_TOKEN`만 등록하면 되살릴 수 있습니다. |
| `code-archive.yml` | GCP Workload Identity, 업로더 서비스 계정 secret, GCS 버킷 secret을 요구합니다(`code-archive.yml:79-90`). | WIF provider, `GCS_CODE_UPLOADER_SA`, `CODE_ARTIFACTS_BUCKET`과 대상 GCS 버킷을 새로 구성해야 합니다. |
| `feast-apply-runner-probe-caller.yml` | `SKYAHO/Autoresearch-infra`의 재사용 워크플로우를 호출하고(`feast-apply-runner-probe-caller.yml:56-60`), 조직의 GKE self-hosted runner와 Workload Identity를 전제합니다(`feast-apply-runner-probe-caller.yml:3-11`, `feast-apply-runner-probe-caller.yml:47-50`). | 접근 가능한 infra 재사용 워크플로우, Feast ARC runner scale set, GKE Workload Identity, GCS·Redis 연결을 복구해야 합니다. 이 caller와 호출 대상은 한 쌍으로 검증해야 합니다. |
| `feast-apply.yml` | 조직의 Feast self-hosted runner에서 실행하며(`feast-apply.yml:46-63`), `FEAST_APPLY_SA`를 사용하는 WIF 인증과 GCS·BigQuery·Redis 좌표를 요구합니다(`feast-apply.yml:79-104`). | prod/dev runner scale set, WIF provider와 `FEAST_APPLY_SA`, GCS Registry, BigQuery, Redis·Secret Manager 좌표를 모두 복구해야 합니다. |
| `pr-report-archive.yml` | `PR Comprehension Report` 완료 이벤트와 기존 `gh-pages` 브랜치를 전제합니다(`pr-report-archive.yml:8-14`, `pr-report-archive.yml:34-39`). | `pr-report.yml`을 함께 복구하고 GitHub Pages 및 `gh-pages` 브랜치를 다시 구성해야 합니다. 두 리포트 워크플로우는 게시 순서가 연결된 한 쌍입니다. |
| `pr-report.yml` | `OPENROUTER_API_KEY`로 리포트를 생성하고(`pr-report.yml:64-69`), 결과를 `gh-pages`에 배포합니다(`pr-report.yml:142-147`). | `OPENROUTER_API_KEY`, GitHub Pages, `gh-pages` 브랜치를 함께 구성해야 합니다. |
| `release.yml` | `GAR_PUSHER_SA`를 사용해 WIF로 Artifact Registry에 이미지를 push하고(`release.yml:92-147`), 조직 GitHub App token으로 `SKYAHO/Autoresearch-infra`와 `SKYAHO/Autoresearch-airflow`를 수정합니다(`release.yml:795-810`, `release.yml:1470-1485`). | GCP WIF·Artifact Registry와 `GAR_PUSHER_SA`, `APP_ID`, `APP_PRIVATE_KEY`, 두 인접 저장소에 설치된 GitHub App 권한을 모두 복구해야 합니다. |
| `rerank-loadtest.yml` | WIF로 GKE 자격 증명을 얻어 부하테스트 Job을 실행하고(`rerank-loadtest.yml:83-100`), 별도 서비스 계정으로 GKE Prometheus를 조회합니다(`rerank-loadtest.yml:358-378`). | GKE cluster, WIF provider, 두 서비스 계정, 배포된 rerank serving과 Prometheus, 관련 repository variables를 복구해야 합니다. |

## 유지한 워크플로우

| 워크플로우 | 유지 근거 |
| --- | --- |
| `ci.yml` | GitHub-hosted runner에서 저장소 checkout, pytest, lock drift 검사와 로컬 Docker build를 수행하며(`../workflows/ci.yml:88-113`), `secrets.*`, `vars.*`, GCP 인증 액션을 참조하지 않습니다. |
| `lint.yml` | GitHub-hosted runner에서 Ruff만 실행하며(`../workflows/lint.yml:11-48`), 조직 자원 참조가 없습니다. |
| `release-drafter.yml` | GitHub가 자동 제공하는 `GITHUB_TOKEN` 범위의 repository 권한만 사용하고 외부 secret이나 조직 자원을 참조하지 않습니다(`../workflows/release-drafter.yml:9-19`). |

비활성 워크플로우의 조직 자원 계약 테스트는 활성 경로에 워크플로우가 없으면
명시적으로 skip합니다. 워크플로우 파일을 `.github/workflows/`로 되돌리면 경로
존재 조건이 참이 되어 별도 테스트 수정 없이 계약 검증이 다시 실행됩니다.
