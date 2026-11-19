# shellcheck shell=bash
# local_kind 스크립트 공용 설정. 직접 실행하지 않고 다른 스크립트가 source 한다.
#
# 비밀 값은 저장소에 두지 않는다. local.env에는 비밀이 저장된 **경로**만 적고,
# 스크립트가 실행 시점에 그 파일을 읽어 kind 클러스터의 Secret으로 만든다.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
STATE_DIR="${HERE}/.state"
mkdir -p "${STATE_DIR}"

if [[ ! -f "${HERE}/local.env" ]]; then
  echo "local.env가 없습니다. 'cp ${HERE}/local.env.example ${HERE}/local.env' 후 값을 채우세요." >&2
  exit 1
fi
# shellcheck source=/dev/null
source "${HERE}/local.env"

CLUSTER_NAME="${CLUSTER_NAME:-autoresearch-local}"
REGISTRY_NAME="${REGISTRY_NAME:-kind-registry}"
REGISTRY_PORT="${REGISTRY_PORT:-5001}"
GITHUB_REPOSITORY="${GITHUB_REPOSITORY:-bbungjun/Autoresearch}"

# 운영 GKE와 같은 이름을 쓴다. executor Pod는 이 노드 풀 라벨·taint가 있는 노드에만 뜬다.
EXECUTOR_NODE_POOL="executor"
EXECUTOR_NODE="${CLUSTER_NAME}-worker2"
PLATFORM_NAMESPACE="orch-system"
JOB_NAMESPACE="experiments"
DATABASE_URL="postgresql://orch:orch@postgres.${PLATFORM_NAMESPACE}.svc.cluster.local:5432/autoresearch"
EXECUTOR_API_URL="http://experiment-api.${PLATFORM_NAMESPACE}.svc.cluster.local:8000"

k() {
  kubectl --context "kind-${CLUSTER_NAME}" "$@"
}

die() {
  echo "error: $*" >&2
  exit 1
}

require() {
  local tool
  for tool in "$@"; do
    command -v "${tool}" >/dev/null 2>&1 || die "'${tool}' 명령이 필요합니다. README의 사전 준비를 확인하세요."
  done
}

load_images() {
  [[ -f "${STATE_DIR}/images.env" ]] || die "이미지 digest가 없습니다. 먼저 ./build.sh를 실행하세요."
  # shellcheck source=/dev/null
  source "${STATE_DIR}/images.env"
}
