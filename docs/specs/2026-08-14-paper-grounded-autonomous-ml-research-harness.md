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
변조를 검출하고, **64 MiB + 1 byte**까지만 읽어 상한 초과를 확인한다. Judge 소유 목적지는
`O_CREAT|O_EXCL`로 만들어 기존 파일을 덮어쓰지 않으며, 이후 candidate 경로가 아니라 이
사본만 파싱한다. 이 64 MiB 상한은
candidate workspace나 commit의 파일 크기를 제한하는 D4를 되살리는 것이 아니라, 불신
프로세스가 Judge에 넘기는 단일 artifact의 메모리·디스크 소비를 제한하는 입출력 계약이다.

CSV parser는 header를 포함해 필드가 정확히 `evaluation_id,slate_id,video_id,score` 4개인지
확인한다. 세 ID는 comma·quote·개행 없는 ASCII로 각 최대 64 byte, `score` token은 최대
24 byte로 제한한다. CRLF까지 허용한 한 행의 최악 크기는
`64 + 64 + 64 + 24 + comma 3 + CRLF 2 = 221 byte`이고 header는 최대 39 byte다.
따라서 유효 행 상한은 **300,000행**으로 둔다. 최악 크기 `39 + 300,000 * 221 =
66,300,039 byte`는 64 MiB(`67,108,864 byte`) 안에 들어가므로 artifact 상한과 모순되지
않는다. 행 수는 이 상한 이하이면서 대상 slate와 정확히 같아야 한다. 격리된 parser
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
예외의 모든 경로에서 TERM grace 뒤 KILL과 최종 wait를 수행해 child·grandchild를 완전히
회수하며, 남은 프로세스가 있으면 trial을 성공으로 기록하지 않는다. 재사용 출발점은 현행
Codex worker의 process-group 회수 계약이다
(`applications/experiment_platform/executor/codex_worker.py:537-574,624-670`).

Kubernetes Job은 다중 사용자 격리, 원격 자원, GPU scheduling이 필요할 때 선택하는
`ExperimentRunner` 구현체다. Kubernetes 배포 자체를 제품 차별점으로 삼지 않는다.

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
    def describe_capabilities(self) -> DomainCapabilities: ...
    def build_evaluation_snapshot(self) -> EvaluationSnapshot: ...
    def validate_candidate(self, candidate: CandidateArtifact) -> ValidationResult: ...
    def evaluate(self, candidate: CandidateArtifact) -> DomainMetrics: ...
    def compare(self, champion: TrialResult, candidate: TrialResult) -> Decision: ...


class PaperSource(ABC):
    async def search(self, query: PaperQuery) -> list[PaperMetadata]: ...
    async def get(self, paper_id: PaperId) -> PaperDocument: ...
    async def citations(self, paper_id: PaperId) -> CitationGraph: ...


class ExperimentRunner(ABC):
    async def run(self, candidate: CandidateArtifact) -> TrialResult: ...


class LocalRunner(ExperimentRunner): ...
class KubernetesJobRunner(ExperimentRunner): ...
```

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
점수화하는 작업이 아니라, 현행 champion 설정을 seed마다 **독립적으로 5회 재학습**한 뒤
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

| 판정 | 조건 |
| --- | --- |
| `promote` | `Δ_ndcg_at_10 ≥ 2σ_ndcg_at_10`이고 모든 guardrail `Δ_metric ≥ -1σ_metric` |
| `revise` | `Δ_ndcg_at_10 ≥ 2σ_ndcg_at_10`이지만 하나 이상의 guardrail `Δ_metric < -1σ_metric` |
| `discard` | 그 외 |

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

## 8. YouTube 리랭킹 Domain Adapter

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
  자격 증명을 주입하지 않는다. candidate가 볼 수 있는 원천은 Harness가 준 `dt < T` 로컬
  파일뿐이다.

로컬 fixture를 쓸 때 평가 출력일 `[T, T_end]`와 스캔용 `T_end + 1`을 생성한 입력 및 seed는
Judge 전용 상태에 보관한다. candidate에는 생성된 `dt < T` history만 줄 수 있으며, 평가
구간을 같은 생성기와 seed로 재생성할 수 있는 입력은 workspace·argv·환경에 두지 않는다.

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

MVP 이후에는 선택한 PaperCard와 탈락한 후보, 논문 claim에서 변환된 가설, URL, 조회 시점,
라이선스, checksum을 같은 ledger에 추가한다. 출처와 생성 과정을 재현할 수 없는 외부
데이터는 최종 candidate 근거로 사용할 수 없다.

## 10. REPORT 계약

### 10.1 MVP REPORT

MVP REPORT는 사람이 준 가설·`ExperimentCard`에서 최종 결론까지의 실행 근거를 남긴다.
최소한 실행 예산과 trial 수, 시도한 변경과 실패, validation·final holdout 지표,
promote/revise/discard 근거, checkpoint 복구 이력, 최종 candidate와 재현 좌표를 포함한다.
논문 출처와 9개 고정 절, paper manifest 교차검증은 요구하지 않는다.

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
- [ ] candidate에는 `dt < T` 로컬 action log만 보이고 원격 데이터 자격 증명과 `dt >= T`
      평가 파티션, fixture 평가 구간 생성 입력·seed는 보이지 않는다. candidate history의
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
