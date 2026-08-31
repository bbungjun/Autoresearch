# Research Harness P0-1 — 재현 가능한 평가 데이터와 split

> 작성: 2026-08-31 | 상태: Stage A producer 계약 구현, Stage B Task 0 canonical 계약 잠금 완료, Stage B builder/Stage C 구현 대기 | 추적: #17
>
> 상위 계약:
> `docs/specs/2026-08-14-paper-grounded-autonomous-ml-research-harness.md`
>
> 구현 순서:
> `docs/plans/2026-08-15-local-research-harness-mvp.md` Task 1-0, Task 1

## 1. 목적

Research Harness가 baseline과 candidate에 **같은 평가 문제**를 주고, candidate에는
정답을 노출하지 않으며, 같은 입력에서 같은 평가 식별자와 split을 다시 만들 수 있게
한다.

P0-1의 산출물은 다음 두 가지다.

1. action log 생성 시점에 부여된 원천 `slate_id`
2. label-free slate와 Judge 전용 label을 분리한 `EvaluationSlateSnapshot`

이 단계는 모델을 학습하거나 지표를 계산하지 않는다. candidate 실행 계약은 P0-3,
지표·판정은 P0-2 Sealed Judge가 소유한다.

## 2. 현재 사실과 해결할 문제

- action log는 한 행이 한 이벤트인 long format이며 `impression`, `click`, `view`, `like`를
  함께 저장한다.
- 일일 생성 경로는 한 유저의 후보 전체를 한 번에 판정하고 최대 한 건을 클릭으로
  선택하지만, 최종 이벤트에는 그 후보 묶음의 식별자가 없다.
- 현재 click label은 같은 `(user_id, video_id)`의 직전 30분 내 가장 최근 impression에
  click을 귀속해 파생한다.
- 평가 기간 raw action log를 candidate가 읽으면 같은 join으로 숨긴 click label을
  복원할 수 있다.
- 과거 파티션에는 `slate_id`가 없으므로 timestamp·rank로 slate를 사후 추정하면 안 된다.
- 기존 `dt=D` 계약은 KST D일 이벤트만 담는 서로소 일일 슬라이스다. P0-1은 이 파티션
  의미를 바꾸지 않는다.

## 3. 비목표

- NDCG·Recall·ROC-AUC·LogLoss·Brier 계산
- `promote|revise|discard` 판정
- candidate `predictions.csv` 검증
- final holdout 소비 registry
- Kubernetes executor·Workbench·MLflow 배선
- 과거 action log의 `slate_id` 소급 생성
- timestamp나 rank를 이용한 legacy slate 추론

## 4. 전체 데이터 흐름

```text
action log producer
  └─ exposure group 확정 시 slate_id 생성
       └─ dt=D 일일 파티션 저장
            ├─ candidate history: history_start <= dt < T
            └─ snapshot source: T <= dt <= T_end + 1
                 └─ click attribution
                      └─ user hash split
                           ├─ validation/slate.parquet       label 없음
                           ├─ validation/labels.parquet      Judge 전용
                           ├─ final_holdout/slate.parquet    반복 중 미공개
                           ├─ final_holdout/labels.parquet   Judge 전용
                           └─ manifest.json
```

## 5. Action log `slate_id` 계약

### 5.1 의미와 전파 범위

`slate_id`는 **한 사용자에게 한 번에 제시하기로 확정한 후보 묶음**의 식별자다. 생성이
끝난 이벤트를 timestamp로 묶어서 만들지 않는다.

- 같은 slate에서 파생된 impression·click·view·like는 모두 같은 `slate_id`를 가진다.
- 한 slate 안에서 `(user_id, video_id)`는 유일해야 한다.
- 서로 다른 user 또는 후보 묶음은 같은 `slate_id`를 가질 수 없다.
- 일일 historical 생성 경로는 `(partition_date, user_id)`당 slate 하나를 만든다.
- 앞으로 한 user에게 하루 여러 번 노출하려면 producer가 별도 exposure-group key를
  제공해야 한다. timestamp 기반 자동 분할 fallback은 추가하지 않는다.

현행 policy simulation은 한 user의 후보를 기본 30일에 나누어 기록하고 click도 user
전체 후보에서 선택한다. 따라서 일일 historical slate와 같은 의미를 갖지 않는다.
P0-1의 평가 원천은 `run_daily_action_log`와 그 shard merge 결과인 일일 파티션으로
제한한다. policy simulation과 slate context 없는 일반 라이브러리 호출은
`slate_id=None`을 기록하며, 별도의 exposure-group 계약이 생기기 전에는 cutover 이후
평가 원천으로 사용할 수 없다.

### 5.2 저장 스키마와 하위 호환

`EventLog`와 Parquet·warehouse row에 다음 additive 컬럼을 추가한다.

