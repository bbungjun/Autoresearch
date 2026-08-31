# action_logs — YouTube event log(action log) 생성 파이프라인

`VirtualUser`(가상 사용자)와 `TrendingVideo`(YouTube backfill) 두 입력을 근거로, CTR 학습셋의 **원천이 되는 event log(action log)** 를 LLM으로 생성하는 패키지다.

- **위치**: `autoresearch/action_log_generation/`
- **범위**: Phase 1(`historical`) MVP. 추천 서버가 필요한 Phase 2(`online_simulated`)는 범위 밖.
- **SSOT**: [`docs/guides/agent-simulator-spec.md`](../guides/agent-simulator-spec.md)
- **설계 근거**: [`docs/archive/specs/2026-07-06-event-log-long-format-design.md`](../archive/specs/2026-07-06-event-log-long-format-design.md)

> 전체 파이프라인에서의 위치: `persona → virtual_users → **user action logs(event log)** → (다음 레이어) CTR training dataset → 개인별 reranking ML`

---

## 1. 핵심 개념 (먼저 이것부터)

**① long 이벤트 스트림 — 한 row = 한 이벤트**
`event_type ∈ {impression, click, view, like}`. 노출마다 `impression` 1행이 남고, "클릭"으로 선정된 노출에만 `click → view (→ like)` 행이 추가된다.

**② 라벨(`clicked`)을 로그에 저장하지 않는다**
`clicked`는 event log에 없다. downstream 학습셋 빌더가 `impression LEFT JOIN click`으로 **파생**한다. 로그에 라벨을 박으면 (a) 새 피처를 못 만들고 (b) label leakage가 구조적으로 발생하기 때문. → event log는 라벨/피처가 아니라 **그것들을 파생할 raw 로그**다.

**③ 이벤트별 의미**
| 이벤트 | 의미 | watch_time_sec |
|---|---|---|
| `impression` | 노출됨(추천/결과에 떴다) | `null` |
| `click` | 클릭 발생 (라벨 아님, raw 사건) | `null` |
| `view` | **실제 시청**(클릭 후 재생) | **값 있음** |
| `like` | 좋아요 발생 | `null` |

**④ LLM은 판단, 코드는 조립**
LLM은 (유저 × 후보영상)마다 `click_propensity`/`watch_fraction`만 판단한다(토큰 절감을 위해 인덱스 포맷 `{"j": [[idx, cp, wf], ...]}` — `idx`는 후보의 0-base 위치라 재정렬에도 재결합 가능). `would_like`는 LLM이 출력하지 않고 코드가 `derive_would_like`로 파생한다. **클릭 여부는 코드가 유저(슬레이트)별로 결정**한다 — 유저별 노출 중 `click_propensity` 최고 1건이 커트라인(`click_threshold`) 이상이면 그 1건만 클릭으로 선정하고, 미만이면 그 유저는 클릭 0건이다. 전역 할당량이 아니라 관련성 커트라인이므로 **CTR은 더 이상 코드가 강제하는 값이 아니라 모델 판단 품질에 따라 창발(emergent)하는 결과 지표**다.

---

## 2. 모듈 구조 & 역할

| 모듈 | 역할 | 핵심 심볼 |
|---|---|---|
| `schema.py` | 데이터 계약(pydantic). 저장 스키마·중간 산출물·요청/결과. | `EventLog`, `ImpressionDraft`, `EventGenerationRequest`, `QuarantineRecord`, `EventLogBatch`, `EventGenerationResult` |
| `video_source.py` | KR TrendingVideo parquet 로드 → 정규 `VideoRecord` dict(`video_id` dedup). 영상 길이 컬럼이 없어 결정론적 근사값 제공. | `load_video_records`, `nominal_duration_sec`, `build_fixture_video_records` |
| `candidate.py` | 유저별 노출 batch 구성 — 관련(키워드 겹침 상위) + exploration(랜덤) 혼합. | `build_candidates` |
| `llm_generator.py` | 프롬프트 구성 + 후보별 판정(judgments) 생성. 테스트용 결정론 fixture와 실서비스 OpenRouter 두 구현. | `build_action_log_prompt`, `RuleBasedActionLogGenerator`, `OpenRouterActionLogGenerator` |
| `pipeline.py` | 오케스트레이션 — 유저 단위 격리 생성 → 유저별 최고 1건 + 관련성 커트라인 선정 → 이벤트 확장 → parquet/warehouse/quarantine 저장 + 실패 가드. | `generate_action_log_batch`, `_expand_events`, `EVENT_LOG_PARQUET_SCHEMA` |

> `__init__.py`는 비어 있다. 서브모듈에서 직접 import 한다.
> [`../archive/reports/action-log-qa-리포트.md`](../archive/reports/action-log-qa-리포트.md) — 실측 QA 리포트(모델별 결과).

