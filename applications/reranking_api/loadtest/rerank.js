import http from "k6/http";
import { check } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

const DEFAULT_BASE_URL =
  "http://autoresearch-serving.autoresearch.svc.cluster.local:8000";
const FIXTURE_USER_ID = "loadtest-user-001";
const LAST_VIDEO_ID = "loadtest-video-200";
const CANARY_CANDIDATE_COUNTS = [24, 200];
const ALLOWED_CANDIDATE_COUNTS = new Set(CANARY_CANDIDATE_COUNTS);
const ALLOWED_VUS = new Set([1, 2, 4, 8]);
const CLOSED_LOOP = "closed";
const OPEN_LOOP = "open";
const ALLOWED_LOAD_MODES = new Set([CLOSED_LOOP, OPEN_LOOP]);
const MAX_ARRIVAL_RATE = 2000;
// 도착률 R을 지연 L초에서 유지하려면 대략 R*L개의 VU가 필요하다. 기본 maxVUs를
// 4R로 두면 지연 4초까지는 생성기가 도착률을 지킬 수 있고, 그보다 느려지면
// dropped_iterations로 드러난다. 이는 서버 결과가 아니라 생성기 한계 신호다.
const DEFAULT_MAX_VU_FACTOR = 4;
const MIN_MAX_VUS = 50;

function readPositiveInteger(name, fallback) {
  const raw = __ENV[name] || fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`${name} must be a positive integer; received ${raw}`);
  }
  return value;
}

const baseUrl = (__ENV.BASE_URL || DEFAULT_BASE_URL).replace(/\/$/, "");
const candidateCount = readPositiveInteger("CANDIDATE_COUNT", "24");
const vus = readPositiveInteger("VUS", "1");
const warmupSeconds = readPositiveInteger("WARMUP_SECONDS", "60");
const measureSeconds = readPositiveInteger("MEASURE_SECONDS", "300");
const loadMode = __ENV.LOAD_MODE || CLOSED_LOOP;

if (!ALLOWED_CANDIDATE_COUNTS.has(candidateCount)) {
  throw new Error("CANDIDATE_COUNT must be one of 24 or 200.");
}
if (!ALLOWED_LOAD_MODES.has(loadMode)) {
  throw new Error(`LOAD_MODE must be one of ${CLOSED_LOOP} or ${OPEN_LOOP}.`);
}

const isOpenLoop = loadMode === OPEN_LOOP;

// 폐루프는 VU가 응답을 받아야 다음 요청을 보내므로 동시 요청 수가 VU 수를 넘지
// 못한다. 따라서 도착률이 처리 용량을 넘는 상태를 만들 수 없고, 대기열 무한 증가나
// 부하 차단 부재 같은 과부하 전용 결함을 드러내지 못한다. 한계점·안정성 검증에는
// 개루프를 쓰고, 폐루프는 같은 동시성에서의 개선 전후 비교에만 쓴다.
if (!isOpenLoop && !ALLOWED_VUS.has(vus)) {
  throw new Error("VUS must be one of 1, 2, 4, or 8.");
}

if (isOpenLoop && !__ENV.ARRIVAL_RATE) {
  throw new Error("ARRIVAL_RATE is required when LOAD_MODE is open.");
}

const arrivalRate = isOpenLoop ? readPositiveInteger("ARRIVAL_RATE", "1") : null;
if (isOpenLoop && arrivalRate > MAX_ARRIVAL_RATE) {
  throw new Error(
    `ARRIVAL_RATE must not exceed ${MAX_ARRIVAL_RATE} requests per second.`,
  );
}

const maxVus = isOpenLoop
  ? readPositiveInteger(
      "MAX_VUS",
      String(Math.max(MIN_MAX_VUS, arrivalRate * DEFAULT_MAX_VU_FACTOR)),
    )
  : null;
const preAllocatedVus = isOpenLoop ? Math.min(maxVus, arrivalRate) : null;

if (isOpenLoop && maxVus < arrivalRate) {
  throw new Error(
    "MAX_VUS must be at least ARRIVAL_RATE; otherwise the generator cannot " +
      "sustain one request per second per VU.",
  );
}

