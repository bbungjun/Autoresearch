#!/usr/bin/env bash
# 가장 최근 executor Pod의 컨테이너별 시작·종료 시각과 소요 시간을 표로 출력하고,
# 컨테이너 로그와 함께 .state/runs/<pod>/ 에 저장한다. As-Is·To-Be 비교의 원자료다.
#
#   ./timeline.sh [pod 이름]
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
require kubectl date

pod="${1:-}"
if [[ -z "${pod}" ]]; then
  pod="$(k -n "${JOB_NAMESPACE}" get pods -l app.kubernetes.io/component=experiment-executor \
    --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}')"
fi
[[ -n "${pod}" ]] || die "executor Pod가 없습니다."

out_dir="${STATE_DIR}/runs/${pod}"
mkdir -p "${out_dir}"

status_fields='{.name}{"\t"}{.state.terminated.startedAt}{"\t"}{.state.terminated.finishedAt}{"\t"}{.state.terminated.exitCode}{"\t"}{.state.terminated.reason}{"\n"}'
rows="$(k -n "${JOB_NAMESPACE}" get pod "${pod}" -o jsonpath="{range .status.initContainerStatuses[*]}${status_fields}{end}{range .status.containerStatuses[*]}${status_fields}{end}")"

seconds() { date -u -d "$1" +%s; }

{
  printf 'container\tstarted\tfinished\tseconds\texit\treason\n'
  while IFS=$'\t' read -r name started finished exit_code reason; do
    [[ -n "${name}" ]] || continue
    duration=""
    if [[ -n "${started}" && -n "${finished}" ]]; then
      duration="$(( $(seconds "${finished}") - $(seconds "${started}") ))"
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "${name}" "${started:--}" "${finished:--}" \
      "${duration:--}" "${exit_code:--}" "${reason:--}"
  done <<< "${rows}"
} | tee "${out_dir}/timeline.tsv" | column -t -s $'\t'

while IFS=$'\t' read -r name _; do
  [[ -n "${name}" ]] || continue
  k -n "${JOB_NAMESPACE}" logs "${pod}" -c "${name}" > "${out_dir}/${name}.log" 2>&1 || true
done <<< "${rows}"

k -n "${JOB_NAMESPACE}" get pod "${pod}" -o yaml > "${out_dir}/pod.yaml"
echo
echo "저장됨: ${out_dir}"