---

## 3. 데이터 흐름

```
virtual_users(parquet) ───┐
                          ├─►  generate_action_log_batch(request, users, videos, generator)   [pipeline.py]
KR TrendingVideo(parquet) ┘         (video_source.load_video_records 로 videos 로드)
        │
        ▼   ── 유저 단위 격리 루프 ──  _generate_drafts_isolated
        │     ├ candidate.build_candidates(user, videos)      # 유저당 24후보 (관련+exploration), seed 고정
        │     ├ generator.generate(user, candidates) → raw JSON
        │     │     # LLM: {"j": [[index, click_propensity, watch_fraction], ...]}  (index로 후보 재결합)
        │     └ _build_user_drafts(raw) → [ImpressionDraft ...]  # would_like는 여기서 코드 파생
        │           # 파싱 실패 시 해당 유저만 quarantine (배치는 계속)
        ▼
  select_clicks_per_slate(drafts, click_threshold)   # 유저별 최고 1건이 커트라인 이상이면 "클릭" 선정
        │
        ▼
  _expand_events(drafts, clicked, request)      # 노출→impression 1행, 클릭분→click/view(+like), timestamp 배치
        │
        ▼
  EventLogBatch  ──►  max_quarantine_ratio 초과 시 ActionLogGenerationError (전량/대량 실패 가드)
        │
        ├─►  _write_event_log_parquet        →  asset/action_log/event_log.parquet
        ├─►  write_event_log_warehouse_jsonl →  data/generated/event_log.jsonl
        └─►  write_quarantine_jsonl          →  data/generated/event_log_quarantine.jsonl
```

**단계 요약**
1. **후보 구성**(`candidate.py`) — 유저 관심 키워드 ↔ 영상 title/tags/description 토큰 겹침으로 관련 후보를 뽑고, exploration 비율만큼 랜덤을 섞는다. exposure_type 라벨은 로그에 남기지 않는다.
2. **LLM 판정**(`llm_generator.py`) — 유저 1명 × 후보 batch를 실제 영상 텍스트 근거로 읽어 후보별 propensity/watch_fraction/would_like를 반환(유저당 1콜).
3. **유저별 최고 1건 + 관련성 커트라인**(`pipeline.select_clicks_per_slate`) — 유저(슬레이트)별로 `click_propensity` 최고 1건만 후보로 삼아, 그 값이 `click_threshold` 이상이면 클릭으로 선정(미만이면 그 유저는 클릭 0건). tie-break: `(-click_propensity, video_id)`로 결정론적. 전역 할당량이 아니므로 CTR은 점수 분포에 따라 창발한다.
4. **이벤트 확장**(`pipeline._expand_events`) — 노출마다 `impression`, 클릭 선정분엔 `click → view (→ would_like면 like)`를 timestamp 단조 증가로 배치.
5. **저장 + 가드** — parquet/warehouse/quarantine 기록. 격리 비율이 `max_quarantine_ratio`를 넘으면 조용한 빈 결과 대신 예외로 실패.

---

## 4. events 스키마 명세 (저장 산출물)

### 4.1 parquet 컬럼 (`EVENT_LOG_PARQUET_SCHEMA`)

현재 final Parquet은 **18개 nullable Arrow field**를 가진다. legacy reader는 필수
필드와 타입을 유지하면서 알려진 optional additive field의 부분집합만 허용한다.

| 컬럼 | 타입(Arrow) | 규칙 |
|---|---|---|
| `event_id` | string | 고유. 예: `evt_00000000` (배치 내 순번) |
| `event_timestamp` | timestamp(us, UTC) | 이벤트 발생 시각 |
| `user_id` | string | VirtualUser (FK) |
| `event_type` | string | `impression` / `click` / `view` / `like` |
| `video_id` | string | 노출/시청 영상 (FK) |
| `watch_time_sec` | int64 (nullable) | **`view`일 때만** 값(≥0), 그 외 `null` |
| `rank` | int64 (nullable) | **Phase 1은 항상 `null`** (추천 순위 없음) |
| `source` | string | `historical` (Phase 1 고정) |
| `policy`, `ctr_score`, `is_exploration`, `policy_version` | string/double/bool/string (nullable) | policy·노출 lineage 호환 필드 |
| `exposure_source` | string (nullable) | `model` / `trending` / `random` 노출 출처. 과거 parquet에는 없을 수 있음 |
| `slate_id` | string (nullable) | 일일 producer가 생성한 exposure-group 식별자. §4.4 참조 |
| `schema_version` | string | `action_log_schema_v1` |
| `prompt_version` | string | `action_log_ctr_v4` |
| `llm_model` | string | 생성 모델명 (예: `mistralai/mistral-nemo`) |
| `generated_at` | string | 배치 생성 시각(ISO) |

