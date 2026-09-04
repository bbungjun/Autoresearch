# Research Harness P0-1 — 재현 가능한 평가 데이터와 split

> 작성: 2026-08-31 | 상태: Stage A·B·C 완료, P0-1 완료 | 추적: #17, #22
>
> 상위 계약:
> `docs/specs/2026-08-14-paper-grounded-autonomous-ml-research-harness.md`
>
> 구현 순서:
> `docs/archive/plans/2026-08-15-local-research-harness-mvp.md` Task 1-0, Task 1

## Stage A·B·C 및 P0-1 완료

Stage B는 원천 파티션 검증, canonical slate 검증, 다일 click attribution, 고정 user
split·구조 coverage, label 분리 artifact·typed manifest, local write-once publisher와 공개
snapshot builder까지 구현했습니다. Stage C는 typed model과 canonical input, production daily
4-run fixture builder, canonical source adapter, write-once 게시, P0-2용 Judge snapshot handoff와
validation 전용 data-only CandidateDataView와 독립 two-root 재생성 verifier까지 구현·실증했습니다.
동일 target fixture/view 재사용은 독립 재생성과 분리된 테스트로 검증합니다.
실제 disposable worktree,
subprocess argv·환경과 Sealed Judge의 지표·판정은 각각 후속 Task 3과 P0-2 책임입니다.

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
- disposable candidate git worktree와 subprocess argv·환경 구성
- GCS·BigQuery credential 제거와 candidate 프로세스 격리
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
                                ├─ CandidateDataView
                                │    ├─ candidate-view.json
                                │    ├─ slate.parquet
                                │    └─ history/action_log/dt=D/part-0.parquet
                                └─ JudgeSnapshotHandoff
                                     └─ 전체 snapshot root + 검증 digest
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
candidate 쪽에는 §13.3의 `CandidateDataView`만 전달한다. Stage B의
`EvaluationSnapshotReceipt.target_path`나 전체 `manifest.json`은 source root·평가 파티션
URI·final artifact 상대 경로를 포함하므로 candidate interface가 아니다.

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

## 13. Stage C local fixture와 handoff

Stage C에는 두 개의 외부 seam만 둔다.

1. `LocalEvaluationFixture` module은 Judge 소유 입력에서 일일 action log와 Stage B
   snapshot을 만들고 `JudgeSnapshotHandoff`를 반환한다.
2. `CandidateDataView` module은 검증된 Judge handoff에서 candidate에게 허용된 파일만
   별도 목적지에 게시한다.

두 module의 interface가 fixture 입력 생성, daily runner 반복, source 검증, artifact 경로
해석과 digest 재검증을 숨긴다. 실제 git worktree 생성, subprocess argv·환경, credential
allowlist는 `docs/archive/plans/2026-08-15-local-research-harness-mvp.md` Task 3이 이 interface를
소비해 구현한다. Stage C 테스트는 임시 빈 디렉터리를 candidate 목적지로 사용하며 Task 3을
선행 구현하지 않는다.

`FixtureActionLogSource`는 Stage B의 기존 `ActionLogSource` seam에 놓는 **내부 adapter**다.
외부 interface에 새 source port를 추가하지 않는다. adapter의 `open_partition(dt)`는 Judge
fixture root의 물리 Parquet를 열지만, `opaque_root`와 `partition_uri(dt)`는 각각
`fixture://<descriptor_sha256>/action-log`와
`fixture://<descriptor_sha256>/action-log/dt=D/part-0.parquet`를 반환한다. 이 canonical URI만
Stage B identity에 들어가므로 Judge root의 물리 위치는 evaluation ID에 참여하지 않는다.

### 13.1 공개 interface와 exact typed contract

```text
build_local_evaluation_fixture(
  request: LocalEvaluationFixtureRequest,
) -> LocalEvaluationFixtureReceipt

materialize_candidate_data_view(
  request: CandidateDataViewRequest,
  *,
  source: ActionLogSource,
) -> CandidateDataViewReceipt
```

아래 값 객체 dataclass는 `frozen=True, slots=True`, JSON model은 `extra="forbid", frozen=True`다.
`Path`가 포함된 receipt는 trusted Harness 프로세스 내부 값이며 candidate로 직렬화하지 않는다.

| Type | Exact fields |
| --- | --- |
| `LocalEvaluationFixtureRequest` | `judge_state_root: Path`, `evaluation_start_date: date`, `fixture_seed: int` |
| `FixtureInputReceipt` | `relative_path: str`, `rows: int`, `sha256: str` |
| `FixturePartitionReceipt` | `dt: date`, `relative_path: str`, `rows: int`, `sha256: str` |
| `FixtureDescriptor` | `contract_version: Literal['youtube-ctr-local-fixture-v1']`, `input_generator_version: Literal['youtube-ctr-input-v1']`, `input_writer: WriterIdentity`, `fixture_seed: int`, `generator: Literal['rule_based']`, `generator_model: Literal['fixture-rule-action-log']`, `history_start_date: date`, `evaluation_start_date: date`, `evaluation_end_date: date`, `slate_id_cutover_date: date`, `candidates_per_user: Literal[24]`, `video_count_per_partition: Literal[48]`, `click_threshold: Literal[0.0]`, `personalized_ratio: Literal[0.7]`, `popular_ratio: Literal[0.2]`, `exploration_ratio: Literal[0.1]`, `history_days_per_run: Literal[1]`, `max_events_per_user_per_day: Literal[24]`, `max_concurrency: Literal[1]`, `chunk_size: Literal[0]`, `max_quarantine_ratio: Literal[0.0]`, `overwrite: Literal[False]`, `validation_user_count: Literal[160]`, `final_holdout_user_count: Literal[40]`, `virtual_users: FixtureInputReceipt`, `youtube_partitions: tuple[FixturePartitionReceipt, ...]` |
| `JudgeSnapshotHandoff` | `snapshot_fingerprint: SnapshotFingerprint`, `snapshot_root: Path`, `manifest_sha256: str`, `validation_id: EvaluationId`, `final_holdout_id: EvaluationId` |
| `LocalEvaluationFixtureReceipt` | `fixture_root: Path`, `descriptor_path: Path`, `descriptor_sha256: str`, `action_log_partitions: tuple[SourcePartitionReceipt, ...]`, `judge: JudgeSnapshotHandoff`, `reused: bool` |
| `CandidateHistoryReceipt` | `dt: date`, `relative_path: str`, `rows: int`, `sha256: str` |
| `CandidateDataManifest` | `contract_version: Literal['candidate-data-view-v1']`, `evaluation_id: EvaluationId`, `evaluation_start_date: date`, `complete_history_label_end_date: date`, `slate: ArtifactReceipt`, `history_partitions: tuple[CandidateHistoryReceipt, ...]` |
| `CandidateDataViewRequest` | `judge: JudgeSnapshotHandoff`, `destination_root: Path` |
| `CandidateDataViewReceipt` | `root: Path`, `manifest: CandidateDataManifest`, `manifest_sha256: str`, `reused: bool` |

`fixture_seed`는 default가 없는 필수 non-negative integer다. 실제 action log randomness에는
`RuleBasedActionLogGenerator` 생성자 인자가 아니라 각 일일
`EventGenerationRequest.seed`로 전달한다. seed와 이를 이용해 만든 virtual user·video 입력은
Judge 소유 `fixture.json`에만 남긴다.
`input_generator_version`은 user/video field derivation과 row order를 버전으로 잠그고,
`input_writer`는 §10.1과 같은 pinned PyArrow identity·options를 사용한다. 이 둘 중 하나가
바뀌면 descriptor hash도 바뀐다. 일일 producer는 `overwrite=false`로만 호출한다.

`judge_state_root`는 존재하는 absolute local directory여야 한다. symlink·junction/reparse
point를 포함하거나 `destination_root`와 서로 포함 관계인 경로는
`fixture_request_invalid`로 거부한다. 같은 UID가 다른 절대 경로를 추측해 읽는 공격까지
막는 보안 sandbox가 아니라는 §4 상위 위협 모델은 유지한다.
Builder는 root 아래 `fixtures`, `by-hash`를 `parents=True`로 한 번에 따라가지 않고 각
component를 lstat한다. 기존 component는 실제 directory이면서 symlink·junction/reparse가
아니어야 하고, 없는 component는 한 단계 생성한 직후 다시 lstat·resolve containment를
검증한다. Descriptor lock도 open 전 regular single-link file인지 검사하고 open descriptor의
device/inode/type/link count를 경로와 다시 대조한 뒤에만 잠근다. 위반은 원문 경로 없이
derived root에서는 `fixture_request_invalid`, descriptor state에서는
`fixture_state_conflict`로 실패한다.

