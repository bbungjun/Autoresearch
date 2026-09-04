# Agent Python Reference

> Last Updated: 2026-07-22

Python 코드를 작성하거나 수정할 때 사용하는 문서입니다.

## Pre-Implementation Checklist

1. 새 함수와 변경된 함수에 반환 타입을 포함한 타입 힌트를 답니다.
2. 호출 계약이 자명하지 않은 공개 헬퍼와 모듈 경계 함수에 Google
   스타일 docstring을 답니다.
3. 새 모듈을 만들거나 기존 모듈의 기능을 추가·변경하면, 모듈 최상단
   docstring을 Module Responsibility 형식으로 작성·갱신합니다(아래 절 참조).
4. 설정 값은 환경 변수로 받고, 사용자에게 노출되는 값이면
   `.env.example`에 문서화합니다.
5. 모듈 간 데이터는 `schema.py`의 pydantic 모델로 검증합니다.
6. 테스트에 임시 디렉터리나 환경 변수 격리가 필요한지 확인합니다.
7. Python 3.11과 3.12 양쪽에서 동작해야 합니다 (CI 매트릭스).

## Module Responsibility

에이전트 산출물을 사람이 전체 파이프라인 기준으로 따라올 수 있도록, 모든
런타임 모듈은 최상단 docstring에 자기 책임을 선언합니다. (과거의 `__arch__`
사이드카·archmap CI 게이트는 2026-07-22에 제거되었습니다 — 선언은
docstring 하나로만 합니다.)

모듈 docstring에 반드시 담을 것:

1. **파이프라인 위치**: 전체 흐름(수집 → 웨어하우스 적재 → 피처 → 학습 →
   일일 추천 배치 → 노출 조립 → LLM 판정 → action log → 재학습) 중 이
   모듈이 담당하는 구간.
2. **제공 기능**: 이 모듈이 해주는 일을 모듈 단위로 1~3문장 서술.
   공개 함수 나열이 아니라 "무엇을 책임지는가"를 적습니다.
3. **비책임 경계**: 인접해 보이지만 이 모듈 소유가 아닌 것과 실제 소유
   위치(모듈 경로 또는 이슈 번호).

예시:

```python
"""user_recommendations 기반 모델 노출 조립 provider.

[파이프라인] 일일 추천 배치(user_recommendations 적재)와 action log 생성
사이 — champion 모델 순위를 일일 노출 70% 슬라이스로 조립하는 구간을
담당한다.

[기능] BQ 순위 파티션 1회 로드, 24개(모델 17·트렌딩 5·랜덤 2) 노출 조립과
정책 태그 생성, 기존 candidate_provider seam에 주입 가능한 팩토리 제공.

[비책임] LLM 판정·클릭 정규화(autoresearch/action_logs/pipeline.py),
일일 CLI 배선(autoresearch/jobs/action_log.py).
"""
```

기능을 추가·변경하는 PR은 해당 모듈 docstring 갱신을 **같은 커밋**에
포함합니다. 리뷰 시 새 공개 심볼이 생겼는데 docstring이 그대로면 변경이
불완전한 것으로 간주합니다.

## Typing

- 함수에는 반환 타입을 포함한 타입 힌트가 필수입니다.
- 이미 존재하는 구체적인 도메인 타입(pydantic 모델)을 우선합니다.
- 파일 경로는 가능하면 raw 문자열 대신 `Path`를 사용합니다.
- 외부 API가 강제하는 경우가 아니면 `Any`로 타입을 넓히지 않습니다.

## Data Validation

- 외부에서 들어오는 데이터(YouTube API 응답, Kaggle parquet, GLM
  응답)는 pydantic 모델로 검증한 뒤 사용합니다.
- pydantic v2 문법을 사용합니다 (`model_validate`, `model_dump`).
- 스키마 변경 시 대응하는 테스트를 함께 갱신합니다.

## Configuration

- 설정은 환경 변수로 주입합니다 (`YOUTUBE_API_KEYS` (복수, 쉼표 구분),
  `YOUTUBE_PROXY_URL`, `YOUTUBE_LAKE_BUCKET`, `YOUTUBE_BACKFILL_SOURCE` 등).
- 기본값은 로컬 개발에 안전한 값으로 둡니다.
- 새 환경 변수를 추가하면 `.env.example`을 갱신합니다.
- 공개 batch CLI에 필요한 설정은 명시적 CLI 인자 또는 환경 변수로
  검증합니다. Airflow Variable과 Secret reference의 매핑은
  `Autoresearch-airflow`가 소유합니다.

## Logging and Errors

- 시크릿, API 키, 토큰, 전체 환경 변수 덤프를 로그에 남기지 않습니다.
- 실패한 작업과 안전한 컨텍스트를 알 수 있는 명확한 에러 메시지를
  씁니다.
- 기존 예외 타입과 동작을 유지합니다. 상세는
  `agent-error-handling-reference.md`를 참조합니다.

## Tests

- 테스트는 `tests/test_<module>.py`에 배치합니다.
- 변경된 동작에 집중된 테스트를 우선합니다.
- 외부 API(YouTube, GCS, GLM)는 mock으로 격리합니다. 실제
  네트워크 호출을 테스트에 넣지 않습니다.
- 파일 산출물은 `tmp_path` 등 임시 디렉터리를 사용합니다.
- 실행: `uv run python -m pytest -n 4 --dist loadfile --durations=25` (CI와 동일)
