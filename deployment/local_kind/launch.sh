#!/usr/bin/env bash
# launcher tick을 클러스터 안에서 한 번 실행한다. 운영 CronJob 한 번과 같은 일을 한다:
# CREATED 실험을 선점하고, jobs.py가 만든 executor Job manifest를 그대로 제출한다.
#
# ORCH_TRAINING_DATASET_URI를 비워 두므로 학습은 꺼진다(1단계).
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require kubectl
load_images

tick_job="launcher-tick-$(date +%s)"
k -n "${PLATFORM_NAMESPACE}" apply -f - <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: ${tick_job}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      serviceAccountName: launcher
      restartPolicy: Never
      containers:
        - name: launcher
          image: ${LAUNCHER_IMAGE}
          env:
            - {name: ORCH_DATABASE_URL, value: "${DATABASE_URL}"}
            - {name: ORCH_JOB_NAMESPACE, value: "${JOB_NAMESPACE}"}
            - {name: ORCH_EXECUTOR_IMAGE, value: "${EXECUTOR_IMAGE}"}
            - {name: ORCH_EXECUTOR_SERVICE_ACCOUNT, value: experiment-executor}
            - {name: ORCH_EXECUTOR_NODE_POOL, value: "${EXECUTOR_NODE_POOL}"}
            - {name: ORCH_GITHUB_APP_SECRET_NAME, value: github-app}
            - {name: ORCH_GITHUB_APP_ID, value: "${GITHUB_APP_ID}"}
            - {name: ORCH_GITHUB_APP_INSTALLATION_ID, value: "${GITHUB_APP_INSTALLATION_ID}"}
            - {name: ORCH_GITHUB_REPOSITORY, value: "${GITHUB_REPOSITORY}"}
            - {name: ORCH_MAX_CONCURRENT_EXPERIMENTS, value: "1"}
            - {name: ORCH_EXECUTOR_API_URL, value: "${EXECUTOR_API_URL}"}
            - {name: ORCH_EXECUTOR_API_TOKEN_SECRET_NAME, value: executor-api-token}
            - {name: ORCH_CODEX_HOME_SECRET_NAME, value: codex-home}
            - {name: ORCH_EXECUTOR_WORKSPACE_SIZE_LIMIT, value: 10Gi}
            - {name: ORCH_CODEX_TIMEOUT_SEC, value: "6000"}
            - {name: ORCH_ACTIVE_DEADLINE_SEC, value: "60000"}
            # 끝난 Job을 하루 보존해 timeline.sh로 단계별 시간을 읽을 수 있게 한다.
            - {name: ORCH_TTL_AFTER_FINISHED_SEC, value: "86400"}
YAML

if ! k -n "${PLATFORM_NAMESPACE}" wait --for=condition=complete "job/${tick_job}" --timeout=180s; then
  k -n "${PLATFORM_NAMESPACE}" logs "job/${tick_job}" || true
  die "launcher tick이 실패했습니다."
fi
k -n "${PLATFORM_NAMESPACE}" logs "job/${tick_job}"

echo
echo "executor Job:"
k -n "${JOB_NAMESPACE}" get jobs -l app.kubernetes.io/component=experiment-executor
echo "진행 보기:  kubectl --context kind-${CLUSTER_NAME} -n ${JOB_NAMESPACE} get pods -w"
echo "끝나면:     ./timeline.sh"
