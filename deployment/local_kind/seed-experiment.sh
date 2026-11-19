#!/usr/bin/env bash
# 이미 GitHub에 만든 Auto Research 이슈 하나를 launcher가 선점할 수 있는 실험 행으로 넣는다.
# 운영에서는 API가 이슈를 발행하며 이 좌표를 봉인하지만, 로컬 재현은 그 단계를 건너뛰고
# 같은 세 좌표(issue_number, issue_branch, base_dev_sha)만 직접 기록한다.
#
#   ./seed-experiment.sh <이슈번호> [메모]
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require kubectl git

issue_number="${1:-}"
note="${2:-local kind run}"
[[ "${issue_number}" =~ ^[1-9][0-9]*$ ]] || die "사용법: ./seed-experiment.sh <이슈번호> [메모]"
[[ "${note}" =~ ^[A-Za-z0-9\ ._:/-]{1,120}$ ]] || die "메모는 영문·숫자·공백·._:/- 만 120자까지 허용합니다."

issue_branch="exp/${issue_number}"
remote="https://github.com/${GITHUB_REPOSITORY}.git"

# 같은 브랜치가 이미 있으면 executor는 Codex를 건너뛰고 기존 candidate를 채택한다.
# 비교 실행이 서로 다른 경로를 타지 않도록, 브랜치가 있으면 멈춘다.
if [[ -n "$(git ls-remote --heads "${remote}" "${issue_branch}")" ]]; then
  die "${issue_branch} 브랜치가 이미 있습니다. 다시 실행하려면 'git push origin --delete ${issue_branch}' 후 재시도하세요."
fi

base_dev_sha="$(git ls-remote "${remote}" refs/heads/dev | cut -f1)"
[[ "${base_dev_sha}" =~ ^[0-9a-f]{40}$ ]] || die "dev 브랜치 SHA를 읽지 못했습니다."

# 위에서 형식을 검증한 값만 SQL에 넣는다.
experiment_id="$(
  k -n "${PLATFORM_NAMESPACE}" exec -i deploy/postgres -- \
    psql -U orch -d autoresearch -v ON_ERROR_STOP=1 -qtA <<SQL
INSERT INTO experiments (hypothesis, status, issue_number, issue_branch, base_dev_sha)
VALUES ('${note} (issue #${issue_number})', 'CREATED', ${issue_number}, '${issue_branch}', '${base_dev_sha}')
RETURNING id;
SQL
)"
experiment_id="$(echo "${experiment_id}" | tr -d '[:space:]')"
[[ -n "${experiment_id}" ]] || die "실험 행을 넣지 못했습니다."

echo "${experiment_id}" > "${STATE_DIR}/last-experiment-id"
echo "실험 생성: id=${experiment_id} issue=#${issue_number} branch=${issue_branch} base=${base_dev_sha:0:12}"
echo "다음: ./launch.sh"
