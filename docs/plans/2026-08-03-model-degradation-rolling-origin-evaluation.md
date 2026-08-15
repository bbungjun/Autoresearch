# 모델 성능 열화 시점 측정 — rolling-origin 평가 구현 계획 (#471)

정본 계약: `docs/specs/2026-08-03-model-degradation-rolling-origin-evaluation.md`

**용어 명확화**: 이 구현은 엄밀히는 **단일 cutoff 기반 forward degradation evaluation**이다
(spec §2 서두). 이슈 본문·완료조건이 "rolling-origin evaluation"이라는 관용 표현을 쓰므로
함수명(`run_rolling_origin`)과 모듈 목적 서술은 그 표현을 그대로 유지하되, 여러 cutoff를
간격을 두고 반복하는 진짜 다중 origin은 이 구현 범위가 아니다(spec §10).

**구현 시 반드시 지킬 경계**(spec §8 재확인): `src/pipeline/experiment_evaluation.py`
(`evaluate_experiment`/`decide_promotion`)는 **#493에서 판정 엔진 단일화 작업 중인 파일**이다.
이 plan의 어떤 Task도 그 파일을 수정하지 않는다. `paired_experiment.py`,
`promotion_gate.py`도 읽기만 하고 호출하지 않는다.

## 파일별 책임

| 파일 | 변경 |
| --- | --- |
| `src/pipeline/degradation_eval.py` | 신규 — 날짜/상태 계약, `run_rolling_origin`, `detect_degradation_point` |
| `src/cli.py` | `measure-degradation` 명령 추가 |
| `scripts/bench/degradation_curve_plot.py` | 신규 — Plotly 시각화 |
| `pyproject.toml`, `uv.lock` | `plotly` 의존성 추가 |
| `docs/README.md` | "🏋️ 학습 파이프라인" 절에 이 spec/plan 등재 |
| `experiments/2026-08-03_model-degradation-rolling-origin/` | Task 1 실측 결과·축소 설정 실행 raw 결과(git 미추적, `CLAUDE.local.md` 관례) |

## Task 1 — BigQuery 실측(구현 착수 전 필수, 코드 변경 없음)

- [ ] `scripts/verify_offline_coverage.py --days 30`을 GCP 접근 가능 환경에서 실행해
      `training_entity`/`video_feature` 등 결손일 분포를 확정한다.
- [ ] spec §3의 `D` 쿼리(`GREATEST(0, DATE_DIFF(expected_latest_date, MAX(...), DAY))`)를
      실행해 `D`를 확정한다.
- [ ] `A = DATE_DIFF(expected_latest_date, data_start_date, DAY) + 1`을 실행 시점 날짜로
      재계산한다.
- [ ] 위 결과로 spec §3의 예시 표를 실측치로 교체하고, Task 7-A/7-B에서 쓸 실제
      `(W, H)` 조합을 정한다.

검증: 실행 로그를 `experiments/2026-08-03_model-degradation-rolling-origin/notes.md`에
Before 필드로 남긴다(`CLAUDE.local.md` 실험 기록 관례).

## Task 2 — 날짜 구간·평가일 상태 계약 (spec §2.1, §2.3)

- [ ] `degradation_eval.py`에 순수 함수로 날짜 구간 헬퍼를 만든다:
      `training_window(cutoff_date, window_days) -> (events_start_date, events_end_date)`
      (§2.1의 `cutoff-1` 보정 포함), `evaluation_dates(cutoff_date, horizon_days) ->
      list[(date, elapsed_days)]`(경과일 `0..H-1`).
- [ ] 평가일 상태 enum `EvaluationStatus`(`valid`/`missing_date`/`insufficient_rows`/
      `single_class`/`evaluation_failed`)를 정의한다.
- [ ] `insufficient_rows` 임계치는 1차로 `build_training_dataset.DEFAULT_MIN_ROWS_PER_DAY`
      (5000, `build_training_dataset.py:86`)를 재사용한다 — 학습 spine의 "붕괴일 판정"
      철학을 평가일에도 같은 근거로 적용하는 것이 임의 상수보다 낫다. Task 1 실측에서
      평가일 표본 규모가 학습 spine과 다른 경향(예: 하루 표본이 원래 작음)이 확인되면
      재조정한다.
