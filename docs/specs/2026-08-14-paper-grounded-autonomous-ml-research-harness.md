# 자율 ML 연구 Harness — YouTube 리랭킹 기반 MVP와 논문 로드맵

> 2026-08-14 | 상태: 제품 방향·상위 설계·구현 계획 확정, 구현 미착수
>
> 이 문서는 기존 executor의 다음 단계를 정의한다. 현재의 단일 가설 실행 계약을
> 폐기하지 않으며, 사람이 준 가설로 반복 실험을 완주하는 기반 MVP와 그 위에 놓일
> 논문 발견·출처 연결 제품 로드맵을 구분해 고정한다.

## 1. 목적

Autoresearch의 최종 목표는 CTR 도메인의 최고 성능 자체가 아니다. YouTube 트렌딩 기반
리랭킹을 구체적인 테스트베드로 삼아 다음 능력을 end-to-end로 증명하는 것이다.

> 사용자가 자연어 연구 목표와 실행 예산을 제출하면, 에이전트가 관련 논문을 스스로
> 발견하고 현재 저장소에서 검증 가능한 가설로 변환하여 코드 수정·학습·평가·실패
> 복구를 반복한 뒤, 모든 출처와 판단 근거가 연결된 최종 REPORT를 반환한다.

사용자가 CTR, 추천 지표, 모델 구조를 미리 알아야 하는 제품으로 만들지 않는다.
도메인별 기본 평가 방법과 안전 계약은 `ResearchDomain` 구현이 소유하며, 사용자가
확인해야 하는 것은 실행 도중의 승인 요청이 아니라 완주 후 REPORT뿐이다.

이번 MVP는 이 최종 목표의 실행 기반을 먼저 닫는다. 사람이 작성한 가설과
`ExperimentCard`를 입력받아 전체 저장소 수정, 반복 실험, 봉인 평가, 복구·재개,
Trial Ledger와 REPORT 생성까지 완주한다. 논문 자동 발견과 claim compiler, 논문 출처가
연결된 9절 REPORT, 웹 배선은 이 기반을 검증한 뒤의 로드맵이다.

## 2. 배경과 현재 한계

현재 저장소는 기준 SHA와 데이터 스냅샷을 고정한 뒤 Codex가 한 번 코드를 수정하고,
baseline/candidate를 학습·평가해 리포트를 남기는 경로까지 도달했다. 이 경로는 다음
기반을 이미 제공한다.

- 고정된 `base_dev_sha`와 실험 브랜치
- content-addressed 학습 데이터 스냅샷
- baseline/candidate의 동일 조건 비교와 다중 seed 실행
- 데이터·분할 fingerprint, 지표 JSON, 에이전트 작성 리포트
- 실패한 Job 회수, 실험 상태와 Step 관측

그러나 현재 실행 모델은 본질적으로 한 번의 `코드 수정 → 검증 → 학습 → 평가 → 보고`다.
에이전트가 결과를 관찰해 다음 가설을 선택하는 반복 연구 루프가 없고, 논문 발견·출처
검증·실험 이력에 기반한 후속 전략도 없다. 따라서 executor만 놓고 보면 범용 coding
agent나 기존 goal 실행과 구별되는 ML 연구 방법론이 부족하다.

관련 현행 계약과 실측은 다음 문서가 소유한다.

- [Auto Research 최소 흐름의 제약](2026-07-29-auto-research-minimum-loop-gaps.md)
- [실험 실행 능력 활성화 계약](2026-08-07-experiment-execution-enablement.md)
- [에이전트 작성 실험 리포트 계약](2026-08-09-agent-authored-experiment-report.md)
- [리랭킹 지표 정합](2026-08-04-reranking-metric-alignment.md)

이 문서는 위 계약의 현재 구현 사실을 반복하지 않고, 그 위에 놓일 자율 연구 계층을
정의한다.

## 3. 제품 계약

### 3.1 입력

MVP의 입력은 사람이 작성한 가설과 `ExperimentCard`, 실행 예산이다. `ExperimentCard`는
실험 의도, 검증 가능한 변경, falsification 조건을 구조화해 Controller에 전달하며,
MVP는 이 입력을 스스로 보완·수정하면서 반복한다.

MVP 이후 최종 제품에서는 사용자가 다음 두 종류의 정보만 제공한다.

- 자연어 연구 목표: 예) `YouTube 추천 목록의 리랭킹 품질을 개선한다.`
- 실행 예산: 최대 시간, 최대 trial 수, CPU/GPU 등 허용 자원

논문 shortlist, 모델 종류, 피처, primary metric, 통계 기법을 사용자에게 선택시키지
않는다. YouTube 도메인의 기본값은 `YouTubeCTRDomain`이 결정한다. 자연어 목표와 예산만으로
`ExperimentCard`를 만드는 경로는 논문 자동 발견·compiler와 함께 MVP 이후 로드맵이다.

### 3.2 실행 중 상호작용

연구 run에는 사람의 승인 gate를 두지 않는다.

- 논문 선택 승인 없음
- 가설 승인 없음
- 코드 변경 승인 없음
- 실패 복구 승인 없음
- candidate 채택 승인 없음

사용자는 상태를 관찰할 수 있지만, 관찰이 실행의 전제 조건이 되어서는 안 된다.
운영 배포나 production champion 전환은 연구 run 바깥의 별도 책임이며, 자율 연구의
완주를 중단시키지 않는다.

### 3.3 출력

MVP에서 사람이 확인하는 주 산출물은 `research-report.md` 또는 동일 내용을 가진 HTML
REPORT다. 다음 기계 판독 산출물도 함께 보존한다.

```text
research-report.md
experiment-ledger.jsonl
artifacts/
```

MVP 이후 논문 기반 로드맵은 `research-report.html`의 9절 계약과
`paper-manifest.json`을 추가한다.

모든 가설이 기각되어도 연구 run은 유효하게 완주할 수 있다. `개선 없음`은 실행 실패가
아니라 근거가 있는 연구 결과다.

## 4. 핵심 원칙

### 4.1 ML Harness가 연구 규칙을 소유한다

Codex는 논문 해석, 가설 구체화, 코드 변경을 담당한다. 데이터 고정, 실행 순서,
평가, 예산, 복구, 이력 보존은 결정론적인 Research Harness가 담당한다. LLM의 자연어
판단만으로 candidate를 승격하지 않는다.

### 4.2 연구 대상 전체는 수정 가능하다

에이전트는 제한된 피처 파일만 수정하는 것이 아니라 연구 대상 저장소 전체를 수정할 수
있다.

- raw 데이터 재조립과 파생 데이터셋
- 피처와 임베딩
- label과 auxiliary task
- sampling, weighting, loss, optimizer
- 모델 아키텍처와 학습·추론 코드
- 리랭킹과 calibration
- 의존성, lock 파일, 디렉터리 구조, CLI, 테스트

파일 allowlist를 연구 공간의 정의로 사용하지 않는다. Git commit 전체를 하나의
candidate genome으로 취급한다.

연구 공간 제한과 안전·저장소 위생 제한은 구분한다. 현행 verifier의 path allowlist와
dependency 변경 금지는 Research Harness에 승계하지 않지만, 시크릿·자격 증명이 candidate
commit에 들어가는 것은 계속 차단한다.

현행 verifier는 `.csv`, `.pkl`, `.parquet` 변경을 생성 데이터로 간주해 거부하므로, 위에서
허용한 raw 데이터 재조립과 파생 데이터셋 생성을 그대로 막는다. Harness는 확장자만으로
workspace 변경을 거부하지 않는다. 파생 데이터셋은 candidate commit이 아니라 disposable
workspace의 산출물로 생성·소비하고, 코드와 재현 계약만 candidate genome에 남겨 저장소
위생과 연구 자유를 함께 지킨다.

### 4.3 외부 심판은 candidate 변경과 분리한다

Research Harness와 Sealed Judge는 candidate commit과 분리된 평가 기준을 소유한다.

- 원본 데이터 snapshot과 fingerprint
- 숨겨진 evaluation slate와 정답
- 최종 지표 계산과 baseline/candidate 비교
- 시간·연산·저장공간 예산과 중단 조건
- 데이터 누수와 산출물 계약 검사
- Trial Ledger와 paper provenance
- 자격 증명과 네트워크 정책
- REPORT의 원본 증거

에이전트가 candidate workspace 안의 evaluator나 테스트를 수정하는 것은 허용할 수 있다.
Sealed Judge의 재평가 대상 artifact는 candidate가 라벨 없는 봉인 slate에 대해 산출한
**예측 점수 파일**이다. Judge는 이 파일과 숨긴 정답으로 채점만 하며 candidate 코드를
import하거나 실행하지 않고 모델 파일도 역직렬화하지 않는다. candidate 코드를 전혀
실행하지 않는 경계가 가장 깨끗하게 봉인되며 역직렬화 위험도 없기 때문이다. 실행 중인
Controller와 Judge는 고정 이미지·digest 또는 별도 프로세스에서 시작하므로, candidate
branch가 그 소스를 수정해도 정상 실행 경로의 현재 run 판정에는 반영되지 않는다.

MVP의 위협 모델은 **실수와 자기 채점 오염 방지**다. candidate와 Judge가 같은 OS 사용자로
실행되는 로컬 환경에서는 worktree 밖의 절대 경로를 아는 일반 subprocess가 Judge 소유
파일을 읽을 수 있을 뿐 아니라 수정·삭제할 수도 있다. 따라서 디렉터리 분리는 기밀성과
무결성을 보장하는 보안 sandbox가 아니며, Judge 파일을 candidate가 "수정할 수 없다"고
단언하지 않는다.
validation 정답과 final holdout slate·정답의 경로는 candidate의
argv·환경·prompt·feedback에 전달하지 않지만, 이 조치도 경로 추측이나
호스트 탐색 또는 같은 UID의 파일 변조에 대한 완전 격리를 보장하지 않는다.
로컬 fixture 평가 구간을 만드는 `RuleBasedActionLogGenerator`의 입력과 seed도 Judge 소유
경로에만 보관하고 candidate workspace·argv·환경에 전달하지 않는다. 같은 UID에서 호스트를
탐색하면 이 값과 final 소비 registry까지 찾거나 지울 수 있으므로, 이 분리는 재생성에 의한
정답 복원과 상태 삭제를 완전히 차단하는 보안 경계가 아니라 실수 방지 수준이다.

Judge가 candidate 경계에서 받는 것은 `predictions.csv` 하나뿐이다. Judge는 candidate
경로를 `O_RDONLY|O_NOFOLLOW`로 한 번만 열고, 검증한 **같은 FD**에서만 복사한다. 경로를
다시 열지 않는다. 복사 전후 `fstat`의 device·inode·mode·size·mtime을 비교해 교체·성장·
변조를 검출하고, **65 MiB + 1 byte probe**까지만 읽어 상한 초과를 확인한다. Judge 소유 목적지는
`O_CREAT|O_EXCL`로 만들어 기존 파일을 덮어쓰지 않으며, 이후 candidate 경로가 아니라 이
사본만 파싱한다. 이 65 MiB 상한은
candidate workspace나 commit의 파일 크기를 제한하는 D4를 되살리는 것이 아니라, 불신
프로세스가 Judge에 넘기는 단일 artifact의 메모리·디스크 소비를 제한하는 입출력 계약이다.

CSV parser는 header를 포함해 필드가 정확히 `evaluation_id,slate_id,video_id,score` 4개인지
확인한다. `evaluation_id`는 정확히 69 byte이고, `slate_id`·`video_id`는 comma·quote·개행
없는 ASCII로 각각 최대 64 byte, `score` token은 최대 24 byte로 제한한다. CRLF까지 허용한
한 행의 최악 크기는 `69 + 64 + 64 + 24 + comma 3 + CRLF 2 = 226 byte`이고 header는
최대 39 byte다. 따라서 유효 행 상한은 **300,000행**으로 둔다. 최악 크기
`39 + 300,000 * 226 = 67,800,039 byte`는 65 MiB(`68,157,440 byte`) 안에 들어가므로
artifact 상한과 모순되지 않는다. 행 수는 이 상한 이하이면서 대상 slate와 정확히 같아야
한다. 격리된 parser
subprocess에 wall-clock 10초와 256 MiB 메모리 상한을 적용하며, 어느 상한이든 넘으면
`invalid_predictions`로 실행을 실패시킨다. 행·시간·메모리 초기값은 Task 7 실측으로 함께
재조정한다.

candidate 실행의 고정 진입점은 다음과 같다.

```text
python -m autoresearch.cli harness-predict --slate <in> --out <out> --seed <n>
```

candidate는 주어진 seed를 split·sampling·모델 초기화를 포함한 학습 파이프라인에 적용해
모델을 새로 학습한 뒤 slate 예측을 산출한다. 이미 학습된 고정 모델을 seed마다 다시
점수화만 하거나 `--seed`를 무시하는 구현은 이 계약을 만족하지 않는다.

별도 OS 사용자, container, read-only mount를 이용한 적대적 탈출 방지는 MVP 이후의 완전
격리 단계로 남긴다.

### 4.4 로컬 실행이 기본이다

장시간 실행과 중단 후 재개는 Kubernetes만의 기능이 아니다. MVP는 다음 조합으로
로컬에서 완주할 수 있어야 한다.

- disposable Git worktree
- 격리된 subprocess 또는 container
- Harness가 주입한 로컬 데이터 snapshot
- 별도 Judge 프로세스
- 영속 Trial Ledger와 checkpoint

LocalRunner는 candidate를 새 process group/session으로 시작한다. 정상 종료·timeout·취소·
예외의 모든 경로에서 TERM grace 뒤 KILL과 최종 wait를 수행해 소유 group/Job을 상속한
child·grandchild를 회수하며, 남은 프로세스가 있으면 trial을 성공으로 기록하지 않는다.
재사용 출발점은 현행
Codex worker의 process-group 회수 계약이다
(`applications/experiment_platform/executor/codex_worker.py:537-574,624-670`).

Kubernetes Job은 다중 사용자 격리, 원격 자원, GPU scheduling이 필요할 때 선택하는
`ExperimentRunner` 구현체다. Kubernetes 배포 자체를 제품 차별점으로 삼지 않는다.

#### 4.4.1 CandidateWorkspace 계약

`CandidateWorkspace`는 저장소 root, 40자리 기준 commit SHA, 생성할 절대 경로와
validation `JudgeSnapshotHandoff`를 입력받는다. Harness는 기준 commit을 먼저 검증한 뒤
detached Git worktree를 만들고, Task 1-C의 `materialize_candidate_data_view()`를 호출해
그 root에 `harness_in/`을 게시하며 빈 `harness_out/`을 만든다. 호출자는 context manager
안에서만 workspace를 사용하고, 정상 종료와 예외 종료 모두에서 Harness가 자신이 만든
worktree를 회수한다. 이미 존재하는 대상 경로를 재사용하거나 삭제하지 않는다.

Candidate에 공개하는 실행 context는 worktree root,
`harness_in/slate.parquet`, `harness_out/predictions.csv`와 명시적 최소 환경뿐이다.
환경은 host 전체를 복사하지 않고 프로세스 실행에 필요한 OS 경로 변수와 Harness가 정한
고정 Python 설정만 allowlist한다. GCS·BigQuery·GitHub 등 원격 credential 변수, credential
파일 경로, `JudgeSnapshotHandoff`, labels, final holdout, fixture 생성 입력과 fixture seed는
validation context에 포함하지 않는다. 모델 재학습 seed를 argv에 넣고 실제 subprocess를 실행하는
책임은 `LocalRunner`가 소유한다.

반복용 workspace는 validation 전용이다. final holdout은 소비 registry를 선점한
Controller가 별도의 final 실행 경계에서만 주입한다. Task 3 당시에는 registry 권한을
흉내 내는 토큰이나 validation manifest의 split flag를 만들지 않았다. #49에서 실제
`FinalConsumptionGrant`를 요구하는 `open_final_candidate_workspace`를 별도로 추가한다.
정확한 metadata·게시 계약은 evaluation snapshot spec §18.9를 따르며, validation
진입점에는 final 선택 인자를 추가하지 않는다.

Candidate는 저장소 코드와 데이터 파일을 자유롭게 바꿀 수 있다. `harness_out`은
`predictions.csv`를 받아들이는 유일한 산출물 경계이지, worktree의 다른 변경을 금지하는
경로 allowlist가 아니다. commit 직전 검사는 현재 존재하는 변경·추가 파일의 내용에 기존
`contains_credential_value()`와 누락된 AWS access-key 형식 탐지만 적용한다. 삭제한
credential 문자열 때문에 변경이 거부되어서는 안 되며, `.parquet`, `pyproject.toml`,
symlink, dependency 변경을 별도 정책으로 차단하지 않는다. 이는 알려진 형식과 concrete
secret assignment를 찾는 MVP 검사이며 범용 secret classifier라고 주장하지 않는다.

diff fingerprint는 차단 판단이 아닌 ledger 증거다. 가변 `HEAD`가 아니라 봉인된 기준
commit에서 달라진 tracked 경로와 ignored 파일을 포함해 Git이 추적하지 않는 경로를 모은다.
base→index와 index→working tree를 각각 비교하고, 정렬된 경로별 index record와 현재
상태(존재/삭제)·mode·type·bytes를 길이 구분해 SHA-256으로 계산한다. gitlink는 index object
ID를 포함하고 초기화된 submodule은 staged gitlink와 HEAD 일치를 강제한 뒤 내부 dirty 상태를
재귀적으로 포함한다. 둘이 다르거나 변경된 gitlink를 검증할 수 없으면 fail-closed한다. Git patch
표현은 fingerprint 입력으로 쓰지 않는다. rename 탐지는 끄고 submodule ignore는 무효화하며
경로를 정렬하므로 repository의 diff 출력 설정과 무관하게 같은 변경은 같은 fingerprint를 만들고,
파일명·내용·삭제·mode 변경은 fingerprint를 바꾼다. credential 검사는 현재 bytes뿐 아니라
commit될 index blob에도 동일하게 적용한다.

위 값은 workspace inspection receipt의 fingerprint다. #52의 `PreparedCandidate`는
최종 commit될 staged patch의 SHA-256을 별도 diff fingerprint로 보존한다(§4.9).
credential 검사는 commit 전에 계속 전체 workspace/index를 대상으로 한다.

#### 4.4.2 LocalRunner 계약

Task 5a의 공개 경계는 동기식 `LocalRunner.run(request) -> LocalRunReceipt` 하나다.
`LocalRunRequest`는 `CandidateProcessContext`, 재학습 `seed`, wall-clock
`timeout_seconds`만 받는다. 호출자는 실행 파일·module·argv·환경을 바꿀 수 없다. Runner는
현재 Python interpreter로 아래 고정 명령을 조립하고, workspace가 만든 `cwd`와 최소 환경을
그대로 사용한다.