const fixtureVersion = __ENV.FIXTURE_VERSION || "rerank-v1";
const benchmarkLabel = __ENV.BENCHMARK_LABEL || "baseline";
const servingImageRef = __ENV.SERVING_IMAGE_REF || "unknown";
const servingGitSha = __ENV.SERVING_GIT_SHA || "unknown";
const allVideoIds = Array.from(
  { length: 200 },
  (_, index) => `loadtest-video-${String(index + 1).padStart(3, "0")}`,
);

if (allVideoIds[allVideoIds.length - 1] !== LAST_VIDEO_ID) {
  throw new Error("The fixed load-test video fixture must contain 200 ordered IDs.");
}

const selectedVideoIds = allVideoIds.slice(0, candidateCount);
const measurementDuration = new Trend("rerank_measure_duration_seconds");
const measurementRequests = new Counter("rerank_measure_requests");
const measurementFailure = new Rate("rerank_measure_failure");
const measurementStatusCode200 = new Counter("rerank_measure_status_code_200");
const measurementStatusCode422 = new Counter("rerank_measure_status_code_422");
const measurementStatusCode500 = new Counter("rerank_measure_status_code_500");
const measurementStatusCode503 = new Counter("rerank_measure_status_code_503");
const measurementStatusCodeOther = new Counter("rerank_measure_status_code_other");

function closedLoopScenarios() {
  return {
    warmup: {
      executor: "constant-vus",
      exec: "warmup",
      vus,
      duration: String(warmupSeconds) + "s",
      gracefulStop: "0s",
    },
    measure: {
      executor: "constant-vus",
      exec: "measure",
      vus,
      startTime: String(warmupSeconds) + "s",
      duration: String(measureSeconds) + "s",
      gracefulStop: "0s",
    },
  };
}

function openLoopScenarios() {
  // 도착률을 고정하면 서버가 느려져도 부하가 줄지 않으므로, 처리 용량을 넘는
  // 상태를 실제로 만들 수 있다. 서버가 못 받아내는 몫은 지연·오류로 나타나고,
  // 생성기가 못 따라가는 몫은 dropped_iterations로 분리되어 나타난다.
  return {
    warmup: {
      executor: "constant-arrival-rate",
      exec: "warmup",
      rate: arrivalRate,
      timeUnit: "1s",
      duration: String(warmupSeconds) + "s",
      preAllocatedVUs: preAllocatedVus,
      maxVUs: maxVus,
      gracefulStop: "0s",
    },
    measure: {
      executor: "constant-arrival-rate",
      exec: "measure",
      rate: arrivalRate,
      timeUnit: "1s",
      startTime: String(warmupSeconds) + "s",
      duration: String(measureSeconds) + "s",
      preAllocatedVUs: preAllocatedVus,
      maxVUs: maxVus,
      gracefulStop: "0s",
    },
  };
}

function buildThresholds() {
  const thresholds = {
    rerank_measure_failure: ["rate<0.01"],
  };
  if (isOpenLoop) {
    // 항상 참인 threshold다. 판정이 아니라 노출이 목적이다 — threshold를 선언해야
    // k6가 이 submetric을 summary에 실어 주고, 그래야 warmup 구간을 섞지 않고
    // 측정 구간의 drop만 읽을 수 있다. drop은 부하 생성기가 도착률을 못 지켰다는
    // 뜻이지 서버가 실패했다는 뜻이 아니므로 k6 실패로 만들지 않는다. 무효 판정은
    // 원시 증거를 모두 보존한 뒤 workflow가 한다.
    thresholds["dropped_iterations{scenario:measure}"] = ["count>=0"];
  }
  return thresholds;
}

export const options = {
  // k6 applies summaryTrendStats to every Trend, including built-in HTTP Trends.
  // The raw artifact intentionally retains these p99 values for custom and
  // built-in metrics alike.
  summaryTrendStats: ["avg", "min", "med", "max", "p(90)", "p(95)", "p(99)"],
  scenarios: isOpenLoop ? openLoopScenarios() : closedLoopScenarios(),
  thresholds: buildThresholds(),
};

