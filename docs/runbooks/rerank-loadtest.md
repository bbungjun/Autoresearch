# 리랭킹 서빙 부하측정 운영 절차

이 문서는 GKE에서 리랭킹 API의 기준선과 단일 개선안을 비교할 때 사용하는
운영 절차입니다. 대상은 실제 Feast online store와 `autoresearch-serving`입니다.
fixture 준비는 이 저장소가, materialize는 Autoresearch-airflow가, GKE 권한과
네트워크는 Autoresearch-infra가 담당합니다.

## 실행 전 확인

- 비교 대상은 같은 모델 버전, fixture 값, serving Pod CPU·메모리 할당,
  워밍업·측정 시간을 사용해야 합니다. 코드 변경은 한 번에 하나의 병목 개선만
  포함합니다.
- loadtest identity는 Deployment를 읽을 수 없습니다. 운영자가 serving의 readiness를
  확인하고, 배포된 immutable image digest와 전체 Git SHA를 확보합니다. 이 값은
  workflow input `serving_image_ref`와 `serving_git_sha`에 직접 복사합니다.
- 이미지 digest는 `image@sha256:` 형식, Git SHA는 소문자 40자리여야 합니다. 준비한
  fixture version은 기본값 `rerank-v1`을 사용하거나, materialize 완료를 확인한
  유효한 버전으로 명시합니다.
- raw GKE/GCP 증거가 아직 없으면 baseline 또는 optimization HTML 보고서를 만들지
  않습니다. `docs/reports/YYYY-MM-DD-rerank-serving-baseline.html`과
  `docs/reports/YYYY-MM-DD-rerank-serving-optimization.html`은 Task 8에서 원시
  증거를 검토한 뒤에만 작성합니다.

## 1. Fixture 준비과 online store 반영

다음 순서를 baseline 시작 전과 optimized 시작 전에 각각 수행합니다. 특히
`UserDynamicView` TTL은 60시간이므로, 비교 전마다 새 timestamp로 다시 적재해야
합니다.

1. 먼저 dry-run으로 범위와 기존 행을 확인합니다. 이 명령은 DML을 실행하지 않습니다.

   ```bash
   uv run python scripts/provision_rerank_loadtest_fixture.py \
     --project "$GCP_PROJECT_ID" --dataset feast_offline_store
   ```

2. 출력에서 fixture version과 UTC timestamp를 기록하고, 네 source table의 기존
   fixture 행 수를 확인합니다. apply 결과의 정확한 row count 계약은
   `user_static_feature=1`, `user_dynamic_feature=1`, `video_feature=200`,
   `user_category_similarity=5`입니다. 네 table은 모두 같은 새 UTC timestamp를
   사용해야 합니다.

3. 검토한 같은 project/dataset으로만 apply합니다.

   ```bash
   uv run python scripts/provision_rerank_loadtest_fixture.py \
     --project "$GCP_PROJECT_ID" --dataset feast_offline_store --apply
   ```

   출력에서 네 table 각각의 `deleted`와 `inserted`를 보관하고, insert count가
   `1/1/200/5`인지 확인합니다. 다른 행을 지우거나 Redis에 직접 쓰지 않습니다.

4. Airflow에서 `feast_online_store_materialize`를 수동 trigger합니다. 해당 task의
   `job_summary.status=succeeded`가 될 때까지 기다린 뒤에만 다음 단계로 진행합니다.
   자정 schedule을 기다리거나 별도 materialize 경로를 만들지 않습니다.

5. 24개 및 200개 canary 요청을 실행하여 모두 HTTP 200인지, 요청 ID 순서와 항목 수가
   유지되는지, 단일 model ID와 유한 score를 반환하는지 확인합니다. 하나라도 실패하면
   fixture/materialize 또는 serving 상태를 해결한 뒤 처음부터 다시 시작합니다.

## 2. 동일 조건 부하 실행

workflow **Rerank serving load test**를 `main`에서 수동 실행합니다. 실행은 공유
ConfigMap 때문에 직렬화됩니다. 각 실행에는 후보 수 하나와 benchmark label 하나만
입력합니다.

1. baseline에서 candidate count `24`를 실행하고, 이어서 `200`을 실행합니다.
   각 실행은 VU `1 → 2 → 4 → 8`을 순서대로 실행합니다.
2. 각 VU Job은 warmup 60초를 결과에서 제외하고, 측정 300초만 집계합니다.
   측정 오류율 gate는 엄격히 `< 1%`입니다. 1% 이상이거나 Job/summary 검증이 실패하면
   다음 VU와 비교 실행으로 진행하지 않고 해당 실행을 실패로 기록합니다.
3. optimized 비교 직전에는 1절을 다시 수행해 `UserDynamicView` timestamp를 refresh하고
   materialize 성공을 다시 확인합니다. 이후 baseline과 같은 순서로 `24`, `200`을
   실행하고 각 후보 수에서 VU `1/2/4/8`을 반복합니다.
4. baseline과 optimized는 candidate/VU/fixture/model/image resources/warmup/measurement가
   같은 행끼리만 짝지어 비교합니다. candidate `24`와 `200`을 평균내거나 합쳐서
   개선이라고 주장하지 않습니다.

## 3. 원시 증거 보관과 해시

GitHub Actions artifact `rerank-loadtest-${benchmark_label}-c${candidate_count}-${run_id}`의
`runner/raw/`를 보관합니다. 각 VU마다 다음을 확보합니다.

