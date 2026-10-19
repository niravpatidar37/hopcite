# syntax=docker/dockerfile:1
# Multi-stage build: dependencies resolved from uv.lock in a builder, copied into a slim non-root runtime.
# One image serves the API (Cloud Run service) and the batch entrypoints (Cloud Run Jobs:
# `python -m kgrag.migrate up`, `python -m kgrag.jobs ingest`). Deploys reference it by digest.
FROM python:3.14.8-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83 AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.21@sha256:15f68a476b768083505fe1dbfcc998344d0135f0ca1b8465c4760b323904f05a /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
# Bake the embedding model into the image from a pinned Hugging Face revision, sha256-checked, so the
# service never downloads weights at startup or request time.
COPY scripts/fetch_embed_model.py ./scripts/fetch_embed_model.py
RUN /app/.venv/bin/python scripts/fetch_embed_model.py /app/models/bge-small-en-v1.5 \
    && rm -rf /app/models/bge-small-en-v1.5/.cache
# Prove the baked model loads with the network path disabled.
RUN HF_HUB_OFFLINE=1 KGRAG_LOAD_DOTENV=0 KGRAG_ROOT=/app KGRAG_EMBED_MODEL_PATH=/app/models/bge-small-en-v1.5 \
    /app/.venv/bin/python -c "from kgrag.embeddings import FastEmbedder; v = FastEmbedder().embed_query('TSMC foundry'); assert v.shape == (384,)"

FROM python:3.14.8-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83
# Pick up Debian security fixes newer than the pinned base image (trivy gate fails on fixable HIGH/CRITICAL).
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/models /app/models
# Ingestion job inputs: manifest + the committed extraction cache (raw filings are fetched by the job).
COPY data/manifest.json /app/data/manifest.json
COPY data/extracted /app/data/extracted
RUN mkdir -p /app/data/cache /app/data/raw /app/logs /app/results \
    && chown -R app:app /app/data /app/logs /app/results
ENV PATH=/app/.venv/bin:$PATH \
    KGRAG_ROOT=/app \
    KGRAG_HOST=0.0.0.0 \
    KGRAG_LOAD_DOTENV=0 \
    KGRAG_LOG_FORMAT=json \
    KGRAG_EMBED_MODEL_PATH=/app/models/bge-small-en-v1.5 \
    HF_HUB_OFFLINE=1 \
    LLM_SPEND_LEDGER=/tmp/kgrag-spend-ledger.jsonl \
    PYTHONUNBUFFERED=1 \
    OLLAMA_URL=http://host.docker.internal:11434
USER app
EXPOSE 8101
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % (os.environ.get('KGRAG_PORT') or os.environ.get('PORT') or '8101'), timeout=4)" || exit 1
CMD ["kgrag-api"]