- [ ] `single_class`/`insufficient_rows` 판정은 `evaluate_held_out_roc_auc` 호출 **전에**
      그날 데이터셋의 `clicked` 분포·행 수를 먼저 읽어 분류한다(§2.3 — 예외를 사후에 잡아
      분류하지 않는다).

검증: `uv run python -m pytest tests/test_degradation_eval_dates.py -v`(신규)

## Task 3 — 평가 실행과 결과 스키마 (spec §2.2, §2.3)

- [ ] `RollingOriginResult`/`PerDayResult`(pydantic) 모델을 정의한다. 필드는 spec §2.3
      그대로: `date`, `elapsed_days`, `status`, `roc_auc`(nullable), `evaluation_provenance`,
      `video_staleness_summary`.
- [ ] `evaluation_provenance`는 `TrainingSnapshotManifest`(§1)를 그대로 쓰지 않고
      **평가 전용 경량 모델** `EvaluationSnapshotProvenance`(`dataset_sha256`, `row_count`,
      `positive_count`, `negative_count`, `feature_service`, as-of 조회 시각)를 새로 둔다.
      근거: `TrainingSnapshotManifest`는 `registry_generation`/`code_archive_sha` 등 승격·
      재현성 감사용 필드까지 포함해 하루 평가마다 반복 생성하기엔 무겁고, 그 필드들의
      의미("누가 이 모델을 학습했나")가 평가일 provenance("이 데이터로 무엇을 관측했나")와
      다르다.
- [ ] `run_rolling_origin`을 구현한다: cutoff 학습 1회(§2.2 step 1-2, Task 2 헬퍼로 날짜
      계산) → 평가일 목록 순회(step 3) → 상태 판정 → `valid`만 `evaluate_held_out_roc_auc`
      호출.
- [ ] `evaluation_failed` 기본 fail-fast, `best_effort: bool = False` 키워드 인자를
      `run_rolling_origin`에 추가한다(§2.3). CLI 노출 여부는 Task 6에서 확정.
- [ ] **산출물 경로 격리 계약**: `build_training_dataset.main`을 학습 1회 + 평가일마다
      (최대 `H`번) 반복 호출하는데, 각 호출이 같은 기본 `output_path`/metadata 경로를
      쓰면 이전 평가일 산출물을 덮어써 `evaluation_provenance.dataset_sha256`이 가리키는
      파일이 사라진다. `run_rolling_origin(cutoff_date, ..., run_root: str)`을 필수
      인자로 받아 다음 구조로 분리한다.

      ```text
      run_root/
        training/
          training_dataset.csv
          snapshot_manifest.json
          model.joblib
          feature_columns.json
        evaluation/
          2026-07-25/
            dataset.csv
            dataset_manifest.json   # EvaluationSnapshotProvenance
          2026-07-26/
          ...
      ```

      각 평가일은 독립된 `output_path`(`evaluation/<date>/dataset.csv`)와
      `EvaluationSnapshotProvenance`(Task 3 위 항목)를 가지며, 같은 이름으로 재실행할
      경우 **조용히 덮어쓰지 않는다** — `require_explicit_experiment_output`
      (`build_training_dataset.py`)이 이미 쓰는 fail-closed 관례를 따라, `run_root`가
      이미 존재하고 비어 있지 않으면 명시적 `overwrite=True` 없이는 에러로 막는다.

**참고 자산**: `scripts/bench/window_holdout_eval.py`가 이미 "arm CSV로 학습 → 별도
홀드아웃 CSV로 ROC-AUC 채점"이라는 같은 뼈대(`train.main` 호출 + `roc_auc_score`)를 갖고
있다. 다만 이 스크립트의 `score_holdout()`은 `evaluate_held_out_roc_auc`를 재사용하지 않고
자체적으로 `roc_auc_score`를 다시 부른다(§1이 이 중복을 미처 못 잡았던 부분) — 이 plan의
새 코드는 `evaluate_held_out_roc_auc`를 그대로 쓰고, `window_holdout_eval.py`를 리팩터링해
같은 함수를 쓰게 하는 것은 **이 이슈 범위 밖**(요청되지 않은 광범위한 리팩터링)이므로
건드리지 않는다.