### 13.2 canonical fixture와 P0-2-ready coverage

canonical fixture는 `T=evaluation_start_date`를 기준으로 다음을 고정한다.

- history: `T-2`, `T-1`; evaluation output: `T`; scan tail: `T+1`
- `history_start_date=slate_id_cutover_date=T-2`, `evaluation_end_date=T`
- `candidates_per_user=24`, video 48개/partition, `click_threshold=0.0`,
  candidate ratio=`0.7/0.2/0.1`, generator=`rule_based`
- 일일 run은 `history_days=1`, `max_events_per_user_per_day=24`, `max_concurrency=1`,
  `chunk_size=0`, `max_quarantine_ratio=0.0`을 사용한다
- user ID 후보를 `fixture_seed`에서 결정적으로 만들고 §8 bucket을 계산해 validation user
  160명과 final holdout user 40명을 정확히 선택한다
- 같은 seed·날짜는 같은 virtual user·video input bytes를 만든다
- 네 날짜 모두 production `run_daily_action_log`와 최종
  `dt=D/part-0.parquet` 경로를 사용한다

`youtube-ctr-local-fixture-v1`은 각 partition `D`의 logical completion timestamp를
`D 00:00:00+00:00`으로 고정한다. Builder는 이 값을 production daily API의 additive
`completion_timestamp` seam에 전달하며, production action-log writer가 `generated_at`을 포함한
최종 Parquet를 직접 기록한다. Builder가 게시 후 Parquet를 다시 쓰거나 event timestamp,
click, event/slate ID를 보정하는 것은 금지한다. Seam을 생략한 production 호출은 기존처럼 실제
완료 시각을 기록한다. 이 logical clock과 production writer 사용은 새 descriptor field가 아니라
기존 `contract_version` 의미이며 golden source projection/hash로 봉인한다.

각 user의 평가 slate는 24행이며 click-positive여야 한다. 따라서 validation/final 각각
`click_positive_slate_count == slate_count`, `click_positive_slate_ratio == 1.0`이고
clicked/non-clicked row를 모두 포함해야 한다. 30개·20%는 최소 기반 조건일 뿐 일부 slate의
click 누락을 허용하지 않는다. 이 조건은 P0-1 structural coverage보다 강하며 P0-2 Judge의 ranking metric
성공 경로가 같은 fixture를 재사용하게 한다. 미달이면 `fixture_coverage_insufficient`로
fixture 전체를 게시하지 않는다.

Judge fixture state의 canonical layout은 다음과 같다.

```text
<judge_state_root>/fixtures/by-hash/<descriptor_sha256>/
  fixture.json
  inputs/
    virtual_users.parquet
    youtube_trending_kr/dt=D/part-0.parquet
  action_log/dt=D/part-0.parquet
  evaluation-snapshots/by-hash/<snapshot_fingerprint>/
    ... Stage B exact artifact tree ...
  _SUCCESS
```

`descriptor_sha256`는 `FixtureDescriptor`의 canonical JSON SHA-256이다. descriptor의 모든
path는 fixture root 기준 POSIX relative path이며 absolute Judge path는 identity에 넣지
않는다. snapshot request의 `action_log_root`와 Stage B receipt URI에는 위
`FixtureActionLogSource`의 canonical `fixture://` URI를 사용한다. cooperating builder lock
아래 descriptor hash의 새 root만 만들고 `_SUCCESS`를 마지막에 기록한다. 완성된 동일 root만
digest 검증 후 멱등 재사용하며 partial·상이한 내용은 `fixture_state_conflict`로 거부한다.

Fixture root의 `_SUCCESS`는 빈 marker가 아니라 canonical JSON
`local-fixture-integrity-v1`이다. `extra="forbid"`, frozen typed model로 descriptor SHA-256,
날짜순 action-log partition receipt 4개, snapshot fingerprint와 manifest SHA-256,
validation/final evaluation ID, validation/final의 slate·label artifact receipt 4개를 봉인한다.
Reuse는 이 marker를 먼저 canonical parse한 뒤 descriptor/input/action-log/snapshot을 receipt와
대조한다. Fixture root와 snapshot root는 위 canonical layout에서 파생한 file·directory
allowlist와 정확히 같아야 하며 extra file/dir, 게시 root 내부 lock/staging, symlink·junction·
hardlink alias를 거부한다. 모든 tree entry는 `lstat` 기준 실제 directory 또는 single-link regular
file이어야 하며 FIFO·socket·device·unknown mode도 거부한다. Cooperating lock과 staging은 final
fixture root 밖에 둔다. Descriptor lock은 유지되는 1-byte range를 truncate하지 않아 cooperating
process의 같은 descriptor 게시를 직렬화한다.

이 receipt는 부분 게시와 우발적·비협력 변조를 탐지하는 local integrity anchor이지 신뢰 경계를
넘는 서명은 아니다. 같은 UID의 hostile actor가 모든 artifact와 manifest, outer marker를
일관되게 다시 쓰는 공격은 §4 위협 모델 밖이며 이 구현이 방어한다고 주장하지 않는다.

### 13.3 CandidateDataView

이 절은 기존 v1 구현 계약이다. Task 6의 신규 v2 목표는 §18이며 v1의 파일 집합을
소급 변경하지 않는다.

candidate view의 exact filesystem interface는 다음뿐이다.

```text
<destination_root>/harness_in/
  candidate-view.json
  slate.parquet
  history/action_log/dt=D/part-0.parquet
```

`candidate-view.json`은 `CandidateDataManifest`의 canonical JSON이다. history는 Judge
manifest의 `candidate_history_partitions`에 있는 `dt < T` 파일만 byte-copy하고,
`slate.parquet`은 validation artifact만 byte-copy한다. 복사 전후 SHA-256과 Parquet row 수를
receipt와 대조한다. symlink·junction·hardlink와 원천 경로를 가리키는 파일은 허용하지 않는다.
주입된 `ActionLogSource.opaque_root`와 Stage B `source.partitions` 전체의 각
`partition_uri(dt)`는 Judge manifest receipt와 정확히 같아야 한다. 이 전체 identity 대조는
source를 열기 전에 끝내되, module은 candidate history receipt의 `dt<T`만
`open_partition(dt)`으로 열고 읽은 bytes의 SHA-256·Parquet row 수를 대조한 뒤 candidate
view로 복사한다. fixture에서는 같은
`FixtureActionLogSource`, production local/GCS에서는 기존 Arrow adapter를 사용한다.
검증된 `fixture://` source에서는 descriptor digest와 component 이름까지 대조한 canonical
`<judge_state_root>/fixtures/by-hash/<descriptor_sha256>/...` layout에서 Judge state root를
역산하고, candidate destination과 이 root의 양방향 포함 관계를 거부한다. 신뢰할 physical
Judge state root를 제공하지 않는 non-fixture local/GCS source에는 이 보장을 과장하지 않고,
snapshot·physical source root/path와 destination 사이의 기존 포함 관계 경계를 적용한다.
`CandidateDataViewReceipt.root`는 `<destination_root>/harness_in`이고,
`CandidateDataManifest.slate.relative_path`는 정확히 `slate.parquet`, history receipt의
`relative_path`는 정확히 `history/action_log/dt=D/part-0.parquet`다. 모든 relative path는
POSIX separator를 사용하며 absolute path, drive, `..`와 backslash를 거부한다.
`CandidateDataViewRequest`에는 `split_name`이나 final 선택 flag를 두지 않는다. final slate
materialization은 final 소비 registry가 발급한 권한을 요구하는 별도 후속 interface가
소유하며, Stage C validation interface를 parameter로 넓혀 재사용하지 않는다.

다음 값은 candidate view의 파일명·내용·typed receipt 어디에도 없어야 한다.

