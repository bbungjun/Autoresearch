# 로컬 Research Harness MVP — 독립 실행 경로 Implementation Plan

> **상태: #769 이슈·브랜치 생성, 기반 MVP 범위와 확정 결정 승인 완료.** 구현은 아래
> Task 순서와 검증·동료 리뷰 규칙을 따른다.

**Goal:** 현행 executor를 수정하지 않고 정적 allowlist를 사용하지 않는 독립 로컬 Research
Harness(봉인된 사후 판정 + 자가 피드백) 경로를 만든다. 사람이 준 가설·`ExperimentCard`로
에이전트가 저장소를 자유롭게 바꾸며 실험하고, 봉인된 Judge가 CTR 예측 품질을 판정해
그 결과를 다시 에이전트에게 돌려주는 반복 환경을 MVP로 완주한다. 현행 executor 배선과
`verifier` 삭제는 이 경로가 검증된 뒤의 후속 범위다.

**Architecture:** 에이전트가 **무엇을 고쳤는지 검사하지 않고, 무엇을 냈는지만
계약한다.** candidate는 disposable worktree에서 저장소 전체를 자유롭게 수정한 뒤
고정 진입점 하나로 예측 점수 파일을 산출한다. 봉인된 slate·정답 라벨·지표 구현·ledger는
worktree 바깥의 Judge 소유 디렉터리와 별도 프로세스에 둔다. 이 분리는 candidate가
자기 evaluator를 고쳐 점수를 바꾸는 **실수와 자기 채점 오염**을 막지만, 같은 OS 사용자의
적대적 subprocess를 차단하는 sandbox는 아니다. 따라서 라벨 경로 비전달과 예측 artifact
복사 hardening을 적용하고, 완전 격리는 후속 단계로 명시한다.

**Tech Stack:** Python 3.11/3.12, uv, pytest, ruff, pandas/pyarrow, typer

