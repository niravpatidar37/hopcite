# syntax=docker/dockerfile:1
# Multi-stage build: dependencies resolved from uv.lock in a builder, copied into a slim non-root runtime.
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.21@sha256:15f68a476b768083505fe1dbfcc998344d0135f0ca1b8465c4760b323904f05a /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
# Pick up Debian security fixes newer than the pinned base image (trivy gate fails on fixable HIGH/CRITICAL).
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY data/manifest.json /app/data/manifest.json
RUN mkdir -p /app/data/cache /app/logs && chown -R app:app /app/data /app/logs
ENV PATH=/app/.venv/bin:$PATH KGRAG_ROOT=/app KGRAG_HOST=0.0.0.0 PYTHONUNBUFFERED=1 \
    OLLAMA_URL=http://host.docker.internal:11434
USER app
EXPOSE 8101
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8101/health', timeout=4)" || exit 1
CMD ["kgrag-api"]