- validation/final `labels.parquet`과 그 digest·상대/절대 경로
- final holdout slate, evaluation ID, count, digest와 상대/절대 경로
- 전체 Stage B manifest, snapshot root/fingerprint, 평가 source partition URI
- `FixtureDescriptor`, fixture seed, virtual user·video input과 Judge root

destination sibling staging에 모두 쓴 뒤 `harness_in`을 atomic rename한다. 기존 완성 view는
manifest와 모든 digest가 같을 때만 `reused=true`로 반환하고, partial·상이한 view는
`candidate_view_conflict`로 거부한다.

Windows에서는 검증을 통과한 `destination_root`가 길이 250자인 경우에도 lock·staging·파일
쓰기·검증·rename·cleanup을 extended path 내부 표현으로 수행한다. 이 표현은 filesystem I/O에만
사용하며 `CandidateDataViewReceipt.root`와 manifest에는 `\\?\` prefix를 노출하지 않는다.
v1/v2 최초 게시와 완전 재검증 재사용은 짧은 destination과 동일한 manifest·artifact digest를
반환해야 하고, 게시 실패 시 소유한 sibling staging을 회수한 뒤 정제된
`candidate_view_conflict`를 반환한다. UNC/device path 자체를 공개 입력 계약으로 추가하지 않는다.

### 13.4 JudgeSnapshotHandoff

Judge handoff는 Stage B receipt의 target을 다시 열어 `_SUCCESS`, typed manifest
fingerprint, manifest SHA-256과 네 artifact digest·row count를 모두 검증한 뒤 만든다.
P0-2 Judge는 `snapshot_root/manifest.json`을 typed model로 읽어 validation/final artifact를
찾으며 candidate 경로나 candidate code를 참조하지 않는다. handoff를 candidate argv·환경·
prompt·feedback 또는 candidate view에 직렬화하지 않는다.

이 handoff는 final holdout 소비를 허가하지 않는다. final slate 주입 시점과 write-once 소비
registry는 각각 후속 Controller와 P0-2/P1 계약이 소유한다.

### 13.5 독립 재생성 검증 프로토콜

write-once `reused=true`는 게시 멱등성 증거이지 fixture 재생성 증거가 아니다. Stage C의
재현성 검증은 다음 두 경로를 분리한다.

1. **독립 재생성:** 같은 `fixture_seed`와 날짜로 서로 다른 두 Judge state root에 source와
   snapshot을 각각 처음부터 생성한다. 두 run의 `FixtureActionLogSource`는 같은 descriptor
   hash에서 같은 canonical `fixture://` root·URI를 보고하고, 두 receipt 모두
   `reused=false`여야 한다. source partition SHA, slate ID projection, validation/final
   evaluation ID, 네 artifact SHA와 snapshot fingerprint를 전부 비교한다.
2. **게시 멱등성:** 같은 완성 fixture/snapshot root를 다시 호출해 digest 검증 뒤
   `reused=true`인지 별도로 확인한다.

`EvaluationIdPayload.source.root`와 partition `uri`는 identity에 참여한다. 따라서 physical
temp path를 그대로 보고하면 독립 run의 ID가 달라지는 것이 정상이다. Stage C fixture만
내부 adapter의 canonical `fixture://` URI를 사용하고, production local/GCS source의 URI
계약은 바꾸지 않는다. 어느 비교든 다르면 `fixture_reproducibility_mismatch`다.

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
| `fixture_request_invalid` | Judge/candidate root·seed·날짜 또는 path containment 위반 |
| `fixture_coverage_insufficient` | canonical fixture가 P0-2-ready split coverage 미달 |
| `fixture_state_conflict` | Judge fixture root가 partial이거나 같은 descriptor 아래 내용 불일치 |
| `candidate_view_conflict` | candidate view가 partial이거나 기존 내용·digest 불일치 |
| `judge_handoff_invalid` | `_SUCCESS`·manifest·artifact 검증 실패 |
| `fixture_reproducibility_mismatch` | 독립 두 run의 source·ID·artifact·fingerprint 불일치 |

오류에는 row 원문·user ID·경로 전체를 넣지 않는다. stage, reason code, dt, count와
제한된 식별자 prefix만 기록한다. Stage C public builder·materializer·reproducibility verifier가
예상 가능한 producer, filesystem, Arrow, Pydantic 또는 domain 오류를 typed `StageCError`로
번역할 때는 exception chaining도 억제한다. 따라서 `traceback.format_exception`을 포함한
호출자 관찰 surface에도 원래 예외의 경로·입력 원문이 남지 않는다.

**#76 오류 객체와 값 객체의 구분:** `StageCError`는 생성자 필드와 필드 기반 equality/hash를
유지하는 slotted dataclass이며, code·stage·dt·count·identifier_prefix는 초기화 후 일반
대입·삭제를 거부한다. 예외 객체 전체에 `frozen=True`를 적용하지 않으며 traceback·context·
cause·suppress_context 등 Python 런타임 메타데이터는 기본 Exception 동작에 위임한다.
with/contextmanager·pytest 경계에서 원래 예외 객체와 code/stage를 보존해야 한다.
`raise ... from None`의 formatted traceback 억제를 유지하되 디버깅용 `__context__`를
지우지는 않는다. 식별자는 UTF-8 16바이트 이내로 축약하고 `str`에는 그 존재 여부만 표시한다.
다른 snapshot/Judge 예외의 동일 전달 충돌은 #79에서 별도로 검증하며 이번에 일괄 변경하지 않는다.

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

이는 2026-08-31 Stage A producer 완료 시점의 기록이다. 이후 Stage B snapshot builder와
Stage C fixture·Judge handoff·CandidateDataView가 구현됐으며, 독립 two-root 최종 실증은
완료로 표시하지 않는다.

### Stage B — snapshot builder (구현 완료)

1. [x] source adapter와 필수 파티션 검증
2. [x] cutover·row schema 검증
3. [x] multi-day attribution
4. [x] user split과 structural coverage 검증
5. [x] evaluation ID·parquet·manifest 생성
6. [x] write-once local publisher

### Stage C — fixture와 실증 (구현 완료, 2026-09-02)

1. [x] versioned fixture descriptor와 Judge 소유 write-once state 구현
2. [x] P0-2-ready rule-based 일일 파티션 fixture 생성
3. [x] data-only `CandidateDataView`와 safe manifest 구현
4. [x] 검증된 `JudgeSnapshotHandoff` 구현
5. [x] 독립 2-run 재생성 및 별도 reuse 경로 실증
6. [x] candidate view에 label·final·source URI·fixture seed·Judge path가 없는지 확인

## Portfolio Record — Stage C contract review

### Problem

초기 Stage C 문구는 candidate workspace·argv·환경 검증을 P0-1에 포함해 후속 Task 3의
책임과 겹쳤습니다. 또한 Stage B receipt나 전체 manifest를 그대로 candidate에 넘기면 final
artifact와 평가 source URI를 함께 노출할 수 있었고, 같은 output을 두 번 호출해
`reused=true`를 보는 검증은 fixture 원천 재생성을 증명하지 못했습니다. physical source
root가 evaluation identity에 포함되므로 서로 다른 temp root의 진짜 독립 run은 같은
입력이어도 다른 ID가 되는 제약도 있었습니다.

### Solution

실제 worktree와 subprocess 환경을 Task 3에 남기고, Stage C를
`LocalEvaluationFixture`와 validation 전용 `CandidateDataView` 두 module의 작은 interface로
제한했습니다. Judge handoff와 candidate-safe manifest의 field·filesystem 계약을 분리하고,
candidate interface에는 final 선택 parameter 자체를 두지 않았습니다. 물리 Judge root에서
읽되 canonical `fixture://` URI를 보고하는 내부 `FixtureActionLogSource` adapter를 기존
Stage B source seam에 배치해 production local/GCS 계약을 바꾸지 않고 독립 run identity를
고정했습니다.

### Result

Stage C 구현자는 fixture 규모·coverage, seed custody, 두 handoff의 exact fields, 게시 충돌,
금지 파일·경로와 독립 재생성 증거를 하나의 정본에서 확인할 수 있습니다. P0-2는 같은
fixture로 성공 metric 경로를 시작할 수 있고 Task 3은 snapshot manifest 해석과 복사 규칙을
중복 구현하지 않습니다. 이 문단은 Stage C runtime 구현 전 계약 검토 당시의 결과이며,
현재 구현·검증 상태는 아래 fixture builder Portfolio Record가 갱신합니다.

