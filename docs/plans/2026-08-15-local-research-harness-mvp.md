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

이 계획과 spec의 공통 전제다. D1~D8은 승인된 확정 결정이며 구현 Task가 뒤집지 않는다.

| # | 결정 | 근거 |
| --- | --- | --- |
| D1 | **봉인 경계는 예측 점수 파일**이다. candidate가 `predictions.csv`를 산출하고, Judge는 숨긴 정답으로 채점만 한다. | Judge가 candidate 코드를 일절 실행하지 않으므로 봉인이 가장 깨끗하다. 모델 파일 역직렬화 위험도 없다. |
| D2 | **로컬 harness를 신규로 만든다.** 현행 K8s executor는 손대지 않고, 이후 `ExperimentRunner` 구현체로 흡수한다. | spec 4.4의 local-first. 지금 도는 폐루프를 깨지 않는다. |
| D3 | **verifier의 연구 공간 제한(path allowlist, dependency 금지, 변경량 상한)을 폐기한다.** candidate checkout 밖의 고정 Judge 판정 경계로 대체한다. | spec 4.2. 금지 목록은 봉인이 아니라 "약속"이며, 자율 시스템에서 약속은 보증이 아니다. 같은 UID에서의 완전한 기밀성·무결성 격리를 뜻하지 않는다. |
| D4 | **시크릿·자격증명 커밋 차단만 남긴다.** symlink·submodule·파일 크기·생성 데이터(`.csv/.pkl/.parquet`) 제한은 폐기한다. | 현행 `.csv/.parquet` 거부는 spec 4.2가 명시 허용한 "raw 데이터 재조립과 파생 데이터셋"을 그대로 막는다. 파생 데이터는 커밋이 아니라 workspace 산출물로 다루면 위생과 자유가 동시에 성립한다. |
| D5 | **판정 임계값은 고정 %가 아니라 baseline seed 노이즈 σ의 배수로 정의한다.** (구 M1) | 아래 "판정 규칙" 참조. |
| D6 | **`slate_id`를 action log 생성 단계에서 부여한다.** 사후 추론하지 않는다. 과거 파티션은 평가 대상에서 제외한다. (구 M2) | slate 경계는 노출 시점에만 존재하는 사실이고, 사후 추론은 언제나 근사다. NDCG는 slate 단위로 계산되므로 경계가 틀리면 지표가 **조용히** 틀리고 그 위의 모든 자율 판정이 함께 틀린다. 두 정의를 섞으면 D5의 σ 측정이 오염된다. |
| D7 | **평가 데이터는 `slate_id` 도입 이후 생성분부터 사용한다.** 개발·검증용으로는 `RuleBasedActionLogGenerator`로 로컬 생성한다. (구 M3) | 로컬에 action log parquet 스냅샷이 없음을 확인했다. rule-based 생성기는 LLM·API 키 없이 동작하므로 로컬 완주 원칙(D2)과 맞는다. |
| D8 | **KST 날짜 cutoff `T`로 candidate 데이터 접근을 제한한다.** candidate에는 Harness가 준 `dt < T` 로컬 action log만 두고, validation/final slate는 `dt >= T`에서 만든다. 원격 데이터 자격 증명은 주입하지 않는다. | raw action log에는 click 이벤트가 있어 평가 구간 파티션을 읽으면 현행 30분 join으로 정답을 복원할 수 있다. 시간 cutoff와 유저 80/20은 각각 정답 접근과 같은 유저 적응을 막으므로 둘 다 유지한다. |

### D1이 만드는 인터페이스

candidate가 지켜야 하는 계약은 **단 하나**다.

```text
입력  <workspace>/harness_in/slate.parquet                   라벨 없는 평가 slate
      <workspace>/harness_in/history/action_log/dt=...        dt < T 로컬 파티션만
출력  <workspace>/harness_out/predictions.csv                evaluation_id, slate_id, video_id, score
진입점 python -m autoresearch.cli harness-predict --slate <in> --out <out> --seed <n>
```

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
  않는다. Harness가 주입한 `dt < T` 로컬 파일만 읽을 수 있다. D3은 코드 수정 범위를 여는
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

**σ 측정.** validation slate에서 baseline(현행 champion 설정)을 seed마다 **독립적으로
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
Task 4 최종 수정 뒤 Research Harness 전체 회귀는 428개 통과·7개 환경 의존 skip이었고,
저장소 전체 Ruff 검사도 통과했다.

---

## Task 5a: LocalRunner

- [ ] `runner.py` — workspace에서 고정 진입점
      (`harness-predict --slate <in> --out <out> --seed <n>`)을 subprocess로 실행.
      timeout·자원 상한·stdout/stderr tail 수집. 실패는 reason code로 분류
      (`predict_timeout`, `predict_crash`, `invalid_predictions`, …)
