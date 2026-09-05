# 선행 이력을 갖는 opt-in fixture 및 학습 날짜 선택 계약

관련: #105, #103. 기존 Stage C fixture 및 candidate view 계약을 확장한다.

## Fixture 시간 범위

`LocalEvaluationFixtureRequest.history_days`는 정확한 정수 `2`(기본) 또는 `32`만 허용한다.
평가일을 E라 하면 일일 producer는 E-history_days부터 E+1까지 날짜 오름차순으로 실행한다.
32일 이력은 30일 warm-up, E-2 학습, E-1 label scan tail을 구성한다.

기본값은 `youtube-ctr-local-fixture-v1`, 32는 `youtube-ctr-local-fixture-v2` descriptor다.
`FixtureDescriptor.history_days`는 버전에서 계산하는 property이며 직렬화 필드가 아니다.
v1의 기존 descriptor bytes 및 생성 규칙을 보존한다. v2의 이력 시작일·partition 순서와
끝 날짜는 validator에서 고정한다. 사용자 metadata 생성일도 이력 시작일로 이동한다.
입력 row schema 및 generator 버전 `youtube-ctr-input-v1`은 유지한다.

Candidate view의 이력은 E-1까지만 공개되고 완전 귀속 학습 label 상한은 E-2다.
평가일 E와 scan tail E+1의 label은 기존 Sealed Judge 경계를 따른다.

## 학습 기간 선택

`select_training_window(inputs, start, end)`는 검증된 `LocalTrainingInput`을 받는다.
요청 날짜는 KST 기준이며 start 이전 연속 30일을 요구하고,
end는 `complete_history_label_end_date`를 초과할 수 없다. 비어 있는 선택도 거절한다.

선택 결과의 `inputs.training_rows`는 해당 날짜 구간의 impression만 포함한다.
`inputs.history`는 바꾸지 않아 warm-up이 피처 계산에 쓰인다. receipt에는 선택 날짜,
원본 manifest hash, 선택·제외 행 수와 선택 event ID 목록의 hash를 보존한다.
실행기는 이 receipt를 학습 receipt에 함께 저장해야 한다. 원본 candidate manifest만으로
날짜 선택까지 재현됐다고 주장하지 않는다.

## 피처 cutoff와 평가

기존 local_features는 요청 날짜 D의 D-7~D-1 및 D-30~D-1만 집계한다.
당일·미래 행동을 추가/제거해도 학습 피처가 같아야 한다. 학습 label 귀속 확인에
사용하는 scan tail을 feature 계산 cutoff로 혼동하지 않는다.

최근 피처가 비영·비상수인지와 실제 model importance는 별도로 측정한다.
기록이 충분하다고 예측력이 있다는 결론을 내리지 않는다. 실험별 seed·채택 기준과
final 소비 정책은 각 사전 등록 계획을 따른다.