```text
python -m autoresearch.cli harness-predict \
  --slate <context.slate> \
  --out <context.predictions> \
  --seed <seed>
```

요청은 실행 전에 fail-closed 검증한다. `seed`는 bool이 아닌 0 이상 32-bit 정수이고
timeout은 유한한 양수다. `cwd`는 존재하는 절대 디렉토리, slate는
`cwd/harness_in/slate.parquet`, predictions는
`cwd/harness_out/predictions.csv`여야 한다. stale prediction이 이미 있으면 삭제하거나
덮어쓰지 않고 `runner_invalid_request`로 거부한다. 입력·출력 경로와 환경을 다시 선택하거나
host 환경을 합치는 기능은 제공하지 않는다. `CandidateProcessContext`는 자유 생성 가능한
dataclass이므로 provenance를 신뢰하지 않는다. Runner는 환경 이름의 중복, 이름·값의 NUL과 허용
값을 검사하고, 실행 시점 host의 OS 경로 allowlist와 고정
`PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1`로 다시 계산한 값과 정확히 일치할 때만
실행한다. trusted launcher는 candidate의 `Popen(env=...)` 경계에서 이 allowlist를 전달하며,
#52에서 ML 런타임을 위해 §4.9의 disposable HOME/USERPROFILE/TEMP/TMP/TMPDIR와 고정
USERNAME만 추가한다. 경로는 host에서 복사하지 않고 검증된 cwd의 새 출력 하위 경로로 고정한다.
Python candidate가 시작된 뒤 interpreter 자체가 locale 정규화를 위해 `LC_CTYPE` 같은 변수를
추가할 수 있으나, 이는 host 환경 상속으로 간주하지 않는다. `PYTHONPATH`, credential 변수와
그 밖의 추가 host 환경은 candidate 생성 경계에서 거부한다. candidate stdin은
항상 `DEVNULL`이며 Harness stdin을 상속하지 않는다. subprocess는 필요한 세 pipe 외의 handle을
상속하지 않도록 `close_fds=True`로 시작한다. trusted launcher의 stdin gate pipe만 parent와
launcher 사이의 추가 private handle이며 candidate에는 전달하지 않는다. stale output은
`Path.exists()`가 아니라 broken
symlink도 잡는 `lexists` 의미로 검사해 symlink·FIFO·directory를 모두 실행 전에 거부한다.

성공 receipt는 prediction 경로, exit code, 실행 시간(ms), stdout/stderr tail 문자열을 담는다.
출력 pipe는 별도 reader가 계속 비우되 decode 전 bytes 기준으로 각각 마지막 64 KiB만 보존하고
UTF-8 replacement decoding하므로 candidate의 많은 로그가 Harness 메모리를 무제한 소비하지
않는다. receipt와 error의 tail 필드는 dataclass `repr=False`이며 안전한 `str`/`repr`에 로그와
경로를 넣지 않는다. Task 5b만 explicit field access로 bounded feedback payload를 조립하고,
credential 형식과 control character를 정제한 뒤에만 외부 feedback 또는 ledger에 넣을 수 있다.
ledger 영속화 여부도 그 단계에서 별도로 결정한다. 성공은 parent exit code가 0이고 전체
process tree가 종료됐으며, 정확한 predictions 경로에 symlink가 아닌 regular file이 새로
생겼을 때만 가능하다. CSV schema·행 수·prediction 값의 의미 검증과 Judge 전용 copy는
`seal_prediction_copy()`의 책임이다.

안정된 실패 코드는 다음으로 제한한다.

| 코드 | 의미 |
| --- | --- |
| `runner_invalid_request` | seed·timeout·context·stale output이 계약을 위반함 |
| `runner_start_failed` | subprocess를 시작하거나 process-tree 격리 경계를 만들지 못함 |
| `predict_timeout` | wall-clock timeout을 초과함 |
| `predict_crash` | candidate parent가 0이 아닌 exit code로 종료함 |
| `invalid_predictions` | exit 0 뒤 prediction regular file이 없음 |
| `runner_process_leaked` | 정상 parent 선종료 때 descendant를 관측했거나 강제 회수 뒤에도 process가 남음 |
| `runner_cleanup_failed` | TERM/KILL/final wait 또는 output reader 정리를 완료하지 못함 |

공개 타입은 `RunnerErrorCode(StrEnum)`, `RunnerError`, `LocalRunRequest`,
`LocalRunReceipt`, 무인자 stateless `LocalRunner` 다섯 개다. request 필드는
`process: CandidateProcessContext`, `seed: int`, `timeout_seconds: float`다. receipt 필드는
`predictions: Path`, `exit_code: int`, `duration_ms: int`, `stdout_tail: str`,
`stderr_tail: str`이며 성공 exit code는 항상 0이다. error 필드는
`code: RunnerErrorCode`, `stage: str`, `exit_code: int | None`, `duration_ms: int`, 두 tail
문자열이다. pre-start validation 오류는 exit code `None`, duration 0, 빈 tail을 사용한다.
`RunnerError`는 이를 구조화해 Controller가 다음 행동을 정하게 한다. 문자열 표현에는 candidate
로그와 로컬 경로를 넣지 않는다. 예측 파일 내용이나 credential을 오류 문자열에 복사하지 않는다.

candidate는 새 process group/session에서 시작한다. POSIX는 session process group을 쓴다.
Windows는 candidate가 결속 전에 descendant를 만드는 race를 허용하지 않는다. trusted
launcher는 candidate workspace 밖의 Harness package에 있는 절대 script 경로를
`sys.executable -I <trusted-script>`로 실행하며, candidate module이나 `sitecustomize`를
workspace에서 import하지 않는다. launcher는 stdin gate에서 한 byte를 기다리고 그 전에는
candidate import·실행을 하지 않는다. parent는 launcher를 kill-on-close Job Object에 결속하고
launcher가 아직 살아 있음을 확인한 뒤 gate byte를 쓰고 pipe를 닫아야만 고정 candidate 명령을
시작한다. Job 결속, 생존 확인, gate write/close 중 하나라도 실패하면 candidate를 release하지
않고 launcher를 회수해 `runner_start_failed`로 끝낸다. candidate와 그 descendant는
breakaway를 허용하지 않는 같은 Job을 상속한다. launcher는
`CREATE_NEW_PROCESS_GROUP`으로 만들고 timeout·취소 때 `CTRL_BREAK_EVENT`를 group-wide
best-effort graceful 요청으로 보낸 뒤, grace를 넘기면 `TerminateJobObject`를 사용한다.
console이 없는 환경의 `CTRL_BREAK_EVENT` 실패는 force 종료와 active-process=0 확인이 성공하면
cleanup 실패가 아니다. 정상 parent
종료에도 active process count가 0인지 확인하고, termination 뒤에도 0이 될 때까지 bounded
wait한 후 마지막에 Job handle을 닫는다. Job handle은 start·success·failure·cancellation의
모든 경로에서 닫고 gate pipe도 start failure를 포함한 모든 경로에서 닫는다.

launcher는 candidate `Popen` 성공 여부를 candidate가 알지 못하는 parent 소유 임시 status
artifact에 `started|failed`로 한 번 기록한다. Runner는 이 private status를 확인해 launcher 내부
start 실패를 `runner_start_failed`로 분류하고, 정상적으로 시작한 candidate의 실제 exit 127은
`predict_crash`로 구분한다. status artifact 생성·읽기·정리 실패도 start/cleanup 실패 계약에
포함하며 오류 문자열에는 그 경로를 노출하지 않는다.

timeout·취소·내부 예외를 포함한 모든 종료 경로는 살아 있는 tree에 가능한 TERM-equivalent를
요청하고 짧은 grace 뒤 KILL-equivalent를 적용한 다음 final wait를 수행한다. Windows에서
이미 parent가 끝나 descendant만 남은 경우에는 일반적인 tree-wide graceful signal이 없으므로
grace 확인 뒤 Job 전체를 종료한다. 회수 실패는 성공이나 단순 candidate crash로 낮추지 않는다.

`run()`은 성공할 때만 receipt를 반환하고 계약상 실패에는 `RunnerError`를 발생시킨다. 복합
실패의 우선순위는 결정적이다. 실행 결과가 timeout·crash·invalid prediction이더라도 종료
API 오류, pipe reader 미종료 또는 final parent wait 실패면 `runner_cleanup_failed`가 우선한다.
종료 API가 반환했지만 소유 경계가 여전히 active process를 보고하면
`runner_process_leaked`다. 정상 parent 뒤 descendant를 발견해 전부 회수한 경우에도 candidate가
실행 계약을 위반했으므로 `runner_process_leaked`다. 이 둘이 없을 때만 primary
timeout·crash·invalid prediction을 반환한다. `KeyboardInterrupt`·`SystemExit`는 회수 후 원래
예외를 다시 발생시키며, 회수 실패 시 원래 예외에 경로·로그 없는
`runner_cleanup_failed` note를 붙여 두 사건을 모두 보존한다. 그 밖의 내부 `BaseException`도
같은 규칙으로 원래 예외를 보존한다.

MVP 자원 상한은 wall-clock timeout과 stdout/stderr tail 메모리 상한이다. prediction artifact는
후속 sealed ingestion의 파일 크기·parser 메모리 상한을 통과해야 한다. candidate 전체의
CPU·RSS·filesystem quota와 별도 OS 사용자/container에 의한 적대적 격리는 Task 5a의 로컬
실행 경계가 보장한다고 주장하지 않으며, 실제 E2E 측정 뒤 Kubernetes/container runner에서
보강한다.

POSIX 소유 경계는 새 session의 process group이다. Candidate가 의도적으로 `setsid()`·double
fork 등으로 새 session을 만들면 이 경계를 벗어날 수 있으며, Task 5a가 완전한 적대적 OS
격리를 제공한다고 주장하지 않는다. 이 탈출까지 막는 요구는 별도 OS 사용자와 PID namespace,
cgroup/container를 쓰는 후속 runner 범위다. Windows Job은 breakaway를 허용하지 않는다.

Task 5a는 실제 사용되지 않는 `CandidateArtifact`, `TrialResult`, 비동기
`ExperimentRunner` 계층을 미리 만들지 않는다. Task 5b Controller가 receipt와 ledger를 실제로
연결할 때 공통 runner Protocol을 가장 작은 소비자 인터페이스로 추출한다.

POSIX process-group과 Windows Job Object 경로는 각각 실제 subprocess 통합 테스트로
검증한다. 활성 GitHub CI(Ubuntu)는 POSIX의 즉시 grandchild spawn, 정상 parent 선종료,
timeout과 cancellation 회수를 실행한다. Windows 보장은 Windows 개발 환경에서 같은 네 경로와
Job 결속 실패 gate를 실행한 근거를 PR에 남긴다. 지원 OS의 해당 통합 검증 없이 process-tree
회수를 완료했다고 기록하지 않는다. 정상 parent 선종료 사례에서는 launcher가 candidate exit
code를 그대로 전달하고 candidate가 남긴 실제 grandchild만 group/Job에 남아야 한다. POSIX
회수 완료는 특정 grandchild의 wait syscall이 아니라 bounded poll 뒤 소유 process-group이
사라졌다는 사실로 검증한다.

### 4.5 Task 6 실행 기준 — 로컬 GPU와 교체 가능한 임베딩 (2026-09-03 승인)

이 절은 Task 6 구현의 승인된 목표이며, 아래 기능이 이미 구현되었다는 의미는 아니다.
기존 운영 학습·서빙의 Vertex AI 경로를 일괄 교체하지 않고 Research Harness에 적용한다.

**실행 자원과 비용.** 첫 개발·실험은 사용자 로컬 RTX 3070 Ti에서 임베딩을 계산하고,
LightGBM은 우선 CPU에서 학습한다. Controller·Judge도 로컬에서 실행하되 candidate와
코드·데이터 경로를 분리한다. 실제 GPU VRAM·드라이버·사용 가능한 메모리는 구현 전
조회하며, 모델 ID·revision·배치 크기·trial 시간 상한은 장치 확인과 smoke 측정 후 기록한다.
GPU 메모리 부족은 실패 근거로 남기고, 조용히 다른 모델이나 클라우드로 대체하지 않는다.
2026-09-03 장치 조회에서 VRAM 8192 MiB와 드라이버 591.86을 확인했다. 조회한 프로젝트
Python 3.12.13 환경에는 PyTorch·Sentence Transformers가 없어 실제 CUDA 추론은 검증 전이다.
GPU 인식만으로 모델 적재 가능 크기나 처리량을 확정하지 않는다.

GCP 신규 가입 무료 체험 크레딧은 초기 실행의 필수 자원이 아니다. 이번 승인은 GCP
자원 생성·유료 계정 전환·유료 API 호출을 허용하지 않는다. 향후 CPU 실행이나 저장 공간이
필요하면 크레딧 적용 대상·잔액·만료·예산을 확인하고 별도로 승인받는다. 크레딧 외 과금은
허용하지 않는다. Codex 사용료가 GCP 크레딧으로 충당된다고 가정하지 않는다.

**출발 모델.** baseline은 기존 `MODEL_FEATURE_COLUMNS`의 21개 피처 구조와
LightGBM 설정을 출발점으로 사용하되, 임베딩은 사전학습된 소형 로컬 모델로 계산한다.
이것은 **Harness 전용 baseline**이며 기존 운영 champion의 동일 재현으로 표현하지 않는다.
seed마다 CTR 모델의 split·sampling·초기화·재학습을 수행한다. 초기 범위에는 임베딩 모델
자체의 파인튜닝을 포함하지 않으며, 동일 입력·모델의 결정적 임베딩은 재사용할 수 있다.
Task 7의 지표별 sigma도 이 baseline에서 실측한다. 기준 모델·데이터·실행 조건을 바꾸면
이전 sigma를 무조건 재사용하지 않고 재측정한다.

**임베딩 interface.** 피처 조립과 임베딩 실행 사이에 교체 가능한 seam을 둔다. 최소
interface는 텍스트 묶음과 query/document 역할을 받아 입력 순서에 맞는 벡터를 반환한다.
adapter는 모델 로딩·역할별 입력 처리·배치 추론·정규화를 맡는다. 벡터는 유한한 값이어야
하고 동일 설정 안에서 차원이 일관되어야 하며, 코사인 계산용 정규화와 영벡터 오류 처리를
명시한다. 모델 간 차원을 일괄 고정하지 않는다. 구체적인 Python 타입·오류명은 구현 설계에서
확정한다. 초기에는 모델 설정을 바꿀 수 있는 로컬 adapter부터 만들고 범용 plugin 체계를
추가하지 않는다.

Codex는 임베딩 모델뿐 아니라 입력 텍스트 구성·유사도 계산 방식도 변경할 수 있다.
interface는 기본 교체 지점이지 수정 파일 allowlist가 아니다. 한 trial은 하나의 핵심
가설로 설명하는 것을 기본으로 하며, baseline A와 candidate B의 모델이 달라도 된다.
각 조건 내부에서는 호환되는 동일 모델·revision·역할 설정으로 사용자와 카테고리 벡터를
만든다. 모델 변경 시 양쪽을 다시 계산하고 CTR 모델도 재학습한다.

모델 다운로드는 준비 단계에서 수행하고 평가에서는 준비된 모델 파일을 사용한다. 모델 ID,
고정 revision 또는 파일 해시, 텍스트 전처리·역할 설정, 정규화, 코드·데이터 버전,
실행 장치·시간·실패를 재현 기록으로 남긴다. 캐시 identity에는 모델·revision·입력 텍스트·
역할·전처리·정규화를 반영하여 이전 모델 벡터가 섞이지 않게 한다. Judge의 예측 CSV·지표·
숨긴 정답 계약은 임베딩 교체에 따라 바뀌지 않는다.

**로컬 입력 확장.** 첫 검증은 기존 합성 fixture에서 허용된 사용자·영상 메타데이터를
별도로 추출해 과거 action log와 조립한다. 필요한 재료는 다음과 같다. 이는 입력 의미의
요약이며, 정확한 파일명·컬럼·manifest 목표는 evaluation snapshot spec §18을 따른다.
#40에서 typed schema·순수 정규화·시점 선택, #42에서 validation용 파일 게시·workspace
opt-in 연결을 구현했다. #44/#46/#48에서 피처·GPU 임베딩·재학습 CLI를 연결했다.
#49의 final 전달 계약은 evaluation snapshot spec §18.9를 따르며 checkpoint 영속화는 후속이다.

- 사용자: ID, 나이, 직업, 시청 시간대, 관심 키워드, 선호 카테고리와 사용 가능 시점.
- 영상·채널: 영상 ID, 카테고리, 길이, 게시 시각, 조회·좋아요·댓글 수, 채널 구독자·
  조회·영상 수와 관측 시점. 평가 영상뿐 아니라 과거 행동의 카테고리 조립에도 사용한다.
- 임베딩 재료: 허용된 관심 키워드, 카테고리 설명, 준비된 모델 파일과 그 버전.
  평가 클릭·라벨로 임베딩을 만들거나 fixture 생성 상태를 통째로 복사하지 않는다.

각 학습·예측 행은 해당 시점에 사용 가능했던 메타데이터만 사용한다. 공통 카탈로그와
평가 대상 목록을 구분하고, 반복 trial에 final 전용 목록이나 이를 드러내는 추출 묶음을
주지 않는다. 기존 action log의 `dt < T`, 완전 학습 라벨의 출력일 상한 `T-2`, final의
마지막 1회 소비 규칙은 유지한다. 메타데이터 허용은 생성 seed·설정·평가 action log 공개가
아니다. 합성 결과는 실험 루프의 동작 증거이며 실제 사용자 성능 개선의 증거가 아니다.

현재 history/slate 전용 candidate view에 파일을 임의로 추가하지 않는다. 확정한 목표 계약은
[evaluation snapshot spec §18](2026-08-31-research-harness-evaluation-snapshot.md)에 있다.
candidate view v2의 manifest/version·시점 선택·누락 처리·해시·final 전달을 함께 구현하고
테스트한다. 기존 피처 조립 helper가 운영 Feast의
계산과 동일한지도 대조하여, 이름만 같은 21개 컬럼을 만들고 재현했다고 주장하지 않는다.

### 4.6 Task 6 로컬 피처 조립 계약 (#44)

이 절은 로컬 Harness baseline의 계산 계약이다. 운영 Feast/BQ를 호출하거나 운영
champion을 재현하지 않는다. 공개 모델 입력 순서는 기존
`feature_engineering.model_contract.MODEL_FEATURE_COLUMNS`의 21개 컬럼을 유지한다.
사용자/영상 ID와 시각, 라벨, missing 진단은 모델 입력에 넣지 않는다.