## Portfolio Record — Stage C fixture input foundation

### Problem

Stage C 전체 orchestration 전에 descriptor와 candidate-safe manifest의 타입만 선언하면 날짜
관계, partition 순서·중복, `evaluation_id` 형식과 경로가 의미적으로 잘못되어도 JSON 검증을
통과할 수 있었습니다. 또한 fixture 입력이 production의 private Arrow schema 객체를 직접
따르면 production schema 변경이 `youtube-ctr-input-v1`의 bytes와 descriptor identity를 같은
version 아래에서 조용히 바꿀 수 있었습니다. 특히 `T=date.min+2`는 T-2 날짜창 검사를
통과한 뒤 채널 발행일 3650일 offset에서 raw `OverflowError`를 내고 일부 입력을 남길 수
있었으며, `date.max` 부근 요청도 날짜 산술 위험이 있었습니다.

### Solution

`FixtureDescriptor`에 T 기준 `history_start=cutover=T-2`, `evaluation_end=T`, 정확한
T-2..T+1 partition 순서·경로·행 수와 200-user receipt 검증을 추가했습니다. Candidate
manifest는 Stage B의 `eval_<64 lowercase hex>` ID, `slate.parquet`, dt에서 유도한 history
경로, 오름차순·unique 과거 partition과 `complete_history_label_end_date=T-2`만 허용합니다.
Fixture 요청과 입력 generator는 T-2 history partition의 채널 발행일 3650일 offset까지
합친 최대 3652일 과거 범위를 공유해 파일 생성 전에 검사합니다. 입력 generator는
production consumer가 읽을 수 있는 field projection을 `youtube-ctr-input-v1` 전용 Arrow
schema로 별도 고정하고, schema fingerprint와 대표 Parquet SHA를 golden test로 봉인했습니다.

### Result

같은 seed 917·평가일 2026-09-01의 두 staging root에서 virtual-user 입력과 네 YouTube
partition 및 descriptor bytes가 일치함을 테스트로 확인했습니다. Focused model/input
테스트 37개가 통과했고, production schema 객체를 빈 schema로 바꾼 테스트에서도 v1 대표
virtual-user/YouTube Parquet SHA가 유지됐습니다. `date.min`부터 `date.min+3651`까지와
`date.max` 요청은 fixture 경로를 만들기 전에 `fixture_request_invalid`로 실패합니다. 이어 실행한 전체
`tests/research_harness`는 198개 통과·1개 환경 의존 skip이었습니다. 이는 input foundation
완료 당시의 범위 기록입니다. 이후 일일 producer, action log·snapshot 생성, write-once fixture
게시, source adapter, Judge handoff 재검증과 candidate view materialization까지 구현됐지만
이 input foundation 완료 시점에는 독립 two-root 최종 프로토콜이 남아 있어 Stage C 전체를
완료로 표시하지 않았습니다. 현재 상태는 아래 독립 재생성 완료 기록이 갱신합니다.

## Portfolio Record — Stage C fixture builder and Judge handoff

### Problem

Stage C 입력 foundation만으로는 입력이 실제 일일 action log와 Stage B snapshot까지 이어진다는
증거가 없었고, 물리 Judge 경로가 evaluation identity에 섞이거나 부분 fixture·변조 artifact가
재사용될 수 있었습니다. Production producer의 `generated_at`은 실행 시각이라 같은 seed·날짜의
event 의미와 ID가 같아도 Parquet bytes는 달라졌으며, 이는 독립 재생성 검증을 방해했습니다.

### Solution

`build_local_evaluation_fixture()`가 descriptor hash별 sibling staging에서 production
`run_daily_action_log`을 T-2부터 T+1까지 네 번 실행하고, 각 요청의 공식 `seed` 인자로
`fixture_seed`를 전달하도록 구현했습니다. Additive logical completion seam으로 partition 날짜를
production writer에 전달해 `generated_at`까지 결정적으로 쓰며, builder의 사후 Parquet rewrite는
사용하지 않습니다. 내부 `FixtureActionLogSource`는 물리 Parquet를 읽되 Stage B에는
`fixture://<descriptor-hash>/action-log` identity만 전달합니다. Snapshot의 두 split은 실제
slate/label artifact를 다시 읽어 user-slate당 24행, 모든 slate click-positive,
clicked/non-clicked 공존과 160/40 user 수를 검사합니다. Handoff 생성과 fixture reuse는 canonical
outer `_SUCCESS` receipt, exact tree, typed manifest, fingerprint, manifest SHA-256, 네 artifact의
digest·row count, 입력과 action-log receipt를 다시 검증하며 상이하거나 부분인 target을
덮어쓰지 않습니다.

### Result

실제 200-user fixture는 날짜별 action log 5,400행을 생성하고 validation 160개 및 final
holdout 40개 slate 모두 24행·click-positive 조건을 충족했습니다. 같은 seed 917·평가일의
서로 다른 두 Judge root를 사용하는 자동화 테스트는 같은 descriptor, source digest, event ID,
evaluation ID와 snapshot fingerprint를 만들었고 seed 918은 다른 source digest를 만들었습니다.
이 fixture builder 완료 시점에는 서로 다른 Judge root의 독립 최종 비교가 후속 작업으로
남아 있었습니다. 같은 root에서
동시에 시작한 실제 두 process는 descriptor lock 아래 한 번만 게시되고 다른 한 번은 검증된
`reused=true`가 됨을 확인했습니다. Focused 테스트는 완성
target reuse, partial/tamper conflict, handoff marker·schema·fingerprint·manifest bytes·artifact
digest/row-count·extra tree와 special-entry 변조, 일부만 click-positive인 coverage 실패의 typed
code를 확인합니다. Staging cleanup 실패는 원문 path나 원인 문자열 없이 warning을 남기고 기존
성공/실패를 덮지 않습니다. CandidateDataView는 후속 기록과 같이 구현했지만 실제
workspace/subprocess 환경은 생성하지 않았으며 Stage C 전체 완료는 주장하지 않습니다.
최신 검증은 fixture focused 31개 통과·Windows 전용 제약 2개 skip,
전체 `tests/research_harness` 229개 통과·3개 skip, `tests/action_log_generation` 254개 통과였고
changed-file Ruff와 `git diff --check`도 통과했습니다.

## Portfolio Record — Stage C validation CandidateDataView

### Problem

검증용 candidate가 Stage B snapshot이나 fixture root를 직접 받으면 labels, final holdout,
source URI와 seed를 파일명·manifest·receipt를 통해 관찰할 수 있었습니다. 단순 파일 복사는
Judge handoff 또는 source partition의 게시 후 변조, symlink·junction·hardlink alias와 부분
target 재사용을 구분하지 못해 평가 격리를 보장할 수 없었습니다.

### Solution

`materialize_candidate_data_view(request, *, source)`는 시작 전에 Judge `_SUCCESS`, canonical
typed manifest와 SHA/fingerprint, validation/final ID, 네 artifact digest·row count를 공통
handoff validator의 단일 typed bundle로 다시 확인합니다. Manifest semantic validation 오류도
`judge_handoff_invalid`로 정제합니다. 주입 source의 opaque root와 전체 Stage B source receipt
URI를 open 전에 대조하되 `dt<T` history만 열어 실제 Parquet bytes의 SHA-256·행 수를
검증합니다. Candidate에는 validation
slate와 history 두 파티션을 새 bytes로만 복사하고 최소 `candidate-data-view-v1` JSON만 남깁니다.
sibling staging의 exact tree를 검증한 뒤 atomic rename하며, 완전히 동일한 single-link target만
`reused=true`로 허용합니다. final 선택과 consumption registry는 이 interface에 추가하지
않았습니다.