검증: `uv run python -m pytest tests/test_degradation_eval.py -v`(신규)

## Task 4 — degradation_point 판정 (spec §2.4)

- [ ] `detect_degradation_point(per_day, baseline, min_auc_drop) -> DegradationPoint | None`을
      구현한다. "2개 연속 유효 관측치"(무효일 스킵, 리셋 없음) 규칙을 §2.4 그대로 따른다.
- [ ] `min_auc_drop`은 **변동폭 → 임계값 변환 규칙**만 이 자리에서 고정하고, 구체적 수치는
      Task 7-A(아래)의 실측으로 정한다.

      ```text
      min_auc_drop = max(min_auc_drop_floor, k × seed_std)
      ```

      `seed_std`는 Task 7-A에서 `seed_sweep.summarize_metric`(§1)로 구한 baseline
      `val_roc_auc`의 시드 간 표본표준편차(개별 관측치 기준, 평균의 표준오차가 아니다)다.

      `k=2`는 **seed 간 변동폭의 약 두 배보다 작은 하락을 열화로 판정하지 않기 위한
      초기 휴리스틱**이다 — 이 저장소의 기존 95% CI 계산 구조(`seed_sweep.py`의
      `_T_CRITICAL_95`, `mean ± t_critical × seed_std / sqrt(n)`)를 참고했지만, `k ×
      seed_std` 자체는 **평균의 95% 신뢰구간이 아니다**(그러려면 `seed_std`를
      `sqrt(n)`으로 나눈 표준오차와 `n`에 따른 `t_critical`을 써야 한다). 이 규칙은
      개별 관측치의 산포를 직접 임계값으로 쓰는 휴리스틱일 뿐, 통계적으로 보정된
      신뢰구간이라고 주장하지 않는다.

      `min_auc_drop_floor`는 `seed_std`가 우연히 0에 가까워 임계값이 사실상 0이 되는
      퇴화 상황(무의미하게 예민한 탐지)을 막는 하한이며, 1차 제안값은 `0.005`다.
      **`k=2`, `floor=0.005` 모두 Task 7-A 실측 후 기록되는 초기 설정값**이며 이
      plan은 최종 정책값으로 못 박지 않는다 — 고정하는 것은 "변동폭에서 임계값으로
      변환하는 규칙의 모양"뿐이다.
- [ ] 유효 평가일이 2개 미만이면 `degradation_point=None`, `reason="insufficient_valid_points"`.

검증: `uv run python -m pytest tests/test_degradation_eval_detection.py -v`(신규)

## Task 5 — video staleness 측정 (spec §4)

**정정**: 이 Task의 이전 초안은 `days_since_upload`를 video feature staleness로 잘못
채택했다. `days_since_upload = DATE_DIFF(collected_at, video_published_at)`
(`autoresearch/jobs/feature_store_build.py:260`, 원문 확인)로, **수집 시점에 그 행에
고정으로 박히는 "콘텐츠 나이"**다 — PIT 조회가 그 행을 나중에 어느 시점 기준으로
골라오는지와는 무관하다. spec §4가 원래 정의한 staleness(§4 "완화책": "스냅샷
`event_timestamp`와 평가일 간 차이")와는 다른 값이며, 이 plan이 잘못 지름길을 탄
것이었다 — spec은 정정할 필요 없이 이 Task만 spec 정의를 다시 따르면 된다.

```text
video_feature_age = evaluation_entity_timestamp - selected_video_feature_timestamp
```

`selected_video_feature_timestamp`(PIT join이 그날 평가에서 실제로 고른 `video_feature`
행의 `event_timestamp`)는 `retrieve_training_features`(`feast_retrieval.py:64-125`)가
반환하는 DataFrame에 **노출되지 않는다** — `store.get_historical_features(...).to_df()`는
`FeatureService`가 선언한 피처 컬럼만 돌려주고, 각 FeatureView의 소스 타임스탬프는
버려진다(코드로 직접 확인). 다음 순서로 조사·구현한다.

- [ ] **1) Feast 네이티브 지원 조사**: `get_historical_features`가 소스 타임스탬프를 함께
      반환하는 옵션(예: 내부 diagnostic 플래그, `full_feature_names`류)이 있는지 이
      저장소가 고정한 Feast 버전 문서·소스를 확인한다.