function validateResponse(response, requestedVideoIds) {
  let payload;
  try {
    payload = response.json();
  } catch (_) {
    payload = null;
  }

  const items = payload && Array.isArray(payload.items) ? payload.items : [];
  const itemCountIsExact = items.length === requestedVideoIds.length;
  const itemOrderIsExact =
    itemCountIsExact &&
    items.every((item, index) => item && item.video_id === requestedVideoIds[index]);
  const hasOneModelId =
    itemCountIsExact &&
    items.every(
      (item) =>
        item &&
        typeof item.model_id === "string" &&
        item.model_id.trim().length > 0,
    ) &&
    new Set(items.map((item) => item.model_id)).size === 1;
  const scoresAreFinite =
    itemCountIsExact &&
    items.every(
      (item) =>
        item &&
        typeof item.ctr_score === "number" &&
        Number.isFinite(item.ctr_score),
    );

  return {
    httpOk: response.status === 200,
    itemCountIsExact,
    itemOrderIsExact,
    hasOneModelId,
    scoresAreFinite,
  };
}

function recordMeasurementStatus(statusCode) {
  switch (statusCode) {
    case 200:
      measurementStatusCode200.add(1);
      break;
    case 422:
      measurementStatusCode422.add(1);
      break;
    case 500:
      measurementStatusCode500.add(1);
      break;
    case 503:
      measurementStatusCode503.add(1);
      break;
    default:
      measurementStatusCodeOther.add(1);
  }
}

function postAndValidate(requestedVideoIds, recordMeasurement) {
  const response = http.post(
    `${baseUrl}/rerank`,
    JSON.stringify({ user_id: FIXTURE_USER_ID, video_ids: requestedVideoIds }),
    { headers: { "Content-Type": "application/json" } },
  );
  const validation = validateResponse(response, requestedVideoIds);
  const isValid = Object.values(validation).every(Boolean);

  check(response, {
    "rerank returns HTTP 200": () => validation.httpOk,
    "rerank returns the requested item count": () => validation.itemCountIsExact,
    "rerank preserves requested item order": () => validation.itemOrderIsExact,
    "rerank returns one non-empty model ID": () => validation.hasOneModelId,
    "rerank returns finite CTR scores": () => validation.scoresAreFinite,
  });

  if (recordMeasurement) {
    // k6 response timing은 milliseconds다. isTime flag 없는 Trend에는 seconds로 기록한다.
    measurementDuration.add(response.timings.duration / 1000);
    measurementRequests.add(1);
    measurementFailure.add(!isValid);
    recordMeasurementStatus(response.status);
  }

  return isValid;
}

export function setup() {
  const canaryFailures = CANARY_CANDIDATE_COUNTS.filter(
    (count) => !postAndValidate(allVideoIds.slice(0, count), false),
  );
  if (canaryFailures.length > 0) {
    throw new Error(
      `Rerank canary failed for candidate counts: ${canaryFailures.join(", ")}.`,
    );
  }
}

export function warmup() {
  postAndValidate(selectedVideoIds, false);
}

export function measure() {
  postAndValidate(selectedVideoIds, true);
}

export function handleSummary(data) {
  // k6 passes the configured Trend statistics through data.metrics; retain the
  // complete map so the custom measurement p99 is present in the raw artifact.
  return {
    stdout: JSON.stringify({
      metadata: {
        base_url: baseUrl,
        candidate_count: candidateCount,
        load_mode: loadMode,
        vus: isOpenLoop ? null : vus,
        arrival_rate: arrivalRate,
        pre_allocated_vus: preAllocatedVus,
        max_vus: maxVus,
        warmup_seconds: warmupSeconds,
        measure_seconds: measureSeconds,
        fixture_version: fixtureVersion,
        benchmark_label: benchmarkLabel,
        serving_image_ref: servingImageRef,
        serving_git_sha: servingGitSha,
      },
      data: { metrics: data.metrics },
    }),
  };
}
