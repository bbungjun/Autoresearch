# 로컬 kind executor 재현

GKE 없이 노트북·데스크톱에서 실험 executor Pod를 운영과 같은 manifest로 실행합니다.
목적은 **executor 컨테이너 재구성(8개 → 4개, A안)의 전후 비교**입니다.

> **상태: 실행 검증 전.** 이 스크립트는 Docker가 없는 환경에서 코드와 대조해 작성했습니다.
> 첫 실행에서 막히는 곳이 나오면 고치면서 진행하고, 고친 내용은 이 문서에 반영합니다.

## 왜 하는가

현재 executor Pod는 컨테이너 8개(init 7 + main 1)를 순서대로 실행합니다. 검토 결과 두 가지
문제가 있습니다.

1. **경계 누락**: 마지막 `candidate-finalizer`에서 에이전트가 쓴 학습 코드와 Codex #2가
   push 토큰·API 토큰과 같은 컨테이너에서 실행됩니다.
2. **과분할**: `codex-worker`와 `candidate-verifier`는 권한이 같은데 나뉘어 있어 pytest가
   두 번 실행됩니다(중복 약 8분).

A안은 컨테이너를 **권한이 바뀌는 곳에서만** 나눕니다.

| # | 컨테이너 | 하는 일 | 권한 |
|---|---|---|---|
| 1 | setup | branch 토큰 발급 + exp 브랜치 생성 | App 키, branch 토큰 |
| 2 | worker | 공개 저장소 clone(토큰 없이) → baseline 학습 → Codex #1 → 검증 → candidate 학습 → 채점 → Codex #2 | **없음** |
| 3 | push-minter | push 토큰 발급 (1시간 만료 때문에 늦게) | App 키 |
| 4 | finalizer | commit → push → 게시 → API 보고 | push·API 토큰 |

저장소가 공개라서 clone에 토큰이 필요 없고, 이 덕분에 기존 8→4 계획
(`docs/plans/2026-08-09-agent-authored-experiment-report.md` Stage 2)의 미결 항목
(clone 토큰이 worker에 들어가는 문제)이 풀립니다. **A안은 아직 채택이 아니라 검증 대상입니다.**

## 단계

| 단계 | 내용 | 필요한 것 |
|---|---|---|
| **1단계 (이 문서)** | 학습을 끄고(`ORCH_TRAINING_DATASET_URI` 비움) As-Is를 실행해 기준값을 남긴다. 이후 A안 브랜치로 같은 절차를 반복해 비교한다 | Docker, GitHub App, Codex 로그인 |
| 2단계 | 학습을 켜고 `metrics.json`이 전후로 일치하는지 확인한다 | 학습 스냅샷이 있는 GCS 버킷 (또는 GCS 에뮬레이터) |

### 이 재현이 확인하는 것과 못 하는 것

- 확인함: Job manifest 전체(컨테이너 순서, 볼륨, 마운트, 보안 설정), Pod Security `restricted`,
  노드 풀 nodeSelector·toleration, GitHub App 토큰 흐름, Codex 실행, 검증, push, API 보고,
  컨테이너별 소요 시간.
- 못 함: 인프라 저장소의 admission 정책(Terraform), egress 제한(kind 기본 CNI는
  NetworkPolicy를 강제하지 않음), GKE 노드 사양과 같은 성능 조건.

## 사전 준비 (한 번만)

1. **Docker**: Windows라면 Docker Desktop → Settings → Resources → WSL integration에서
   사용하는 배포판을 켭니다. `docker version`이 서버 버전까지 출력되면 됩니다.
2. **kind v0.26.0, kubectl**
   ```bash
   curl -Lo ~/.local/bin/kind https://kind.sigs.k8s.io/dl/v0.26.0/kind-linux-amd64
   chmod +x ~/.local/bin/kind
   kubectl version --client
   ```
3. **GitHub App**: `GITHUB_REPOSITORY`에 설치된 App이 필요합니다.
   권한은 Contents read/write, Issues read, Metadata read입니다.
   App ID, Installation ID, private key(.pem) 경로를 `local.env`에 적습니다.
   키 파일은 저장소 밖에 두고, 저장소에 커밋하지 않습니다.
4. **Codex 로그인**: `codex login`. executor는 모델을 `gpt-5.6-luna`로 고정하므로
   그 계정에서 이 모델을 쓸 수 있어야 합니다. 실행하면 Codex 사용량이 소모됩니다.
