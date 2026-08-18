# 리랭킹 서빙 부하측정 운영 절차

이 문서는 GKE에서 리랭킹 API를 측정할 때 사용하는 운영 절차입니다. 대상은 실제
Feast online store와 `autoresearch-serving`입니다. fixture 준비는 이 저장소가,
materialize는 Autoresearch-airflow가, GKE 권한과 네트워크는 Autoresearch-infra가
담당합니다.

## 0. 두 모드 중 무엇을 쓸지 먼저 정합니다

`load_mode` 입력이 측정이 답할 수 있는 질문을 결정합니다. **잘못 고르면 측정은
성공하지만 결론이 틀립니다.**

| 모드 | 부하 방식 | 답할 수 있는 질문 | 답할 수 없는 질문 |
| --- | --- | --- | --- |
| `closed` | 동시 VU 고정 (`constant-vus`) | 같은 동시성에서 개선 전후가 나아졌는가 | 용량 한계, 과부하 거동 |
| `open` | 초당 도착률 고정 (`constant-arrival-rate`) | 한계점은 어디인가, 넘으면 어떻게 실패하는가 | — |

폐루프는 VU가 응답을 받아야 다음 요청을 보내므로 **동시 요청 수가 VU 수를 넘지
못합니다.** 도착률이 곧 처리량이 되어 과부하 상태를 만들 수 없고, 대기열 무한 증가나
부하 차단 부재처럼 **과부하에서만 드러나는 결함을 관측하지 못합니다.** 폐루프로
한계·안정성을 판정하면 "오류율 0%, p95 양호"라는 잘못된 합격이 나옵니다.

실제 유저는 서로 기다려 주지 않으므로 온라인 서빙의 도착 과정은 개루프입니다.
**트래픽이 걸렸을 때 안정적으로 운영되는지 검증하려면 `open`을 씁니다.**

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

1. `closed` 모드에서는 baseline의 candidate count `24`를 실행하고, 이어서 `200`을
   실행합니다. 각 실행은 VU `1 → 2 → 4 → 8`을 순서대로 실행합니다.
   `open` 모드에서는 `arrival_rates`에 초당 도착률을 낮은 값부터 공백으로 구분해
   줍니다(예: `20 40 80 160`). 한계를 넘는 값을 반드시 포함해야 과부하 거동을
   관측할 수 있습니다.
2. 각 Job은 warmup 60초를 결과에서 제외하고, 측정 300초만 집계합니다.
   측정 오류율 gate는 엄격히 `< 1%`입니다. 1% 이상이거나 Job/summary 검증이 실패하면
   다음 단계와 비교 실행으로 진행하지 않고 해당 실행을 실패로 기록합니다.
   `open` 모드에는 게이트가 하나 더 있습니다 — 아래 "생성기 한계와 서버 한계의 구분"을
   참조합니다.
3. optimized 비교 직전에는 1절을 다시 수행해 `UserDynamicView` timestamp를 refresh하고
   materialize 성공을 다시 확인합니다. 이후 baseline과 같은 순서로 `24`, `200`을
   실행하고 각 후보 수에서 VU `1/2/4/8`을 반복합니다.
4. baseline과 optimized는 candidate/부하 조건/fixture/model/image resources/warmup/
   measurement가 같은 행끼리만 짝지어 비교합니다. candidate `24`와 `200`을 평균내거나
   합쳐서 개선이라고 주장하지 않습니다. **`closed` 결과와 `open` 결과도 서로 비교하지
   않습니다** — 부하를 인가하는 방식이 달라 같은 양을 재고 있지 않습니다.

### 생성기 한계와 서버 한계의 구분 (`open` 전용)

개루프에서는 부하 생성기의 VU가 모자라 목표 도착률을 못 지키는 경우가 생깁니다. k6는
이를 `dropped_iterations`로 셉니다. **이 값이 0이 아니면 그 측정은 서버가 아니라 부하
생성기의 한계를 잰 것입니다.**

- 워크플로우는 측정 구간(`dropped_iterations{scenario:measure}`)만 봅니다. warmup에서
  생긴 drop은 판정에 섞이지 않습니다.
- 0이 아니면 해당 단계를 `invalid_generator_limited`로 기록하고 실행을 멈춥니다.
  **이는 서버 실패가 아닙니다.** 보고서에는 성능 결과가 아니라
  `invalid; rerun required`로 적고, `max_vus`를 올려 다시 측정합니다.
- **`max_vus`를 올려도 drop이 거의 그대로면 생성기 한계가 아닙니다.** 서버가 이미
  포화되어 지연이 계속 늘어나는 상태라, 어떤 `max_vus`로도 도착률을 따라잡을 수
  없습니다. 이때는 재측정을 반복하지 말고 **그 도착률이 이미 용량을 넘었다**고
  읽습니다. 무릎과 실패 방식은 `drop = 0`인 마지막 측정점에서 판정하고, drop이 난
  구간은 "이 지점 이후로는 측정 불가"로만 기록합니다. 두 경우를 구분하려면
  `max_vus`를 4배 이상 올린 재측정과 원래 측정의 RPS·p50을 나란히 비교합니다 —
  거의 같으면 서버 포화, 뚜렷이 개선되면 생성기 한계였습니다.