| 필드 | 타입 | 저장 null 허용 | 의미 |
| --- | --- | --- | --- |
| `slate_id` | string | 예 | 생성 시점에 확정된 노출 묶음 ID |

저장 스키마에서는 과거 파티션을 읽기 위해 nullable이다. `slate_id`를
`OPTIONAL_ADDITIVE_COLUMNS`에 포함하고 `ACTION_LOG_SCHEMA_VERSION`은 v1을 유지한다.
새 평가 가능 여부는 schema version이 아니라 §5.5의 cutover 계약으로 판정한다.

### 5.3 생성 입력

일일 생성 경로의 `_expand_events` 호출자는 다음 값을 가진 명시적 slate context를
전달한다.

```text
partition_date    이벤트가 속할 KST 파티션 날짜
producer          daily-action-log-v1
```

identity의 `members`는 해당 user의 `ImpressionDraft`와 노출 메타데이터에서
`video_id`, nullable `rank`(`exposure_rank`), nullable `exposure_source`, nullable
`policy_version`을 뽑아 `(rank is null, rank, video_id)` 순으로 정렬한 목록이다.
`exposure_source`가 non-null이면 `rank`도 1 이상이어야 하며, 불완전한 노출
메타데이터는 생성 단계에서 거부한다. worker·chunk·shard의 처리 순서는 입력에 포함하지
않는다. 일반 라이브러리 호출과 policy simulation이 slate context를 생략하면
`slate_id=None`인 legacy 결과를 만들 수는 있지만, 그 결과는 cutover 이후 평가
파티션으로 사용할 수 없다.

### 5.4 ID 형식과 생성식

ID 형식은 다음과 같다.

```text
slt_<YYYYMMDD>_<24 lowercase hex>
```

마지막 24 hex는 아래 identity를 canonical JSON으로 직렬화한 SHA-256의 앞 96 bit다.

```json
{
  "members": [
    {
      "exposure_source": "trending",
      "policy_version": null,
      "rank": 1,
      "video_id": "video-456"
    }
  ],
  "partition_date": "2026-08-31",
  "producer": "daily-action-log-v1",
  "user_id": "user-123",
  "version": "action-log-slate-v1"
}
```

canonical JSON은 key 정렬, UTF-8, `ensure_ascii=false`, 공백 없는 separator를 사용한다.
출력 순서·worker 수·shard 수·event sequence는 identity에 넣지 않는다. 후보 구성,
rank 또는 policy lineage가 바뀌면 다른 `slate_id`가 되고, 같은 입력의 재실행은 같은
`slate_id`를 만들어야 한다.

96 bit truncation은 식별자 길이를 제한하기 위한 것이며 충돌을 관용한다는 뜻이 아니다.
한 실행에서 같은 ID가 서로 다른 identity를 가리키면 `slate_id_collision`로 전체 생성을
실패시킨다.

### 5.5 Cutover

snapshot builder는 필수 `slate_id_cutover_date`를 받는다.

- `dt < slate_id_cutover_date`: 평가 slate 원천 선택에서 제외한다. candidate history는
  §6의 별도 시간 경계를 따르므로 legacy 파티션을 포함할 수 있다.
- `dt >= slate_id_cutover_date`: 읽은 모든 event row의 `slate_id`가 non-null이어야 한다.
- column 부재와 null 값은 모두 `slate_id_missing_after_cutover`로 실패한다.
- 평가 impression에서 재구성한 canonical identity의 hash와 저장 ID가 다르거나, ID의
  날짜 prefix가 파티션 날짜와 다르면 `slate_id_invalid`로 실패한다.
- 과거 row를 timestamp·rank·user로 묶는 fallback은 없다.
- 운영 cutover는 P0-1 원천인 일일 producer가 `slate_id`를 기록한 첫 완전한 KST
  파티션 날짜다.
- 로컬 fixture의 cutover는 fixture의 첫 생성 `partition_date`다.

## 6. Snapshot 입력 경계

### 6.1 필수 입력

```text
action_log_root
history_start_date
evaluation_start_date T
evaluation_end_date T_end
slate_id_cutover_date
output_root
```

날짜는 KST `YYYY-MM-DD`다. 다음을 만족하지 않으면 `invalid_date_range`다.

```text
history_start_date < T <= T_end
slate_id_cutover_date <= T
```

### 6.2 필요한 파티션

- candidate history: `history_start_date <= dt < T`
- 평가 출력: `T <= impression dt <= T_end`
- label 스캔: `T <= dt <= T_end + 1`

builder는 각 날짜의 최종 파일
`<action_log_root>/dt=<YYYY-MM-DD>/part-0.parquet`만 읽고 `shard=*` 중간 산출물은 읽지
않는다. `history_start_date`부터 `T_end + 1`까지 모든 일일 파티션은 존재하고 읽을 수
있어야 한다. 파티션이 존재하지만 행이 0개인 것은 허용하되 manifest에 기록한다.
파티션 자체가 없거나 읽을 수 없으면 `source_partition_missing`으로 실패한다.

