# 로컬 자율 ML 실험 에이전트: 실행보다 먼저 검증 가능성을 만들기

> 실측 기록 · 2026-09-03. 실제 두 trial, checkpoint 중단·재개, 단일 final batch와 종료
> 재호출을 확인했습니다. 최종 7개 지표의 평균은 모두 개선됐지만 고정된 채택 기준에는
> 미달해 baseline을 유지했습니다. 실험 절차의 완주와 모델 채택을 구분한 기록입니다.

## 1. 해결하려는 문제

목표는 사람이 가설을 주면 AI agent가 코드를 수정하고, 모델을 새로 학습해 기준 모델과
비교한 다음, 관측된 결과를 다음 실험에 반영하는 연구 도구입니다. YouTube 추천의 클릭
확률(CTR)을 예측하는 모델은 이 자율 실험 과정을 검증하기 위한 테스트베드입니다.

코드 변경과 실행에 성공했다는 사실만으로는 이 목표를 증명할 수 없었습니다. 모델이 정말
새로 학습됐는지, 같은 데이터·seed로 비교했는지, agent의 개선 주장과 실제 수치가 일치하는지,
중단 뒤 이미 끝난 agent 호출·학습을 반복하지 않는지에 대한 근거가 필요했습니다. 마지막
평가 데이터를 반복해서 보며 좋은 결과만 고르면 평가 자체도 신뢰할 수 없게 됩니다.

이번 작업은 이 문제를 작은 로컬 실행 경로에서 검증 가능하게 만드는 데 집중했습니다.
Kubernetes·클라우드 배포나 새로운 범용 agent framework를 추가하는 대신, 기존 workspace,
학습 runner, 수치 Judge, ledger, Controller를 연결하고 실제 실패를 측정했습니다.

## 2. 설계의 핵심: 실행·판정·설명을 분리하기

| 역할 | 담당하는 일 | 담당하지 않는 일 |
|---|---|---|
| Coding agent | 가설과 validation feedback을 받아 candidate 코드·테스트를 변경하고 자기 설명을 반환 | final 정답 열람, 공식 지표·임계값 변경, 자기 주장으로 승격 확정 |
| Harness와 수치 Judge | 입력·기준 SHA·seed 고정, 독립 학습, prediction 봉인, 지표 계산·판정, checkpoint 기록 | LLM의 자연어 설명을 측정값으로 취급 |
| 새 문맥의 연구 기록 Judge | 구조화된 실험 기록을 읽고 근거·한계에 대한 advisory 검토 | 모델 재학습, 수치 판정 덮어쓰기, 추가 실험 자동 실행 |

Validation은 다음 실험을 위한 피드백에 사용합니다. Final holdout은 validation 반복이 끝난
뒤 초기 baseline과 선택된 champion을 비교하는 종료 평가입니다. Validation에서 champion이
바뀌었다고 최종 채택된 것은 아닙니다. 여기서 final **1회**는 하나의 소비 marker 아래
5개 seed의 baseline/candidate를 각각 학습하는 **10 fit의 paired batch**를 의미합니다.

각 실험은 SHA·diff·prediction·학습 receipt·지표·판정을 연결합니다. 중단 복구는 이 기록을
소비하는 기존 Controller가 담당합니다. 보고서 게시가 중단된 경우에는 input·ledger·terminal
결속을 검증해 보고서만 재개하며, 이를 위해 실험이나 final을 다시 실행하지 않습니다.

이 경계는 동일 OS 사용자의 악의적 접근까지 막는 완전한 보안 sandbox라는 주장은 아닙니다.
평가 계약을 candidate가 바꾸지 않는 운영 전제와, 실제 코드로 검증하는 파일·입력·소비
무결성 경계를 구분합니다.

## 3. 실제 문제를 발견하고 해결한 과정

### Agent 실행과 Windows 제약 — #52 / PR #53

실제 agent 호출에서 실행 정책 차단, `uv`의 외부 cache 접근 거부, sandbox 사용자가 만든
pytest 임시 폴더의 접근·회수 문제가 드러났습니다. 요청한 workspace-write/read-only 범위를
유지하면서 호출에 한정해 Windows sandbox 구현과 승인 정책을 명시했습니다. 전역 정책이나
full-access 설정을 바꾸지 않았습니다.

