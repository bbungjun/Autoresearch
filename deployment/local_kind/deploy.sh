#!/usr/bin/env bash
# Secret을 만들고 Postgres·Experiment API를 띄운 뒤 DB migration을 실행한다.
# 여러 번 실행해도 된다. 이미지를 다시 빌드했다면 다시 실행해 API를 새 이미지로 바꾼다.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require kubectl openssl sed
load_images

[[ -n "${GITHUB_APP_ID:-}" && -n "${GITHUB_APP_INSTALLATION_ID:-}" ]] \
  || die "local.env의 GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID를 채우세요."
[[ -r "${GITHUB_APP_PRIVATE_KEY_PATH:-}" ]] \
  || die "GitHub App private key를 읽을 수 없습니다: ${GITHUB_APP_PRIVATE_KEY_PATH:-<비어 있음>}"
[[ -r "${CODEX_AUTH_JSON_PATH:-}" ]] \
  || die "Codex 인증 파일을 읽을 수 없습니다: ${CODEX_AUTH_JSON_PATH:-<비어 있음>} ('codex login' 필요)"

# 로컬 전용 토큰. 처음 한 번 만들고 재사용한다. API는 두 토큰이 32자 이상이고 서로 달라야 한다.
tokens_file="${STATE_DIR}/tokens.env"
if [[ ! -f "${tokens_file}" ]]; then
  umask 077
  {
    echo "API_TOKEN=$(openssl rand -hex 24)"
    echo "EXECUTOR_API_TOKEN=$(openssl rand -hex 24)"
  } > "${tokens_file}"
fi
# shellcheck source=/dev/null
source "${tokens_file}"

sed "s|__API_IMAGE__|${API_IMAGE}|g" "${HERE}/manifests/platform.yaml" | k apply -f -

apply_secret() {
  # create --dry-run | apply 로 멱등하게 만든다.
  k create secret generic "$@" --dry-run=client -o yaml | k apply -f -
}

# executor Pod가 마운트하는 Secret. 키 이름은 launcher/jobs.py의 items와 같아야 한다.
apply_secret github-app -n "${JOB_NAMESPACE}" \
  --from-file=private-key.pem="${GITHUB_APP_PRIVATE_KEY_PATH}"
apply_secret codex-home -n "${JOB_NAMESPACE}" \
  --from-file=auth.json="${CODEX_AUTH_JSON_PATH}"
apply_secret executor-api-token -n "${JOB_NAMESPACE}" \
  --from-literal=token="${EXECUTOR_API_TOKEN}"

# API 설정. 이번 재현에서 쓰지 않는 기능(이슈 발행, 채팅 LLM, baseline 학습 App)은
# 형식만 맞춘 자리 값을 넣는다. executor가 부르는 candidate·결과 보고 경로만 실제로 쓰인다.
apply_secret api-runtime -n "${PLATFORM_NAMESPACE}" \
  --from-literal=db.env="ORCH_DATABASE_URL=${DATABASE_URL}"
apply_secret api-env -n "${PLATFORM_NAMESPACE}" \
  --from-literal=ORCH_API_TOKEN="${API_TOKEN}" \
  --from-literal=ORCH_EXECUTOR_API_TOKEN="${EXECUTOR_API_TOKEN}" \
  --from-literal=ORCH_GITHUB_REPOSITORY="${GITHUB_REPOSITORY}" \
  --from-literal=ORCH_GITHUB_TOKEN=unused-in-local-kind \
  --from-literal=ORCH_BASELINE_GITHUB_APP_ID=1 \
  --from-literal=ORCH_BASELINE_GITHUB_APP_INSTALLATION_ID=1 \
  --from-literal=ORCH_BASELINE_GITHUB_APP_PRIVATE_KEY_PATH=/nonexistent/unused.pem \
  --from-literal=LLM_BACKEND=codex_cli \
  --from-literal=CODEX_HOME=/tmp

k -n "${PLATFORM_NAMESPACE}" rollout status deploy/postgres --timeout=180s

migration_job="db-migrate-$(date +%s)"
k -n "${PLATFORM_NAMESPACE}" apply -f - <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: ${migration_job}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: alembic
          image: ${API_IMAGE}
          workingDir: /app
          command: ["alembic", "-c", "applications/experiment_platform/alembic.ini", "upgrade", "head"]
          env:
            - {name: ORCH_DATABASE_URL, value: "${DATABASE_URL}"}
YAML
if ! k -n "${PLATFORM_NAMESPACE}" wait --for=condition=complete "job/${migration_job}" --timeout=180s; then
  k -n "${PLATFORM_NAMESPACE}" logs "job/${migration_job}" || true
  die "DB migration이 실패했습니다."
fi

k -n "${PLATFORM_NAMESPACE}" rollout restart deploy/experiment-api
k -n "${PLATFORM_NAMESPACE}" rollout status deploy/experiment-api --timeout=180s
echo "플랫폼 준비 완료. 다음: ./seed-experiment.sh <이슈번호>"