각 row의 `event_timestamp` KST 날짜는 경로의 `dt`와 같아야 한다. 다르면 기존 일일
슬라이스 계약 위반이므로 `partition_timestamp_mismatch`로 실패한다.

## 7. Click attribution

P0-1은 기존 training entity의 30분 의미를 유지하되, 동일 timestamp의 tie를 새로
결정적으로 고정한다.

### 7.1 Timestamp 경계

`T0`는 T KST 00:00, `T1`은 `T_end + 1` KST 00:00이다.

- output impression: `T0 <= event_timestamp < T1`
- click scan: `T0 <= click_timestamp < T1 + 30분`
- impression candidate: 선택된 `dt BETWEEN T AND T_end + 1`의 impression

`T_end + 1` 전체 impression을 후보로 읽는 이유는 자정 이후 click이 더 최근의 다음 날
impression을 건너뛰고 전날 impression에 잘못 붙는 것을 막기 위해서다. 다음 날 impression은
attribution 후보일 뿐 snapshot 출력에는 들어가지 않는다.

### 7.2 귀속 규칙

각 click에 대해 다음 조건의 impression 중 한 건을 선택한다.

```text
same user_id
same video_id
impression_timestamp < click_timestamp
impression_timestamp >= click_timestamp - 30분
```

후보는 `(event_timestamp DESC, source_event_id DESC)`로 정렬해 첫 행을 선택한다. 기존
구현의 timestamp 우선 의미는 유지하면서 동일 timestamp의 비결정성을 닫는다.

선택된 impression의 `slate_id`와 click row의 `slate_id`가 다르면
`slate_attribution_mismatch`로 실패한다. `slate_id`를 label join shortcut으로 사용하지
않고 기존 시간 귀속을 수행한 뒤 무결성 검증에만 사용한다.

출력 impression은 선택된 source event ID가 하나 이상의 click에 귀속되면
`clicked=true`, 아니면 `false`다.

## 8. Validation/final split

분할 단위는 slate가 아니라 user다.

```text
bucket = int(
  sha256("research-harness-slate-v1:" + user_id).hexdigest()[:8],
  16,
) % 10
```

- bucket `0..7`: validation
- bucket `8..9`: final holdout
- 한 user의 모든 slate는 한 split에만 존재한다.
- 같은 입력에서 split은 항상 같아야 한다.
- split마다 user 1명 이상, slate 1개 이상, click-positive slate 1개 이상,
  clicked/non-clicked row가 각각 1개 이상이어야 한다.
- 위 최소 구조를 만족하지 않으면 `split_coverage_insufficient`로 snapshot 생성을
  실패시킨다.

NDCG 유효 slate 30개·20% 같은 제품 판정 coverage는 P0-2 Judge가 별도로 강제한다.
P0-1 manifest는 그 판정에 필요한 모든 count를 제공한다.

## 9. Snapshot artifact 계약

### 9.1 디렉터리

```text
<output_root>/evaluation-snapshots/by-hash/<snapshot_fingerprint>/
  manifest.json
  validation/
    slate.parquet
    labels.parquet
  final_holdout/
    slate.parquet
    labels.parquet
  _SUCCESS
```

`labels.parquet`와 final holdout 전체 경로는 Judge 소유 상태에만 전달한다. 반복 trial의
candidate workspace에는 validation `slate.parquet`만 주입한다.

### 9.2 `slate.parquet`

| 필드 | 타입 | null | 제약 |
| --- | --- | --- | --- |
| `evaluation_id` | string | 불가 | split manifest와 일치 |
| `slate_id` | string | 불가 | `slt_YYYYMMDD_<24hex>` |
| `user_id` | string | 불가 | 빈 문자열 금지 |
| `video_id` | string | 불가 | slate 안에서 유일 |
| `event_timestamp` | timestamp(us, UTC) | 불가 | KST 날짜가 `[T, T_end]` |
| `candidate_source` | string | 가능 | `model|trending|random` 또는 null |
| `original_rank` | int64 | 가능 | non-null이면 1 이상 |

row 정렬은 `(user_id, slate_id, event_timestamp, video_id)`로 고정한다. `clicked`, click
event ID, label file path와 label digest는 포함하지 않는다.

### 9.3 `labels.parquet`

| 필드 | 타입 | null | 제약 |
| --- | --- | --- | --- |
| `evaluation_id` | string | 불가 | split manifest와 일치 |
| `slate_id` | string | 불가 | slate와 일치 |
| `user_id` | string | 불가 | slate와 일치 |
| `video_id` | string | 불가 | slate와 일치 |
| `source_event_id` | string | 불가 | 원천 impression PK |
| `clicked` | bool | 불가 | §7 귀속 결과 |

