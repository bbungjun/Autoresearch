# Judge v2 고정 후보 재검증 — validation·final promote

> 2026-10-21 | #150 | 선행 #149 병합 완료 | synthetic offline, Production 변경 없음

기존 #147의 동일 `interest_peak_gap` 후보를 새 profile과 Judge v2로 재학습했습니다. validation과 처음 소비한 final 모두 `promote / promotion_threshold_met`입니다. 과거 v1 판정은 수정하지 않았습니다.

## 실험 조건과 귀속

- [사전 등록](../specs/2026-10-21-judge-v2-interest-peak-gap-revalidation.md): 원격 commit `34fad94`, 실제 fit 이전에 profile·driver SHA를 #150에 게시했습니다.
- Judge: #149 병합 `a05f3b61ee32fc45a29e9693213625e8974d8ab8`, 정책은 명시적 v2입니다.
- 기준 학습 코드 `f06c9ab75461a04df4ccef65bc9ec7bcd90313fa`, 후보 `db1cb70920cb3ff29c1b063d5d4e4ed733f1a46e`를 각각 그대로 사용했습니다.
- 후보는 #147 Agent가 만든 기존 22개 안전 입력의 결정적 조합입니다. 이번에는 새 가설 생성·코드 수정 없이 고정 후보를 재검증했으며, coding Agent·advisory LLM 신규 호출은 0회입니다.
- 고정 E5 임베딩과 binary LightGBM, 기존 데이터·설정·seed를 유지했고 calibration은 SHA로 결속해 재사용했습니다. 모델 파일·라이브러리·metadata를 실행 직전에 재대조했습니다.
- validation은 160 slate / 3,840행, final은 별도 40 slate / 960행입니다. 각 단계에서 같은 평가 표본을 seed 101~105의 기준·후보 5쌍으로 반복했습니다. 5배의 독립 slate 표본으로 계산하지 않습니다.

## 판정 결과

| 단계 | NDCG@10 기준 평균 | 후보 평균 | 방향 정규화 Δ | primary 문턱 2σ | 판정 |
|---|---:|---:|---:|---:|---|
| validation (5쌍) | 0.874144439 | 0.938146816 | +0.064002377 | 0.043001530 | promote |
| final (5쌍) | 0.839334270 | 0.899183419 | +0.059849149 | 0.043001530 | promote |

두 단계 모두 NDCG@10이 5/5 seed에서 개선됐습니다. validation 수치는 #147의 기존 관측값을 재현했으며, 새로운 독립 근거는 아래 final 결과로 구분합니다.

## Final의 전체 지표

| 지표 | 기준 평균 | 후보 평균 | 방향 정규화 Δ | 통과 문턱 | 개선 seed |
|---|---:|---:|---:|---:|---|
| ndcg_at_10 | 0.839334270 | 0.899183419 | +0.059849149 | ≥ 0.043001530 | 5/5 |
| ndcg_at_24 | 0.839334270 | 0.899183419 | +0.059849149 | ≥ -0.021500765 | 5/5 |
| recall_at_10 | 1.000000000 | 1.000000000 | +0.000000000 | ≥ 0.000000000 | 동률 5/5 |
| grouped_roc_auc | 0.917934783 | 0.929021739 | +0.011086957 | ≥ -0.004189544 | 5/5 |
| pr_auc | 0.659070176 | 0.661145835 | +0.002075658 | ≥ -0.000820625 | 5/5 |
| log_loss | 0.084734766 | 0.068391787 | +0.016342979 | ≥ -0.003166975 | 5/5 |
| brier | 0.015496912 | 0.015100835 | +0.000396077 | ≥ -0.000037748 | 5/5 |

LogLoss·Brier의 방향 정규화 Δ는 기준−후보이며, 양수는 오차 감소입니다. Recall@10은 σ=0·calibration 상한 1.0이므로 v2에서 허용 폭 0으로 검사했고 final에서도 1.0을 유지했습니다.

## 실행·무결성 검증

