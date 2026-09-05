# #109 학습 입력 조립과 비교 조건 고정

## 범위와 종료 조건

#107 원본 3개 world는 개발/학습용이다. 이번 Goal은 9/2 학습 입력의 실제 정답·E5 피처·
분할 receipt 조립, 15/10피처 비교 사전 등록, 회귀/실물 검증/독립 리뷰/문서/CI 반영까지다.
모델 fit, 예측, ranking 평가와 final 소비는 실행하지 않는다.

## 결정

- 평가 slate가 없는 개발 데이터를 기존 CandidateDataManifest로 가장하지 않는다.
  별도 학습 bundle을 만들며 원본 `db1s{seed}` ID와 source hash를 보존한다.
- 기존 `attribute_clicks`의 30분 귀속으로 KST 9/2 impression에만 정답을 붙인다.
  9/3은 귀속 확인에 사용하고 8/3~9/1 warm-up은 fit label에서 제외한다.
- Metadata와 raw 이력은 기존 schema/날짜/receipt 감사 후 조립한다. 실제 학습 피처에는
  9/1까지의 이력만 쓴다. 같은 행 순서의 15/10 projection과 label을 저장한다.
- E5 small revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`, CUDA/기존 검증 cache,
  query/passage prefix와 max_seq_length=512, batch_size=8을 유지한다.
  감사용 상수 embedding을 실제 산출물에 사용하지 않는다.
- 학습 seed 401/402/403, sklearn 기존 stratified 60/20/20 방식으로 source event ID
  목록을 고정한다. 두 arm은 동일 split 파일을 공유한다. Internal validation/test는
  새 사용자 holdout 또는 final이 아니며, 모델 fit은 train 60%만 사용한다.
- 신규 디렉터리만 게시하고 bundle manifest를 마지막에 쓴다. 로더는 파일 hash·행 정렬·
  열 projection·분할 배타성/완전성·두 클래스 존재를 검증한다. 원본 파일은 수정하지 않는다.

## 작업 순서

1. 별도 비교 spec에 미래 검증용 seed/날짜·판정·소비 조건 고정, 독립 계획 리뷰.
2. 입력 조립/재로딩/분할 구현과 귀속·변조·arm 정렬 회귀.
3. 고정 10701/10702/10703 원본에서 E5 피처와 3 seed별 split 생성, receipt/수치 검증.
4. 독립 구현·실물 리뷰, 포트폴리오 문서, PR CI/머지 후 Goal 완료.

## 검증

30분 경계·다음날 귀속·warm-up 제외, 당일/미래 이력 무영향, label/feature ID 일치,
10피처가 15피처의 정확한 projection인지, 동일 split 재계산과 overlap/변조 거절을 확인한다.
실제 최근5개가 fit subset에서도 각각 비상수인지 검사하고 실패는 그대로 보존한다.
개발 데이터에서의 품질 확인을 성능 향상으로 해석하지 않는다.
