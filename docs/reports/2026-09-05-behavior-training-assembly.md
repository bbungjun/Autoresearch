# #109 행동 데이터 학습 입력 조립과 ablation 조건 고정

## 문제

#107에서 다양한 행동 이력을 만들었지만 실제 학습에 바로 넣을 피처 행렬·정답·분할은
없었다. 기존 `LocalTrainingInput` 로더는 봉인 평가의 candidate manifest와 평가 slate,
`evt_` ID를 요구한다. 평가 전 개발 이력에는 이 계약이 없고 world 고유 `db1s{seed}`
ID가 있으므로 평가 ID나 slate를 만들어 끼우면 데이터 역할과 계보가 불명확해진다.

이번 작업은 학습 입력 준비와 실험 조건 고정까지만 승인됐다. 모델 fit/예측/평가/final
소비 없이 원본을 보존하며 다음 실행기가 읽을 수 있는 검증된 산출물을 만드는 것이 목표다.

## 해결과 선택 근거

기존 평가 로더를 완화하는 대신 별도 `behavior-training-bundle-v1`을 추가했다.
원본 manifest hash를 고정하고 품질 감사 및 파일의 안전한 재읽기/hash 확인 뒤
기존 `attribute_clicks`로 9/2 impression의 30분 클릭 정답을 붙인다. 9/3 scan tail을
귀속 확인에 사용하되, 피처는 8/3~9/1 과거 이력으로만 계산한다.

실제 E5 adapter로 조립한 15/10열 행렬과 정답에 같은 source ID 순서를 보존한다.
3개 학습 seed의 stratified 60/20/20 분할을 한 파일에 저장해 두 arm이 공유한다.
분할별 행 수/양성 수/source ID 순서 hash/자동 class weight를 receipt로 남긴다.
행렬 재로딩 시 같은 분할을 재계산하고 10열이 15열의 정확한 projection인지 확인한다.

동일 sklearn 분할 방식을 유지해 이번 작업에 새로운 split 방식의 효과를 섞지 않았다.
내부 validation/test에는 같은 사용자의 다른 노출이 있을 수 있으므로 새로운 사용자에
대한 최종 성능 검증으로 취급하지 않는다. 향후 평가는 별도 신규 사용자 cohort에서 한다.

사전 등록은 `c1e5083`, 실행 구현은 `39e7368401be39fd4ef2b1a618e060d57787b067`이다.
준비 도구는 깨끗한 checkout에서 실행하고, 당시 비교 문서 bytes를 산출물에 복사해
SHA256으로 고정했다. 모델 설정/seed/판정은 결과를 본 뒤 바꾸지 않는다.

## 조립 결과

**3개 원본에서 4,608개 학습 노출과 클릭 정답 163개를 조립하고 재로딩 검증했다.**
GPU adapter 초기화·원본 검사·피처 조립·게시·재로딩까지 47.020초가 걸렸다.
각 행렬은 source_event_id 외에 15개 또는 10개의 모델 입력 열을 갖는다.

| 원본 seed | 학습 노출 | 클릭 정답 | fit에서 제외한 warm-up 노출 | train / validation / test 행 수 |
| --- | ---: | ---: | ---: | --- |
| 10701 | 1,632 | 53 | 48,944 | 978 / 327 / 327 |
| 10702 | 1,528 | 58 | 46,336 | 916 / 306 / 306 |
| 10703 | 1,448 | 52 | 50,280 | 868 / 290 / 290 |

학습 seed는 401/402/403이다. 행 수는 같아도 각 seed의 source ID 배정은 다르며,
각 seed 안에서는 두 arm이 같은 분할을 쓴다. 모든 subset에 두 클래스가 있고,
9개 fit subset 모두 최근 5개 피처가 각각 비상수다. 미래 행동까지 포함해 계산한 결과와
과거 이력만 사용한 결과의 전체 피처가 같음을 검증했다.

과거 피처에 사용한 event 수는 53,199 / 50,336 / 54,900행이며, label 확인을 위해
읽은 전체 event는 56,714 / 53,569 / 58,265행이다. Warm-up의 정답은 fit에 넣지 않는다.

### 실제 E5와 실행 범위

