# 다양한 합성 행동 이력 데이터 계약

관련: #107, #105. `diverse-behavior-v1`은 학습 전 데이터 품질 검증용 생성기다.
기존 `youtube-ctr-local-fixture-v1/v2`의 descriptor/bytes를 변경하지 않는다.

## 시간·입력 계약

`BehaviorDataRequest`는 음이 아닌 정수 seed와 학습일을 받는다. 학습일 기본값은
2026-09-02다. 학습일 D에 대해 D-30~D+1의 32개 날짜를 오름차순 생성한다.
D-30~D-1은 피처를 위한 선행 이력, D는 학습 노출, D+1은 귀속 확인일이다.
평가일 D+2와 이후 action log는 생성하지 않는다. 학습 label/평가 snapshot은 후속
실험에서 새 계약으로 조립해야 하며 이 데이터에 final 봉인이 됐다고 주장하지 않는다.

사용자는 기존 hash bucket 선택의 validation 160명과 final용 예약 40명이다.
이번 데이터 품질 감사는 모든 사용자에서 수행하므로 **새 최종 평가를 위한 미관측
사용자라고 주장할 수 없다**. 후속 최종 실험은 새 seed/평가일과 사전 등록 조건을
사용하고, 이 데이터는 개발/학습 이력으로 취급한다.

raw 사용자·영상 열은 fixture input v1 schema를 재사용한다. 사용자 관측 시각은
이력 시작일 KST 00:00, 일별 영상 관측 시각은 해당일 KST 00:00이다.
잠재 관심 전환일과 전환 후 선호는 `manifest.json`의 생성 감사용 `latent_profiles`에만
들어가며 사용자 metadata에는 최초 선호만 제공한다. 이 manifest는 candidate 입력으로
그대로 전달하지 않는다.

## 행동 규칙과 재현성

SHA256에 버전·seed·사용자·날짜·목적을 넣어 호출 순서와 독립적인 값을 만든다.
사용자별 접속 확률은 0.25/0.5/0.8 중 하나, 클릭 확률은 [0.25,0.85)다.
접속한 사용자는 8/16/24개 영상을 노출받으며 후보 선택은 선호와 독립적이다.
절반 정도는 시작 후 14~21일에 선호 카테고리가 바뀐다.

클릭 의향이 있는 날만 propensity가 0.5를 넘는다. 후보 내 utility는 선호 일치 0.6과
독립 noise 최대 0.4로 구성한다. 따라서 선호 카테고리 후보가 존재하면 그 안에서
클릭이 선택되는 강한 합성 규칙이다. 시청 비율은 선호와 독립 noise로 변하고 like는
production `derive_would_like`로 계산한다. 값은 실제 CTR을 추정한 확률이 아니다.

정렬한 사용자·후보의 `ImpressionDraft`를 production `expand_action_log_drafts`로
확장해 최대 1 click/slate, click→view 결합을 유지한다. 일일 capacity는 24다.
event ID prefix는 `db1s{seed}`이며 날짜와 seq를 유지해 다른 world도 구분한다.
`generated_at`은 KST 다음날 00:00의 고정 완료 시각이다. production Parquet schema와
writer를 사용하며 같은 소스·PyArrow runtime의 재생성은 파일 bytes까지 같아야 한다.

## 게시·감사

`generate_behavior_data`는 존재하지 않는 출력 root만 받는다. 중간 실패 시 부분 파일을
보존하고 완료 manifest를 만들지 않는다. 완료 후에는 일별 파일 경로·행 수·SHA256,
seed/날짜/버전·PyArrow 버전·생성 코드 및 직접 재사용 모듈 source hash를 기록한다.
이 hash는 재현성 증거이며 공격자에 대한 인증/서명이 아니다.

`audit_behavior_data`는 manifest와 실제 파일의 날짜 순서·schema·hash·행 수를 검사한
뒤 KST 파티션, event ID, 사용자/영상 membership, slate 크기, 엄격히 증가하는
impression→click→view→like 시각을 확인한다. `local_features`로 전체 사용자 및
학습일 실제 노출의 최근 5개 피처를 계산하고 각각 unique>=2/std>0을 요구한다.
학습일·이후 행동을 제거해도 전체 피처가 같아야 한다. coverage는 실제 32개 파일을
검사한 후에만 확인한다. 상수 임베딩은 행동 피처 감사용이며 학습 산출물이 아니다.

관심 변화는 전환 전/후 클릭이 모두 있는 사용자에서 새 선호 카테고리 비율 변화를
계산한다. 변화 사용자 평균이 양수여야 하며 비전환 사용자의 동일한 가상 경계도
대조 기록한다. 이 검사는 합성 규칙 작동 확인이며 모델 성능 검증이 아니다.

도구 `python -m tools.generate_behavior_data --output <새 경로>`는 #107의 고정 seed
10701/10702/10703을 순차 실행하고 각 world의 manifest/audit와 전체 summary를 남긴다.
감사 기준 미달 시 결과를 보존한 채 실패 종료한다. 모델 학습·LLM/GPU·final 호출은 없다.