- **PK**: `event_id`. `(user_id, video_id)`는 **non-unique FK** — 한 (유저, 영상) 쌍이 `impression`/`click`/`view`/`like` 최대 4행을 가질 수 있다.
- **Warehouse jsonl**(`EventLog.to_warehouse_row`)은 event·user·video·watch·rank·source와
  policy/exposure lineage 및 `slate_id`를 flat하게 담고 timestamp는 ISO 문자열로 직렬화한다.

### 4.2 검증 규칙 (`EventLog`)
- `watch_time_only_for_view`: `event_type == "view"` → `watch_time_sec`는 non-null(≥0). 그 외 이벤트 → `watch_time_sec is None` 강제. 위반 시 `ValidationError`.

### 4.3 이벤트 생성 규칙 (`_expand_events`)
- **항상**: 노출 1건 → `impression` 1행 (`watch_time_sec=None`, `rank=None`, `source=historical`).
- **클릭 선정분에만 추가**:
  - `click` 1행
  - `view` 1행 — `watch_time_sec = max(1, round(watch_fraction × duration_sec))`
  - `like` 1행 — `would_like=true`일 때만 (코드 파생: `derive_would_like`)
- **timestamp**: 같은 (user, video) 세션은 `impression < click < view < like`로 단조 증가.
- **일일 상한**(`max_events_per_user_per_day`): **impression 기준**으로만 적용(파생 click/view/like는 같은 노출의 후속이라 상한에 안 셈). → "하루에 노출 몇 개"를 제한하는 의미.
- **window**: 모든 이벤트가 `[history_end - history_days일, history_end]` 안. impression을 `history_end`보다 최소 `_MIN_IMPRESSION_HOURS`시간 이전에 배치해 후속 세션 이벤트가 `history_end`를 넘지 않도록 보장(→ 5.2 참고).

### 4.4 일일 `slate_id` 계약 (Stage A 구현 완료)

`slate_id`는 한 사용자의 한 번에 확정된 후보 묶음을 producer 시점에 식별한다. 형식은
`slt_<YYYYMMDD>_<24 lowercase hex>`이며, `action-log-slate-v1` canonical JSON의
SHA-256 앞 96 bit다. canonical identity는 KST `partition_date`, producer
`daily-action-log-v1`, user와 정렬된 member(`video_id`, nullable rank/source/policy
version)를 포함하고 shard·worker·event 순서는 제외한다.

- `run_daily_action_log`와 `run_daily_action_log_shard`/`merge_daily_action_log_shards`는
  `SlateGenerationContext(partition_date, producer)`를 명시해 ID를 만든다. 같은 slate의
  impression·click·view·like는 같은 ID를 가진다.
- context 없는 범용 생성과 30일 policy simulation은 자동 추론하지 않고 `slate_id=null`을
  남긴다. 이 결과는 P0-1 cutover 이후 평가 원천이 아니다.
- daily context는 `history_days == 1` 및 `max_events_per_user_per_day >=
  candidates_per_user`를 요구한다. non-null `exposure_source`에 rank가 없거나 1 미만이면
  typed failure로 final publish 전에 중단한다. 서로 다른 canonical payload가 같은
  truncated ID가 되면 run-local collision registry가 생성 전체를 실패시킨다.
- `slate_id`는 additive nullable final event/warehouse/spool field다. 기존 parquet의
  column 부재와 null은 legacy 호환으로 읽되, schema version은 `action_log_schema_v1`을
  유지한다. draft/checkpoint Arrow schema에는 저장하지 않으며 shard merge에서 context로
  다시 계산한다. cutover fail-closed는 아직 구현하지 않은 snapshot builder(Stage B)의 책임이다.

---

## 5. 보조 데이터 계약 & 규칙

### 5.1 `ImpressionDraft` (중간 산출물, 저장 안 됨)
LLM 판정 결과 1건 = 후보(노출) 1건 = `impression` 1행에 대응.

| 필드 | 타입 | 비고 |
|---|---|---|
| `user_id`, `video_id` | str | |
| `click_propensity` | float [0,1] | 유저별 최고 1건 + 커트라인 판정용 |
| `watch_fraction` | float [0,1] | view watch_time 산출용 |
| `would_like` | bool | like 생성 여부. 코드 파생(`derive_would_like`: cp≥0.7 & wf≥0.6) |
| `duration_sec` | int ≥1 | `nominal_duration_sec(video_id)` (60~900s, 결정론) |