또한 coding 단계는 기존 설치 Python과 임시 데이터 없는 설정 계약 테스트를 사용하고,
실제 학습·예측은 Harness가 수행하도록 역할을 나눴습니다. 이 범위에서 실제 agent의 코드
변경, 표적 테스트 6개, 단일 seed의 두 독립 학습·채점과 workspace 회수를 확인했습니다.
확률 지표 개선과 ranking 저하가 함께 관측됐으므로 이를 모델 승격이나 final 개선으로
표현하지 않았습니다. Windows pytest 임시 경로의 ACL·회수 제약은
[#54](https://github.com/bbungjun/Autoresearch/issues/54)로 분리했고, 등록된 temp의
회수 구현과 검증 결과는 §10에 기록했습니다.

### 자연어 검토를 공식 판정으로 쓰지 않기 — #55 / PR #56

분산된 코드 변경·학습·평가 증거를 구조화된 연구 기록과 Markdown 보고서로 연결하고,
새 문맥의 read-only Judge가 한 번 검토하도록 구현했습니다. 재호출 시 중복 검토를 막기
위해 호출 intent와 원래 응답·receipt를 결속했습니다. 실패한 검토의 관측 토큰·시간도
버리지 않고, private 경로·자격 증명 형태 텍스트를 정제했습니다.

실제 독립 Judge smoke는 공유된 `evaluation_id`를 모델 실행 식별자의 충돌로 오해했습니다.
실제로 이 ID는 평가 snapshot/split을 뜻하므로 같은 paired 비교에서 동일해야 합니다.
잘못된 의견을 숨기거나 원하는 답이 나올 때까지 재호출하지 않고, 기록과 prompt에 ID의
의미를 명시했습니다. 이는 LLM 검토를 advisory로 제한해야 하는 실제 근거입니다.

### 기준 모델의 변동량을 가정하지 않고 측정하기 — #57 / PR #59

한 seed의 높은 점수만으로 모델이 안정적이라고 판단할 수 없었습니다. 고정 baseline을
seed 101~105에서 각각 새로 학습하는 5회 single-fit으로 7개 지표의 raw 값과 표본 표준편차
σ(ddof=1)를 기록했습니다. 고정 모델의 재점수화도, 같은 baseline을 두 번씩 학습하는
불필요한 10-fit 비교도 하지 않았습니다.

일곱 지표 모두 유효 관측 5개를 확보했고 σ가 기존 `> 1e-6` 조건을 충족했습니다. 예를 들어
NDCG@10 평균은 0.7678924740923143, σ는 0.012644841892910248이었습니다. 0을 epsilon으로
바꾸거나 임계값을 낮춰 판정을 열지 않았습니다. 이 수치는 해당 fixture·코드·설정의 변동량이며
다른 모델·데이터로 일반화하지 않습니다. 전체 raw 값과 평균·σ는
[spec §4.10](../specs/2026-08-14-paper-grounded-autonomous-ml-research-harness.md)에 있습니다.

### 유효 CSV가 내부 형식 변환에서 실패한 문제 — #58 / PR #61

300,000행의 유효한 CSV를 측정하자 일반 문자 입력은 처리됐지만 backslash가 많은 입력은
실패했습니다. 원본 CSV 크기만 제한하고, JSONL로 바뀌며 문자가 escape되는 확장을 충분히
고려하지 않은 내부 80MiB 상한이 문제 설명과 일치했습니다. worker의 exit 1만으로 원인을
단정하지 않고, 실제 부분 출력과 다음 행을 썼을 때의 크기를 대조했습니다.

허용 문자를 축소하는 대신 내부 출력 상한만 유한한 104MiB로 보정했습니다. 허용 입력의
보수적인 최대 크기는 `361 byte × 300,000 = 108,300,000 byte`이며 104MiB 이내입니다.
외부 CSV 65MiB·300k행·물리 행 226byte, parser 10초·256MiB 제한은 그대로 유지했습니다.

기존에 실패한 동일 입력과 최대 JSON 확장 입력을 다시 측정해 모두 처리했습니다. 최대
확장 사례는 108,000,000byte의 JSONL을 만들었고 실제 ingestion은 4.525초였습니다.
별도 worker 관측의 peak working set은 25,493,504byte였습니다. 초과 행·물리 행·파일 크기
입력은 계속 거부됐습니다. 메모리는 별도 worker의 관측값이며 전체 애플리케이션 메모리나
다른 장비의 성능 보장이 아닙니다. 최대 확장 사례의 중복 key도 parser 용량 검증용이지
모델 품질 평가용 데이터가 아닙니다.

위 변경은 각각 독립 리뷰와 해당 CI를 거쳐
[PR #53](https://github.com/bbungjun/Autoresearch/pull/53),
[PR #56](https://github.com/bbungjun/Autoresearch/pull/56),
[PR #59](https://github.com/bbungjun/Autoresearch/pull/59),
[PR #61](https://github.com/bbungjun/Autoresearch/pull/61)로 반영됐습니다.

## 4. 이번 E2E에서 무엇을 검증하는가

이번 가설은 class weighting과 트리 복잡도를 조절하면 확률 보정과 ranking의 균형이
개선되는지입니다. 첫 trial은 `scale_pos_weight=1.0`을 적용하고, 두 번째는 실제 validation
feedback을 받은 agent가 같은 범위에서 최소 수정하도록 합니다. 결과를 좋게 만들기 위해
평가 데이터·metric·`2σ/-1σ` 임계값을 바꾸지 않습니다.

측정은 같은 config로 세 번 호출하되 매번 새로운 측정 출력 디렉터리를 사용합니다.

1. 첫 validation checkpoint의 원래 append가 fsync·lock 해제 후 `created=True`를 반환한
   직후 `KeyboardInterrupt`를 주입합니다.
2. 같은 config로 재개해 첫 trial의 candidate·receipt를 유지하고, 그 trial의 agent·학습은
   반복하지 않으면서 두 번째 trial과 종료 평가를 수행하는지 확인합니다.
3. 종료 결과를 다시 호출해 agent·학습·final을 반복하지 않고 같은 결과·보고서를 사용하는지
   확인합니다.

중단은 정확한 ledger·checkpoint ID·stage에만 적용하는 **측정 경계의 주입**입니다.
순정 CLI를 외부에서 강제 종료하거나 임의 지점의 장애를 모두 복구했다는 실험이 아닙니다.
Final marker를 소비한 뒤의 중단은 재평가가 가능한 중단 지점으로 취급하지 않습니다.

관측 코드 자체가 ledger tail을 복구하면 복구 전 증거를 바꿀 수 있으므로, 관측에는 일반
파일 읽기와 streaming hash만 사용합니다. 신규 측정 wrapper는 기존 runtime을 한 번만
호출하며 새 재개 엔진이나 registry 자동 생성·reset 기능을 갖지 않습니다. 관측 파일 개수는
성공 호출 수가 아니므로, trial별 receipt와 원본 hash를 함께 비교합니다.

구현의 독립 집중 회귀는 wrapper와 기존 runtime 재개 테스트를 합쳐 40개가 통과했습니다.
이는 중단 조건·원래 메서드 복원·관측 비변경·기존 출력 거부·실패 증거 보존의 근거이지,
아래 실제 E2E 관측을 대신하는 수치는 아닙니다.

실측 종료 후 전체 Harness 회귀는 **1,033 passed / 13 skipped / 기존 경고 2건**
(501.07초), 측정 wrapper·판정 golden 집중 회귀는 41 passed(1.11초)였습니다.
Ruff와 diff check도 통과했고, 독립 reviewer가 코드·문서·원본을 최종 대조했습니다.

## 5. 실제 E2E 결과

### 모델 품질: validation에서는 승격, final에서는 기준선 유지

첫 trial은 class weight를 auto→1.0으로 바꿨습니다. Seed 42의 NDCG@10이
0.7817132868→0.7794390051로 낮아져 `discard / primary_not_improved`가 기록됐습니다.
확률 보정 지표가 좋아졌다는 이유만으로 ranking의 악화를 무시하지 않았습니다.

재개 후 두 번째 agent는 첫 feedback을 받아 class weight=1.0과 `num_leaves` 31→63을
선택했습니다. 실제 patch는 학습 기본값과 공유 학습 설정을 바꿨으며, 두 번째 candidate는
`9122ab51de8c16bb0b9be8017642d1d1d0e6a135`입니다. 첫 candidate가 폐기됐기 때문에 처음
baseline에서 필요한 변경을 다시 적용했습니다. 신규 피처 추가 실험은 아닙니다.

두 번째 screening의 NDCG@10 개선폭은 +0.0033538832491794013이었고, 이어진 seed 101~105
confirmation 평균 개선폭은 +0.036972964886937555였습니다. 후자가 고정된
2σ=0.025289683785820496을 넘고 guardrail도 통과해 실제 validation `promote`가 발생했습니다.

하지만 final은 별도 데이터에서 판단했습니다. 아래는 final 5개 paired seed의 평균이며,
표시값은 소수점 10자리로 반올림했습니다. 개선폭은 큰 값이 좋은 지표에서는 candidate−baseline,
LogLoss·Brier에서는 baseline−candidate로 방향을 맞췄습니다. 판정은 반올림 전 값으로 합니다.

| Final metric | Baseline 평균 | Candidate 평균 | 개선 방향 Δ |
|---|---:|---:|---:|
| NDCG@10 ↑ | 0.7829057233 | 0.8081143457 | +0.0252086224 |
| Recall@10 ↑ | 0.9950000000 | 1.0000000000 | +0.0050000000 |
| NDCG@24 ↑ | 0.7843004381 | 0.8081143457 | +0.0238139077 |
| grouped ROC-AUC ↑ | 0.8941304348 | 0.9006521739 | +0.0065217391 |
| PR-AUC ↑ | 0.4642324504 | 0.5103150191 | +0.0460825688 |
| LogLoss ↓ | 0.1571948645 | 0.1302961329 | +0.0268987316 |
| Brier ↓ | 0.0372185981 | 0.0264611741 | +0.0107574240 |

모든 지표의 평균이 개선됐지만 primary NDCG@10의 raw 개선폭
`0.02520862238046171 < 0.025289683785820496 (2σ)`이므로 최종 판정은
`discard / primary_threshold_not_met`, Controller 결론은 `no_improvement`,
`baseline_retained=true`입니다. 여기서 `no_improvement`는 **채택 기준을 충족한 개선이
없다**는 결론이지 관측된 수치 개선이 전혀 없다는 뜻이 아닙니다. 근소한 차이를 이유로
임계값을 낮추거나 final을 재평가하지 않았습니다. 이 정책 판정을 통계적 유의성 검정으로
해석하지 않습니다.

평균만으로 반복 간 차이를 가리지 않도록 validation confirmation과 final의 seed별
NDCG@10을 함께 남깁니다. 아래 역시 표시만 소수점 10자리로 반올림했습니다.

| Seed | Validation baseline | Validation candidate | Final baseline | Final candidate |
|---|---:|---:|---:|---:|
| 101 | 0.7899734882 | 0.8009745997 | 0.7927134487 | 0.8175848005 |
| 102 | 0.7623291747 | 0.8087717202 | 0.7740670082 | 0.8161732909 |
| 103 | 0.7667907613 | 0.8076604693 | 0.7827007890 | 0.8252071188 |
| 104 | 0.7594751444 | 0.8008965583 | 0.7909803627 | 0.7965796679 |
| 105 | 0.7608938020 | 0.8060238473 | 0.7740670082 | 0.7850268504 |

### 자율성: feedback 수정과 이미 끝난 작업의 비반복

실제 두 번째 agent 입력의 첫 trial feedback은 원래의 7개 지표·delta와 일치했습니다.
첫 trial 완료 checkpoint 뒤 중단한 다음 같은 config로 재개했으며, 첫 trial의 candidate와
관련 원본 24개 hash가 유지됐습니다. 첫 trial의 prepare/agent receipt는 각각 1개, paired
receipt는 1개, training receipt는 2개로 재개 전후 변하지 않았습니다.

전체 실행량은 coding agent 2회, 독립 fit 24회, fresh-context 연구 기록 Judge 1회였습니다.
Fit 구성은 첫 screening 2회 + 두 번째 screening 2회 + validation confirmation 10회 +
final 10회입니다. 두 번째 trial의 추가 호출은 정상적인 feedback 실험이며 첫 trial의 중복
실행이 아닙니다. Final은 하나의 소비 marker 아래 5개 paired 결과를 남겼습니다.

종료 재호출에서는 관측 대상 **208개 파일 hash가 동일**했고 추가·변경·삭제는 모두 0개였습니다.
기존 runtime의 비반복 회귀와 원본 receipt 개수·hash를 함께 확인해 agent·학습·final·기록 Judge가
추가 실행되지 않았음을 검증했습니다. 관측 대상 밖의 모든 파일까지 불변이라고 주장하지는 않습니다.
독립 원본 감사에서도 관측 파일 208개의 hash/stat, ledger 연결 artifact SHA 151건,
학습 execution 24건과 coding execution 2건의 exit 0, terminal·marker·intent·manifest 결속이
일치했습니다. 이는 디렉터리 개수만으로 실행 성공을 추정한 결과가 아닙니다.

### 시간과 비용: 실행 구간만 측정하고 모르는 비용은 null

| 호출 | 관측 결과 | `run_local_research` 구간 |
|---|---|---:|
| 첫 trial 후 중단 | `interrupted`, 주입 여부 true | 156.511초 |
| 같은 config 재개 | trial2·final·보고서 완료 | 866.336초 |
| 종료 결과 재호출 | 208개 원본 재사용, 추가 실행 없음 | 3.023초 |

이 시간은 Python cold import, 사용자의 대기·판단 시간, 호출 전후 관측 비용을 포함하지 않습니다.
Agent와 학습 시간이 runtime 안에 포함되므로 아래 시간을 다시 더하지 않습니다.

| Agent 역할 | 호출 시간 | Input tokens | Cached input | Output tokens | Reasoning output |
|---|---:|---:|---:|---:|---:|
| Coding trial1 | 73.891초 | 181,018 | 150,016 | 2,001 | 419 |
| Coding trial2 | 79.328초 | 253,365 | 211,328 | 2,454 | 812 |
| 연구 기록 Judge | 29.577초 | 95,861 | 0 | 1,389 | 475 |

모두 CLI receipt의 관측값입니다. Cached input과 reasoning output은 각각 해당 token 범주의
부분집합이므로 input/output에 중복 합산하지 않습니다. 달러 비용과 사람 개입 횟수는
측정하지 않았으므로 각각 `null`입니다. 비용 절감률이나 완전 무인 실행을 추정하지 않습니다.
세 LLM 호출의 합계는 input 530,244 / output 5,844 tokens입니다.

### 독립 Judge가 발견한 설명의 한계

Fresh-context Judge는 한 thread의 한 turn으로 호출됐고 command 실행 이벤트는 0개였습니다.
`availability=available`, 검토 결과 `concerns`를 남겼습니다.
공유 evaluation ID는 이번에는 정상적인 paired 비교로 해석했지만, 두 가지 설명 부족을
지적했습니다.

첫째, trial2의 `observed_metrics`에는 seed42 screening의 candidate 절대값과 5-seed
confirmation의 평균 delta가 함께 있습니다. 각각의 수치는 원본과 일치하지만
`metric_scope=validation screening candidate`라는 하나의 표기가 두 범위를 구분하지
못합니다. 예를 들어 NDCG@10 절대값 0.7850671700658637의 screening delta는
+0.0033538832491794013인 반면, 같은 요약의 +0.036972964886937555는 confirmation 평균입니다.
수치 계산 오류로 결론내릴 문제가 아니라 출처·집계 범위 설명을 보완해야 할 문제입니다.

둘째, `run.baseline_sigmas`는 기록에 존재하지만, `2σ/-1σ` 판정 규칙과 실제 산출 threshold를
검토자에게 명시하지 않아 final 사유를 기록만으로 재검증하기 어렵다는 지적입니다.
σ 값 자체가 누락됐다고 표현하지 않습니다. 이 보고서에서는 위 raw 개선폭·threshold를
원본 계약과 대조해 설명했으며, 구조화 기록의 설명 보강은
[#62](https://github.com/bbungjun/Autoresearch/issues/62)로 분리했습니다.

Judge는 코드 patch나 테스트 실행 내용을 모두 직접 검토한 것이 아니라, 전달된 구조화 기록과
참조 evidence에 한정해 의견을 냈습니다. 원래 연구 기록·Judge 응답은 보존하고 원하는 의견을
얻기 위해 LLM을 다시 호출하지 않았습니다. 이 결과는 모델 판정과 별개로 **실험을 설명하는
agent interface에도 검증 가능한 출처 표기가 필요하다**는 개선 과제를 남겼습니다.

### 무변경 및 판정 시나리오의 증거 구분

별도로 #52에서 보존한 실제 무변경 paired receipt를 읽기 전용으로 판정 재생했습니다.
연결 artifact 11개의 hash와 scalar를 독립 대조했고 7개 delta는 모두 0.0,
`should_confirm=false / primary_not_improved`였습니다. 새로운 학습·agent·final은 실행하지
않았으며 이 과거 receipt의 baseline SHA는 이번 #60 기준선과 다릅니다. 따라서 이를 이번
baseline의 새 5-seed 무변경 E2E로 집계하지 않습니다.

이번 실행에서는 실제 validation promote와 final discard를 관측했습니다. Controlled
golden의 revise 분기나 계획에 있었던 유효 피처 추가 실험까지 실제로 수행했다고 표시하지
않습니다. 테스트의 분기 검증, 과거 실제 receipt 재생, 이번 E2E를 각각 별도 근거로 남깁니다.

### 원본 근거 식별

비공개 로컬 경로 대신 주요 원본의 논리적 이름과 SHA-256을 남깁니다. Seed 표는 validation/
final `pair.json`, 지표 평균·결론은 `research-record.json`, 호출 수·비반복은 세 호출의
`measurement.json`과 전후 관측 파일, 검토 의견은 `research-judge.json`을 대조했습니다.

- 연구 기록: `125d4ac21f4898e55b133fcae18e39a5bd562819f9192ffd37f033a04d019d37`
- 독립 Judge 결과: `f2ad208cfb86958e4f237c0476f6e1a02db13265e7e36d2ab1c0e66cdbb533ab`
- 중단 호출 측정: `4586406e0f19ad0697b77d0903ceae6e2911e294b2dad9dc9a2ace2db1803529`
- 재개 호출 측정: `b77ccaed2c99d7b94a3d5aaf5ecb2b81713d6fc00527b2654f446b4f22984101`
- 종료 재호출 측정: `41a07b15492f76977c4fe52d70e5801e9113e336910379bc4563363438300b35`

전체 변경·검증 경과는 [Task 7 실측 기록](../plans/2026-08-15-local-research-harness-mvp.md),
판정·재개 계약은 [spec §4.10–4.11](../specs/2026-08-14-paper-grounded-autonomous-ml-research-harness.md)이 정본입니다.

## 6. 재현 조건과 실행 방법

기준선은 commit `8dd67038d98817b3b4a5f33a4d9dd5009c2ce9fd`, seed는 screening 42와
confirmation 101~105입니다. Embedding은 `intfloat/multilingual-e5-small` revision
`614241f622f53c4eeff9890bdc4f31cfecc418b3`이며 GPU CUDA 환경을 사용합니다.
LightGBM 학습은 CPU입니다. 두 과정을 모두 GPU 학습이라고 부르지 않습니다.

재현에는 같은 fixture/snapshot·metadata·모델 파일과 라이브러리 identity, 실측 raw σ,
고정된 HarnessRunConfig가 필요합니다. 실제 입력과 실행 증거는 로컬에 보존하고 이 문서에
private 경로나 원본 데이터를 게시하지 않습니다. 따라서 저장소 checkout만으로 동일
실험 결과가 즉시 재현된다고 주장하지 않습니다.

아래는 실행 형식만 보여주는 예시입니다. `<...>`는 사용 환경의 절대경로로 대체해야 하며,
기존 소비 marker를 삭제하거나 같은 final을 새 run으로 반복하기 위한 명령이 아닙니다.

```text
<GPU_PYTHON> -m scripts.research_harness.measure_e2e --config <CONFIG> --out <NEW_MEASUREMENT_1> --interrupt-after-first-validation
<GPU_PYTHON> -m scripts.research_harness.measure_e2e --config <SAME_CONFIG> --out <NEW_MEASUREMENT_2>
<GPU_PYTHON> -m scripts.research_harness.measure_e2e --config <SAME_CONFIG> --out <NEW_MEASUREMENT_3>
```

CLI의 중단 반환값은 130이며 `interruption_injected=true`를 보존합니다. 이번 첫 측정의
PowerShell wrapper는 native 반환값을 명시적으로 전달하지 않아 외부 관측 exit는 1이었습니다.
따라서 실제 중단 근거는 measurement와 durable checkpoint이며 외부 exit 130을 관측했다고
주장하지 않습니다. 이후 호출은 native 반환값을 명시적으로 전달했습니다. CLI 실패 반환값은
1, 정상 runtime 반환은 0이며, exit 0 자체가 모델 개선을 뜻하지 않습니다.
`invocation.json`, `before.json`, `after.json`, `measurement.json`으로 호출 구간과 원본
identity를 확인합니다. 재개 사이에 config·모델·라이브러리·Harness 코드를 바꾸지 않습니다.
Prediction 설정에는 명시된 embedding 값만 넣어 agent가 바꾼 학습 기본값을 덮어쓰지 않습니다.

## 7. 검증 범위와 남은 한계

- 이번 데이터는 **2일의 학습 히스토리와 validation 160 slate를 갖는 합성 테스트베드**입니다.
  운영의 30일 히스토리·실사용자 CTR·온라인 A/B·분포 변화에 대한 품질 검증이 아닙니다.
- 5-seed σ는 해당 기준선의 반복 변동량입니다. 통계적 일반화나 현재 판정 임계값의 보편적
  최적성을 증명하지 않습니다. 임계값은 이번 성과를 만들기 위해 조정하지 않았습니다.
- 사람이 가설·데이터·모델·예산을 준비하고 실행 조건을 승인했습니다. 제한된 두 trial의
  자율 실행과 무인 연구 시스템 전체의 자율성을 구분합니다. 이 준비 과정과 AI-assisted
  구현·독립 검토 자체를 숨기지 않고 협업 및 검증 설계 역량으로 설명합니다.
- LLM Judge는 실제로 도메인 의미를 오해했습니다. 구조화된 설명을 보강해도 무오류가
  보장되지 않으며, advisory 의견은 공식 수치 판정을 대체하지 않습니다.
- 원본 #60 실험 당시에는 #54의 Windows 임시 경로 회수 제약이 미해결이었습니다.
  후속 §10에서 등록된 temp 회수를 검증했지만 이를 범용 sandbox 테스트 지원 완료나
  원본 실험의 실행 범위를 소급해 넓힌 것으로 표현하지 않습니다.
- 온전한 동일 상태 루트의 final marker는 재소비를 막지만, 상태 루트 전체가 삭제된 과거를
  파일 부재만으로 탐지하지는 못합니다. 자동 reset은 제공하지 않습니다.
- Parser 측정은 한 Windows/Python 환경에서 사례별 한 번 수행했습니다. 동시 부하나 다른
  하드웨어의 SLA가 아니며, 전체 프로세스 메모리와 별도 worker 메모리를 구분합니다.
- 관측 token·시간의 범위를 공개하되 달러 비용은 미측정 `null`을 유지합니다. 무료 사용,
  비용 절감률 또는 사람 개입 횟수를 추정해 채우지 않습니다.

설계·측정 원본의 정본은 [Research Harness spec](../specs/2026-08-14-paper-grounded-autonomous-ml-research-harness.md)과
[Task 7 구현·실측 기록](../plans/2026-08-15-local-research-harness-mvp.md)입니다.

## 8. 실험에서 발견한 설명 문제를 제품 개선으로 연결하기 — #62

**문제:** 새 문맥 Judge가 지적한 것은 모델 성능 문제가 아니라 연구 기록의 설명 방식이었다.
하나의 trial에서 단일 seed 점수와 5-seed 평균 개선폭을 같은 범위로 표시하면, 정확한 값도
불일치처럼 보인다. 이는 agent에게 원본 숫자만 전달하는 것으로는 충분하지 않다는 사례다.

**해결:** 새로운 v2 기록에서 지표별 관측 범위·seed·집계 방법을
분리하고, 사전에 고정한 판정 규칙과 threshold를 같이 전달하도록 구현했다. 기존 module의
projection을 사용해 Judge 입력과 사람이 읽는 Markdown이 같은 설명을 소비하게 한다.
공개 interface를 추가하거나 수치 Judge를 바꾸는 대신 설명 책임을 기존 report module
안에 유지했다. 기존 v1 기록을 덮어쓰는 방식은 원본 prompt/응답/digest의 연속성을 깨므로
선택하지 않았다. 완전한 근거가 없으면 수치를 보존하되 범위는 unknown으로 표시한다.

**결과:** 기존 구현에서 새 계약 테스트의 RED를 확인한 후 구현했다. 독립 reviewer가
보고서 관련 테스트 **63개 통과**를 재확인했고 차단 발견 사항은 없었다. 검증에는 서로
다른 screening/confirmation 값, 실제 수치 Judge의 threshold 경계, 부분·중복·미연결 근거,
null 값과 원본 불일치, 기록 유실 및 내용 변조, v1 게시 중단 복구를 포함한다. 수정 전
구현에서 고정한 v1 record·prompt·Markdown 해시 3개도 모두 일치한다.

실제 #60 기록의 연결된 **12쌍**을 읽기 전용으로 메모리에 투영해 세 trial의 범위와 원래
수치를 대조했다. NDCG@10 기준으로 Trial 2의 screening 점수 **0.7850671701**과 confirmation 평균 delta
**0.0369729649**는 서로 다른 범위로 분리됐다. Final 평균 delta **0.0252086224**가
threshold **0.0252896838**에 못 미친다는 기존 판정도 그대로 설명된다. 이 수치는 표시용
반올림이며 실제 비교는 원래 정밀도로 확인했다. 기존 완료 관측 파일 **208개**의 해시가
전부 같았고 final 소비 marker도 보존됐다. 전체 Ruff와 diff 검사도 통과했다.
추가 전체 회귀·CI 및 병합 결과는 [#62](https://github.com/bbungjun/Autoresearch/issues/62)에
연결된 PR의 검증 기록을 정본으로 확인한다.

**한계:** 개선된 설명을 실제 fresh-context LLM이 어떻게 평가하는지는 다시 측정하지
않았다. 테스트와 읽기 전용 대조로 projection을 검증했으며, Judge 정확도 향상이나 모델
성능 향상을 이번 변경의 성과로 주장하지 않는다. 이전 실험 원본을 다시 게시하거나
새 runtime으로 재실행하지 않았으며 v1 호환성은 기존 입력 계약의 report 게시에 한정한다.

## 9. Agent 입력 탐색 정상화와 남은 테스트 회수 제약 — #54A

**문제:** E2E는 완주했지만, 일반적인 pytest 임시 파일을 만드는 agent 작업은 Windows
sandbox와 host의 파일 소유권 차이 때문에 회수가 실패한 이력이 있다. candidate-safe
입력 읽기 거부도 별도로 관측됐다. 따라서 현재 성과를 범용 데이터 탐색·테스트 자율성까지
확대해 설명해서는 안 된다.

**접근:** 입력 읽기 정상화와 임시 파일 회수를 나눴다. source에서 private
staging rename 게시를 확인했지만 당시 입력 ACL 자체는 보존된 workspace에서 확인할 수
없어 원인 가설로 남겼다. 전체 접근 전환과 강제 삭제 대신, 검증된 입력에 한정한 읽기
공개와 실패 증거 보존을 구현했다. 사용자가 승인한 A 범위는 새 검증 입력의 읽기 공개와
합성 검증뿐이며, B의 소유권 변경·기존 실패 폴더 삭제는 포함하지 않는다.

**재현 결과:** CLI 0.153.0-alpha.5의 실제 coding-agent 경로에서 새 v2 입력을 읽는
PowerShell 명령이 `System.UnauthorizedAccessException`, HResult `0x80070005`로 실패했다.
Agent의 최종 응답은 `blocked`였고 31.594초가 걸렸다. 종료 코드 0만으로 입력 접근 성공을
판정하지 않았다. 적용 전 입력은 protected DACL이었고 sandbox 그룹의 읽기 ACE가 없었다.
입력 내용·소유자·부모 ACL은 전후 동일했고, 이 새 작업 공간은 정상 회수됐다.

앞선 43.031초 관측은 agent가 예외 원인을 숨긴 채 blocked만 반환했으므로 ACL 오류 확정
근거로 쓰지 않았다. 보조 `codex sandbox` 호출도 permission profile 인자 부족으로 명령
실행 전 종료됐으므로 sandbox 읽기 실패 재현과 구분했다. 전역 profile을 추가하지 않고
실제 adapter 경로로 대조한 이유다.

**구현 중 발견한 문제:** 첫 native 권한 준비는 `handle_identity` 단계에서 중단됐다.
적용된 ACL은 0개이고 CLI도 실행되지 않았다. 별도 읽기 전용 점검에서 Python의
64비트 `st_dev`와 legacy Win32 API의 32비트 볼륨 번호를 비교한 것이 원인으로 확인됐다.
Mock은 두 값을 같은 형식으로 돌려주어 이 플랫폼 차이를 잡지 못했다. 하위 비트만 잘라
통과시키지 않고 Python 버전에 맞는 파일 identity 형식과 native API layout을 검증했다.
이는 원래의 sandbox 읽기 거부와 구별되는, 수정 코드의 플랫폼 호환성 문제다.

다음 native 시도는 첫 객체에 READ ACE를 추가한 뒤 readback에서 중단됐다. 실패 receipt의
`applied_count=1`, CLI 미실행을 보존했고 이를 앞선 0개 적용 실패와 구분했다. 새 합성
공간에서 비교한 결과 owner·group·기존 ACE는 같았지만 Windows가 input root와 자식의
`SE_DACL_AUTO_INHERITED` 비트를 추가했다. 따라서 권한 검사를 통째로 생략하지 않고,
그 비트의 0→1만 허용하며 전후 값을 기록했다. 보호 비트 제거·다른 control 변경·기존 ACE
변경은 계속 실패하고, 전체 적용 뒤 모든 handle을 재확인한다. 이 두 문제에 대해 재현
테스트가 먼저 실패하는 것을 확인한 뒤 수정을 적용했다.

**최종 native 결과:** 동일한 합성 candidate view로 만든 새 작업 공간에서 실제 agent가
입력 파일 6개(manifest·slate·history 2개·metadata 2개)를 모두 읽고 SHA256을 계산했다.
응답은 `readable`이며 manifest SHA가 host의 사전 검증 값과 일치했다. 실제 PowerShell
명령의 종료 코드·출력도 확인했다. 32.045초는 한 번의 agent 호출 시간이며, 앞선
31.594초 실패와 비교해 처리 속도 개선을 주장하지 않는다.

- 권한 준비 12/12 객체 완료. 추가 ACE는 정확히 `0x120089` READ, 비상속이다.
- CLI 시작 전 별도 PowerShell ACL 대조로 기존 ACE 바이트·순서와 소유자 보존을 확인했다.
  입력 밖의 workspace root·부모·`harness_out` SDDL은 변경되지 않았다.
- agent 실행 후에도 모든 입력 내용·소유자·부모 ACL이 같고, 새 disposable workspace는
  정상 회수됐다. 이 결과는 일반적인 pytest 산출물 회수 성공을 의미하지 않는다.
- 기존 E2E 증거·final marker를 포함한 208개 SHA256 대조에서 변경은 0개다.
- 최종 성공 호출의 usage는 input 38,619 / cached input 18,560 / output 1,174 /
  reasoning output 450 tokens다. cached·reasoning은 각각 해당 token 수의 하위 관측치이며
  합산하지 않는다. 단가 근거가 없으므로 달러 비용은 `null`이다. 앞선 실패 호출까지
  포함한 전체 작업 비용으로 제시하지 않는다.

**포트폴리오에서 설명할 역량:** AI assistant가 구현하고 독립 agent가 검토하되,
mock 통과를 실제 실행 성공으로 오인하지 않고 OS 경계에서 실패를 재현·분리했다.
에이전트가 실험 재료를 탐색할 수 있도록 권한 준비를 작은 내부 모듈로 분리하면서
평가 정답과 host prediction 경로에는 적용하지 않았다. 성공뿐 아니라 잘못된 가정,
차단된 실행과 부분 적용 증거를 남긴 것이 이번 문제 해결의 핵심이다.

**코드 검증:** 최종 helper·coding agent·local trial·report 통합 테스트 122개가
194.84초에 모두 통과했다. 이 회귀에서는 실제 ACL·LLM·학습을 호출하지 않았다.
독립 reviewer는 별도 회귀와 보존된 native 전후 증거를 대조해 코드·문서의 차단 사항이
없음을 확인했다. 전체 Ruff·diff 검사도 통과했다. Linux 전체 CI 및 merge의 최종 상태는
#54에 연결된 부분 PR 기록에서 확인한다.

**남은 한계:** 실제 OS 검증은 Windows/Python 3.12의 현재 설치된 CLI 한 조합이다.
Python 3.11 identity와 타 플랫폼 무동작은 회귀 테스트로 검증하며, 다른 Windows/CLI
버전의 실제 동작까지 보장하지 않는다. 추가 권한만 READ이며 기존 effective 권한을
회수하거나 입력을 보안상 read-only로 강제하는 기능은 아니다. 모델 학습·성능 측정·
final 소비는 하지 않았고 기존 실패 폴더를 수정하지 않았다. A 완료 시점에는 pytest 임시
산출물 회수를 #54B로 남겨 이슈를 종료하지 않았으며, 후속 결과는 아래 §10에 기록한다.

## 10. Windows pytest 임시 산출물 회수 — #54B

**문제와 근거:** 입력 탐색이 복구돼도 sandbox가 만든 private pytest 폴더를 host가
회수하지 못하면 candidate 완료가 막힌다. 설치 pytest 소스는 `0700`으로 임시 폴더를
만들고 지정 basetemp도 삭제 후 다시 생성한다. 따라서 host 사전 생성이나 TEMP 위치
변경만으로 파일을 만든 사용자와 회수하는 사용자의 권한 차이가 없어지지 않는다.

**선택과 트레이드오프:** 같은 sandbox 주체의 고정 helper가 등록된 temp만 회수하고
host가 실제 부재를 확인하는 접근을 선택했다. 공식 CLI의 읽기 전용 실행으로 해당 주체를
확인했다. 전역 설정·ACL·owner를 바꾸지 않으며, 등록 root 밖의 arbitrary private
산출물이나 host 강제 종료까지 지원하는 완전한 회수 시스템은 이번 MVP 범위가 아니다.

**독립 리뷰로 보완한 순서:** agent의 finally에서 즉시 temp를 회수하면 실패 시 candidate
patch 보존 전에 빠져나갈 수 있다. 이를 피하기 위해 증거 보존 뒤에 회수를 배치하는
작은 호출 수명 관리 interface를 사용한다. 회수 실패는 계속 실패이며 후보를 Controller에
전달하지 않는다. 추가 리뷰에서는 `.git` 확인의 무제한 읽기를 16 KiB로 제한하고,
helper 중단 직후 로그 저장까지 실패해도 원 중단과 부분 회수 개수를 보존하도록 보완했다.
AI assistant의 구현을 독립 agent가 검토하고 실패 주입 테스트로 확인한 과정이다.

**구현:** 큰 범용 cleanup framework 대신 작은 stdlib helper와 호출자의 `ExitStack`을
사용했다. Windows coding prepare에만 등록 temp를 만들고 Codex가 실행하는 명령의 임시
환경을 지정한다. Codex host 자체의 TEMP는 그대로 유지한다. helper는 고정 anchor의
자식만 전체 사전 검증 후 삭제하고 host가 실제 비어 있음을 다시 검사한다. 로그·회수
receipt는 작업 공간 밖 attempt에 남으며 후보 evidence와 최종 REPORT의 digest 대상이다.

**실제 검증 결과:** 설치된 Windows/Python 3.12/CLI 조합에서 새 합성 workspace로
작은 `tmp_path` 테스트를 실행했다. 아래 두 호출은 mock이 아닌 실제 sandbox 실행이다.

| 합성 시나리오 | pytest 종료 코드 | 등록 temp 회수 | 회수 helper 시간 | 새 workspace 회수 |
| --- | ---: | ---: | ---: | --- |
| 정상 테스트 | 0 | 4/4개 | 968 ms | 성공 |
| 파일 생성 후 의도적 assertion 실패 | 1 | 4/4개 | 1,156 ms | 성공 |

두 경우 모두 child의 TEMP/TMP/TMPDIR/PYTEST_DEBUG_TEMPROOT와 실제 `tmp_path`가
등록 경로를 사용하는 것을 테스트 안에서 확인했다. 회수 후 anchor는 유지되고 자식은
없었으며, 입력 내용·소유자·anchor 밖 sentinel·회수 전후 점검 대상 ACL·상위 ACL이
보존됐다. tracked 코드 변경도 없었다. 실패 테스트를 고쳐 성공한 것으로 바꾸지 않고
pytest 종료 코드 1과 helper 종료 코드 0을 분리해서 기록했다.

리뷰 수정 뒤 추가 정상 호출에서는 **회수 전 host의 private 파일 읽기가 실제
PermissionError로 거부됨**까지 확인했다. 이후 같은 생성 주체의 helper가 4개를
969 ms에 회수하고 host empty 검증과 worktree 회수를 통과했다. 따라서 이번 검증은
권한 문제가 없는 폴더만 지운 결과가 아니다. 이 호출의 agent 시간은 13,906 ms,
usage는 input 36,844 / cached input 18,048 / output 307 / reasoning output 31이었다.

첫 정상 호출의 agent 시간은 17,109 ms, usage는 input 38,101 / cached input 18,688 /
output 334 / reasoning output 94 tokens였다. 실패 테스트 호출은 15,358 ms,
input 38,385 / cached input 18,688 / output 317 / reasoning output 82 tokens였다.
cached·reasoning은 각각 하위 관측치이며 합산하지 않는다. 이는 해당 호출 관측값이지
전체 개발 비용이 아니며 단가를 적용하지 않아 달러 비용은 `null`이다.

**코드 검증:** 최종 helper·coding agent 테스트 74개가 19.87초에 통과했다. 원 예외와
부분 회수 개수를 유지하는 로그 저장 실패, 경로 교체·hardlink·초과 크기 입력 거부,
실제 owned fake process의 attach 실패·timeout·leak 및 호출 수명 관리 경로를 포함한다.
이 회귀는 실제 sandbox/ACL 호출과 분리했다. 전체 Ruff와 `git diff --check`도 통과했다.
관련 helper·coding agent·local trial 통합 회귀 94개도 178.04초에 통과했고, 이 실행 이후
추가된 중단/로그 실패 2개는 앞의 최신 74개 재실행에 포함됐다. 독립 reviewer는 별도
회귀와 세 native 증거, 코드·문서 계약을 대조해 차단 사항 없음을 확인했다.

**검증의 한계:** 이번 작업은 ML 품질 개선이나 전체 E2E 재실험이 아니다. 학습·final
평가는 0회이며, 기존 실패 폴더와 #60 원본 실험을 수정하지 않았다. 기본 pytest/temp를
지원하며, 별도 경로의 private 산출물·host 강제 종료·다른 Windows/CLI 조합까지 회수
성공을 보장하지 않는다. 등록 범위 밖은 기존 workspace fail-closed를 유지한다.

## 11. 중간점검: 구현 상태와 실증 의무를 다시 맞추기 — #67

**문제:** 세부 구현·실측 기록은 누적됐지만 상위 spec에는 `구현 미착수`, plan과 인덱스에는
Task 2 이후가 미완료인 초기 상태가 남아 있었다. 실제로 구현하지 않은 것으로 오해하게
할 뿐 아니라, 모든 체크를 한꺼번에 닫으면 반대로 미실측 자율성까지 완성된 것으로 보이게 된다.
현재 개인 저장소 #17과 최초 설계의 이전 조직 #769도 구분할 필요가 있었다.

**해결:** spec §12에 구현·회귀, 실제 측정, 잔여 실측·수용 판단, 제품 로드맵을 나눠 적고
plan의 Task 6·Task 7·전체 체크리스트와 문서 인덱스를 같은 상태로 맞췄다. 당시 문제와
측정 수치는 과거 시점의 기록으로 보존했다. 실제 CLI·설정 모델도 대조해 초기 설계의
직접 Judge 상태 루트 옵션을 현행 config 기반 실행과 구분했다. AI assistant가 정리한
완료 주장도 코드·테스트·원본 실측의 범위 안에서만 채택하는 방식이다.

**결과:** Task 1~6 핵심 구현과 Task 7 주요 실측이 완료됐다는 현재 위치를 문서에서
추적할 수 있다. #60의 validation promote는 일반 승격 실증 의무를 충족하지만, 유효한
피처 하나를 추가하는 구체적 시나리오는 완료하지 않았다. 다음 항목도 그대로 남긴다.

- 실제 깨진 candidate를 실패 feedback 뒤 새 agent가 한 번 수정해 학습·평가까지 복구하는 실측
- 사람 개입 횟수 계측과 중간 승인 없는 완주 증명; 달러 비용은 계속 미측정 `null`
- calibration 분포·개선/무변경 결과에 근거한 threshold·coverage 기준의 수용 판단
- #62와 #54A/B를 모두 반영한 새 통합 E2E 및 개선된 설명에 대한 fresh-context Judge 관측

**다음 권고:** 먼저 실패 한 번 → feedback → 수정 한 번 → 학습·평가·보고의 최소 복구
시나리오를 고정된 판정 기준으로 검증한다. 그다음 피처 추가, 필요 시 별도 임베딩 교체
실험으로 자율성 범위를 넓힌다. 결과가 미승격이면 그대로 보존하며 성공을 위해 기준을
낮추지 않는다. 상세 관측·실행 조건은 [plan의 잔여 검증 우선순위](../plans/2026-08-15-local-research-harness-mvp.md#잔여-검증-우선순위--2026-09-03-권고)를
따른다. 이번 작업은 문서 정합화이며 새 실험 실행이나 수용 기준 변경을 승인하지 않는다.
기존 실험 원본·final 소비 상태를 수정하지 않고 새 모델 성과나 비용 절감도 주장하지 않는다.

**문서 검증:** 수정한 spec·plan·인덱스·이 기록의 로컬 Markdown 링크 대상 127건에 누락이
없고 `git diff --check`를 통과했다. CLI와 실행 설정, 수치 판정 상수, 실패 feedback·registry
회귀의 존재를 코드와 대조했다. 문서 정합화 검증이며 새 학습·LLM·E2E 실행 결과가 아니다.

## 12. 실패한 코드를 고쳤는가, 정상 코드에서 다시 시작했는가 — #69

**문제:** 자동 오류 복구 실증을 준비하며 Controller가 오류 feedback은 전달하지만 다음
coding workspace를 정상 champion에서 만든다는 사실을 확인했다. 실패한 candidate의
SHA나 patch가 다음 요청에 없으므로, 다음 trial이 성공해도 깨진 코드를 실제 수정했다고
볼 수 없었다. 오류를 받은 뒤 정상 코드에서 재구현하는 능력과 코드 복구 능력이 달랐다.

**해결 선택:** 실패 후보를 champion으로 바꾸면 비교 baseline까지 깨진다. 측정 도구에서
실패 patch를 두 번째 workspace에 몰래 재주입하면 제품에 없는 기능을 실증한 것처럼 보인다.
따라서 정식 prepare 요청에 직전 실패 후보를 나타내는 선택 입력 하나를 추가하고,
champion 기준 workspace에 검증된 실패 diff를 복원하는 최소 경로를 선택했다.
HEAD·paired baseline·최종 diff의 기준은 champion을 유지한다. 기록에는 복구 출처를 남기고
이전 record의 optional 필드 부재는 byte를 바꾸지 않는 방식으로 호환한다.

**사전 실험 조건:** 새 synthetic fixture(seed 6901, 평가일 2026-09-01), 기존 고정
baseline·E5-small·LightGBM, 별도 5-seed calibration을 사용한다. 첫 후보는 측정용 대역이
속성명 오타 하나를 주입하고 실제 학습 프로세스에서 실패하게 한다. 둘째 후보는 정식
복구 경로로 전달한 실패 코드와 오류 feedback을 실제 새 agent가 한 번 수정한다.
최초 오류가 agent에게 자연 발생했다고 주장하지 않으며 promote를 복구 성공 기준으로
사용하지 않는다. 공식 학습 receipt와 agent가 수행한 작은 단위 테스트의 학습도 구분한다.

**사전 측정 결과:** baseline 5회 독립 fit은 모두 성공했지만 Recall@10의 raw 값이
`[1.0, 1.0, 1.0, 1.0, 1.0]`이라 표본 σ가 0이었다. 다른 6개 지표의 σ는 기존
`> 1e-6` 조건을 만족했다. 측정 실패가 아니라, 이 합성 validation에서 Recall@10이
관측한 seed 모두 상한에 도달해 현재 채택 기준의 전제와 맞지 않는 상황이다.

| 새 fixture baseline metric | 5-seed 평균 | 표본 σ (ddof=1) |
| --- | ---: | ---: |
| NDCG@10 | 0.7935075560357766 | 0.0071041641367052 |
| Recall@10 | 1.0 | 0.0 |
| NDCG@24 | 0.7935075560357766 | 0.0071041641367052 |
| grouped ROC-AUC | 0.8975271739130435 | 0.00383624054164796 |
| PR-AUC | 0.49559793611220787 | 0.04295995591399886 |
| LogLoss | 0.15157223571105577 | 0.004566722144103255 |
| Brier | 0.0349276585070097 | 0.0007021595985074564 |

사전에 고정한 조건을 따라 실제 agent 복구 호출 전에 이 제약을 보고했다. σ에 epsilon을
대입하거나 통과하는 fixture seed를 탐색하지 않았다. 복구 능력 측정과 성능 채택을 분리해
진행할지 사용자에게 확인했고, σ=0과 판정 기준을 유지한 채 복구 실증을 계속하도록 승인받았다.
실행 전 기준으로 실제 agent 복구·final·기록 Judge 호출은 0회이며, 이후 관측을 별도로 기록한다.
기존 #60의 관측 대상 208개 파일은 이전 hash와 모두 일치했고 final marker도 동일했다.
새 fixture의 final 평가 ID는 기존과 다르며 기존 raw 기록·판정 기준은 수정하지 않았다.

**구현 검증:** 핵심 Controller·runner·REPORT와 두 측정 wrapper의 통합 집중 회귀는
114개가 통과했다(203.38초). 별도 두 테스트는 실제 수치 비교·Controller·ledger·REPORT를
연결해 σ=0의 `inconclusive / insufficient_baseline_noise`가 실행 오류와 구분되고,
종료 재호출에서 추가 실행·기록 Judge 호출이 없는지 검증한다. 여기서 학습·agent·소비
grant는 대역이므로 실제 복구 성공의 증거로 세지 않는다. Ruff와 문서 링크 31건도 통과했다.

### 실제 단일 복구 실행 결과

승인 이후 실행을 한 번 수행해 **465.904초(약 7분 46초)**에 종료했다. 측정 전체는
466.307초이며 아래 agent 시간·attempt 합계는 이 시간에 포함되므로 더하지 않는다.
구현 코드 기준은 `dcbc07c`, 실행 시 HEAD는 실행 조건 문서만 추가한 `74bb7c6`이다.
설치 CLI는 `0.153.0-alpha.5`를 사용했다. 설정·script hash와 원본 receipt는 별도
로컬 측정 증거에 보존하고 데이터·모델·로컬 경로는 저장소에 게시하지 않는다.

| 단계 | 실제 관측 | 해석 |
| --- | --- | --- |
| 최초 후보 | 대역이 `features`를 `featuers`로 변경; 외부 LLM 0회 | 자연 발생한 agent 실수가 아닌 사전 주입 결함 |
| 최초 평가 | baseline 학습 성공, candidate `predict_crash` 및 실제 `AttributeError` | candidate는 fit 전에 실패 |
| 복구 준비 | 실패 파일과 복원 파일의 SHA-256 일치 | 정상 champion에서 새로 시작한 결과가 아님 |
| 실제 수정 | coding agent 1회, 명령 3개·파일 편집 1개 | reset/restore 없이 속성명 오타 한 곳 수정 |
| 재평가 | baseline/candidate 2회 학습 성공, `discard / primary_not_improved` | 정상 코드 복구 성공, 품질 향상은 없음 |
| final | 고정 seed 101~105의 5 pair·10회 학습, 새 소비 marker 1개 | `inconclusive / insufficient_baseline_noise`, baseline 유지 |
| 보고·검토 | REPORT 생성, 새 문맥 기록 Judge 1회, `concerns` | 원본 기록을 변경하거나 재평가하지 않음 |

원래 baseline은 `8dd67038d98817b3b4a5f33a4d9dd5009c2ce9fd`, 실패 후보는
`9d98b7295ed61b1c7c0ea99f4092b4d0ff5f15d8`이다. 수정 후보는 원 baseline과 같아
champion 기준 최종 patch는 0바이트다. 이는 편집하지 않았다는 뜻이 아니다. 실패 코드와
복구 코드 사이에는 실제 한 줄 편집이 있고, native agent stdout이 이를 뒷받침한다.
실패·복원 파일 hash는 모두
`c067eb10a756a2451f7f17f48801bb67e94b5f0398c28f9b84a1d98ece6620b3`이다.

정식 prediction은 **14회 중 학습 성공 13회, 결함으로 실패 1회**다. 성공 13회는
첫 baseline 1회 + 복구 뒤 validation 2회 + final 10회다. 사전 calibration 5 fit과
agent의 작은 CPU 단위 테스트 3 fit은 이 수에 포함하지 않는다. 해당 단위 테스트는
1 passed / 2 warnings / 1.11초였다. 물리 코어 탐색 fallback과 cp949 subprocess
출력 디코딩 경고가 있었으므로 경고 없는 실행이나 모든 환경 지원을 주장하지 않는다.

final의 각 pair는 코드·prediction·model hash가 같고 평균도 같다.

| 지표 | baseline 평균 | candidate 평균 |
| --- | ---: | ---: |
| NDCG@10·24 | 0.7735540422149131 | 0.7735540422149131 |
| Recall@10 | 1.0 | 1.0 |
| grouped ROC-AUC | 0.8909782608695652 | 0.8909782608695652 |
| PR-AUC | 0.47311568032228435 | 0.47311568032228435 |
| LogLoss | 0.1514273399934094 | 0.1514273399934094 |
| Brier | 0.03339508206947623 | 0.03339508206947623 |

σ=0인 판정 전제에서 중단하는 현재 계약에 따라 final의 공식 decision delta는
`unknown / not_available`다. 위 원시 평균의 동일성을 근거로 공식 delta를 0으로
고쳐 쓰지 않았다. 복구가 성공했다는 실행 결론과 성능 채택이 판정 불가라는 결론을
분리하며, 평가 기준을 바꿔 승격을 만들어 내지 않았다.

### 독립 검토, 비용과 남은 한계

| 실제 외부 호출 | 시간 | input tokens | cached input | output tokens | reasoning output |
| --- | ---: | ---: | ---: | ---: | ---: |
| 수정 coding agent 1회 | 47.438초 | 134,908 | 106,368 | 1,045 | 131 |
| 기록 Judge 1회 | 33.436초 | 69,878 | 0 | 1,320 | 184 |

cached·reasoning은 각각 input·output의 하위 관측치다. 대역인 최초 prepare에는 외부
호출 receipt가 없으므로 구조화 기록의 coding token coverage는 1/2다. 실제 모델
호출 하나가 누락된 뜻이 아니다. 달러 비용과 사람 개입의 자동 관측값은 여전히 `null`이다.

별도 운영자 관찰 기록에서는 **단일 실행 호출부터 프로세스 종료까지** 사용자 추가 승인,
사용자 코드 편집 관측, 코디네이터 코드 편집·stdin 입력·수동 재시작·추가 수정 기회가
각각 0이었다. 이는 해당 작업의 도구/메시지 이력에 근거한 수동 증언이며 자동 감지기가
아니다. 가설·예산 선택, fixture·calibration 준비, σ=0 실행 승인과 사후 검토는 제외한다.
전체 준비 과정의 사람 개입 0이나 미관측 외부 활동 부재로 확대하지 않는다.

새 문맥 기록 Judge는 구조화 기록만 받았으므로 원본 traceback·소스·편집 로그를 직접
검사할 수 없다는 `concerns`를 남겼다. 정확히 어떤 오류를 어떻게 고쳤는지는 그 입력만으로
독립 확정하기 어렵다는 합리적인 지적이다. 동일 evaluation ID는 비교 좌표로 해석하여
#62의 설명 개선을 관측했지만 모든 증거 가시성 문제가 해결된 것은 아니다. Judge를 다시
불러 좋은 의견을 얻거나 원본 record·REPORT·검토 결과를 덮어쓰지 않았다.

별도의 구현 비참여 reviewer는 원본 로그·patch까지 읽고 **차단사항 없음**으로 판단했다.
실제 오류와 전달 feedback, 실패/복원 파일 일치, 한 줄 편집, 13개 학습 receipt를 확인했다.
record source 118개·ledger artifact 참조 169개·report manifest·입력/ledger/result 결속·
Judge evidence 6개의 hash도 일치했다. 기존 #60의 관측 대상 208개는 모두 이전 hash와
같았으며 기존 final marker를 재사용하지 않았다. 기록 Judge보다 넓은 증거를 읽은 기술
감사이므로 둘의 의견을 같은 입력에서 나온 찬반처럼 비교하지 않는다.

#54A/B가 포함된 이번 실제 pytest 호출은 등록 temp **14/14개를 1,281ms에 회수**했고
host empty·process cleanup 확인을 통과했다. 신규 experiment workspace와 등록 worktree
잔존은 없었다. 이전 실패 폴더는 보존했으며 그 폴더까지 회수했다고 주장하지 않는다.

**현재 입증 범위:** 사전 주입한 속성명 오류 한 건을 실제 agent의 수정 한 번으로
복구해 재학습·평가·보고까지 끝낼 수 있다. 정상 baseline 복구이므로 모델 개선 성과는
없다. AI assistant가 구현하고 별도 agent가 실제 증거를 감사한 과정, 실패를 숨기지 않고
실행 성공·품질 판정·관측 한계를 분리한 점이 이번 포트폴리오의 핵심이다. 다음은 유효한
피처 추가 실증, 영분산 지표의 수용 정책 판단과 더 넓은 자율성·비용 계측이다. 이 작업으로
전체 MVP 완료나 임베딩/모델 자유 선택의 실증 완료를 선언하지 않는다.

## 13. 새 피처가 실제 모델과 실험 기록에 들어가는가 — #71

**문제:** 단일 오류 복구 다음에는 agent가 새 피처를 작성하고 실제 학습·평가까지 연결하는
실증이 필요했다. 기존 `local_training`은 feature table 전체를 모델에 전달하지만
receipt에는 고정 21개 이름만 기록한다. 따라서 새 열을 추가하면 실제 모델과 설명 증거가
어긋날 수 있었다. 이는 모델 학습 불가가 아니라 관측·계약의 불일치다.

**사전 대안 검토:** 처음 고려한 과거 클릭 카테고리 비중은 현재 합성 이력이 T-2/T-1뿐이고
완전 학습일은 T-2라, 당일 제외 규칙을 지키면 학습행에서 모두 cold-start가 된다. 긴 이력
생성은 유용하지만 fixture 계약 확장이므로 이번 최소 실험과 분리했다. 조회수/관측 영상
나이도 검토했으나 fixture metadata에서 조회수와 나이가 반대 방향의 같은 순서를 가져
tree가 새 분할 정보를 얻기 어렵다. candidate 품질이나 evaluation label을 본 뒤 피처를
고른 것이 아니라 입력 기간·계산 구조를 검토해 범위를 좁힌 과정이다.

**선택:** 독립 reviewer의 제안 중 기존 최대 관심사 유사도를 보완하는 평균 관심사
유사도를 선택했다. 키워드 하나의 높은 일치와 전체 관심사의 적합도를 구분한다는 가설이다.
기존 21개 피처·고정 E5·LightGBM 설정은 그대로 두며 실제 agent가 candidate에 22번째
열을 구현한다. 키워드 중복·빈 입력·as-of·clip/반올림 계약과 손 계산 기대값은 실행 전에
spec §4.8.1로 고정한다. 준비 worker는 실제 피처를 미리 구현하지 않는다.

**최소 연결과 리뷰:** 기존 `LocalFeatureBatch` interface를 유지하고 학습 module 안에서
학습·예측 열 순서/타입·추가 수치 유효성을 검증한 뒤 실제 입력 열을 receipt에 기록하는
방식을 택했다. 새로운 feature registry나 별도 공개 옵션을 만들지 않는다. 독립 리뷰는
calibration CLI의 선택 baseline 인자가 내부의 초기 SHA 상수 제한 때문에 새 기준선에
작동하지 않는 문제도 발견했다. 기본 SHA를 유지하고 명시 full SHA·실제 commit 일치와
고정 5-seed·새 출력 조건을 검증하도록 최소 보완한다.

**실행 전 봉인:** 새 fixture는 seed 7101/T=2026-09-01로 한 번 준비했으며 baseline은
`cf1642a5ba61fd85940c2a41762a91b6b4073044`로 고정했다. calibration과 실제 실행의
SHA·snapshot·prediction 설정을 대조했고 새 final ID는 이전 두 실험과 다르다.
calibration의 기존 σ 전제를 만족한 뒤 최초 registry를 준비했다. #69의 σ=0 실행 승인을
재사용하거나 seed 탐색·epsilon 대입으로 조건을 바꾸지 않았다.

**준비 코드 검증:** 추가 피처 연결은 RED 27건으로 receipt 불일치와 사전 검증 누락을
확인한 뒤 보완했다. calibration의 새 SHA는 RED 2건 후 명시 pin을 지원했다. 최종
확장 회귀는 154 passed / 2 warnings / 17.29초이며 기존 MLflow의 Pydantic deprecated
경고는 숨기지 않는다. 실제 작은 CPU 모델의 22열 fit/predict·native model·receipt와
21열 baseline 회귀를 포함한다. 추가 열은 테스트용 대역이며 평균 관심사 피처의 구현이나
실제 실험 성공 증거로 세지 않는다. 전체 Ruff·변경된 calibration script Ruff도 통과했다.

### Calibration과 실제 실행 결과

별도 baseline calibration은 고정 seed 101~105의 5회 학습을 완료했다. 7개 지표 모두
표본 표준편차(ddof=1)가 기존 최소값 1e-6보다 컸다. 이 구간의 coding·final 호출은 0회다.

| 지표 | validation 평균 | 표본 표준편차 |
| --- | ---: | ---: |
| NDCG@10 | 0.7853719357553741 | 0.031146796617403168 |
| Recall@10 | 0.99625 | 0.005590169943749454 |
| NDCG@24 | 0.7863867240492686 | 0.030362046005638774 |
| grouped ROC-AUC | 0.8936413043478261 | 0.009817645590152394 |
| PR-AUC | 0.4931464140828802 | 0.03906264239060431 |
| LogLoss | 0.15689895477735716 | 0.013612612198730584 |
| Brier | 0.03623990959647297 | 0.001626391593488854 |

실제 Controller 호출은 924.622초에 종료돼 REPORT까지 생성했다. 그러나 **피처 추가의
공식 학습·평가 실증은 실패**했다. coding 2회 모두 코드·patch를 보존했지만 prepare의
`workspace_cleanup_failed`로 중단됐다. 첫 후보는 `cc6b1eb6e8f4d7bfff5094453989bc964989c86b`,
둘째는 `d7aefcca5a371840390ed53a81a59f58a6ef0440`이다. prepare가 성공 반환되지 않아
ledger의 두 trial candidate SHA는 null이다. 두 번째도 원 champion에서 새로 구현했으며,
첫 코드에 대한 validation 기반 수정 성공 사례가 아니다.

| 구간 | 관측 | 해석 |
| --- | --- | --- |
| 실제 coding | 2회, 모두 patch 보존 | 코드 작성과 prepare 성공은 다름 |
| 추가 피처의 공식 validation 학습 | 0회 | 피처의 성능·split 사용은 미검증 |
| final | 기존 baseline끼리 5 pair·10회 학습, 21열 | 추가 피처 후보의 평가가 아님 |
| 수치 결론 | `discard / primary_threshold_not_met`, baseline 유지 | 동일 구성의 차이 0이지 피처 무효의 증거가 아님 |
| 새 문맥 기록 Judge | 1회, `concerns` | 22열 후보 실증 근거 부족을 정확히 지적 |

각 final pair의 코드·prediction·model hash와 7개 지표는 동일했다. NDCG@10의 차이 0은
고정 임계값 0.062293593234806335에 미달했다. 새 final 소비는 한 번이며, 이 평가 대상을
초기화해 피처 후보로 다시 평가하지 않는다. calibration 5 fit, final 10 fit, coding 중
작은 CPU 단위 테스트의 fit은 서로 다른 범위로 기록한다.

### 회수 실패의 근거와 후속 조치

두 cleanup은 각각 1.282초·1.468초에 `ValueError`, object_count null, removed_count 0으로
실패했다. helper 프로세스는 회수됐지만 파일 회수는 실패한 것이다. 유력 가설은 기존
`test_hardlinked_input_is_rejected`가 남긴 하드링크와 `_agent_temp`의 단일 링크만 허용하는
사전 검사 충돌이다. 두 agent 모두 그 테스트 파일을 실행했다. 로그 형태도 전체 preflight
완료 전 거부와 일치한다. 다만 잔류 하위 폴더 열람이 거부되어 실제 inode까지 확인하지
못했으므로 **확정 원인이 아닌 강한 가설**로 남긴다. ACL 변경·접근 우회·강제 삭제는 하지 않았다.

원본 기술 감사에서 첫 agent의 신규 테스트 RED 4건→GREEN 4건과 최종 관련 회귀
108 passed / 2 deselected를 확인했다. 앞서 published-fixture 두 테스트가
`fixture_state_conflict`로 실패한 뒤 선택에서 빠졌다. 전체 회귀 통과로 포장하지 않는다.
넓은 코드 검색·traceback에서 fixture 소스 일부가 노출돼 사전의 좁은 열람 범위보다
넓어진 한계도 있다. 두 번째 후보의 작은 학습 fixture에서는 추가 열이 상수 0이었으며,
단위 테스트상 22열 전달만으로 실제 데이터에서의 유효성을 주장하지 않는다.

[후속 #73](https://github.com/bbungjun/Autoresearch/issues/73)은 독립된 hardlink 최소 재현,
테스트 소유 alias의 finally 정리 가능성, 기존 안전 정책 보존, published-fixture 충돌의
분리 진단을 추적한다. 실행 중 수동 수정이나 세 번째 coding 기회는 넣지 않았다.
실패 workspace와 raw 증거를 보존하며 새 실제 실험의 예산·final 대상은 별도로 결정한다.

### 비용·자율성·포트폴리오 결론

| 외부 호출 | 시간 | input tokens | cached input | output tokens | reasoning output |
| --- | ---: | ---: | ---: | ---: | ---: |
| coding 1 | 383.187초 | 1,502,374 | 1,409,152 | 10,899 | 2,179 |
| coding 2 | 238.202초 | 782,581 | 700,800 | 7,348 | 1,840 |
| 기록 Judge | 29.592초 | 72,763 | 0 | 1,383 | 229 |

cached·reasoning은 각각 input·output의 하위 관측치이며 합산하지 않는다. 달러 비용과
자동 사람 개입 값은 null 그대로다. 단일 실행 구간의 별도 운영자 관찰에서는 사용자 추가
승인·관측된 코드 편집, 코디네이터 코드 수정·stdin 입력·재시작·추가 coding 기회가 0이었다.
코디네이터는 읽기 검토·안내와 후속 이슈 발행을 했으므로 운영자 활동 전체가 0인 것은 아니다.
이 수동 증언을 준비 과정까지 포함한 완전 자율성이나 외부 활동 자동 감지로 확대하지 않는다.

기존 #60 관측 208개·#69 관측 128개는 각 final marker를 포함해 사후에도 hash가 일치했다.
새 문맥 Judge는 구조화 기록만, 구현 비참여 기술 reviewer는 raw 로그·patch까지 읽었다.
따라서 코드 작성의 존재를 확인한 기술 감사가 기록 Judge의 증거 부족 지적을 무효화하지 않는다.

이번 성과는 기존 interface만 사용해 학습 입력과 기록의 불일치를 고치고, 실제 자율 실행에서
새로운 테스트/회수 충돌을 발견해 재현 가능한 후속 문제로 분리한 것이다. 피처 성능 개선은
입증하지 못했다. PR #72는 준비 코드와 이 결과 기록만 반영하며 #71·상위 #17을 닫지 않는다.
AI assistant의 작성·독립 검토와 실패 해석 과정을 함께 남기되, 실패를 성공 수치로 바꾸지 않는다.

## 14. 안전 검사를 낮추지 않고 테스트 잔류물을 정리하기 — #73

**문제:** #71의 두 candidate가 코드를 작성한 뒤 temp 회수에서 중단됐다. 이전 원본 로그와
코드는 보안 테스트가 남긴 하드링크를 유력 원인으로 가리켰으나, sandbox 잔류 inode는
권한 때문에 직접 확인하지 못했다. 이 한계를 유지한 채 새 로컬 입력으로 인과를 검증했다.

**재현:** 신규 회귀는 기존 local training의 hardlink 입력 거부 테스트와 temp helper의
hardlink preflight 거부 테스트를 등록된 바깥 temp anchor 안에서 직접 실행한다. 정상 실행,
loader 예외, 기대한 거부가 일어나지 않는 assertion 실패를 포함한 5건 모두 수정 전에는
`_agent_temp._identity`의 `ValueError: unsafe_object`로 실패했다(1.15초). 보안 테스트 자체가
통과해도 남겨 놓은 링크 때문에 이후 회수가 막히는 관계를 host의 실제 파일 링크로 재현했다.
이 결과가 과거 sandbox 폴더의 inode까지 확인했다는 뜻은 아니다.

**대안과 결정:** 회수 helper가 하드링크를 허용하도록 바꾸면 삭제 범위 검증 정책이
달라진다. 권한 확대나 host 강제 삭제도 필요하지 않다. 기존 fixture 테스트에 이미 있는
`try/finally` 패턴을 따라, 두 테스트가 성공적으로 만든 정확한 alias만 해제하도록 했다.
원본 파일의 내용을 덮어쓰지 않고 거부 assertion도 유지한다. 실패를 skip하거나 테스트를
선택에서 제외하는 방법은 사용하지 않았다. 런타임 제품 코드·sandbox·ACL·소유권·학습/평가
정책은 변경하지 않았다.

**검증:** 신규 회귀 5건과 두 원본 테스트 파일 전체는 47 passed / 10.89초였다. 회귀는
alias 해제 뒤 원본 내용과 내부 ordinary 파일이 그대로인지 확인하고, 바깥 temp를 정상
회수한 뒤 anchor와 `.git`·외부 sentinel을 대조한다. temp 안의 원본은 이 후속 회수 단계에서
정상 삭제되며 외부 입력의 영구 보존을 테스트한 것으로 확대하지 않는다. 관련 Ruff·diff
검사는 통과했다. 실제 LLM coding·새 품질 실험·final 소비는 수행하지 않았다.

구현에 참여하지 않은 reviewer의 독립 재실행도 47 passed / 10.59초였다. 수정 전 두
테스트 함수를 파일 변경 없이 메모리에만 로드한 대조에서는 신규 5건 모두 unsafe_object로
실패했다(1.85초). 테스트가 단순히 변경 코드와 함께 통과하도록 작성된 것이 아니라
수정 전 결함을 검출한다는 근거다. 코드·문서 리뷰에서 차단 사항은 발견되지 않았다.

**함께 발견한 별도 문제:** 짧은 host 경로의 기존 published-fixture 표적은 1 passed
(29 deselected, 진단용 선택 실행)였고, 이후 위 47건에는 해당 테스트를 제외 없이 포함했다.
그러나 동일 seed1937/T=2026-09-01의 새 합성 fixture를 길이만 다른 root에 생성하면
59자 root에서는 성공하고 153자 root에서는 `fixture_build` 오류가 났다. 숨겨진 원본
예외는 파일 경로 길이270자의 `FileNotFoundError(errno=2)`였다. 하드링크 없이도 실패하므로
[후속 #74](https://github.com/bbungjun/Autoresearch/issues/74)로 분리했다. 정확한
OS/라이브러리 실패 연산과 과거 sandbox 오류와의 동일성은 추가 진단이 필요하다.

**결과와 한계:** 이번 수정은 두 보안 테스트와 후속 temp 회수가 양립하도록 만드는 최소
개선이다. 모든 테스트의 임시 산출물을 정리하거나 긴 경로·권한 문제까지 해결한 것은 아니다.
특히 #74를 해결하기 전에는 #71 피처 실험을 바로 재실행하지 않는다. 이전 #60의208개,
#69의128개, #71의112개 관측 파일과 각 final marker는 기존 reference hash와 일치했다.
실패 workspace는 그대로 보존했다. AI assistant 구현과 독립 검토를 분리하고, 새로 재현한
원인과 미확인 가설·후속 문제를 구분한 과정 자체를 문제 해결 근거로 남긴다.

## 15. 중첩 fixture의 생성 경로와 내용 identity 분리 — #74

**문제와 근거:** #73에서 새로 재현한 fixture 생성 오류를 실제 파일 연산까지 추적했다.
기존 실패 workspace를 열거나 지우지 않고 seed1937/T=2026-09-01의 새 합성 입력으로
비교했다. root70자는 성공했지만 root130자는 snapshot lock의 `Path.touch`에서 전체
265자 경로의 `FileNotFoundError(errno=2)`로, root153자는 action log 임시 파일의
`shutil.copyfile`에서 전체270자 경로의 동일 예외로 실패했다. 상위 공개 오류는
`StageCError(stage=fixture_build)`다. 이 재현만으로 과거 sandbox 오류의 원인을 모두
확정한 것은 아니다.

**원인과 선택:** 같은 모듈의 읽기·검증에는 Windows 확장 경로용 `_io_path`가 이미
있었지만 하위 생성기로 넘기는 staging과 일부 탐색·재사용·정리에는 적용되지 않았다.
[Windows 경로 문서](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation)의
확장 경로 형식을 기존의 검증된 절대 경로 I/O에 적용했다. 시스템 설정 변경, temp를 등록
경계 밖으로 이동, 파일 이름/내용 계약 변경보다 기존 패턴의 누락 경계를 보완하는 접근을
선택했다. 생성 함수에만 적용한 첫 수정은 snapshot 탐색·재사용에서 여전히 실패했다.
따라서 같은 모듈 안의 생성·snapshot 탐색·완성본 재검증·소유 staging 회수까지 적용했다.
공개 receipt는 접두사 없는 canonical 경로를 유지하며 alias/reparse/hardlink 검사를
완화하지 않는다. daily action log·snapshot publisher 모듈은 변경하지 않았다.

**검증:** 신규 root130/153 생성·재사용 회귀는 수정 전 2 failed / 7.68초였다. 수정 뒤
신규 3건과 fixture·소비 상태·기존 피처 view 회귀는 49 passed, 2 skipped / 41.23초였다.
생성 후 의도적인 하위 오류를 주입해 공개 오류 code/stage와 private detail 비노출,
staging 회수·정리 경고 부재를 확인했다. 같은 입력의 짧은/중첩 root 간 전체 artifact와
descriptor·snapshot·manifest identity가 같았다. 별도로 수정 전 성공한 root70의 fixture와
수정 후 root130의 fixture도 전체17파일 SHA가 일치했다. 경로 변경으로 평가 데이터가
달라진 것이 아님을 변경 전 산출물까지 대조했다.

구현 비참여 reviewer는 신규 nested·fixture·소비 상태·candidate data view를 독립 실행해
86 passed, 2 skipped / 40.70초를 확인했다. skip 2건은 기존 Windows symlink 생성 제한과 FIFO 미지원이며
신규 nested 3건은 모두 실행됐다. 수정 전 함수를 파일 변경 없이 메모리에 복원하면 신규
3건 모두 실패했다(9.11초). 현재 생성 수정은 유지하고 cleanup만 수정 전으로 복원한 대조도
잔여 staging과 정리 경고로 실패했다(1 failed / 3.34초). 생성과 실패 회수 각각의 회귀가
실제 결함을 검출한다는 근거다.

코디네이터의 추가 검증은 신규 nested·candidate data view·local training·local feature
view·training features 5파일 99 passed / 34.39초였다. 전체 Ruff와 diff 검사가 통과했다.
Windows native 경로 검증과 원격 Linux CI는 별개이며 CI·merge 상태는
[PR #78](https://github.com/bbungjun/Autoresearch/pull/78)을 정본으로 확인한다.

기존 테스트를 긴 이름의 basetemp에서 실행한 첫 회귀는 11 failed, 35 passed, 2 skipped였다.
테스트 자체의 원시 `manifest.read_bytes` 등이 긴 경로에서 실패했다. 짧은 basetemp에서
기존 회귀를 수행하되 신규 테스트는 root130/153을 강제했다. 실패 테스트를 skip/deselect로
숨기지 않았으며, 모든 기존 테스트가 임의의 긴 경로에서 동작한다는 보장은 하지 않는다.

**범위 밖 발견:** 생성·재사용 뒤 중첩 fixture의 `prepare_candidate_metadata`가
`JUDGE_HANDOFF_INVALID / fixture_source_provenance`로 실패했다. root130의 snapshot
경로306자에서 `resolve(strict=True)`의 WinError3를 별도로 재현했다. candidate data view의
비슷한 I/O는 소스 감사 후보이지 도달·실패가 확정된 단계가 아니다.
[#77](https://github.com/bbungjun/Autoresearch/issues/77)에서 소비 경계를 검증한다.
또한 파일 I/O 없는 최소 재현에서 frozen/slots `StageCError`를 contextlib가 전달할 때
traceback 대입이 `TypeError`로 가려졌다. 예외 계약 수정은
[#76](https://github.com/bbungjun/Autoresearch/issues/76)으로 분리했다.

**결과와 한계:** 중첩 합성 fixture의 생성·재사용과 실패 회수를 보완했으며 임의 길이,
UNC/device 경로, 실제 sandbox 전체 동작은 보장하지 않는다. 실제 LLM coding·추가 피처
학습·품질 측정·final 소비를 수행하지 않았고 #71 실증은 여전히 미완료다. 기존 #60의208개,
#69의128개, #71의112개 관측과 각 final marker의 reference hash가 그대로임을 확인했다.
실패 workspace와 원본 로그를 보존했다. AI assistant의 단계별 RED/GREEN·내용 동일성
대조·독립 리뷰를 문제 해결 근거로 삼으며 생성 성공을 모델 성능 개선으로 표현하지 않는다.

## 16. 실패 원인을 전달하는 과정의 2차 오류 제거 — #76

**문제:** #71의 fixture 실패 뒤 `TypeError`가 덧붙어 원래 실패를 가렸다. #74에서는
파일 I/O 없이도 StageCError를 contextlib 경계로 전달할 때 같은 충돌을 재현했다. 이는
fixture 생성 실패와 별개인 오류 전달 결함이다. 에이전트가 실패 종류·단계를 해석하는
입력의 신뢰성을 떨어뜨리지만 실제 복구율이나 비용 손실은 측정하지 않았다.

**원인과 대안:** StageCError에 적용된 `frozen=True, slots=True`는 구조화 필드뿐 아니라
contextlib가 수행하는 traceback 대입까지 막았다. Python의
[예외 계약](https://docs.python.org/3/library/exceptions.html#BaseException.__traceback__)은
traceback을 쓰기 가능한 필드로 정의한다. frozen 옵션만 제거하면 오류 종류·실패 단계도
변경 가능해지므로 채택하지 않았다. contextlib를 우회하거나 TypeError를 삼키는 방법도
원본 오류 전달 계약을 복구하지 않는다.

StageCError 한 클래스에서 구조화 필드의 초기화 이후 일반 대입·삭제만 차단하고, 나머지는
Exception의 기본 동작으로 전달하도록 했다. slotted dataclass의 생성자·필드 기반 동등성과
hash·replace 동작은 유지했다. traceback·context·cause·suppression·notes는 Python 런타임
동작을 따르며 잘못된 타입을 대입했을 때의 기본 거부도 유지한다. 오류 문자열과 UTF-8
16바이트 식별자 축약은 바꾸지 않았다. 이는 일반 필드 불변성 계약이지 Python 내부 접근을
통한 적대적 변조까지 막는 보안 장치가 아니다.

**검증:** 구현 전 신규 회귀 24건은 10 failed, 14 passed / 1.20초였다. 실제 with와 중첩
contextmanager·pytest 경계의 원본 전달, 런타임 메타데이터 갱신·원인 연쇄가 실패했고 기존
필드 보호·메시지 계약은 통과했다. 수정 후 신규28건과 기존 모델28건은 56 passed / 0.35초였다.
모든6개 오류 code에 대해 동일 객체와 code/stage 보존을 검사한다. 명시적 원인 연쇄는
유지하고, `from None`이면 formatted traceback에도 하위 오류 sentinel이 노출되지 않는지
확인한다. 숨긴 context를 삭제하거나 실패를 성공으로 바꾸지는 않는다.

구현 비참여 reviewer의 오류·모델·fixture·candidate 회귀는 131 passed, 2 skipped /
17.89초였다. skip은 기존 Windows symlink 생성 제한과 FIFO 미지원이며 신규 오류28건은
모두 실행했다. 수정 전 클래스를 워크트리 변경 없이 메모리에서만 로드한 독립 대조는
10 failed, 18 passed / 0.13초였다. 코드·문서 차단 발견은 없었다. 로컬 Python3.12 결과와
Python3.11/3.12 원격 CI는 구분하며, CI·merge 상태는
[#76의 연결 PR](https://github.com/bbungjun/Autoresearch/issues/76)을 정본으로 확인한다.

추가 metadata·final candidate view·입력 게시·workspace 및 신규 오류 회귀 8파일은
265 passed, 1 skipped / 118.44초였다. skip은 기존 POSIX 실행 권한 전용 테스트다.
전체 Ruff·diff 검사와 문서 로컬 링크135개 검증이 통과했다. 기존 #60의208개, #69의128개,
#71의112개 관측 및 각 final marker를 reference hash와 대조해 변경 없음을 확인했다.

**범위와 후속:** 영향 조사에서 EvaluationSnapshotError와 JudgeError도 같은 최소
contextlib 호출로 TypeError가 발생했다. 이는 직접 전달 충돌의 재현이지 모든 실제 E2E
경로의 장애를 확인한 것은 아니다. 각 오류의 별도 계약을 검토하도록
[#79](https://github.com/bbungjun/Autoresearch/issues/79)로 분리했고 이번에 일괄 수정하지 않았다.
#77의 긴 경로 candidate 입력 소비는 그대로 남는다. 기존 raw 기록·final marker와 실패
workspace를 보존하고 실제 LLM coding·추가 품질 실험·final 소비는 수행하지 않는다.
AI assistant의 원인 재현·최소 수정·구현 비참여 검토를 기록하되, 오류 전달 개선을 모델
품질이나 자율 복구 성공률 향상으로 표현하지 않는다.

## 17. 중첩 fixture를 candidate 입력까지 소비하기 — #77

**문제와 근거:** #74는 길이 130·153자의 state root에서 동일한 fixture를 생성·재사용할
수 있게 했지만 candidate 소비는 완주하지 못했다. seed1937/T=2026-09-01의 새 합성 입력에서
root130의 snapshot root는 306자였고 validation/final metadata가
`fixture_source_provenance`, validation v1/v2 view가 `judge_snapshot_layout`에서 실패했다.
root153의 첫 action-log partition은 275자였으며 `pa.OSFile` open이 `WinError 3`으로
실패했다. 구현 전 기대 동작을 validation/final, v1/v2, source open 세 종류로 분리한 결과
5 failed, 기존 fixture 회귀 3 passed였다.

**원인과 선택:** fixture 생성 I/O에는 기존 Windows extended path adapter `_io_path`가
적용됐지만, candidate provenance와 source/snapshot/destination 관계 비교의 `resolve()`,
regular-file identity의 `lstat()`, `FixtureActionLogSource.open_partition()`에는 raw 경로가
남아 있었다. Windows 전역 설정이나 temp 위치를 바꾸면 실제 제품 경계를 검증하지 못하고,
extended path를 공개 handoff에 저장하면 기존 계약이 달라진다. 별도 filesystem 계층을
추가하지 않고 기존 adapter를 신뢰된 로컬 I/O에만 적용했다. 공개 receipt·manifest는 기존
canonical 절대 경로를 유지하고 source/destination 격리와 symlink·reparse·hardlink 검사는
그대로 수행한다.

**검증:** 세 신규 계약과 기존 중첩 fixture 회귀는 8 passed / 77.82초였다. 동일 입력의
짧은 root와 비교해 validation/final metadata bytes·receipt, v1/v2 manifest·게시 파일 hash가
같고 view 재사용도 성공했다. root153에서는 공개 partition 경로가 extended prefix 없이
275자임을 확인한 뒤 실제 handle payload의 digest·행 수와 open 전후 regular-file identity를
대조했다. Windows의 `pyarrow.OSFile.fileno()`는 descriptor를 제공하지 않으므로 기존
`_open_local_identity()` 계약에 따라 handle validity를 확인하고, identity가 제공되는
플랫폼에서만 path와 직접 대조한다.

Metadata/view/final/consumption 5파일은 100 passed / 161.03초였다. 공용 identity helper를
사용하는 fixture·workspace·runtime·feature view·run inputs·report·Windows sandbox 입력까지
넓힌 검증은 중복 없이 272 passed, 3 skipped였다. 기본 시스템 pytest 경로에서는 기존
`test_fixture.py`의 raw `Path.read_*()` 11건이 260자를 넘어 실패했고, 저장소 안의 짧은
`--basetemp`에서는 해당 파일 전체가 37 passed, 2 skipped였다. 이 테스트 실행 환경 한계를
제품 회귀와 구분해 남긴다. 전체 Ruff와 `git diff --check`는 통과했다.

저장소 전체 4,203건의 Windows/Python 3.12 xdist 실행도 짧은 basetemp에서 수행했다.
결과는 3,996 passed, 135 skipped, 80 failed / 400.88초였다. 80건은 모두 이번에 바꾼
research harness 밖에서 발생했고 `/bin/sh` 부재, symlink 생성 권한, POSIX 경로·파일 모드
단언과 cp949 decode 등 기존 Windows 비호환 조건을 포함했다. 따라서 로컬 전체 suite 통과는
주장하지 않으며, 변경 범위 회귀와 원격 Linux CI 근거를 구분한다.

**결과와 한계:** 중첩 합성 fixture는 생성·재사용에 이어 candidate metadata와 validation
view 게시·재사용까지 완주한다. 독립 리뷰 수정 검증을 위해 별도 합성 fixture의 final grant와
marker를 만들었지만 기존 실험의 final을 다시 소비하지 않았고 실제 평가·판정도 수행하지 않았다.
실제 coding agent·22열 학습·품질 판정을 실행하지 않았으므로 #71의 피처 실증은 여전히
미완료다. 임의 UNC/device 경로와 hostile filesystem 경쟁도 검증 범위가 아니다. 원격
Python 3.11/3.12, Feast/Postgres, lock drift, Ruff와 선택 이미지 CI는 PR #89에서 통과했다.
구현 비참여 독립 리뷰는 P1 1건과 P2 1건을 찾았다. P1은 긴 snapshot을 가진 fixture에서
final consumption registry가 raw `Path.resolve(strict=True)`로 실패해 final candidate view
전체를 막는 문제였다. 별도 합성 fixture와 marker로 `state_root_validation` 실패를 RED로
고정하고 registry의 신뢰된 내부 resolve·marker I/O·directory sync에 `_io_path`를 적용했다.
공개 grant evidence와 handoff 경로에는 extended prefix를 넣지 않았다. 수정 뒤 중첩 fixture,
consumption registry, final candidate view 3파일은 48 passed였다. 기존 #60/#69/#71의 final
marker와 관측 파일은 소비하거나 수정하지 않았다.

첫 수정 재리뷰에서는 resolved extended path로 검증한 뒤 `absolute()` alias를 evidence에
반환해 marker를 선점하고도 grant authorization이 실패하는 P1을 추가 발견했다. 긴 `..` alias를
RED에 포함하고, resolved path를 공개 canonical 경로로 되돌리는 내부 변환을 추가했다. registry와
grant는 정규화된 일반 절대 경로를 보존하고 실제 marker I/O에서만 extended path를 사용한다.

P2는 250자 candidate destination에서 267자 lock 파일을 raw `os.open()`으로 만들다가
`candidate_lock_prepare`로 실패하는 별도 게시 경계다. #77의 fixture 입력 범위 밖이므로
[#90](https://github.com/bbungjun/Autoresearch/issues/90)으로 분리했으며, 긴 destination까지
지원한다고 주장하지 않는다. 새 head의 전체 관련 회귀와 원격 CI는 다시 확인한다.

## 18. Snapshot과 Judge 오류의 2차 전달 충돌 제거 — #79

**문제:** #76에서 `StageCError`의 contextlib 충돌을 수정한 뒤 영향 범위를 조사하면서
`EvaluationSnapshotError`와 `JudgeError`도 같은 `frozen=True, slots=True` 조합으로 원본 대신
`TypeError`를 전파함을 확인했다. 최신 main `271b148`, Windows/Python 3.12의 파일 I/O 없는
최소 재현에서 두 클래스 모두 같은 결과였다. 이는 직접 오류 전달 결함의 증거이며 실제 E2E
실패 로그에서 두 오류가 발생했다는 뜻은 아니다.

**해결:** #76과 같은 최소 패턴을 각 오류 모듈에 적용했다. Dataclass 전체 frozen을 제거하고
code·stage·`dt`·`count`·`identifier_prefix`·`row_number`만 초기화 후 일반 대입·삭제로부터
보호한다. traceback·context·cause·suppression·notes는 기본 `Exception`에 맡긴다. 공용 기반
클래스를 만들지 않아 두 오류의 문자열과 선택 필드, Snapshot identifier 축약 계약을 섞지
않았다. equality/hash/repr/`replace()`는 유지했다.

**검증:** 제품 변경 전 RED 3종 36건은 22 failed, 14 passed였다. 모든 Snapshot code와 두
Judge code의 중첩 context manager 전달, 실제 `_run_lock`, runtime metadata와 원인 연쇄가
실패했고 기존 구조 필드 보호·타입 거부·dataclass value 계약은 통과했다. 구현과 optional
`None` 필드 회귀 보강 뒤 신규 38건은 모두 통과했다. 기존 Snapshot/Judge 소비 대조는 변경 전
79 passed, 3 skipped였고, 변경 후 두 오류를 직접 참조하는 테스트 전체는 짧은 basetemp에서
260 passed, 5 skipped였다. 전체 Ruff와 `git diff --check`도 통과했다.

기본 pytest temp의 영향 실행에서는 기존 `test_fixture.py` raw `Path.read_*()` 11건이 260자
초과 경로 때문에 실패했다. 같은 집합을 짧은 basetemp에서 모두 통과시켜 제품 회귀와 테스트
환경 한계를 분리했다. 임시 test root 삭제는 자동 승인 검사에서 거부되어 코드·커밋 밖 로컬
폴더로 남았으며 검증 결과에는 영향을 주지 않는다.

**결과와 한계:** 두 오류는 generator context manager와 실제 run lock에서 원본 객체·구조
필드·traceback과 원인 연쇄를 유지한다. 오류 전달 신뢰성만 검증했으며 자율 복구율이나 모델
품질이 향상됐다고 주장하지 않는다. 실제 coding agent·학습·평가·final 소비를 실행하지 않았고
기존 #60/#69/#71 증거도 수정하지 않았다. 전체 Research Harness는 CI와 같은 xdist 4 worker와
짧은 basetemp에서 1,302 passed, 13 skipped / 228.42초였다. 구현 비참여 독립 리뷰는
P0/P1/P2/P3 모두 0건이었고 관련 회귀 260 passed, 5 skipped를 재확인했다. PR #91의 Linux
Python 3.11/3.12, Feast/Postgres, lock drift, Ruff와 선택 이미지 CI도 통과했다.

## 19. 유효 후보가 없는 실행에서 final을 보존하기 — #92

**문제:** #73·#74·#76·#77·#79 반영 뒤 #71을 seed 7102와 baseline `0680e16`에서
재실행했다. 새 fixture·validation/final ID와 입력 view preflight, baseline seed 101~105의
7개 양수 sigma calibration은 통과했다. 그러나 coding trial 2회는 모두 prepare에서 끝났다.
첫 agent는 `mean_topic_similarity` 구현 뒤 표적 112 tests와 Ruff를 통과했지만 420.063초
timeout 전에 commit을 남기지 못했고 temp cleanup도 실패했다. 두 번째 agent는 제한된 PATH에서
테스트 interpreter를 실행하지 못해 `agent_blocked`를 반환했다. 따라서 candidate SHA,
validation metric, 공식 22열 학습은 모두 0건이었다.

기존 Controller는 이 상태에서도 final을 claim해 baseline `0680e16`을 양쪽 역할로 5 pair,
10 fit 평가했다. 모든 delta는 0이었고 `discard / primary_threshold_not_met`가 기록됐다. 이는
피처 품질의 증거가 아니며 새 single-use final만 소비한 결과다. 기록 Judge도 candidate가 구현·
학습·평가되지 않았고 final이 동일 baseline 비교라고 concerns에 남겼다. 관측 실행 7건의 합은
1,025,464ms였다. 두 coding 호출 중 token receipt가 남은 1건은 input 679,859, cached 604,160,
output 4,670, reasoning 1,656이며 달러 비용과 사람 개입 횟수는 측정하지 못했다.

**해결:** validation 종료 시 candidate SHA·metric이 있고 실행 실패가 없는 record가 최소 1건인지
검사한다. 없으면 final registry와 runner를 호출하지 않고
`inconclusive / no_valid_validation_candidate`를 반환한다. validation 실패 record와 checkpoint는
그대로 보존해 같은 ledger 재개가 agent·final을 반복하지 않으며, REPORT terminal 검증도 이
명시적 no-final 사유를 허용한다. 한 trial 실패 뒤 다음 candidate가 screening을 완료한 흐름과
유효 candidate의 final 단일 소비는 바꾸지 않았다.

**결과와 한계:** 변경 전 RED는 전부 prepare 실패와 validation 0건 두 시나리오에서 final claim을
호출해 2 failed, 10 passed였다. GREEN 뒤 Controller·REPORT·runtime 73 tests와 전체 Ruff,
`git diff --check`가 통과했다. Windows 저장소 전체 검증은 `/bin/sh` 부재 등 기존 POSIX 전제
실패가 나타나 28%에서 중단했고, Research Harness 전체 검증도 기존 260자 fixture 경로의 raw
`Path.read_bytes()` 실패를 확인한 뒤 중단했다. 이 두 실패는 #92 변경 파일 밖의 기존 로컬 환경
한계이며 Linux CI 결과와 구분한다. 이 수정은 future final을 보존하지만 이미 소비된 #71 final을
복구하거나 이번 실행의 22열 피처 효과를 입증하지 않는다.

구현 비참여 첫 리뷰는 P1 1건과 P2 1건을 찾았다. P1은 no-final reason이 실제 ledger의 유효
candidate 0건 조건과 결속되지 않아 거짓 terminal을 봉인할 수 있다는 문제였다. REPORT 검증이
Controller와 같은 predicate를 사용하도록 연결하고, 유효 validation record에 해당 reason을
붙이면 거부하는 회귀를 추가했다. P2는 첫 trial 실패 뒤 두 번째 candidate가 성공했을 때 final
5회와 candidate SHA를 직접 단언하지 않은 테스트 공백이었다. 두 단언을 추가한 뒤 위 73 tests를
재실행했다. 독립 재리뷰는 P0/P1/P2/P3 0건이었고 Controller·REPORT·context 83 tests와 변경
Python 파일 Ruff, `git diff --check`를 다시 확인했다.
PR #93의 Linux Python 3.11/3.12, Feast/Postgres, Ruff, lock drift와 선택 Docker 이미지 CI도
모두 통과했다.

## 20. 이미 사라진 빈 agent temp anchor를 안전하게 회수하기 — #94

**문제:** #71 seed 7102 재실행의 첫 coding agent는 피처 구현과 표적 112 tests, Ruff를
완료했지만 제한 시간 전에 응답과 commit을 남기지 못했다. process 회수 뒤 등록된
`harness_out/.agent-tmp`가 존재하지 않아 cleanup preflight가 `FileNotFoundError`로 끝났고,
원래 timeout과 별도로 `workspace_cleanup_failed`가 기록됐다. Agent command 기록에는 해당
anchor를 삭제한 명령이 없었다. 임시 디렉터리 사용 주체가 빈 root까지 제거할 수 있는데도 기존
계약은 anchor 자체의 identity가 계속 존재해야만 회수를 성공으로 인정했다.

**해결:** 등록된 `.agent-tmp` 보안 경계 아래 disposable `runtime` 자식을 만들고 agent child의
TEMP/TMP/TMPDIR/PYTEST_DEBUG_TEMPROOT만 그 경로로 지정한다. 임시 도구가 빈 runtime을 제거하면
anchor는 남으므로 helper와 host가 등록 identity와 부모 경계를 계속 검증하면서 잔여물 0건으로
처리할 수 있다. Anchor 부재·교체와 부모 경계 변경, hardlink·reparse·object limit 검사는
기존처럼 실패한다.

**결과와 한계:** 최초 RED는 clean, sandbox helper, lifecycle host verification 세 층에서
3 failed, 기존 회귀 74 passed였다. 첫 구현은 anchor 부재를 직접 허용했지만 독립 리뷰가 내용이
든 anchor를 rename해 회수를 우회하는 P1을 재현했다. 이를 폐기하고 보안 anchor와 disposable
runtime을 분리했으며, non-empty anchor rename은 실패하고 이동된 파일을 삭제하지 않는 회귀로
고정했다. 최종 temp/coding 회귀는 78 passed, runner/workspace 회귀는 61 passed, 1 skipped였고
전체 Ruff와 `git diff --check`도 통과했다. 수정 재리뷰는 P0/P1/P2/P3 모두 0건이었다.
ACL·소유권 변경이나 동시 악성 inode 교체를 새로 방어하지 않는다. #71의 기존 실패 evidence와
소비된 final은 수정하지 않았고, 피처의 공식 학습·평가 성능도 아직 입증하지 않았다.
PR #95의 Linux Python 3.11/3.12, Feast/Postgres, Ruff, lock drift와 선택 Docker 이미지 CI도
모두 통과했다.
