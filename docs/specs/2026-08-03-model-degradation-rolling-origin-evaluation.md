# 모델 성능 열화 시점 측정 — rolling-origin 평가 (#471)

- **상태**: Proposed
- **날짜**: 2026-08-03
- **이슈**: #471 (선행: 멘토 코칭 17차, 연계: #466 통계적 유의성, #461 승격 게이트 payload)
- **선행 계약**:
  - `docs/specs/2026-08-01-training-window-coverage-guard.md` (spine 커버리지 가드, #464)
  - `docs/specs/2026-08-03-paired-offline-experiment-comparison.md` (paired 비교 계약, #454) —
    이 spec이 다루지 **않는 것**의 경계 근거로 인용한다.
  - `docs/guides/training-experiment-provenance.md` §5 (write-once evidence, #423/#466)

> [!IMPORTANT]
> **[이슈 흡수 — #485, 2026-08-04]** 이 spec이 다루던 rolling-origin 측정 하네스는
> 설계·구현 변경 없이 그대로 유효하다. `#485`(최신성·모델 열화 기반 시간축 평가)와
> 범위가 겹쳐 `#471`을 close하고 이 문서·구현을 `#485`로 흡수했다 — 정정이나 대체가
> 아니라 **관련 이슈 이동 + 범위 확장**이다. 구현물은 브랜치
> `feat/471-model-degradation-rolling-origin`에 보존되어 있으며 그대로 재사용한다.
> 재사용/신규 경계는 아래 "`#485` 흡수 경계" 절을 참고한다.

## `#485` 흡수 경계 (재사용 vs 신규)

`#485`로 이관된 뒤 이 문서·구현이 커버하는 범위와, `#485`가 새로 정의해야 하는 범위를
명확히 가른다.

| 구분 | 항목 | 출처 |
| --- | --- | --- |
| **재사용(이미 구현됨)** | rolling-origin 실행 하네스, 단일 cutoff 학습 → 하루 단위 순차 평가 | `#471`(`autoresearch/model_evaluation/degradation_eval.py`의 `run_rolling_origin`) |
| | Plotly 열화 곡선 시각화 | `#471`(`scripts/bench/degradation_curve_plot.py`) |
| | CLI 진입점(`measure-degradation`) | `#471`(`autoresearch/cli.py`) |
| **신규(`#485`가 정의)** | hard retrain limit 산출 로직(성능과 무관하게 강제 재학습하는 시점) | `#485` 작업 범위 |
| | baseline/challenger가 동일 as-of cutoff·feature snapshot·split 규칙·paired seed를 쓰는지 검증 | `#485` — `#478`의 30개 paired seed·write-once receipt 계약 재사용 |
| | `#425` 판정 스키마(`confidence`/`robustness_note`/`direction_vs_*`)에 temporal signal 연결 | `#485` |
| | 데이터 부족·미래 구간 누락·feature snapshot 불일치·시간 순서 위반 시 `hold`로 종료하는 fail-closed 로직 | `#485` |
| | production Feature Service·production alias는 이 작업에서 직접 변경하지 않음 | `#485` 명시 제약 |

`#472`(하드 리밋 값을 `#461` 게이트에 배선)와의 경계는 `#472` 본문(2026-08-04 갱신)을
정본으로 한다 — 값 산정은 `#485`, 게이트 배선은 `#472`.

## 목적

현재 모델 채택 기준(무작위 샘플링 + 다중 시드 유의성 판정, #407/#441)은 시간 흐름에
따른 성능 열화를 반영하지 않는다. 과거 특정 시점(`cutoff_date`) 데이터로 학습한
모델을 이후 날짜에 하루 단위로 순차 적용해 일별 ROC-AUC를 측정하고(rolling-origin
evaluation), 성능이 꺾이는 지점을 Plotly로 시각화해 재학습 주기 정책의 근거를
만든다.

## 비목적 (무엇을 측정하지 않는가)

- **baseline vs candidate 코드/피처 비교** — `paired_experiment.py`(#454)의 영역이다.
  이 spec은 조건 하나(고정된 코드 버전)를 시간 축으로만 이동한다.
- **승격 여부 판정**(`eligible`/`reject`/`hold`) — `autoresearch/model_evaluation/experiment_evaluation.py`
  (`evaluate_experiment:365`, `decide_promotion:511`)의 영역이며, 이 파일은 현재
  **#493에서 판정 엔진 단일화 작업 중인 파일**이다. 이 spec은 이 모듈을
  호출하지도, 수정하지도 않는다(§8).
- **다중 시드 유의성 검정 로직 자체의 재작성** — `seed_sweep.py`가 이미 구현했다
  (#407/#441). 이 spec은 그 함수를 day별로 반복 호출할 뿐 판정 통계를 새로 만들지
  않는다.
- **통계적 change point detection의 고도화** — 이슈 완료조건이 "단순 임계값 또는
  통계적 변화점 탐지 중 택1, 선택 근거 문서화"를 요구한다. 이 spec은 단순 임계값
  방식을 택해 판정 규칙을 §2.4에서 확정한다. 통계적 변화점 탐지는 §6에서 기각 근거를
  남기고 범위에서 제외한다.
- **Video/User 피처의 시점 정합성을 보정하는 것** — staleness는 측정해 결과에
  기록하지만(§4), 보정 로직(예: age 기반 가중치, 재조회)은 만들지 않는다.
- **재학습 자동 트리거** — 후속 이슈("재학습/강제 교체 주기 정책", 이슈 본문상
  아직 미발행)의 영역이다.

## 1. 기존 자산 재사용 범위

| 자산 | 위치 | 이 spec에서 |
| --- | --- | --- |
| spine 커버리지 가드 | `build_training_dataset.py:271-373`(`summarize_spine_coverage`/`require_spine_coverage`) | **그대로 재사용.** 절대 일수 기준(요청 대비 비율이 아님, `require_spine_coverage` docstring)이라 cutoff별로 반복 호출해도 판정 의미가 흔들리지 않는다. |
| 임의 구간 조회 | `build_training_dataset.py:186` `load_training_entity_spine(start_date, end_date)` | **그대로 재사용.** cutoff 이전 학습 구간과 cutoff 이후 하루치 평가 구간을 각각 그대로 넘긴다. |
| ROC-AUC 계산 | `evaluate.py:36-50` `evaluate_held_out_roc_auc(model, dataset, feature_columns) -> float` | **그대로 재사용.** 이미 순수 함수이고 `evaluate.py:142`·`train.py:801`이 공유 중이라 day별 반복 호출에 적합하다. |
| 모델·피처 로드 | `evaluate.py:105-112` `load_model`/`load_feature_columns`/`require_model_feature_columns` | **그대로 재사용.** cutoff 학습이 끝난 모델 아티팩트를 day별 평가 루프에서 반복 로드하지 않고 1회 로드해 재사용한다. |
| MLflow 지표 기록 | `tracking/logger.py:50-58` `log_metrics(metrics, step=None)` | **그대로 재사용.** `step` 인자에 "cutoff 이후 경과일"을 채워 시계열로 기록한다(§9 결과 매핑). |
| provenance manifest | `training_provenance.py:96-137` `TrainingSnapshotManifest`/`TrainingSplitManifest` | **확장 없이 조회만, cutoff 학습 셋에 한정.** cutoff 학습 셋의 provenance를 결과 payload(`RollingOriginResult.snapshot_manifest`)에 남기는 데 쓴다. `paired_experiment.py`가 쓰는 write-once evidence store 강제는 적용하지 않는다 — 이건 승격 후보가 아니라 측정·리포트이기 때문이다. **평가일별 provenance는 별도**다(§2.3 `evaluation_provenance`) — 이 manifest를 그대로 재사용할지 경량 스키마를 새로 둘지는 plan 단계에서 결정한다. |
| 다중 시드 통계 | `seed_sweep.py:217-427` `summarize_metric`/`run_seed_sweep` | **재사용 가능, 1차 범위는 좁힘.** day별로 시드를 반복하면 비용이 `일수 × 시드수`로 곱해지므로, 1차 구현은 시드 1개(재현용 고정 시드)로 curve를 내고, 시드 반복 확장은 §7 대안 범위에서 다룬다. |
| PR-AUC/LogLoss/Brier | `evaluate.py:143,145,146` (인라인) | **재사용 안 함** — §5. |

## 2. 신규 구현 범위

이 spec의 1차 범위는 **단일 origin**이다 — cutoff 하나를 학습 경계로 고정하고, 그 이후
`H`일을 하루 단위로 순회 평가한다(이슈 본문의 "과거 특정 시점 데이터로 학습 → 이후
날짜별 데이터에 순차 적용"과 동일). 여러 cutoff를 간격 `S`로 이동시키며 반복하는
**다중 origin** 확장(문헌상 "rolling-origin"이 원래 가리키는 전체 형태)은 이 spec
범위 밖이며 §10에 후속 확장으로 남긴다.

### 2.1 날짜 구간 계약

`load_training_entity_spine(start_date, end_date)`(`build_training_dataset.py:186`)의
실제 쿼리는 `WHERE DATE(event_timestamp,'Asia/Seoul') BETWEEN start_date AND
end_date`(`:177`)로 **양끝 포함(inclusive-inclusive)**이다. 이 spec은 half-open
구간을 다음처럼 고정하고, 실제 호출 시 종료일 인자를 명시적으로 하루 당겨 반영한다.

```text
학습 구간:  [cutoff-W, cutoff)   — cutoff 당일은 학습에 포함하지 않는다
평가 구간:  [cutoff, cutoff+H)   — cutoff 당일이 첫 평가일이다(elapsed_days=0)
```

`events_start_date`/`events_end_date` 인자로 환산하면:

```text
학습 조회:            events_start_date = cutoff - W,  events_end_date = cutoff - 1
                      (BETWEEN이 inclusive라 cutoff 당일을 빼려면 -1이 필요하다)
평가 조회(경과일 i, i=0..H-1):  events_start_date = events_end_date = cutoff + i
```

`elapsed_days`는 **관측 순번이 아니라 cutoff 기준 달력 일수**(`(평가일 - cutoff).days`)다.
결손일도 자신의 달력 위치에 해당하는 `elapsed_days`를 그대로 갖되 상태를
`missing_date`로 표시한다(§2.3) — 그래야 x축과 재학습 주기 판단이 실제 달력을
왜곡하지 않는다. 예: 8월 5일 데이터가 없고 8월 6일을 평가하면, 8월 6일의
`elapsed_days`는 (관측 순번이 아니라) cutoff로부터의 실제 달력 일수 그대로다.

### 2.2 신규 모듈

새 모듈 `autoresearch/model_evaluation/degradation_eval.py`(가칭):

- `run_rolling_origin(cutoff_date: str, window_days: int, horizon_days: int, ...) -> RollingOriginResult`
  1. §2.1 계약대로 `events_end_date = cutoff_date - 1일`로 `build_training_dataset.main`을
     호출해 학습 데이터셋을 조립한다(기존 함수 재사용, §1).
  2. `train.main(...)`으로 모델을 1회 학습한다(기존 함수 재사용). 학습이 이미 계산하는
     `val_roc_auc`(`train.main()` 내부 val split 검증 블록, `roc_auc_score(y_val, y_val_pred_proba)` 호출 지점 — 라인 번호 대신 심볼로 참조한다, PR #510 리뷰)를 §2.4 기준선으로 그대로 가져오며, 별도로
     재계산하지 않는다.
  3. `cutoff_date`부터 `cutoff_date + horizon_days - 1`까지(경과일 `0..H-1`) 하루씩
     순회하며, 그날 하루치를 `build_training_dataset.main`(`events_start_date=
     events_end_date=그날`)으로 조립하고 §2.3의 상태를 판정한 뒤 `valid`면
     `evaluate_held_out_roc_auc`를 호출한다.
  4. 결과를 구조화된 값(`RollingOriginResult`, pydantic 모델)으로 반환한다 — 필드는
     §2.3·§2.4 참고.
- `detect_degradation_point(...)` — §2.4.
- Plotly 시각화(스크립트 또는 `agent_orchestration`/`scripts/` 산출물 — 배치 위치는
  plan 단계에서 결정): x축 `elapsed_days`(달력 기준), y축 ROC-AUC. `missing_date`
  등 무효일은 결측으로 표시하고 선을 잇지 않는다. 기준선·열화 지점 마커를 포함한다.

CLI: `autoresearch/cli.py`에 `measure-degradation` 명령 추가(가칭). `docs/specs/2026-07-13-public-batch-execution-contract.md:17-18`이 "그 밖의 학습·평가와 FastAPI serving command는 각 기능이 운영화될 때 별도 revision으로 추가한다"고 명시하므로, 이 명령을 그 계약에 즉시 등재할 필요는 없다. **판단 근거**: 이 명령은 Airflow DAG가 부르지 않는 **operator 수동 도구**다(`sweep-seeds`/`compare-paired-experiment`와 같은 성격) — 그래서 `public-batch-execution-contract.md`에는 등재하지 않되, `README.md`의 `Dockerfile.train` 서브커맨드 목록(PR #510 리뷰 지적)에는 다른 두 도구와 같은 이유로 등재한다.

### 2.3 평가일 상태와 결과 스키마

일별 데이터에 양성 또는 음성 클래스 하나만 있으면 `roc_auc_score`가 예외를 던진다
(sklearn) — ROC-AUC를 계산할 수 없는 날을 조용히 건너뛰지 않고 상태로 남긴다.

```text
valid              — 정상 평가, roc_auc 값 있음
missing_date       — 그 날짜의 spine/평가 데이터 자체가 없음(action_log 없음 등, §3 "결손일 영향")
insufficient_rows  — 행 수가 임계치 미만(임계치를 §1 spine 가드의 DEFAULT_MIN_ROWS_PER_DAY와
                      같이 쓸지, 평가 전용 별도 값을 쓸지는 plan 단계에서 결정)
single_class       — 그 날 라벨이 단일 클래스(전부 클릭 또는 전부 비클릭)라 ROC-AUC 정의 불가
evaluation_failed  — 그 외 예외(환경·조회 오류 등)
```

`missing_date`/`insufficient_rows`/`single_class`는 **분석 가능한 데이터 부족 상태**다
— `roc_auc=null`로 남기고 §2.4의 열화 탐지에서 제외하되(연속성 판정에서 건너뛰고
리셋시키지 않음, §2.4), 실행 자체는 계속 진행한다. `single_class`/`insufficient_rows`
판정은 `evaluate_held_out_roc_auc` 호출 **전에** 그날 데이터셋의 라벨 분포·행 수를
먼저 확인해 분류한다 — `roc_auc_score`의 예외를 사후에 잡아 분류하지 않는다.

`evaluation_failed`는 **다르다** — 환경·조회 오류(BigQuery 연결 실패, 스키마 불일치,
모델 로드 실패 등)까지 다른 무효 상태처럼 조용히 건너뛰면, 실패한 실행이 정상 분석처럼
완료된 것으로 보일 위험이 있다. 기본 동작은 다음과 같다.

```text
기본값: evaluation_failed 발생 시 run_rolling_origin 전체를 즉시 실패시킨다
        (예외를 전파한다 — 그 날짜만 건너뛰고 계속 진행하지 않는다)
best_effort=True(명시적 옵션): 개별 평가일 실패를 evaluation_failed로 기록하고
        실행을 계속한다. 이 모드에서만 evaluation_failed도 §2.4 열화 탐지에서
        missing_date와 동일하게 제외 처리된다.
```

`per_day` 원소 스키마:

```text
date, elapsed_days, status, roc_auc(nullable)
evaluation_provenance:
  dataset_id 또는 fingerprint   # 그날 평가 CSV의 식별자. §1 TrainingSnapshotManifest
                                # 재사용 여부는 plan 단계에서 결정(무겁다고 판단되면
                                # 평가 전용 경량 스키마를 새로 정의한다)
  row_count, positive_count, negative_count
  feature snapshot 범위          # get_historical_features 조회의 as-of 시각 범위
  missing/default 비율           # apply_cold_start_defaults가 채운 비율
video_staleness_summary        # §4
```

`RollingOriginResult`는 위 `per_day` 목록 외에 `cutoff_date`, `degradation_point`(§2.4),
`snapshot_manifest`(cutoff 학습 셋 provenance, §1)를 포함한다.

### 2.4 degradation_point 판정 규칙(확정)

> [!IMPORTANT]
> **[부분 supersede — #485, 2026-08-04]** 아래 `baseline` 정의(cutoff 학습의
> `val_roc_auc`)는 `docs/specs/2026-08-04-temporal-signal-promotion-integration.md`
> §4.3에서 개정됐다. 사유: §10이 기록한 산출 경로 불일치 — 랜덤 val 분할 지표와
> forward held-out 지표는 이 저장소 실측에서 약 4%p 오프셋이 있어
> (`experiments/2026-07-31_training-window-length/notes.md`), `elapsed_days` 0~1에서
> 오탐이 난다. 개정 내용:
>
> ```text
> baseline = forward_baseline_roc_auc (per_day 중 첫 valid 관측치)
>            — 같은 산출 경로끼리 비교해 §10의 4%p 오프셋을 상쇄한다.
> cutoff 학습의 val_roc_auc는 baseline_val_roc_auc로 결과 필드에 유지하되(하위호환),
> 판정 로직(detect_degradation_point 호출)에는 사용하지 않는다.
> ```
>
> `degraded`·`degradation_point`의 나머지 규칙(절대 하락폭, "2개 연속 유효 관측치")은
> 그대로 유효하다. `run_rolling_origin` 실행 기록이 0건인 시점의 개정이라 소급 영향이
> 없다.

```text
baseline = cutoff 학습의 val_roc_auc (train.main() 내부 val split 검증 블록에서 계산된 값을 그대로 재사용)
degraded(day) = day.status == valid  AND  day.roc_auc ≤ baseline - min_auc_drop
degradation_point = "2개 연속 유효 관측치에서 degraded"가 처음 성립하는 시점의 elapsed_days
```

- `min_auc_drop`은 **절대 하락폭**이다(비율 아님) — `val_roc_auc`가 이미 0~1 스케일
  단일 값이라 절대값이 해석하기 쉽고, 기존 승격 게이트(`promote.py`)도 절대 임계치를
  쓴다. 구체적 수치는 plan 단계에서 실측 기반으로 정한다 — 이 spec은 **규칙의 모양**만
  고정한다.
- **"2회 연속"은 달력상 연속이 아니라 유효 관측치 순서상 연속이다.** `missing_date` 등
  무효일(§2.3)은 사이에 끼어도 건너뛸 뿐 카운트를 리셋하지 않는다. 예: 월요일과
  수요일이 `degraded`이고 화요일이 `missing_date`라면, 화요일을 건너뛰고 월/수를
  "2개 연속 유효 관측치"로 세어 수요일에 `degradation_point`가 성립한다.
- 유효 평가일이 2개 미만이면 `degradation_point=null`이고 사유
  `insufficient_valid_points`를 결과에 남긴다(§7 "데이터 부족 시" 대응과 연결).

## 3. 데이터 가용성 제약과 (W, H) 상한

**확정**: 데이터 시작일 `data_start_date` = 2026-07-07 (`docs/specs/2026-07-27-feast-pit-phase0-audit.md:43`, 원문 재확인 완료: "보정 1: 데이터 시작일 = 2026-07-07").

**물리 상한 `A`(공식)**: "정상적으로 존재해야 하는 마지막 완료일"을 `expected_latest_date`(아래 `D` 쿼리와 같은 정의: 오늘 - 1일)로 잡으면,

```text
A = DATE_DIFF(expected_latest_date, data_start_date, DAY) + 1
```

이 spec 작성 시점(2026-08-03) 기준 `expected_latest_date` = 2026-08-02이므로 `A = 27`이다.
**`A`는 오늘 날짜가 바뀔 때마다 달라지는 값**이며, 이 spec은 "27"을 고정 상수로 쓰지
않는다 — 구현·실행 시점에 위 공식으로 재계산해야 한다. (이전 초안은 `expected_latest_date`를
"어제"로 정의해 놓고 상한 계산에는 "오늘"까지 포함해 `28`을 썼는데, 이는 자기모순이었다 —
이번 개정으로 `expected_latest_date` 기준 하나로 통일했다.)

**미확정 변수 — 실측 필요**: spine(`training_entity`)이 `expected_latest_date`까지 채워져 있는지, 아니면 반영 지연 `D`일이 있는지. `phase0-audit.md:53`의 "07-26(D+2 미도래)"는 **측정일(2026-07-27) 기준 개별 사실**이며, 이 저장소 어디에도 "spine이 상시 D+2 지연으로 채워진다"는 SLA 문서화는 없다. **`D=2`를 기본값으로 채택하지 않는다** — `D`는 실측 전까지 미확정 변수로 남긴다.

**시도한 명령과 그 명령이 실제로 측정하는 것**:
```
$ uv run --no-dev --group feast python scripts/verify_offline_coverage.py --days 5
GCP_PROJECT_ID (또는 --project)가 필요합니다
```
`.env` 부재·`gcloud` 미인증으로 BigQuery 조회 자격 증명이 없어 **실행 자체가 실패**했다(쿼리가 발행되지 않음).

이 스크립트가 하는 일을 정확히 밝힌다(`scripts/verify_offline_coverage.py:44-65` `_daily_sql`, 원문 확인): `training_entity`를 포함한 5개 테이블 각각에 대해 `[오늘-days일, 어제]` 고정 윈도우 안에서 `GENERATE_DATE_ARRAY`로 만든 날짜 목록과 실제 존재하는 날짜(`DATE(event_timestamp, 'Asia/Seoul')` 집계)를 대조해 **그 윈도우 안의 결손일 목록**을 돌려준다. 이는 **전역 `MIN`/`MAX` 조회가 아니다** — "오늘 기준 spine이 며칠 전까지 채워져 있는가"(`D`)를 직접 알려면 이 스크립트의 결손일 목록에서 윈도우 꼬리(어제, 그제, …)가 결손으로 잡히는지를 읽어내야 하며, 그 자체가 목적인 더 직접적인 방법은 아래 쿼리다.

→ **Task 1(구현 착수 전 필수 스텝)**로 다음을 GCP 접근 가능한 환경(로컬 `.env` 설정 또는 Airflow)에서 실행해 `D`를 확정한다. "정상 계약은 spine이 최소 어제까지는 채워져 있어야 한다"는 것을 기준(`expected_latest_date`)으로 삼고, 실제 최신 날짜가 그보다 최신(파이프라인이 기대보다 앞서 있는 경우)이면 `D`를 0으로 floor한다 — 이전 식(`DATE_DIFF(CURRENT_DATE(...), latest_date, DAY) - 1`)은 이 경우 `D=-1`이 나오는 결함이 있었다.

```sql
DECLARE expected_latest_date DATE DEFAULT DATE_SUB(CURRENT_DATE('Asia/Seoul'), INTERVAL 1 DAY);
SELECT
  MAX(DATE(event_timestamp, 'Asia/Seoul')) AS latest_date,
  GREATEST(0, DATE_DIFF(expected_latest_date, MAX(DATE(event_timestamp, 'Asia/Seoul')), DAY)) AS lag_days_d
FROM `<project>.<dataset>.training_entity`
WHERE NOT REGEXP_CONTAINS(user_id, r'^user_[0-9]{4}$');  -- 더미 seed 제외(verify_offline_coverage.py:38-41 패턴 재사용)
```

보조로 `scripts/verify_offline_coverage.py --days 30`도 함께 실행해 그 30일 창 안의 결손일 목록(§ "결손일 영향" 참고)을 확정한다 — 이 스크립트는 `D`가 아니라 결손일 분포를 확정하는 데 쓴다.

**상한식(확정)**: 실제 사용 가능 기간은 물리 상한 `A`에서 반영 지연 `D`를 뺀 값이다. §2가 확정한 대로 이 spec의 1차 범위는 단일 origin(학습 `W`일 + 평가 `H`일)이므로,

```text
W + H ≤ A - D
```

여러 origin으로 확장할 경우(간격 `S`, origin 개수 `N`, §2 서두에서 범위 밖으로 명시한
다중 origin 확장):

```text
W + H + (N - 1) × S ≤ A - D
```

`A`는 위 공식으로 계산되는 값(이 spec 작성 시점 기준 27)이고, `D`는 Task 1 실측 전까지
**미확정**이며 특정 값(예: 2)을 기본값으로 채택하지 않는다. 아래 표는 `A=27`(이 spec
작성 시점) 기준 계산식을 보여주기 위한 **예시(illustration)**이며 채택된 기본값이 아니다
— 구현 시점에는 `A`를 재계산한 값으로 이 표를 다시 만들어야 한다.

| 가정 | 물리 상한(A-D) | H=10일 때 W 상한 | H=5일 때 W 상한 | H=3일 때 W 상한 |
| --- | --- | --- | --- | --- |
| D=0(예시) | 27 | 17 | 22 | 24 |
| D=2(예시, phase0-audit.md:53의 개별 사실과 같은 값) | 25 | 15 | 20 | 22 |

학습 spine 커버리지 가드 자체는 `usable_days ≥ min_days`(기본 3, `build_training_dataset.py:85` `DEFAULT_MIN_COVERAGE_DAYS`)만 요구하므로 `W`의 하한은 `D` 값과 무관하게 사실상 3이다.

멘토 사례(180일 학습/10일 검증 = 190일)는 `D=0`으로 가장 낙관적으로 잡아도 현재 가용 데이터(`A=27`일)의 **약 7배**를 요구한다 — 이슈 본문이 이미 이 우려("가용 데이터가 적으므로 샘플링 방식 확정 필요")를 제기했고, 이번 조사로 수치로 확인됐다. §7에서 대안을 다룬다.

**결손일 영향**: `phase0-audit.md:28-34,47-53`의 07-07~07-26 20일 창 기준, `training_entity`는 백필 후에도 6/20일 결손(action_log 없는 날). 근거는 같은 문서 "action_log 없는 날은 impression(=학습 예제) 자체가 없어 PIT 조회 대상이 아니다" — 즉 결손일은 §2.3의 `missing_date` 상태로 분류되고 평가 포인트가 될 수 없다. `video_feature`도 18/20(07-08, 07-11 결손). 2026-07-27 이후 결손율이 이어지는지는 **확인 실패** — Task 1 실측에 포함한다.

## 4. VideoFeatureView 무제한 staleness

`feature_repo/feature_definitions.py:158-177`, `ttl=None`(174행), 사유 주석: "트렌딩 이탈 = 인기 식음 신호라 마지막 스냅샷 유지(모델링 결정)". `UserDynamicView`도 `ttl=60h`(42, 153행)로 결손 2일 초과 시 콜드스타트 기본값으로 전환되며 이 전환 자체가 성능에 영향을 줄 수 있지만, 그 staleness는 60시간으로 **상한이 있다**. Video는 상한이 없다.

rolling-origin이 cutoff+`i`일째(elapsed_days=`i`)를 평가할 때, 그 시점 video 피처가 실제로는 훨씬 오래된 스냅샷일 수 있다 — "모델 자체의 열화"와 "video 피처의 staleness로 인한 성능 저하"가 뒤섞인다. 날짜가 하루씩 전진할수록 이 왜곡 폭이 무한정 커질 수 있는 **무제한 staleness가 가능한 FeatureView**다(User/Similarity는 정적이라 애초에 시간에 따라 안 변하는 것이 정상 동작이고, UserDynamic은 60시간 상한이 있어 왜곡 폭에 천장이 있다).

**완화책(측정만, 보정 안 함)**: 일별 평가 시 `evaluate_held_out_roc_auc` 호출과 별도로, 그 날 평가셋의 video 피처 age(스냅샷 `event_timestamp`와 평가일 간 차이)를 함께 집계해 `per_day` 결과에 `video_staleness_summary`(mean/max age)로 남긴다. 열화 곡선과 나란히 놓고 해석은 사람이 한다 — 자동 분리는 이 spec 범위 밖.

age를 뽑아내는 구체적 방법(`feast_retrieval.py` 쪽에 이미 노출된 필드가 있는지, 아니면 별도 조회가 필요한지)은 **확인 실패** — 구현 단계(plan)에서 조사한다.

## 5. PR-AUC/LogLoss/Brier 인라인 — 순수 함수 분리 여부

`evaluate.py:140-151`에 PR-AUC/LogLoss/Brier가 `main()` 내부 인라인으로 남아 있다(ROC-AUC만 `evaluate_held_out_roc_auc`로 분리됨, §1).

**판단**: 이번 이슈 완료조건은 "ROC-AUC(또는 기존 지표)"를 요구하고, 열화 곡선의 y축은 ROC-AUC 단일 지표로 충분하다(`autoresearch/model_registry/promote.py`의 승격 게이트도 `val_roc_auc` 단일 지표만 쓴다 — #390). PR-AUC 등을 rolling-origin에 포함할 필요가 이 이슈 범위에서 확인되지 않는다.

→ **이번 spec은 ROC-AUC만 다루고, `evaluate_held_out_roc_auc`를 그대로 재사용한다. PR-AUC/LogLoss/Brier의 순수 함수 분리는 이 spec에 포함하지 않는다** — 필요해지면 별도 리팩터링 이슈로 분리한다(`CLAUDE.md` "구조 변경과 동작 변경은 분리" 원칙).

## 6. 통계적 change point detection을 기각하는 근거

§3의 데이터 가용성 제약상 `H`(평가 기간)가 3~10일 수준으로 작다. 통계적 변화점 탐지(예: CUSUM, Bayesian change point)는 유의미한 검정력을 얻으려면 관측치가 최소 수십 개는 필요한데, 일별 관측치가 10개 미만인 상황에서는 탐지 결과의 신뢰구간이 사실상 무의미하다. 따라서 1차 구현은 **단순 임계값 방식**(§2.4 `detect_degradation_point`)을 택하고, 데이터가 축적된 뒤(§7) 관측치가 충분해지면 통계적 방법으로 재검토하는 것을 후속 과제로 남긴다.

## 7. 데이터 부족 시 대안 범위

180일 학습/10일 검증 같은 이상적 설정은 현재 데이터(§3)로 불가능하다. 이슈 완료조건 "최소 1회 실제 데이터로 실행한 보고서"는 §3 표의 예시 조합 중 실측된 `A`/`D`로 재계산한 조합(축소된 `W`/`H`)으로 만족시키고, 결과 해석에 다음을 명시한다:

1. **1차 목표는 "측정 프레임워크가 동작함"을 증명하는 것**이다 — 축소 설정에서 나온 열화/비열화 결론 자체는 재학습 주기 정책의 확정 근거로 쓰지 않는다.
2. 실제 열화 시점에 대한 결론은 데이터가 축적된 뒤(수개월 후 window가 넓어졌을 때) 같은 프레임워크로 재측정해서 낸다.
3. 결과 리포트(§9)에 이 caveat을 명시한다.

## 8. #493과의 경계

`autoresearch/model_evaluation/experiment_evaluation.py`(`evaluate_experiment:365`, `decide_promotion:511`)는 **#493에서 판정 엔진 단일화 작업 중인 파일**이다. 이 spec은:

- 이 파일을 **수정하지 않는다**(함수 추가·시그니처 변경 모두 금지).
- `paired_experiment.py`, `promotion_gate.py`도 **읽기만 한다** — 애초에 rolling-origin은 승격 판정이 아니므로 호출 대상도 아니다.
- 향후 열화 시점 산출물(`degradation_point`)이 재학습 주기 정책(후속 이슈) 또는 `#461` 승격 게이트 payload에 연결될 필요가 생기면, **"연결 지점"만 문서로 남긴다**: `RollingOriginResult.degradation_point`를 promotion 판정에 반영하려면 `experiment_evaluation.py`에 무엇이 필요한지를 이 spec의 "알려진 계약 간극" 절(§10)에 적어 두고, 실제 배선은 하지 않는다 — 필요해지면 별도 이슈로 분리한다.

## 9. 완료 조건과의 대응

| 이슈 완료 조건 | 이 spec의 대응 |
| --- | --- |
| 임의 cutoff 입력 → Plotly 열화 곡선 | `run_rolling_origin` + 시각화 스크립트(§2) |
| 열화 시작 지점 자동 탐지 + 구조화된 값 | `detect_degradation_point`(단순 임계값, 판정 규칙 §2.4, 통계적 방법 기각 근거 §6) |
| 최소 1회 실제 데이터 실행 보고서 | §7의 축소 설정으로 실행, `CLAUDE.local.md`의 실험 기록 관례(`experiments/`, git 미추적)를 따라 raw 결과를 남기고, 가치가 있으면 추적 문서로 정리 |

## 범위 제외

- 재학습 자동 트리거(후속 이슈, "재학습/강제 교체 주기 정책" — 이슈 본문상 아직 미발행)
- `#461` 승격 게이트 payload 배선(연결 지점만 §8·§10에 문서화)
- 통계적 change point detection 고도화(§6)
- Video/User feature staleness 보정 로직(§4 — 측정만 하고 보정하지 않음)
- PR-AUC/LogLoss/Brier 순수 함수 분리(§5 — 필요해지면 별도 이슈)

## 10. 알려진 계약 간극

- **spine 반영 지연 `D` 미확정**: §3의 물리 상한 `A`(공식 `DATE_DIFF(expected_latest_date, data_start_date, DAY) + 1`, 작성 시점 기준 27)는 달력 기준으로 계산 가능하지만, 실사용 상한 `A-D`의 `D`는 미확정이다. Task 1(§3의 `MAX(event_timestamp)` 쿼리 + `verify_offline_coverage.py --days 30`)이 구현 착수 전 필수 선행 작업이며, 실행 시점에 `A`도 재계산해야 한다. 결과에 따라 `(W, H)` 표(§3, 현재는 `D=0`/`D=2` 예시일 뿐 채택된 기본값 아님)가 달라진다.
- **다중 origin 확장 미착수**: §2 서두에서 명시한 대로 이 spec의 1차 범위는 단일 origin이다. 여러 cutoff를 간격 `S`로 이동시키는 전체 "rolling-origin"(`W + H + (N-1)×S ≤ A-D`, §3)은 데이터 가용성이 더 넉넉해진 뒤 후속 확장으로 검토한다.
- **`min_auc_drop` 구체값 미확정**: §2.4가 판정 규칙의 모양(절대 하락폭, 2회 연속, 유효일 기준)은 고정했지만 임계값 자체는 plan 단계에서 실측 기반으로 정한다.
- **`insufficient_rows` 임계치 결정 방식 미확정**: §1의 `DEFAULT_MIN_ROWS_PER_DAY`(5000, 학습 spine 기준)를 평가일 판정에도 그대로 쓸지, 평가 목적에 맞는 별도 값을 쓸지 plan 단계에서 결정한다.
- **평가일 provenance의 `dataset_id`/`fingerprint` 산출 방법 미확정**: §2.3에서 `TrainingSnapshotManifest`(§1) 재사용 여부를 열어 뒀다 — 무겁다고 판단되면 평가 전용 경량 스키마를 plan 단계에서 새로 정의한다.
- **시드 반복 범위 축소**: §1에서 1차 구현은 시드 1개로 좁혔다. `seed_sweep.run_seed_sweep`으로 일별 다중 시드까지 확장하면 비용이 `일수 × 시드수`로 곱해지므로, 확장 여부는 §3 실측 결과와 함께 plan 단계에서 재검토한다.
- **degradation_point → 승격 게이트 연결**: §8에서 명시한 대로 이 spec은 배선하지 않는다. 연결이 필요해지면 `#493`의 `experiment_evaluation.py` 계약과 충돌하지 않는지 먼저 확인한다.
- **video 피처 age 추출 방법**: §4에서 "확인 실패"로 남긴 대로, `feast_retrieval.py`가 age를 이미 노출하는지는 plan 단계에서 조사가 필요하다.
- **video staleness는 현재 CLI에서 도달 불가능**(PR #510 리뷰): 평가일 CSV는 `MODEL_FEATURE_COLUMNS + clicked`만 담고 `video_id`/`event_timestamp`(엔티티 키)를 보존하지 않는다. `_resolve_staleness_summary`는 이 두 컬럼이 없으면 `UNAVAILABLE`로 안전하게 떨어지도록 수정했지만(크래시 방지), `measure-degradation` CLI가 `bigquery_client`/`bigquery_project`/`bigquery_dataset`를 아예 받지 않아 이 기능은 지금 공개 경로로 켤 방법이 없다. 켜려면 (a) 평가일 조립이 엔티티 키를 보존하도록 계약을 확장하거나 (b) CLI에 BigQuery 연결 옵션을 추가해야 하며, 둘 다 이 PR 범위 밖이다. 또한 `_resolve_staleness_summary`는 하루 전체에 단일 as_of(그날 KST 자정)를 쓰므로, 실제 per-row PIT 대비 **체계적으로 더 stale한 방향**(최대 ~24h)으로 치우친다 — 정확한 값은 평가 CSV가 행별 `event_timestamp`를 보존해야 가능하다.
- **baseline과 per-day forward ROC-AUC는 산출 경로가 다르다**(PR #510 리뷰): `baseline_val_roc_auc`는 cutoff 학습의 랜덤 val 분할 지표이고 `per_day`는 forward held-out 지표다. 이 저장소의 실측(`experiments/2026-07-31_training-window-length/notes.md`)은 랜덤 val이 실제 다음 날 성능보다 **약 4%p 높게** 나옴을 보였다 — `elapsed_days` 0~1 부근에서 잡히는 `degradation_point`가 이 상수 오프셋 때문인지 실제 열화인지는 이 결과만으로 구분되지 않는다. baseline 정의를 `per_day[0]` 기준으로 바꾸는 안은 §2.4 계약 변경이라 이 PR에서 다루지 않았다 — **`#485`가 이어받아 §2.4를 부분 supersede했다**(`docs/specs/2026-08-04-temporal-signal-promotion-integration.md` §4.3, 위 §2.4의 admonition 참고).
