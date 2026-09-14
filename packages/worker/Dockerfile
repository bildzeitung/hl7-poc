# Build context is the repo root (needs root pyproject.toml, uv.lock, and
# packages/core): docker build -f packages/worker/Dockerfile -t hl7poc-worker .

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Third-party deps as their own cached layer, before any source is copied.
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/pyproject.toml
COPY packages/listener/pyproject.toml packages/listener/pyproject.toml
COPY packages/worker/pyproject.toml packages/worker/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-workspace --package hl7poc-worker

COPY packages/core packages/core
COPY packages/worker packages/worker
# --no-editable is load-bearing: uv installs workspace members editable by
# default, and an editable install's .pth points at /app/packages/... which
# does not exist in the runtime stage below.
RUN uv sync --frozen --no-dev --no-editable --package hl7poc-worker

FROM python:3.14-slim-bookworm AS runtime

RUN useradd --create-home --uid 10001 hl7poc
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH

EXPOSE 8081

USER hl7poc
ENTRYPOINT ["hl7worker"]