- 모델: multilingual-e5-small, revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`.
- CUDA adapter, 출력 384차원, batch 8, max_seq_length 512, 기존 query/passage prefix.
- 실행 identity: `99df5a4c76f3522375a6166db96bf542c153fc3aaf1fbab654b36a9e2cd866cd`.
- 검증된 E5 cache hit 54, miss 0, 새 encode inference 0회. 실제 E5 캐시 벡터를 사용했으며
  테스트 전용 상수 embedding을 실물에 사용하지 않았다. 새 GPU 추론 성능을 측정한 것은 아니다.
- 모델 fit 0, 예측/평가 0, final claim 0. 새 모델 성능에 대한 결론은 없다.

## 고정한 후속 비교

정본은 [사전 등록 spec](../specs/2026-09-05-diverse-behavior-ablation.md)이다.
이번 산출물의 복사본 hash는
`a1490bca5ebbe8114f6a3619dca6f3684b9eac4cecbcb18eb95af6abd0f624aa`다.

- 15피처(with_recent) 대 10피처(without_recent), 학습 seed 401/402/403.
  기존 LightGBM 설정과 sampling_rate=1.0을 유지한다.
- 학습 world 10701/10702/10703은 각각 신규 평가 cohort 10901/10902/10903에 대응한다.
  각 평가 cohort는 validation 800명/final 200명이며 기존 hash bucket 규칙을 유지한다.
- 평가일 9/4, label scan tail 9/5, 잠재 상태 anchor 8/3. 평가 당일에도 실제 접속한
  사용자의 8/16/24개 slate만 사용한다. 신규 평가 데이터는 아직 생성하지 않았다.
- 18개 모델을 한 번씩 fit해 validation/final 양쪽에 사용한다. 총 36개 평가 관측이다.
  Primary NDCG@10과 기존 guardrail·최소 유효 group 30개/20% 기준을 유지한다.
- 유효한 validation 후 고정 모델 묶음으로 final을 cohort별 한 번, 최대 3회 소비한다.
  실패/표본 부족을 seed 교체나 기준 완화로 복구하지 않는다. 30분 hard stop은 실측 보장이 아니다.

계획 리뷰 중 초안의 최소 positive slate 5개가 기존 최소 30개와 맞지 않음을 확인해
실행 전에 수정했다. 비활성 사용자와 무클릭 slate가 있는 새 환경에서 기준을 낮추는 대신
평가용 cohort를 늘렸다. 이번에는 이 조건만 고정했으며 평가 coverage 통과를 주장하지 않는다.

## 검증과 산출물

신규 회귀 9 passed, ID/분할 receipt 보완 후 기존 click 귀속 회귀를 포함해 19 passed였다.
두 실행은 중복이 있으므로 합산하지 않는다. 같은 날/다음날 30분 경계, warm-up 제외,
source hash와 output 겹침 거절, 15→10 projection, 기존 sklearn split의 동등성,
파일 변조·행 순서 교란·split 중복·NaN·잘못된 label 날짜 거절을 확인했다.
변경 Python Ruff와 `git diff --check`를 통과했고 독립 코드/계약 리뷰에 P1/P2가 없었다.
독립 실물 리뷰에서도 raw 노출·클릭에서 4,608개 label을 별도 계산해 전행 일치를 확인했다.
원본 직전 7일의 최근 피처, 9개 분할 및 통계, E5 모델 파일/identity와 모든 receipt를
대조했으며 추가 P1/P2는 없었다. 전체 summary SHA256은
`14ddb68fcc3b0ef59b0d5d6c6e6d71001107fa61fe46199d35e1f74123200140`이다.

Bundle에는 `labels.parquet`, `with_recent.parquet`, `without_recent.parquet`,
`splits.json`, `bundle.json`이 있다. 원본 잠재 프로필/미래 선호는 전달하지 않는다.
전체 `summary.json`과 `comparison-contract.md`는 준비 실행과 조건을 고정한다.
생성 데이터·모델 파일과 로컬 경로는 커밋하지 않는다.

| 원본 seed | bundle manifest SHA256 |
| --- | --- |
| 10701 | `a4bf85660b7f9aa7992f9f1019b9829104435e1953ccd17c1e0399e5be444eec` |
| 10702 | `aa3e610f2c52187ac2f66adcbc981d5365e5d6e89c6362790df3259aff843e7f` |
| 10703 | `fdf1b35245c2ec8fdc4cf2afa21f8d0825ed8b63c3094b2fb7f737fe2e52ef94` |

준비 명령은 `python -m tools.prepare_behavior_training`이며 `--source`, `--output`,
`--model-dir`, `--cache-dir`를 전달한다. 기존 출력과 원본은 덮어쓰지 않는다.

## 남은 한계와 다음 작업

입력 준비와 비교 조건 고정은 완료됐지만 기존 `train_local_candidate`가 이 bundle을
자동으로 읽는 것은 아니다. 후속 executor가 bundle을 명시적으로 소비하도록 연결하고,
고정한 신규 cohort의 9/5까지 생성 확장·snapshot 봉인·18회 fit·36개 채점을 구현해야 한다.
이 작업에서 해당 실행을 완료했다고 주장하지 않는다.
합성 규칙의 강한 카테고리 선호, click/view 결합과 실제 사용자 대표성 미입증 한계도 유지된다.