`fixture://` identity는 문자열 일치만 신뢰하지 않습니다. exact 내부
`FixtureActionLogSource`의 physical fixture root를 얻어 snapshot이 그 root의
`evaluation-snapshots/by-hash/<fingerprint>`인지 확인하고, outer `_SUCCESS`, `fixture.json`,
descriptor digest와 전체 fixture integrity receipt를 다시 검증합니다. Local fixture와 local
Arrow source는 physical root·partition path가 candidate destination과 서로 포함 관계이면
거부합니다. 또한 canonical fixture layout에서 검증해 역산한 Judge state root와 destination의
양방향 포함 관계도 거부하며, non-fixture source에는 알 수 없는 state root까지 보호한다고
주장하지 않습니다. Slate와 history source의 가능한 `(st_dev, st_ino)`를 캡처해 최초 게시와 reuse
모두에서 candidate 파일 identity가 source와 다르고 link count가 1인지 확인하며, remote
source는 local identity가 없는 대신 기존 URI·bytes digest·Parquet row receipt 경계를 유지합니다.
예상 가능한 하위 계층 오류를 `StageCError`로 번역하는 모든 public 경계는 exception chaining을
억제해 formatted traceback에도 producer 메시지나 local path가 노출되지 않게 합니다.

### Result

실제 production fixture를 사용하는 focused 테스트에서 validation slate와 T-2/T-1 history의
byte identity·행 수, canonical manifest receipt, 허용 날짜만 open하는 동작을 확인했습니다.
root 또는 T/T+1을 포함한 전체 URI mismatch는 history source open 전에 실패하고 bad bytes,
timezone-naive manifest, Judge marker·manifest·네 artifact
변조, fixture provenance spoof, destination/source reparse·포함 관계와 hardlink,
source↔candidate inode alias, partial·extra·tampered target은 sanitized typed error로
거부됩니다. Receipt와 게시 tree의 파일명·전체 bytes를 탐색해 labels/final ID·path,
snapshot fingerprint, source URI, fixture seed와 Judge path가 없음을 확인했습니다. 동일 완성
target만 `reused=true`였습니다. Judge state root 내부와 그 parent destination 반례 및 producer,
malformed manifest, candidate source 오류의 formatted traceback 비노출도 회귀 테스트로
고정했습니다. Hook 없는 buffer-backed
source도 전체 URI를 대조한 뒤 history 두 날짜만 열었고, 같은 destination의 동시 두 호출은
한 번 게시하고 한 번 `reused=true`로 반환했습니다. 독립 두 Judge root의 최종
프로토콜은 아래 최종 기록에서 완료했습니다. 실제 worktree/subprocess/final consumption
registry는 여전히 후속 범위입니다.

## Portfolio Record — Stage C independent reproduction completion

### Problem

같은 target의 `reused=true`만으로는 source부터 snapshot까지 다시 생성해도 같은 결과가
나온다는 증거가 아니었습니다. 또한 물리 Judge root가 evaluation identity에 섞이면 독립
root의 결과가 달라지고, Candidate와 Judge interface를 합치면 label·final artifact와 source
identity가 반복 연구 경계로 새어 나갈 수 있었습니다. 실제 독립 실행을 추가하자 fingerprint와
artifact는 같아도 실행 시각인 manifest `created_at` 때문에 manifest SHA가 달라지는 결함도
드러났습니다.

### Solution

외부 seam은 `build_local_evaluation_fixture()`와
`materialize_candidate_data_view()` 두 개로 유지했습니다. 내부 typed verifier가 두 receipt의
`reused=false`와 서로 다른 physical root를 요구합니다. 각 receipt는 먼저 해당 exact root의
complete tree, outer `_SUCCESS`, descriptor, canonical snapshot path와 실제 reconstructed
receipt에 다시 결속한 뒤 비교하므로 다른 root의 snapshot/path를 가리키는 receipt spoof를
거부합니다. verifier는 descriptor 및 virtual-user·네 YouTube
input receipt, 날짜순 action-log SHA·행 수·canonical URI, validation/final slate ID projection,
두 evaluation ID, 네 snapshot artifact SHA·행 수, manifest SHA와 fingerprint를 한곳에서
비교합니다. Slate projection은 Parquet에 저장된 행 순서와 중복을 보존한 전체 `slate_id`
sequence의 canonical SHA입니다. 어떤 parse·I/O·비교 실패도 원문 root·user·seed 없이
`fixture_reproducibility_mismatch`로 정제합니다. Stage C는 snapshot 조립의 private clock seam에
평가일 00:00 UTC를 넣어 manifest까지 결정적으로 만들되 Stage B 공개 facade signature는
바꾸지 않았고, 공개 builder는 호출 전후 실제 UTC window 안의 `created_at`을 유지합니다.
CandidateDataView는 각 Judge handoff에서 별도 destination으로 최초
materialize하고, 동일 destination 재호출 reuse는 별도 protocol로 유지했습니다.

### Result

2026-09-02 fresh 실행에서 seed 917·평가일 2026-09-01의 서로 다른 두 기존 absolute Judge
root를 production daily부터 snapshot까지 각각 생성하고 두 fixture와 두 candidate view가 모두
`reused=false`인 상태로 9.664초에 검증했습니다. descriptor는
`d5424f2614020828080597636197b4a0892e94ff8d3886b5acdf4364b1550358`, manifest SHA는
`c39a17535f5f2aa81f031ada34121477575311f3ae3f36c2dfc1abc0f9dd7a6d`, fingerprint는
`17492a8d485d21fb6e1ba30c62afbf0607f1b451debb38d0c0cfabc498d94a54`로 일치했습니다.
네 action-log는 각각 5,400행, validation/final slate·labels는 각각
3,840/3,840/960/960행이었고, candidate manifest SHA는
`291f1b1db07951ff264662018432999856e1c63f180548d1da5ec6a05a115140`로 같았습니다.
두 candidate view의 exact tree와 모든 file bytes가 같고 labels, final ID, snapshot fingerprint,
source URI, fixture seed, virtual-user input과 두 Judge path가 없음을 확인했습니다. 이어 같은
fixture target과 같은 candidate destination을 완전 재검증해 각각 `reused=true`를 별도로
확인했습니다. Immutable receipt의 controlled descriptor difference는 exact typed mismatch를
냈고 원 fixture는 변경하지 않았습니다. Cross-root snapshot redirect와 fixture/descriptor path
spoof도 같은 typed mismatch였으며, 서로 다른 순서·중복의 pure slate projection hash가 다름을
확인했습니다. 최종 전체 `tests/research_harness`는 270개 통과·Windows/POSIX 환경 의존 3개
skip(34.64초), `tests/action_log_generation`은 254개 통과·기존 의존성 warning 2개(9.23초)였습니다. 전체
`autoresearch tests` Ruff와 `git diff --check`도 통과했습니다.

이 완료는 같은 UID의 hostile actor가 descriptor·artifact·manifest·outer marker를 모두
일관되게 다시 쓰는 공격을 방어한다는 뜻이 아닙니다. 또한 CandidateDataView의 Judge 검증과
새 verifier가 `local_evaluation_fixture` 및 `slate`의 private helper에 결합되어 있으므로,
Task 2+를 진행하며 공통 내부 integrity module로 옮기는 리팩터링 후보가 남습니다. 실제
worktree/argv/env와 final consumption, P0-2 metric은 이 P0-1 범위에 포함하지 않았습니다.

## Portfolio Record — Stage B snapshot builder

### Problem

P0-1 이전에는 action log에 후보 묶음의 원천 식별자가 없어 slate를 사후 추정하면 안 되었고,
평가 기간 raw log를 candidate가 읽으면 같은 30분 join으로 숨긴 click label을 복원할 수
있었습니다. 또한 부분 게시나 동일 경로의 상이한 artifact가 재현성 근거를 훼손할 수
있었습니다. Stage B는 producer 계약을 바꾸지 않고, 로컬 Parquet·PyArrow와 기존 action log
계약 안에서 이 경계를 fail-closed로 고정해야 했습니다.

### Solution

기존 action log의 저장된 `slate_id`만 검증해 사용하고, `T`부터 `T_end + 1`까지를 scan하되
출력은 `[T, T_end]` impression으로 제한했습니다. click은 같은 `(user_id, video_id)`의 직전
30분 내 전역 최근 impression에 귀속하고, 고정 SHA-256 80/20 user split으로 validation과
final holdout을 분리했습니다. 네 Parquet artifact에서 slate와 labels를 분리하고 typed
manifest의 fingerprint를 content address로 사용했습니다. 게시은 cooperating publisher의
lock protocol 아래 동일 완성 target만 재사용하며, 다른 내용·불완전 target은 overwrite 대신
`snapshot_write_conflict`로 실패하도록 선택했습니다. 임의 filesystem actor 경쟁, GCS 게시,
fixture/Judge/candidate workspace는 이 단계의 범위에서 제외했습니다.