- screening seed 42의 1쌍이 primary Δ>0을 충족했습니다. 이어 confirmation 5쌍이 정확한 candidate·v2 promote를 기록한 뒤에만 원래 Controller의 final 경로를 호출했습니다.
- 실제 학습 프로세스 22회 = screening 2 + confirmation 10 + final 10. 학습 sidecar도 22개이며, seed·코드 SHA·설정·예측 및 모델 SHA를 원장의 artifact SHA와 대조했습니다.
- 11개 paired 학습 모두 입력 manifest·train/validation/test 분할·sampling·모델 설정·임베딩·이력 기간·seed가 일치했습니다. 후보에 추가된 입력 열은 `interest_peak_gap` 하나이며 기준 22열·후보 23열입니다.
- 기존 #147·calibration·fixture의 568개 파일 bytes/SHA가 실행 전후 동일했습니다. 원본 final registry를 재사용하고 marker를 1회 게시했으며, 복제·초기화하지 않았습니다.
- 완료 재생에서 추가 학습 0회, 기존 run 파일 173개 bytes/SHA와 final marker가 보존됐습니다.
- 준비 리뷰에서 중간 재시도 방지와 실행 직전 모델·환경 재검증을 보완했습니다. 최초 준비는 0fit으로 보존했고 수정한 revision2만 실행했습니다. 실제 학습의 재시도는 없습니다.
- 자연어 advisory는 요청하지 않았습니다. 표준 보고서의 advisory unavailable 상태를 수치 Judge 실패로 해석하지 않습니다.

## 해석 한계와 후속

이 결과는 고정된 합성 데이터·모델·후보와 final 40 slate에서 Judge v2 수용 조건을 충족했다는 근거입니다. 실제 사용자 효과·Production 성과·인과 기전 입증은 아닙니다. v2를 결과를 본 뒤 설계했으므로 validation 재실행을 독립 증거로 확대하지 않습니다. 2σ는 미리 고정한 운영 판정 문턱이며 p-value 유의성 검정이 아닙니다.

목표 모드의 profile/D5 사전 차단 연결과 변별력 있는 지표·bootstrap 진단은 여전히 후속입니다. 이번 실험을 목표 모드 전체 병합·완성으로 표현하지 않습니다. 이 final은 이미 소비했으므로 다른 후보 검증을 위해 marker를 초기화하거나 재사용하지 않습니다.

## 증거 SHA256

원본 모델·예측·로컬 경로·비공개 데이터는 커밋하지 않고 실험 artifact로 보존했습니다.

| 증거 | SHA256 |
|---|---|
| profile.json | `351a23ca59f9b260701683c68b4cc1629b6f139f338dc06ec043245e219df21c` |
| preflight.json | `bca6dea145d5cdaa66e56bfd4f9814adfcdd0c5b901224a858b656b81d2c2c33` |
| pre-execution-review.json | `af35ab51bf5d2465ba3a37b2ba3f5ca7195dca339f6aba066fc3cd1589ec7d28` |
| outcome.json | `80e5bad574aea42ffcf0df4934db7c7c70b68c429c0433c8879b88fb73d424e5` |
| numeric-audit.json | `ab13d044fb2add6847fb536f77086926aa07a522ac51d0973d760387b315c5c8` |
| training-input-audit.json | `6fec9f9875a9cc7b9a4fb2d99b3eb9cae9f8adb51a1607e402b0085f29cd6605` |
| independent-result-review.json | `b1282cf94be8cb78c65c75ab047976385f6528637cdce9cb04789fb3cff76e81` |
| replay-verification.json | `2a407e0cb3112292c7cad6473763064792ef7c70adcd81e895b9bdd30a982c76` |
| run/experiment-ledger.jsonl | `24b838c97b652f4055d425d2df19f1a086689daaa06c165d9af11f8c90d47780` |
| run/research-record.json | `58f268bca78ce31b2965434fbe43dd0cf6c16222108965280e8476d73170923d` |
| run/research-report.md | `7a40fbf4ce3260875b82f341dfcfc5b5004769a34b5befafbb741b0f0465516a` |
| final 소비 marker | `1b8fa339b53c069cd46665bcec9180b386b931fcd0e0e6f2ee15b4b1b7e7ff11` |

독립 결과 리뷰에서 수정이 필요한 발견 사항은 없었습니다. 원장 artifact 123개 SHA,
학습 sidecar 22개, 기존 보호 파일 568개 SHA, trusted 소스 56개와 calibration 7개 지표를
독립 대조했습니다. 리뷰 중 추가 학습·registry 변경·원본 final label 파싱은 0회였습니다.
