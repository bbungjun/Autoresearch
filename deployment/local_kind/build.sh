#!/usr/bin/env bash
# executor·api·launcher 이미지를 현재 작업 트리에서 빌드해 로컬 레지스트리에 push하고,
# launcher가 요구하는 digest 형식(name@sha256:...)으로 .state/images.env에 기록한다.
# As-Is와 To-Be를 비교할 때는 브랜치를 바꾼 뒤 이 스크립트를 다시 실행한다.
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require docker git

cd "${REPO_ROOT}"
vcs_ref="$(git rev-parse HEAD)"
images_file="${STATE_DIR}/images.env"
: > "${images_file}.tmp"

for name in executor api launcher; do
  tag="localhost:${REGISTRY_PORT}/autoresearch-${name}:local"
  echo "빌드: ${name} (${vcs_ref:0:12})" >&2
  docker build -f "deployment/experiment_platform/${name}.Dockerfile" \
    --build-arg "VCS_REF=${vcs_ref}" -t "${tag}" . >&2
  docker push "${tag}" >&2
  digest_ref="$(docker inspect --format '{{index .RepoDigests 0}}' "${tag}")"
  [[ "${digest_ref}" == *@sha256:* ]] || die "${tag}의 digest를 읽지 못했습니다: ${digest_ref}"
  echo "${name^^}_IMAGE=${digest_ref}" >> "${images_file}.tmp"
done
echo "SOURCE_REF=${vcs_ref}" >> "${images_file}.tmp"
mv "${images_file}.tmp" "${images_file}"

echo "기록됨: ${images_file}"
cat "${images_file}"