### Result

Stage B snapshot surface 여섯 항목은 유지되며 package facade에는 이후 Stage C foundation의
typed contract와 순수 identity helper가 추가됐습니다. Stage B builder는 local output에 네
artifact(두 label-free slate와 두 sealed labels)와 `manifest.json`을 게시합니다. Stage B 최종 검증에서 `tests/research_harness`와
`tests/action_log_generation`의 404개 테스트가 통과했고, real local Parquet manual QA는 같은
입력 재빌드의 `reused=true`, 네 artifact의 1:1 join key, tampered target의 typed conflict,
staging residue 없음 을 관찰했습니다. 이는 Stage B 완료 시점 기록으로, 당시 미구현이던
Stage C 일일 producer·write-once fixture·Judge handoff는 현재 구현됐습니다. CandidateDataView와
이 Stage B 완료 시점에는 독립 two-root 최종 실증이 후속 범위였습니다. 현재 P0-1 상태는
Stage C 독립 재생성 완료 기록이 갱신합니다.
실제 candidate workspace와 Sealed Judge는 각각 Task 3과 P0-2의 후속 과제입니다.

## 16. 검증 매트릭스

| 영역 | 필수 검증 |
| --- | --- |
| slate 생성 | 같은 묶음 동일 ID, 다른 user/후보 묶음 다른 ID, worker·shard 수 변화에도 동일 ID |
| 호환성 | legacy parquet 읽기 성공, cutover 이전 제외, 이후 null fail-closed |
| 파티션 | dt와 KST timestamp 일치, `T_end+1` 누락 실패 |
| attribution | 30분 경계, 다음 날 더 최근 impression, timestamp tie-break, slate mismatch 실패 |
| split | 같은 user가 한쪽에만 존재, 동일 입력 동일 split, 빈 split 실패 |
| label 봉인 | slate에 `clicked` 없음, candidate view에 labels/final/source URI/fixture seed/Judge path 없음 |
| lineage | slate-label 1:1, source event 추적, evaluation ID 재현 |
| 게시 | 동일 snapshot 멱등, partial·digest conflict 거부 |
| 전체 | rule-based fixture의 독립 두 run은 source·ID·artifact·fingerprint 일치, 같은 target 재호출은 reuse |

좁은 검증은 다음 순서로 실행한다.