### 5.2 타임스탬프 헤드룸(window ↔ `_MAX_DURATION` 결합, 명시적)
`pipeline.py` 모듈 상수로 결합을 코드에 드러냈다.
- `_CLICK_DELAY_MAX_SEC = 30`, `_VIEW_DELAY_MAX_SEC = 5`
- `_MAX_SESSION_SPAN_SEC = 30 + 5 + max(2, _MAX_DURATION)` — 세션이 impression 뒤로 늘어날 수 있는 최대 초
- `_MIN_IMPRESSION_HOURS = max(1, ceil(_MAX_SESSION_SPAN_SEC / 3600))` — impression을 end에서 최소 이만큼 이전에 배치
- 런타임 가드 `assert last_ts <= end` — `_MAX_DURATION`을 키우면 여유가 자동으로 늘고, 결합이 깨지면 조기 실패.

### 5.3 `EventGenerationRequest` (실행 파라미터)
| 파라미터 | 기본값 | 의미 |
|---|---|---|
| `click_threshold` | 없음(필수, #260) | 유저별 최고 `click_propensity`가 이 값 이상이어야 클릭으로 선정(관련성 커트라인). 미지정 시 조용히 0.55로 채우지 않고 명시적으로 실패한다 |
| `candidates_per_user` | `24` | 유저당 노출 후보 수 |
| `exploration_ratio` | `0.2` | 후보 중 exploration(랜덤) 비율 |
| `history_days` | `30` | historical window 길이(일) |
| `slate_context` | `null` | daily producer 전용 `partition_date`와 `daily-action-log-v1` producer. 있을 때 1일 history와 충분한 impression capacity를 강제 |
| `history_end` | now(UTC) | window 끝 시각 |
| `max_events_per_user_per_day` | `8` | 유저별 **일일 impression 상한** |
| `seed` | `42` | 재현성 시드 |
| `max_quarantine_ratio` | `0.5` | 격리 비율 초과 시 배치 실패 |
| `output_path` | `asset/action_log/event_log.parquet` | parquet 경로 |
| `warehouse_output_path` | `data/generated/event_log.jsonl` | warehouse jsonl |
| `quarantine_output_path` | `data/generated/event_log_quarantine.jsonl` | 격리 jsonl |

### 5.4 결과 요약 (`EventLogBatch.summary` / `EventGenerationResult.summary`)
`{total_events, impressions, clicks, ctr}` + 결과 레벨엔 `{quarantined_users, api_error, invalid_json, schema_fail}`. **CTR = clicks / impressions**이며, 더 이상 코드가 강제하는 목표값이 아니라 `click_threshold` 커트라인과 모델 판단 품질에 따라 창발하는 결과 지표다.

---

## 6. 장애 격리 & quarantine

- **유저 단위 격리**: 한 유저의 LLM 실패가 배치 전체를 죽이지 않는다. 실패 유저는 `QuarantineRecord`로 격리되고 나머지는 계속.
- **응답 교정 재시도**: OpenRouter 응답이 `invalid_json` 또는 `schema_fail`이면
  complete JSON skeleton이 포함된 교정 prompt로 같은 청크를 최대 1회 다시
  생성한다. 두 번째 응답도 실패할 때만 최종 응답을 quarantine한다. API/transport
  오류는 이 경로가 아니라 기존 HTTP retry 정책을 따른다.
- **error_type 3종 & 예외 순서**(load-bearing):
  - `generator.generate(...)` 예외 → `api_error`
  - `json.JSONDecodeError` → `invalid_json`
  - `(ValidationError, ValueError, KeyError, TypeError, AttributeError)` → `schema_fail`
- **전량/대량 실패 가드**: 격리 비율이 `max_quarantine_ratio`(기본 0.5)를 넘으면 quarantine 파일을 남기고 `ActionLogGenerationError`를 raise(조용한 빈 성공 방지).

---

## 7. 실행 방법

### 7.1 테스트/결정론 (LLM 없이)
```python
from autoresearch.action_logs.pipeline import generate_action_log_batch
from autoresearch.action_logs.llm_generator import RuleBasedActionLogGenerator
from autoresearch.action_logs.schema import EventGenerationRequest

result = generate_action_log_batch(
    EventGenerationRequest(), users, videos, RuleBasedActionLogGenerator()
)
print(result.summary)   # {'total_events':..., 'impressions':..., 'clicks':..., 'ctr':...}
```

### 7.2 실서비스 (OpenRouter LLM)
```python
from autoresearch.action_logs.llm_generator import OpenRouterActionLogGenerator
gen = OpenRouterActionLogGenerator(model_name="mistralai/mistral-nemo")  # OPENROUTER_API_KEY 필요
result = generate_action_log_batch(EventGenerationRequest(...paths...), users, videos, gen)
gen.close()
```
- generator는 worker thread별 OpenAI client/HTTP connection pool을 재사용하며 daily runner가
  lifecycle 종료 시 모두 닫는다. SDK 내장 retry는 비활성화하고 408/429/502/503/504와
  `APITimeoutError`만 `Retry-After` + exponential backoff + jitter로 제한 재시도한다.
  timeout/retry는 `OPENROUTER_TIMEOUT_SEC`, `OPENROUTER_MAX_RETRIES`,
  `OPENROUTER_TIMEOUT_MAX_RETRIES`, `OPENROUTER_RETRY_BACKOFF_BASE_SEC`,
  `OPENROUTER_RETRY_BACKOFF_MAX_SEC`로 설정한다. timeout 재시도는 비용 제어를 위해
  기본 1회이며, `OPENROUTER_MAX_RETRIES`의 요청 전체 재시도 상한 안에서 추가로
  제한한다.
- `OPENROUTER_PROVIDER_SORT`, `OPENROUTER_ALLOW_FALLBACKS`,
  `OPENROUTER_REQUIRE_PARAMETERS`는 선택 설정이다. 비워 두면 기존 OpenRouter routing
  기본값을 유지하며 특정 provider나 `:nitro`를 강제하지 않는다.
- 400/401/402/403 및 성공 응답의 JSON/schema 오류는 동일 요청을 재시도하지 않는다.
  최종 API 실패 로그/예외에는 status, error type, provider, attempts만 남긴다.

### 7.4 입력 포맷 (중요 — 두 입력의 소비 형태가 다르다)
`generate_action_log_batch`는 `virtual_users: list[dict]`, `videos: list[dict]`를 **받기만** 하고 로드는 호출자 몫이다. 각 입력의 소비-준비 포맷이 다르다:

| 입력 | 정본 저장 | 로더 |
|---|---|---|
| **videos** | KR TrendingVideo parquet(`data/raw/youtube/*.parquet`) | `video_source.load_video_records(path)` |
| **virtual_users** | parquet(`asset/virtual_user/*.parquet`, `VIRTUAL_USERS_PARQUET_SCHEMA`) | `pq.read_table(path).to_pylist()` |

> virtual_users parquet은 **`user_id` 컬럼**을 쓰고(action_logs가 기대하는 키와 동일), affinity성
> 필드(`category_affinity`/`category_evidence`/`shorts_affinity`/`longform_affinity`)를 담지 않는다
> (그런 점수는 feature engineering 단계 소관). 그래서 `pq.read_table(path).to_pylist()` 결과를
> 별도 어댑터 없이 그대로 `generate_action_log_batch`의 `virtual_users` 인자로 넣을 수 있다.
> candidate 관련도는 `primary_categories`/`interest_keywords` 등으로 계산한다.

### 7.5 공개 batch 실행 명령

Airflow를 포함한 외부 실행자는 `action_logs` 내부 함수를 import하지 않고 다음
module 명령만 호출한다.

```bash
python -m autoresearch.jobs.action_log --mode single \
  --partition-date 2026-07-13 \
  --youtube-base-path gs://<bucket>/data_lake/youtube_trending_kr \
  --virtual-users-path gs://<bucket>/asset/virtual_user/vu_1000.parquet \
  --output-base-path gs://<bucket>/data_lake/action_log

python -m autoresearch.jobs.action_log --mode shard \
  --partition-date 2026-07-13 \
  --youtube-base-path gs://<bucket>/data_lake/youtube_trending_kr \
  --virtual-users-path gs://<bucket>/asset/virtual_user/vu_1000.parquet \
  --output-base-path gs://<bucket>/data_lake/action_log_work \
  --progress-base-path gs://<bucket>/data_lake/action_log_progress \
  --checkpoint-base-path gs://<bucket>/data_lake/action_log_checkpoints \
  --shard-index 0 --shard-count 5

python -m autoresearch.jobs.action_log --mode merge \
  --partition-date 2026-07-13 \
  --shard-output-base-path gs://<bucket>/data_lake/action_log_work \
  --output-base-path gs://<bucket>/data_lake/action_log \
  --shard-count 5 --max-quarantine-ratio 0.2
```

stdout은 JSON Lines이며 마지막 event는 `job_summary`다. exit 0은 성공 또는
명시적 skip, exit 1은 runtime·입력 데이터·schema·품질 실패, exit 2는 CLI
문법·범위·조합 오류다. 세 후보 구성 비율의 합은 절대 허용 오차 `1e-9` 안에서
1이어야 한다.

`--quarantine-base-path`는 single과 shard의 선택적 QA 산출물이다. 상세 격리
JSONL 게시 실패는 `quarantine_publish_failed` warning을 남기지만 정상 Parquet을
실패시키지 않는다. merge의 전역 격리 비율은 shard manifest 집계만 사용한다.
실패 유형 집계가 없는 구형 manifest의 격리 건수는
`unclassified_quarantine_count`로 분리하고
`quarantine_error_counts_unavailable` warning을 출력한다.

기존 final이 있고 `--overwrite`가 없으면 skip한다. overwrite 실행도 기존 final을
미리 삭제하지 않으며, 새 결과의 생성·schema·품질 검증이 끝난 뒤 마지막 단계에서
교체한다. shard 0을 포함한 모든 shard는 자신의 draft·manifest·checkpoint·progress만
관리하고 final 삭제 같은 특수 역할을 갖지 않는다.

내부 Python 함수의 기존 기본 재생성 동작은 호환성을 위해 유지한다. 공개 CLI는
`--overwrite` 값을 항상 명시적으로 전달하므로 플래그가 없으면 위 skip 계약을
따른다.

### 7.6 공개 batch 실행과 외부 Airflow 오케스트레이션

이 저장소는 `python -m autoresearch.jobs.action_log` 공개 명령과 아래 데이터
계약을 소유한다. schedule, Sensor, shard task dependency, retry와 KPO 설정은
[`Autoresearch-airflow`](https://github.com/SKYAHO/Autoresearch-airflow)가
소유하고, `Dockerfile.app`으로 발행한 immutable image digest에서 공개 명령을
실행한다.

```text
입력 영상: gs://<YOUTUBE_LAKE_BUCKET>/data_lake/youtube_trending_kr/dt=YYYY-MM-DD/part-0.parquet
입력 유저: gs://<YOUTUBE_LAKE_BUCKET>/asset/virtual_user/vu_1000.parquet
출력 로그: gs://<YOUTUBE_LAKE_BUCKET>/data_lake/action_log/dt=YYYY-MM-DD/part-0.parquet
Shard 진행률: gs://<YOUTUBE_LAKE_BUCKET>/data_lake/action_log_progress/dt=YYYY-MM-DD/shard=NNN/progress.json
Shard checkpoint: gs://<YOUTUBE_LAKE_BUCKET>/data_lake/action_log_checkpoints/dt=YYYY-MM-DD/shard=NNN/fingerprint=<SHA256>/
Shard manifest: gs://<YOUTUBE_LAKE_BUCKET>/data_lake/action_log_work/dt=YYYY-MM-DD/shard=NNN/manifest.json
```

기본 후보 믹스는 유저당 24개 노출 기준 `70% personalized / 20% popular /
10% exploration`이다. 기본 generator는 `rule_based`이며, `ACTION_LOG_GENERATOR=openrouter`
로 설정하면 `OpenRouterActionLogGenerator`를 사용한다.

Shard 모드(`run_daily_action_log_shard`)는 draft 생성 중 진행률을 logger와
`progress.json`으로 주기적으로 갱신한다. 공개 CLI의 stdout은 JSON event 전용이다.
기본 progress root는 shard work 출력 root 옆의 `action_log_progress`이며,
progress 기록 실패는 경고만 남기고 shard 생성은 계속한다. `progress.json`은
덮어쓸 수 있는 관측용 snapshot일 뿐 재개 checkpoint가 아니다.

구조화 telemetry는 logger를 통해 남긴다. `event`로
`openrouter_retry_scheduled`, `openrouter_attempt_complete`,
`openrouter_request_complete`, `action_log_micro_work_complete`,
`action_log_shard_progress`를 구분한다. micro work의
`queue_wait_ms`, OpenRouter request/attempt/retry/backoff, JSON parse·schema validation,
checkpoint parquet write/rows, progress write, 다음 work submit, 전체 elapsed를 각각
기록한다. shard 집계에는 completed/total/failed/active/pending, throughput/min,
latency p50/p95, ETA가 포함된다. token·reasoning·reported cost는 OpenRouter 응답에
존재할 때만 기록한다.

기본적으로 전체 work가 100개 이하면 work별 상세 로그를 남긴다. 그보다 큰 실행은
retry/error를 제외한 request별 로그를 생략하고 15초마다 집계한다.
`ACTION_LOG_TELEMETRY_DETAIL_MAX_WORK`와 `ACTION_LOG_TELEMETRY_INTERVAL_SEC`(10~30초)로
조정할 수 있다. 이 설정은 생성 결과에 영향을 주지 않으므로 checkpoint fingerprint에
포함되지 않는다. API key, prompt, raw response, user/work ID와 persona 필드는 telemetry에
기록하지 않는다. `shard_index=-1`, `work_sequence=-1`은 shard/work context 밖에서
발생한 로그를 뜻한다. telemetry 환경 변수에 숫자가 아니거나 허용 범위를 벗어난 값이
들어오면 shard 생성을 중단하지 않고 기본값으로 대체하며, 원래 값은 로그에 남기지 않는다.

Durable checkpoint는 별도 `action_log_checkpoints` root에 성공한 API work를
immutable parquet part로 즉시 추가한다. config fingerprint에는 생성 설정과 입력
parquet 내용의 SHA-256이 포함되고, work_id는 partition/shard/user/chunk/config
fingerprint로 결정된다. 재실행은 같은 fingerprint의 완료 work를 건너뛴다. 중복
part는 work_id로 dedup하고, 다른 fingerprint의 이전 checkpoint는 별도 namespace에
격리한다. 최종 shard parquet은 checkpoint와 새 성공 결과를 원본 work 순서로
조립한 뒤 `manifest.json`을 마지막에 기록한다.

Shard 단계는 로컬 quarantine 비율만으로 성공 draft를 폐기하지 않는다. merge가
모든 shard manifest의 fingerprint/model/schema/prompt 계약과 완료 여부를 확인한 뒤
`total_work`와 `quarantine_count`를 합산해 전역 `max_quarantine_ratio`를 검증한다.

### 7.7 click_threshold 캘리브레이션 절차 (필수, #260)

5.3에서 본 대로 `click_threshold`는 기본값이 없는 fail-closed 필수값이며,
전역 클릭 할당량이 아니라 유저별 최고 `click_propensity`에 걸리는 관련성
커트라인이다. 임의값을 넣고 그대로 운영 실행하면, LLM이 대부분 후보를 낮게
채점하는 경향 때문에 대다수 유저의 최고값이 그 커트라인 미만이 되어 **클릭
0건(CTR≈0%)의 무의미한 로그**가 나올 위험이 있다. 그래서 **캘리브레이션 없이
운영 실행을 하지 않는다** — 다음 절차로 먼저 목표 CTR에 맞는 추천 커트라인을
구한 뒤에만 `--click-threshold`를 확정해 운영에 반영한다.

**한 번 정하면 끝나는 값이 아니다.** `click_propensity`는 champion 모델이
만드는 노출 후보에 대한 LLM의 판단이라, champion alias가 다른 버전으로
바뀌면 이 분포 자체가 달라진다 — **champion을 교체할 때마다 재캘리브레이션이
필요**하다. 실측(2026-07-26): champion이 v6→v12로 바뀐 뒤 이전 커트라인
0.7을 그대로 쓰면 동일 목표 CTR(2%) 기준 실제 CTR이 0.79%로 급락했다(100유저
실측, v12 기준 추천 커트라인은 0.4~0.5대). 운영 반영값은
`Autoresearch-airflow`의 `AIRFLOW_VAR_ACTION_LOG_CLICK_THRESHOLD` Airflow
Variable이며, 그 값 옆 주석에 마지막 재캘리브레이션 근거를 남긴다. (아래
2단계 예시의 `--target-ctr 0.015`는 이 실측과 무관한 별도 예시 실행값이며,
실제 목표 CTR은 운영 정책에 따라 정한다.)

1. **draft parquet 확보** — 기본(champion) 모델로 폐루프(`exposure_source=model`,
   single/shard 기본값)를 1회 실행한다. `--mode shard`의 출력은 최종
   EventLog가 아니라 `ImpressionDraft` parquet(§7.5 참고, 최종 CTR 정규화는
   merge에서 1회만 수행)이므로, shard 1개만 돌려도 유저별 `click_propensity`
   원본 분포를 담은 draft parquet를 얻을 수 있다. 이 단계의
   `--click-threshold`는 fail-closed 검증을 통과시키기 위한 자리값일 뿐—
   shard 단계는 커트라인을 적용하지 않고 manifest에 기록만 하므로 draft
   내용(유저별 propensity)에는 영향을 주지 않는다.

   ```bash
   python -m autoresearch.jobs.action_log --mode shard \
     --partition-date 2026-07-13 \
     --youtube-base-path gs://<bucket>/data_lake/youtube_trending_kr \
     --virtual-users-path gs://<bucket>/asset/virtual_user/vu_1000.parquet \
     --output-base-path gs://<bucket>/data_lake/action_log_work \
     --progress-base-path gs://<bucket>/data_lake/action_log_progress \
     --checkpoint-base-path gs://<bucket>/data_lake/action_log_checkpoints \
     --shard-index 0 --shard-count 1 \
     --click-threshold 0.5   # 자리값 — draft 내용에 영향 없음
   ```

   draft parquet 경로: `gs://<bucket>/data_lake/action_log_work/dt=2026-07-13/shard=000/part-0.parquet`

2. **추천 커트라인 산출** — 위 draft parquet으로 캘리브레이션 CLI를 실행한다.

   ```bash
   python -m autoresearch.jobs.click_threshold_calibrate \
     --draft-path gs://<bucket>/data_lake/action_log_work/dt=2026-07-13/shard=000/part-0.parquet \
     --target-ctr 0.015
   ```

   stdout JSON 1줄에 `recommended_threshold`(목표 CTR을 달성하는 추천
   커트라인), `achieved_ctr`(그 커트라인에서 실제 달성되는 CTR), `target_ctr`,
   `users`, `impressions`, `per_user_max_quantiles`(유저별 최고
   `click_propensity` 분포 분위수), `sweep`(커트라인 후보별 CTR 스윕 표)가
   담긴다. 실패 시(draft 없음 등) `{"status": "failed", "error_type": ...}`를
   emit하고 exit 1이다.

3. **확정 및 운영 반영** — `recommended_threshold`(필요하면 `achieved_ctr`/
   `sweep`을 보고 조정한 값)를 운영 `--mode single`/`--mode shard` 실행의
   `--click-threshold`로 명시한다. `--mode merge`는 `--click-threshold`를
   받지 않는다(반려) — merge는 각 shard manifest에 이미 기록된 값을 쓴다.

**(부수) 합동 pool 클릭 의미론 (`autoresearch/recommendation/simulate_policy_round.py`)**
정책 A/B 비교 시뮬레이션은 위 daily 운영 파이프라인과 별개로, 두 정책 노출의
합집합에 `select_clicks_per_slate`를 1회만 적용한다 — 유저(슬레이트)별
`click_propensity` 최고 1건이 합집합의 승자다. 승자 영상이 두 정책의 노출에
모두 겹치면 두 정책이 그 클릭을 공유 판정으로 나눠 가지므로, 유저 합계 클릭은
최대 2건(정책별 1건씩)까지 가능하지만 각 (policy, user) 쌍은 여전히 최대
1클릭으로 보장된다.

### 7.3 환경
- Python 3.12 venv에서 실행/테스트. 시스템 python3(3.10)은 프로젝트 실행 불가.
- 테스트: `python -m pytest tests/test_action_logs_pipeline.py`, 린트: `python -m ruff check autoresearch tests`.

---

## 8. 산출물

| 파일 | 내용 |
|---|---|
| `asset/action_log/event_log.parquet` | 명시적 Arrow 스키마 event log(12컬럼) |
| `data/generated/event_log.jsonl` | warehouse 적재용 flat row(도메인 8컬럼) |
| `data/generated/event_log_quarantine.jsonl` | 격리된 유저(원본 + raw 응답 + error) |
| `data_lake/action_log_progress/dt=YYYY-MM-DD/shard=NNN/progress.json` | Shard draft 생성 진행률(status, completed/success/failed/quarantined chunks) |
| `data_lake/action_log_checkpoints/dt=YYYY-MM-DD/shard=NNN/fingerprint=<SHA256>/parts/*.parquet` | 성공 work durable checkpoint(immutable, work_id dedup) |
| `data_lake/action_log_work/dt=YYYY-MM-DD/shard=NNN/manifest.json` | shard/merge config fingerprint와 work/quarantine 계약 |

---

## 9. 범위 밖 / 다음 단계

- **CTR training dataset 빌더** — `impression LEFT JOIN click`로 `clicked` 라벨 파생, view/like/click 집계로 dynamic feature 생성. (다음 레이어)
- **Phase 2**(`online_simulated`) — 추천 서버 연동, `rank`/`exposure_type` 실제값, `session_id`/`request_id`/`query`/`search` 이벤트.
- **action_logs 내 category_affinity 잔여 참조 정리** — candidate/llm_generator가 `category_affinity`를 읽는 코드가 남아 있으나 virtual_user가 더는 제공하지 않아 항상 빈 값이다(무해). 죽은 경로 제거는 후속 정리 대상.
- **스케일** — 100k 규모 병렬/Batch 생성.
- **Research Harness Stage B/C** — snapshot builder의 cutover 검증, multi-day click
  attribution, validation/final split·label 봉인과 Judge handoff는 아직 구현하지 않았다.

---

## 10. Stage A 구현 기록 (2026-08-31)

**문제:** 일일 후보 묶음 경계가 final event log에 남지 않아 평가 slate를 사후에
추정해야 했고, direct·shard 경로 및 legacy/policy 결과의 의미가 혼동될 수 있었다.

**해결:** canonical identity와 explicit daily context를 도입하고 nullable final schema로만
전파했다. shard/checkpoint의 중간 schema와 `event_id`·KST `dt` 의미는 그대로 유지했다.

**결과:** fresh local RuleBased fixture에서 direct와 2-shard merge는 각각 24 row, 4개의
non-null slate를 만들고 전체 projection SHA-256이 일치했다. event ID 고유성, 파생 event
ID 전파, deterministic rerun, malformed rank 시 기존 final SHA-256 보존을 실측했다.
상세 machine-readable 증거는 `.omo/evidence/research-harness-stage-a/task-6.json`에 남긴다.
