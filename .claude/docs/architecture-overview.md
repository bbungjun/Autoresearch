# Architecture Overview

> Last Updated: 2026-07-13

4개 도메인 아키텍처의 조감도, 핵심 설계 결정, 도메인 간 상호작용을
정리한 문서입니다. 현재 구현된 부분과 계획(별도 브랜치 진행 중)을
구분해서 읽어야 합니다.

## When To Use This Doc

- 4개 도메인이 어떻게 상호작용하는지 파악해야 할 때
- 도메인을 가로지르는 변경을 하며 제약을 이해해야 할 때
- 특정 설계 결정(예: ODFV vs FeatureView)의 이유를 알아야 할 때
- 프로젝트에 온보딩하며 전체 그림이 필요할 때

## Four Domains

```text
Autoresearch
  ├─ YouTube 수집·backfill·action log·quality 공개 CLI
  ├─ Feature Store·학습·평가·추론 애플리케이션
  └─ Dockerfile.app → immutable application image
                         ↑ 실행
Autoresearch-airflow
  └─ DAG·Sensor·KPO·schedule·retry·timeout·Airflow Helm
                         ↑ 기반
Autoresearch-infra
  └─ GKE·GAR·GCS·BigQuery·IAM/WIF·Secret·Kubernetes policy
```

## Current Data Flow (구현됨)

```
YouTube Data API
    → client.py (복원력 래퍼: 재시도/Key롤링/IP밴시그니처/Cloud Run 프록시)
    → fetch.py (수집 로직)
    → transform.py (pydantic 스키마 검증)
    → load.py (GCS 데이터 레이크, parquet)

공개 batch 실행:
    autoresearch.jobs.youtube_trending
    autoresearch.jobs.youtube_backfill
    autoresearch.jobs.action_log
    autoresearch.jobs.action_log_quality
    → Dockerfile.app application image

Airflow (외부 Autoresearch-airflow):
    DAG/KPO → immutable image digest → 위 공개 module command

가상 유저 (실험):
    persona 원천 → autoresearch/virtual_user_generation/pipeline.py
    → GLM API (glm_generator.py) → 가상 유저 데이터셋
```

## Domain 1: Model Training

**책임:** CTR(클릭률) 모델 정의, 학습 오케스트레이션, 평가 지표.

**상태:** `autoresearch/`의 단계 패키지에 LightGBM 학습·평가 파이프라인과 피처
빌더가 구현되어 있습니다.

**주요 파일:**
- `autoresearch/model_training/lgbm_model.py` — LightGBM 모델 클래스
- `autoresearch/model_training/train.py`, `build_training_dataset.py`
- `autoresearch/model_evaluation/evaluate.py`
- `autoresearch/model_training/config.yaml` — 하이퍼파라미터·경로의 단일 출처
- `docs/guides/ctr-model-specification.md` — CTR 모델링 스펙 (전체 상세)

**모델링 과업:**
- **목표:** user_id가 video_id를 봤을 때의 클릭 확률 예측
- **출력:** 영상별 클릭 확률 (추천 리스트가 아님). 후처리로 확률 순
  정렬, Top-N 추출, 탐색 아이템 혼합
- **핵심 규칙** (상세는 `docs/guides/ctr-model-specification.md`):
  - 스칼라 피처만 직접 입력. 벡터/리스트는 유사도 계산에만 사용
  - 유저 피처는 라벨 시점 **이전** 이벤트로만 생성 (누수 금지)
  - Interaction 피처는 학습과 서빙에서 동일하게 계산 (skew 금지)
  - Cold-start: 이력 없는 값은 "unknown" 처리 (대치 금지)

## Domain 2: Feast Features

**책임:** 피처 정의, 피처 스토어 구축, 피처 엔지니어링 변환.

