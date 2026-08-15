# 실험 전용 Feast registry 분리·보존 계약

- 상태: 초안 (이슈 미발행 — 아래 §미결 사항 2건은 `Autoresearch-infra` 확인 후 확정)
- 관련: #399(피처 스토어 prod/dev 분리) · #530(학습 데이터셋 스냅샷 스토어) ·
  #546(실험 브랜치 Bootstrap Job Phase 1) · #423/#466(학습 실험 provenance) ·
  #409(ODFV dill body 갱신 실패) · #537(manifest first-publisher-wins)
- 선행 문서: `docs/specs/2026-08-05-experiment-job-baseline-freeze.md`,
  `docs/specs/2026-08-04-training-dataset-snapshot-store.md`

## 배경

UI에 입력한 가설은 `[AR]` 이슈로 발행되고, launcher가 executor Pod를 띄워 실험을
실행한다. 가설이 3~5개 한 번에 등록되면 Pod도 그만큼 뜬다.

실험이 피처를 새로 정의하려면 `feast apply`가 필요한데, dev registry는
`GCS_REGISTRY_PATH`가 가리키는 **GCS 객체 하나**다(`.env.example:71`). 여러 실험이
같은 객체에 apply하면 두 종류의 손상이 생긴다.

1. **쓰기 경합** — Feast의 file registry는 다운로드 → 수정 → 업로드이고 전제조건
   없이 덮어쓴다. `GCSRegistryStore._write_registry()`가 `blob.upload_from_file()`만
   호출하므로(feast 0.64.0) 동시 apply는 last-writer-wins가 된다.
2. **선언적 reconcile에 의한 삭제** — `feast apply`는 레포 정의와 registry를
   맞추면서 레포에 없는 FeatureView를 삭제한다. #346 spec의 검증 절차가 이 동작을
   전제한다("임시 FV 추가 → apply → 삭제 → apply 후 `Deleted N rows ...` 확인").
   즉 실험 B의 apply는 B 브랜치에 없는 실험 A의 FeatureView를 지운다.

2번이 이 문서의 핵심이다. 1번만 있었다면 상호배제로 충분하지만, 2번은 A가 apply를
끝낸 **뒤에** 일어나므로 apply 구간을 잠그는 것으로는 막히지 않는다.

## 목표

1. 실험 Pod가 여러 개 떠도 **아무도 기다리지 않고** 각자 `feast apply`를 수행한다.
2. 실험이 사용한 피처 정의를 **나중에 다시 찾아** 같은 실험을 재현할 수 있다.

## 현재 상태 — 아직 발생 조건이 아니다

- executor Pod는 지금 exp 브랜치 생성만 한다
  (`agent_orchestration/executor/main.py`). Codex 실행·코드 수정·학습은 #546
  Phase 1의 비범위다.
- `feast apply`는 Pod가 아니라 `.github/workflows/feast-apply.yml`이 트리거하고,
  이 워크플로우에는 `concurrency: {group: feast-apply, cancel-in-progress: false}`가
  걸려 있어 prod/dev 구분 없이 전역 직렬화된다.

따라서 오늘은 겹치지 않는다. 문제는 **executor가 GHA를 거치지 않고 직접 apply하는
다음 Phase에 발생**한다. 이 계약은 그 Phase의 선행 조건이다.

## 왜 분산락이 아닌가

락은 상호배제 원시이지 격리 원시가 아니다. 필요한 정합성은 "내가 apply한 registry
내용이 **내 학습·평가가 끝날 때까지** 유지된다"인데, 락으로 그 구간을 덮으면 실험이
완전 직렬화되고(목표 1 위반), 덮지 않으면 보호가 0이다. 중간값이 없다.

apply 뒤 스냅샷을 떠서 그것만 읽으면 이 구간 불일치는 닫힌다. 그러나 그 경우
**복사를 apply 앞으로 옮기면 공유 자원 쓰기 자체가 사라져 락이 불필요해진다.**
복사본 개수는 같다.

락 방식이 추가로 지불하는 비용도 있다.

