FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# lightgbm이 런타임에 libgomp(OpenMP)를 dlopen한다. python:3.12-slim에는
# 기본으로 포함되어 있지 않다.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN adduser --disabled-password --gecos "" appuser

COPY --from=builder /app/.venv /app/.venv

# 코드는 이미지에 포함하지 않는다. ENTRYPOINT 부트스트랩이 파드 시작 시
# GCS 코드 아카이브(#174 파이프라인)를 /app에 풀고 커맨드를 실행한다.
# revision 라벨·AUTORESEARCH_REVISION은 이미지 빌드 시점 커밋을 뜻하며,
# 실행 코드 버전은 부트스트랩 로그([gcs-bootstrap] code: ...)가 담당한다.
COPY scripts/gcs_code_bootstrap.sh /usr/local/bin/gcs_code_bootstrap.sh
# 이 스테이지가 /app에 COPY하는 것은 .venv 하나뿐이라(pyproject.toml·uv.lock은
# builder의 bind mount로만 쓰인다) 아카이브가 덮어쓸 root 소유 파일이 없다.
# 새 엔트리를 만들 디렉토리 쓰기 권한만 있으면 되므로 -R을 쓰지 않는다.
RUN chown appuser:appuser /app

# 커밋별 메타데이터는 설치·복사 뒤에 기록해 의존성 레이어 캐시를 보존한다.
ARG VCS_REF=unknown
ENV AUTORESEARCH_REVISION=${VCS_REF}
LABEL org.opencontainers.image.source="https://github.com/SKYAHO/Autoresearch" \
      org.opencontainers.image.revision="${VCS_REF}" \
      io.autoresearch.batch-contract.version="batch-contract-v1"

USER appuser

ENTRYPOINT ["/usr/local/bin/gcs_code_bootstrap.sh"]

CMD ["python", "-c", "import autoresearch; print('autoresearch image ready')"]
