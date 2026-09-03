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
표현하지 않았습니다. 범용 Windows pytest 임시 경로의 ACL·회수 제약은
[#54](https://github.com/bbungjun/Autoresearch/issues/54)에서 여전히 추적합니다.

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
- #54의 Windows 임시 경로 ACL·회수 제약은 미해결입니다. 현재의 좁은 coding 테스트 범위를
  범용 sandbox 테스트 지원 완료로 표현하지 않습니다.
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
