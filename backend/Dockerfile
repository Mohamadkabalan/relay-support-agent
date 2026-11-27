# syntax=docker/dockerfile:1
# Base images are pinned by digest; Dependabot proposes updates (tag and digest together).
# One image for the API, the MCP server and the `relay` CLI.
# Stages: builder (locked prod deps, embedding model) -> runtime (default target); dev adds dev
# deps and an editable install so compose.dev.yaml can bind-mount src/ and hot-reload.

ARG PYTHON_IMAGE=python:3.14.10-slim-trixie@sha256:22eafb370920ad8b429e0aec3fff433e0669968519f5d60469d83729c138b28b
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.9@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS base
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

FROM base AS builder
# Dependencies first, so source edits don't invalidate this layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
# The help-center embedding model (BAAI/bge-small-en-v1.5, about 65 MB) is baked in, so the
# containers never download anything at runtime. This layer only rebuilds when uv.lock changes.
# ONNX Runtime 1.32 sends usage telemetry unless ORT_DISABLE_TELEMETRY is set; we never want that.
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
ENV ORT_DISABLE_TELEMETRY=1
RUN /app/.venv/bin/python -c "from fastembed import TextEmbedding; \
TextEmbedding('${EMBEDDING_MODEL}', cache_dir='/opt/models')" \
    && chmod -R a+rX /opt/models
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM base AS dev
RUN groupadd --system --gid 10001 relay \
    && useradd --system --uid 10001 --gid relay --no-create-home --shell /usr/sbin/nologin relay
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked
COPY --from=builder /opt/models /opt/models
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/opt/models \
    HF_HUB_OFFLINE=1 \
    ORT_DISABLE_TELEMETRY=1
USER relay:relay
CMD ["relay", "api", "serve", "--host", "0.0.0.0", "--port", "8000", "--reload"]

FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 relay \
    && useradd --system --uid 10001 --gid relay --no-create-home --shell /usr/sbin/nologin relay
# Owned by root, so the unprivileged runtime user can't modify installed code.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /opt/models /opt/models
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/opt/models \
    HF_HUB_OFFLINE=1 \
    ORT_DISABLE_TELEMETRY=1
WORKDIR /app
USER relay:relay
EXPOSE 8000 8001
CMD ["relay", "api", "serve", "--host", "0.0.0.0", "--port", "8000"]