- `k6-summary-${job_name}.json`: custom measurement 중앙값(`med`, p50)/p95/p99,
  request 수, status별 count, 오류율을 읽는 원본입니다. k6 summary의 `med` 키가
  p50(중앙값)에 해당합니다.
- `metadata-vu-${vus}.json`: Job 이름, 생성·완료 UTC, candidate/VU, fixture version,
  benchmark label, serving image digest, Git SHA를 확인합니다.
- Prometheus range artifacts: 실제 파일명은
  `prometheus-${query_name}-vu-${vus}.json`입니다. `phase_p50`, `phase_p95`,
  `outcome_rate`, `max_in_flight`, `cpu_seconds`, `rss`, `cfs_throttling_ratio`
  query와 query time range를 함께 보관합니다.
- Prometheus 응답은 HTTP/API `status=success`만으로 유효하다고 보지 않습니다.
  필수 query의 `data.result`가 비어 있으면 workflow를 실패시키며, 빈 series를
  0이나 정상 측정값으로 보고하지 않습니다.
- `cfs_throttling_ratio`는 GKE cAdvisor의
  `container_cpu_cfs_throttled_periods_total / container_cpu_cfs_periods_total`
  5분 rate 비율입니다. 분자·분모를 `pod,container`별로 먼저 집계해 동일한
  container label끼리 나눈 뒤 결과를 검증합니다. 분모를 `1`로 보정하지 않으므로
  분모 series가 없으면 빈 결과로, 분모 rate가 0이면 `NaN`으로 남고 둘 다 snapshot
  단계에서 실패합니다. range/step으로 계산한 예상 sample 수의 80% 이상(최소 2개)이
  각 CFS series에 있어야 하며, 유효한 결과만 0~1 비율로 기록합니다.
  2026-08-03 dev GKE에서 확인한 예시는
  throttled rate `0`, period rate `3.650252529797797`, ratio `0`이었으며,
  이는 해당 시점의 관측값이지 고정된 정상 범위의 추정값이 아닙니다.
- PR #499 이전 run의 `prometheus-cfs_throttling-vu-*.json`은 존재하지 않는
  `container_cpu_cfs_throttled_seconds_total` query가 `status=success`와 빈
  `data.result`로 저장된 진단용 artifact입니다. 해당 CFS 값은 `0` 또는
  `N/A`로 성능 비교에 사용하지 않고 `invalid; rerun required`로 기록합니다.
- query response가 JSON이 아니면 `invalid JSON`, JSON이지만 series가 없거나
  값이 유효 범위를 벗어나면 각각 원인을 기록합니다. 모든 VU·query를 끝까지
  수집한 뒤 실패 목록을 `prometheus-validation-failures.txt`에 저장하고,
  `SHA256SUMS`를 만든 다음 workflow를 실패시켜 사후 분석용 증거를 보존합니다.
- 각 요청에는 `<response>.request-status`와 `<response>.request-stderr` sidecar가
  함께 남습니다. 따라서 빈 JSON 파일이 정상적인 빈 series 응답인지, `kubectl
  get --raw` 요청 자체가 실패해 생긴 파일인지 구분할 수 있습니다.
- `SHA256SUMS`와 Job/Pod describe·log 및 Prometheus reader diagnostic이 있으면 함께
  보관합니다. artifact URL과 각 파일 또는 archive의 SHA-256을 보고서에 기록합니다.

스키마 호환 메모: 이 문서에서 요구하는 literal `prometheus-range-`는 Prometheus
range-query 원시 응답의 범주를 뜻합니다. Task 5 workflow는
`prometheus-range-${query_name}-vu-${vus}.json`이라는 파일을 만들지 않으며, 실제
경로는 위의 `prometheus-${query_name}-vu-${vus}.json`입니다.

## 4. Task 8 보고서 작성 스키마

원시 증거가 확보된 Task 8에서만 baseline 및 optimization HTML 보고서를 작성합니다.
한 행은 정확히 하나의 `candidate_count`와 하나의 VU 조합이며, 보고서 표는 최소 다음
열을 갖습니다.

| 구분 | 기록값 |
| --- | --- |
| 식별·재현 | benchmark phase, candidate count, VU, Job, UTC range, fixture/model/image digest/Git SHA/resources, artifact URL/hash, Prometheus query/time range |
| k6 측정 | custom measurement 중앙값(`med`, p50)/p95/p99, request count, status count, error count, `RPS = custom request count / 300` |
| Prometheus phase·outcome | phase p95, outcome rate, in-flight max |
| 리소스 | CPU, RSS, CFS throttling ratio (0~1), `CPU-seconds/request = CPU seconds rate / RPS` |

각 행의 caption에는 Job, UTC range, fixture/model/image/SHA/resources, artifact URL/hash,
Prometheus query/time range를 모두 적습니다. 수정된 workflow에서는 원시 query가
없거나 빈 series이면 run 자체가 실패하므로 성능 결과로 보고하지 않습니다. PR #499
이전 artifact처럼 이미 성공으로 저장된 빈 query는 `invalid; rerun required`로
표시합니다. `N/A` 또는 invalid는 개선 근거가 아니며, 측정되지 않은 개선
수치·감소율·비용 절감 수치를 채우거나 추정해서는 안 됩니다.
