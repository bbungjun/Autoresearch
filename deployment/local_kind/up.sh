#!/usr/bin/env bash
# 로컬 레지스트리와 kind 클러스터를 만들고, executor 노드에 운영과 같은 라벨·taint를 붙인다.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require docker kind kubectl

if [[ "$(docker inspect -f '{{.State.Running}}' "${REGISTRY_NAME}" 2>/dev/null || true)" != "true" ]]; then
  echo "로컬 레지스트리 시작: localhost:${REGISTRY_PORT}"
  docker run -d --restart=always -p "127.0.0.1:${REGISTRY_PORT}:5000" \
    --network bridge --name "${REGISTRY_NAME}" registry:2.8.3
fi

if ! kind get clusters | grep -qx "${CLUSTER_NAME}"; then
  kind create cluster --name "${CLUSTER_NAME}" --config "${HERE}/kind-config.yaml"
fi

registry_dir="/etc/containerd/certs.d/localhost:${REGISTRY_PORT}"
for node in $(kind get nodes --name "${CLUSTER_NAME}"); do
  docker exec "${node}" mkdir -p "${registry_dir}"
  printf '[host."http://%s:5000"]\n' "${REGISTRY_NAME}" \
    | docker exec -i "${node}" cp /dev/stdin "${registry_dir}/hosts.toml"
done

if [[ "$(docker inspect -f '{{json .NetworkSettings.Networks.kind}}' "${REGISTRY_NAME}")" == "null" ]]; then
  docker network connect kind "${REGISTRY_NAME}"
fi

k label node "${EXECUTOR_NODE}" "cloud.google.com/gke-nodepool=${EXECUTOR_NODE_POOL}" --overwrite
k taint node "${EXECUTOR_NODE}" "workload=${EXECUTOR_NODE_POOL}:NoSchedule" --overwrite

echo "클러스터 준비 완료: kind-${CLUSTER_NAME}"
k get nodes -L cloud.google.com/gke-nodepool
