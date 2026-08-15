# 피처 계약 정렬 — DuckDB ↔ offline store 값 diff 판정 (#357)

> 작성: 2026-07-27 | 상태: Task 1 실측 완료 · **(A)(B)(C) 확정** (ttl: 일 스냅샷 60h / 정적·video None) |
> 관련: [EPIC] #299(학습 데이터셋 Feast PIT 전환), #356(Phase 0 spine 빌드),
> #358(FeatureService 조회 전환), #359(DuckDB 제거), #365(폐루프 수집 구멍)

> **표본 한계 (강조)**: 정상 데이터는 **11일뿐**(07-07, 07-12~21). 나머지는 폐루프
> 수집 구멍(#365). 아래 모든 수치는 **n=11일, 참고용**이며 통계적 결론 확정용이
> 아니다. 결손일은 stale 오판을 유발하므로 diff 대상에서 제외했다.

## 결정

학습 경로(`build_training_dataset.py`의 DuckDB 재계산)와 offline store 정의
(`autoresearch/jobs/feature_store_build.py`의 BigQuery SQL)는 **동형이 아니다**.
`user_dynamic_feature` 6개 피처가 광범위하게 어긋나며(불일치율 10~69%), 원인은
① **카운팅 기반 차이**(주), ② **timezone 버킷 차이**(부, DuckDB 잠재 버그)다.

- **offline store 정의를 정본으로 채택한다** — 서빙이 읽는 값 = 학습이 배우는 값을
  맞추려면, raw event_type을 직접 세고 KST 자정을 앵커로 쓰는 offline이 옳다. DuckDB
  경로는 wide-format 근사(30분 귀속 생존분)에 UTC 버킷 버그까지 겹쳐 있다.
- #358(조회 전환)은 DuckDB 재계산을 offline PIT 조회로 교체하며, 그 순간 위 diff가
  자동 해소된다. 이 스펙은 **무엇이 왜 달랐는지**의 정본 기록이다.

## 실측 방법 (재현 가능)

diff 하네스: `scripts/diff_feature_contract.py` (BigQuery 필요, `uv run python
scripts/diff_feature_contract.py --project <gcp-project-id>`). verify_offline_coverage
스타일 read-only.

- **offline 경로**: `_USER_DYNAMIC_SELECT` / `_VIDEO_SELECT`를 partition_date별
  BigQuery에서 **재실행**한다. 적재된 테이블을 읽지 않으므로 결손일 ttl stale
  fallback(#356)이 값에 섞이지 않는다.
- **DuckDB 경로**: 같은 raw를 내려받아 `derive_wide_events` →
  `compute_point_in_time_user_features`를 실제 학습 코드로 돌린다. query_points =
  대상일 실제 임프레션.
- **정렬**: 두 경로는 query point가 다르다(DuckDB=임프레션 시각별, offline=일 스냅샷
  1개). KST 날짜 D의 임프레션은 Feast PIT 조회 시 D-00:00 스냅샷을 받으므로
  `(user_id, KST 날짜)`로 조인해 apples-to-apples로 만든다.
- 규모: 비교 임프레션 **1,775,808** / DuckDB user-day 73,992 / offline user-day 76,813.

## 6개 지점 판정

| 지점 | 실측 | 판정 |
| --- | --- | --- |
| 1·2 timezone / off-by-one | 하루내 변동 그룹 total **66,921/73,992(90%)**, recent_* 13~17k, affinity 6,803 | 확정 diff (DuckDB 버그) |
| 3 집계 정의 | total_event **69.1%**, recent_click/view/like **~12.7%**, watch abs_max 6,417 | 확정 diff |
| 4 무활동 기본값 | offline-only **2,821 user-day**, duck-only 0 | 확정 (커버리지 차) |
| 5 스냅샷 결손 stale | 재실행 방식 + 결손 없는 11일로 배제 | 설계로 회피 |
| 6 카테고리 dedup | affinity **10.1%** 불일치(top 전부 `X↔Gaming`), video category 0.0% | 확정 diff (반응 가중) |

### 지점별 상세

- **지점 1·2 (timezone)** — 올바른 KST 일 버킷이면 같은 `(user, KST일)`의 모든
  임프레션은 **동일 값**이어야 한다(당일 제외라 창이 임프레션 시각에 안 달림). 그런데
  `total_event_count_7d`가 **90%(66,921/73,992) user-day에서 하루 안에 흔들린다**.
  원인은 DuckDB가 `CAST(as_of AS DATE)`를 **naive UTC**로 버킷해 KST 하루가 UTC
  두 날짜로 쪼개지는 것(KST 00~08시=UTC 전날, 09~23시=UTC 당일). offline은
  `TIMESTAMP(D, 'Asia/Seoul')` 앵커라 KST 정렬. → **DuckDB의 UTC 버킷은 문서
  (assembly docstring "Asia/Seoul 날짜 경계")와도 어긋나는 잠재 버그**이며, offline
  정본 채택이 이를 교정한다.
- **지점 3 (집계 정의)** — DuckDB는 wide-format(impression 1행 + 귀속된
  clicked/liked/watch)에서 세고, offline은 raw long의 `event_type`을 직접 센다.
  - `recent_click_count_7d`: DuckDB=30분 귀속에 생존한 clicked 임프레션 수 /
    offline=raw click 이벤트 수. 직전 임프레션 없는 click은 offline만 셈.
  - `recent_view_count_7d`: DuckDB=`watch_time_sec>0` 근사(임프레션당 최대 1) /
    offline=raw view 이벤트 수. **DuckDB는 click→view 인과 때문에 view_count가
    click_count와 사실상 동일하게 움직임**(실측: 두 컬럼 불일치 건수·diff가 완전
    동일) — wide-format이 view를 독립 카운트 못 하는 한계.
  - `total_event_count_7d` (**69.1%, 최다**): DuckDB=`impression+click+view+like`
    합성 가중 / offline=raw 전량 COUNTIF. 가장 큰 카운트라 정의차·tz차가 모두 최대
    증폭.
- **지점 4 (무활동 기본값)** — offline `users` CTE가 기존 피처 테이블 ∪ action_log로
  모든 기지 유저에 0-채움 스냅샷을 냄(2,821 user-day가 임프레션 없이도 존재). DuckDB는
  임프레션 있는 유저만 계산(duck-only 0 = offline이 상위집합). 같은 query point 위에선
  기본값(0/`unknown`)이 일치하므로 **값 diff가 아니라 엔티티 커버리지 차** — #358
  조회 시 offline이 더 넓은 엔티티에 답한다.
- **지점 6 (카테고리)** — affinity 10.1% 불일치인데 **video category_id는 0.0%(4건)만
  다름**. 즉 불일치는 카테고리 스냅샷 선택(as-of vs latest-in-window)이 아니라 **argmax
  반응 가중 규칙 차이**에서 옴: DuckDB=반응 임프레션 수(임프레션당 1) / offline=raw
  click/view/like 이벤트 수(임프레션당 최대 3). top 불일치가 전부 Gaming 축인 건 특정
  카테고리에서 가중 방식에 따라 argmax가 계통적으로 뒤집힘을 시사.

## 근본 원인 분해 (정의차 vs tz)

dump(`user_dynamic_mismatch.csv`, 불일치 1,259,366행)를 as_of의 UTC 날짜 ≠ KST
날짜로 갈라 분석:

- **tz-shifted(UTC≠KST) 행: 34.6%** / **aligned(UTC=KST) 행: 65.4%**.
- aligned 구간(쿼리포인트 창 밀림 **없음**)에도 컬럼별 불일치의 60~65%가 남고 diff도
  큼(total aligned diff_mean 8.2, click 0.9, watch 309). → **카운팅 기반 차이(정의차)가
  tz와 무관하게 지배적인 주 원인**이다.
- timezone은 실재하는 **부차 축**(불일치의 34.6% + 이벤트-레벨 UTC 버킷). 90% 하루내
  변동이 그 확정 증거.
- **결론**: offline 채택은 정의차 해소가 본질이고, KST 앵커링이 tz 버그를 덤으로 교정.

## video_feature (2차·근사)

`--include-video`, latest-per-video 근사(DuckDB=최신 trending_date, offline=11일 내
최신 collected_at):

- **category_id 4/47,923 (0.0%)** — 카테고리 스냅샷 선택 차는 실무상 무시 가능(위 지점6
  판정의 근거).
- **duration_sec 30/47,923 (0.1%)**, abs_max 143,906초(≈40h) — `P#D`(일 단위) 장시간
  영상. DuckDB 파서(`PT#H#M#S`만)가 일을 못 잡아 0, offline regex는 초 반영. 실제 diff이나
  극소수.
- **view_count 30.9% / like_ratio 98.6% / comment_ratio 94.2%** — **하네스 근사 노이즈가
  주원인**: 두 경로가 서로 다른 스냅샷을 집고(view_count는 시간에 따라 증가) + DuckDB가
  ratio를 `ROUND(...,4)`로 반올림, offline은 미반올림이라 거의 전건이 5번째 자리에서
  어긋남. → **프로덕션 train/serve skew로 단정 불가**. 스냅샷을 이벤트 시각으로 정렬한
  진짜 비교는 #358 몫.
  - 단 **ratio 4dp 반올림은 스냅샷과 무관한 진짜 정의 diff** — offline 정본이면
    `ROUND` 제거 방향(같은 스냅샷이어도 어긋나므로).

## 설계 결정 3종 (#358이 딛고 설 것)

Task 1 diff와 별개로 #358 조회 전환이 전제하는 3개. (A)(B)는 대안이 약해 이번에
**확정**하고, (C)는 서빙 영향이 있어 **확인 대기**로 분리한다.

### (A) 파생 2종 — ODFV로 확정 (O)

`preferred_category_match`, `historical_category_match`는 raw 피처가 아니라 (user 피처
vs 영상 category) 비교로 나오는 파생값(현재 `compute_interaction_columns`의 pandas
비교). **결정: Feast On-Demand FeatureView(ODFV)로 정의한다.**

- 근거: 학습·서빙이 **같은 변환을 공유**해 skew를 원천 차단 — `feature_builder.py`의
  `compute_preferred_category_match` / `compute_historical_category_match`를 ODFV 본체로
  재사용한다. 후처리(경로별 파이썬/SQL 복제)는 이번 diff가 드러낸 것과 같은 종류의
  drift를 재생산할 위험이라 기각.

### (B) cross-entity 조인 — staged 조회로 확정 (O)

`topic_similarity`는 (user, **영상의 category_id**) 키인데 category는 이벤트 시점 video
스냅샷에서 나오는 닭-달걀. **결정: staged 조회** — 1차 video PIT로 category 확정 → 2차
`user_category_similarity` 조인.

- 근거: cross-entity 키 구조상 category를 먼저 확정하지 않으면 조인 키가 없어 사실상
  유일한 방법. Feast `get_historical_features` 단일 호출로 될지 2단계로 나눌지의 **구현
  형태만** #358에서 실측(결정 자체는 staged로 고정).

### (C) ttl 정책 — 확정 (2026-07-27): 일 스냅샷 60h / 정적·video None

전 FeatureView `ttl` 부재 → 결손일 stale fallback(#356 실증, **원 지적: bbungjun**).
학습뿐 아니라 online 서빙 조회에도 직접 영향(결손 시 null vs stale)한다.

- **일 스냅샷 뷰 = `ttl=60h`** (UserDynamicView)
  - 당일 임프레션(≤24h) + 1일 결손(≤48h)까지 stale 허용, 2일+ 결손은 null
  - #365 결손이 잦아 1일마다 null 내면 학습이 과도하게 비므로, "무제한 stale"이 아니라
    **stale 상한 60h로 묶는** 절충
- **정적/준정적 뷰 = `ttl=None`** (UserStaticView, UserCategorySimilarityView)
  - 갱신 주기가 불규칙(persona 변경·임베딩 재계산 시만)이라 배치 주기 기반 ttl이 성립 안 함
  - 60h를 걸면 정상 상태인데도 매일 null(가짜 경보)이 됨
- **VideoFeatureView = `ttl=None`** (모델링 결정, 별도)
  - 일 스냅샷이지만, 트렌딩 이탈은 "피처 없음"이 아니라 **"인기 식음"이라는 신호**일 수 있어
    마지막 스냅샷을 유지한다
  - 트레이드오프: view_count 등이 마지막 트렌딩 시점 값으로 고정됨(days_since_upload도 그
    시점 값에서 안 자람). 그래도 null보다 마지막 known 상태가 유용하다고 판단 — 모델링
    재검토 여지는 열어둠

**제안 대비 변경(silent rewrite 아님)**: 초안은 "결손 시 null화"였으나, #365 결손 빈도를
고려해 "1일 결손은 stale 허용(60h 상한), 2일+는 null"로 완화. null-on-any-gap이 아니라
bounded-stale이다.

**남은 의존(서빙 도메인)**: 2일+ 결손 시 online 조회가 실제 null을 반환하므로, 서빙의 null
피처 처리 여부는 별도 확인이 필요하다. 이 의존은 #357 코멘트에 기록했다.

**학습 단계까지 관철(#358)**: (C)의 "결손을 null로 드러낸다"를 학습 파이프라인에서도 지킨다 —
feast 조회 결과에서 **UserDynamic 피처가 전부 null인 행(ttl 초과/결손)은 채우지 않고 드롭**한다
(`feast_retrieval.drop_user_dynamic_gap_rows`). 영상 미발견 cold-start(0/unknown 채움)와 구분:
후자는 "정보가 원래 없음"이지만 전자는 **활동 유저**(라벨 clicked이 그 유저 것)의 기록 유실이라,
0으로 채우면 "신규 유저" 거짓을 학습에 주입(=stale이 몰래 섞이던 것과 같은 피해). 드롭 건수는
학습 로그에 별도로 남겨 #365 gap을 감지한다.

## 남은 작업

- 설계 결정 3종 확정 → #358 착수·완료(feast 경로 실환경 검증 완료, PR 아래).
- **Task 5 (ROC-AUC 영향 정량화) — 후속으로 미룸**: feast/duckdb 데이터셋 학습 비교.
  n=11 **참고용**이라 착수 여부가 결론(offline 정본 채택)에 영향 없고, duckdb 실데이터
  셋업(videos·personas 실경로)이 번거로워 비용 대비 얻는 게 적음. feast 경로 정확성은
  #358에서 실환경 검증됨(21피처·cold-start·ttl·이름충돌 전부 실측 통과). 필요 시 별도
  세션에서 참고 수치로 뽑는다.

## 비범위

- 실제 조회 경로 교체(`--assembly-source feast`), FeatureService 정의 = #358.
- 폐루프 데이터 신뢰성 = #365.