**Spec:** [`docs/specs/2026-08-14-paper-grounded-autonomous-ml-research-harness.md`](../specs/2026-08-14-paper-grounded-autonomous-ml-research-harness.md)
(#769 선행 커밋에 반영됨)

**Issue:** #769

---

## MVP와 이후 로드맵의 관계

MVP의 정본은 spec 11·12장이고, 이 plan이 그 MVP 전체의 구현 순서를 소유한다. 범위는
사람이 준 가설·`ExperimentCard` → 전체 저장소 수정 → 반복 실험 → 봉인 평가 → 복구·재개 →
Trial Ledger와 MVP REPORT까지다.

Task 5b가 받는 사람이 작성한 가설과 `ExperimentCard`는 Controller·피드백 seam을
검증하는 MVP 입력이다. 다음 단계에서는 Paper Discovery/Capability Matcher가 만든
`ExperimentCard`가 같은 seam으로 들어온다.

OpenAlex/arXiv/Crossref 자동 발견, PaperCard·compiler·출처 provenance, 논문이 연결된 9절
REPORT, 기존 웹 request·budget·REPORT 배선은 MVP에서 버리는 항목이 아니라 이 plan 완료
후 이어질 제품 로드맵이다.

---

## 확정 결정

이 계획과 spec의 공통 전제다. D1~D8은 승인된 확정 결정이며 구현 Task가 임의로 뒤집지 않는다.
2026-09-03 승인으로 Task 6의 candidate 입력에 안전한 metadata를 추가한다(spec 4.5절).

| # | 결정 | 근거 |
| --- | --- | --- |
| D1 | **봉인 경계는 예측 점수 파일**이다. candidate가 `predictions.csv`를 산출하고, Judge는 숨긴 정답으로 채점만 한다. | Judge가 candidate 코드를 일절 실행하지 않으므로 봉인이 가장 깨끗하다. 모델 파일 역직렬화 위험도 없다. |
| D2 | **로컬 harness를 신규로 만든다.** 현행 K8s executor는 손대지 않고, 이후 `ExperimentRunner` 구현체로 흡수한다. | spec 4.4의 local-first. 지금 도는 폐루프를 깨지 않는다. |
| D3 | **verifier의 연구 공간 제한(path allowlist, dependency 금지, 변경량 상한)을 폐기한다.** candidate checkout 밖의 고정 Judge 판정 경계로 대체한다. | spec 4.2. 금지 목록은 봉인이 아니라 "약속"이며, 자율 시스템에서 약속은 보증이 아니다. 같은 UID에서의 완전한 기밀성·무결성 격리를 뜻하지 않는다. |
| D4 | **시크릿·자격증명 커밋 차단만 남긴다.** symlink·submodule·파일 크기·생성 데이터(`.csv/.pkl/.parquet`) 제한은 폐기한다. | 현행 `.csv/.parquet` 거부는 spec 4.2가 명시 허용한 "raw 데이터 재조립과 파생 데이터셋"을 그대로 막는다. 파생 데이터는 커밋이 아니라 workspace 산출물로 다루면 위생과 자유가 동시에 성립한다. |
| D5 | **판정 임계값은 고정 %가 아니라 baseline seed 노이즈 σ의 배수로 정의한다.** (구 M1) | 아래 "판정 규칙" 참조. |
| D6 | **`slate_id`를 action log 생성 단계에서 부여한다.** 사후 추론하지 않는다. 과거 파티션은 평가 대상에서 제외한다. (구 M2) | slate 경계는 노출 시점에만 존재하는 사실이고, 사후 추론은 언제나 근사다. NDCG는 slate 단위로 계산되므로 경계가 틀리면 지표가 **조용히** 틀리고 그 위의 모든 자율 판정이 함께 틀린다. 두 정의를 섞으면 D5의 σ 측정이 오염된다. |
| D7 | **평가 데이터는 `slate_id` 도입 이후 생성분부터 사용한다.** 개발·검증용으로는 `RuleBasedActionLogGenerator`로 로컬 생성한다. (구 M3) | 로컬에 action log parquet 스냅샷이 없음을 확인했다. rule-based 생성기는 LLM·API 키 없이 동작하므로 로컬 완주 원칙(D2)과 맞는다. |
| D8 | **KST 날짜 cutoff `T`로 candidate action log 접근을 제한한다.** action log는 `dt < T`만 두고 validation/final slate는 `dt >= T`에서 만든다. Task 6은 spec 4.5절에 따라 시점이 검증된 metadata와 임베딩 재료를 추가한다. 원격 데이터 자격 증명은 주입하지 않는다. | 평가 action log로 정답을 복원하는 경로는 계속 닫고 기존 피처 조립용 설명 데이터만 추가한다. 시간 cutoff와 유저 80/20을 유지한다. |

### D1이 만드는 인터페이스

candidate가 지켜야 하는 계약은 **단 하나**다.

```text
입력  <workspace>/harness_in/slate.parquet                   라벨 없는 평가 slate
      <workspace>/harness_in/history/action_log/dt=...        dt < T 로컬 파티션만
출력  <workspace>/harness_out/predictions.csv                evaluation_id, slate_id, video_id, score
진입점 python -m autoresearch.cli harness-predict --slate <in> --out <out> --seed <n>
```

위 파일은 v1 기준이다. Task 6의 metadata 파일 및 v2 manifest 목표는 evaluation snapshot
spec §18을 따르며, 기존 v1의 파일 집합은 변경하지 않는다.

이 명령이 동작하는 한 나머지는 전부 자유다 — 피처 재조립, 모델 교체, 의존성 추가,
디렉터리 구조 변경, 학습 코드 재작성 모두 허용된다. 진입점 계약이 곧 allowlist의
대체물이다.

candidate는 주어진 seed로 split·sampling·모델 초기화를 포함한 학습 파이프라인을 실행한
뒤 예측을 산출해야 한다. 고정 모델을 seed마다 다시 점수화만 하거나 seed를 무시하는 구현은
계약 위반이다.

`score`는 `[0,1]` click 확률 추정치다. 범위 검사만으로 보정 여부를 증명하지 않으며 실제
보정 품질은 LogLoss·Brier guardrail이 감시한다. 범위 밖·NaN·Inf는
`invalid_predictions` 실행 실패이고, ranking은 score 내림차순(`video_id` 오름차순
tie-break)으로 계산한다.

### 평가 slate 2단계 계약

snapshot manifest에 평가 출력일 `[T, T_end]` KST 파티션 경계를 봉인한다. click과 귀속 후보
impression의 스캔은 `dt BETWEEN T AND T_end + 1`, slate·labels 출력은 impression `dt`가
`[T, T_end]`인 행으로 분리한다. `T_end + 1` 파티션이 없으면 snapshot을 fail-closed한다.
candidate가 볼 수 있는 action log는 `dt < T`이고, 이 범위에서 완전한 라벨을 만들 수 있는
마지막 출력일은 `T-2`다. 그 평가 출력 구간을 반복 피드백으로 같은 holdout에 적응하지
못하게 다시 유저 단위로 나눈다.

- `bucket = int(sha256("research-harness-slate-v1:" + user_id).hexdigest()[:8], 16) % 10`
- bucket `0..7`은 `validation slate`, `8..9`는 `final holdout`이다. 같은 유저의 모든
  slate는 한쪽에만 속한다. slate 단위 분할은 같은 유저의 선호가 양쪽에 섞이므로 쓰지 않는다.
- 반복 trial·decision·feedback은 validation만 사용한다.
- final holdout의 label-free slate도 반복 중 candidate에 주지 않는다. 예산 종료 후 고정된
  champion과 baseline을 마지막 1회만 평가하고, 결과를 에이전트에 돌려주지 않은 채
  Controller를 종료한다. 두 번째 평가는 온전한 같은 Judge 상태 루트의 `evaluation_id`
  소비 registry가 거부한다.
- 최종 REPORT의 대표 수치는 validation 최고값이 아니라 final holdout 값이다. 마지막 평가가
  실패하면 재평가하지 않고 `판정 불가`를 기록한다.

유저 단위 분할 뒤에도 같은 영상의 인기도·시간대·카테고리 같은 공통 요인은 두 split에
공유된다. 시간 cutoff는 정답 자체의 접근을 막지만 이 분포 수준 정보 공유까지 제거하지
않으므로 validation과 final은 완전히 독립적이지 않다.

---

## Global Constraints

- **봉인 규칙(가장 중요).** Judge·원본 평가 slate·라벨·ledger는 candidate worktree **바깥**에
  둔다. Judge 프로세스는 **고정 SHA의 harness 체크아웃**에서 실행하며, `cwd`나
  `sys.path`가 candidate worktree를 가리켜서는 안 된다.

  현행 코드가 정확히 이 지점에서 실패한다 —
  `applications/experiment_platform/executor/measurement.py:192-205`가
  `cwd=config.workspace`로 평가를 돌리고, 주석이 "executor 이미지가 아니라
  workspace에서 돈다(#754)"고 명시한다. 이 계획의 모든 코드는 그 반대여야 한다.
  Task마다 검증 항목에 "candidate 코드가 Judge 경로에서 import되지 않음"을 넣는다.
- **MVP 위협 모델.** 방어 대상은 실수와 자기 채점 오염이다. 같은 OS 사용자의 일반
  subprocess가 절대 경로를 추측하면 Judge 파일을 읽을 뿐 아니라 수정·삭제할 수도 있다.
  디렉터리 분리는 기밀성·무결성 sandbox가 아니며, 별도 OS 사용자, container,
  read-only mount는 후속 완전 격리 범위다. 평가 fixture를 만드는
  `RuleBasedActionLogGenerator`의 입력·seed와 final 소비 registry도 같은 한계를 가진다.
- **라벨은 candidate에게 어떤 형태로도 노출하지 않는다.** slate 주입 파일에 `clicked`
  컬럼이 없어야 하고 라벨 파일 경로를 candidate의 argv·환경·prompt에 넣지 않는다.
  validation feedback에는 집계 지표만 넣고, final holdout 결과는 집계값도 돌려주지 않는다.
- **Judge 입력 hardening.** candidate의 `predictions.csv`를 `O_RDONLY|O_NOFOLLOW`로 열고
  검증한 같은 FD에서 `65 MiB + 1 byte probe`까지만 복사한다. 경로를 다시 열지 않고, 복사
  전후 `fstat`으로 교체·성장을 검출하며, Judge 목적지는 `O_CREAT|O_EXCL`로 만든다.
  Judge는 candidate 경로가 아니라 이 사본만 읽는다. parser는 정확히 4개 필드, 대상
  slate와 같은 행 수를 강제한다. evaluation ID 69 byte, slate/video ID 각 64 byte,
  score token 24 byte, comma 3개와 CRLF 2 byte를 합친 최악 행 길이 226 byte와 header
  39 byte를 기준으로 최대 **300,000행**만 허용한다.
  `39 + 300,000 * 226 = 67,800,039 byte`이므로 65 MiB와
  모순되지 않는다. parser는 10초·256 MiB 상한도 강제하고, Task 7에서 행·시간·메모리
  초기값을 실측해 함께 재조정한다. 이 상한은 workspace/commit 파일 제한을 폐기한 D4와
  충돌하지 않는 inter-process artifact 계약이다.
- **데이터 자격 증명 제거.** candidate 환경 allowlist에 GCS·BigQuery 자격 증명을 넣지
  않는다. action log는 Harness가 주입한 `dt < T` 파일만, metadata·임베딩 재료는 spec 4.5절의
  로컬 입력만 제공한다. D3은 코드 수정 범위를 여는
  결정이고 이 규칙은 평가 데이터 접근을 닫는 결정이라 충돌하지 않는다.
- **Judge 상태 루트.** `harness-run --judge-state-root <absolute-path>`를 필수 설정으로 받고,
  final 소비 marker는 그 고정 절대 경로 아래에 둔다. 경로가 상대 경로이거나 상태 루트가
  없거나 접근 불가하면 final 평가를 시작하지 않는다. Harness가 임시 경로를 만들거나
  fallback하지 않는다.
- **기존 실행 경로를 건드리지 않는다.** `applications/experiment_platform/**`는 이 계획의
  변경 대상이 아니다. verifier 삭제는 로컬 harness가 검증된 뒤 별도 이슈로 한다.
- **지표 정의를 새로 만들지 않는다.** LogLoss·Brier·PR-AUC·grouped ROC-AUC는
  `autoresearch/model_evaluation/evaluate.py`의 기존 구현을 재사용한다. NDCG·Recall만
  신규다.
- **새 최상위 패키지를 만든다.** `autoresearch/research_harness/` 도입은 CLAUDE.md에
  따라 **같은 PR에서** `README.md`와 `.claude/docs/agent-project-reference.md`를
  갱신해야 한다(Task 6).
- **모듈 docstring 규칙.** 새 모듈마다 전체 파이프라인 기준 담당 구간(및 담당하지 않는
  인접 책임)과 제공 기능을 최상단 docstring에 적는다.
- 커밋 메시지는 `<type>: 한국어 설명` + 본문 + `Refs #<issue>` +
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.

---

## 작업 진행 방식

**이슈는 Task 1개당 1개씩 발행한다.** 여러 Task를 한 이슈에 묶지 않는다. 이슈는 착수
직전에 만들며, 전체 backlog를 미리 발행하지 않는다 — 앞선 Task의 결과가 뒤 Task의 범위를
바꾸는 경우가 잦기 때문이다. Task 목록의 정본은 이 plan이다.

**worker에게 한 번에 Task 1개만 준다.** "다음 것도 이어서 해라"라고 지시하지 않는다.
한 Task가 승인되어 머지된 뒤 다음 이슈를 만든다.

### Task 1건의 진행 순서

```text
1. 이슈 발행        gh issue create --title "[FEAT] ..." --label feature --assignee @me
2. 브랜치 생성      이슈의 Development > Create a branch (gh issue develop, main 기준)
3. 구현             Codex worker — 전용 worktree에서 해당 브랜치 체크아웃
4. 동료 리뷰        별도 Codex worker — 구현자의 추론 맥락 없이 diff만 보고 적대적 리뷰
5. 종합·판정        코디네이터가 구현 결과 + 리뷰 결과를 대조해 승인 또는 수정 재투입
6. PR               Closes #<이슈번호>, 팀원 1명 이상 Approve 후 Squash Merge
```

### 동료 리뷰 규칙 (4단계)

**리뷰어는 구현자와 다른 worker여야 하고, 구현자의 사고 과정을 보지 않는다.** 구현한
에이전트가 자기 코드를 리뷰하면 같은 전제 위에서 같은 결론에 도달한다 — 놓친 것을 다시
놓친다. 리뷰어에게는 다음만 준다.

- 해당 이슈 본문과 이 plan의 Task 항목 (= 무엇을 했어야 하는가)
- `git diff`와 테스트 실행 결과 (= 무엇을 했는가)

리뷰어에게 주지 않는 것: 구현자의 worker 로그, 구현 중 판단 근거, "왜 이렇게 했는지"에
대한 설명. 리뷰어는 **결과물만 보고 독립적으로 판단**해야 한다.

리뷰어의 임무는 승인이 아니라 **반증**이다. 다음을 우선순위대로 확인한다.

1. 봉인 규칙 위반 — Judge 경로가 candidate workspace를 참조하는가
2. 라벨 누출 — 정답이 candidate나 피드백 payload로 흘러가는가
3. Task 범위를 벗어난 변경 — 요청하지 않은 리팩터링이 섞였는가
4. 계약 위반 — 이 plan의 결정(D1~D8)과 어긋나는가
5. 테스트가 실제 실패를 잡는가 — 통과하는 테스트가 무엇을 보장하는지

리뷰어와 구현자의 판단이 갈리면 코디네이터가 코드를 직접 열어 판정한다. **다수결로 정하지
않는다.**

---

## 판정 규칙 (D5 상세)

spec 7장과 이 plan은 얼마나 좋아져야 promote인지 아래 지표별 규칙으로 함께 고정한다.
고정 %를 쓰지 않는 이유는 **그 값이 잡음보다 큰지 알 수 없기 때문**이다. 같은 코드·같은
데이터로 seed만 바꿔도 `NDCG@10`은 흔들리며, 자율 루프가 수십 trial을 돌리면 **우연히
좋아 보이는 결과가 반드시 나온다.** 임계값이 잡음보다 낮으면
harness는 잡음을 champion으로 승격시키고 그 위에서 다음 실험을 이어가므로, 오류가 조용히
누적된다.

**σ 측정.** validation slate에서 baseline(Task 6의 Harness baseline 설정)을 seed마다 **독립적으로
5회 재학습**한 뒤 같은 slate를 점수화하고, `NDCG@10`뿐 아니라 `Recall@10`, `NDCG@24`,
grouped ROC-AUC, PR-AUC, LogLoss, Brier의
표준편차를 **각각** 구한다. 각 `σ_metric`은 해당 지표에서 "아무것도 바꾸지 않아도
흔들리는 폭"이다. 지표별 σ map을 ledger에 기록하고, 데이터 규모나 모델 구조가 크게
바뀌면 같은 5-seed sweep으로 전부 재측정한다.

**방향 정규화.** 모든 delta는 개선이 양수가 되게 만든다.

- NDCG·Recall·grouped ROC-AUC·PR-AUC: `candidate - baseline`
- LogLoss·Brier: `baseline - candidate`

**판정.**

아래 규칙 전에 모든 필수 지표의 `σ_metric > 1e-6`을 요구한다. `1e-6`은 단위 구간으로
정규화한 지표의 fail-closed 수치 해상도 초기값이며 Task 7에서 재조정한다. `σ ≤ 1e-6`이면
`insufficient_baseline_noise`로 판정 불가이며, 동률을 promote하지 않는다. metric 값이
`None`이면 `metric_unavailable`로 판정 불가다. 특히 현행 grouped ROC-AUC는 양성·음성을
모두 가진 그룹이 없으면 `None`을 반환한다
(`autoresearch/model_evaluation/evaluate.py:77-124`).

지표별 최소 coverage는 다음과 같다.

- NDCG@10·NDCG@24·Recall@10: click 보유 slate가 전체의 20% 이상이면서 최소 30개
- grouped ROC-AUC: 채점 가능 유저가 non-null 전체 유저의 20% 이상이면서 최소 30명
- PR-AUC·LogLoss·Brier: item 1:1 coverage 100%와 양성·음성 label 모두 존재

하나라도 미달하면 `insufficient_metric_coverage`로 판정 불가이며
`promote/revise/discard`를 내지 않는다.

| 판정 | 조건 |
| --- | --- |
| `promote` | `Δ_ndcg_at_10 ≥ 2σ_ndcg_at_10` **그리고** 모든 guardrail `Δ_metric ≥ -1σ_metric` |
| `revise` | primary 조건은 충족하지만 하나 이상의 guardrail이 자기 지표의 `-1σ_metric` 미만 |
| `discard` | 그 외 |

**임계값 근거.** `2σ`는 측정 전에 정한 실용적 출발값이다. seed 5개의 표본 표준편차는
모분산을 정확히 아는 경우가 아니므로 알려진 σ의 정규 꼬리확률이나 trial 30회의 우연 승격
기대값을 인용하지 않는다. Task 7에서 baseline noise와 promote/discard 경로를 실측한 뒤
primary·guardrail 배수와 coverage 하한을 재조정한다. guardrail을 `-1σ`로 더 민감하게 둔
것은 개선 측정보다 부작용 감지를 우선하는 초기 비대칭이며, 역시 실측 대상이다.

**통계적 유의성 검정은 MVP 범위 밖이다.** seed 5회는 σ 추정에는 쓰되 가설검정에는 부족하다.

**screening과 확인 실험.** screening은 고정 seed 하나를 같은 seed의 baseline과 비교해
primary가 좋아지고 유효성·coverage gate를 통과한 candidate만 확인 실험으로 보낸다.
screening 값은 champion을 승격시키지 않고 확인 실험 평균에도 섞지 않는다. 최종
`promote/revise/discard`는 baseline/candidate를 같은 5개 seed로 실행한 확인 실험의
seed별 paired normalized delta 평균으로만 확정한다.

**final 결론.** final holdout의 유효한 비교가 `promote`면 REPORT는 `개선`, `revise` 또는
`discard`면 baseline을 유지하고 `개선 없음`으로 쓴다. 실행·registry·prediction·metric·
coverage 문제로 유효한 비교 자체가 없을 때만 `판정 불가`로 쓴다.

**promote 경로 검증 의무.** MVP 종료 전 **일부러 더 나은 candidate**(예: 유효한 피처 1개
추가)로 promote가 실제로 발생하는지 1회 확인한다. 이를 하지 않으면 "한 번도 promote되지
않은" 상태가 임계값 때문인지 코드 결함 때문인지 구별할 수 없다.

---

## MVP 이후 로드맵

다음 항목은 제외가 아니라 MVP가 제공하는 `ExperimentCard` 입력 seam, Trial Ledger,
REPORT evidence 위에 이어질 후속 구현이다.

- OpenAlex/arXiv 논문 자동 발견과 Crossref 식별자 해소
- PaperCard·Capability Matcher·출처 provenance
- paper claim에서 검증 가능한 `ExperimentCard`를 만드는 compiler
- 최종 `research-report.html` 전체 9개 섹션과 paper manifest 교차검증
- 기존 workbench의 자연어 research request·budget 제출과 최종 REPORT 조회 배선

다음은 MVP 밖의 별도 이슈 또는 필요 실측 뒤 재검토 범위다.

- `applications/experiment_platform/executor/verifier.py` 삭제 (별도 이슈)
- KubernetesJobRunner 연결 (spec 15장 로드맵)
- 별도 OS 사용자·container·read-only mount를 이용한 적대적 candidate 완전 격리
- `training_entity` SQL 변경 — **불필요하다.** Judge는 slate 식별자와 라벨만 필요하고
  피처는 candidate가 스스로 조립하므로, 학습 데이터 계약을 건드리지 않는다.

---

## 기존 코드 재사용 지도

기존 자산을 복제하지 않고 아래 경계 위에 얹는다. `새로 쓰는 것`은 해당 Task의 실제
증분이며, 경로가 비슷하다는 이유만으로 기존 Kubernetes executor를 수정하지 않는다.

| Task | 기존 경로 | 재사용하는 것 | 새로 쓰는 것 |
| --- | --- | --- | --- |
| Task 1-0 | `autoresearch/action_log_generation/schema.py:37-61`, `autoresearch/action_log_generation/pipeline.py:859-939`, `autoresearch/action_log_generation/daily.py:847-880`, `autoresearch/action_log_generation/llm_generator.py:149-171` | EventLog 계약, 유저별 후보 묶음 확장, 일일 1-day 생성 설정, API key 없는 결정적 fixture 생성 | 원천 `slate_id` 필드와 묶음별 결정적 ID 부여 |
| Task 1 | `autoresearch/jobs/feature_store_build.py:295-370`, `autoresearch/model_training/training_provenance.py:38-45,96-180`, `autoresearch/model_training/training_snapshot_store.py:147-205` | 30분 click 귀속, immutable manifest, content-addressed write-once 게시 패턴 | label-free slate/봉인 labels 분리와 evaluation manifest |
| Task 2a | `autoresearch/model_evaluation/evaluate.py:55-124` | 그룹 단위 metric과 coverage를 함께 반환하는 순수 계산 패턴 | 결정적 NDCG@k·Recall@k와 0-click coverage 규칙 |
| Task 2b | `autoresearch/model_evaluation/evaluate.py:55-124,366-427` | 기존 ROC-AUC/PR-AUC/LogLoss/Brier/grouped ROC-AUC 의미와 계산 primitive | labels/scores 순수 metric 추출, prediction schema 검증, ranking·probability metric 결합 |
| Task 2c | `autoresearch/model_evaluation/seed_sweep.py:139-248`, `autoresearch/model_training/training_provenance.py:96-180` | seed 평균·표준편차와 immutable provenance | sealed prediction ingestion, 지표별 σ 기반 deterministic Judge decision |
| Task 2d | `autoresearch/model_evaluation/evaluate.py:55-124,366-427`, `autoresearch/model_training/training_provenance.py:96-180` | YouTube 지표·coverage와 immutable snapshot/split/seed manifest 계약 | `ResearchDomain` ABC와 slate/Judge에 위임하는 `YouTubeCTRDomain` |
| Task 3 | `applications/experiment_platform/executor/workspace.py:153-251`, `applications/experiment_platform/executor/finalizer.py:411-501`, `applications/experiment_platform/executor/safety.py:21-38` | credential-free Git 준비, content fingerprint·검증 tree commit 패턴, credential 값 탐지 | 기준 SHA의 disposable local worktree, 입출력 디렉터리, 시크릿만 남긴 commit guard |
| Task 4 | `applications/experiment_platform/api/experiments/models.py:235-357`, `applications/experiment_platform/executor/results_store.py:91-145` | 멱등 event/log/Step 모델과 write-once 산출물 의미론 | append-only local Trial Ledger와 단계별 resume checkpoint |
| Task 5a | `applications/experiment_platform/executor/codex_worker.py:253-281,537-574,624-670` | bounded output tail, timeout 시 process-group 회수, `Popen`·timeout 처리 패턴 | candidate `harness-predict` 실행 결과를 `TrialResult`로 정규화하는 `LocalRunner` |
| Task 5b | `applications/experiment_platform/api/experiments/models.py:67-125`, `applications/experiment_platform/api/experiments/issue_authoring.py:80-112` | 상태 enum·전이와 구조화된 사전등록 입력 검증 패턴 | `ResearchDomain`을 통한 budget loop, validation 피드백, 이전 trial memory, 자동 실패 복구 Controller |
| Task 6 | `autoresearch/cli.py`, `applications/experiment_platform/workbench/views.py:173-180,240-260` | Typer command 배선과 현행 연구 입력 UI 계약 | `--seed`별 재학습 candidate CLI, run CLI와 새 패키지 문서 등록 |
| Task 7 | `applications/experiment_platform/executor/measurement.py:169-297`, `autoresearch/model_evaluation/seed_sweep.py:217-248`, `autoresearch/model_training/training_provenance.py:163-180` | 동일 seed 조건 평가, 평균·표준편차, split/seed manifest | Judge 지표별 baseline noise 등록과 로컬 end-to-end 증거 |

### MVP 이후 로드맵의 재사용 출발점

| 후속 범위 | 기존 경로 | 재사용/확장 방향 |
| --- | --- | --- |
| Paper Discovery | `autoresearch/data_collection/client.py:132-186,222-318,319-365,368-393` | paper 전용 구현은 신규지만 HTTP 오류 분류·backoff·rate-limit·key 회전·호출 예산 패턴을 재사용 |
| PaperCard·ExperimentCard | `autoresearch/model_training/training_provenance.py:38-45,96-180`, `applications/experiment_platform/api/experiments/issue_authoring.py:80-112,183-220` | immutable provenance 모델과 사전등록 검증 위에 paper claim·falsification 구조를 확장 |
| 최종 REPORT | `applications/experiment_platform/executor/report.py:126-159,197-230`, `applications/experiment_platform/executor/prompt.py:77-85,410-471`, `applications/experiment_platform/workbench/report.py:29-42,70-129`, `autoresearch/reporting/report_html.py:86-149` | 실제 diff·metrics 기반 작성, 절 검사, safe HTML shell과 자기완결 renderer를 multi-trial·paper lineage로 확장 |
| 웹 연결 | `applications/experiment_platform/api/experiments/router.py:81-106,320-333`, `applications/experiment_platform/workbench/views.py:337-410,613-633` | 생성·목록·상세·report 조회 흐름에 request budget과 research report 모델만 추가 |

---

## File Structure

| 새 위치 | 책임 |
| --- | --- |
| `autoresearch/research_harness/__init__.py` | 공개 타입 재수출 |
| `autoresearch/research_harness/slate.py` | 평가 slate 조립·라벨 봉인·fingerprint |
| `autoresearch/research_harness/domain.py` | `ResearchDomain` ABC와 `YouTubeCTRDomain` adapter |
| `autoresearch/research_harness/judge.py` | 예측 점수 채점, 리랭킹 지표, Decision 산출 |
| `autoresearch/research_harness/ranking_metrics.py` | NDCG@k, Recall@k 순수 계산 |
| `autoresearch/research_harness/workspace.py` | disposable worktree 생성·주입·회수 |
| `autoresearch/research_harness/ledger.py` | Trial Ledger(JSONL) append·재개 |
| `autoresearch/research_harness/consumption_registry.py` | evaluation_id 전역 final holdout 소비 marker |
| `autoresearch/research_harness/feedback.py` | 자가 피드백 payload 조립 |
| `autoresearch/research_harness/runner.py` | LocalRunner — candidate 실행 |
| `autoresearch/research_harness/controller.py` | 예산·반복 루프·checkpoint |
| `autoresearch/research_harness/report.py` | ledger·final evidence 기반 MVP REPORT 생성 |
| `tests/research_harness/` | 위 각 모듈의 테스트 |

---

## Task 0: spec에 확정 결정 반영 후 커밋 (완료)

대상 spec과 이 plan은 #769 브랜치의 선행 커밋에 이미 반영되어 추적 중이다.

- [x] spec 4.3에 **artifact 정의** 추가 — "재평가 대상은 candidate가 산출한 예측 점수
      파일이며, Judge는 candidate 코드를 실행하지 않는다"
- [x] spec 4.2에 **연구 공간 제한과 안전 제한의 분리** 명시 — allowlist는 폐기하되
      시크릿 커밋 차단은 유지
- [x] spec 11장 기반 MVP 범위에 현행 executor와 분리된 allowlist 없는 로컬 경로를 포함
- [x] spec 15장에 실행 위치 결정(D2) 반영
- [x] D5(지표별 σ 기반 판정 규칙)를 spec 7장 `compare()` 설명에 반영
- [x] D6(`slate_id` 생성 시점 부여)를 spec 8장 `EvaluationSlateItem`에 반영
- [x] 이슈 #769의 브랜치에서 spec + 이 plan 커밋

**검증:** spec과 plan이 Git 추적 대상이며 확정 결정 표와 서로 일치한다.

---

## Task 1-0: action log 생성 단계에 `slate_id` 부여 (D6)

Task 1의 선행 작업이다. **이 저장소의 데이터 계약을 바꾸는 유일한 작업**이므로 범위를
좁게 유지한다. 정확한 ID 생성식, producer namespace, cutover와 호환성 계약은
`docs/specs/2026-08-31-research-harness-evaluation-snapshot.md` §5가 정본이다.

- [x] `autoresearch/action_log_generation/schema.py`에 `slate_id`를 **optional 컬럼**으로
      추가 (하위 호환 유지 — 기존 파티션은 null)
- [x] 유저별 후보 묶음을 만드는 지점에서 slate 단위로 ID를 부여한다. `daily.py`가
      `history_days=1`, `max_events_per_user_per_day=candidates_per_user`로 하루치 묶음을
      만드는 경로가 시작점이다
- [x] ID는 `slt_<YYYYMMDD>_<24 lowercase hex>` 형식과
      `action-log-slate-v1` canonical identity를 사용한다. 일일 producer, user와 정렬된
      candidate member identity를 포함하고 worker·shard·event sequence는 제외한다.
      현행 30일 policy simulation은 slate 의미가 다르므로 P0-1 평가 원천에서 제외한다
- [x] **`docs/specs/2026-07-24-action-log-slice-semantics.md`의 파티션 계약에 영향이 없는지
      먼저 확인한다.** `dt=D`가 KST 하루치 서로소 슬라이스라는 계약과 slate 경계가
      충돌하면 spec 갱신을 먼저 제안한다
- [ ] slate 빌더는 필수 인자 `slate_id_cutover_date`를 받아 **파티션 선택 단계에서**
      `dt < slate_id_cutover_date`를 제외한다. 운영값은 rollout 후 `slate_id`가 전 행에
      채워진 첫 파티션 날짜이고, 로컬 fixture는 생성 요청의 `partition_date`다
- [ ] 선택된 `dt >= slate_id_cutover_date` 파티션에서 `slate_id` null이 한 행이라도 나오면
      오류로 거부한다. 과거 파티션 제외와 선택된 새 파티션 fail-closed를 같은 규칙으로
      섞지 않고, fallback 추론도 넣지 않는다
- [x] 테스트: 같은 노출 묶음의 행이 같은 `slate_id`를 갖고 다른 묶음과 겹치지 않음,
      기존 파티션 읽기가 깨지지 않음

**검증:** `uv run python -m pytest tests/action_log_generation/ -v`

> Stage A 구현 기록(2026-08-31): direct·1-shard·2-shard merge와 legacy/policy null
> 경계를 검증했다. Stage A 당시 snapshot builder와 cutover·label validation은 Stage B로
> 이관되었다. 이관 항목은 아래 Stage B checklist에서 현재 완료로 기록한다. 다만 위 Stage A
> checklist는 소급해 완료로 표시하지 않습니다. 이 문장은 Stage A 완료 시점 기록이며,
> 현재 P0-1과 Stage C는 아래 Task 1-C에서 완료됐습니다.

---

## Task 1: `EvaluationSlateSnapshot` 빌더

action log parquet에서 평가 slate를 조립하고 정답을 분리 봉인한다. 정확한 timestamp
경계, parquet schema, `evaluation_id`·snapshot fingerprint, manifest와 write-once 계약은
`docs/specs/2026-08-31-research-harness-evaluation-snapshot.md` §6~§12가 정본이다. 특히
Stage B Task 0은 §10~§12의 exact `EvaluationIdPayload`, canonical JSON bytes, writer
identity, typed nested manifest와 fingerprint exclusion을 잠근다.

### Task 1 및 P0-1 완료, 후속 Research Harness MVP 진행 중

이 계획에서 Stage A producer 계약, Stage B snapshot builder와 Stage C data-only 경계 및
독립 two-root 최종 실증이 완료되어 Task 1과 P0-1을 완료 처리합니다. Task 2 이후 지표,
실제 worktree·subprocess, final consumption과 전체 Research Harness MVP는 완료 처리하지 않습니다.

- [x] `slate.py` 작성 (Stage B builder/public facade 범위). 입력은 action log parquet 경로(로컬/GCS), 평가 **출력일** 범위
      `[T, T_end]`, 필수 `slate_id_cutover_date`. `T`는 첫 출력일이고
      `slate_id_cutover_date <= T`여야 한다
- [x] impression 행에서 slate 조립: `slate_id`(**Task 1-0의 원천 컬럼을 그대로 사용.
      추론하지 않는다**), `user_id`, `video_id`, `event_timestamp`,
      optional `original_rank`(원천 `rank`), optional `candidate_source`(원천 `exposure_source`)
- [x] 파티션 선택을 통과한 `dt >= slate_id_cutover_date` 행에서 `slate_id`가 null이면
      **오류로 거부**한다. 조용히 건너뛰거나 추론으로 채우지 않는다 (D6)
- [x] 개발·검증용 입력은 `RuleBasedActionLogGenerator`로 로컬 생성한다 (D7 — LLM·API 키
      불필요). 평가 구간을 생성한 입력과 seed는 Judge 소유 경로에만 두고 Stage C
      `CandidateDataView`에 넣지 않는다. 실제 workspace·argv·환경 검증은 Task 3이 소유한다
- [x] click과 귀속 후보 impression은 **`dt BETWEEN T AND T_end + 1`**로 스캔하고,
      slate·labels 출력은 impression `dt`가 **`[T, T_end]`**인 행으로 제한한다.
      `T_end + 1` 파티션이 없거나 읽을 수 없으면 snapshot 생성을 fail-closed한다
- [x] click 귀속으로 `clicked`를 산출한다. **귀속 규칙(직전 30분, 같은
      `(user_id, video_id)`의 전역 최근 impression 1건)은
      `docs/specs/2026-07-26-training-entity-incremental-slice.md:68-100`과
      `autoresearch/jobs/feature_store_build.py:295-370`의 기존 계약과 동일해야 한다.** 상수를
      공유하거나, 불가능하면 후보·출력 범위를 포함해 동일 규칙임을 테스트로 고정한다
- [x] raw action log 선택을 시간으로 분리한다. candidate history는 설정한 history 시작일부터
      **`dt < T`까지만** 허용한다. 평가 출력은 `[T, T_end]`, 라벨 스캔은
      `[T, T_end + 1]`만 허용한다. 현행
      action log가 click을 저장하고(`autoresearch/action_log_generation/schema.py:37-61`)
      현행 30분 join으로 정답을 복원할 수 있으므로
      (`autoresearch/jobs/feature_store_build.py:295-370`) 평가 파티션은 candidate history에
      섞이면 안 된다. `dt < T`는 누출이 아니지만, candidate history로 완전한 라벨을 만들 수
      있는 마지막 출력일은 **`T-2`**다. 마지막 파티션 `T-1`을 출력일 `T-1`의 완전한 라벨로
      사용하지 못하게 manifest와 검증 계약에 기록한다
- [x] `[T, T_end]` 평가 출력 구간 안에서 고정 salt의 SHA-256 bucket으로 유저 단위 80/20
      분할한다.
      validation/final 중 한쪽이 비거나 spec §8의 structural coverage가 없으면 snapshot
      생성을 거부한다. 지표별 제품 coverage는 P0-2 Judge가 소유한다
- [x] 산출물을 content-addressed 디렉터리에 write-once 게시:
      - `validation/slate.parquet` — `evaluation_id`, `slate_id`, `user_id`, `video_id`,
        `event_timestamp`와 optional 메타데이터. **라벨 없음**
      - `validation/labels.parquet` — `evaluation_id`, `slate_id`, `user_id`, `video_id`,
        `source_event_id`, `clicked`. **봉인**
      - `final_holdout/slate.parquet`과 `final_holdout/labels.parquet` — 각각 대응하는 validation artifact schema와 일치하며, final slate는 label-free이고 final labels는 봉인. 반복
        loop의 candidate 노출 차단은 Stage C `CandidateDataView` 범위로 남김
      - `manifest.json` — split별 `evaluation_id`(content hash), 유저 분할 규칙, 행 수,
        출력일 `[T, T_end]`, candidate history의 `dt < T` 파티션 목록과 완전 라벨 출력일
        상한 `T-2`, 평가 스캔의 `[T, T_end + 1]` 원천 파티션, slate 수, slate당 평균 크기,
        click 보유 slate 비율
- [x] optional 컬럼의 실제 non-null 비율을 manifest에 기록 (갭 조사에서 미확인 항목)
- [x] 테스트: `clicked`는 labels에만 있고 slate에는 없음, 두 파일이 join key
      (`evaluation_id`, `slate_id`, `user_id`, `video_id`)를 공유함, 같은 유저의 slate가
      split을 넘지 않음, candidate history에 `dt >= T`가 한 건도 없음, 출력에
      `[T, T_end]` 밖 impression이 없음, `T_end + 1` click과 impression이 귀속 후보에는
      포함되지만 출력에는 없음, `T_end + 1` 파티션 누락 시 실패, 출력일 `T-1`을
      candidate의 완전 라벨로 취급하지 않음, 동일 입력 → 동일 `evaluation_id`,
      write-once 위반 시 실패

Stage C typed contract·canonical 입력, `RuleBasedActionLogGenerator` production daily 실행,
fixture seed custody, canonical adapter와 Judge handoff는 구현됐습니다. Data-only candidate view와
독립 two-root 최종 실증은 위 Stage B 체크의 완료 범위에 포함하지 않습니다. 실제 candidate
worktree·argv·환경 주입 검사는 Task 3 책임입니다.

**Task 1 상태: [x] 완료.** Research Harness MVP 전체는 완료 처리하지 않는다.

**검증:** `uv run python -m pytest tests/research_harness/test_slate.py -v`

---

## Task 1-C: 결정적 local fixture와 candidate/Judge handoff

이 Task는 #22와
`docs/specs/2026-08-31-research-harness-evaluation-snapshot.md` §13의 exact contract를
구현해 P0-1을 닫는다. 실제 git worktree와 subprocess 환경은 만들지 않는다.

> **2026-09-02 완료 상태:** exact typed model/error와 canonical 입력, production daily 4-run
> fixture builder, canonical `fixture://` source adapter, Stage B snapshot build, P0-2 coverage,
> outer integrity marker 기반 write-once 게시와 Judge handoff 재검증까지 구현했습니다.
> CandidateDataView와 private typed reproducibility verifier로 독립 두 Judge root의 최종 실증 및
> 별도 same-target reuse를 완료했습니다. 문제·해결·검증 근거는 연결 spec의 Stage C
> Portfolio Record에 기록합니다. 후속 Task 2+와 전체 MVP는 진행 중입니다.

- [x] `LocalEvaluationFixture` module의 작은 interface
      `build_local_evaluation_fixture(LocalEvaluationFixtureRequest)`를 구현한다. 필수
      `judge_state_root`, `evaluation_start_date`, default 없는 `fixture_seed`만 받고, 내부에서
      canonical input 생성·4개 일일 producer 실행·Stage B snapshot build를 완주한다
- [x] frozen/extra-forbid `FixtureDescriptor`와 exact receipt를 구현하고, descriptor hash의
      Judge-owned root에 input·action log·snapshot을 write-once 게시한다. seed와 input은 이
      root 밖이나 candidate-safe model에 직렬화하지 않는다
- [x] Stage B `ActionLogSource` seam의 내부 `FixtureActionLogSource` adapter를 구현한다.
      물리 Judge root에서 Parquet를 읽되 identity에는
      `fixture://<descriptor_sha256>/action-log/...` canonical URI만 보고한다. production
      local/GCS adapter 계약은 바꾸지 않는다
- [x] canonical fixture는 `T-2..T+1`, 24 candidates, validation user 160명, final user
      40명을 결정적으로 만들고 양 split의 모든 evaluation slate가 24행·click-positive
      (`click_positive_slate_count == slate_count`, ratio `1.0`)이며 clicked/non-clicked row를
      함께 만족하게 한다
- [x] `CandidateDataView` module의
      `materialize_candidate_data_view(CandidateDataViewRequest, *, source: ActionLogSource)`를
      구현한다. source root·전체 Stage B partition URI를 Judge manifest receipt와 먼저
      대조하되 candidate history 날짜만 열며 exact
      output은 `harness_in/candidate-view.json`, validation `slate.parquet`, manifest가 허용한
      `dt < T` history의 물리적 byte-copy뿐이다. fixture는 같은 내부 adapter를 주입한다
- [x] candidate view에서 labels/final, 전체 snapshot manifest·root·fingerprint, 평가 source
      URI, fixture descriptor·seed·input과 Judge path를 filename·내용·receipt 모두에서
      배제한다. fixture source는 outer integrity와 같은 physical root에 결속하고 source와
      candidate의 filesystem identity alias, symlink·junction·hardlink를 거부하며 identical
      complete target만 reuse한다. canonical fixture layout에서 Judge state root를 역산해
      candidate destination과의 양방향 포함 관계도 거부한다
- [x] `CandidateDataViewRequest`에 split/final 선택 parameter를 두지 않는다. final slate는
      consumption registry 권한을 요구하는 별도 후속 interface로 남긴다
- [x] Stage B `_SUCCESS`, typed manifest, manifest SHA와 네 artifact digest·row count를
      재검증한 최소 `JudgeSnapshotHandoff`를 만든다. P0-2는 이 handoff만 소비한다
- [x] 서로 다른 두 Judge root에서 같은 seed·날짜를 독립 생성해 두 receipt가 모두
      `reused=false`이고 source SHA·slate ID·evaluation ID·네 artifact SHA·snapshot
      fingerprint가 같은지 확인한다. 같은 target 재호출의 `reused=true`는 별도 테스트다
- [x] spec의 실패 code를 typed error로 구현하고, 오류에 user/input/path 원문을 노출하지
      않는다. public Stage C 경계의 알려진 하위 오류 번역은 exception chaining도 억제한다
- [x] Problem/Solution/Result에 candidate/Judge interface 분리, canonical source adapter,
      독립 재생성 증거와 남은 same-UID 한계를 기록한다

**검증:**

```bash
uv run python -m pytest tests/research_harness/test_fixture.py -v
uv run python -m pytest tests/research_harness/test_slate.py -v
uv run python -m pytest tests/action_log_generation/ -v
uv run --no-sync ruff check autoresearch tests
git diff --check
```

---

## Task 2a: 리랭킹 지표 순수 함수

P0-2A PR이다. 의존이 없고 순수 계산이라 단독으로 완결하며 이 Task만으로 PR 1개를
만든다. Judge 파일 I/O·prediction 1:1 검증·제품 coverage gate·판정은 포함하지 않는다.

- [x] `ranking_metrics.py` — 같은 길이의 `labels`, `scores`, `slate_ids`, `video_ids`와
      양의 정수 `k`를 받는 `ndcg_at_k()`, `recall_at_k()`. grouping·정렬·집계를 숨기고
      공통 `RankingMetricResult(value, total_slates, scored_slates,
      skipped_zero_click_slates, coverage)`를 반환한다
- [x] binary relevance와 계산식을 spec §7의 P0-2A 계약대로 고정한다.
      `DCG@k = sum(rel_i/log2(i+1))`, `NDCG@k = DCG@k/IDCG@k`,
      `Recall@k = top-k click 수/slate 전체 click 수`이며 slate별 동일 가중치 macro 평균이다
- [x] **0-click slate 처리 규칙을 명시적으로 정한다** — ideal DCG가 0이라 NDCG가 정의되지
      않으므로 평균에서 제외하고, 제외 비율을 `coverage`로 함께 보고한다. 조용히 0점으로
      처리하면 지표가 데이터 구성에 따라 왜곡된다
- [x] ranking은 click 확률 추정치 `score` 내림차순, 동률은 `video_id` 오름차순으로 고정한다.
      `[0,1]` 범위 검사는 보정 품질의 증거가 아니며 LogLoss·Brier guardrail이 이를 감시한다
- [x] 길이 불일치, `k <= 0`, 비 binary label, 빈 식별자, NaN·Inf score는 typed
      `RankingMetricError`와 고정 reason code로 거부한다. 식별자는 앞뒤 공백 없는 non-empty
      `str`이고, key 1:1 유일성과 score `[0,1]`은 P0-2B가 소유한다
- [x] `total_slates`는 고유 slate ID 수이고 `skipped_zero_click_slates = total_slates -
      scored_slates`다. slate ID 오름차순과 `math.fsum()`으로 P0-2B가 보장한 고유 key
      입력의 row 순서에 독립적인 macro 집계를 만든다
- [x] 손 계산 golden test: 완전 정답 순서 → 1.0, 역순 → 손 계산값, 0-click slate 제외,
      동점 처리, 고유 key 입력의 row 순서 불변, k보다 짧은 slate, click 수 > k인 slate,
      유효 slate 없음
- [x] Problem/Solution/Result 기록에 지표 왜곡 위험, macro/coverage 선택 근거, 손 계산 및
      회귀 테스트 결과를 남긴다

**검증:** `uv run python -m pytest tests/research_harness/test_ranking_metrics.py -v`

---

## Task 2b: prediction 계약과 Judge scoring

P0-2B PR이다. 공개 `build_validation_target()`이 검증된 Stage C handoff의 validation ID와
정확한 slate/label artifact를 고정한 opaque `JudgeEvaluationTarget`을 만들고, Judge 소유
prediction 사본과 함께 CSV parse, schema·1:1 key 계약 검증과 ranking·probability metric
계산에 사용한다. target은 직접 생성할 수 없고 package에서 재수출하지 않는다. Candidate
CSV의 `evaluation_id`는 대상을 선택하지 않고 target의 기대값과 일치하는지만 검증한다.
P0-2B/C에는 final target factory를 두지 않으며, 후속 final registry Task가 발급한
`FinalConsumptionGrant` 전용 factory가 추가되기 전 production scoring은 validation으로
제한한다. candidate 경로에서 안전하게 사본을 만드는 ingestion과 σ 판정은 P0-2C가 소유한다.

- [x] `judge.py` — 입력은 `build_validation_target()`이 만든 trusted target + Judge 소유
        prediction 사본. target은 expected validation ID·정확한 slate/label artifact를 고정하며
        prediction 값으로 split을 선택하지 않는다. 직접 target 생성과 handoff만으로 final target
        생성을 시도하면 typed `invalid_judge_target` 오류로 거부한다. package 공개 interface는
        `build_validation_target()`, `score_predictions()`, 결과·오류 타입으로 제한하고 opaque
        target과 parser row는 재수출하지 않는다
- [x] Judge 소유 사본을 읽는 parser가 header·field byte·행 수 계약을 검증해 typed prediction
        rows를 만들고, semantic validator와 scoring은 이 값만 소비한다. evaluation ID는 정확히
        69 byte, slate/video ID는 1~64 ASCII byte, score token은 최대 24 ASCII byte, 행은 최대
        300,000개다
- [x] **predictions 스키마 강제 검증**: 컬럼은
      `evaluation_id, slate_id, video_id, score`. `evaluation_id`는 대상 split manifest와
      정확히 같고, 나머지 키는 slate와 정확히 1:1이어야 한다 — 누락 행, 중복 행,
      slate에 없는 행, NaN/Inf 또는 `[0,1]` 밖 score는
      전부 `invalid_predictions`로 거부. 거부는 실행 실패이지 지표 0점이 아니다
- [x] 지표 산출: `JudgeScoringResult`에 primary `ndcg_at_10`, ranking guardrail
        `recall_at_10`·`ndcg_at_24`,
        probability guardrail은 `labels/scores/groups`를 받는 순수
        `model_evaluation/probability_metrics.py`로 기존 `evaluate.py` 계산을 동작 변경 없이 먼저
        추출하고 기존 CLI와 Judge가 함께 사용한다. 결과에는 row·positive·negative count를
        포함하고 단일 클래스 전역 지표는 `None`으로 구조화한다. 구조 추출과 Judge 동작 추가는
        별도 커밋이다
- [x] 테스트: schema·artifact 계약 위반, evaluation ID 불일치, key 누락·중복·extra,
      `[0,1]` 범위 위반, prediction이 target split을 선택하지 못함, 직접 target 생성과 final
      factory 부재, ranking metric 결합과 기존 probability metric 의미 보존

**검증:** `uv run python -m pytest tests/research_harness/test_judge.py -v`

---

## Task 2c: sealed prediction ingestion과 deterministic 판정

P0-2C PR이다. candidate 경로를 Judge 소유 사본으로 봉인하는 파일 ingestion과, P0-2B의
metric 결과를 지표별 baseline σ에 비교하는 판정을 추가한다.

- [x] candidate 경로의 regular file을 `O_RDONLY|O_NONBLOCK|O_NOFOLLOW`로 한 번만 열고 검증한 같은 FD에서
        `65 MiB + 1` byte까지만 읽는다. 복사 전후 `fstat`의 identity·mode·size·mtime을
      비교해 교체·성장을 검출하고, Judge 목적지는 `O_CREAT|O_EXCL`로 만든다. 경로를 다시
      열지 않으며 schema와 지표 계산은 Judge 사본만 읽는다
- [x] P0-2B의 동일 parser 구현을 격리 subprocess에서 실행해 정확히 4개 필드와 field
      byte 계약을 강제한다. 검증된 행은 exclusive 정규화 JSONL로 저장하고 CSV·JSONL
      identity와 digest를 opaque receipt에 결속한다. scoring은 이 receipt만 받아 정규화 행을
      streaming 소비한다. 부모가 정규화 목적지를 `O_EXCL`로 예약하고 worker는 같은 inode에만
      쓰며 cleanup도 소유 identity가 유지된 파일에만 적용한다. P0-2C가 별도 parser·schema
      정의를 만들지 않는다.
        `evaluation_id`는 현재 ID 계약에 맞는 정확한 69 byte, `slate_id`·`video_id`는
        comma·quote·개행 없는 ASCII 각 1~64 byte, `score` token은 최대 24 byte다. CRLF 기준
        최악 행 226 byte와 header 39 byte에서 300,000행은 67,800,039 byte이므로 65 MiB 안에
        든다. 행 수는 대상 slate와 같으면서
      최대 300,000행, wall-clock 10초, 메모리 256 MiB여야 하며 상한 위반은
      `invalid_predictions`다
- [x] `compare()` — 지표 방향을 정규화하고 D5 규칙(primary `≥2σ_primary` 개선 + 각
      guardrail `≥-1σ_metric`)으로
      `promote | revise | discard` + `reason_code` 산출. **임계값을 코드에 상수로 박지 않고
      지표별 σ map을 인자로 받는다.** 실제 σ 값 측정은 Task 7에서 한다 — 여기서는 map을
      주입받아 판정하는 로직만 만든다
- [x] 모든 필수 `σ_metric > 1e-6`을 강제하고, grouped ROC-AUC 등 필수 값이 `None`이면
      `metric_unavailable`로 판정 불가 처리한다. grouped ROC-AUC coverage는 채점 유저가
      non-null 전체 유저의 20% 이상이면서 최소 30명, probability metric은 item 100%와
      양성·음성 label 모두를 요구한다. 미달이면 `insufficient_metric_coverage`다
- [x] screening은 고정 seed의 same-seed baseline보다 primary가 좋아진 candidate만 확인
      실험으로 보내는 비용 gate다. champion 승격은 같은 5개 seed의 확인 실험에서 계산한
      paired normalized delta 평균만 확정한다
- [x] NDCG@10·NDCG@24·Recall@10은 유효 slate가 전체의 20% 이상이면서 최소 30개여야 한다.
      미달이면 `insufficient_metric_coverage`로 판정 불가다
- [x] 테스트: champion 동률 시 판정, symlink·65 MiB 초과·복사 중 성장·기존 목적지 거부,
      FIFO·regular-to-FIFO 교체 비차단 거부, 실패·중단 cleanup, 봉인 CSV·정규화 행 변조와
      unsealed Path 채점 거부, ID/score field byte 상한과 300,000행 경계, `[0,1]` 범위 위반,
      parser 행/시간/메모리 상한, higher/lower 방향 정규화,
      σ=0·`1e-6` 경계, metric `None`, 지표별 coverage 미달, 2σ 직전/직후와 guardrail
      -1σ 직전/직후 판정 전환

**검증:**

```bash
uv run python -m pytest tests/research_harness/test_judge.py \
  tests/research_harness/test_judge_decision.py \
  tests/research_harness/test_prediction_ingestion.py \
  tests/research_harness/test_slate.py -v
```

**봉인 검증(필수):** Judge 모듈이 candidate workspace 경로를 참조하지 않음을 테스트로
고정한다.

---

## Task 2d: `ResearchDomain` ABC + `YouTubeCTRDomain`

Task 1(slate), Task 2a(지표), Task 2b(scoring), Task 2c(봉인·판정)가 완료된 뒤 시작하고,
Task 5b Controller보다
먼저 끝낸다. Controller는 구체 slate/Judge 구현이 아니라 이 interface를 통해 호출한다.

- [x] `domain.py`에 spec 5.1의 다섯 메서드
      (`describe_capabilities`, `build_evaluation_snapshot`, `validate_candidate`, `evaluate`,
      `compare`)를 가진 `ResearchDomain` ABC를 정의한다
- [x] 현재 typed interface는 `EvaluationSnapshotRequest`→`EvaluationSnapshotReceipt`,
      candidate/Judge `Path`→`SealedPredictionReceipt`, `JudgeSnapshotHandoff` + sealed receipt
      →`JudgeScoringResult`, 단일 `PairedJudgeResult`→`ScreeningResult`, 5-seed pair sequence +
      지표별 σ map→`ConfirmationDecision`으로 고정한다. 아직 없는 Candidate/Trial 임시 모델은
      만들지 않는다
- [x] `YouTubeCTRDomain`은 MVP에서 실제 필요한 `build_evaluation_snapshot()`,
      `validate_candidate()`, `evaluate()`, `compare()`를 Task 1·2b·2c 구현에 위임한다.
      논문 발견이 없는 MVP에서 호출하지 않는 `describe_capabilities()`는 명시적 미지원
      오류를 내고, Paper Discovery 단계 전에는 빈 값이나 임시 capability를 꾸며 내지 않는다
- [x] `__init__.py`에서 공개 domain 타입을 재수출한다
- [x] 테스트: ABC가 다섯 메서드 계약을 강제함, YouTube adapter가 snapshot·검증·평가·비교를
      올바른 구현으로 전달함, `describe_capabilities()`가 명시적 미지원 오류를 냄

**의존 순서:** `Task 1 + Task 2a → Task 2b → Task 2c → Task 2d → Task 5b`

**검증:** `uv run python -m pytest tests/research_harness/test_domain.py -v`

---

## Task 3: CandidateWorkspace + 산출물 계약

현행 executor를 건드리지 않고 정적 allowlist 없는 독립 로컬 workspace를 만드는 지점이다.

- [x] `workspace.py` — 기준 SHA에서 disposable git worktree 생성, 종료 시 회수
- [x] 반복 중에는 Task 1-C의 `CandidateDataView`를 새 worktree root에 materialize한다.
      `harness_in/slate.parquet`, `candidate-view.json`과 `dt < T` history 이외의 데이터가
      있으면 workspace 생성을 거부한다. Task 3이 snapshot manifest를 다시 해석하거나 파일
      복사 규칙을 중복 구현하지 않는다
- [x] 반복용 공개 API에는 final 주입 함수를 두지 않는다. final holdout slate는 Controller가
      loop를 닫고 Task 4 consumption registry 권한을 얻은 뒤 Task 5b의 별도 final 실행
      경계로 한 번만 주입한다. validation `CandidateDataView`에 split flag나 임시 권한
      token을 추가해 final을 우회 노출하지 않는다. **labels, ledger, judge 체크아웃은
      worktree 바깥 경로에 두고 경로도 candidate에 전달하지 않는다**
- [x] Task 3의 candidate 실행 context·환경에서 fixture input·fixture seed와 Judge handoff를
      제외한다. 모델 재학습 seed를 포함한 실제 argv 조립과 subprocess 환경 전달 검증은
      Task 5a가 소유한다
- [x] candidate 환경은 명시적 allowlist로 새로 만들고 GCS·BigQuery credential env와
      credential 파일을 주입하지 않는다. 원격 데이터 접근 없이 Harness의 로컬 history만
      읽는 계약을 테스트한다
- [x] `harness_out/` 생성. candidate가 여기에만 산출물을 쓴다
- [x] **allowlist 검사 없음.** 대신 커밋 직전 시크릿 스캔만 수행(D4). 기존
      `applications/experiment_platform/executor/safety.py:21-38`의
      `contains_credential_value()`를 재사용한다. verifier의 path·dependency·generated-data
      검사는 가져오지 않고 credential 값 탐지만 호출한다
- [x] diff content fingerprint를 계산해 ledger에 넘긴다(차단용이 아니라 기록용)
- [x] 테스트: worktree 격리, labels가 주입·argv·환경에 없음, final slate가 반복 중 없음,
      history에 `dt >= T` 없음, fixture 평가 입력·seed 없음, 원격 credential 없음, 시크릿 포함
      diff 거부, `.parquet`/`pyproject.toml` 수정이 **허용**되는지(D3·D4 회귀 방지)

**검증:** `uv run python -m pytest tests/research_harness/test_workspace.py -v`

### Task 3 포트폴리오 기록

**문제.** 기존 executor workspace는 단일 실행과 verifier의 경로·의존성 제한을 전제로 해
저장소 전체를 바꾸는 자율 ML 실험에 그대로 사용할 수 없었다. 반대로 제한을 단순히 없애면
candidate가 자기 평가 데이터나 host credential을 우연히 전달받고, 어떤 변경을 평가했는지
재현할 근거도 사라진다. Task 1-C가 데이터 자체의 봉인을 담당하므로 Task 3에서 snapshot을
다시 해석하면 두 구현이 어긋날 위험도 있었다.

**해결.** 기준 commit을 검증한 detached Git worktree를 context manager로 감싸 수명주기를
한 모듈에 숨기고, 기존 `CandidateDataView`를 그대로 호출해 validation slate와 `dt < T`
history만 주입했다. candidate에는 worktree·slate·prediction 경로와 최소 OS 환경만 담은
실행 context를 주고 Judge handoff·fixture seed·원격 credential은 제외했다. 변경 정책은
경로 allowlist 대신 기존 credential 값 탐지만 재사용했으며, tracked binary diff와 정렬된
untracked bytes를 길이 구분해 ledger용 SHA-256 fingerprint를 계산했다. final 주입은 아직
없는 registry 권한을 모사하지 않고 Task 4/5b의 별도 경계로 명시적으로 남겼다.

**결과.** 독립 리뷰가 가변 `HEAD` 때문에 commit된 candidate와 credential이 누락되는 문제,
ignored 파생 데이터가 fingerprint에서 빠지는 문제, 소유권을 잃은 교체 경로 삭제와 Git
metadata 회수 실패 은폐, Git remove 직후 교체 경로 삭제, patch 표현 설정에 따른
fingerprint 변동, index-only secret과 gitlink 상태 누락을 발견했다. 이를 봉인 `base_sha`
기준 base→index/index→working tree 비교, ignored 파일의 상태·mode·type·bytes와 gitlink
상태의 canonical hash, index blob credential 검사, worktree identity 재검증과 fail-closed
cleanup으로 수정했다. 추가 리뷰에서 submodule 내부의 harness 예약명 제외 우회와 staged
gitlink/checkout HEAD 불일치를 발견해, 예약명 제외를 최상위로 한정하고 불일치는 fail-closed했다.
Task 3 집중 테스트 29개 통과·POSIX mode 테스트 1개 환경 의존 skip으로 정확한
SHA, validation-only 데이터, 환경 격리, commit·uncommitted secret 거부, 삭제된 secret 허용,
`.parquet`·`pyproject.toml` 변경 허용, 설정 독립 fingerprint와 교체 경로 보존을 검증했다.
전체 Research Harness 회귀 테스트는 최종 수정 뒤 401개 통과·7개 환경 의존 skip이었다.
이는 완전한 OS sandbox가 아니라 실수와 자기 채점 오염을 줄이는 MVP 경계이며, 실제
subprocess 회수와 final 단일 소비는 각각 Task 5a와 Task 4/5b에 남는다.

---

## Task 4: Trial Ledger + checkpoint

- [x] `ledger.py` — `experiment-ledger.jsonl` append-only. 공개 interface는
      `open_trial_ledger(path)`, `append(TrialRecord | CheckpointRecord)`, `read_state()`로 제한하고
      process lock, canonical JSONL, 연속 sequence, file `fsync`를 내부에서 소유한다
- [x] `consumption_registry.py` — 필수 harness 설정
      `harness-run --judge-state-root <absolute-path>`를 정규화한 **고정 절대 경로** 아래
      `final-holdout-consumed/<evaluation_id>` marker를 둔다. state root는
      run·workspace·ledger에 종속시키지 않는다. 상대 경로, root/registry 디렉터리 부재,
      읽기·marker 생성·`fsync` 불가는 모두 final 평가 시작 전 fail-closed하고, 임시 경로를
      만들거나 fallback하지 않는다
- [x] final 평가 시작 전에 marker를 `O_CREAT|O_EXCL`로 생성하고 metadata를 쓴 뒤 file과
      parent directory를 `fsync`한다. 이미 있으면 두 번째 평가를 거부한다. `O_EXCL` 생성에
      성공한 뒤 어떤 write/sync가 실패해도 marker는 삭제하지 않고 소비된 상태로 남긴다
- [x] registry는 검증된 handoff에서 final ID를 가져와 opaque `FinalConsumptionGrant`를 발급하고,
      `judge.build_final_target(handoff, grant)`만 같은 handoff의 final artifact를 열 수 있다.
      registry는 snapshot을 먼저 재검증하며 grant를 fingerprint·manifest digest·final ID·marker
      evidence에 결속한다. snapshot이 정규화된 state root의 실제 하위 경로가 아니면 거부한다.
      grant 직접 생성, 다른 handoff 재사용, handoff 단독 final target 생성을 거부한다
- [x] trial당 기록: `trial_id`, 기준/candidate SHA, diff fingerprint, `evaluation_id`,
      seed, 전체 지표, decision과 reason_code, 소요 시간, 실패 reason code, champion lineage
- [x] validation trial과 final holdout을 구분하고 ledger에는 registry marker의 경로·digest를
      증거로 기록한다. 소비 권한은 ledger가 아니라 전역 registry가 소유한다
- [x] 단계별 idempotent checkpoint — 프로세스 종료 후 마지막 완료 단계부터 재개.
      **Job 전체 재실행을 기본 재시도 단위로 쓰지 않는다**(spec 7.2)
- [x] `read_state()`는 마지막 sequence, 순서 보존 record, 완료 checkpoint ID, registry evidence를
      불변 `TrialLedgerState`로 반환한다. trial/checkpoint key의 동일 payload 재호출은 no-op,
      다른 payload 재호출은 conflict로 거부한다. 마지막 newline 뒤 불완전 bytes만 복구하고
      newline-terminated 오류·중간 손상·sequence 단절·중복 key는 fail-closed한다
- [x] 테스트: 중단 후 재개가 완료 단계를 건너뜀, 같은 trial 중복 append 방지,
      ledger의 손상된 마지막 줄 복구. 별도 테스트에서 새 run·새 ledger·동시 Controller가
      같은 `evaluation_id`를 소비하지 못함, marker 생성 뒤 crash해도 재평가 거부,
      손상 marker도 존재만으로 소비 처리함, root 부재·접근 불가 시 final 미시작을 고정한다.
      이전 evidence가 기록한 marker가 사라진 경우 상태 무결성 위반으로 fail-closed하고
      marker를 재생성하지 않는다. Task 5는 복구한 evidence를 registry claim에 반드시 전달한다.
      ledger 마지막 줄 복구를 registry에 적용하지 않는다

**검증:** `uv run python -m pytest tests/research_harness/test_ledger.py -v`

**문제.** final holdout은 후보가 반복해서 볼수록 사실상 validation으로 오염되지만, run별
checkpoint만으로는 새 run이나 동시 Controller의 재소비를 막을 수 없었다. 또한 프로세스가
중단되면 어떤 실험과 외부 부작용이 완료됐는지 구조적으로 복원할 기록이 없어, 안전한 재개와
맥락 없는 Judge·REPORT의 근거 연결이 불가능했다.

**해결.** Judge 상태 루트에 evaluation별 marker를 원자적으로 선점하고 file·directory를
동기화한 뒤에만 opaque grant를 발급하도록 했다. grant와 snapshot handoff가 일치해야만 final
target을 열 수 있다. 별도의 append-only JSONL Ledger는 canonical record, 연속 sequence,
trial/checkpoint idempotency key와 process lock을 한 경계 안에서 관리한다. crash로 마지막 줄이
미완성인 경우에만 마지막 newline 이후를 복구하고, 완성된 손상 record와 marker 손상은
fail-closed한다.

**결과.** 손 계산 fixture 기반 final target, 동시 marker claim, marker sync 실패와 재시도,
evidence 소실, Ledger의 thread·process 동시 append, 멱등 재시도, checkpoint 재개, tail 복구와
손상 탐지 테스트를 통과했다. 이로써 Task 5 Controller는 raw 파일 형식을 알 필요 없이
`TrialLedgerState`와 grant만 소비할 수 있다. 실제 E2E 실험의 모델 품질·자율성·비용 수치는 전체
구현 후 측정하며, 동일 OS 사용자가 상태 루트를 삭제하는 공격 방지는 MVP 범위 밖에 남긴다.
독립 리뷰에서는 상위 Judge root로 두 번째 registry를 만들 수 있는 문제, marker 없는 grant
발급 우회, 새 재개 시각에 따른 정상 evidence 오판, 비정본 JSONL 수용, Windows 잠금 오류의
무한 재시도, 동기화 실패 재시도 우회, lock hardlink 변조와 crash 후 빈 lock의 영구 실패를
발견했다. 이를 11개의 실패 반례로
재현한 뒤, snapshot의 canonical state root 결속, 실제 marker digest 재검증, canonical physical
line 비교, 잠금 오류 분류, lock path↔descriptor identity와 재시도 가능한 공통 directory sync
경계로 수정했다. Task 4 최종 수정 뒤 Research Harness 전체 회귀는 441개 통과·7개 환경 의존
skip이었고, 저장소 전체 Ruff 검사도 통과했다.
첫 Linux CI에서는 directory sync와 append sync가 같은 `os.fsync` mock을 공유한 테스트 오판과
Linux `OSError`에 `winerror`가 없다는 이식성 오류가 드러났다. file sync seam을 분리하고
선택 속성 접근으로 바꿔 Windows·POSIX가 같은 오류 계약을 사용하도록 수정했다.

---

## Task 5a: LocalRunner

- [x] `runner.py` — 공개 경계를 `LocalRunner.run(LocalRunRequest) -> LocalRunReceipt`
      하나로 제한한다. request는 `CandidateProcessContext`, 0 이상 32-bit `seed`, 유한한
      양수 timeout만 받고, 호출자 지정 command·argv·환경 확장은 허용하지 않는다
- [x] workspace에서 현재 Python interpreter의 고정 진입점
      (`-m autoresearch.cli harness-predict --slate <in> --out <out> --seed <n>`)을
      subprocess로 실행한다. stale output은 실행 전에 거부하고 stdin은 항상 `DEVNULL`로
      닫으며 불필요한 handle을 상속하지 않는다. stale 검사는 broken symlink를 포함한
      `lexists` 의미를 사용한다
- [x] 자유 생성 가능한 `CandidateProcessContext`를 provenance로 신뢰하지 않는다. 환경의
      중복·NUL을 거부하고 실행 시점 allowlist OS 값과 고정 Python 설정을 재계산해 exact
      equality를 확인한 뒤에만 candidate에 전달한다
- [x] stdout/stderr pipe를 계속 비우되 각각 마지막 64 KiB만 보존하고, wall-clock timeout을
      적용한다. 상한은 decode 전 bytes 기준이고 UTF-8 replacement decode한다. tail 필드는
      `repr=False`로 implicit 로그 노출을 막는다. MVP의 candidate 자원 상한은 이 둘이며
      CPU·RSS·filesystem quota와 적대적 격리는 E2E 측정 뒤 별도 runner에서 보강한다
- [x] 실패는 `runner_invalid_request`, `runner_start_failed`, `predict_timeout`,
      `predict_crash`, `invalid_predictions`, `runner_process_leaked`,
      `runner_cleanup_failed`로 분류한다. 오류 문자열에는 로그·로컬 경로를 넣지 않는다
- [x] exit 0 뒤 exact output이 새 regular file인지 확인한다. CSV 의미 검증과 Judge copy는
      기존 `seal_prediction_copy()`에 남겨 runner가 Sealed Judge 책임을 침범하지 않게 한다
- [x] candidate를 새 process group/session으로 시작한다. 정상 종료·timeout·취소·예외 모두
      TERM grace 뒤 KILL과 최종 wait를 거쳐 소유 group/Job을 상속한 child/grandchild를
      회수하고, 남은 process가 있으면 실행 실패로 처리한다. POSIX에서 의도적으로 새 session을
      만드는 적대적 탈출은 container/PID namespace 후속 범위다. 기존 패턴은
      `applications/experiment_platform/executor/codex_worker.py:537-574,624-670`이다
- [x] POSIX는 새 session/process group, Windows는 kill-on-close Job Object를 사용한다.
      Windows는 candidate workspace 밖 절대 경로의 `-I` trusted launcher를 Job에 먼저 붙이고
      private stdin gate를 release한 뒤 candidate를 만드는 방식으로 pre-assignment spawn race를
      없앤다. gate 전 candidate import를 금지하고 모든 gate/Job handle을 회수한다.
      best-effort `CTRL_BREAK_EVENT` grace 뒤
      `TerminateJobObject`, active process count 0 확인, 모든 경로의 handle close 순서를
      지킨다
- [x] launcher는 parent 소유 임시 status artifact로 candidate `Popen`의 started/failed를
      보고해 내부 start 실패와 candidate 실제 exit 127을 구분한다. status 실패·cleanup은
      start/cleanup 오류 우선순위에 포함하고 경로를 노출하지 않는다
- [x] cleanup 실패는 timeout·crash·invalid prediction보다 우선한다. 정상 parent 종료 뒤
      descendant를 발견해 성공적으로 회수했으면 `runner_process_leaked`다. cancellation에서는
      회수 후 `KeyboardInterrupt`/`SystemExit`를 다시 발생시키고 cleanup 실패는 sanitized
      exception note로 함께 보존한다
- [x] 테스트용 임시 `autoresearch.cli` candidate package를 public interface로 실행해
      command·seed·최소 환경 전달, 정상 실행, bounded tail, invalid request/stale output,
      start 실패, timeout, 비정상 종료, 산출물 미생성의 reason code를 검증한다
- [x] stale broken symlink·FIFO/directory와 성공 뒤 symlink/non-regular output을 나눠 검증하고,
      receipt/error `repr`에 tail·경로·credential이 나타나지 않는지 확인한다
- [x] timeout, 정상 parent 선종료, cancellation에서 즉시 spawn한 grandchild가 실제로
      회수되는지 POSIX·Windows 각각 통합 검증한다. Windows에서는 candidate release 전 Job
      결속 실패도 검증한다. 활성 Ubuntu CI와 로컬 Windows의 OS별 실행 근거를 PR에 남긴다
- [x] 문서의 문제·해결·결과 기록을 실제 검증 근거로 갱신한다. `TrialResult`와 공통
      `ExperimentRunner`는 Task 5b 소비 형태가 생길 때까지 만들지 않는다

**검증:** `uv run python -m pytest tests/research_harness/test_runner.py -v`

### Task 5a 포트폴리오 기록

**문제.** 자율 실험 candidate를 host process에서 직접 호출하면 timeout 뒤 child가 남거나,
parent가 먼저 끝낸 grandchild가 다음 trial의 CPU·파일을 계속 점유할 수 있었다. 기존 Codex
worker의 POSIX process-group 패턴은 Windows에서 parent만 종료하므로 그대로 재사용할 수 없었다.
또한 자유 생성 가능한 `CandidateProcessContext`를 신뢰하면 호출자가 `PYTHONPATH`나 credential
환경을 다시 넣을 수 있고, Windows에서 일반 `Popen` 후 Job Object에 붙이면 candidate가 그
짧은 사이에 Job 밖 descendant를 만들 수 있었다.

**해결.** 공개 interface를 stateless `LocalRunner.run()` 하나로 제한하고 command·argv·환경을
Harness가 고정했다. candidate workspace 밖의 절대 경로에 있는 trusted launcher를 `-I`로
시작해 1-byte gate에서 정지시키고, Windows Job Object 또는 POSIX session에 먼저 결속한 뒤에만
candidate를 release한다. timeout·정상 parent 선종료·cancellation은 공통 cleanup 상태 머신에서
graceful 요청, 강제 종료, active tree 부재, final wait, pipe reader 종료 순으로 처리한다.
stdout/stderr는 계속 drain하면서 decode 전 마지막 64 KiB만 보존하고, 오류 우선순위와 안전한
representation을 typed contract로 고정했다. prediction CSV 의미 검증은 기존 Sealed Judge에
남겨 runner가 채점 책임을 중복 소유하지 않게 했다.

**결과.** Windows 개발 환경에서 고정 argv·seed·stdin 차단·exact 최소 환경, forged context,
stale/non-regular artifact, crash·missing output, launcher 시작·Job 결속 실패, 64 KiB tail,
timeout·정상 parent 선종료·cancellation 뒤 실제 grandchild 회수를 검증한 집중 테스트가
26개 통과했고 symlink·FIFO 환경 의존 테스트 4개는 skip됐다. Research Harness 전체는
467개 통과·11개 환경 의존 skip, 저장소 전체 Ruff는 통과했다. 독립 spec 검토에서 발견한
Windows pre-assignment spawn race, console 없는 host의 `CTRL_BREAK_EVENT` 실패, 환경 재주입,
stdin/handle 상속과 복합 오류 모호성을 구현 전에 계약과 반례로 보강했다. 코드 리뷰에서는
시작 gate 도중 cancellation, cleanup 예외가 원래 오류를 덮는 경로, candidate start 실패와
exit 127 혼동, 살아 있는 writer의 buffered pipe close가 무기한 대기하는 문제를 재현했다.
이를 no-throw cleanup, 별도 start status, deadline 뒤 non-blocking 실패 반환으로 수정했다. 이 단계는
wall-clock·로그 메모리·소유 group/Job 수명만 제한하며 CPU/RSS/filesystem quota와 적대적 격리는
실제 E2E 측정 후 별도 container/Kubernetes runner에서 판단한다.

---

## Task 5b: 자가 피드백 + Controller

- [x] spec 7.3에 `ExperimentCard`·`ResearchBudget`, planner/runner seam, budget 종료,
      ledger-first feedback, final 무피드백과 checkpoint 재생 계약을 고정한다
- [x] `feedback.py` — **에이전트에게 돌려주는 불변 payload**:
      - validation primary/guardrail 지표 값과 champion 대비 방향 정규화 델타
      - `decision`과 `reason_code`
      - 이전 trial 이력 요약 (무엇을 시도했고 왜 기각됐는지)
      - 실패 시 stage + reason code + 로그 tail
      - **행 단위 정답과 지표 구현 코드는 포함하지 않는다**
- [x] `controller.py` — Task 2d의 `ResearchDomain` interface를 주입받아 snapshot·candidate
      실행 adapter가 돌려준 paired scoring 결과를 비교하고 예산(최대 시간/trial 수) 안에서
      반복한다. candidate workspace·LocalRunner·봉인·평가는 `ResearchTrialRunner` adapter가
      같은 domain interface로 조립하며 Controller는 구체
      `YouTubeCTRDomain`의 slate/Judge 모듈을 Controller에서 직접 호출하지 않는다. spec 7장
      루프 구조를 따르되
      MVP에서는 사람이 준 가설과 `ExperimentCard`를 입력 seam에 주입한다. 다음
      단계에서는 Paper Discovery/Capability Matcher가 만든 `ExperimentCard`가 같은 seam을
      사용한다
- [x] `ResearchDomain.evaluate(..., final_grant=...)` keyword-only 확장으로 validation과
      승인된 final 평가를 같은 domain seam에 유지하고 Controller의 Judge 직접 import를 막는다
- [x] 실패 시 사용자에게 묻지 않고 다음 행동을 스스로 정한다(spec 7.2)
- [x] validation loop 종료 후 champion을 고정하고 final holdout을 마지막 1회 평가한다.
      필수 절대 `judge_state_root`의 존재·접근을 확인하고 전역 registry marker를 fsync한
      뒤에만 실행한다. final 결과는 feedback을 만들지 않고 ledger/REPORT evidence에만 기록한
      뒤 종료한다
- [x] final holdout의 유효한 판정이 `promote`면 `개선`, `revise|discard`면 baseline을
      유지하고 `개선 없음`, 유효한 비교를 만들지 못했을 때만 `판정 불가`로 결론낸다
- [x] 테스트: seed-sensitive fake domain/runner로 2 trial 이상 루프가 도는지,
      피드백 payload에 라벨이 없는지, final 결과가 payload에 없는지, final 평가가 두 번째
      호출을 거부하는지, 예산 소진 시 정상 종료하는지
- [x] Task 4 ledger의 기존 record를 깨지 않으면서 새 trial에 canonical
      `experiment_summary`를 보존하고, 재개 시 planner replay가 달라지면 fail-closed한다

**검증:** `uv run python -m pytest tests/research_harness/ -v`

### Task 5b 포트폴리오 기록

**문제.** Task 5a까지는 candidate subprocess를 안전하게 한 번 실행할 수 있었지만, 그 결과를
보고 다음 실험을 선택하는 정책과 durable memory가 없었다. 기존 `ResearchDomain.evaluate()`는
validation만 표현해 Controller가 final Judge 함수를 직접 import해야 했고, Trial Ledger에는
무엇을 시도했는지가 없어 프로세스 재시작 뒤 동일한 피드백을 재구성할 수 없었다. 이 상태에서는
실행 횟수가 늘어날 뿐 자율 ML 연구의 핵심인 관찰→판단→다음 변경 루프가 성립하지 않았다.

**해결.** `ResearchController.run()` 하나 뒤에 trial/time budget, screening→5-seed confirmation,
promote-only champion 전이, 실패 후 자동 계속, ledger-first feedback, checkpoint 재생과 terminal
final 판정을 모았다. candidate coding·workspace·LocalRunner·prediction 봉인은
`ResearchTrialRunner` seam 뒤에 두고, Controller는 `ResearchDomain.compare()`만으로 정책을
결정하게 했다. `ExperimentCard`는 canonical JSON summary로 ledger에 남기고 기존 Task 4 record는
선택 필드가 없는 형식 그대로 읽도록 호환성을 유지했다. final은 registry grant가 있을 때만
domain의 같은 `evaluate(..., final_grant=...)` interface로 접근하고 결과를 agent feedback 타입에
표현할 수 없게 분리했다.

**결과.** seed에 따라 첫 candidate를 discard하고 두 번째 candidate를 promote하는 2-trial
시나리오, candidate 생성 실패 후 다음 trial 계속, trial/time budget 종료 뒤 terminal final 실행,
registry 거부 시 final 미실행, 동일 ledger 재개 시 planner memory 재생과 runner 무호출을 검증했다.
집중 검증 51개와 Research Harness 전체 `479 passed, 11 skipped`가 통과했고 저장소 전체 Ruff와
`git diff --check`도 통과했다. 아직 실제 coding agent와 `LocalRunner`를 조립하는 production
adapter, 재학습 `harness-predict` CLI, baseline sigma 실측은 없으며 각각 Task 6과 Task 7의
의도적인 후속 범위다.

---

## Task 6: 로컬 GPU·메타데이터·임베딩 연결 + CLI + MVP REPORT

**실행 기준 확정: 2026-09-03.** 정본은 spec 4.5절이다. 사용자 로컬 RTX 3070 Ti에서
사전학습 임베딩 모델 추론, CPU에서 LightGBM 학습, 로컬에서 Controller·Judge를 실행한다.
기존 21개 피처 구조와 LightGBM 설정을 출발점으로 삼되 로컬 임베딩을 사용하므로
운영 champion의 동일 재현이 아닌 Harness baseline으로 기록한다. 임베딩 파인튜닝은
초기 범위에서 제외한다. 아래 체크리스트에서 metadata 변환·v2 모델과 validation 게시·workspace를 구현했으며,
Task 6 전체 완료가 아니다.

**구현 순서:** 입력 계약 확정 → 로컬 피처·임베딩 → 재학습 CLI → 실행 연결·REPORT.

- [x] #40에서 metadata 정규화·시점 조인·v2 manifest의 RED 테스트를 먼저 작성했다.
      최초 테스트 전용 요청에서는 production 구현 없이 실패를 확인했다
- [x] 후속 구현 승인에 따라 같은 #40에서 정규화·시점 선택·v2 manifest 모델을 구현하고
      계약 테스트를 GREEN으로 전환했다. 후속 #42에서 validation 게시와 workspace까지 연결했다
- [x] 2026-09-03 GPU와 기존 Python 환경을 조회했다. RTX 3070 Ti, VRAM 8192 MiB,
      NVIDIA 드라이버 591.86, Python 3.12.13을 확인했다. 조회한 프로젝트 가상환경에는
      LightGBM·PyArrow가 있고 PyTorch·Sentence Transformers는 없다. GPU 인식과
      CUDA 텐서 연산 성공은 다른 검증이며, 후자는 아직 수행하지 않았다
- [x] [evaluation snapshot spec §18](../specs/2026-08-31-research-harness-evaluation-snapshot.md)에
      두 metadata 파일의 컬럼·시점·cold-start·v2 manifest·final 전달·재개 identity 목표를 확정했다
- [ ] CUDA 지원 실행 의존성을 준비하고 작은 텐서 연산과 실제 모델 추론을 검증한 뒤
      소형 로컬 모델 ID/revision·배치 크기·trial 시간 상한을 정한다. 초기 실험에 GCP는 사용하지 않는다.
      클라우드 자원 생성·계정 유료 전환·유료 API·크레딧 외 과금은 별도 승인 없이 하지 않는다
- [ ] 합성 fixture의 사용자 프로필·영상/채널 관측 정보를 candidate-safe 입력으로 추출한다.
      확정한 §18을 typed model·materializer·workspace에 구현한다. v1 manifest를 덮어쓰지 않고
      새 workspace에 v2를 게시한다. 문서 확정과 동작 구현을 구분한다
- [ ] 과거 action log와 메타데이터로 기존 피처를 조립한다. 학습 행에도 당시 사용 가능한
      정보만 쓰고, 운영 Feast와 기존 로컬 helper의 계산 차이를 확인한다. 평가 action log,
      생성 seed·설정, 반복 중 final 전용 목록은 전달하지 않는다
- [ ] 텍스트 묶음과 query/document 역할을 받아 벡터를 반환하는 최소 임베딩 interface와
      모델 설정으로 교체 가능한 로컬 adapter를 구현한다. 모델 로딩·배치·정규화는 adapter가
      맡고 Codex는 모델·입력 텍스트·유사도 계산을 변경할 수 있다. 범용 plugin 체계는 만들지 않는다
- [ ] 모델·revision·텍스트·역할·전처리·정규화별로 캐시를 구분한다. 모델 변경 시 사용자와
      카테고리 벡터를 함께 갱신한다. 평가에서는 준비된 모델 파일만 사용하며, 메모리 부족이나
      모델 부재를 다른 모델/클라우드로 조용히 우회하지 않고 실패로 기록한다
- [ ] `autoresearch/cli.py`에
      `harness-predict --slate <in> --out <out> --seed <n>`(candidate용)와
      `harness-run --judge-state-root <absolute-path>`(연구 실행) 추가
- [ ] `harness-predict` 기본 구현 — 위 Harness baseline 설정으로 주어진 seed에서 split·sampling·
      모델 초기화를 포함해 **재학습한 뒤** slate를 점수화한다. 고정 모델을 다시 점수화만
      하는 구현은 허용하지 않는다. **이것이 baseline이자 candidate가 고쳐 나갈 출발점이다**
- [ ] `report.py` — Trial Ledger와 final holdout evidence에서 `research-report.md`를 만든다.
      사람이 준 가설·`ExperimentCard`, trial·실패·복구 이력, validation/final 지표,
      최종 결론과 재현 좌표를 포함한다. 논문 출처와 9절 고정 형식은 로드맵 범위다
- [ ] 실제 coding agent·LocalRunner·Controller를 연결하고 모델 ID/revision 또는 파일 해시,
      임베딩 처리 설정·코드·데이터 버전·실행 장치·시간·실패를 재현 기록으로 남긴다.
      합성 환경 동작 증거를 실제 사용자 품질 개선이나 미측정 비용 절감으로 표현하지 않는다
- [ ] REPORT 결론은 final holdout의 유효한 비교 결과에 따라 `개선|개선 없음|판정 불가`
      중 하나이고, validation champion이 final에서 기각되면 `개선 없음`과 baseline 유지를
      명시한다
- [ ] `README.md`와 `.claude/docs/agent-project-reference.md`에
      `autoresearch/research_harness/` 추가 (CLAUDE.md 필수 규칙)
- [ ] `docs/README.md` 역할별 인덱스에 이 spec/plan 등재

**검증:**

- 입력: 시점 이후 메타데이터 배제, history cutoff·완전 라벨 상한 유지, final 목록 비노출,
  receipt 검증과 누락 정책을 테스트한다.
- 임베딩: 입력 순서·행 수·유한값·차원·정규화·query/document 역할·모델 교체 시 캐시 분리를
  interface를 통해 테스트한다. 가벼운 테스트 adapter 검증과 실제 GPU smoke 결과를 구분한다.
- 실행: seed별 CTR 재학습, 사전학습 임베딩의 안전한 재사용, OOM 실패 기록, 외부 API 없는
  실행을 검증한다. 지표·시간·메모리 수치는 측정 후에만 기록한다.

```bash
uv run python -m pytest -v
uv run --no-sync ruff check applications autoresearch tests tools
```

### Task 6 기준 변경 기록 — 문제·해결·결과

**문제:** 기존 계획은 운영 champion 재학습을 요구했지만 candidate 입력은 history와 slate에
한정되어 사용자·영상 피처 재료가 빠져 있었다. 기존 임베딩 호출은 Vertex AI에 연결되고
카테고리 캐시도 모델별 구분이 없어 모델 교체 실험을 그대로 지원하지 못한다. 근거는
`model_contract.py`, `assembly.py`, `embeddings.py`, `category_reference.py` 및 candidate view다.

**해결:** history-only 축소 모델 대신 안전한 메타데이터를 추가해 기존 피처 구조를 유지하고,
최소 임베딩 interface 뒤에 교체 가능한 로컬 adapter를 둔다. 사용자 보유 GPU를 우선 사용해
초기 GCP 의존성을 없앤다. 운영 모델의 동일 재현은 포기하지만 실험 자유도와 로컬 완주를
확보하는 선택이며, 운영 경로 일괄 변경과 임베딩 파인튜닝은 제외한다.

**결과:** 실행 기준에 이어 metadata v2의 목표 schema·전달·시점 계약을 문서로 확정하고
GPU/Python 환경을 조회했다. PyTorch와 Sentence Transformers가 없는 현재 환경에서
CUDA 추론 성공을 주장하지 않는다. 이 기준 확정 시점에는 typed schema도 없었으며,
후속 #40에서 최소 변환·모델만 구현했다. 모델 선택·의존성 설치·성능 및 비용
실측은 후속 작업이다. 조회 시 여유 VRAM은 다른 프로그램 사용에 따라 변하므로 고정 예산으로
쓰지 않는다. 모델·데이터 다운로드나 GCP 작업은 수행하지 않았다.

### Task 6 테스트 선행 기록 — #40 (2026-09-03)

**문제:** 문서만으로는 KST/UTC 시점 오류, 선호 카테고리 소실, v1/v2 혼용을 재현할 수
없었다. 제품 구현 전 기대 동작을 실행 가능한 입력·출력 계약으로 고정할 필요가 있었다.

**해결:** 작은 Arrow 원본과 독립 기대 schema를 작성하고, metadata 정규화·시점 선택·v2
manifest를 외부 interface로 테스트한다. 시점 직전/동일/직후, 입력 순서와 중복 요청,
duration 초 변환, 필수 컬럼·타입·중복키·경로·digest 오류를 포함한다. 성공 입력을 먼저
검증하여 모든 입력을 거부하는 구현의 거짓 성공을 막는다. 내부 파일 배치 알고리즘이나
제품 helper로 기대값을 계산하지 않으며, GPU와 외부 API는 사용하지 않는다.

**검증 결과:** 동일 lock의 기존 Python 3.12 dev 환경에서 다음 결과를 확인했다.

- 신규 4개 test module에서 **93개 수집·실행: 89 failed, 4 passed**. 실패는
  `candidate_metadata` 및 `CandidateDataManifestV2` 부재의 명시적 assertion이다.
  통과 4개는 기존 v1 호환성 2개와 테스트 입력의 기존 원본 schema 대조 2개다.
- 기존 `test_fixture_models.py`, `test_candidate_data_view.py`, `test_workspace.py`:
  **95 passed, 1 skipped**. skip은 Windows에서 POSIX executable-mode 검증 제외다.
- `python -m ruff check --no-cache applications autoresearch tests tools`와
  `git diff --check` 통과. 신규 테스트에는 skip·xfail·제품 stub을 추가하지 않았다.

신규 테스트 실행: `python -m pytest tests/research_harness/test_metadata_case_fixtures.py
tests/research_harness/test_candidate_metadata_normalization.py
tests/research_harness/test_candidate_metadata_as_of.py
tests/research_harness/test_candidate_metadata_manifest.py -q` (명령은 한 줄로 실행).
RED는 제품 동작 검증 성공이 아니며 main 병합 대상이 아니다. 테스트 선행 단계에서는 전체 pytest·CI를
실행하지 않았다. 실제 v2 파일 게시·누락 파일/digest 변조·final grant·동일 metadata 전달·
checkpoint 재개 검증은 materializer/workspace 구현 때 추가한다. 다음 작업은 이 테스트를
통과시키는 최소 정규화/시점 선택/v2 모델 구현이었으며, 아래 GREEN 단계로 이어졌다.
원본 작업 폴더의 기존 변경은 보존했다.

### Task 6 최소 구현 기록 — #40 GREEN (2026-09-03)

**문제:** 실행 가능한 계약은 마련됐지만 실제 metadata 변환·시점 선택·v2 manifest 모델이
없어 93개 테스트 중 89개가 실패했다. 관측 이전 정보 사용과 정상 cold-start의 구분이
필요하며 기존 v1 소비자를 바꾸지 않아야 했다.

**해결:** `candidate_metadata.py`의 세 함수에 Arrow 타입 검증·Pydantic 값 검증·허용 열
투영을 모았다. 정렬한 entity별 관측 시각 배열에서 이진 탐색하여 요청마다 과거 최신 행을
선택하고, 미관측은 null과 `metadata_missing`으로 남긴다. 별도 v2 모델은 기존 v1의
history cutoff 검증을 재사용하되 metadata receipt의 타입·고정 경로·digest를 엄격히
검증한다. 범용 저장소/adapter나 파일 게시를 함께 만들지 않은 것은 순수 계산 계약을 먼저
검증하기 위한 범위 결정이다. 기존의 잘못된 duration을 0으로 만드는 helper는 재사용하지
않고 fixture용 PT 정수 H/M/S만 엄격하게 파싱한다.

**트러블슈팅:** 최초 93개 GREEN 이후 UTC 변환과 Arrow→Python 변환의 날짜 범위 초과가
`OverflowError`로 빠져나가는 문제를 경계값 검증과 독립 리뷰에서 확인했다. 두 변환 경계에서
기존 `StageCError`로 바꾸고, 날짜/초 수 범위·null 목록 원소·중복 열·receipt 강제 변환 거부
테스트를 추가했다. 독립 리뷰 worker가 수정 후 UTC 상·하한 및 Arrow 범위 초과 재현을
확인했으며 최소 구현 범위의 미해결 발견 사항은 없었다.

**검증 결과:** 기존 93개 테스트를 모두 통과한 뒤 경계값을 더해 metadata 테스트
**116 passed**를 확인했다. 독립 리뷰에서도 그중 normalization/as-of/manifest의
**114 passed**를 별도로 실행했다(원본 schema 대조 테스트 2개 제외).
경계값 보강 전 전체 `tests/research_harness` 회귀는 **572 passed, 11 skipped**였으며,
저장소 전체 Ruff와 `git diff --check`도 통과했다. 전체 저장소 pytest와 이미지 빌드는
로컬에서 실행하지 않았으며 PR의 Linux CI 결과를 별도로 확인한다.

**남은 한계:** 이 결과는 순수 계산·모델 계약 검증이다. 실제 metadata 파일 게시·hash 변조
검증·workspace/final grant·checkpoint 재개 연결, 피처 조립·임베딩·모델 학습은 아직 없다.
모델 품질·자율성·비용 개선 수치를 측정하거나 주장하지 않는다. 모델 다운로드·GPU 의존성
설치·GCP 작업을 하지 않았으며, 다음 구현은 v2 materializer/workspace 연결이다.

---

### Task 6 validation metadata 게시·workspace 연결 — #42

- [x] 검증된 fixture 원본 receipt와 history/validation 요청으로 prepared byte bundle을 만든다.
- [x] v2 opt-in materializer에 atomic 게시·동일 target 재사용·변조/누락/alias 거부를 연결한다.
- [x] workspace opt-in 및 기존 view digest 전달, 오류 시 회수를 검증한다.
- [ ] 독립 리뷰·Harness 회귀·Ruff·CI를 확인하고 문제·해결·결과를 기록한다.

범위는 validation 파일 게시와 workspace 연결이다. final용 준비/grant, Controller
checkpoint 영속화, 피처 조립·임베딩·학습은 후속으로 분리한다. v1을 유지하고 같은 prepared
bytes를 paired workspace에 재사용한다. 검증은 작은 golden 선택 → 실제 fixture의 파일
게시/재사용/변조 → workspace 통합 → 전체 Harness 순으로 넓힌다.

**문제:** #41까지는 Arrow 변환과 manifest 모델만 있어 candidate worktree에 metadata 파일이
없었다. baseline과 candidate의 실행마다 metadata를 다시 계산하면 같은 평가에서도 입력이
달라질 여지가 있고, 단순 파일 추가는 기존 exact-tree 검증에서 거부된다.

**해결:** 검증된 fixture에서 허용 history impression/validation slate의 ID·최대 요청 시각으로
관측을 제한하고, 고정 writer 옵션으로 직렬화한 bytes/receipt를 불변 bundle에 담는다.
별도 v2 interface가 기존 lock·staging·atomic rename·파일 검증을 재사용한다. workspace는
bundle 제공 시에만 v2를 선택하며, 기존 view digest에 metadata receipt가 자동 포함된다.
작은 interface를 유지하기 위해 별도 범용 registry나 v1 복제 게시기를 만들지 않았다.

**트러블슈팅:** 최초 통합 테스트에서 staging의 metadata 인자 누락과 reuse 검증의 잘못된
인자를 잡았다(10 failed, 7 passed). 수정 후 최초 17개가 통과했다. 독립 리뷰에서는
파티션별 검증 후 필터하는 순서가 미허용/미래 행의 파티션 간 중복을 숨길 수 있음을 발견해,
전체 영상 이력의 중복을 필터 전에 검사하고 회귀 테스트를 추가했다. Windows 손상 fixture
복사 테스트는 긴 경로 제약으로 검증 전 실패하여 기존 긴 경로 helper와 짧은 임시 state root를
사용했다. 제품 검증을 느슨하게 하거나 손상 테스트를 skip하지 않았다.
전체 Harness 회귀에서는 신규 공개 함수·타입 5개가 exact export 기대 목록에서 빠진
테스트 1개만 실패하고 나머지 **617 passed, 11 skipped**였다. 기대 목록을 실제 공개
계약과 대조해 추가했고 동일성 assertion은 유지했다.
수정한 export 테스트와 metadata 통합의 재실행은 **24 passed**였다.

**결과·한계:** 독립 리뷰의 연결/중복 지적은 수정·재확인됐다. 실제 파일 게시, 두 workspace의
bytes 동일성, 파일 변조/누락/alias 및 부분 게시 거부, workspace 회수 테스트를 추가했다.
`python -m pytest tests/research_harness/test_candidate_metadata_view.py -q --tb=short`는
**23 passed**이며 신규 skip은 없다. 전체 저장소 Ruff와 `git diff --check`도 통과했다.
final metadata·권한 연결, checkpoint 영속화, 피처/임베딩·학습은 남아 있다. 품질·자율성·비용
개선은 아직 측정하지 않았고, GPU 설치·모델 다운로드·GCP 작업은 하지 않았다.

### 2026-09-03 goal 실행 순서 — Task 6 잔여와 Task 7

사용자가 1~5단계 전체의 구현·검증·머지를 goal로 승인했다. 의존성은 프로젝트 격리
환경에 설치하고 공개 사전학습 모델을 준비할 수 있다. 유료 API/GCP 자원 생성/시스템
드라이버 변경은 승인 범위가 아니다. 각 단계는 이슈 연결 브랜치와 작은 PR로 진행하며,
독립 리뷰 차단 사항이 없고 CI가 통과하면 별도 머지 승인 질문 없이 squash merge한다.

| 순서 | PR 범위와 완료 증거 | 상태 |
| --- | --- | --- |
| 1 (#44) | 로컬 21개 피처, 최소 embedding interface, 손 계산 및 시간 경계 테스트 | 진행 중 |
| 2 | 실제 로컬 GPU adapter, 모델 고정 revision/파일 해시, 캐시 분리, CUDA 추론·OOM 검증 | 대기 |
| 3 | 재학습 CLI, seed별 학습·예측, 입력 무결성과 완전 라벨 기간 검증 | 대기 |
| 4 | final 권한/metadata, checkpoint identity, 실제 coding agent·독립 Judge·REPORT 연결 (필요하면 여러 PR) | 대기 |
| 5 | 5회 독립 재학습 calibration, E2E·복구·판정 시나리오와 품질/자율성/비용 실측 | 대기 |

완료는 테스트 adapter의 성공이 아니라 실제 로컬 모델·agent의 완주와 증거 보존, 모든
관련 PR의 main 머지다. 성능 개선은 성공 조건으로 강제하지 않는다. 합성 환경 결과를
실제 사용자 성능이나 미측정 비용 절감으로 표현하지 않는다. Task 7의 parser 자원 실측,
final 1회 소비 및 checkpoint 검증도 유지하며 작은 부분 구현으로 전체 완료를 대체하지 않는다.

### Task 6 로컬 피처 조립 — #44

**문제:** metadata 파일을 candidate workspace에 게시할 수 있지만 CTR 학습 입력으로
조립하는 로컬 경로가 없다. 기존 assembly helper를 그대로 쓰면 Vertex AI 의존성과
wide-event 기반 view/전체 이벤트 수 근사가 들어간다. 기존 21개 컬럼명을 만드는 것만으로
시간 누수 방지나 운영 계산 동일성을 증명할 수 없다.

**해결 계획:** spec 4.6에 계산식을 고정하고, 순수 Arrow 조립 함수와 최소 임베딩
interface로 계산·추론을 분리한다. raw event_type을 직접 집계하며 KST 일 경계와
metadata as-of를 각각 검증한다. GPU/모델 설치·CLI·final 연결은 후속 PR로 분리한다.
운영 Feast import/조회나 범용 plugin registry를 추가하지 않는다.

- [x] 손 계산 21개 피처 golden을 구현 전 실행하여 RED 확인
- [x] 요청/이벤트 시점, KST 당일 제외, 7일/30일 경계, future metadata 불사용 검증
- [x] 빈 입력, cold-start, raw view count, category tie, 순서/중복 요청 검증
- [x] 임베딩 역할/차원/유한값/영벡터/정규화 실패 검증
- [ ] 독립 리뷰와 Harness 회귀·Ruff·CI 후 merge 및 결과 기록

**검증 중간 기록:** 구현 전 `test_local_features.py`와 `test_embedding.py`는 **22 failed**로
새 module 부재를 확인했다. 별도 실제 fixture→v2 Parquet→피처 통합 테스트도 module
부재로 **1 failed**였다. 손 계산 golden은 0초 view도 1건으로 세는지, 7일 watch 합 13,
전체 이벤트 5, 1/3·2/3 비율, 관측 기준 영상 나이 29일과 cosine 0.96을 검증한다.
이는 RED 증거이며 동작 성공이 아니다.

**트러블슈팅:** 30일 경계 테스트를 추가하며 영상 관측 시각만 과거로 옮겨 게시 시각보다
앞서게 만든 입력 오류를 확인했다(**1 failed, 68 passed**). 게시 시각도 명시적으로 더
과거로 옮겨 정상 입력에서 window 경계를 검증하도록 수정했다. 제품의 관측 검증을
완화하지 않았다. KST 날짜 변환의 최대 연도 overflow와 int64 시청시간 합 overflow도
안전한 계약 오류로 처리하며 회귀 테스트를 추가했다.
첫 전체 회귀는 수정 전 날짜 경계 입력을 수집한 실행에서 **1 failed, 687 passed,
11 skipped**였고, 실패는 위 영상 관측/게시 순서 오류였다. 파일 수정과 동시에 회귀를
실행한 검증 절차의 문제이므로, 최종 파일을 고정한 뒤 전체 회귀를 다시 실행한다.

**결과·한계:** 신규 세 테스트 파일을 실행해 **71 passed**를 확인했다. 독립 리뷰 worker도
동일한 최종 파일에서 71개를 직접 실행했고 차단 사항을 발견하지 못했다. 저장소 전체
Ruff와 `git diff --check`가 통과했다. 최종 파일을 고정한 전체 Harness 회귀는
**689 passed, 11 skipped (128.63초)**였다. skip은 기존 플랫폼 조건이며 신규 테스트에
skip을 추가하지 않았다. 전체 저장소 pytest/이미지 검증은 PR의 Linux CI에서 확인한다.
기존 helper의 근사와 달리 실제 v2 파일에서 raw view 수·시청시간·전체 이벤트 수를
검증했고, 첫 history 학습일에 이후 행동이 섞이지 않는 것도 확인했다. 임베딩 테스트
adapter는 의미 유사도나 실제 GPU 추론을 증명하지 않는다. CTR 재학습·실험 완주·품질과
자율성·비용 실측은 후속 단계다. 이 PR은 계측 가능한 실험 입력을 마련한 결과다.

재현 명령(저장소 dev 환경):

```bash
uv run python -m pytest tests/research_harness/test_local_features.py tests/research_harness/test_embedding.py tests/research_harness/test_local_feature_view.py -q
uv run python -m pytest tests/research_harness -q
uv run --no-sync ruff check applications autoresearch tests tools
git diff --check
```

## Task 7: 지표별 baseline σ 측정 + end-to-end 완주

앞선 Task가 전부 머지된 뒤에만 가능하다. σ 측정에 `harness-predict`(Task 6)와
slate(Task 1)가 모두 필요하기 때문이다.

- [ ] **지표별 baseline σ 측정** (D5) — validation slate에서 Task 6의 Harness baseline 설정을 seed
      5개로 **5회 독립 재학습**한 뒤 같은 slate를 점수화해 primary와 모든 guardrail의
      표준편차를 각각 구하고 ledger에 기록한다. 측정 전에는 `compare()`가 판정할 수 없다
- [ ] 측정된 지표별 σ map과 baseline 지표 절대값을 이 plan과 spec에 기록한다 — 이후
      실험의 기준선이다
- [ ] 5-seed 실측 분포와 의도된 개선·무변경 candidate 경로를 보고 `2σ/-1σ`,
      `σ > 1e-6`, 지표별 20%·30개 coverage 하한이 실용적인지 재조정한다. 변경이 필요하면
      승격 판정을 열기 전에 spec과 plan을 먼저 갱신한다
- [ ] 최악 길이 300,000행 predictions fixture로 parser wall-clock 10초·메모리 256 MiB와
      65 MiB artifact 상한을 실측한다. 시간·메모리 안에서 안정적으로 처리하지 못하거나
      실사용 slate 규모와 맞지 않으면 행 상한을 포함한 세 초기값을 함께 낮춰 spec과 plan을
      먼저 갱신한다
- [ ] 로컬 end-to-end 1회 완주 — slate 조립 → baseline 점수화 → Judge 판정 → ledger 기록
      → 피드백 반환 → 2차 trial
- [ ] **promote 경로 검증** — 일부러 개선된 candidate(유효한 피처 1개 추가)로 `promote`가
      실제 발생하는지 확인한다. 이걸 하지 않으면 승격이 없는 상태의 원인을 알 수 없다
- [ ] 중단 후 checkpoint 재개 1회 확인
- [ ] validation loop가 끝난 뒤 final holdout을 정확히 1회 평가하고 feedback 없이 종료되는지,
      새 run·새 ledger·동시 Controller에서도 온전한 같은 Judge 상태 루트의 registry가
      재평가를 막는지, root 부재·접근 불가·관측된 marker 삭제에서 fail-closed하는지, 최종
      REPORT evidence의 대표 수치가 final 결과인지 확인

**검증:** 전체 테스트 + 실제 1회 완주 로그와 ledger 산출물

---

## MVP 완료 조건

- [ ] 에이전트가 저장소 어느 파일이든 수정해도 harness가 차단하지 않는다
- [ ] candidate가 evaluator·테스트·split 코드를 고쳐도 판정 수치가 바뀌지 않는다
- [ ] candidate action log는 `dt < T`로 제한하고 metadata·임베딩 재료는 spec 4.5절을 따른다.
      평가 action log·원격 데이터 자격 증명·fixture 생성 상태와 seed는 주지 않는다. candidate가
      완전 라벨로 사용할 수 있는 마지막 출력일은 `T-2`다
- [ ] 평가 출력일 `[T, T_end]`의 click·귀속 후보 impression은
      `dt BETWEEN T AND T_end + 1`로 스캔하고 출력은 `[T, T_end]` impression으로 제한하며,
      `T_end + 1` 파티션 누락은 fail-closed한다
- [ ] `predictions.csv` 계약 위반이 지표 조작이 아니라 실행 실패로 처리된다
- [ ] `harness-predict`가 필수 `--seed`로 학습부터 실행하며 baseline 5-seed sweep은 5회
      재학습이다. 고정 모델 재점수화 구현은 계약 위반이다
- [ ] `score`는 `[0,1]` click 확률 추정치이며 범위 위반, metric `None`, σ·coverage 미달은
      `promote/revise/discard` 없이 fail-closed하고, 보정 품질은 LogLoss·Brier가 감시한다
- [ ] 판정 결과가 구조화된 피드백으로 에이전트에게 돌아가고, 다음 trial이 그것을 참조한다
- [ ] 프로세스를 중단한 뒤 마지막 checkpoint부터 재개된다
- [ ] 로컬에서 Kubernetes 없이 완주한다
- [ ] 지표별 baseline σ가 측정되어 ledger에 기록되고, 판정이 각 지표의 값을 입력으로 쓴다
- [ ] 일부러 개선된 candidate로 `promote`가 실제 발생함을 1회 확인했다 (D5 검증 의무)
- [ ] validation은 반복 피드백에 쓰고 final holdout은 마지막 1회·무피드백으로만 사용한다
- [ ] 필수 절대 `judge_state_root`가 없거나 접근 불가하면 final 평가를 시작하지 않고,
      온전한 같은 상태 루트에서는 `evaluation_id` marker가 남은 final holdout의 재소비를
      거부한다. 상태 루트 자체의 무결성은 위협 모델의 한계다
- [ ] `ResearchDomain` ABC와 `YouTubeCTRDomain`이 구현되고 Controller가 이 interface를 통해
      snapshot·검증·평가·비교를 호출한다
- [ ] 최종 REPORT의 대표 수치는 final holdout 결과다