- summary에 이 submetric이 아예 없으면 0으로 해석하지 않고 별도로 실패시킵니다.
  없는 값을 정상으로 읽으면 생성기 한계를 서버 용량으로 오인하게 됩니다.

`max_vus` 기본값은 도착률의 4배입니다. 도착률 R을 지연 L초에서 유지하려면 대략 R×L개의
VU가 필요하므로, 기본값은 지연 4초까지 버팁니다. 서버가 그보다 느려지는 구간을 재려면
`max_vus`를 명시적으로 올립니다.

## 3. 원시 증거 보관과 해시

GitHub Actions artifact `rerank-loadtest-${benchmark_label}-c${candidate_count}-${run_id}`의
`runner/raw/`를 보관합니다. sweep의 각 step(폐루프는 VU, 개루프는 도착률)마다 다음을
확보합니다.

- `k6-summary-${job_name}.json`: custom measurement 중앙값(`med`, p50)/p95/p99,
  request 수, status별 count, 오류율을 읽는 원본입니다. k6 summary의 `med` 키가
  p50(중앙값)에 해당합니다.
- `metadata-step-${step}.json`: Job 이름, 생성·완료 UTC, candidate, `load_mode`,
  `vus`/`arrival_rate`, fixture version, benchmark label, serving image digest,
  Git SHA를 확인합니다. `load_mode`가 없거나 기대와 다르면 그 증거는 쓰지 않습니다 —
  어느 방식으로 인가한 부하인지 모르면 수치를 해석할 수 없습니다.
- Prometheus range artifacts: 실제 파일명은
  `prometheus-${query_name}-step-${step}.json`입니다. `phase_p50`, `phase_p95`,
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
`prometheus-range-${query_name}-step-${step}.json`이라는 파일을 만들지 않으며, 실제
경로는 위의 `prometheus-${query_name}-step-${step}.json`입니다.

## 4. Task 8 보고서 작성 스키마

원시 증거가 확보된 Task 8에서만 baseline 및 optimization HTML 보고서를 작성합니다.
한 행은 정확히 하나의 `candidate_count`와 하나의 부하 조건(폐루프 VU 또는 개루프
도착률) 조합이며, 보고서 표는 최소 다음 열을 갖습니다.

| 구분 | 기록값 |
| --- | --- |
| 식별·재현 | benchmark phase, candidate count, `load_mode`, VU 또는 도착률, Job, UTC range, fixture/model/image digest/Git SHA/resources, artifact URL/hash, Prometheus query/time range |
| k6 측정 | custom measurement 중앙값(`med`, p50)/p95/p99, request count, status count, error count, `RPS = custom request count / 300` |
| Prometheus phase·outcome | phase p95, outcome rate, in-flight max |
| 리소스 | CPU, RSS, CFS throttling ratio (0~1), `CPU-seconds/request = CPU seconds rate / RPS` |

각 행의 caption에는 Job, UTC range, fixture/model/image/SHA/resources, artifact URL/hash,
Prometheus query/time range를 모두 적습니다. 수정된 workflow에서는 원시 query가
없거나 빈 series이면 run 자체가 실패하므로 성능 결과로 보고하지 않습니다. PR #499
이전 artifact처럼 이미 성공으로 저장된 빈 query는 `invalid; rerun required`로
표시합니다. `N/A` 또는 invalid는 개선 근거가 아니며, 측정되지 않은 개선
수치·감소율·비용 절감 수치를 채우거나 추정해서는 안 됩니다.

## 5. 개루프 결과로 안정성을 판정하는 방법

`open` 실행의 목적은 "빠른가"가 아니라 **"한계를 넘겼을 때 예측 가능하게 실패하는가"**
입니다. 도착률 순으로 행을 늘어놓고 다음을 읽습니다.

**한계점(무릎).** 도착률을 올려도 `RPS`가 더 이상 따라 오르지 않는 첫 지점입니다.
그 아래까지가 인스턴스당 안정 용량입니다. `RPS`는 인가한 도착률이 아니라 측정된
request count에서 계산해야 합니다 — 둘이 벌어지는 것 자체가 포화 신호입니다.

**실패 방식.** 한계를 넘긴 행에서 다음 두 갈래 중 어느 쪽인지 판정합니다.

| 관측 | 해석 |
| --- | --- |
| 초과분이 `503`으로 거절되고 나머지 요청의 p95가 유지됨 | 부하 차단이 동작함 — 예측 가능한 실패 |
| 오류율은 0%인데 p95·p99만 폭증하고 in-flight가 계속 증가 | 부하 차단 없음 — 대기열이 무한히 쌓이는 상태 |

**p95/p50 비율.** 한계 직전에는 지연이 이중 분포가 되어 p50은 정상인데 p95만 튑니다.
**p50·평균만 보는 대시보드는 이 상태를 정상으로 읽습니다.** 비율이 급등하는 지점을
기록하고, p50 절대값이 무너지는 지점과 함께 남깁니다. 둘은 서로를 대체하지 않습니다.

**회복.** 한계를 넘긴 실행 다음에 낮은 도착률로 한 번 더 재서, 지연이 이전 수준으로
돌아오는지 확인합니다. 돌아오지 않으면 과부하가 영구적 열화를 남긴 것이므로 별도
결함으로 기록합니다.

측정된 행이 없으면 이 판정을 쓰지 않습니다. `invalid; rerun required`인 행은 서버
거동의 근거가 아닙니다.