**입력과 출력.** `research_harness.local_features.build_local_features`는 요청 Arrow
table과 허용 history, 정규화된 사용자/영상 metadata, 임베딩 adapter, history 시작일과
평가 시작일을 받는다. 요청의 `user_id`, `video_id`, UTC-aware `event_timestamp`를
기준으로 계산하고 순서·중복 요청을 보존한다. 결과는 21개 피처 table과 같은 순서의
진단 table을 분리한 `LocalFeatureBatch`다. 파일·receipt 검증과 학습 라벨 생성은
이 순수 계산 interface의 책임이 아니다. 상위 loader가 검증된 파티션 기간을 전달해야
하며, history의 중복 event ID·잘못된 타입/값·허용 기간 밖 이벤트는 실패시킨다.

**시점과 행동 집계.** 요청 시각을 KST 날짜로 바꾼 뒤 그날 자정 `d`를 구한다.
최근 행동은 `[d-7일, d)`, 선호 이력은 `[d-30일, d)`이며 당일 행동은 제외한다.
history는 항상 평가 시작일 `T` 미만이다. 평가 기간 후반의 요청에도 평가 행동을
추가하지 않는다. 사용자/영상 metadata는 요청 시각 이하 최신 행, 과거 반응의 영상
카테고리는 그 **반응 이벤트 시각 이하** 최신 행만 사용한다.

| 피처 묶음 | 로컬 baseline 계산 |
| --- | --- |
| 정적 사용자 3개 | 나이를 10s/20s/30s/40s/50s+로 구분, 직업 원본 보존, 시청 시간대는 기존 morning/evening/night alias와 unknown 규칙 적용 |
| 최근 행동 5개 | raw click/view/like 각각의 개수, view의 watch_time_sec 합, impression을 포함한 전체 이벤트 수 |
| historical_category_affinity | 30일 raw click/view/like의 최빈 카테고리, 동률은 카테고리 문자열 오름차순, 미관측은 unknown |
| 영상·채널 9개 | 관측된 category/duration/view/channel 값, like와 comment를 view로 나눈 비율(분모 0은 0), 관측 available_at과 published_at의 KST 날짜 차이 |
| 상호작용 3개 | 키워드별 query 벡터와 카테고리 설명 document 벡터의 최대 cosine(소수 4자리), 원본 primary_categories 포함 여부, 과거 선호와 영상 카테고리 일치 여부 |

**기존 경로와의 차이.** `assembly.compute_point_in_time_user_features`는 wide event에서
시청 시간을 이용해 view 수와 전체 이벤트 수를 근사한다. 이 경로는 long-format의 실제
event_type을 센다. 운영 BQ 집계는 snapshot 이전 최신 영상 카테고리를 과거 반응에
조인하지만, Harness는 각 반응 시각 이하 관측만 사용하여 과거 카테고리를 소급하지 않는다.
영상 비율은 기존 DuckDB helper의 소수 4자리 사전 반올림 없이 계산한다.
운영 `jobs.feature_store_build`는 영상 나이에 collected_at의 UTC 날짜를 쓰지만, v2는
collected_at과 trending 시각 중 최댓값인 available_at만 전달하므로 해당 관측의 KST
날짜를 쓴다. 요청일을 사용해 오래된 영상 관측의 나이를 매번 갱신하지 않는다.
이 차이들은 로컬 baseline의 명시적 정의이며 운영 동일성이나 품질 향상을 뜻하지 않는다.

**누락과 임베딩.** 정상 미관측에는 모델 계약의 범주형 unknown/수치형 0을 적용한다.
빈 관심 목록·미관측 영상의 similarity는 0이며, 임의의 기본 카테고리로 임베딩하지 않는다.
원본 선호 카테고리를 키워드 fallback으로 덮어쓰지 않는다. 임베딩 adapter의
`encode(texts, *, role)`은 query/document 역할과 입력 순서를 보존하는 2차원 NumPy
배열을 반환한다. 소비 helper가 행 수·양의 차원·유한값·영벡터 여부를 검사하고 L2
정규화를 수행하며, 두 역할의 차원도 일치해야 한다. 모델별 차원은 고정하지 않으며
잘못된 벡터를 0으로 숨기지 않는다.
테스트 adapter와 실제 GPU adapter의 검증 결과를 구분한다.

**진단.** 각 요청의 사용자/영상 metadata 미관측 여부와 7일/30일 history 기간 충족 여부를
별도로 반환한다. 기간 충족은 요청 window가 전달받은 history 기간 안에 있다는 뜻이지
사용자 활동이 존재한다는 뜻이 아니다. 기간 밖의 짧은 history를 완전한 관측으로 표시하지
않는다. 상위 재학습/REPORT 경로가 이 진단을 집계·보존한다.

### 4.7 로컬 GPU 임베딩 adapter와 캐시 (#46)

`research_harness.local_embedding`은 §4.5의 실제 모델 실행 adapter다. 설정은 모델
ID·고정 revision·준비된 모델 디렉터리·캐시 디렉터리·실행 장치(cpu/cuda)·배치 크기·
최대 토큰 길이·query/document 접두어·텍스트 전처리를 명시한다. 기본 역할 접두어는
E5의 `query: `와 `passage: `이며 다른 모델은 그 모델에 맞게 설정을 바꾼다.
MVP는 정규화된 dense sentence embedding만 지원하고 파인튜닝·원격 API는 지원하지 않는다.

Sentence Transformers를 **local_files_only**, **trust_remote_code=False**,
**safetensors 사용**으로 로드한다. 모델과 의존성 다운로드는 준비 단계에서 수행하고,
MVP 입력은 safetensors 전용 snapshot이다. 하위 Dense 모듈의 legacy weight fallback도
막기 위해 `.bin`, `.pt`, `.pth`, `.pkl`, `.pickle` 파일이 섞이면 loader 호출 전에
거부한다(대소문자 무관). 원본을 삭제·변환하지 않으며 SentencePiece tokenizer의
`.model`은 허용한다. 임의 custom checkpoint 형식 지원은 범위 밖이다.
adapter는 파일 부재를 네트워크 요청으로 해결하지 않는다. GPU 없음·CUDA OOM·모델 로딩
오류를 안전한 고정 code의 실패로 전달하고 CPU/다른 모델/cloud로 자동 재시도하지 않는다.
OOM 검증을 위해 실제 장치를 고의로 고갈시키지 않고, 실제 GPU 성공 smoke와 주입한
OOM 실패 경로 테스트를 별도의 증거로 남긴다.

**identity와 캐시.** 모델 디렉터리의 모델 실행 파일 목록과 SHA-256, model ID/revision,
역할 접두어, 최대 길이, 전처리 버전, L2 정규화 정책, 장치와 실행 라이브러리 버전을
설정 identity에 포함한다. 로컬 절대 경로는 재현 identity에 넣지 않는다. 각 텍스트의
캐시 key는 이 identity와 원본 텍스트·query/document 역할로 정한다. 모델 또는 처리
설정을 바꾸면 양쪽 역할의 cache namespace가 함께 바뀐다. pickle 기반 캐시는 사용하지
않고, 읽은 벡터의 차원·유한성·정규화를 검사한다. 손상된 캐시를 조용히 사용하거나
다른 모델의 벡터로 채우지 않는다. 동일 입력의 중복은 한 번 계산하고 반환 순서는 유지한다.
캐시는 파생 산출물이며 Judge 정답이나 평가 판정의 정본이 아니다.

**자원과 검증.** 로컬 GPU 의존성은 기본 dev/배포 환경과 분리한 선택 그룹으로 관리한다.
Windows baseline은 CUDA 12.8 wheel을 사용하여 시스템 CUDA/드라이버를 변경하지 않는다.
첫 후보는 한국어를 포함하는 `intfloat/multilingual-e5-small`, revision
`614241f622f53c4eeff9890bdc4f31cfecc418b3`이다. 이 값은 Hub 조회로 확인한 준비 좌표이며
추론 성공·적합성의 증거가 아니다. 실제 tensor 연산, query/document 추론, 반복 cache
hit, 처리 시간·GPU 할당 peak를 측정하고 모델 파일 해시와 함께 기록한다. 단위 테스트는
모델을 다운로드하지 않고 loader/추론 seam을 대체한다. 재학습·E2E·품질 판정은 후속이다.