labels row는 정렬된 slate row와 같은 순서로 기록한다. slate와 labels는
`(evaluation_id, slate_id, user_id, video_id)`가 정확히 1:1이어야 하며, 원천 row의
입력 순서가 달라도 결과 순서와 digest는 같아야 한다.

## 10. 식별자와 fingerprint

### 10.1 `evaluation_id`

각 split은 서로 다른 evaluation ID를 가진다.

```text
eval_<64 lowercase hex>
```

digest 입력은 추가 field를 허용하지 않는 다음 exact payload다. 날짜는 `YYYY-MM-DD`,
UTC timestamp는 정확히 `YYYY-MM-DDTHH:MM:SS.ffffffZ`, nullable 값은 JSON `null`로
직렬화한다.

```text
EvaluationIdPayload = {
  contract_version: "evaluation-slate-snapshot-v1",
  split_name: "validation" | "final_holdout",
  source: {
    root: str,
    partitions: [{dt: YYYY-MM-DD, uri: str, rows: int>=0, sha256: lowercase-64hex}],
    slate_id_cutover_date: YYYY-MM-DD
  },
  window: {
    history_start_date: YYYY-MM-DD,
    evaluation_start_date: YYYY-MM-DD,
    evaluation_end_date: YYYY-MM-DD,
    label_scan_end_date: YYYY-MM-DD,
    complete_history_label_end_date: YYYY-MM-DD,
    candidate_history_partitions: [full SourcePartitionReceipt]
  },
  attribution: {
    version: "click-attribution-v1",
    lookback_seconds: 1800,
    tie_break: ["event_timestamp_desc", "source_event_id_desc"]
  },
  split: {
    version: "user-hash-80-20-v1",
    salt: "research-harness-slate-v1:",
    validation_buckets: [0,1,2,3,4,5,6,7],
    final_holdout_buckets: [8,9]
  },
  writer: {engine: "pyarrow", version: str, options: WriterOptions},
  slate_rows: [{
    slate_id: str, user_id: str, video_id: str, event_timestamp: UTC-timestamp,
    original_rank: int | null, candidate_source: str | null
  }],
  label_rows: [{
    slate_id: str, user_id: str, video_id: str, source_event_id: str, clicked: bool
  }]
}
```

`source.partitions`와 `window.candidate_history_partitions`는 각각 `dt` 오름차순이다.
후자는 `dt < T`인 source receipt의 부분열이며 `dt`, `uri`, `rows`, `sha256`를 생략한
요약이 아니라 full `SourcePartitionReceipt` 배열이다. `slate_rows`는
`(user_id, slate_id, event_timestamp, video_id)`로, `label_rows`는 같은 slate row 순서로
정렬한다. 두 row object는 위 열 집합만 가지며 `evaluation_id` 삽입 전 값을 담는다.

canonical bytes는 UTF-8, `ensure_ascii=false`, 재귀 key sort,
`separators=(',', ':')`, trailing newline 없음으로 만든 JSON이다. 배열의 순서는 위
규칙으로 먼저 결정하고 serializer는 object key만 정렬한다. 이 계산으로 ID를 먼저
만든 뒤 두 parquet에 `evaluation_id`를 채워 순환 참조를 피한다. `evaluation_id`는 재현
식별자이지 label 기밀성을 제공하는 secret이 아니다.

`WriterOptions`는 정확히 다음 10 field다. `uv.lock`이 고정한 PyArrow 21.0.0 runtime에서
writer identity는 `engine="pyarrow"`, `version="21.0.0"`과 이 options 전체다.

```json
{"version":"2.6","coerce_timestamps":"us","allow_truncated_timestamps":false,"use_deprecated_int96_timestamps":false,"compression":"NONE","use_dictionary":false,"row_group_size":50000,"write_statistics":true,"data_page_version":"1.0","store_schema":true}
```

writer의 engine, version 또는 options 하나라도 바뀌면 identity가 바뀌고 evaluation ID도
바뀐다. 결정성 보장은 같은 `uv.lock`과 같은 writer runtime에 한정하며, writer 변경은
contract-impact review 없이는 허용하지 않는다.

#### Canonical JSON test vector

아래 한 줄은 synthetic receipt를 사용하는 validation split의 literal canonical UTF-8
JSON이다. `slt_20260901_0123456789abcdef01234567`은 `slt_YYYYMMDD_<24hex>`이고,
`evt_20260901_00000001`은 `evt_YYYYMMDD_<8 digits>` 형식이다. 별도 one-shot
SHA-256 재계산의 expected digest는
`dafb0e95e3595ada1da4ddbe0b75a076b173ffffc5b9e0c53021fc659df49d8d`이며,
evaluation ID는 `eval_dafb0e95e3595ada1da4ddbe0b75a076b173ffffc5b9e0c53021fc659df49d8d`다.