5. **테스트 이슈**: 저장소에서 *Auto Research* 이슈 폼(`.github/ISSUE_TEMPLATE/auto_research.yml`)으로
   작은 가설 이슈를 하나 만듭니다. 공개 저장소이므로 누구나 볼 수 있습니다.
6. **설정 파일**
   ```bash
   cd deployment/local_kind
   cp local.env.example local.env   # 값 채우기
   ```

## 1단계 실행

```bash
cd deployment/local_kind
git switch main                  # As-Is 기준
./up.sh                          # 레지스트리 + kind 클러스터 + executor 노드 라벨·taint
./build.sh                       # executor·api·launcher 이미지 빌드, digest 기록
./deploy.sh                      # Secret, Postgres, API, DB migration
./seed-experiment.sh <이슈번호> "as-is baseline"
./launch.sh                      # launcher tick 1회 → executor Job 생성
kubectl --context kind-autoresearch-local -n experiments get pods -w   # 진행 보기
./timeline.sh                    # 끝나면 컨테이너별 시간·로그 저장
```

결과는 `.state/runs/<pod>/`에 저장됩니다(`timeline.tsv`, 컨테이너별 로그, `pod.yaml`).
이 폴더는 git에 올라가지 않습니다.

### 같은 이슈로 다시 실행할 때

executor는 `exp/<이슈번호>` 브랜치가 이미 있으면 Codex를 건너뛰고 기존 candidate를
채택합니다. 비교 실행이 다른 경로를 타지 않도록 `seed-experiment.sh`는 브랜치가 있으면
멈춥니다. 다시 실행하려면 먼저 브랜치를 지웁니다.

```bash
git push origin --delete exp/<이슈번호>
```

### A안과 비교할 때

A안 구현 브랜치로 바꾼 뒤 `./build.sh` → `./deploy.sh` → 브랜치 삭제 →
`./seed-experiment.sh` → `./launch.sh` → `./timeline.sh` 순서로 반복합니다.

## 기록할 것

| 항목 | As-Is | A안 | 어디서 읽나 |
|---|---|---|---|
| 컨테이너 수 | 8 |  | `timeline.tsv` 행 수 |
| Job 전체 시간 (초) |  |  | 첫 컨테이너 시작 ~ 마지막 종료 |
| pytest 실행 횟수 | 2 |  | codex-worker·verifier 로그 |
| 에이전트가 실행되는 컨테이너에 마운트된 토큰 | finalizer에 push·API |  | `pod.yaml`의 volumeMounts |
| 결과 | 완주 / 실패 단계 |  | `timeline.tsv`의 exit·reason |

완주율은 실행 몇 건으로는 말할 수 없으므로 이 표에 넣지 않습니다.

## 막혔을 때

| 증상 | 확인할 것 |
|---|---|
| executor Pod가 `Pending` | `kubectl describe pod`. executor 노드 라벨·taint(`up.sh`)와 리소스 요청(컨테이너당 1 CPU/2Gi) |
| `ImagePullBackOff` | `up.sh`의 레지스트리 연결 단계, `.state/images.env`의 digest가 `localhost:5001/...`인지 |
| Pod 생성이 거부됨 | `restricted` 위반. `kubectl get events -n experiments` |
| token-minter 실패 | App ID, Installation ID, 키 파일, App 권한 |
| finalizer의 API 보고 실패 | `kubectl -n orch-system logs deploy/experiment-api` |

## 정리

```bash
./down.sh    # 클러스터와 레지스트리 삭제. .state/runs 기록은 남김
```

## 이 문서를 이어받는 에이전트에게

이 작업의 결정 맥락은 위 "왜 하는가"가 전부입니다. 다음 순서로 진행하세요.

1. 1단계 As-Is를 실행하고 "기록할 것" 표를 채웁니다. 막힌 곳은 고치고 이 문서에 반영합니다.
2. A안 구현은 구조 변경이므로 spec(`docs/specs/2026-08-09-agent-authored-experiment-report.md`
   결정 3의 미결 블록)을 먼저 갱신하고, 저장소 소유자의 결정을 받은 뒤 진행합니다.
3. 채점 경로 고정(에이전트가 `autoresearch/model_evaluation/`을 수정하지 못하게 하는 것)은
   동작 변경이라 이 구조 변경과 별도 작업으로 다룹니다.
