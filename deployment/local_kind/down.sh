#!/usr/bin/env bash
# kind 클러스터와 로컬 레지스트리를 지운다. .state/runs의 측정 기록은 남긴다.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require docker kind

kind delete cluster --name "${CLUSTER_NAME}" || true
docker rm -f "${REGISTRY_NAME}" >/dev/null 2>&1 || true
rm -f "${STATE_DIR}/images.env" "${STATE_DIR}/last-experiment-id"
echo "정리 완료. 측정 기록은 ${STATE_DIR}/runs 에 남아 있습니다."
