# #111 고정 학습 실행기와 신규 평가 봉인

## 문제

#109는 4,608행의 15/10피처 입력과 분할을 준비했지만 기존 train_local_candidate는 평가 candidate view에서 자체 학습 데이터를 재조립했다. 이 경로를 그대로 쓰면 고정된 bundle을 소비한다는 증거가 없고 validation/final에서 반복 fit할 위험이 있다. 기존 fixture는 모든 사용자가 24개 노출과 클릭을 갖는 조건이어서 비접속·무클릭·8/16/24 노출을 갖는 이번 평가를 표현하지 못했다.

## 해결과 선택 근거

학습과 예측을 별도 함수로 분리했다. train_behavior_model은 bundle.json의 호출자 고정 hash와 기존 loader를 거쳐 등록 seed/arm 및 LocalTrainingConfig 기본값만 사용한다. train 행만 fit하고 validation은 categorical vocabulary에만 사용한다. 내부 test 행은 fit/vocabulary에서 제외한다. 새로운 모델 디렉터리에 attempt.json을 먼저 쓰고 native model.txt와 완료 receipt.json을 남긴다. 실패한 디렉터리도 재사용하지 않는다.

predict_behavior_model은 receipt와 native 모델 hash, 정확한 피처 순서/dtype, E5 identity를 검증하고 저장 모델만 재로딩한다. 새 fit을 하지 않는다. 모델 출력은 온라인 serving 배포 패키지가 아니며 serving server를 변경하지 않았다.

새 평가 생성은 기존 daily_drafts, production event 확장과 writer를 재사용한다. 기존 개발 데이터를 바꾸거나 event ID validator를 완화하지 않고, 신규 평가 원본에는 production evt_ 형식을 그대로 쓴다. 서로 다른 cohort는 원본 manifest hash를 포함한 source identity로 구분한다. 기존 ID 후보식/hash bucket에서 목표 사용자 수만 800/200으로 바꾼다.

정책 파일에는 생성 요청, 날짜, 비교 문서 hash, 코드 hash와 PyArrow 버전을 먼저 저장한다. 정책에 없는 요청이나 현재 코드와 다른 정책은 생성 전에 거절한다. 깨끗한 checkout의 commit과 도구 hash도 preflight에 남긴다. Judge source는 호출자가 고정한 raw manifest와 매 파일 hash를 검사하며 Stage B snapshot builder/publisher가 기존 시간·귀속·split·봉인 계약을 수행한다.

Metadata는 허용 열만 normalize하고 as-of 범위로 선택한다. 기존 candidate v2 publisher가 동일 snapshot과 receipt를 재검증해 validation slate, 9/3까지 이력, 안전한 metadata만 게시한다. 잠재 profile·당일/미래 raw·평가 정답은 candidate에 전달하지 않는다. Final candidate 게시에는 기존 실제 consumption grant가 필요하다.

## 검증

실행기와 봉인 및 기존 bundle 회귀 22 passed(21.02초), Ruff 통과. 작은 합성 입력으로 양 arm의 실제 LightGBM 저장·재로딩 예측 일치, train-only fit, test vocabulary 제외, hash/설정/피처/seed 오류 시 fit 전 거절을 검증했다. 평가 회귀는 날짜 anchor·기존 행동 규칙 일치, 기존 hash bucket의 prefix 보존, 비접속 사용자 미보충, label 없는 candidate view와 final grant 요구를 확인했다.

정책과 요청 결속 보완 후 평가 회귀 4 passed(4.12초). 위 검증은 중복이 있으므로 테스트 수를 합산하지 않는다. 독립 리뷰에서 P1/P2 발견 사항은 없었다. 실제 학습 bundle은 읽기 검증만 했으며 모델 fit을 하지 않았다.

## 실물 실행 상태

깨끗한 commit `c94a0d9cc43d4abdd9ef5eb644e96fbb32d5b751`에서 생성·봉인·validation view 게시를 **81.502초**에 완료했다. 신규 cohort 3개 각각 1,000명(등록 validation 800명/final 200명), 34일로 총 raw event **915,501건**을 만들었다. 실제 접속한 validation 사용자의 노출은 총 **19,600행**이다. 등록 사용자 수와 평가일 접속 사용자 수는 다르며, 미접속 사용자를 위해 노출을 보충하지 않았다.