```bash
uv run python -m pytest tests/action_log_generation/ -v
uv run python -m pytest tests/research_harness/test_slate.py -v
uv run python -m pytest tests/research_harness/test_fixture.py -v
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
- candidate view는 safe manifest·validation slate·`dt < T` history의 물리적 복사본만 가진다.
- P0-2-ready coverage를 만족하고 검증된 Judge handoff가 전체 snapshot을 정확히 가리킨다.
- 독립 두 run 재생성과 동일 target reuse가 서로 다른 검증으로 통과한다.

## 18. Task 6 — candidate metadata v2 계약 (2026-09-03, validation 게시·workspace 구현)

상위 실행 기준은 [Research Harness spec §4.5](2026-08-14-paper-grounded-autonomous-ml-research-harness.md)다.
이 절은 **v2 전체 목표와 부분 구현의 경계**를 기록한다. #40에서 §18.7의 순수 정규화·
시점 선택과 `CandidateDataManifestV2` 모델을 구현했다. `CandidateDataManifest`는 여전히
v1만 받는다. #42는 별도 opt-in v2 파일 게시와 validation workspace 연결을 구현한다.
인자를 생략한 기존 v1 경로는 slate와 history만 복사한다. #49는 §18.9의 final 전달을
구현하며 checkpoint 영속화는 후속이다. 기존 v1 reader·fixture·evaluation fingerprint는
그대로 보존한다.

### 18.1 전달 파일과 소유권

candidate view v2는 기존 `harness_in/slate.parquet`, `harness_in/candidate-view.json`,
`harness_in/history/action_log/dt=.../part-0.parquet`에 다음 **두 파일만** 추가한다.

- `harness_in/metadata/users.parquet`: 시점별 사용자 프로필.
- `harness_in/metadata/videos.parquet`: 시점별 영상·채널 메타데이터.

이 경로는 신규 v2 목표 경로다. 최초 구현에는 별도 DB나 범용 metadata registry를 추가하지
않는다. Harness의 metadata materializer가 원본 검증·필드 추출·게시를 소유하고 candidate의
피처 조립 module이 시점 조인·파생 피처·임베딩 계산을 맡는다. 카테고리 설명은 버전 관리된
코드 자산을 사용한다. 모델 파일은 별도 준비된 로컬 실행 자산이며 metadata Parquet에
임베딩 벡터나 모델 가중치를 넣지 않는다. 모델 revision은 실험 재현 기록에서 고정한다.

### 18.2 사용자 파일 — 정확한 목표 컬럼

아래 순서의 Arrow schema를 사용한다. 모든 컬럼과 list 원소는 non-null이다. 빈 키워드·
선호 목록은 `[]`로 허용하지만 필수 컬럼 누락과 null을 빈 값으로 조용히 바꾸지는 않는다.

| 컬럼 | Arrow 타입 | 현재 fixture 원본·의미 |
| --- | --- | --- |
| `user_id` | `string` | 같은 이름의 사용자 ID |
| `available_at` | `timestamp[us, tz=UTC]` | `generated_at`을 timezone-aware UTC로 파싱한 사용 가능 시점 |
| `age` | `int64` | `age`, 0 이상 |
| `occupation` | `string` | `occupation` |
| `watch_time_band` | `string` | 원본 `watch_time_band`, baseline에서 기존 정규화 적용 |
| `hobby_keywords` | `list<string>` | 같은 이름의 취미 키워드 |
| `interest_keywords` | `list<string>` | 같은 이름의 관심 키워드 |
| `lifestyle_keywords` | `list<string>` | 같은 이름의 생활 키워드 |
| `primary_categories` | `list<string>` | 같은 이름의 선호 카테고리, 기존 카테고리 어휘 사용 |

정렬·고유키는 `(user_id, available_at)`이다. ID는 비어 있을 수 없고 중복키는 실패한다.
`source_persona_json`, 원천 식별용 hash·UUID, 인구통계의 불필요한 필드, 생성 모델·prompt·
fixture descriptor는 전달하지 않는다. 새 이력 버전에는 실제 사용 가능 시점을 붙이고,
현재 값을 과거에 알고 있었던 것처럼 소급하지 않는다.

기존 `to_personas_frame()`은 세 키워드 목록을 변환하지만 `primary_categories`를 출력하지
않는다. Task 6의 조립은 이 값을 명시적으로 보존한다. 알려진 선호값을 버린 뒤 키워드 기반
fallback으로 다시 추측해서는 안 된다. 기존 운영 adapter의 동작을 함께 바꿀 필요는 없다.

### 18.3 영상 파일 — 정확한 목표 컬럼

아래 순서의 Arrow schema를 사용하며 모든 컬럼은 non-null이다. 이 최초 계약은 검증된
합성 fixture용이다. 실제 데이터의 비공개 구독자 수·nullable 필드는 별도 명시 계약 없이
숫자를 만들어 이 schema에 맞추지 않는다.

| 컬럼 | Arrow 타입 | 현재 fixture 원본·변환 |
| --- | --- | --- |
| `video_id` | `string` | 같은 이름의 영상 ID |
| `available_at` | `timestamp[us, tz=UTC]` | `collected_at`과 `video_trending_date` 중 늦은 시각 |
| `category_id` | `string` | `video_category`, 기존 카테고리 어휘 |
| `duration_sec` | `int64` | `video_duration`의 ISO 8601 duration을 초 단위로 변환, 양의 정수 |
| `published_at` | `timestamp[us, tz=UTC]` | `video_published_at` |
| `view_count` | `int64` | `video_view_count`, 0 이상 |
| `like_count` | `int64` | `video_like_count`, 0 이상 |
| `comment_count` | `int64` | `video_comment_count`, 0 이상 |
| `channel_subscriber_count` | `int64` | 같은 이름의 원본, 0 이상 |
| `channel_view_count` | `int64` | 같은 이름의 원본, 0 이상 |
| `channel_video_count` | `int64` | 같은 이름의 원본, 0 이상 |

정렬·고유키는 `(video_id, available_at)`이며 중복키·빈 ID·파싱 실패·게시 시각이 사용 가능
시각보다 늦은 행은 실패한다. 영상 제목·설명·URL·생성 설정은 21개 baseline 피처에 필요하지
않으므로 최초 입력에는 넣지 않는다. 향후 텍스트 실험용 카탈로그 확장은 별도로 추적한다.

현재 합성 fixture용 duration parser는 `PT` 뒤 정수 H/M/S 성분을 순서대로 받는다
(예: `PT5M`, `PT1H2M3S`). 음수·소수·0초·int64 범위 초과와 날짜/주 단위 duration은
거부한다. ISO 8601의 모든 형식을 지원한다는 의미가 아니며, 운영의 잘못된 값을 0초로
치환하는 helper와도 구분한다.

### 18.4 시점 선택·누락·평가 split

1. 학습과 예측의 기준 시각 `q`는 해당 impression의 `event_timestamp`다. 각 사용자·영상은
   `available_at <= q`인 행 중 가장 최신 1개만 사용한다. timezone-naive 시각은 거부하고,
   날짜 기반 집계는 KST로 계산한다. 사용 시점보다 미래인 행을 backfill하지 않는다.
2. 제공 대상 ID는 **허용된 history에 등장하는 ID와 현재 전달할 slate의 ID의 합집합**이다.
   final ID를 참조해 validation 묶음을 만들지 않는다. 메타데이터 이력은 해당 ID의 허용된
   학습·예측 요청 중 최대 시각 이하로 제한한다. 각 행의 실제 조인은 다시 1번을 따른다.
3. raw metadata 이력은 여러 요청 시점을 함께 담는다. MVP의 시점 불사용 보장은 기본 조립
   코드와 테스트에서 검증하는 계약이며, candidate가 이력 중 미래 행을 고르는 행위까지
   OS 수준에서 막는다는 뜻은 아니다. 평가 action log·정답은 이와 별개로 계속 숨긴다.
4. 파일·필수 컬럼 부재, schema·digest 오류는 전체 입력 준비 실패다. 반면 해당 시점에
   아직 프로필/영상이 관측되지 않은 경우는 정상 cold-start다. baseline은 기존 모델 계약의
   범주형 `unknown`·수치형 0 기본값을 적용하고 빈 관심/선호 목록의 match·similarity는 0으로
   둔다. 실제 관측 누락과 파일 손상을 혼동하지 않는다. row별 누락 여부와 집계 coverage는
   재현 기록에 남기며, 모든 행에 metadata가 있다고 주장하지 않는다.
5. action log `dt < T`, 완전 학습 라벨 출력일 `<= T-2`, 일 단위 행동 피처의 당일 제외를
   유지한다. 짧은 fixture의 7일·30일 이력 부족은 cold-start/관측 coverage로 기록하며
   데이터가 실제 7일·30일 존재했다고 표현하지 않는다.
6. validation용 view builder는 final 선택 인자를 받지 않는다. final용 builder는 기존
   `FinalConsumptionGrant`를 검증한 뒤에만 현재 final slate와 필요한 metadata를 별도
   workspace에 만든다. grant·marker·Judge 경로와 final 집계 결과는 coding agent에 주지 않는다.
   하나의 final claim 안에서 계획된 paired seed 실행을 하며 새 claim으로 재평가하지 않는다.

baseline과 candidate는 같은 split·seed에서 **동일한 metadata bytes**를 받는다. metadata를
baseline 실행 후 다시 조회하지 않고 run 시작에 Judge 측 receipt로 고정한다. final용
metadata도 시작 시 Judge 측에 고정하지만 candidate에는 소비 권한 획득 후에만 전달한다.
숨겨진 원천 URI·fixture descriptor·전체 카탈로그 hash는 candidate manifest에 노출하지 않는다.

### 18.5 manifest·identity·오류 계약

`CandidateDataManifest` v2는 기존의 `evaluation_id`, `evaluation_start_date`,
`complete_history_label_end_date`, `slate`, `history_partitions` 의미를 유지한다.
`contract_version`은 `candidate-data-view-v2`이며 아래 필드를 추가한다.

- `metadata_contract`: 고정 문자열 `candidate-metadata-v1`.
- `user_metadata`: 기존 `ArtifactReceipt`와 같은 `relative_path`, `rows`, `sha256` 구조.
  경로는 `metadata/users.parquet`로 고정한다.
- `video_metadata`: 같은 receipt 구조, 경로는 `metadata/videos.parquet`로 고정한다.

manifest는 extra-forbid로 검증하고 기존 canonical JSON 규칙으로 hash한다. 평가 slate/label
bytes가 같으면 metadata 추가만으로 evaluation ID를 새로 만들지 않는다. 대신 view manifest
SHA-256으로 metadata identity를 고정하고 run/checkpoint/artifact evidence에 결속한다.
같은 run의 metadata 변경은 재개 오류이며 새로운 입력으로 조용히 재실행하지 않는다.
새 view나 ledger를 만들더라도 기존 final evaluation ID 소비 registry를 우회할 수 없다.

v1을 v2로 제자리 덮어쓰지 않는다. 새 workspace에 atomic/write-once 게시하고 기존 receipt·
경로 안전성·독립 파일 복사 검증을 재사용한다. 예상 밖 파일이나 기존 target의 내용 차이는
실패한다. validation/final 각각 baseline·candidate의 view digest와 모델 identity를 기록한다.
입력 준비 오류는 기존 상위 data-view/실행 실패 경로로 전달하고 원본 행·자격증명·절대
Judge 경로를 오류 메시지에 싣지 않는다. 순수 metadata 변환은 기존
`StageCErrorCode.CANDIDATE_VIEW_CONFLICT`를 사용하며 새 오류 enum을 추가하지 않는다.

### 18.6 첫 검증과 발견 근거

- source field 근거: `fixture_inputs.py`의 두 schema와 `_virtual_user_rows`,
  `_fixture_video_rows`; 피처 목록: `model_contract.MODEL_FEATURE_COLUMNS`.
- 선호 카테고리 보존: `virtual_user_generation/adapter.py`와
  `feature_engineering/assembly.py`의 명시 선호값 우선 규칙을 대조했다.
- cold-start: `feast_retrieval.apply_cold_start_defaults`의 범주형/수치형 규칙을 재사용한다.
  과거 helper의 view-count 근사 등은 운영 계산과 별도로 대조하며, 컬럼명 일치가 전체
  학습 동작의 동일성을 증명하지는 않는다.
- fixture v1의 `generated_at`과 영상 관측 시각은 UTC 00:00이고, 일일 impression은 KST
  하루에 배치된다. 따라서 같은 날짜라도 관측 전에 발생한 노출이 가능하다. 이는 코드 대조로
  확인한 조건이며 발생 행수·성능 영향은 아직 측정하지 않았다. v1 시간을 소급 수정하지 않는다.

구현 테스트는 schema/파싱 실패, hash 변경·재개 거부, 시점 직전·동일·직후 조인,
정상 cold-start와 파일 손상의 구분, `primary_categories` 보존, history cutoff,
validation에서 final 미접근, grant 없는 final view 거부, 두 조건의 metadata 동일성을 포함한다.
원본 fixture bytes나 생성 의미를 변경해야 할 경우 새 fixture 버전과 golden 검증으로 분리한다.

### 18.7 테스트 선행 후 구현한 최소 interface (#40)

아래 이름은 RED 테스트로 고정한 뒤 구현한 **순수 계산·모델 interface**다.
파일 게시와 분리해 입력 변환·시점 선택을 작은 Arrow table로 검증한다. `codebase-design`의
작은 interface 원칙에 따라 schema/값 검증과 정렬·이진 탐색을 하나의 module 안에 둔다.

- `fixture_models.CandidateDataManifestV2`: §18.5의 새 Pydantic 모델. 기존
  `CandidateDataManifest`는 v1 reader로 유지하여 v2 파일을 조용히 받아들이지 않는다.
- `candidate_metadata.normalize_user_metadata(raw: pa.Table) -> pa.Table`:
  필요한 원본 컬럼의 타입·값을 검증하고 §18.2의 exact schema로 정렬·투영한다.
- `candidate_metadata.normalize_video_metadata(raw: pa.Table) -> pa.Table`:
  필요한 원본 컬럼을 검증하고 §18.3으로 정렬·투영한다. 원본의 불필요한 컬럼은 제거하되
  필수 컬럼 부재·타입 오류를 자동 보정하지 않는다. 빈 typed table은 유효한 빈 결과다.
- `candidate_metadata.select_metadata_as_of(metadata: pa.Table, requests: pa.Table,
  *, entity_key: str) -> pa.Table`: key는 `user_id` 또는 `video_id`다. 요청은 key와
  UTC timezone-aware `event_timestamp` 두 컬럼이다. 요청 순서와 중복 요청을 보존하고,
  입력 metadata 정렬 여부에 의존하지 않으며 요청되지 않은 ID는 반환하지 않는다.

시점 선택 결과는 요청 두 컬럼, metadata의 key 이외 컬럼(원래 순서),
`metadata_missing: bool` 순서다. 적합한 과거 행이 없으면 metadata 부분만 null이고
`metadata_missing=True`다. 원래 값 0은 missing이 아니다. 이후 피처 조립이 cold-start
기본값을 적용하므로 이 selector가 age나 count를 미리 0으로 채우지 않는다. 빈 요청도
같은 결과 컬럼의 0행 table이다. duplicate metadata key·invalid 요청은 실패한다.

정규화/selector의 공개 오류는 기존 `StageCError`로 전달한다. raw metadata의 누락·null·
잘못된 타입, timezone-naive 시각, 중복키와 날짜 범위 초과를 거부한다.
원본의 추가 열은 제거하지만 중복 열 이름은 거부한다. selector는 정규화된 schema만 받으며
외부에서 직접 넘긴 metadata 값도 다시 검증한다.
manifest 검증은 기존 패턴처럼 `ValidationError` 또는 `StageCError`를 허용한다.
metadata receipt의 rows는 bool이 아닌 0 이상 정수, digest는 소문자 hex 64자다.

테스트는 성공 입력을 먼저 검증한 후 실패 입력을 넣는다. 모든 입력을 무조건 거부하는
구현이 오류 테스트를 통과하지 않게 한다. 최초 RED에서는 제품 module 부재를 collection
오류나 skip 대신 명시적인 assertion으로 확인했다. 실제 구현 후 같은 테스트가 GREEN이며,
production stub·xfail은 추가하지 않았다.
§18.7의 테스트는 순수 계산만 검증한다. 실제 validation 파일 게시와 workspace는 §18.8의
integration 테스트로 추가 검증하며, grant·checkpoint 재개는 여전히 후속이다.

### 18.8 validation 파일 게시·workspace 연결 (#42, 구현 계약)

이번 단계는 validation 전용이다. `prepare_candidate_metadata(judge, *, source)`는 검증된
`FixtureActionLogSource`만 받아 원본 receipt를 대조하고, 허용 history의 impression과
validation slate에서 entity별 최대 요청 시각을 구한다. 해당 ID의 그 시각 이하 관측만
정규화·직렬화한다. final slate를 선택에 사용하지 않는다(기존 전체 fixture/snapshot
무결성 검증은 그대로 수행한다). 원본 metadata 오류는 필터 전 검증에서 실패한다.

결과 `PreparedCandidateMetadata`는 평가 ID·snapshot fingerprint와 두 Parquet의
immutable bytes/receipt를 Harness 메모리에 보관한다. baseline/candidate는 같은 객체를
재사용한다. 원본 경로·descriptor·seed는 candidate manifest와 process context에 없다.
새 프로세스에서의 bundle 복구·checkpoint 결속과 final용 bundle 준비는 후속 계약이다.

`materialize_candidate_data_view_v2(request, *, source, metadata)`는 기존 v1과 별도로
명시적으로 선택한다. 기존 lock·staging·atomic rename·exact tree·독립 복사 검증을 재사용하고
metadata hash/rows/schema 및 snapshot/evaluation identity를 재확인한다. 같은 target은
정확히 같은 manifest와 파일일 때만 재사용한다. v1 target의 제자리 upgrade는 거부한다.

`open_candidate_workspace(..., metadata=bundle)`은 v2를 게시하며, 인자를 생략하면 v1이다.
workspace의 기존 `candidate_view_sha256`은 v2 manifest 전체 digest여서 metadata 변경도
반영한다. 실패 시 workspace를 회수한다. 이 필드 제공이 Controller checkpoint 영속화까지
완성했다는 뜻은 아니다. final 권한·registry와 모델 실행 경로는 이번 단계에서 변경하지 않는다.

### 18.9 final metadata·소비 grant·workspace 연결 (#49)

validation 공개 interface에는 split/final 선택 인자를 추가하지 않는다. 다음 final 전용
interface는 Harness 소유이며 coding agent에 Judge handoff나 grant를 넘기는 통로가 아니다.

- `prepare_final_candidate_metadata(judge, *, source)`는 검증된 fixture의 final slate와
  허용 history impression을 기준으로 §18.8과 동일한 정규화·관측 제한을 적용한다.
  `PreparedCandidateMetadata`의 evaluation ID는 final ID다. run 시작에 Judge 측에서
  준비하고 baseline/candidate의 계획된 paired seed 전체에서 같은 bytes를 재사용한다.
- `materialize_final_candidate_data_view(request, *, source, metadata, grant)`는 실제
  `FinalConsumptionGrant` 타입과 현재 marker·handoff 동일성을 검증한 뒤 final v2만 게시한다.
  missing/duck grant, 다른 snapshot, 삭제·변조된 marker와 validation bundle 혼용은 실패한다.
  기존 target 재사용 직전과 staging rename 직전에도 권한을 재확인한다. 같은 claim 안의
  계획된 paired 실행을 허용하는 것이지 새 claim이나 실패 후 재평가를 허용하지 않는다.
- `open_final_candidate_workspace(request, *, source, metadata, grant)`는 기존 detached
  checkout·credential 검사·회수 구현을 공유한다. candidate process context에는 기존
  `cwd/slate/predictions/environment`만 있고, handoff·grant·marker 경로는 넣지 않는다.
  `candidate_view_sha256`은 final metadata를 포함한 전체 v2 manifest를 식별한다.

v1/validation v2의 입력과 오류 의미는 유지한다. 공통 private 구현에서 선택한 split의
slate receipt만 사용하며 history cutoff, 독립 byte copy, exact tree, lock/write-once 게시와
metadata schema/hash 검증을 공유한다. final 데이터 준비 실패는 기존 StageC/workspace
오류로 정제한다. OS 사용자에 의한 악의적 파일 race까지 완전 차단한다고 주장하지 않는다.

**fixture와 소비 상태의 결합.** 기존 registry의 고정 상태 루트는 snapshot의
`evaluation-snapshots/by-hash` 상위이며 fixture snapshot에서는 fixture root 자체다.
소비 기록을 생성하면 기존 fixture exact-tree와 충돌하는 것을 실제 임시 복사본으로
재현했다. registry 위치를 바꾸지 않고 fixture 검증에 선택적인
`final-holdout-consumed` 디렉터리 및 현재 final evaluation ID 이름의 일반 파일 하나만
허용한다. 빈 디렉터리도 허용하며 다른 파일·하위 폴더·alias는 계속 거부한다. 원본 데이터
receipt/fingerprint는 바꾸지 않는다. marker 내용의 권한 검증은 registry가 담당하므로
validation 데이터 검증이 marker 내용만을 이유로 실패하지는 않는다. final은 marker가
삭제되거나 내용이 바뀌면 기존 grant도 거부한다.

이 단계는 final 입력 전달까지다. 준비 bytes의 새 프로세스 복구·run/checkpoint 결속,
실제 agent·Controller adapter·REPORT·실측은 후속이며 단위 테스트의 final 소비를 실제
실험 완주로 계산하지 않는다.