```json
{"attribution":{"lookback_seconds":1800,"tie_break":["event_timestamp_desc","source_event_id_desc"],"version":"click-attribution-v1"},"contract_version":"evaluation-slate-snapshot-v1","label_rows":[{"clicked":false,"slate_id":"slt_20260901_0123456789abcdef01234567","source_event_id":"evt_20260901_00000001","user_id":"user-01","video_id":"video-A"}],"slate_rows":[{"candidate_source":"model","event_timestamp":"2026-09-01T00:00:00.000000Z","original_rank":1,"slate_id":"slt_20260901_0123456789abcdef01234567","user_id":"user-01","video_id":"video-A"}],"source":{"partitions":[{"dt":"2026-08-30","rows":2,"sha256":"f42aeca04305f5654582dd541ef0d56d832bd3cea2e1da0b8274fb88fcf34bf8","uri":"memory://research-harness-vector/dt=2026-08-30/part-0.parquet"},{"dt":"2026-08-31","rows":3,"sha256":"3f7b574ee4fde5dfd56a206317e6959311f8577667dfe07614d94d80e4ce7573","uri":"memory://research-harness-vector/dt=2026-08-31/part-0.parquet"},{"dt":"2026-09-01","rows":1,"sha256":"e930108a7a48791a5891486b4419eb2186b84a45b01a1cde3081514ec99e3420","uri":"memory://research-harness-vector/dt=2026-09-01/part-0.parquet"},{"dt":"2026-09-02","rows":0,"sha256":"663fc92e06df292c05a44a4bf5c86d2b4429b03edbe2f11cef21da9cfe6ea5d0","uri":"memory://research-harness-vector/dt=2026-09-02/part-0.parquet"},{"dt":"2026-09-03","rows":1,"sha256":"aa40602ef43f323ba14e1b52265d8f2bc9ea3d624ce0a0a650045c5006de282b","uri":"memory://research-harness-vector/dt=2026-09-03/part-0.parquet"}],"root":"memory://research-harness-vector","slate_id_cutover_date":"2026-08-30"},"split":{"final_holdout_buckets":[8,9],"salt":"research-harness-slate-v1:","validation_buckets":[0,1,2,3,4,5,6,7],"version":"user-hash-80-20-v1"},"split_name":"validation","window":{"candidate_history_partitions":[{"dt":"2026-08-30","rows":2,"sha256":"f42aeca04305f5654582dd541ef0d56d832bd3cea2e1da0b8274fb88fcf34bf8","uri":"memory://research-harness-vector/dt=2026-08-30/part-0.parquet"},{"dt":"2026-08-31","rows":3,"sha256":"3f7b574ee4fde5dfd56a206317e6959311f8577667dfe07614d94d80e4ce7573","uri":"memory://research-harness-vector/dt=2026-08-31/part-0.parquet"}],"complete_history_label_end_date":"2026-08-30","evaluation_end_date":"2026-09-02","evaluation_start_date":"2026-09-01","history_start_date":"2026-08-30","label_scan_end_date":"2026-09-03"},"writer":{"engine":"pyarrow","options":{"allow_truncated_timestamps":false,"coerce_timestamps":"us","compression":"NONE","data_page_version":"1.0","row_group_size":50000,"store_schema":true,"use_deprecated_int96_timestamps":false,"use_dictionary":false,"version":"2.6","write_statistics":true},"version":"21.0.0"}}
```

### 10.2 `snapshot_fingerprint`

두 split parquet를 쓴 뒤 각 파일 SHA-256을 계산한다. fingerprint payload는 typed
`EvaluationSnapshotManifest` 전체의 model dump에서 정확히 `snapshot_fingerprint`와
`created_at`만 제거한 canonical JSON이다. 다른 field를 제외하거나 unknown field를
묵살하는 것은 금지한다. 이 payload의 SHA-256은 directory name, `_SUCCESS` 내용, typed
manifest의 `snapshot_fingerprint`의 정본이다.

동일 원천과 계약은 동일 `evaluation_id`·`snapshot_fingerprint`를 만들어야 한다.
`created_at`은 관측 메타데이터이며 identity에 참여하지 않는다.

`optional_non_null_ratio`는 JSON object shape를 유지하되, 정확히
`candidate_source`와 `original_rank` 각각 `0..1` float만 받는 frozen/extra-forbid
`OptionalNonNullRatio`다. `SplitSummary`는 입력 dict alias를 이 typed model로 즉시
정규화하므로, 이후 외부 dict 또는 constructed ratio를 바꿔 manifest model dump를
변경할 수 없다. Task 6의 canonical fingerprint helper는 이 immutable
`EvaluationSnapshotManifest.model_dump()`를 입력으로 사용한다.