- executor Pod에 조율용 Postgres 또는 Redis 접근 권한을 줘야 한다. 현재 executor는
  메모리 volume의 GitHub token 파일 하나만 갖는다(#546이 private key도 넘기지 않으려
  initContainer를 분리한 설계다). Codex가 사용자 코드를 실행하게 될 Pod에 조율용
  자격을 두는 것은 되돌리기 어렵다.
- dev는 의도적으로 Redis-free다(#399 D1/D3, dev는 `full_scan_for_deletion: false`).
  조율용으로 Redis를 끌어들이면 그 경계가 깨진다.
- 자율 에이전트는 정의를 고쳐 재-apply하는 것이 기본 동작이므로 락 획득이 실험당
  여러 번 반복된다.

## 결정

### D1. 실험 registry는 이슈 발행 직후 API가 만든다

Experiment API가 `[AR]` 이슈를 발행한 **직후**, 공유 dev registry를 그 실험 전용
경로로 복사한다. 복사 대상은 서버 사이드 object copy이며 파일을 내려받았다 올리지
않는다.

```
작업용(가변)
gs://<registry_root>/work/issue-<issue_number>/exp-<experiment_id>/registry.db
```

경로에 이슈 번호와 실험 ID를 모두 넣는다. 이슈 번호만 쓰면 같은 이슈를 다시 실행할
때 경로가 충돌하고, 실험 ID만 쓰면 버킷을 열었을 때 어느 이슈의 것인지 알 수 없다.
둘을 겹치면 `work/issue-601/` prefix 조회로 그 이슈의 실행이 모두 나오면서 각 실행은
고유하다.

복사 시 원본 dev registry의 generation을 복사본의 custom metadata로 남긴다. 어느
기준선에서 갈라졌는지를 기록하는 용도이며, 재현은 이 값이 아니라 D3의 보존본으로
한다(§D6).

**executor Pod는 복사하지 않는다.** 전달받은 경로를 `GCS_REGISTRY_PATH`로 그대로
쓴다. `AUTORESEARCH_ENV`는 `dev`를 유지한다 — 실험 registry는 dev 환경의 하위
좌표이지 세 번째 환경이 아니다. 따라서 `full_scan_for_deletion`은 dev 규칙(false)을
그대로 따르고 실험 apply는 Redis에 접속하지 않는다. Redis에 접속하지 않으므로 별도
feast-apply GKE Job을 만들 필요 없이 Pod 안에서 직접 apply한다.

`GCS_REGISTRY_PATH`는 이미 env 주입이고 #399가 "코드 변경 0"으로 확인한 좌표다.
apply 대상 경로를 바꾸는 데 `feature_repo/`나 `feature_store.yaml`의 변경은 필요
없다.

### D2. 기준선은 참조가 아니라 실물로 고정한다

`base_dev_sha`가 코드 기준선을 고정하듯 registry 기준선도 수락 시점에 고정한다.
다만 generation 번호를 저장해두고 나중에 역참조하는 방식이 아니라, **그 시점에 실물을
복사**한다(D1).

이 차이가 중요하다. 참조 방식은 실험이 대기하는 동안 승격 apply가 dev registry를
덮어쓰면 그 generation이 noncurrent version이 되므로, 버킷의 object versioning
활성화에 의존한다. 실물 복사는 객체를 소유하므로 이후 dev registry가 어떻게 바뀌든
무관하고 versioning에 의존하지 않는다.

`Experiment`에 컬럼 하나를 추가한다.

| 컬럼 | 형식 | 의미 |
| --- | --- | --- |
| `work_registry_uri` | `String(512)`, nullable | D1이 만든 실험 전용 registry의 gs:// 경로 |

launcher의 선점 조건에 이 컬럼을 포함한다. 현재 `issue_number`·`issue_branch`·
`base_dev_sha`가 모두 있어야 dispatch하는 fail-closed 계약(#546)에 한 항목을 더하는
것이다. 값이 없으면 Job을 만들지 않는다.

복사는 멱등이다. 재호출 시 이미 `work_registry_uri`가 있으면 다시 복사하지 않고 그
값을 그대로 쓴다. 이슈 발행은 성공했는데 복사가 실패하면 그 행은 `work_registry_uri`가
비어 선점되지 않으며, 재호출이 복사부터 이어서 수행한다.

### D3. 확정된 registry는 content-addressed로 write-once 보존한다

실험이 최종 apply를 마치면 `registry.db`의 SHA-256을 주소로 보존 영역에 게시한다.

```
보존용(불변)  gs://<registry_root>/by-hash/<registry_sha256>/registry.db
              if_generation_match=0 (write-once), 412는 no-op으로 흡수
```

이 레이아웃과 게시 규칙은 #530의 `src/pipeline/training_snapshot_store.py`가 이미
구현한 것과 같다. 새 스토어를 발명하지 않고 같은 계약을 registry 객체로 확장한다.

보존 주소를 이슈 키가 아니라 내용 해시로 잡는 이유는 셋이다.

1. **불변성이 구조로 보장된다.** 이슈 키 경로는 가변이라 재시도·재-apply가 덮어쓰면
   재현용으로 보존한 아티팩트가 조용히 사라진다. write-once면 그 경로가 없다.
2. **중복이 제거된다.** 피처 정의를 바꾸지 않은 실험은 같은 주소로 수렴한다.
3. **주소가 이미 계산된다.** `TrainingSnapshotManifest.registry_sha256`이 그 값이다.

이슈로 찾아가는 경로는 D1의 `work/issue-<n>/`과 Experiment 행의
`work_registry_uri`·MLflow run이 담당한다. 보존 주소는 조회 키가 아니라 내용
식별자다.

`work/`와 `by-hash/`는 lifecycle 정책이 다르다.

| prefix | 정책 | 근거 |
| --- | --- | --- |
| `work/` | 생성 후 **14일** 자동 삭제 | 실험 주기가 하루 이내이므로 실패 실험의 디버깅 여유로 충분하다. `registry.db`가 작아 스토리지는 제약이 아니다 |
| `by-hash/` | 무기한 보존 | 재현 대상이다. 삭제 rule이 걸리면 보존이 조용히 깨진다(§미결 사항) |

`work/`가 14일 뒤 사라져도 재현은 영향받지 않는다. 재현이 읽는 것은 실험이 **확정한**
`by-hash/` 보존본이지 출발점 사본이 아니다.

### D4. provenance manifest 스키마는 바뀌지 않는다

`TrainingSnapshotManifest`(`src/pipeline/training_provenance.py:96`)는 이미
`registry_uri`·`registry_generation`·`registry_sha256`을 담고,
`build_training_dataset._download_pinned_registry()`가 "현재 generation을 고정해
내려받고 Feast에는 local path를 전달"한다. 실험이 자기 registry를 읽으면 그 좌표가
자동으로 기록된다.

by-hash 주소는 불변이므로 그 주소의 generation은 언제 읽어도 같다. 따라서 기존
generation pinning 로직을 고칠 필요가 없다. `TrainingSnapshotManifest`는
`extra="forbid"`·`frozen`이므로 필드 추가는 비용이 크지만, **이 계약은 필드를
추가하지 않는다.**

`src/pipeline/paired_experiment.py`의 `ConditionLineage`도 마찬가지다. 현재
`registry_uri`만 담고 generation·sha256이 없어 공유 가변 registry에서는 정체성이
성립하지 않았으나, URI가 by-hash 주소가 되면 URI 하나로 정체성이 확정된다. 모델
변경 없이 계약이 강화된다.

### D5. 공유 registry 쓰기는 승격 경로에만 남는다

실험이 게이트를 통과하면 exp 브랜치를 `dev`에 머지하고, 기존
`.github/workflows/feast-apply.yml`(push: dev)이 공유 dev registry를 갱신한다. 이
경로가 유일한 공유 쓰기이고 GHA concurrency 그룹이 이미 직렬화한다.

실험 간 피처 누적은 실험끼리 직접 일어나지 않고 승격을 통해서만 일어난다. 이후
수락되는 실험은 전진한 dev registry를 복사한다. 검증을 통과한 정의만 기준선에
반영되므로 격리의 "늦은 통합" 비용은 여기서 상쇄된다.

### D6. 재현 단위는 registry 하나가 아니라 receipt 한 벌이다

registry만 보존하면 재현되지 않는다. ODFV의 UDF는 dill로 직렬화돼 registry 안에
들어가므로, feast·python·의존성 버전이 바뀌면 보존된 registry가 열리지 않을 수
있다. #409는 apply가 성공하고 generation도 바뀌었는데 ODFV의 dill body가 그대로여서
학습이 계속 깨진 실사례이고, `scripts/verify_registry_portability.py`가 존재하는
이유다.

따라서 보존 시 **image digest를 함께 봉인**하고, 재현은 "그때 그 이미지 + 그
registry"로 정의한다. 필요한 좌표는 `ConditionLineage`가 이미 정의한 집합
(`source_sha`, `image_digest`, `code_archive_sha`, `code_archive_uri`,
`registry_uri`, `feature_schema_fingerprint`)에 `base_dev_sha`를 더한 것이다.

재현 절차는 다음을 모두 통과해야 성공으로 인정한다.

1. receipt의 `registry_uri`로 by-hash 객체를 내려받고 주소의 sha와 바이트 SHA-256이
   같은지 대조한다. 다르면 중단한다.
2. `scripts/verify_registry_portability.py`로 로드 가능성을 확인한다. 실패하면
   fail-closed로 중단하고, receipt의 `image_digest` 런타임으로 재시도하도록 안내한다.
3. 학습 데이터셋은 #530 by-hash 스냅샷을 재사용해 재조립하지 않는다.

## 좌표 수명주기

```text
가설 수락 (API Pod)
  → heads/dev 1회 조회 → base_dev_sha 저장
  → Experiment 행 저장 (CREATED)
  → [AR] 이슈 발행 → issue_number, issue_branch 저장
  → dev registry 를 work/issue-<n>/exp-<uuid>/registry.db 로 복사
  → work_registry_uri 저장                                    ← 여기까지 있어야 dispatch
launcher 선점
  → 봉인 좌표를 executor Job env 로 전달
executor Pod
  → base_dev_sha 에서 exp branch 생성
  → GCS_REGISTRY_PATH = work_registry_uri
  → 자기 registry 에 N회 apply, 그 사이 학습·평가 반복
  → 최종 apply 후 sha256 계산 → by-hash/<sha>/ 에 write-once 게시
  → 이후 학습은 by-hash 주소를 읽는다
승격
  → exp branch 를 dev 에 머지 → feast-apply.yml 이 공유 dev registry 갱신
```

현재 launcher가 주입하는 env는 `ORCH_EXPERIMENT_ID`, `ORCH_ISSUE_NUMBER`,
`ORCH_ISSUE_BRANCH`, `ORCH_BASE_DEV_SHA`, `ORCH_GITHUB_REPOSITORY`,
`ORCH_GITHUB_TOKEN_FILE`이다(`agent_orchestration/launcher/jobs.py:121-128`).
여기에 `ORCH_WORK_REGISTRY_URI`와 보존 루트를 더한다.

실험 구간(수락 이후 ~ 승격 전) 동안 공유 dev registry에 대한 쓰기는 **한 건도 없다.**
따라서 실험끼리 기다리지 않는다.

## 소유 경계

- **Autoresearch**: registry 복사·게시·다운로드 모듈, `work_registry_uri` 컬럼과
  migration, launcher의 선점 조건·좌표 주입, executor의 apply·게시 단계, 재현 검증
  절차.
- **Autoresearch-infra**: registry 버킷의 lifecycle 정책(prefix별 분리), API Pod
  ServiceAccount의 `work/` prefix 쓰기 권한, executor ServiceAccount의 `work/`·
  `by-hash/` 쓰기 권한.
- **Autoresearch-airflow**: 학습·평가 Job에 실험 registry 좌표를 주입하는 배선.
  공개 batch 계약은 registry 좌표를 환경 변수로 받으므로 Job spec 변경이 필요하다.

## 전제

- Experiment 행의 유일한 생성 경로는 Experiment API다. GitHub에서 직접 만든 이슈가
  launcher로 유입되는 경로가 있다면 D1이 성립하지 않는다.

## 비범위

- executor의 Codex 실행·코드 수정·검증·candidate SHA push (#546 후속 Phase)
- executor 이미지에 feast를 포함시키는 변경. Phase 1 이미지는 검증된 branch
  bootstrap 코드만 포함하도록 제한돼 있으므로(#546), Pod 내 apply는 그 경계를 다시
  설계하는 후속 Phase에서 함께 다룬다.
- `feast-apply.yml` concurrency 그룹의 대기 런 취소 문제 (별도 이슈)
- prod registry 운영 변경 — 이 계약은 dev 실험 경로에만 적용된다
- 비교 집합(`baseline_cohort_id`) batch 등록 API
- **Feast registry 쓰기 semantics의 실증.** apply 결과가 레포 정의만의 함수인지,
  다운로드한 registry에서 이어받는 상태가 있는지는 확인되지 않았다. D2가 실물 복사로
  모든 실험에 동일한 방식의 고정 출발점을 주므로 어느 쪽이든 이 계약은 성립한다 —
  결정에 영향을 주지 않으므로 이번 범위에 넣지 않는다.
- **보존된 registry의 주기적 portability 검증.** D6은 재현 시도 시점의 fail-closed만
  둔다. 미리 알아도 이미 열리지 않는 registry를 되살릴 방법은 `image_digest` 런타임
  복원 외에 없다.
- #537이 추적하는 manifest first-publisher-wins 정밀도 문제. by-hash registry
  객체에도 같은 성질이 있다 — 같은 정의를 쓴 두 실험은 한 주소를 공유하고, 그 주소에
  함께 올라가는 메타데이터는 최초 게시자의 것으로 고정된다. registry 바이트 자체는
  영향받지 않으므로 무결성 문제가 아니라 provenance 정밀도의 한계이며, "이 registry를
  쓴 실험 목록"은 실험 receipt에서 역방향으로 조회한다.

## 미결 사항

둘 다 `Autoresearch-infra` 소유다.

1. **`by-hash/` prefix에 걸린 lifecycle 삭제 rule 유무** (D3의 전제). 보존 영역에
   자동 삭제 rule이 적용되면 재현용 아티팩트가 조용히 사라진다.

   GCS lifecycle의 `matchesPrefix`는 **포함 조건만 있고 제외 조건이 없다.** 따라서
   기존에 prefix 없는 age rule이 걸려 있으면 "`by-hash/`만 예외"를 추가할 수 없고,
   rule 자체를 아래 형태로 다시 써서 삭제 대상을 `work/`로 한정해야 한다.

   ```json
   {"condition": {"age": 14, "matchesPrefix": ["<prefix>/registry/work/"]},
    "action": {"type": "Delete"}}
   ```

2. **API Pod ServiceAccount의 GCS 쓰기 권한 범위** (D1의 전제). 외부에 노출된
   표면이므로 `registry/work/` prefix에만 쓰기를 허용하고, 보존 영역(`by-hash/`)과
   공유 dev registry 객체에는 쓰지 못하게 한다.

## 검증

- 복사 단위 테스트: 이슈 발행 후 경로 형식(`work/issue-<n>/exp-<uuid>/registry.db`),
  멱등 재호출이 재복사하지 않음, 복사 실패 시 `work_registry_uri`가 비어 선점되지 않음.
- 게시 단위 테스트: write-once 최초 게시, 412 no-op 흡수, 주소 sha와 바이트 sha
  불일치 거부, 재시도 소진 시 오류 메시지에 로컬 경로 포함(#530 패턴과 동일).
- 선점 fail-closed 테스트: `work_registry_uri`가 없는 행은 Job을 만들지 않는다
  (#546의 좌표 fail-closed 계약과 동일).
- 좌표 전파 테스트: launcher가 `ORCH_WORK_REGISTRY_URI`를 Job env로 전달한다.
- 동시성 회귀: 실험 N건이 동시에 apply해도 공유 dev registry의 generation이 변하지
  않는다.
- 재현 회귀: 게시된 by-hash registry로 학습을 재실행하면 manifest의
  `registry_uri`·`registry_generation`·`registry_sha256`이 최초 실행과 같다.
- 기존 경로 무영향: 실험 좌표를 주지 않으면 prod/dev apply와 학습 경로가 변경 전과
  동일하다.
- feast 계열 테스트는 `uv sync --only-group feast` 환경에서 CI
  `pytest (feast group)` 목록으로 실행한다.
