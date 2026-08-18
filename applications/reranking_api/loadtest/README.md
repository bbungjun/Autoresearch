# rerank k6 부하 측정

`rerank.js`는 dev GKE 내부에서 `/rerank`의 실제 Feature Store 경로를
측정합니다. 기본 대상은
`http://autoresearch-serving.autoresearch.svc.cluster.local:8000`이며, 요청마다
HTTP 200, 후보 수·입력 순서, 단일 비어 있지 않은 model ID, 유한 CTR score를
검증합니다.

시작 전 고정 fixture를 BigQuery에 적재하고 `feast_online_store_materialize`가
성공했는지 확인해야 합니다. 스크립트의 `setup()`은 시나리오 실행 전에 후보 24개와
200개 canary를 모두 호출합니다. 어느 하나라도 계약을 만족하지 않으면 warmup과
측정을 시작하지 않습니다.

## 입력과 허용값

| 환경 변수 | 기본값 | 계약 |
| --- | --- | --- |
| `BASE_URL` | ClusterIP serving URL | `/rerank` 대상 |
| `CANDIDATE_COUNT` | `24` | `24` 또는 `200` |
| `LOAD_MODE` | `closed` | `closed`(동시 VU 고정) 또는 `open`(도착률 고정) |
| `VUS` | `1` | `closed`에서 `1`, `2`, `4`, `8` 중 하나 |
| `ARRIVAL_RATE` | 없음 | `open`에서 필수. 초당 도착률, 1~2000 |
| `MAX_VUS` | 도착률의 4배 | `open`의 생성기 VU 상한 |
| `WARMUP_SECONDS` | `60` | 양의 정수, 결과 제외 |
| `MEASURE_SECONDS` | `300` | 양의 정수, 측정 구간 |
| `FIXTURE_VERSION` | `rerank-v1` | summary 메타데이터 |
| `BENCHMARK_LABEL` | `baseline` | summary 메타데이터 |
| `SERVING_IMAGE_REF` | `unknown` | summary 메타데이터 |
| `SERVING_GIT_SHA` | `unknown` | summary 메타데이터 |

시나리오는 동일 VU의 `constant-vus` warmup 뒤에 같은 VU의 측정 구간을 지연
시작합니다. `rerank_measure_duration_seconds`,
`rerank_measure_requests`, `rerank_measure_failure`,
`rerank_measure_status_code_200`, `_422`, `_500`, `_503`, `_other`는 측정
구간에서만 기록합니다. duration Trend에는 k6 HTTP timing의 milliseconds를 1,000으로
나눈 **seconds 숫자**를 기록하며, time Trend flag는 사용하지 않습니다. 따라서 raw
summary에서 상태별 request count와 seconds 단위 중앙값(`med`, p50)/p95/p99를 태그
해석 없이 읽을 수 있습니다. `summaryTrendStats`는 k6의 전역 Trend 설정이므로
`rerank_measure_duration_seconds`뿐 아니라 `http_req_duration` 같은 내장 Trend에도
동일한 통계 키가 보존됩니다. 이는 raw artifact의 진단 범위를 넓히기 위한 의도된
동작이며, 오류율 threshold는 `rerank_measure_failure < 0.01`입니다.

## 폐루프와 개루프

기본 `closed`는 `constant-vus`로 동시 VU를 고정합니다. VU가 응답을 받아야 다음 요청을
보내므로 서버 안의 동시 요청 수가 VU 수를 넘지 못하고, 도착률이 곧 처리량이 됩니다.
따라서 **과부하 상태를 만들 수 없어** 대기열 무한 증가·부하 차단 부재 같은 결함을
관측하지 못합니다. 같은 동시성에서의 개선 전후 A/B 비교에만 씁니다.

`open`은 `constant-arrival-rate`로 초당 도착률을 고정합니다. 서버가 느려져도 부하가
줄지 않으므로 처리 용량을 넘기는 상태를 실제로 만들 수 있습니다. 한계점과 과부하
거동을 재려면 이 모드를 씁니다.

`open`에서 부하 생성기가 목표 도착률을 못 지키면 k6가 `dropped_iterations`로 셉니다.
그 측정은 서버가 아니라 생성기의 한계를 잰 것이므로, warmup을 섞지 않도록
`dropped_iterations{scenario:measure}` submetric으로 노출합니다. 이 값에 대한 threshold는
항상 참이며 **판정은 workflow가 합니다** — 생성기 한계를 k6 실패로 만들면 서버가
무너진 것과 구분되지 않기 때문입니다.

## 정적·구문 검증

배포 Job manifest의 immutable digest pin은 다음 작업에서 처리합니다. 이 작업의
로컬 구문 검증은 계획의 validation image tag를 그대로 사용합니다.

```bash
docker run --rm -v "$PWD/loadtest:/scripts:ro" grafana/k6:0.54.0 inspect /scripts/rerank.js
```

실제 GKE Job은 운영 절차와 격리된 `loadtest` namespace를 통해서만 실행합니다.
로컬에서 실제 부하를 보내지 않습니다. 실행하면 `handleSummary`는 stdout에 아래 두
최상위 키만 가진 하나의 JSON 객체를 출력합니다.

```text
metadata
data.metrics
```