## 11. Manifest

`manifest.json`은 추가 field를 허용하지 않는 typed `EvaluationSnapshotManifest`이며 다음
중첩 구조를 가진다.

```json
{
  "contract_version": "evaluation-slate-snapshot-v1",
  "snapshot_fingerprint": "64 lowercase hex",
  "created_at": "UTC ISO-8601",
  "source": {
    "root": "opaque root",
    "partitions": [
      {"dt": "2026-08-31", "uri": "opaque uri", "rows": 100, "sha256": "..."}
    ],
    "slate_id_cutover_date": "2026-08-31"
  },
  "window": {
    "history_start_date": "2026-08-01",
    "evaluation_start_date": "2026-08-31",
    "evaluation_end_date": "2026-09-02",
    "label_scan_end_date": "2026-09-03",
    "complete_history_label_end_date": "2026-08-29",
    "candidate_history_partitions": [
      {"dt": "2026-08-01", "uri": "opaque uri", "rows": 100, "sha256": "64 lowercase hex"}
    ]
  },
  "attribution": {
    "version": "click-attribution-v1",
    "lookback_seconds": 1800,
    "tie_break": ["event_timestamp_desc", "source_event_id_desc"]
  },
  "split": {
    "version": "user-hash-80-20-v1",
    "salt": "research-harness-slate-v1:",
    "validation_buckets": [0, 1, 2, 3, 4, 5, 6, 7],
    "final_holdout_buckets": [8, 9]
  },
  "writer": {
    "engine": "pyarrow",
    "version": "21.0.0",
    "options": {
      "version": "2.6",
      "coerce_timestamps": "us",
      "allow_truncated_timestamps": false,
      "use_deprecated_int96_timestamps": false,
      "compression": "NONE",
      "use_dictionary": false,
      "row_group_size": 50000,
      "write_statistics": true,
      "data_page_version": "1.0",
      "store_schema": true
    }
  },
  "validation": {
    "evaluation_id": "eval_...",
    "counts": {
      "user_count": 80,
      "slate_count": 80,
      "row_count": 1920,
      "clicked_row_count": 42,
      "click_positive_slate_count": 42,
      "click_positive_slate_ratio": 0.525,
      "mean_slate_size": 24.0
    },
    "optional_non_null_ratio": {
      "candidate_source": 1.0,
      "original_rank": 1.0
    },
    "artifacts": {
      "slate": {"relative_path": "validation/slate.parquet", "rows": 1920, "sha256": "64 lowercase hex"},
      "labels": {"relative_path": "validation/labels.parquet", "rows": 1920, "sha256": "64 lowercase hex"}
    }
  },
  "final_holdout": {
    "evaluation_id": "eval_...",
    "counts": {
      "user_count": 20,
      "slate_count": 20,
      "row_count": 480,
      "clicked_row_count": 11,
      "click_positive_slate_count": 11,
      "click_positive_slate_ratio": 0.55,
      "mean_slate_size": 24.0
    },
    "optional_non_null_ratio": {
      "candidate_source": 1.0,
      "original_rank": 1.0
    },
    "artifacts": {
      "slate": {"relative_path": "final_holdout/slate.parquet", "rows": 480, "sha256": "64 lowercase hex"},
      "labels": {"relative_path": "final_holdout/labels.parquet", "rows": 480, "sha256": "64 lowercase hex"}
    }
  }
}
```

`| None`이 없는 field는 non-null/required다. typed field는 다음 exact table을 따른다.

