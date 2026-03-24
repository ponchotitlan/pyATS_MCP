# ── Stage 1: build wheel with uv ─────────────────────────────────────────────
# Pinned to linux/amd64: pyATS dependency 'unicon' ships x86_64-only wheels.
FROM --platform=linux/amd64 python:3.12-slim AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy dependency manifests first (layer cache)
COPY pyproject.toml .
COPY README.md .

# Copy source so the editable install works
COPY pyats_mcp/ pyats_mcp/

# Install into an isolated virtual environment inside the image
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
RUN uv sync --no-dev


# ── Stage 2: lean runtime image ───────────────────────────────────────────────
FROM --platform=linux/amd64 python:3.12-slim

WORKDIR /app

# Install network clients required by pyATS/unicon to connect to devices
RUN apt-get update && apt-get install -y --no-install-recommends \
      telnet \
      openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Copy the pre-built venv from the builder stage
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/pyats_mcp /app/pyats_mcp

# Make the venv the default Python
ENV PATH="/app/.venv/bin:$PATH"

# Path inside the container where you mount your testbed.yaml
ENV PYATS_TESTBED_PATH=/app/testbed.yaml

# Artifacts are written here; mount a volume to persist them
ENV PYATS_MCP_ARTIFACTS_DIR=/app/artifacts
RUN mkdir -p /app/artifacts

CMD ["pyats-mcp"]