**상태:** 운영 중 — `feature_repo/`에 실데이터 스키마 기반 Entity·FeatureView
정의 (BigQuery source table 연동). Feast 0.64, BigQuery offline store +
Redis online store, `feast_materialize` 공개 batch CLI 제공. registry apply는
GitHub Actions `feast-apply` 워크플로우(feast 공식 CLI)가 소유한다(#331).

### Feast 핵심 설계 결정 (확정)

**1. ODFV(On-Demand Feature View) 필수**
- 실시간 변환(정규화, 버킷팅 등)은 ODFV로 구현합니다.
- 변환 목적으로 일반 `FeatureView`를 쓰지 않습니다. 모든 변형을
  사전 물질화해야 하는 안티패턴입니다.

**2. TTL ≠ 윈도우 집계**
- TTL은 피처 신선도 요구(예: 1시간마다 갱신), 윈도우는 계산
  범위(예: 최근 7일 합)입니다. 서로 독립적으로 설정합니다.

```yaml
ttl: 3600            # 신선도: 1시간
window_size: 604800  # 계산 범위: 7일
```

**3. Cold-start는 명시적 null 또는 "unknown"**
- 이력이 없는 엔티티는 0이나 평균으로 대치하지 않습니다. 명시적
  결측을 반환해 모델이 학습 시 본 희소성을 그대로 보게 합니다.

**4. Training-Serving 일관성**
- Interaction 피처는 학습 데이터셋 생성 로직과 Feast 변환 로직이
  동일해야 합니다. 리뷰에서 양쪽 로직 일치를 확인합니다.

## Domain 3: Airflow Orchestration (bbungjun)

**책임:** DAG 정의, 스케줄링, 파이프라인 오케스트레이션.

**상태:** `SKYAHO/Autoresearch-airflow`에 구현되어 있습니다. 이 저장소에는
Airflow runtime source나 설정을 두지 않습니다.

**이 저장소가 제공하는 경계:**
- `Dockerfile.app` — canonical application image
- `autoresearch/jobs/` — versioned public batch command
- `docs/specs/2026-07-13-public-batch-execution-contract.md` — CLI·I/O·exit 계약

**설계 제약:**
- Airflow는 application 내부 Python API를 import하지 않고 공개 CLI만
  실행합니다.
- schedule·retry·timeout·Pool·KPO 정책은 application에 넣지 않습니다.
- application image는 `Autoresearch` checkout 하나로 발행하고 Airflow는
  immutable digest를 선택합니다.

## Domain 4: GCP Infrastructure

**책임:** 클라우드 배포, 환경 구성, 시크릿 관리.

**상태:** GCP·Kubernetes 리소스는 `SKYAHO/Autoresearch-infra`가
소유합니다. 이 저장소의 release workflow는 infra가 제공한 GAR·WIF reference를
소비해 application image만 발행합니다.

## Cross-Domain Interactions

- **Airflow → application:** KPO가 immutable application image에서
  `autoresearch.jobs.*` 공개 CLI를 실행합니다. 내부 모듈을 직접 import하지
  않습니다.
- **Model Training ← Feast (계획):** 학습 스크립트가 Feast 클라이언트로
  피처를 조회합니다. 직접 SQL 조회는 금지합니다.
- **모든 workload ← infra:** bucket, cluster, service account와 Secret
  reference를 소비합니다. 자격 증명 원문 하드코딩을 금지합니다.

## Key Architecture Rules

1. **도메인 간 강결합 금지.** 다른 도메인의 내부 구현에 의존하지
   않습니다.
2. **설정은 단일 출처.** 파이프라인 파라미터는 설정 파일과 환경
   변수로 관리하고 코드에 하드코딩하지 않습니다.
3. **데이터 계약은 pydantic 스키마.** 모듈 간 데이터는 `schema.py`
   모델로 검증합니다.
4. **시크릿은 환경 변수.** 자격 증명, API 키, 버킷 이름을 코드에
   넣지 않습니다.
5. **데이터 스펙의 단일 출처.** Event Log는
   `docs/guides/agent-simulator-spec.md`, 피처/라벨 정의는
   `docs/guides/ctr-model-specification.md`를 따릅니다.

## Verification Checklist

- [ ] 새 코드가 올바른 도메인에 있다 (`agent-project-reference.md` 참조)
- [ ] 자격 증명·경로 하드코딩이 없다
- [ ] Feast 변환은 ODFV를 사용한다 (계획 작업 시)
- [ ] TTL과 윈도우 집계를 독립적으로 설정했다
- [ ] Cold-start 처리가 명시적이다 (null/"unknown", 대치 금지)
- [ ] 학습과 서빙의 피처 로직이 동일하다
- [ ] 스펙 문서가 데이터 정의의 단일 출처로 유지된다
