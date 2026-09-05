# #111 고정 bundle 학습 실행기와 평가 봉인

상태: 진행 중. 기준: #109의 사전 등록 spec와 복사본 SHA256 a1490bca5ebbe8114f6a3619dca6f3684b9eac4cecbcb18eb95af6abd0f624aa.

## 목표와 경계

Serving server는 저장 모델의 온라인 추론을 맡는다. 이번 작업은 오프라인 학습 입력 소비·모델 재사용 코드와 Judge 소유 평가 원본/snapshot을 준비한다. 실제 18개 모델 실험, 지표 채점, final claim, 배포는 실행하지 않는다. 단위 테스트의 작은 합성 모델 fit으로 연결을 확인한다.

## 설계

- 새 behavior execution 모듈은 호출자가 고정한 bundle hash를 요구하고 기존 loader로 검증한다. 등록 seed/arm/기본 모델 설정만 허용한다. 공유 split의 train만 fit하며 internal validation은 categorical vocabulary 조립에만 쓴다. 모델 text와 receipt를 저장하고 재로딩 예측은 fit을 호출하지 않는다.
- 새 evaluation 생성 모듈은 기존 diverse-behavior-v1 daily_drafts/production 확장과 writer를 재사용한다. 8/3 anchor, 9/4 평가, 9/5 scan tail, bucket별 800/200명과 cohort 10901/10902/10903을 유지한다. 신규 평가 event ID는 production evt_ 계약을 유지하고 snapshot별 source identity로 world를 구분한다. #107 개발 데이터는 재작성하지 않는다.
- 신규 raw 생성 전에 깨끗한 commit, 비교 문서 hash, 생성기 및 의존 모듈 hash를 preflight.json으로 고정한다. 새 출력 경로만 허용한다. 생성된 파일은 manifest로 pin하고 기존 Stage B builder/publisher로 snapshot을 봉인한다. 원본/잠재 속성은 Judge root에만 둔다.
- Candidate는 기존 materialize_candidate_data_view_v2로 validation slate, 평가 이전 이력, 허용된 as-of metadata만 받는다. 새 pinned source용 metadata 준비만 추가하고 기존 fixture 보안 검증은 완화하지 않는다. Final view는 기존 실제 grant가 있어야 게시하며 이번에는 게시하지 않는다.
- 원본 및 snapshot 재로딩 검증, 기존 학습 사용자 및 평가 identity와 중복 거절, 모든 fit/score/final 실험 호출 0을 receipt에 기록한다. 품질 부족은 seed 교체 없이 기록한다.

## 순서와 완료 조건

1. [ ] hash/분할/fit-only-train/저장 모델 재사용의 회귀 및 실행기 구현.
2. [ ] 날짜·사용자 bucket·잠재 anchor·metadata/final 경계 회귀와 생성/봉인 구현.
3. [ ] 독립 리뷰, 좁은 회귀 검증 후 깨끗한 commit을 고정.
4. [ ] 신규 3개 cohort 실물 생성/봉인 및 receipt 재검증. 학습/채점/final 0 유지.
5. [ ] 문제·선택 이유·실측·한계 보고서, 독립 실물/문서 리뷰, CI·squash 머지.

## 남은 실험 단계

향후 실행은 고정된 18개 모델을 fit하고 저장한 뒤 validation을 먼저 검사하고 cohort별 final 한 번만 소비한다. 새 데이터 생성과 준비 시간은 이 작업에서 별도 계측하며 실제 학습/채점 실행의 30분 제한을 대체하지 않는다. 이번 실행 이후 최종 비교 성능을 주장하지 않는다.