| cohort | 대응 학습 world | raw event | validation slate 행 | candidate history 일수 |
| --- | --- | ---: | ---: | ---: |
| 10901 | 10701 | 302,392 | 6,896 | 32 |
| 10902 | 10702 | 305,733 | 6,280 | 32 |
| 10903 | 10703 | 307,376 | 6,424 | 32 |

생성 범위는 8/3~9/5, 평가일 9/4, candidate 관측 이력은 8/3~9/3이다. Evaluation metadata는 평가 요청 시점의 as-of 허용 행을 포함한다. Final slate/labels는 Judge snapshot에 보관하며 candidate-final view와 consumption marker는 만들지 않았다. **실제 모델 fit 0, 평가/채점 0, final claim 0**이다. 테스트의 소형 LightGBM fit과 구분한다.

생성 정책 SHA256은 `1c600173494e1db1754465bc0aa2e8c4a8778dd2a8a62bd6493afbc31048af19`, summary SHA256은 `459c938eaf3a0d00b56fe46327a023745acddaae06e6afb904358f103b2c7d89`다. #109에서 복사한 비교 문서의 hash는 `a1490bca5ebbe8114f6a3619dca6f3684b9eac4cecbcb18eb95af6abd0f624aa`로 유지했다.

| cohort | raw manifest SHA256 | snapshot fingerprint |
| --- | --- | --- |
| 10901 | `18c278d2b96207ead001304e71a6d623ff6ff5da5ac547a52596926418ce5b75` | `eb31c567185ddf8b6cdee29c56d0f1a3fb1d550399d4c152bf41b30eadf0eadd` |
| 10902 | `eeb49a0bf1c9c029ba6751d60015382059c81b0e7d6e1a400bc3e129436a49c1` | `8982799854e1424d9e8e6f4e8027b6cf4e9814793f58adbdad2c150ea06b8e35` |
| 10903 | `c95413a8db237cf88852a7a14e83600817280dda1a99f41cedebde221953bcb7` | `f0d369e288da5430a0620d789e8cb8475c0bc40e1cdf819e0faac8af90c9fe25` |

실행 도구는 `python -m tools.prepare_behavior_evaluation --output <새 출력> --training <고정 bundle 묶음> --previous-result <기존 결과>`이며 `--previous-result`를 #16/#103/#105 결과에 각각 지정한다. 생성 데이터·모델·로컬 전용 경로는 커밋하지 않는다. 실행 중 실패한 산출물이나 기존 final marker를 초기화하지 않는다.

독립 실물 검증에서 3개 cohort의 모든 원본/snapshot/candidate 파일 hash, 정책/코드 hash, 날짜/bucket 및 신규 사용자 비중복을 대조했다. 평가일 접속 여부와 사용자별 8/16/24노출을 독립 재계산해 일치를 확인했다. Candidate의 과거 이력·metadata as-of 경계, final marker 및 실제 모델 산출물 부재, 기존 #109 bundle 및 #16/#103/#105 결과 hash 유지도 확인했다. Final 정답은 파일 무결성만 검사했고 모델 채점에는 사용하지 않았다. 최종 보고서·포트폴리오 수치까지 독립 검토했으며 P1/P2 발견 사항은 없었다.

## 한계와 후속

이번 범위는 실행기 API와 평가 입력 준비다. 실제 18모델 fit·36회 평가는 후속 오케스트레이션에서 고정 bundle·model receipt·snapshot을 연결해야 한다. 평가 피처는 실제 E5 identity를 맞추어 조립하며, validation 유효성 검사 뒤 기존 registry로 cohort별 final을 한 번만 소비해야 한다. 이 단계의 준비 시간은 모델 실험 시간을 대신하지 않으며 합성 데이터 대표성이나 성능 개선을 입증하지 않는다.