- [ ] candidate를 새 process group/session으로 시작한다. 정상 종료·timeout·취소·예외 모두
      TERM grace 뒤 KILL과 최종 wait를 거쳐 child/grandchild까지 완전히 회수하고, 남은
      process가 있으면 실행 실패로 처리한다. 기존 패턴은
      `applications/experiment_platform/executor/codex_worker.py:537-574,624-670`이다
- [ ] 테스트: seed가 argv에 전달됨, 정상 실행, timeout, 비정상 종료, 산출물 미생성 각각의
      reason code와 timeout 뒤 grandchild process가 남지 않음을 검증한다

**검증:** `uv run python -m pytest tests/research_harness/test_runner.py -v`

---

## Task 5b: 자가 피드백 + Controller

- [ ] `feedback.py` — **에이전트에게 돌려주는 payload**:
      - validation primary/guardrail 지표 값과 champion 대비 방향 정규화 델타
      - `decision`과 `reason_code`
      - 이전 trial 이력 요약 (무엇을 시도했고 왜 기각됐는지)
      - 실패 시 stage + reason code + 로그 tail
      - **행 단위 정답과 지표 구현 코드는 포함하지 않는다**
- [ ] `controller.py` — Task 2d의 `ResearchDomain` interface를 주입받아 snapshot·candidate
      검증·평가·비교를 호출하고 예산(최대 시간/trial 수) 안에서 반복한다. 구체
      `YouTubeCTRDomain`의 slate/Judge 모듈을 Controller에서 직접 호출하지 않는다. spec 7장
      루프 구조를 따르되
      MVP에서는 사람이 준 가설과 `ExperimentCard`를 입력 seam에 주입한다. 다음
      단계에서는 Paper Discovery/Capability Matcher가 만든 `ExperimentCard`가 같은 seam을
      사용한다
- [ ] 실패 시 사용자에게 묻지 않고 다음 행동을 스스로 정한다(spec 7.2)
- [ ] validation loop 종료 후 champion을 고정하고 final holdout을 마지막 1회 평가한다.
      필수 절대 `judge_state_root`의 존재·접근을 확인하고 전역 registry marker를 fsync한
      뒤에만 실행한다. final 결과는 feedback을 만들지 않고 ledger/REPORT evidence에만 기록한
      뒤 종료한다
- [ ] final holdout의 유효한 판정이 `promote`면 `개선`, `revise|discard`면 baseline을
      유지하고 `개선 없음`, 유효한 비교를 만들지 못했을 때만 `판정 불가`로 결론낸다
- [ ] 테스트: seed-sensitive fake domain/runner로 2 trial 이상 루프가 도는지,
      피드백 payload에 라벨이 없는지, final 결과가 payload에 없는지, final 평가가 두 번째
      호출을 거부하는지, 예산 소진 시 정상 종료하는지

**검증:** `uv run python -m pytest tests/research_harness/ -v`

---

## Task 6: CLI 진입점 + MVP REPORT + 문서 갱신

- [ ] `autoresearch/cli.py`에
      `harness-predict --slate <in> --out <out> --seed <n>`(candidate용)와
      `harness-run --judge-state-root <absolute-path>`(연구 실행) 추가
- [ ] `harness-predict` 기본 구현 — 현행 champion 설정으로 주어진 seed에서 split·sampling·
      모델 초기화를 포함해 **재학습한 뒤** slate를 점수화한다. 고정 모델을 다시 점수화만
      하는 구현은 허용하지 않는다. **이것이 baseline이자 candidate가 고쳐 나갈 출발점이다**
- [ ] `report.py` — Trial Ledger와 final holdout evidence에서 `research-report.md`를 만든다.
      사람이 준 가설·`ExperimentCard`, trial·실패·복구 이력, validation/final 지표,
      최종 결론과 재현 좌표를 포함한다. 논문 출처와 9절 고정 형식은 로드맵 범위다
- [ ] REPORT 결론은 final holdout의 유효한 비교 결과에 따라 `개선|개선 없음|판정 불가`
      중 하나이고, validation champion이 final에서 기각되면 `개선 없음`과 baseline 유지를
      명시한다
- [ ] `README.md`와 `.claude/docs/agent-project-reference.md`에
      `autoresearch/research_harness/` 추가 (CLAUDE.md 필수 규칙)
- [ ] `docs/README.md` 역할별 인덱스에 이 spec/plan 등재

**검증:**
```bash
uv run python -m pytest -v
uv run --no-sync ruff check applications autoresearch tests tools
```

---

## Task 7: 지표별 baseline σ 측정 + end-to-end 완주

앞선 Task가 전부 머지된 뒤에만 가능하다. σ 측정에 `harness-predict`(Task 6)와
slate(Task 1)가 모두 필요하기 때문이다.

- [ ] **지표별 baseline σ 측정** (D5) — validation slate에서 현행 champion 설정을 seed
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
- [ ] candidate에는 `dt < T` 로컬 action log만 주입되고 `dt >= T` 평가 파티션과 원격
      데이터 자격 증명, fixture 평가 구간 생성 입력·seed는 주입되지 않는다. candidate가
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