- [ ] **2) 안 되면 진단 전용 별도 조회**: `video_feature` 테이블에 Feast의 PIT join과
      동일한 규칙(ASOF, `VideoFeatureView.ttl=None`이라 상한 없음, §4)으로 진단 쿼리를
      직접 던진다 — `SELECT video_id, MAX(event_timestamp) AS selected_ts FROM
      video_feature WHERE video_id IN (...) AND event_timestamp <= @as_of GROUP BY
      video_id`. 평가일 CSV의 모델 피처 조회와는 **별도 경로**이며, staleness 진단
      용도로만 쓰고 모델 입력에는 영향을 주지 않는다.
- [ ] **3) 그래도 정확한 source timestamp를 못 얻으면**: staleness 수치를 만들어내지
      않고 `video_staleness_summary.status = "unavailable"`과 사유를 결과에 남긴다 —
      부정확한 값을 그럴듯한 숫자로 내는 것보다 안전하다.
- [ ] `video_staleness_summary`(mean/max `video_feature_age`)는 `apply_cold_start_defaults`
      가 영상 미발견 시 채우는 기본값이 섞인 행을 제외하고 집계한다(`feast_retrieval.py:191-206`)
      — 채워진 행 비율은 `evaluation_provenance`의 missing/default 비율(Task 3)에 이미
      담기므로 중복 없이 분리한다.
- [ ] `days_since_upload`는 **보조 콘텐츠 연령 지표**로만 결과에 별도 필드(예:
      `content_age_days_summary`)로 남길 수 있다 — `video_staleness_summary`라는 이름으로
      쓰지 않는다.

검증: `uv run python -m pytest tests/test_degradation_eval_staleness.py -v`(신규)

## Task 6 — Plotly 시각화·CLI

- [ ] `plotly` 의존성을 `pyproject.toml`에 추가하고 `uv lock`으로 lockfile을 갱신한다
      (이 저장소에 현재 `plotly` 의존성이 없음을 `pyproject.toml` 직접 확인함).
- [ ] `scripts/bench/degradation_curve_plot.py`를 만든다(`scripts/bench/`가 이미
      `compare_seed_sweeps.py`/`window_holdout_eval.py` 같은 실험 보조 스크립트 자리이므로
      같은 위치를 따른다): `RollingOriginResult` JSON을 읽어 x축 `elapsed_days`, y축
      `roc_auc`, `missing_date` 등 무효일은 결측으로 표시(선을 잇지 않음), 기준선·
      `degradation_point` 마커를 그린다.
- [ ] `src/cli.py`에 `measure-degradation` 명령을 추가한다(§2.2). `--best-effort` 플래그로
      `run_rolling_origin`의 `best_effort`를 노출한다 — 근거: 하루 평가 실패로 전체를
      fail-fast시키면 비용이 큰 cutoff 학습까지 반복해야 하므로, 우연한 BigQuery flakiness
      상황에 운영자가 선택할 수 있어야 한다(내부 전용 상수로 고정하면 그 선택지가 없다).

검증: `uv run python -m pytest tests/test_cli.py -v`

## Task 7-A — threshold calibration (spec §2.4의 `min_auc_drop` 실측)