근거: [E5 모델 카드](https://huggingface.co/intfloat/multilingual-e5-small),
[SentenceTransformer 로딩 계약](https://sbert.net/docs/package_reference/sentence_transformer/model.html),
[PyTorch 2.10 CUDA wheel](https://pytorch.org/get-started/previous-versions/#v2100).

### 4.8 로컬 재학습 CLI (#48)

`python -m autoresearch.cli harness-predict --slate <in> --out <out> --seed <n>`은
seed별 새 학습과 예측을 수행한다. 선택 `--config`의 기본값은 실행 디렉터리의
`harness_config.json`이다. 이 로컬 JSON은 `embedding` 설정(§4.7의
`LocalEmbeddingConfig`)과 선택 `training` 설정을 담는다. 고정 runner argv와 최소
환경을 유지하며 모델 준비 좌표를 새로운 필수 환경 변수나 data manifest에 넣지 않는다.
설정의 상대 모델/캐시 경로는 설정 파일 디렉터리 기준으로 해석한다. config와 모델·캐시,
생성 산출물은 공개 커밋 대상이 아니다.

**입력.** slate 옆 `candidate-view.json`의 v2 manifest를 요구한다. manifest가 지정한
slate/history/users/videos를 같은 bytes에서 SHA-256·행수·스키마 검증 후 파싱한다.
허용 루트 밖 경로, 링크/alias, manifest 불일치, 중복 이벤트, KST 파티션과 timestamp
불일치는 실패다. history는 시작일부터 `T-1`까지 연속이어야 하며 `T-2` 이하의 학습일이
하나 이상 있어야 한다. 비어 있는 파티션은 존재하는 파일로 증명하고 누락과 혼동하지 않는다.
완전 라벨 출력 범위는 `[history 시작일,T-2]`, attribution 입력의 마지막 날은 `T-1`이다.
기존 `attribute_clicks`의 최신 직전·30분 경계 포함·동시 시각 제외·event_id 동률 해소와
slate 일치 계약을 재사용한다. `T-1`의 더 최근 impression이 click을 차지할 수 있으므로
먼저 출력일 impression만 남겨 귀속하지 않는다. MVP 학습은 slate_id가 있는 이력만
지원하고 없는 legacy 이력은 명시적으로 실패한다. 평가 action log/라벨은 읽지 않는다.

입력 검증을 GPU 적재보다 먼저 수행한다. 검증한 Arrow 입력에서 §4.6의 과거 시점 피처를
조립하며 ID·label·diagnostics는 모델 입력에서 제외한다. 지표 계산용 정답을 후보에 전달하는
경로는 추가하지 않는다. 읽은 안전한 입력은 메모리에 보관하므로 검증 뒤 파일을 재읽지 않는다.

**학습.** 기존 `LGBMModel`과 negative sampling 계산을 재사용하되 운영 `train.main`의
MLflow/등록/원격 게시 orchestration은 호출하지 않는다. 기본 설정은 트리 200개,
learning rate 0.05, leaves 31, sampling 1.0, scale_pos_weight auto다. 기존 학습과 같이
stratified test 20% 분리 후 남은 데이터의 25%를 validation으로 분리한다(60/20/20).
같은 seed를 split/sampler/model에 전달하며 `0 <= seed <= 2**32-1`이다. 전체 및 각
split에 양·음성이 있어야 한다. train/validation union으로 categorical vocabulary를
고정하고 평가 slate의 미관측 값은 missing으로 처리한다. 내부 test는 이번 단계에서
변환·점수화하지 않으며 향후 소비할 때도 같은 vocabulary를 사용해야 한다. 평가 데이터로
vocabulary를 확장하지 않는다. 내부 validation/test는 학습 fit에 넣지 않으며 이들의
모델 선택/성능 보고를 이번 단계에서 추가하지 않는다.

매 호출 새 모델을 생성해 fit한다. sampling 1.0은 no-op이라고 기록하고, 더 작은 비율을
설정하면 train에만 적용해 scale_pos_weight=1 및 기존 실현 비율 확률 보정을 함께 적용한다.
다른 수치 weight와의 이중 보정 설정은 실패다. sampling off의 auto weight는 train의
negative/positive 비율이다. 같은 seed 재현 및 다른 seed split 변경을 검증하되 다른 seed의
예측이 반드시 달라야 한다고 단언하지 않는다. 모델·split 캐시나 이전 모델 load는 없다.

**출력·재현.** 기존 exact CSV `evaluation_id,slate_id,video_id,score`와 `[0,1]` 유한
확률 계약을 유지한다. 출력 stem에 `.model.txt`, `.training.json`을 붙인 native 모델과
receipt도 보존한다. 기존 파일을 덮어쓰지 않는다. 세 산출물 중 일부만 생긴 실패를 성공으로
취급하지 않으며 CSV는 마지막에 게시한다. receipt는 seed·입력 manifest digest·split별
행수/원천 event ID digest·sampling 실현값·모델 text hash·categorical 목록·피처 진단·
embedding manifest/identity·실행 라이브러리·시간을 담고 로컬 절대 경로를 담지 않는다.
모델 text의 확률을 재현할 때 sampling 보정은 receipt의 실현값을 함께 적용해야 한다.
실제 CLI smoke와 fake adapter 테스트는 구분하며 5-seed 품질/σ calibration은 후속 단계다.
`duration_seconds`는 설정 검증부터 예측 준비까지이며 Python import와 파일 게시 시간은
포함하지 않는다(`timing_scope=prediction_call_before_publication`).
`training_duration_seconds`는 피처 조립과 학습/예측을 함께 포함한다. 프로세스 전체 시간은
후속 LocalRunner receipt에서 측정한다. 실패는 경로·원문 입력을 노출하지 않는 고정 코드와
nonzero exit로 전달한다.

### 4.9 실제 agent 실행과 불변 run 입력 (#52)

기존 `ResearchController` 정책은 유지하고 실제 `ResearchTrialRunner` adapter를 연결한다.
한 run은 초기 card, budget, baseline/champion SHA, screening/confirmation seeds, baseline
sigma, Judge handoff와 registry 위치, runtime 설정을 처음 실행 전에 고정한다.
`RunInputContract`는 이 값들을 명시적 typed field로 가지며 runtime 설정만 canonical JSON
문자열로 묶는다. runtime JSON은 호출자가 strict 설정 모델로 검증한 값이며 임의 환경 변수나
자격 증명을 포함하지 않는다. 임베딩 모델 파일 identity와 Codex 모델·reasoning effort,
실행 timeout도 이 설정에 포함한다.

`freeze_run_inputs(root, *, contract, validation_metadata, final_metadata)`와
`load_run_inputs(root, *, expected_contract)`는 Judge-owned run root의 `run-inputs/`에서
두 metadata bundle과 manifest digest 및 ledger artifact evidence를 반환한다.
manifest와 validation/final 각각 users/videos Parquet의 다섯 파일만 게시한다.
metadata는 원래 bytes 그대로 보존하고 schema·행수·receipt·split identity를 대조한다.
신규 게시는 lock/staging/rename/fsync, 재사용은 exact-tree·canonical manifest·digest
검증을 거친다. 다른 계약이나 bytes, 파일 누락·추가·alias는 실패한다. 이 디렉터리를
fixture root나 candidate checkout 안에 두지 않는다. 재개 시 원천 metadata를 재조회하지
않고 저장 bytes를 복구한다. 호출자는 Controller 실행 전에 `run-inputs` checkpoint의
manifest digest를 대조/기록한다. 게시 후 checkpoint 전 중단은 동일 게시물을 검증하여
checkpoint만 보완한다. Controller의 ledger schema는 확장하지 않는다.

실제 coding agent는 기존 ChatGPT 로그인으로 Codex CLI의 새 ephemeral 실행을 사용한다.
명시적 model/effort와 `--ignore-user-config`로 실행 설정을 고정하고 workspace-write,
JSON 이벤트와 구조화된 최종 응답을 사용한다. 이 옵션은 저장 로그인 자체를 제거하지 않는다.
비대화형 호출의 approval policy는 `never`로 명시한다. native Windows에서는
`windows.sandbox="elevated"`를 호출 인자로 명시하여 개인 설정을 읽지 않아도 기존에
설치된 Windows sandbox를 선택한다. 다른 플랫폼에는 Windows 설정을 전달하지 않는다.
코드 작성은 workspace-write, 독립 기록 검토는 read-only이며 전역 설정·전체 접근 전환·
규칙 무시·실패 후 자동 sandbox 약화는 하지 않는다. sandbox 설치/정책 문제가 있으면
실패를 보존하고 실행 환경을 해결한 뒤 별도 attempt로 재개한다.
기존 executor의 Linux 전용 프로세스 처리를 그대로 가져오지 않는다. timeout과 프로세스
트리 회수는 Harness의 Windows Job Object/POSIX process group 패턴을 따른다.
OS 실행에 필요한 환경과 Codex 로그인 위치만 전달하고 GitHub/GCP/API key 환경은 전달하지
않는다. 이는 동일 OS 사용자에 대한 보안 sandbox를 보장하는 설계가 아니다.

agent는 initial card와 validation feedback만 받아 현재 champion에서 한 가설을 구현한다.
채점 규칙·정답·final 결과·grant·Judge 경로는 prompt/context에 넣지 않는다. 저장소 내부
수정 경로 allowlist는 추가하지 않으며, 외부 trusted Judge가 수치 판정을 소유한다.
agent는 커밋/push·원격 자원 생성·데이터 다운로드를 수행하지 않는다. Harness는 변경의
credential 검사와 diff fingerprint를 확보한 뒤 로컬 candidate commit을 만들고 설명·
변경 SHA·usage·시간을 artifact로 보존한다. agent가 보고한 개선 주장은 실제 metric과
구분하며, usage가 없는 경우 0으로 만들지 않는다. 달러 비용은 근거 없으면 null이다.
구조화 응답은 `status=implemented|no_change|blocked`를 명시한다. CLI exit 0만으로 구현
성공을 간주하지 않으며 `blocked`는 설명을 보존한 실행 실패로 기록한다. 의도적인
`no_change`와 도구 정책 차단을 구분한다. 변경 경로·fingerprint는 보존된 commit diff와
일치해야 하며 checkout line-ending 차이를 실제 코드 변경으로 보고하지 않는다.

예측은 code SHA별 새 disposable workspace에 runtime `harness_config.json`을 배치하고
기존 `LocalRunner`를 실행한다. 이 로컬 설정·모델·캐시는 candidate commit에 넣지 않는다.
training override는 명시한 값만 전달하여 candidate의 코드 기본값 변경이 가려지지 않는다.
예측 trusted launcher는 host의 home/temp를 상속하지 않고 `harness_out/.runtime/home`과
`harness_out/.runtime/tmp`를 새로 만든다. child의 HOME/USERPROFILE과 TEMP/TMP/TMPDIR은
각각 이 경로로 고정한다. ML 라이브러리가 `Path.home()`이나 임시 파일을 필요로 하더라도
사용자 홈·자격 증명 경로를 노출하지 않는다. 이미 존재하거나 alias인 runtime 경로는 실패다.
PyTorch 캐시 초기화의 `getpass.getuser()`용 USERNAME은 고정 문자열 `harness`다.
이는 host 이름 상속이나 OS 계정/실행 권한 변경이 아니라 cache naming용 child 환경이다.
이 경로는 disposable workspace와 함께 회수하며 저장 로그인은 prediction child에 주지 않는다.
각 seed는 독립 학습이며 baseline/candidate CSV와 모델·receipt를 Judge-owned run artifact에
보존하고 trusted domain에서 봉인·평가한다. final은 기존 같은 grant 아래 계획된 paired
seed만 실행하고 실패 후 새 claim으로 재시도하지 않는다. 재개는 완료 trial을 재실행하지
않고 중단된 validation trial만 다시 시작할 수 있다. 완료 전 중단된 agent/학습 비용도
attempt artifact에 남기며 정확히 한 번의 LLM 실행은 보장하지 않는다.

MVP planner는 초기 card와 validation feedback 수로 재생 가능한 card 순서를 만든다.
coding agent가 feedback을 읽어 구현을 조정하며, 별도 LLM planner framework는 추가하지
않는다. 후속 #55의 context-free 연구 기록 Judge·REPORT는 §10.1.1을 따르며,
실제 5-seed calibration은 Task 7에서 측정한다.

### 4.10 Task 7 baseline·parser 실측 계약 (#57)

수동 calibration은 기존 validation fixture와 고정 baseline code SHA에서 seed
101, 102, 103, 104, 105로 각각 한 번 새 학습을 실행한다. screening seed 42와 분리한다.
Workspace/LocalRunner/Domain과 기존 prediction 설정을 재사용하고 agent·Controller·
final grant·registry 초기화·새 원격 API를 호출하지 않는다. 같은 모델의 반복 점수화나
baseline/candidate가 같은 paired 10회 학습으로 5회 독립 fit을 대체하지 않는다.

실행 전에 baseline SHA·fixture/metadata identity·prediction 설정·모델 파일 identity·
seed 순서를 고정한다. 각 seed마다 새 workspace에서 기존 harness-predict를 한 번
실행하고 CSV/모델/training receipt/execution evidence를 보존한 뒤 workspace를 회수한다.
그 뒤 trusted domain으로 validation prediction을 봉인·채점한다.

7개 필수 metric의 raw float를 반올림하지 않고 남긴다. 지표별 유한한 값 5개가 모두
있을 때만 평균과 표본 표준편차(`statistics.stdev`, ddof=1)를 계산한다. 미달이면 유효
표본 수와 null을 기록한다. 관측된 0은 0.0이며 epsilon을 더하지 않는다. 측정 완료와
현행 sigma/coverage 판정 gate 통과는 별개다. 이 script는 승격 판정을 하지 않는다.

별도 calibration output root의 TrialLedger checkpoint에 입력·seed intent·seed 완료·
전체 완료 및 원본 metric/요약/산출물 digest를 기록한다. Controller ledger와 섞거나
promote/discard 판정을 만들지 않는다. 실패/중단의 원본과 관측 비용은 남기고 같은
output root에서 자동 재학습하지 않는다. MVP는 one-shot 측정을 우선하며 별도의
재개 엔진을 추가하지 않는다. 실제 원본 데이터/모델/private 경로는 커밋하지 않는다.

parser 실측은 실제 `seal_prediction_copy`의 성공/실패와 전체 봉인 wall time, 같은
parser worker의 별도 관측 실행(시작~종료 시간·메모리 종류)을 구분한다. 파일 생성과
최종 scoring 시간은 parser 시간에 합치지 않는다. 현재 300k행·65MiB·10초·256MiB 및
parsed JSONL 80MiB 상한은 #57 최초 측정에 그대로 적용했다. 긴 alphanumeric 식별자와 backslash-heavy
유효 식별자를 별개로 측정하고, 초과 행/물리 행/파일 byte 제한의 음성 케이스도 확인한다.

backslash의 JSON escaping으로 내부 출력 상한과 충돌할 가능성도 측정한다.
worker의 exit 1은 여러 원인이 합쳐진 결과이므로 직접 확인한 원인만 확정하며
근거가 없으면 unknown으로 남긴다. 예상 크기 계산과 실측 실패를 구분한다. 메모리
관측이 불가능하면 null이며 sampled peak를 OS 보장 peak로 표현하지 않는다. 합성
300k CSV는 parser 자원 검증이지 실제 평가 fixture의 품질/coverage 증거가 아니다.
Windows 관측은 `PROCESS_MEMORY_COUNTERS`의 PeakWorkingSetSize와 PeakPagefileUsage를
분리하며 후자는 process commit의 생애 최대값이다(둘 다 bytes).
정의는 [Microsoft 공식 문서](https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters)를 따른다.

실측이 초기 sigma/coverage/parser 가정과 다르면 원인과 대안을 spec/plan에서 먼저
검토하고 필요한 정책 수정은 별도 이슈/PR로 분리한다. 수치 조작 없이 그 다음에
Controller E2E·checkpoint 재개·무변경/승격 시나리오·단일 final·REPORT를 실측한다.

**2026-09-03 기준선 실측:** baseline `8dd67038d98817b3b4a5f33a4d9dd5009c2ce9fd`,
seed 101~105의 새 fit 5회, validation 3,840행·160 slate를 사용했다. snapshot fingerprint는
`1d81f2037c65b928cf83139242333cc87498951236569dc192fe1a1fc86c1bd4`이며,
`intfloat/multilingual-e5-small` revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`
(384차원, CUDA embedding)과 CPU LightGBM 학습을 사용했다. 모두 유효 표본 5개이며
아래 σ는 보정하지 않은 ddof=1 표본 표준편차다. 이 fixture/code/config에 한정된
기준선이며 다른 데이터나 모델의 노이즈로 일반화하지 않는다.

| metric | baseline 평균 | σ |
|---|---:|---:|
| ndcg_at_10 | 0.7678924740923143 | 0.012644841892910248 |
| recall_at_10 | 0.9925 | 0.011180339887498952 |
| ndcg_at_24 | 0.7698834576695183 | 0.011363737745236012 |
| grouped_roc_auc | 0.8804891304347826 | 0.006821680591097526 |
| pr_auc | 0.443332460607605 | 0.035806512787097684 |
| log_loss | 0.15790130458219007 | 0.006165367875655428 |
| brier | 0.03645414647885959 | 0.0002480487726970229 |

모든 σ가 현행 `> 1e-6` 조건을 충족했다. seed 42에서 Recall@10이 1.0이었다는
단일 관측만으로 σ=0이라고 추정하지 않고 실제 5회 결과로 확인했다. 아직 candidate
승격 또는 final 개선은 측정하지 않았다. parser의 일반 문자 300k행은 봉인 3.998초에
성공했지만 backslash-heavy 300k행은 3.356초에 거부됐다. 따라서 parser 최대 입력
지원은 아직 완료가 아니며 별도 수정이 필요하다. 상세 raw 값·자원 측정·한계는
[Task 7 실측 기록](../plans/2026-08-15-local-research-harness-mvp.md#첫-실측-pr--57)에 남긴다.

**#58 내부 출력 상한 보정:** 외부 CSV의 허용 문자를 줄이지 않고 내부 정규화 JSONL
상한만 104MiB로 보정한다. evaluation ID 69byte, 두 ID는 각각 최대 64개의 backslash가
128byte로 확장되고, 정규화 float 표현을 보수적으로 24byte로 잡으면 JSON 문법 11byte와
LF 1byte를 포함해 한 행은 최대 `69 + 2×128 + 24 + 11 + 1 = 361byte`다.
`361 × 300,000 = 108,300,000byte`는 104MiB(`109,051,904byte`) 이내다.
외부 65MiB·300k행·226byte, parser 10초·256MiB 및 내부 행 512byte 제한은 유지한다.
출력은 streaming 파일이므로 출력 한도와 프로세스 메모리를 동일하게 취급하지 않는다.

기존 alphanumeric/unique backslash 측정을 보존하고 두 ID 전체 backslash와
24byte score token `+1.2345678901234567e-100`의 별도 300k행을 추가 검증한다.
CSV 행은 226byte이며 정규화 후 score는 23byte, JSONL은 행당 360byte가 된다.
이 마지막 입력은 parser capacity만 검증하며 중복 key가 있어 Judge의 유효한 평가
데이터셋이라고 주장하지 않는다. 실제 ingestion·별도 worker 자원 관측과 음성 입력을
다시 측정한 뒤 초기 한도의 충분성을 판단한다. 임계값·metric·final 계약은 변경하지 않는다.

2026-09-03 재측정에서 세 유효 입력은 모두 300k행 처리에 성공했다. 실제 ingestion은
일반 문자 3.968초, 기존 backslash 4.417초, 최대 JSON 확장 4.525초였다.
마지막 입력의 내부 출력은 108,000,000byte이며 별도 worker 시작~종료 4.547초,
peak working set 25,493,504byte·peak commit 13,754,368byte를 관측했다.
300,001행/물리 행 초과/65MiB 초과는 계속 거부됐다. 해당 Windows/Python 환경의
한 번씩 측정 결과이며 다른 하드웨어·동시 부하의 성능 보장은 아니다. 세 입력 모두
현행 시간·메모리 안에서 처리돼 외부 행/파일 한도를 낮추지는 않는다.

## 5. 목표 아키텍처

아래는 MVP 이후 논문 계층까지 포함한 최종 구조다. MVP에서는 사람이 준 가설과
`ExperimentCard`가 Paper Discovery·Capability Matcher 자리를 대신해 Research Agent 입력으로
직접 들어간다.

```text
Research Request
       |
       v
+-----------------------+
| Research Controller   |  budget, checkpoint, state machine
+-----------+-----------+
            |
            v
+-----------------------+     +-----------------------+
| Paper Discovery       | --> | Capability Matcher    |
| search, resolve, cite |     | feasible hypotheses   |
+-----------------------+     +-----------+-----------+
                                            |
                                            v
                               +-----------------------+
                               | Research Agent        |
                               | full-repo mutation    |
                               +-----------+-----------+
                                           |
                               disposable candidate
                                           |
                                           v
                               +-----------------------+
                               | Experiment Runner     |
                               | local / Kubernetes    |
                               +-----------+-----------+
                                           |
                                           v
                               +-----------------------+
                               | Sealed Judge          |
                               | evaluate and compare  |
                               +-----------+-----------+
                                           |
                       promote / revise / discard
                                           |
                    +----------------------+------------------+
                    |                                         |
                    v                                         v
          next research iteration                    Trial Ledger
                                                              |
                                                              v
                                                    Final REPORT
```

### 5.1 주요 인터페이스

```python
class ResearchDomain(ABC):
    def describe_capabilities(self) -> Never: ...
    def build_evaluation_snapshot(
        self,
        request: EvaluationSnapshotRequest,
        *,
        source: ActionLogSource | None = None,
    ) -> EvaluationSnapshotReceipt: ...
    def validate_candidate(
        self,
        candidate_prediction: Path,
        judge_copy: Path,
    ) -> SealedPredictionReceipt: ...
      def evaluate(
          self,
          handoff: JudgeSnapshotHandoff,
          sealed_prediction: SealedPredictionReceipt,
          *,
          final_grant: FinalConsumptionGrant | None = None,
      ) -> JudgeScoringResult: ...
    def compare(
        self,
        results: PairedJudgeResult | Sequence[PairedJudgeResult],
        *,
        baseline_sigmas: Mapping[str, float] | None = None,
    ) -> ScreeningResult | ConfirmationDecision: ...


class PaperSource(ABC):
    async def search(self, query: PaperQuery) -> list[PaperMetadata]: ...
    async def get(self, paper_id: PaperId) -> PaperDocument: ...
    async def citations(self, paper_id: PaperId) -> CitationGraph: ...


class ExperimentRunner(ABC):
    async def run(self, candidate: CandidateArtifact) -> TrialResult: ...


class LocalRunner(ExperimentRunner): ...
class KubernetesJobRunner(ExperimentRunner): ...
```

위 `ExperimentRunner` 계층은 Kubernetes 실행까지 포함한 목표 구조다. Task 5a MVP의 실제
계약은 4.4.2의 동기식 `LocalRunner`이며, Task 5b에서 실제 소비 형태가 확인되기 전에는 위
placeholder 타입을 구현하지 않는다.

MVP에는 `YouTubeCTRDomain`과 `LocalRunner`만 실제 구현한다. `YouTubeCTRDomain`은 MVP가
실제로 호출하는 `build_evaluation_snapshot()`, `validate_candidate()`, `evaluate()`,
`compare()`를 slate/Judge 구현에 연결한다. 논문 발견이 없는 MVP에서는
`describe_capabilities()`를 호출하지 않으며 명시적인 미지원 오류로 남긴다. 이 메서드는
Paper Discovery 단계에서 구현한다. 이커머스·뉴스피드는
`ResearchDomain` 계약의 확장 가능성을 설명하는 후속 사례이며 MVP 구현 범위가 아니다.
테스트에서는 작은 fixture용 fake domain을 사용할 수 있으나 두 번째 제품 domain으로
간주하지 않는다.

## 6. 논문 자동 발견 (MVP 이후 로드맵)

논문 발견은 서비스 내부의 첫 번째 연구 단계다. 사용자가 PDF나 논문 목록을 제공하는
흐름을 기본값으로 두지 않는다.

### 6.1 로드맵 source 구성

- OpenAlex: 여러 출판처를 포괄하는 검색, 필터, 인용 관계 탐색
- arXiv: 추천·CTR·representation learning 분야의 공개 preprint와 원문
- Crossref: DOI와 canonical publication metadata 해소 및 중복 제거
- Semantic Scholar: 후속 단계에서 관련 논문 추천과 citation expansion 보강

범용 웹 crawl 결과를 곧바로 논문 근거로 사용하지 않는다. 원문은 공개적으로 접근 가능한
문서만 수집하며, 원문을 확보하지 못한 경우 `abstract_only`로 표시한다. 유료 원문을
우회하지 않는다.

공식 API 문서:

- [OpenAlex Search](https://developers.openalex.org/guides/searching)
- [arXiv API User's Manual](https://github.com/arXiv/arxiv-docs/blob/develop/source/help/api/user-manual.md)
- [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)
- [Semantic Scholar Academic Graph API](https://www.semanticscholar.org/product/api)

### 6.2 발견 절차

1. `ResearchDomain`이 현재 데이터, 모델, label, 실행 가능 범위를 설명한다.
2. Controller가 연구 목표와 capability를 조합해 검색 query family를 만든다.
3. 여러 source에서 후보를 수집하고 DOI, arXiv ID, 제목으로 중복을 제거한다.
4. 상위 후보의 인용·참고문헌 그래프를 제한된 깊이로 확장한다.
5. 관련성, 데이터 적합성, 구현 가능성, 검증 가능성, 비용, 누수 위험으로 점수화한다.
6. 동일 유형의 오래된 유명 논문만 남지 않도록 seminal, recent, domain-specific,
   debiasing 등 연구 축의 다양성을 유지한다.
7. 사람이 shortlist를 승인하지 않아도 예산 안에서 top candidate를 자동 선택한다.
8. 검색 query, provider, 조회 시점, 원문 범위, 선택·탈락 사유를 manifest에 보존한다.

인용 수만으로 논문을 정렬하지 않는다. 인용 수는 evidence quality의 일부일 뿐이며,
현재 프로젝트에서 실제로 반증 가능한 가설을 만들 수 있는지가 더 중요하다.

### 6.3 PaperCard

논문은 링크 목록이 아니라 다음 구조의 연구 입력으로 변환한다.

```yaml
paper_id: P03
title: "Up Next: Retrieval Methods for Large Scale Related Video Suggestion"
identifiers:
  doi: null
  arxiv_id: null
sources:
  canonical_url: "official publisher or research page"
  discovery_provider: openalex
  discovery_query: "video topical representation recommendation"
  retrieved_at: "RFC3339 timestamp"
evidence:
  scope: full_text
  document_sha256: "..."
  referenced_sections:
    - section: "4"
      claim: "topical representation과 관련된 검증 근거"
repository_fit:
  usable_fields: [title, description, tags]
  missing_requirements: []
hypothesis:
  change: "category 기반 유사도를 video text representation으로 확장"
  expected_metrics: [ndcg_at_10, recall_at_10]
falsification:
  - "동일 evaluation slate에서 champion 대비 개선되지 않음"
usage:
  status: implemented
  hypothesis_ids: [H-004]
  trial_ids: [T-004]
```

Google Research의 [Up Next](https://research.google/pubs/up-next-retrieval-methods-for-large-scale-related-video-suggestion/)
같이 현재 raw 필드로 검증 가능한 논문은 즉시 가설 후보가 된다. DIN처럼 시간순 사용자
행동 sequence가 필요한 논문은 에이전트가 action log에서 sequence dataset을 재조립해
검증할 수 있다. 예산이나 데이터가 부족하면 `infeasible_with_current_data`로 기록하고
다음 후보로 진행한다.

### 6.4 논문 사용 상태

REPORT는 논문을 다음 상태로 구분한다.

- `discovered`: 검색 결과에서 발견
- `screened_out`: 적합성 검토 후 제외
- `reviewed`: 초록 또는 원문 분석
- `hypothesis_source`: 가설 생성의 근거로 사용
- `implemented`: 실제 candidate로 구현
- `promoted` 또는 `rejected`: 실험 판정 완료

존재 여부와 canonical identifier를 검증하지 못한 논문은 구현 근거로 사용할 수 없다.

## 7. 자율 연구 루프

MVP는 사람이 준 가설·`ExperimentCard`에서 아래 루프를 시작한다. 코드의 discovery와
paper compile 단계는 MVP 이후에 같은 입력 seam 앞에 붙는다.

```python
while budget.remaining:
    experiment_card = planner.next_card(initial_card, trial_history)
    candidate = agent.implement(experiment_card, disposable_workspace)

    validation = harness.validate(candidate)
    if not validation.runnable:
        ledger.reject(candidate, validation.reason)
        continue

    screening = runner.run(candidate, fidelity="screening")
    confirmation = None
    if judge.should_confirm(screening, champion):
        confirmation = runner.run(candidate, fidelity="multi_seed")
        decision = judge.decide_confirmation(confirmation, champion)
    else:
        decision = "discard"

    if decision == "promote":
        champion = candidate
    ledger.record(experiment_card, candidate, screening, confirmation, decision)
```

각 iteration은 현재 champion에서 disposable worktree를 만들고, 에이전트가 전체 저장소를
수정한 뒤 candidate artifact를 생성한다. 저비용 screening에서 가능성을 확인한 후보만
다중 seed 확인 실험으로 올린다. 실패하거나 성능이 나빠진 변경은 자동 폐기하고 champion
상태로 되돌아간다.

단일 scalar leaderboard만 최적화하지 않는다. `ResearchDomain.compare()`는 primary
metric 개선과 guardrail 충족을 분리하며, 어떤 지표를 선택했는지는 REPORT에 설명한다.
승격 임계값은 고정 비율이 아니라 baseline을 seed만 바꿔 반복했을 때의 노이즈 표준편차
`σ`의 배수로 정의한다. validation slate의 5-seed baseline sweep은 고정 모델을 다섯 번
점수화하는 작업이 아니라, 4.5절의 Harness baseline 설정을 seed마다 **독립적으로 5회 재학습**한 뒤
같은 slate를 점수화하는 작업이다. 이 sweep으로
primary와 모든 guardrail의 `σ_metric`을 **지표별로 각각** 계산한다. NDCG의 σ를
Recall·AUC·LogLoss·Brier에 공용하지 않는다.

모든 delta는 **개선이 양수**가 되도록 방향을 정규화한다.

- 높을수록 좋은 `NDCG@10`, `Recall@10`, `NDCG@24`, grouped ROC-AUC, PR-AUC:
  `normalized_delta = candidate - baseline`
- 낮을수록 좋은 LogLoss, Brier Score:
  `normalized_delta = baseline - candidate`

아래 표의 `Δ_metric`은 이 정규화된 delta이고, `σ_metric`은 같은 지표의 baseline
5-seed 표준편차다.

판정 전에 다음 유효성 gate를 모두 통과해야 한다.

- 모든 필수 지표의 baseline `σ_metric`이 **`1e-6`보다 커야 한다.** `σ ≤ 1e-6`이면
  단위 구간으로 정규화한 지표에서 실측 잡음이 MVP의 수치 해상도보다 작아
  `Δ=0 ≥ 2σ=0` 같은 동률 승격이 가능하므로 `insufficient_baseline_noise`로 판정 불가
  처리한다. `σ`를 인위적으로 올려 승격시키지 않고, seed 수·데이터를 늘려 다시 측정한
  뒤에만 판정을 연다. `1e-6`은 이 fail-closed 수치 해상도의 초기값이며 Task 7 실측에서
  재조정한다.
- NDCG@10·NDCG@24·Recall@10은 click이 하나 이상인 유효 slate가 전체 slate의 20% 이상이고
  최소 30개여야 한다. 즉 `scored_slates >= max(30, ceil(total_slates * 0.20))`다.
- grouped ROC-AUC는 양성과 음성을 모두 가진 유효 유저가 non-null 전체 유저의 20% 이상이고
  최소 30명이어야 한다. 즉 `scored_users >= max(30, ceil(total_users * 0.20))`다. 현행
  grouped ROC-AUC는 채점 가능한 그룹이 없으면 `None`을 반환하므로
  (`autoresearch/model_evaluation/evaluate.py:77-124`), `None`은 통과가 아니라
  `metric_unavailable` 판정 불가다.
- PR-AUC·LogLoss·Brier는 대상 slate의 모든 item을 1:1로 채점해 item coverage가 100%여야
  하며 label에 양성과 음성이 모두 있어야 한다.

coverage 또는 metric 값이 하나라도 기준에 미달하면 `promote/revise/discard` 중 어느 값도
내지 않고 `insufficient_metric_coverage` 또는 `metric_unavailable`로 실행을 fail-closed한다.
0-click slate 제외로 지표별 유효 표본 수가 다르므로 전체 row 수 하나로 대체하지 않는다.

#### P0-2A ranking metric 계산 계약

`autoresearch.research_harness.ranking_metrics`는 Judge 파일 I/O나 판정을 소유하지 않는
순수 계산 module이다. 외부 interface는 `ndcg_at_k()`와 `recall_at_k()` 두 함수, 공통
불변 결과 타입 `RankingMetricResult`, 안정적인 오류 타입 `RankingMetricError`와
`RankingMetricErrorCode`로 제한한다. 두 함수는 같은 길이의 `labels`,
`scores`, `slate_ids`, `video_ids`와 양의 정수 `k`를 받고, slate grouping·결정적 정렬·
0-click 제외·macro 평균·coverage 집계를 module 내부에서 수행한다.

- 각 slate는 `score` 내림차순, 동률이면 `video_id` 오름차순으로 정렬한다. P0-2B가 보장한
  고유 `(slate_id, video_id)` 입력에서는 입력 row 순서가 결과에 영향을 주지 않는다.
- rank를 1부터 셀 때 `DCG@k = sum(rel_i / log2(i + 1))`다. binary relevance에서
  `rel_i`는 `0|1`이며, `IDCG@k`는 같은 slate의 click label을 내림차순으로 정렬해 계산한다.
  `NDCG@k = DCG@k / IDCG@k`다.
- `Recall@k = top-k 안의 click 수 / slate 전체 click 수`다. 전체 click 수가 `k`보다 커도
  분모를 `k`로 자르지 않는다.
- click이 없는 slate는 NDCG와 Recall 모두 정의되지 않으므로 0점으로 넣지 않고 제외한다.
  최종 값은 `slate_id` 오름차순으로 정렬한 유효 slate별 값을 `math.fsum()`으로 더한 동일
  가중치 macro 평균이다. 유효 slate가 없으면 `value=None`이다.
- `RankingMetricResult`는 `value`, `total_slates`, `scored_slates`,
  `skipped_zero_click_slates`, `coverage`를 담는다. `coverage = scored_slates / total_slates`이며
  입력이 비면 `0.0`이다. `total_slates`는 고유한 non-empty `slate_id` 수이고,
  `skipped_zero_click_slates = total_slates - scored_slates`다. 30개·20% 제품 gate의 통과
  여부는 P0-2C가 이 결과로 판정한다.
- `slate_ids`와 `video_ids`는 앞뒤 공백 없는 non-empty `str`이어야 한다. 이 module은 길이
  불일치, `k <= 0`, 비 binary label, 잘못된 식별자, NaN·Inf score를 reason code가 있는
  `RankingMetricError`로 거부한다. reason code는 `length_mismatch`, `invalid_k`,
  `invalid_label`, `invalid_identifier`, `non_finite_score`로 고정한다.
  `(slate_id, video_id)` 1:1 유일성과 score `[0,1]` 확률 범위는 P0-2B prediction 계약이
  소유하며 여기서 중복 검증하지 않는다.

손으로 계산한 고정 예제로 완전 정답·역순·동점·0-click·짧은 slate·click 수가 `k`보다 큰
slate와 고유 key 입력의 row 순서 불변을 검증한다. 테스트가 구현의 동일 helper를 기대값
계산에 재사용해서는 안 된다.

##### Portfolio Record — P0-2A 결정적 리랭킹 지표

**문제.** 자율 실험 에이전트가 candidate를 비교하려면 CTR 평균만으로는 실제 노출 순서가
개선됐는지 알 수 없었다. 특히 click이 없는 slate를 0점으로 평균에 넣거나 click 수가
`k`보다 클 때 Recall 분모를 잘못 자르면 데이터 구성에 따라 지표가 달라진다. 동점 순서와
집계 순서도 고정하지 않으면 같은 prediction이 재실행마다 다른 증거를 만들 수 있다.

**해결.** Judge와 분리된 순수 `ranking_metrics` module에 NDCG@K·Recall@K 두 interface만
두고, score 내림차순·video ID 오름차순 tie-break, binary relevance, slate별 macro 평균을
고정했다. zero-click slate는 점수에서 제외하되 `RankingMetricResult`의 유효/제외 slate 수와
coverage로 손실을 드러낸다. 입력 계약 위반은 원본 값을 노출하지 않는 다섯 고정 reason
code로 거부한다. key 고유성·score `[0,1]` 검증은 prediction 의미를 아는 P0-2B에 남겨 같은
규칙을 두 계층이 중복 소유하지 않게 했다.

**결과.** 구현 helper를 기대값 계산에 재사용하지 않은 손 계산 golden test 20개로 완전
정답 `1.0`, click 1개가 3위인 NDCG `0.5`, 동점 click 2위
`1/log2(3)`, Recall 분모 `3`일 때 `2/3`, zero-click 제외 coverage, 빈 입력,
짧은 slate, row 순서 불변과 오류 코드를 검증했다. P0-2A 집중 테스트는 20개가 통과했고,
Research Harness 회귀 테스트는 `294 passed, 3 skipped`였다. 아직 prediction 1:1 검증,
제품 coverage gate와 sigma 기반 판정은 구현하지 않았으며 각각 P0-2B/C의 후속 범위다.

#### P0-2B/C 평가 대상 선택과 prediction 신뢰 경계

Candidate의 `predictions.csv`는 validation/final 대상을 선택하지 못한다. P0-2B/C의 공개
interface는 검증된 Stage C handoff에서 validation ID와 정확한 slate/label artifact를 고정하는
`build_validation_target()`만 제공한다. 반환되는 `JudgeEvaluationTarget`은 직접 생성할 수 없는
opaque 내부 타입이며 package `__init__.py`에서 재수출하지 않는다. P0-2B는 CSV의
`evaluation_id`가 target의 기대값과 같은지만 검증한다.

P0-2B/C에는 final target factory가 없다. write-once final 소비 registry를 구현하는 후속
Task가 `FinalConsumptionGrant`를 발급하고, 그 grant만 받는 `build_final_target()`을 추가한
뒤에만 final 채점이 가능하다. Stage C handoff 단독 또는 임의 ID·path로 final target을 만드는
interface는 제공하지 않으며, 직접 target 생성을 시도하면 typed 오류로 거부한다.

P0-2B는 field byte·schema·1:1 key 의미 검증과 metric scoring을 소유한다. P0-2C는 candidate
경로에서 Judge 사본을 만드는 단일-FD ingestion과 **같은 P0-2B parser 구현**을 제한
subprocess에서 실행하는 책임을 소유한다. worker가 검증한 행은 exclusive 정규화 JSONL로
남기고 CSV·JSONL identity와 digest를 opaque receipt 하나에 결속한다. scoring은 이 receipt만
받아 정규화 행을 streaming 소비하므로 candidate 경로나 CSV를 무제한으로 다시 parse하지
않는다. 두 단계가 서로 다른 parser나 schema 정의를 갖지 않는다.

P0-2B의 package 공개 interface는 `build_validation_target(handoff)`와
`score_predictions(target, sealed_prediction)` 두 함수, 불변 `JudgeScoringResult`, 그리고
`JudgeError`·`JudgeErrorCode`로 제한한다. `JudgeEvaluationTarget`, `PredictionRow`,
`parse_prediction_copy()`는 P0-2C가 같은 구현을 재사용할 module 내부 interface이며 package
`__init__.py`에서 재수출하지 않는다. target은 공개 constructor 없이 검증된 factory만 만들고,
직접 생성은 `invalid_judge_target`으로 거부한다. handoff·slate·label artifact가 계약과 다르거나
재검증에 실패해도 같은 code로 fail-closed한다. CSV·prediction key·score 계약 위반은
`invalid_predictions`로 고정하고 원본 field 값이나 Judge path를 오류에 싣지 않는다.

`parse_prediction_copy()`는 정확한 header
`evaluation_id,slate_id,video_id,score`와 최대 300,000행을 streaming으로 읽어 typed row를
만든다. ID field는 comma·quote·개행·앞뒤 공백 없는 ASCII이며 `evaluation_id`는 현재
`eval_` + SHA-256 계약에 맞춰 정확히 69 byte, `slate_id`와 `video_id`는 각각 1~64 byte다.
`score` token은 앞뒤 공백 없는 ASCII 최대 24 byte이고 finite float `[0,1]`이어야 한다.
target factory는 slate artifact의 모든 key가 이 prediction 표현 계약으로 encode 가능한지도
검증한다. 표현할 수 없는 trusted key를 candidate prediction 오류로 전가하지 않고 target을
`invalid_judge_target`으로 거부한다.
P0-2C의 ingestion byte 상한은 **65 MiB + 1 byte probe**로 고정한다. CRLF 최악 행은
`69 + 64 + 64 + 24 + comma 3 + CRLF 2 = 226 byte`이고 300,000행과 39 byte header는
`67,800,039 byte`라 65 MiB(`68,157,440 byte`) 안에 든다. 기존 64 MiB 계산은 실제
69-byte evaluation ID를 64 byte로 센 오류이므로 사용하지 않는다.

`JudgeScoringResult`는 target evaluation ID와 row count, `ndcg_at_10`, `recall_at_10`,
`ndcg_at_24`, 그리고 `ProbabilityMetricResult`를 담는다. probability 결과는
`row_count`, `positive_count`, `negative_count`, `roc_auc`, `pr_auc`, `log_loss`, `brier`,
`GroupedRocAuc`를 포함한다. 양성·음성 중 한 클래스가 없으면 전역 probability metric은
예외 대신 `None`으로 구조화해 P0-2C가 `metric_unavailable`로 판정할 수 있게 한다. 기존
`evaluate.py`의 같은 계산을
`model_evaluation/probability_metrics.py`의 순수 interface로 먼저 이동하고 기존 CLI가 이를
호출하게 해 Judge와 정의가 갈라지지 않게 한다. 지표별 coverage gate와 `None` 판정은
P0-2C가 결과를 소비하면서 적용한다.

##### Portfolio Record — P0-2B validation Judge scoring

**문제.** 자율 실험 candidate가 자기 CSV의 `evaluation_id`로 validation과 final holdout 중
유리한 대상을 고르거나, 일부 key만 제출하고도 점수를 받으면 실험 지표를 신뢰할 수 없다.
기존 probability metric도 CLI 내부에 묶여 있어 Judge가 별도 정의를 만들 경우 같은 score가
서로 다른 증거가 될 위험이 있었다. 또한 최초 문서의 64 MiB 계산은 실제 69-byte evaluation
ID를 64 byte로 세어 300,000행 최악 크기를 잘못 계산했다.

**해결.** 검증된 Stage C handoff만 받는 `build_validation_target()`이 validation artifact를
고정하고, 직접 만들 수 없는 opaque target을 `score_predictions()`에 전달하도록 interface를
제한했다. streaming parser는 exact header, ASCII field byte, 300,000행, finite `[0,1]` score를
검증하고 target key와 누락·중복·extra 없는 exact 1:1 관계만 허용한다. CSV로 표현할 수 없는
artifact key와 null identity는 candidate 오류가 아니라 target 오류로 조기 거부한다. 기존 CLI의
ROC-AUC·PR-AUC·Log Loss·Brier·grouped ROC-AUC 계산은 순수 probability module로 이동해
Judge와 공유하고, 단일 클래스는 count와 `None`으로 구조화해 P0-2C가 판정하게 했다.

**결과.** P0-2B 집중 계약 테스트 29개가 통과했으며 64/65-byte ID, 24/25-byte score,
정확히 300,000/300,001행, LF/CRLF, final ID 선택 시도, key 누락·중복·extra, artifact 변조와
null identity를 회귀 고정했다. 기존 평가 계산 회귀 25개와 Research Harness 회귀
`319 passed, 3 skipped`, 전체 Ruff가 통과했다. 독립 spec 및 코드·보안 리뷰는 수정 후
Critical/Important/Minor 발견 없이 PASS했다. candidate 경로 봉인, subprocess 자원 제한,
coverage gate와 sigma 판정은 의도대로 P0-2C에 남아 있다.

#### P0-2C sealed ingestion과 판정 interface

candidate 경로를 여는 책임은 `prediction_ingestion` module에만 둔다.
`seal_prediction_copy(candidate_prediction, judge_copy)`는 source를 한 번 열고 동일 FD에서
exclusive Judge 사본을 만든 뒤, 공통 `prediction_parser`를 별도 worker에서 실행한다. worker가
출력한 정규화 행과 원본 사본은 identity·SHA-256이 결속된 `SealedPredictionReceipt`로만
scoring에 전달된다.
POSIX는 `RLIMIT_AS`, Windows는 Job Object process-memory limit으로 256 MiB를 강제하고,
부모는 wall-clock 10초 timeout을 적용한다. source는 regular-file 사전 검사 후
`O_NONBLOCK|O_NOFOLLOW`로 열어 FIFO와 검사-열기 사이 교체도 비차단 거부한다. 실패·timeout·
interruption이면 생성된 CSV와 정규화 부분 파일을 모두 제거한다. `judge.py`는 candidate 경로를
열지 않고 봉인 receipt의 검증된 정규화 행만 소비한다.

판정 interface는 `PairedJudgeResult(seed, baseline, candidate)`를 입력 단위로 사용한다.
`screen_candidate()`는 같은 seed에서 primary가 엄격히 개선되고 모든 metric 유효성·coverage
gate를 통과했을 때만 confirmation을 요청한다. `compare_confirmation()`은 서로 다른 seed
5개의 pair와 지표별 `baseline_sigmas` map을 받아 seed별 방향 정규화 delta의 평균으로만
최종 판정을 낸다. 실제 sigma 값은 이 module이 소유하지 않으며 Task 7 측정 전에는 호출자가
유효한 map을 제공할 수 없으므로 제품 승격 판정은 열리지 않는다.

##### Portfolio Record — P0-2C sealed ingestion과 deterministic decision

**문제.** P0-2B는 Judge 소유 사본을 정확히 채점할 수 있었지만 candidate 경로에서 그 사본을
만드는 과정은 아직 신뢰 경계 밖이었다. 경로를 두 번 열거나 기존 목적지를 덮어쓰면 검사한
파일과 채점한 파일이 달라질 수 있고, 무제한 CSV parser는 candidate 입력 하나로 Judge의
시간·메모리를 소진할 수 있었다. 또한 단일 screening 점수나 공용 sigma 하나로 champion을
바꾸면 seed noise와 지표 방향 차이를 승격 근거와 혼동하게 된다.

**해결.** candidate 경로 접근을 `prediction_ingestion` module 하나에 모아 no-follow source를
한 번 열고, 같은 FD의 전후 device·inode·mode·size·mtime이 같은 경우에만
`O_CREAT|O_EXCL` Judge 사본을 남긴다. 최대 65 MiB와 1-byte probe를 적용하고 실패한 부분
사본은 열린 FD를 먼저 닫은 뒤 회수한다. P0-2B parser는 표준 라이브러리만 의존하는 내부
module로 추출해 격리 worker가 검증된 정규화 행을 만든다. CSV 사본과 정규화 행의
identity·digest를 직접 만들 수 없는 receipt에 함께 묶고 scoring은 receipt만 받게 해 봉인 우회를
막았다. regular-file 사전 검사와 non-blocking open으로 FIFO와 교체 race를 거부한다.
정규화 목적지는 부모가 `O_EXCL`로 빈 파일을 먼저 예약하고 worker가 같은 device·inode에만
쓰며, 실패 cleanup도 그 소유 identity가 유지될 때만 삭제해 동시 실행의 파일을 보존한다.
worker는 10초 timeout과
POSIX `RLIMIT_AS`/Windows Job Object의 256 MiB 상한 아래에서 실행한다. 판정은 seed를 명시한
pair를 사용하며, screening은 primary의 엄격한 양수 delta만 confirmation으로 보내고 최종
결정은 서로 다른 5개 seed의 paired normalized delta 평균과 지표별 sigma map으로만 만든다.

**결과.** champion 동률, 2sigma와 -1sigma 직전·경계, higher/lower 방향, sigma 0·1e-6,
metric `None`, ranking·grouped·item coverage 부족, symlink, 65 MiB 초과, source 성장,
기존 목적지, parser timeout·실패·중단 후 cleanup, 봉인 CSV·정규화 행 변조, 직접 Path 채점
우회와 FIFO 교체를 회귀 테스트로 고정했다. P0-2C 및 기존 Judge 집중 테스트는
`68 passed, 3 skipped`, Research Harness 전체는 `359 passed, 6 skipped`, model evaluation은
`301 passed, 46 skipped`였고 전체 Ruff와 diff 검사가 통과했다. Windows에서는 symlink 권한
테스트 1개와 POSIX FIFO 테스트 2개가 skip되며 Linux CI에서 실행한다. 실제 sigma 값과 최대
300,000행의 시간·메모리 실측·임계값 조정은 계획대로 Task 7에 남겼다.

| 판정 | 조건 |
| --- | --- |
| `promote` | `Δ_ndcg_at_10 ≥ 2σ_ndcg_at_10`이고 모든 guardrail `Δ_metric ≥ -1σ_metric` |
| `revise` | `Δ_ndcg_at_10 ≥ 2σ_ndcg_at_10`이지만 하나 이상의 guardrail `Δ_metric < -1σ_metric` |
| `discard` | 그 외 |

#### P0-2D ResearchDomain interface

MVP의 `ResearchDomain` interface는 후속 Controller가 알아야 하는 다섯 동작만 노출한다.
아직 존재하지 않는 `CandidateArtifact`·`TrialResult` 임시 모델을 만들지 않고 완료된 P0-1/2의
typed 계약을 그대로 사용한다.

- `build_evaluation_snapshot(request, *, source=None) -> EvaluationSnapshotReceipt`
- `validate_candidate(candidate_prediction, judge_copy) -> SealedPredictionReceipt`
- `evaluate(handoff, sealed_prediction) -> JudgeScoringResult`
- `compare(pair) -> ScreeningResult` 또는
  `compare(pairs, *, baseline_sigmas) -> ConfirmationDecision`
- `describe_capabilities() -> Never` — Paper Discovery 전에는
  `DomainErrorCode.CAPABILITIES_UNAVAILABLE`로만 실패한다.

`evaluate()`는 validation handoff에서 opaque target을 내부 생성해 봉인 receipt와 함께
채점한다. 따라서 Controller가 `JudgeEvaluationTarget`이나 parser 행을 알 필요가 없다.
`compare()`는 단일 `PairedJudgeResult`를 받으면 P0-2C의 screening 비용 gate를, sequence를
받으면 5-seed confirmation 판정을 그대로 반환한다. confirmation에서 sigma map이 빠지면
P0-2C의 기존 `invalid_comparison_input`으로 fail-closed한다. 이로써 Controller는 구체
`screen_candidate()`·`compare_confirmation()`을 직접 알지 않고 실행 fidelity를 선택할 수 있다.

`describe_capabilities()`의 반환 모델은 실제 Paper Discovery 요구사항이 생길 때 정의한다.
MVP에서 비어 있는 capability 객체나 문자열 map을 먼저 만들면 호출자가 존재하지 않는
데이터·모델 역량을 사실로 오해할 수 있으므로, 현재 interface는 명시적인 typed 미지원
오류만 계약한다.

##### Portfolio Record — P0-2D ResearchDomain seam

**문제.** P0-1과 P0-2에서 snapshot, prediction 봉인, scoring, screening·confirmation이
각각 검증됐지만 후속 Controller가 이 함수들을 직접 조립하면 YouTube 전용 target 생성과
Judge 호출 순서를 알아야 했다. 이 상태에서는 새 domain을 추가할 때 Controller를 수정해야
하고, 테스트 fake도 구체 파일·Judge module을 흉내 내야 한다. 반대로 아직 없는
`CandidateArtifact`·`TrialResult`·capability schema까지 먼저 만들면 MVP가 검증하지 않은
개념을 interface에 고정하는 문제가 있었다.

**해결.** 다섯 메서드만 가진 `ResearchDomain` ABC를 두고 `YouTubeCTRDomain`이 기존 typed
interface에 위임하도록 했다. adapter는 `evaluate()` 안에서 validation target 생성을 숨기고,
`compare()` 입력 형태로 단일 screening과 5-seed confirmation을 기존 P0-2C 판정에 연결한다.
새 지표·schema·판정 규칙은 만들지 않았다. Paper Discovery 전 capability 호출은 빈 객체 대신
`domain_capabilities_unavailable` typed 오류로 거부한다.

**결과.** ABC의 exact 다섯 abstract method, 네 위임 경로, target 생성 순서, screening과
confirmation 분기, 잘못된 sequence·sigma 조합의 fail-closed, capability 미지원 오류·`Never`
반환 계약을 domain 테스트 13개로 고정했고 package 공개 surface 테스트 3개도 함께
통과했다. Research Harness 전체 `372 passed, 6 skipped`, 전체 Ruff와
`git diff --check`가 통과했다. 실제 Controller 주입과 fake domain을 사용한 반복 loop 증명은
Task 5b에 남으며, capability 모델은 Paper Discovery 요구사항을 측정한 뒤 정의한다.

고정 비율은 그 값이 실제 seed 잡음보다 큰지 알려 주지 않는다. 자율 루프가 수십 trial을
반복하면 우연히 좋아 보이는 결과가 누적되므로, baseline에서 실측한 잡음에 상대적인
임계값을 사용한다. 다만 seed 5개의 표본 표준편차로 모분산을 정확히 안다고 볼 수 없으므로
정규분포의 알려진 σ 꼬리확률을 승격 오류율로 인용하지 않는다. `2σ`는 측정 전 선택한
실용적 출발값이며, Task 7에서 baseline noise와 승격·기각 경로를 실측한 뒤 배수와 coverage
기준을 재조정한다.

### 7.1 screening, 확인 실험, final 결론

screening 단일 결과는 champion을 승격시키지 않는다. 고정 screening seed에서 같은 seed의
baseline보다 primary가 좋아지고 모든 지표 유효성·coverage gate를 통과한 candidate만
확인 실험으로 보낸다. screening 값은 확인 실험 평균에 섞지 않는다.

승격을 확정하는 값은 baseline과 candidate를 같은 5개 seed로 실행한 확인 실험의 **seed별
paired normalized delta 평균**이다. 위 표의 `Δ_metric`은 이 평균이며,
`promote/revise/discard`는 확인 실험에서만 확정한다.

validation에서 고른 champion을 final holdout에서 baseline과 비교할 때도 같은 확인 실험
판정 규칙을 적용한다. 유효한 final 비교가 `promote`를 만족하면 REPORT 결론은 `개선`,
`revise` 또는 `discard`이면 원래 baseline을 최종 candidate로 유지하고 `개선 없음`으로
기록한다. final 실행 실패, registry 선점 실패, invalid predictions, metric `None`, coverage
미달처럼 유효한 비교를 만들지 못한 경우에만 `판정 불가`로 기록한다.

### 7.2 실패 처리

실행 중 실패는 사용자 질문으로 전환하지 않는다.

- (MVP 이후 논문 로드맵) 논문 원문 없음: `abstract_only`로 낮추거나 다음 논문 선택
- (MVP 이후 논문 로드맵) 현재 데이터로 검증 불가: 필요 capability를 기록하고 다음 후보 선택
- 코드 실행 실패: 제한 횟수만큼 오류를 관찰해 수정
- lint/test 실패: trial 예산 안에서 수정 후 재검증
- timeout/OOM: fidelity 또는 자원 설정을 축소해 제한 재시도
- 데이터 계약·누수 실패: candidate 즉시 차단
- 지표 악화: 변경 폐기 후 다음 가설 선택
- 전체 후보 기각: `개선 없음` REPORT 생성

각 단계는 idempotent checkpoint를 가져 프로세스 종료 후 마지막 완료 단계부터 재개할 수
있어야 한다. Job 전체를 처음부터 다시 실행하는 방식은 LLM 호출과 학습 결과를 불필요하게
잃으므로 기본 재시도 단위로 사용하지 않는다.

### 7.3 Task 5b Controller 계약

Task 5b의 공개 실행 interface는 동기식
`ResearchController.run(ControllerRunRequest) -> ControllerRunResult` 하나다. 요청은 사람이
작성한 초기 `ExperimentCard`, validation trial 수와 wall-clock 한도를 담은
`ResearchBudget`, 기준·현재 champion SHA, 검증된 `JudgeSnapshotHandoff`, 절대
`judge_state_root`, 지표별 baseline sigma, screening seed와 서로 다른 confirmation seed
5개, `TrialLedger`를 받는다. trial 수와 시간 한도는 **새 validation trial을 시작할 수 있는지**
결정하며, 이미 시작한 subprocess 회수와 validation 종료 뒤 단 한 번의 final 판정은 이 한도를
이유로 중간 취소하지 않는다.

Controller는 다음 두 seam을 생성하지 않고 주입받는다.

- `ResearchPlanner.next_card(initial_card, feedback_history)`는 이전 구조화 피드백을 보고 다음
  `ExperimentCard` 또는 계획 종료를 반환한다. Task 5b의 사람이 작성한 초기 card와 fake
  planner는 이후 Paper Discovery/Capability Matcher와 coding agent가 연결될 자리다.
- `ResearchTrialRunner.run_validation(...)`과 `run_final(...)`은 disposable workspace,
  candidate 변경, `LocalRunner`, prediction 봉인과 `ResearchDomain.validate_candidate()`·
  `evaluate()` 호출을 하나의 실행 adapter로 감춘다. Controller가 받는 성공 결과는
  same-seed `PairedJudgeResult`와 candidate SHA·diff fingerprint·duration·artifact evidence뿐이다.
  Task 5b는 이 interface와 fake adapter로 정책을 완성하고, 실제 candidate CLI와 재학습 경로를
  만드는 Task 6 및 end-to-end Task 7에서 local adapter를 연결한다.

Task 2d의 다섯 동작 interface는 유지하되 `ResearchDomain.evaluate()`에 keyword-only
`final_grant: FinalConsumptionGrant | None = None`을 추가한다. grant가 없으면 validation,
있으면 해당 grant가 승인한 final holdout만 평가한다. 이 확장은 Controller/runner가
`build_final_target()`을 직접 import하지 않게 하며, 임의 split 문자열이나 final path를
interface에 노출하지 않는다.

Controller 자신은 구체 `YouTubeCTRDomain`, slate/Judge 함수 또는 metric 구현을 import하지
않는다. screening pair에는 `ResearchDomain.compare(pair)`를 호출하고,
`should_confirm=True`일 때만 같은 candidate의 서로 다른 5-seed confirmation pair에
`ResearchDomain.compare(pairs, baseline_sigmas=...)`를 호출한다. `promote`만 champion SHA를
교체한다. `revise`, `discard`, invalid comparison과 typed 실행 실패는 기존 champion을 유지하고
다음 feedback을 만든다. planner가 종료하거나 validation trial/time 예산이 소진되면 새
validation trial을 만들지 않는다.

`FeedbackPayload`는 초기 card, 현재 trial의 card, validation metric 값, 개선이 양수인
normalized delta, decision·reason code, 이전 trial의 card 요약·decision·reason과 실패
stage·reason code·bounded stdout/stderr tail만 포함한다. 행 단위 label, prediction row,
Judge 경로·구현 코드, final metric·decision은 포함하지 않는다. 성공과 실패 trial 모두 ledger에
먼저 durable append한 뒤에만 다음 feedback history에 보인다. `TrialRecord`의 선택적
`experiment_summary`는 새 record에서 card의 canonical 요약을 보존하며, 기존 Task 4 record는
이 필드가 없는 상태로 계속 읽을 수 있다.

validation 종료 뒤 Controller는 현재 champion을 고정하고 `claim_final_consumption()`으로
marker를 원자 생성·fsync해 `FinalConsumptionGrant`를 받은 뒤에만 `run_final()`을 한 번 호출한다.
final은 baseline과 고정 champion의 5-seed pair를 같은 `ResearchDomain.compare()` 규칙으로
판정한다. 유효한 `promote`는 `improved`, `revise|discard`는 `no_improvement`, grant·실행·metric
실패와 decision 없음은 `inconclusive`다. final 결과와 registry evidence는 ledger와 반환값에만
남기고 planner나 `FeedbackPayload`에는 절대 전달하지 않는다. marker가 이미 있거나 온전하지
않으면 재평가하지 않고 `inconclusive`로 종료한다.

각 validation trial은 ledger의 trial record를 durable append한 뒤
`<trial_id>:validation_recorded` checkpoint를 남긴다. 재개 시 ledger에 이미 있는 trial 수만큼
planner를 같은 feedback history로 재생하고 완료 checkpoint의 trial은 runner를 다시 호출하지
않는다. final record/checkpoint가 있으면 final도 재실행하지 않는다. 카드 재생 결과가 기존
`experiment_summary`와 다르면 무결성 오류로 fail-closed한다. 이 결정론적 planner 재생은 MVP
checkpoint 계약이며, planner 내부 상태 snapshot은 실제 agent adapter를 연결할 때 별도
artifact로 추가한다.

## 8. YouTube 리랭킹 Domain Adapter

P0-1의 `slate_id` 생성식, 시간·click 귀속 경계, validation/final split, artifact schema,
`evaluation_id`·manifest·write-once 계약의 정본은
[`2026-08-31-research-harness-evaluation-snapshot.md`](2026-08-31-research-harness-evaluation-snapshot.md)다.
이 절은 상위 제품 경계와 P0-2 이후 소비 의미를 소유한다.

YouTube MVP의 목표는 CTR 확률 숫자만 낮은 오차로 맞히는 것이 아니라 사용자별 후보
영상의 순서를 개선하는 것이다. 사용자가 세부 지표를 선택하지 않아도 adapter가 다음
기본값을 제공한다.

- primary: `NDCG@10`
- ranking guardrail: `Recall@10`, `NDCG@24`, grouped ROC-AUC
- probability guardrail: LogLoss, Brier Score, PR-AUC
- robustness: 사용자·카테고리 그룹별 성능, seed 간 평균·분산, 평가 coverage

현재 action log에는 유저별 노출 묶음과 선택 필드인 `rank`·`exposure_source`가 있지만
명시적인 `slate_id`는 없다. 최종 학습 CSV에는 평가 전용 `user_id`만 보존되고
`video_id`와 slate 경계는 남지 않으며, NDCG·Recall 판정 경로도 아직 없다. 따라서 자율
연구 loop의 선행 기반으로 고정된 `EvaluationSlateSnapshot`을 만들어야 한다.

```text
EvaluationSlateItem
  evaluation_id
  slate_id
  user_id
  video_id
  event_timestamp
  clicked
  candidate_source
  original_rank

CandidateRanking
  evaluation_id
  slate_id
  video_id
  score
```

`CandidateRanking.score`는 단순 ranking 점수가 아니라 **`[0, 1]` 범위의 click 확률
추정치**다. 범위 검사만으로 보정 여부나 품질을 증명할 수는 없다. NaN·Inf 또는 범위 밖
값은 지표 0점으로 흡수하지 않고
`invalid_predictions` 실행 실패로 거부한다. LogLoss와 Brier는 이 확률로 계산하고,
실제 보정 품질의 악화는 LogLoss·Brier guardrail이 감시한다. 랭킹은 같은 추정치의
내림차순으로 정한다. 동률은 `video_id` 오름차순으로 끊어 실행마다 순서가 흔들리지 않게
한다.

`EvaluationSlateItem.slate_id`는 유저별 후보 노출 묶음이 확정되는 action log 생성 시점에
부여한다. 사후에 timestamp나 rank로 추론하지 않으며, `slate_id`가 없는 과거 파티션은
평가 대상에서 제외한다. slate 경계는 노출 시점에만 존재하는 사실이고 사후 추론은
근사에 불과하며, NDCG가 slate 단위로 계산되기 때문에 잘못된 경계는 오류 없이 지표를
왜곡한다.

Judge는 같은 slate와 같은 label로 baseline과 candidate를 평가한다. candidate가 자체
split이나 evaluator를 바꾸어도 최종 평가에는 영향을 주지 못한다. 더 전문적인
counterfactual label이나 graded relevance는 MVP 완주 이후 Domain Adapter의 버전된
평가 계약으로 검토한다.

### 8.1 시간 cutoff와 candidate 데이터 접근

`labels.parquet`만 숨겨서는 정답이 봉인되지 않는다. 현행 raw action log의 `EventLog`는
`event_type`으로 impression뿐 아니라 click을 그대로 저장하고
(`autoresearch/action_log_generation/schema.py:37-61`), 현행 feature build는 impression과
click을 30분 윈도우로 join해 `clicked`를 복원한다
(`autoresearch/jobs/feature_store_build.py:295-370`). candidate가 평가 기간 raw action log를
읽으면 같은 join으로 숨긴 정답을 다시 만들 수 있다.

snapshot을 만들 때 평가 **출력일** 구간을 KST 파티션 날짜 경계
`[T, T_end] = [evaluation_start_date, evaluation_end_date]`로 manifest에 봉인한다. 라벨
계산의 스캔 범위와 snapshot 출력 범위는 다음처럼 분리한다.

- click과 귀속 후보 impression은 **`dt BETWEEN T AND T_end + 1`**로 읽는다.
- slate와 labels에는 impression `dt`가 **`[T, T_end]`**인 행만 출력한다.
- `T_end + 1` 파티션이 존재하고 읽을 수 있어야 snapshot을 만든다. 없으면 자정 직전
  impression의 다음 날 click을 완전하게 관측할 수 없으므로 fail-closed한다. 이는 출력일
  `D`를 `D+2` 이후 빌드하는 기존 정본과 같은 이유다
  (`docs/specs/2026-07-26-training-entity-incremental-slice.md:68-100`).
- click을 직전 30분 내 같은 `(user_id, video_id)`의 가장 최근 impression 한 건에 붙이는
  귀속 규칙과 후보 범위는 기존 정본 및 구현
  (`autoresearch/jobs/feature_store_build.py:295-370`)과 동일해야 한다. 출력일 행만 후보로
  좁히거나 별도 규칙을 만들면 학습 라벨과 평가 라벨의 의미가 갈리므로 허용하지 않는다.

candidate 데이터 접근 계약은 다음과 같다.

- candidate workspace에는 Harness가 복사한 로컬 action log 파티션 중 **`dt < T`만** 둔다.
  `dt >= T` 파티션은 경로만 숨기는 것이 아니라 workspace에 두지 않는다.
- 이 범위는 평가 출력일 `T` 이후 impression이 파티션 `T` 이후에만 존재하므로 누출이 아니다.
  다만 candidate가 자기 history만으로 완전한 라벨을 만들 수 있는 마지막 출력일은
  **`T-2`**다. candidate가 가진 마지막 파티션 `T-1`만으로는 출력일 `T-1`의 다음 날 click을
  담을 수 없으므로, 이를 완전한 라벨로 조용히 사용해서는 안 된다.
- validation slate와 final holdout slate는 봉인된 출력일 **`[T, T_end]`**에서만 만든다.
- 평가 구간 내부에서는 아래 유저 단위 해시 80/20을 그대로 적용한다. 시간 cutoff는 평가
  정답 접근을 막고, 유저 분할은 validation 피드백으로 같은 유저 선호에 적응하는 것을
  막으므로 목적이 다르며 둘 다 필요하다.
- candidate subprocess의 argv·환경·filesystem에 GCS·BigQuery 등 원격 데이터 소스
  자격 증명을 주입하지 않는다. action log 원천은 Harness가 준 `dt < T` 로컬 파일뿐이다.
  Task 6에서는 4.5절에 따라 시점이 검증된 로컬 metadata와 임베딩 재료를 추가한다.

로컬 fixture를 쓸 때 평가 출력일 `[T, T_end]`와 스캔용 `T_end + 1`을 생성한 입력 및 seed는
Judge 전용 상태에 보관한다. candidate의 action log는 생성된 `dt < T` history로 제한한다.
Task 6 metadata 제공은 4.5절의 별도 추출 계약을 따르며 생성 입력 전체의 공개가 아니다.
평가 구간을 같은 생성기와 seed로 재생성할 수 있는 묶음은 workspace·argv·환경에 두지 않는다.

이 계약은 D3과 충돌하지 않는다. D3은 candidate가 **어떤 코드 파일을 수정할 수 있는지**를
열어 두는 연구 공간 결정이고, 시간 cutoff는 **어떤 평가 구간 데이터에 접근할 수 있는지**를
닫는 데이터 접근 결정이다. 서로 다른 층위다.

남는 한계도 있다. 같은 영상의 인기도, 시간대, 카테고리처럼 유저 외 공통 요인은
validation과 final holdout에 공유된다. 시간 cutoff는 평가 정답 자체의 접근은 막지만 이런
분포 수준 정보의 공유까지 제거하지 않으므로 두 split은 완전히 독립적이지 않다.

### 8.2 validation slate와 final holdout

하나의 평가 원천을 반복 피드백용 `validation slate`와 마지막 1회용 `final holdout`으로
나눈다. 같은 유저의 행동·선호가 양쪽에 섞이면 slate가 달라도 에이전트가 validation
피드백으로 그 유저에 적응할 수 있으므로 **유저 단위**로 분할한다.

- `bucket = int(sha256("research-harness-slate-v1:" + user_id).hexdigest()[:8], 16) % 10`
- bucket `0..7`: validation, bucket `8..9`: final holdout
- 한 유저의 모든 slate는 하나의 split에만 속한다. 두 split이 비거나 필수 지표 coverage를
  만들 수 없으면 snapshot 생성을 fail-closed한다.
- 두 split은 서로 다른 `evaluation_id`를 가지며, `CandidateRanking.evaluation_id`는 해당
  split의 manifest와 정확히 일치해야 한다.

반복 trial과 에이전트 feedback은 validation slate만 사용한다. final holdout의 label-free
slate도 반복 중에는 candidate에 주입하지 않는다. 예산이 끝나 champion이 고정되면 baseline과
그 champion을 final holdout에서 **마지막 1회만** 점수화한다. 이 단계의
지표·delta·decision은 에이전트에게 어떤 형태로도 돌려주지 않고 Controller를 종료한다.
final holdout 실행 자체가 실패하면 재평가로 정보를 더 소비하지 않고 REPORT를 `판정 불가`로
끝낸다.

### 8.3 final holdout 전역 소비 registry

final holdout 소비 상태는 run별 ledger나 checkpoint가 아니라 `evaluation_id` 기준의 **전역
소비 registry**가 소유한다. Judge 상태 루트는 필수 harness 설정
`harness-run --judge-state-root <absolute-path>`로 결정하며, 상대 경로를 받거나 run·workspace·
ledger 아래로 유도하지 않는다. registry marker의 고정 절대 경로는
`<judge-state-root>/final-holdout-consumed/<evaluation_id>`다.

Controller는 final 평가 전에 설정값을 정규화한 절대 경로로 해석하고, 상태 루트와 registry
디렉터리가 이미 존재하며 접근 가능한지 확인한다. Harness가 이를 임의로 새로 만들거나
다른 임시 경로로 fallback하지 않는다. 상태 루트가 없거나 읽기·marker 생성·`fsync`가
불가능하면 registry 선점과 final holdout 평가를 시작하지 않고 fail-closed한다.

Controller는 final 평가를 시작하기 **전에** marker를 `O_CREAT|O_EXCL`로 원자 생성한다.
marker에 `evaluation_id`, 시작 시각, 비교 대상 SHA를 쓴 뒤 파일과 부모 디렉터리를
`fsync`하고 나서만 평가를 실행한다. marker가 이미 있으면 성공 여부와 관계없이 두 번째
평가를 거부한다. marker 기록 뒤 crash가 나면 해당 holdout은 소비된 것으로 남고 REPORT는
`판정 불가`가 된다. 재평가 가능성보다 보수적 단일 소비를 우선한다.

Task 4의 registry interface는 `claim_final_consumption(request, prior_evidence=None)` 한 개다.
request는 `JudgeSnapshotHandoff`, 기준/candidate 40자리 commit SHA, timezone-aware 시작
시각과 절대 Judge 상태 루트를 받는다. registry는 `_validated_judge_snapshot()`으로 snapshot
전체를 다시 검증하고 반환 handoff가 입력과 동일한지 확인한 뒤에만 marker 생성을 시도한다.
검증된 snapshot root는 정규화된 Judge 상태 루트의 실제 하위 경로여야 하며, 다른 상태 루트를
지정해 같은 evaluation에 새 registry를 만드는 요청은 거부한다.
호출자가 `evaluation_id`나 marker 경로를 따로 고르지 않으며 final ID는 handoff에서만 가져온다.
marker는 canonical UTF-8 JSON 한 줄로
`contract_version`, `evaluation_id`, UTC 시작 시각, 기준/candidate SHA를 기록한다. 성공 결과는
marker의 정규화된 절대 경로와 SHA-256 evidence, 그리고 직접 만들 수 없는 opaque
`FinalConsumptionGrant`다. grant는 snapshot fingerprint, manifest digest, final evaluation ID와
marker evidence에 결속된다. `build_final_target(handoff, grant)`만 이 grant를 소비하며,
이 값과 handoff 동일성을 확인하고 기존 Judge 방식으로 final artifact를 다시 검증한 뒤 target을
만든다. validation factory나 handoff 단독으로 final target을 만드는 우회 interface는 두지 않는다.

`O_EXCL` marker 생성에 성공한 이후 어떤 write·file sync·directory sync가 실패해도 marker를
삭제하지 않는다.
grant는 발급하지 않지만 marker 존재 자체가 소비 사실이므로 다음 호출은 이미 소비됨으로
거부한다. 기존 marker는 내용을 parse하거나 복구하지 않고 종류·내용과 무관하게 소비로 본다.
이전 ledger/checkpoint evidence가 주어지면 registry는 고정 marker 경로와 digest를 먼저
재검증하며, 누락·불일치는 상태 무결성 오류로 거부하고 marker를 재생성하지 않는다. 오류는
`invalid_request | state_unavailable | already_consumed | integrity_violation`의 typed code와
민감한 경로·ID를 포함하지 않는 stage만 외부에 공개한다. registry 자체는 evidence 없이
사라진 marker와 최초 claim을 구분할 수 없으므로, Task 5 Controller는 복구한 ledger/checkpoint에
registry evidence가 있으면 반드시 `prior_evidence`로 전달해야 한다. marker 소실 방지는 이
호출자 계약이 지켜진 경우의 보장이다.

Trial Ledger는 registry marker의 경로와 digest를 증거로 기록할 뿐 소비 권한의 정본이
아니다. JSONL ledger의 손상된 마지막 줄 복구 규칙은 파일 하나가 소비 사실 하나인 이
registry에 적용하지 않는다. registry marker가 손상되거나 불완전해도 `evaluation_id`의
존재 자체로 이미 소비된 것으로 fail-closed한다. 이전 checkpoint·ledger·Judge evidence가
marker 생성을 기록했는데 marker가 사라졌다면 상태 루트 무결성 위반으로 보고 재생성하거나
재평가하지 않는다. marker 삭제는 정상 복구 절차가 아니다.

MVP가 보장하는 수준은 **온전한 같은 상태 루트에서 marker가 남아 있는 evaluation의 재소비를
거부하는 것**이다. 같은 UID의 candidate가 상태 루트나 marker 자체를 탐색해 삭제하는 공격은
이 보장 밖이며 위협 모델의 명시적 한계다.

## 9. Trial Ledger와 재현성

Trial Ledger는 REPORT의 근거이자 다음 iteration의 memory다. 최소한 다음을 기록한다.

- 사람이 준 가설·`ExperimentCard`와 budget
- 기준 commit, candidate commit, 전체 diff fingerprint
- 가설과 falsification 조건
- 데이터 snapshot, 파생 데이터 lineage, split과 evaluation fingerprint
- 실행 환경, dependency lock, seed, 소요 시간과 자원 사용량
- stdout/stderr 요약과 실패 reason code
- validation의 모든 지표·피드백과 Judge의 `promote/revise/discard` 또는 판정 불가 근거
- final holdout의 단일 평가 결과와 전역 소비 registry marker의 경로·digest
- champion lineage와 checkpoint

Task 4의 ledger는 하나의 run 디렉터리 안 `experiment-ledger.jsonl`을 소유한다. 공개
interface는 `open_trial_ledger(path)`, `TrialLedger.append(record)`, `TrialLedger.read_state()`로
제한한다. record는 `TrialRecord` 또는 `CheckpointRecord`이며, 각 physical line은 version,
0부터 증가하는 sequence, record type과 canonical payload를 담은 UTF-8 JSON 한 줄이다.
trial은 `trial_id`를, checkpoint는 `checkpoint_id`를 idempotency key로 사용한다. 같은 key와
동일 payload의 재호출은 기존 sequence를 반환하고 새 line을 쓰지 않으며, 같은 key의 다른
payload는 typed conflict로 거부한다.

trial payload는 validation/final 구분, 기준/candidate SHA, diff fingerprint, evaluation ID,
seed, 이름별 전체 metric, decision·reason code, duration, failure reason, artifact evidence와
champion lineage를 보존한다. checkpoint는 완료된 stage, 선택 trial ID, 완료 시각과 artifact
evidence를 보존한다. checkpoint line은 해당 단계의 외부 부작용이 완료된 뒤에만 append하며,
재개 시 `read_state()`의 완료 checkpoint ID를 건너뛰는 근거로 쓴다.

`read_state()`는 마지막 sequence, 순서가 보존된 trial/checkpoint record, 완료 checkpoint
ID 집합, 기록된 registry evidence를 담은 불변 `TrialLedgerState`를 반환한다. Task 5는 raw
JSONL을 직접 해석하지 않는다. ledger 오류 code는
`invalid_request | io_failed | idempotency_conflict | integrity_violation`로 고정한다.

append와 복구는 process 간 exclusive lock 안에서 수행하고 file `fsync`가 끝나야 성공을
반환한다. 파일이 newline 없이 끝났으면 **마지막 newline 뒤 bytes만** truncate하고 file을
sync한 뒤 복구한다. newline으로 끝난 record는 마지막 line이어도 JSON/schema 오류를 자동
복구하지 않는다. 중간 line 손상, sequence 단절, 중복 key 또는 기존 record의 schema 위반은
ledger 무결성 오류로 fail-closed한다. registry marker에는 이 tail 복구 규칙을 재사용하지
않는다.

MVP 이후에는 선택한 PaperCard와 탈락한 후보, 논문 claim에서 변환된 가설, URL, 조회 시점,
라이선스, checksum을 같은 ledger에 추가한다. 출처와 생성 과정을 재현할 수 없는 외부
데이터는 최종 candidate 근거로 사용할 수 없다.

## 10. REPORT 계약

### 10.1 MVP REPORT

MVP REPORT는 사람이 준 가설·`ExperimentCard`에서 최종 결론까지의 실행 근거를 남긴다.
최소한 실행 예산과 trial 수, 시도한 변경과 실패, validation·final holdout 지표,
promote/revise/discard 근거, checkpoint 복구 이력, 최종 candidate와 재현 좌표를 포함한다.
논문 출처와 9개 고정 절, paper manifest 교차검증은 요구하지 않는다.

### 10.1.1 구조화 기록·독립 연구 기록 Judge (#55)

Sealed Judge는 metric/승격의 정본이다. 연구 기록 Judge는 실행 종료 뒤 설명의 근거와
한계를 검토하는 advisory 역할이며 metric, champion, final 결론을 바꾸거나 feedback을
추가하지 않는다. 기존 CodingAgent interface의 새 ephemeral/read-only 호출을 재사용한다.

`publish_research_report(run_root, *, contract, result, judge, judge_workspace_parent)`는
기존 RunInputContract와 ControllerRunResult, CodingAgent를 받는 report module의
interface다. immutable run-input와 typed ledger, 종료 결과를 대조한 뒤 로컬 attempt
증거를 조립한다. 게시물은 `research-record.json`, `research-judge.json`,
`research-report.md`와 digest manifest다. 기록은 다음을 나눈다.

- 가설/card·budget·seed·baseline/champion·snapshot/evaluation 식별자.
- trial의 실제 SHA/diff/변경 경로, agent 설명/주장, 별도로 관측한 validation/final 수치.
- 실패/중단 attempt와 checkpoint, 부분 evidence와 관측되지 않은 비용.
- 모델 ID/revision/파일 identity, 라이브러리/trusted-code/input digest와 실행 설정.

Judge에는 구조화 기록만 prompt 데이터로 전달하고 도구를 사용하거나 그 안의 명령을
실행하지 않도록 지시한다. 이전 대화·candidate checkout·raw log·평가 정답·private
Judge/registry 경로는 전달하지 않는다. 빈 Judge cwd는 candidate repository와 run root
밖의 별도 workspace parent 아래 만든다. 일반 시스템 지침을 제외한 새 실행 context와
제공 입력의 분리를 보장하는 MVP이며, 같은 OS의 임의 탐색 방지를 보장하지 않는다.
runtime JSON/artifact URI를 통째로 넘기지 않고 공개 식별자·상대 경로·digest를 선택한다.
card와 agent 설명의 자유 텍스트도 알려진 private 경로를 제거한다.
새 Judge가 용어를 오해하지 않도록 기록/prompt에 도메인 의미도 전달한다.
`evaluation_id`는 모델 실행 ID가 아니라 공유 평가 snapshot/split 식별자이며,
같은 paired 비교에서는 baseline/candidate가 같은 ID를 갖는 것이 필수다.
실행 역할은 code SHA·seed로 구분하고, validation champion과 최종 채택은 다르다.

strict 응답의 상태는 `consistent|concerns|insufficient_evidence`이며 근거와 연결한
요약/지적/한계를 포함한다. 수치나 승격 결론을 생성하는 근거로 사용하지 않는다.
호출 전에 record digest·prompt/schema identity·고정 attempt 위치에 결속된 intent를
내구적으로 게시한다. 전용 lock과 write-once 게시로 동일 run의 동시/중복 호출을 막는다.
intent 뒤 crash/timeout/잘못된 응답은 unavailable로 남기고 자동 재호출하지 않는다.
복구는 동일 attempt의 성공 상태·완전한 receipt·strict 응답이 모두 맞을 때만 허용하며
intent-only 또는 cleanup 실패는 unavailable이다.

REPORT 대표 결과는 Controller final 결론과 완전한 final mean이다. final 실패/부재를
validation 최고점으로 대체하지 않는다. `ControllerRunResult.champion_sha`는 validation
champion이므로 최종 채택으로 표시하지 않고 `validation_champion_sha`, `final_decision`,
`baseline_retained`를 구분한다. baseline 평균은 digest 검증된 final `pair.json`의 5개
confirmation seed와 역할별 SHA·evaluation ID를 대조하여 구한다. 일부 pair만 있으면
부분 관측으로 남긴다. 연구 기록 Judge의 실패와 수치 판정 불가는 별도로 기록한다.

종료 결과는 input manifest·typed ledger와 결속해 보존한다. 복구할 결과 payload와
input/ledger/result digest를 담은 `controller-result-binding.json`을 먼저 게시하고
`controller-result.json`을 게시한다. binding만 남은 중단은 검증 후 결과 파일만 복구하고,
result만 있는데 binding이 없으면 결속 삭제/구버전 미결속으로 실패한다. 이미 결속된
종료 결과가 있는 run의 report 재개는 runner나
Controller·final claim을 재실행하지 않는다. final claim 실패로 final ledger가 없는
INCONCLUSIVE 결과도 지원하고, 충돌하는 결과를 덮어쓰지 않는다.

비용은 ledger에 연결되지 않은 실패를 포함해 `attempts/*/attempt.json`을 확인한다.
attempt별 시간은 `failure.json` 우선, 없으면 해당 stage의 `candidate.json`/`pair.json`
하나만 집계한다. prepare 성공 시간은 workspace 회수 전 관측값이라는 한계를 명시한다.
agent token은 `agent/receipt.json`만 집계하고 candidate 복사본/ledger를 더하지 않는다.
cached/reasoning token은 input/output에 재합산하지 않는다. 누락값은 0이 아닌 null과
coverage로 표시한다. 달러 비용과 사람 개입 횟수는 측정 장치가 없으므로 미측정이다.
검토 Judge의 usage는 봉인한 기록을 바꾸지 않고 최종 보고서에 별도로 보탠다.

게시·재사용은 digest와 regular-file/alias 검증을 따르고 입력·ledger·완료 결과가 다른
출력을 섞지 않는다. Markdown에서 agent 텍스트의 raw HTML과 임의 링크를 무력화한다.
이미지/웹 UI, 논문별 고정 9절, 추가 feedback loop는 이 PR 범위가 아니다.

### 10.2 MVP 이후 논문 기반 REPORT

REPORT는 최고 점수만 보여주는 결과 페이지가 아니라 논문에서 candidate까지의 감사 가능한
research lineage다.

1. Executive Summary
   - champion 대비 최종 결과. 대표 수치는 반복 validation 최고값이 아니라 final holdout 값
   - 실행 시간, trial 수, 사용 자원
   - 개선·개선 없음·판정 불가 중 최종 결론
2. Research Brief
   - 사용자가 제출한 목표와 Domain Adapter가 선택한 평가 기준
3. Paper Discovery
   - 검색 source와 query
   - 발견·선택·탈락 논문 및 사유
4. Experiment Lineage
   - 논문 → claim → 가설 → 코드 변경 → 결과 → 판정
5. Metric Comparison
   - primary, guardrail, seed별 값과 변동성
6. Safety and Validity
   - 데이터 누수, split, snapshot, Judge 검증
7. Negative Findings
   - 실패한 가설, 실패 원인, 다시 시도할 조건
8. Final Candidate
   - 최종 diff, artifact, 재현 명령, champion lineage
9. Reference Ledger
   - 공식 출처와 실험별 인용 관계

각 실험 설명에는 `[P03]` 같은 인라인 인용을 붙인다. Reference Ledger에는 제목, 저자,
연도, DOI/arXiv ID, 공식 URL, 검토 범위, 조회 시점, 연결된 hypothesis/trial, 최종 판정을
포함한다. 논문의 아이디어를 그대로 재현한 것인지 프로젝트 조건에 맞게 변형한 것인지도
명시한다.

원문 전체를 REPORT에 복제하지 않는다. 근거 위치와 짧은 요약만 제공하며,
`abstract_only` 논문을 원문 검토 논문처럼 서술하지 않는다.

## 11. MVP 범위

### 11.1 기존 자산 인벤토리와 판정

아래 판정은 기능 이름이 비슷한지만 보지 않고, 실제 MVP 계약을 얼마나 구현해 두었는지로
정한다. `재사용`은 기존 코드가 대부분을 제공하고 얇은 배선만 필요하다는 뜻이고,
`확장`은 기존 경계 위에 설 수 있지만 상당한 추가 구현이 필요하다는 뜻이며, `신규`는
직접 대응하는 제품 자산이 없다는 뜻이다.

| 역량 항목 | 단계 | 판정 | 현재 재사용 가능 자산 | 더 필요한 것 |
| --- | --- | --- | --- | --- |
| 추상 `ResearchDomain`과 `YouTubeCTRDomain` | MVP | 확장 | 확률 지표와 유저 단위 grouped ROC-AUC(`autoresearch/model_evaluation/evaluate.py:55-124,366-427`), snapshot·split·seed manifest(`autoresearch/model_training/training_provenance.py:96-180`) | domain capability, slate 생성·검증, 지표 방향과 비교 정책을 묶는 adapter 계약 |
| OpenAlex/arXiv 발견과 Crossref 식별자 해소 | 로드맵 | 신규 | paper provider는 없고, 재시도·rate-limit 분류·key 회전·호출 예산을 갖춘 외부 HTTP 패턴만 있다(`autoresearch/data_collection/client.py:132-186,222-318,319-365,368-393`) | provider client, 중복 제거, 원문 범위, query·선택·탈락 manifest |
| PaperCard·capability matching·출처 provenance | 로드맵 | 신규 | 엄격한 immutable Pydantic provenance 모델 패턴(`autoresearch/model_training/training_provenance.py:38-45,96-180`)과 선택적 선행 연구 입력(`applications/experiment_platform/api/experiments/issue_authoring.py:80-112`)만 간접 재사용 가능 | PaperCard 모델, claim 근거 위치, repository capability와 실행 가능성 판정 |
| 논문에서 검증 가능한 ExperimentCard 생성 | 로드맵 | 확장 | 사전등록 입력 검증과 `[AR]` 이슈 본문 조립(`applications/experiment_platform/api/experiments/issue_authoring.py:80-112,183-220`) | paper claim에서 change·falsification·metric 계약을 구조화하는 compiler |
| 전체 저장소 수정 coding agent와 독립 로컬 실행 경로 | MVP | 확장 | credential-free checkout(`applications/experiment_platform/executor/workspace.py:153-251`), process-group 회수를 포함한 Codex 호출(`applications/experiment_platform/executor/codex_worker.py:537-574,624-670`), 검증 tree commit·non-force push(`applications/experiment_platform/executor/finalizer.py:382-547`) | 현행 경로·의존성 allowlist(`applications/experiment_platform/executor/verifier.py:377-393`)를 쓰지 않는 disposable local workspace와 사후 artifact 계약. executor 배선·verifier 삭제는 후속 |
| `LocalRunner` 기반 반복 실험 | MVP | 확장 | bounded output tail(`applications/experiment_platform/executor/codex_worker.py:253-281`), process-group 회수(`applications/experiment_platform/executor/codex_worker.py:537-574`), `Popen`·timeout 처리(`applications/experiment_platform/executor/codex_worker.py:624-670`) | 교체 가능한 runner 인터페이스, candidate 예측 실행, budget loop와 iteration orchestration |
| screening과 확인 실험 | MVP | 재사용 | 조건별 seed 평가와 paired delta 조립(`applications/experiment_platform/executor/measurement.py:169-252,255-297`), 다중 seed 평균·표준편차(`autoresearch/model_evaluation/seed_sweep.py:139-248`) | 저비용/확인 fidelity 선택과 Sealed Judge 결과 연결 |
| 판정·checkpoint·실패 복구·자가 피드백 | MVP | 확장 | 상태 전이(`applications/experiment_platform/api/experiments/models.py:67-103`), 멱등 event·log·Step 영속화(`applications/experiment_platform/api/experiments/models.py:235-357`) | trial 단위 결정론 판정, 완료 단계 resume payload, 다음 iteration 전략과 구조화 feedback |
| 외부 Sealed Judge와 YouTube 평가 snapshot | MVP | 확장 | action log의 user/video/rank/source 원천(`autoresearch/action_log_generation/schema.py:37-61`), API key 없는 결정적 fixture 생성기(`autoresearch/action_log_generation/llm_generator.py:149-171`), 30분 click 귀속(`autoresearch/jobs/feature_store_build.py:295-370`), 기존 확률 지표(`autoresearch/model_evaluation/evaluate.py:366-427`) | `slate_id`, 라벨 분리 snapshot, NDCG/Recall, candidate와 분리된 Judge 프로세스 |
| Trial Ledger와 MVP REPORT | MVP | 확장 | DB의 commit·metric·event·log·Step(`applications/experiment_platform/api/experiments/models.py:139-357`), write-once 결과 게시(`applications/experiment_platform/executor/results_store.py:91-145`), agent report 입력·절 검사(`applications/experiment_platform/executor/report.py:126-159,197-230`, `applications/experiment_platform/executor/prompt.py:77-85,410-471`) | 여러 trial의 ledger, 복구·판정 근거가 연결된 로컬 REPORT |
| 출처가 연결된 최종 REPORT 9절 | 로드맵 | 확장 | 자기완결 HTML renderer(`autoresearch/reporting/report_html.py:86-149`)와 MVP ledger/report | paper lineage, 9개 REPORT 절, 원본 manifest 교차검증 |
| 웹 research request 제출과 REPORT 열람 | 로드맵 | 재사용 | 제목·가설 제출 화면(`applications/experiment_platform/workbench/views.py:173-180,240-260`), 실험 목록·상세 조회(`applications/experiment_platform/workbench/views.py:337-410`), safe Markdown→HTML과 iframe 표시(`applications/experiment_platform/workbench/report.py:29-42,70-129`, `applications/experiment_platform/workbench/views.py:613-633`) | budget 입력, research run API 배선, 최종 multi-trial REPORT 조회 모델 |

이 인벤토리는 MVP에서 제외된 항목을 폐기하지 않는다. MVP는 현재 코드 위에 바로 세울 수
있는 반복 실행·봉인 평가 기반을 먼저 완주하고, 논문·출처·웹 제품 계층은 그 결과를
입력 seam과 evidence로 재사용하는 다음 단계다.

### 11.2 MVP 포함

- 추상 `ResearchDomain`과 YouTube 구현 1개
- 전체 연구 저장소를 수정할 수 있는 coding agent와 현행 executor를 수정하지 않고 정적
  allowlist를 사용하지 않는 독립 로컬 봉인 artifact 판정 경로. executor 배선과 verifier
  삭제는 후속이다
- 외부 Sealed Judge와 YouTube 리랭킹 평가 snapshot
- LocalRunner 기반 반복 실험
- screening과 확인 실험
- promote/revise/discard, checkpoint, 실패 복구
- Trial Ledger
- 사람이 준 가설·`ExperimentCard`로 동작하는 자가 피드백 반복 루프와 MVP REPORT

### 11.3 MVP 이후 로드맵

다음은 제외가 아니라 MVP 위에 이어서 구현할 다음 단계다.

- OpenAlex/arXiv 논문 자동 발견과 Crossref 식별자 해소
- PaperCard, capability matching, 출처 provenance
- 논문 claim에서 `ExperimentCard`를 만드는 compiler
- 출처가 연결된 최종 REPORT 9개 절과 paper manifest 교차검증
- 기존 웹 workbench의 research request·budget 제출과 최종 REPORT 조회 배선

### 11.4 영구 제외와 재검토 시점

| 제외 항목 | 제외 이유 | 재검토 시점 |
| --- | --- | --- |
| 실제 이커머스·뉴스피드 adapter | YouTube adapter 하나로 interface와 end-to-end를 검증할 수 있고 두 번째 domain은 신규 데이터 계약을 만든다 | YouTube 제품 MVP가 한 번 완주하고 domain interface 변경점이 수렴한 뒤 |
| Kubernetes를 필수 실행 경로로 만드는 작업 | 로컬 완주와 제품 가치를 증명하는 데 필요 없고 인접 Airflow/infra 책임까지 넓어진다 | 로컬 실행에서 원격 GPU·다중 사용자 격리 요구가 실측된 뒤 `KubernetesJobRunner`로 연결 |
| 대규모 분산 학습과 multi-GPU scheduling | MVP 데이터·trial 예산에 비해 신규 orchestration 비용이 크다 | 단일 runner 자원 상한 때문에 검증 가능한 가설을 반복해서 포기하게 될 때 |
| production 자동 배포·무인 champion 전환 | 연구 판정과 운영 배포의 실패 반경이 다르다 | offline 판정의 재현성과 운영 승인 계약이 별도 spec으로 확정된 뒤 |
| 실제 사용자 온라인 A/B 테스트 | 사용자 트래픽·동의·운영 guardrail이 별도 제품 책임이다 | offline final holdout 신뢰성이 검증되고 온라인 실험 플랫폼 소유자가 정해진 뒤 |
| 유료 논문 원문 우회 수집 | 라이선스와 접근 통제를 침해하며 MVP 기능이 아니다 | 재검토하지 않는다. 정식 라이선스 연동은 별도 제품 기능으로만 검토한다 |
| 범용 웹 검색 결과를 검증 없이 연구 근거로 사용 | 출처 검증과 재현 계약을 깨뜨린다 | 구조화 paper source의 recall 부족이 실측되면, 검증·canonicalization 계약을 먼저 설계한 뒤 |
| Research Harness나 실행 중인 Sealed Judge의 자기 수정 | root of trust를 없애 현재 run의 판정을 무효화한다 | 새 고정 digest로 **다음 run**을 시작하는 배포 절차로만 검토한다 |
| 별도 OS 사용자·container·read-only mount를 이용한 적대적 candidate 완전 격리 | 로컬 제품 MVP의 방어 대상은 실수와 자기 채점 오염이며, 같은 사용자 프로세스의 호스트 탐색까지 막는 sandbox는 신규 실행 기반이 필요하다 | 불신 코드나 다중 사용자 run을 받기 전에 위협 모델과 실행 기반을 별도 spec으로 확정한다 |

## 12. MVP 완료 조건

- [ ] 사람이 준 가설과 `ExperimentCard`, 예산으로 research run을 시작한다.
- [ ] 에이전트가 저장소 전체 범위에서 candidate를 만들 수 있다.
- [ ] 이전 결과를 관찰해 서로 다른 trial을 순차 실행한다.
- [ ] 의도적으로 깨진 candidate에서 자동 복구하고 다음 trial을 계속한다.
- [ ] 동일한 Sealed Judge로 baseline과 candidate를 비교한다.
- [ ] candidate action log는 `dt < T`로 제한하며 추가 metadata·임베딩 재료는 4.5절을 따른다.
      원격 데이터 자격 증명·평가 action log·fixture 생성 상태와 seed는 주지 않는다. candidate history의
      완전 라벨 출력일 상한은 `T-2`로 강제한다.
- [ ] 평가 출력일 `[T, T_end]`의 click·귀속 후보 impression은
      `dt BETWEEN T AND T_end + 1`로 스캔하고 출력은 `[T, T_end]` impression으로 제한하며,
      `T_end + 1` 파티션이 없으면 snapshot 생성을 거부한다.
- [ ] 유저 단위로 분리된 validation에서만 반복 피드백하고, final holdout은 마지막 1회만
      평가해 에이전트에게 피드백하지 않는다.
- [ ] 필수 절대 `judge_state_root`가 없거나 접근 불가하면 final 평가를 시작하지 않고,
      온전한 같은 상태 루트에서는 `evaluation_id` marker가 남은 final holdout의 재소비를
      거부한다. 상태 루트 자체의 무결성은 위협 모델의 한계다.
- [ ] click 확률 추정치·σ·metric 값·지표별 coverage 계약이 불완전하면 판정을 내리지 않고,
      보정 품질은 LogLoss·Brier guardrail로 감시한다.
- [ ] 프로세스를 중단한 뒤 마지막 checkpoint부터 재개한다.
- [ ] 개선 여부와 무관하게 지표·commit·snapshot·복구 이력이 ledger와 일치하는 MVP
      REPORT를 생성한다.
- [ ] 사람의 중간 승인 없이 시작부터 REPORT까지 완주한다.

### MVP 이후 로드맵 완료 조건

- [ ] 자연어 목표와 예산만으로 research run을 시작한다.
- [ ] 서비스가 논문을 자동 발견하고 선택·탈락 이유를 기록한다.
- [ ] 선택한 논문 하나 이상을 코드 변경이 있는 검증 가능한 가설로 변환한다.
- [ ] REPORT의 논문, 수치, commit, snapshot이 원본 paper manifest·ledger와 일치한다.
- [ ] 기존 웹 workbench에서 request·budget을 제출하고 최종 REPORT를 조회한다.

## 13. 오픈소스 차별점

paper-grounded hypothesis generation은 최종 제품의 핵심 차별점으로 유지하지만 이번 MVP의
구현 범위는 아니다.

오픈소스 가치의 중심은 웹에서 작업을 제출하거나 자리를 비워도 실행이 계속되는 기능이
아니다. 다음 연구 방법론과 재현 가능한 reference implementation이 차별점이다.

- paper-grounded hypothesis generation
- repository capability-aware paper selection
- full-repository mutation with an external sealed evaluator
- iterative experiment control loop and failure recovery
- negative-result-aware trial memory
- paper-to-code-to-metric provenance
- local-first runner와 교체 가능한 원격 backend
- 한 도메인에 최적화되면서도 adapter로 확장 가능한 구조

## 14. 이력서·데모 포지셔닝

논문 기반 가설 생성과 출처가 연결된 REPORT는 최종 데모 목표이며 이번 MVP 범위는 아니다.

프로젝트는 CTR 연구 전문성을 과장하기보다 자율 에이전트 시스템과 재현 가능한 ML 실험
설계를 강조한다.

> 논문을 자동 발견하고 코드 변경·모델 학습·반복 평가·실패 복구를 수행한 뒤, 실험
> 계보와 참고문헌을 포함한 보고서를 생성하는 자율 ML 연구 에이전트를 설계·구현했다.

가장 중요한 데모는 논문 수나 단일 최고 점수가 아니다. 서비스가 관련 논문을 발견하고,
현재 데이터로 검증 가능한 가설을 선택하고, 저장소를 수정하고, 실패를 복구하며 여러
trial을 수행한 뒤, 채택 또는 기각 근거와 출처가 연결된 REPORT를 생성하는 한 번의 완전한
실행이다.

## 15. 구현 순서 원칙

MVP 구현은 [로컬 Research Harness MVP 계획](../plans/2026-08-15-local-research-harness-mvp.md)의
의존 순서를 따른다. 로컬 Harness를 신규로 만들고 현행 Kubernetes executor는 MVP에서
수정하지 않으며, 이후 `ExperimentRunner` 구현체로 흡수한다.

1. `EvaluationSlateSnapshot`, 리랭킹 지표와 Sealed Judge
2. 그 세 구현 위의 `ResearchDomain`과 YouTube adapter
3. Trial Ledger, LocalRunner와 disposable candidate workspace
4. `ResearchDomain`을 호출하는 checkpoint 가능한 Research Controller와 반복
   `promote/revise/discard` loop
5. Trial Ledger 근거와 MVP REPORT

MVP 이후에는 Paper Discovery와 PaperCard → Capability Matcher와 ExperimentCard compiler →
출처가 연결된 9절 REPORT → 기존 웹 workbench 연결 순으로 진행한다. 원격 자원 필요가
실측된 뒤에만 KubernetesJobRunner를 연결한다.

구현 착수 전 기존 관련 spec과의 대체·확장 관계를 이슈에서 확정하고, 저장소 workflow에
따라 이슈에서 생성한 브랜치와 구현 plan을 사용한다.