| Type/module | Exact fields |
| --- | --- |
| `EvaluationSnapshotError` / errors | `code: SnapshotErrorCode`, `stage: str`, `dt: date | None`, `count: int | None`, `identifier_prefix: str | None`; prefix는 UTF-8 최대 16 bytes를 code-point 경계에서 자른다 |
| `EvaluationSnapshotRequest` / snapshot | `action_log_root: str`, `history_start_date: date`, `evaluation_start_date: date`, `evaluation_end_date: date`, `slate_id_cutover_date: date`, `output_root: Path` |
| `SnapshotSource` / snapshot | `root: str`, `partitions: tuple[SourcePartitionReceipt, ...]`, `slate_id_cutover_date: date` |
| `EvaluationWindow` / snapshot | `history_start_date: date`, `evaluation_start_date: date`, `evaluation_end_date: date`, `label_scan_end_date: date`, `complete_history_label_end_date: date`, `candidate_history_partitions: tuple[SourcePartitionReceipt, ...]` |
| `SourceEvent` / source | `partition_date: date`, `source_event_id: str`, `event_type: Literal['impression', 'click', 'view', 'like']`, `user_id: str`, `video_id: str`, `event_timestamp: datetime`, `slate_id: str | None`, `rank: int | None`, `exposure_source: str | None`, `policy_version: str | None` |
| `SourcePartitionReceipt`, `LoadedPartition` / source | receipt=`dt: date, uri: str, rows: int, sha256: str`; loaded=`receipt: SourcePartitionReceipt, events: tuple[SourceEvent, ...]` |
| `AttributedImpression` / snapshot | `slate_id: str`, `user_id: str`, `video_id: str`, `event_timestamp: datetime`, `source_event_id: str`, `clicked: bool`, `original_rank: int | None`, `candidate_source: str | None` |
| `EvaluationSplit` / snapshot | `name: SplitName`, `rows: tuple[AttributedImpression, ...]`, `user_ids: tuple[str, ...]` |
| `AttributionContract`, `SplitContract` / snapshot | attribution=`version: Literal['click-attribution-v1'], lookback_seconds: Literal[1800], tie_break: tuple[str, str]`; split=`version: Literal['user-hash-80-20-v1'], salt: Literal['research-harness-slate-v1:'], validation_buckets: tuple[int, ...], final_holdout_buckets: tuple[int, ...]` |
| `WriterIdentity`, `WriterOptions` / snapshot | writer=`engine: Literal['pyarrow'], version: str, options: WriterOptions`; options는 §10.1의 literal 10 fields 전부다 |
| `ArtifactReceipt`, `SplitArtifacts`, `SplitCounts`, `OptionalNonNullRatio`, `SplitSummary` / snapshot | artifact=`relative_path: str, rows: int, sha256: str`; artifacts=`slate: ArtifactReceipt, labels: ArtifactReceipt`; counts=`user_count: int, slate_count: int, row_count: int, clicked_row_count: int, click_positive_slate_count: int, click_positive_slate_ratio: float, mean_slate_size: float`; ratio=frozen/extra-forbid `candidate_source: float[0,1], original_rank: float[0,1]`; summary=`evaluation_id: EvaluationId, counts: SplitCounts, optional_non_null_ratio: OptionalNonNullRatio, artifacts: SplitArtifacts` |
| `EvaluationSnapshotManifest` / snapshot | `contract_version: Literal['evaluation-slate-snapshot-v1'], snapshot_fingerprint: SnapshotFingerprint, created_at: datetime, source: SnapshotSource, window: EvaluationWindow, attribution: AttributionContract, split: SplitContract, writer: WriterIdentity, validation: SplitSummary, final_holdout: SplitSummary` |
| `SnapshotArtifactInput`, `EvaluationSnapshotReceipt` / snapshot | input=`request, window, partitions, validation, final_holdout, created_at`; receipt=`snapshot_fingerprint, target_path: Path, validation_id, final_holdout_id, reused: bool` |

`complete_history_label_end_date=T-2`는 candidate가 받은 history만으로 완전한 label을
만들 수 있는 마지막 출력일이다. candidate history의 실제 파티션 목록도 manifest에
기록한다.

## 12. Write-once 게시

1. `output_root`와 같은 filesystem의 sibling staging directory에 파일을 쓴다.
2. 모든 parquet와 manifest를 flush하고 파일 SHA-256을 계산한다.
3. staging 안에 `<snapshot_fingerprint>\n`을 내용으로 가진 `_SUCCESS`를 마지막으로 쓴다.
4. `<snapshot_fingerprint>` target이 없으면 staging을 atomic rename한다.
5. target이 이미 있으면 `_SUCCESS`, manifest fingerprint와 모든 artifact digest가 같을
   때만 기존 snapshot을 멱등 재사용한다.
6. target이 불완전하거나 같은 fingerprint 아래 내용이 다르면
   `snapshot_write_conflict`로 실패하며 덮어쓰지 않는다.

GCS 게시를 추가할 때도 generation precondition을 사용해 같은 write-once 의미를
유지한다. local과 GCS 구현은 동일 manifest를 생산해야 한다.

## 13. Local fixture

개발·CI fixture는 `RuleBasedActionLogGenerator`를 사용한다.

- 최소 history 구간, 평가 `[T, T_end]`, scan용 `T_end+1`을 모두 생성한다.
- virtual user·video 입력과 생성 seed는 Judge 소유 fixture state에 둔다.
- candidate에는 생성된 `dt < T` history와 validation slate만 전달한다.
- 평가 구간을 재생성할 수 있는 fixture 입력·seed는 candidate argv·환경·workspace에
  전달하지 않는다.
- fixture도 production과 같은 parquet·cutover·attribution·split·manifest 경로를 탄다.

## 14. 실패 코드