Task 4가 `degradation_point` 판정에 쓰는 `min_auc_drop`은 이 Task가 먼저 확정해야
Task 7-B(최종 실행)가 가능하다 — "Task 4 구현 → 최종 실행 → 그 실행 결과로 threshold를
정함"이라는 순환(threshold 없이는 실행할 수 없는데, threshold를 실행 결과로 정하려 함)을
피하기 위해 calibration을 별도 선행 단계(Task 7-A)로 분리한다.

- [ ] Task 1 실측 `(W, H)` 조합의 `W`(학습 구간)로 cutoff 학습 데이터셋을 1회 조립한다.
- [ ] `seed_sweep.run_seed_sweep`(§1, 재사용)으로 같은 데이터셋을 소규모 시드 집합(예:
      3개 — §1이 명시한 "1차 구현은 시드 1개"는 rolling-origin curve 자체의 반복 비용
      절감 방침이지, 이 1회성 calibration에는 적용되지 않는다)으로 반복 학습해
      `val_roc_auc`를 모은다.
- [ ] `summarize_metric`(§1)으로 `seed_std`(표본표준편차)를 구한다.
- [ ] `min_auc_drop = max(min_auc_drop_floor, k × seed_std)`(Task 4의 규칙, 1차 `k=2`,
      `floor=0.005`)로 최종 `min_auc_drop`을 계산하고, 계산 과정과 근거를
      `experiments/2026-08-03_model-degradation-rolling-origin/notes.md`에 기록한다.

검증: calibration 실행 로그와 `seed_std`/`min_auc_drop` 계산값을
`experiments/2026-08-03_model-degradation-rolling-origin/raw_calibration.json`으로 보관.

## Task 7-B — 최종 열화 곡선 실행과 보고 (spec §7 "데이터 부족 시 대안 범위")

- [ ] Task 1 실측 `(W, H)` 조합과 Task 7-A에서 확정한 `min_auc_drop`으로
      `measure-degradation`을 1회 실행한다(§1의 방침대로 이 실행 자체는 단일 시드).
- [ ] `experiments/2026-08-03_model-degradation-rolling-origin/notes.md`에
      `CLAUDE.local.md` 5필드 포맷(배경/문제, Before, After, 선택 근거&Trade-off, 재현
      방법)으로 기록한다. Before는 "이 실행이 처음이라 이전 값 없음"으로 명시하고,
      선택 근거&Trade-off에는 spec §7의 caveat("1차 목표는 측정 프레임워크 동작 증명,
      이 축소 설정의 열화/비열화 결론 자체는 재학습 주기 정책의 확정 근거로 쓰지 않는다")
      를 그대로 옮긴다.
- [ ] 결과를 포트폴리오·추적 문서로 정리할 가치가 있는지 판단한다(`CLAUDE.local.md` 관례).

검증: 실행 로그와 결과 JSON을 `experiments/2026-08-03_model-degradation-rolling-origin/`에
`raw_final_run.json`으로 보관.

## Task 8 — 문서

- [ ] `docs/README.md`의 "🏋️ 학습 파이프라인" 절에 이 spec/plan을 등재한다(기존
      paired-offline-experiment 항목과 같은 형식).
- [ ] `degradation_eval.py` 모듈 최상단에 `[파이프라인]`/`[기능]`/`[비책임]` 형식
      docstring을 작성한다(`CLAUDE.md` Module Responsibility 규칙, `[비책임]`에 §8의
      #493 경계를 명시한다).

검증: `git diff --check`

## 전체 검증

```bash
uv run python -m pytest -v
uv run --no-sync ruff check agent_orchestration autoresearch tests tools
```

로컬에 `libomp`가 없으면 LightGBM 의존 테스트(이 plan이 만드는 `test_degradation_eval*.py`
포함, `train.main` 호출 경로라 LightGBM을 거친다)가 수집 단계에서 실패한다(환경 문제,
변경과 무관). 해당 파일은 CI에서 검증한다.

feast 계열 변경(§5 `days_since_upload` 조회 경로)이 있으므로
`uv sync --only-group feast` 환경에서 CI `pytest (feast group)` job의 테스트 목록도
실행한다.
