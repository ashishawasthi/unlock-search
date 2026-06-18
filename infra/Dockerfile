# One image for the CORE app. The HTTP surface is byte-identical across profiles
# (core/api/app.py:create_app); the active profile is chosen at runtime by UNLOCK_PROFILE.
#
# Base build (local profile, no cloud/onprem SDKs):
#   docker build -f infra/Dockerfile -t gcp-unlock:local .
#
# On-prem build (adds requirements-onprem.txt: psycopg, opensearch-py, boto3, presidio, ...):
#   docker build -f infra/Dockerfile --build-arg EXTRAS=onprem -t gcp-unlock:onprem .
#
# GCP build (adds requirements-gcp.txt: google-cloud-*, vertexai, google-adk, ...):
#   docker build -f infra/Dockerfile --build-arg EXTRAS=gcp -t gcp-unlock:gcp .
#
# Run:
#   docker run -p 8000:8000 -e UNLOCK_PROFILE=onprem ... gcp-unlock:onprem
FROM python:3.12-slim

# EXTRAS selects the per-target extra requirements file: "" (none / local), "onprem", or "gcp".
ARG EXTRAS=""

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UNLOCK_PROFILE=local

WORKDIR /app

# System deps: build toolchain for any wheels that need it (psycopg, etc.); curl for HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better layer caching. Copy all requirement files so the
# EXTRAS branch can find its file regardless of which target is being built.
COPY requirements.txt requirements-onprem.txt requirements-gcp.txt ./
RUN pip install -r requirements.txt \
    && if [ -n "$EXTRAS" ]; then pip install -r "requirements-$EXTRAS.txt"; fi

# Application code. Profiles and the static UI ship inside the image.
COPY core ./core
COPY adapters ./adapters
COPY infra ./infra
COPY profiles ./profiles
COPY ui ./ui

# Run as non-root.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/ || exit 1

# Factory entrypoint. Single worker by default; scale with replicas, not in-process workers,
# so the SQLite local profile stays correct and stateless replicas scale cleanly on K8s.
CMD ["uvicorn", "core.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