| 코드 | 조건 |
| --- | --- |
| `invalid_date_range` | 날짜 관계 위반 |
| `source_partition_missing` | 필수 파티션 부재·읽기 실패 |
| `partition_timestamp_mismatch` | row KST 날짜와 dt 불일치 |
| `source_schema_invalid` | 필수 column·타입·field domain 위반 |
| `slate_id_missing_after_cutover` | cutover 이후 column 부재·null |
| `slate_id_invalid` | 형식·identity 불일치 |
| `slate_id_collision` | 서로 다른 identity가 같은 ID 사용 |
| `duplicate_slate_video` | 한 slate에 같은 video 중복 |
| `slate_attribution_mismatch` | click과 귀속 impression의 slate 불일치 |
| `split_coverage_insufficient` | validation/final 최소 구조 미달 |
| `snapshot_write_conflict` | write-once target 불완전·digest 불일치 |

오류에는 row 원문·user ID·경로 전체를 넣지 않는다. stage, reason code, dt, count와
제한된 식별자 prefix만 기록한다.

## 15. 구현 단계

### Stage A — producer 계약 (구현 완료, 2026-08-31)

1. [x] `EventLog`, Parquet/warehouse/spool schema에 optional `slate_id` 추가
2. [x] canonical ID helper와 slate context 구현
3. [x] daily·shard merge producer에서 context 전달
4. [x] 모든 파생 event에 같은 ID 전파
5. [x] quality 검사에서 legacy column 부재는 관용하고 cutover 검사는 builder가 소유

구현은 `SlateGenerationContext(partition_date, producer="daily-action-log-v1")`에서만
canonical ID를 만들며, context 없는 generic/policy 결과는 null을 유지한다. final
event schema는 additive nullable `slate_id`로 유지하고 draft/checkpoint schema와
`action_log_schema_v1`은 바꾸지 않았다. fresh local RuleBased fixture의 direct와
2-shard merge는 24 row·4 distinct non-null slate 및 같은 projection SHA-256을
생성했다. malformed exposure rank는 `invalid_slate_exposure_rank`로 실패하면서 기존
final artifact SHA-256을 보존했다. 재현 가능한 수치와 cleanup receipt는
`.omo/evidence/research-harness-stage-a/task-6.json`에 있다.

이는 producer 기반만 완료한 상태다. 아래 Stage B/C, 특히 cutover fail-closed,
snapshot, label 봉인, validation/final split 및 Judge handoff는 완료로 표시하지 않는다.

### Stage B — snapshot builder

1. source adapter와 필수 파티션 검증
2. cutover·row schema 검증
3. multi-day attribution
4. user split과 structural coverage 검증
5. evaluation ID·parquet·manifest 생성
6. write-once local publisher

### Stage C — fixture와 실증

1. rule-based 일일 파티션 fixture 생성
2. 동일 입력 재생성의 ID·fingerprint 일치 확인
3. candidate view에 label·final holdout·fixture seed가 없는지 확인
4. P0-2 Judge가 소비할 수 있는 validation/final artifact handoff

## 16. 검증 매트릭스

| 영역 | 필수 검증 |
| --- | --- |
| slate 생성 | 같은 묶음 동일 ID, 다른 user/후보 묶음 다른 ID, worker·shard 수 변화에도 동일 ID |
| 호환성 | legacy parquet 읽기 성공, cutover 이전 제외, 이후 null fail-closed |
| 파티션 | dt와 KST timestamp 일치, `T_end+1` 누락 실패 |
| attribution | 30분 경계, 다음 날 더 최근 impression, timestamp tie-break, slate mismatch 실패 |
| split | 같은 user가 한쪽에만 존재, 동일 입력 동일 split, 빈 split 실패 |
| label 봉인 | slate에 `clicked` 없음, candidate 경로에 labels/final 없음 |
| lineage | slate-label 1:1, source event 추적, evaluation ID 재현 |
| 게시 | 동일 snapshot 멱등, partial·digest conflict 거부 |
| 전체 | rule-based fixture에서 동일 snapshot 두 번 생성해 fingerprint 일치 |

좁은 검증은 다음 순서로 실행한다.

```bash
uv run python -m pytest tests/action_log_generation/ -v
uv run python -m pytest tests/research_harness/test_slate.py -v
uv run --no-sync ruff check autoresearch tests
git diff --check
```

## 17. 완료 조건

- 모든 평가 가능 action log row가 원천 생성 시점의 `slate_id`를 가진다.
- legacy 파티션을 추론 없이 제외하고 cutover 이후 null을 fail-closed한다.
- candidate history에 `dt >= T`가 없고 validation slate에 label이 없다.
- validation/final이 user 단위로 분리되고 두 split의 evaluation ID가 다르다.
- multi-day attribution이 기존 30분 의미와 자정 경계를 보존한다.
- 같은 입력에서 같은 `evaluation_id`와 `snapshot_fingerprint`가 나온다.
- write-once snapshot이 부분 결과나 다른 내용을 같은 ID로 받아들이지 않는다.
- RuleBasedActionLogGenerator fixture로 GCP 없이 P0-1 전체가 재현된다.
