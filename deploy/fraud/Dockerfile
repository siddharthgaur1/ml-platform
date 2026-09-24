# Single shared image for producer/scorer(api)/monitor — they differ only by
# the command docker-compose runs, so one image keeps builds and deps in sync.
#
# Multi-stage: gcc and libpq-dev are needed to build psycopg2, but shipping a
# compiler in the runtime image is attack surface for no benefit. The builder
# produces a venv; the runtime stage copies that and nothing else.
#
# Build context is the repo root (see docker-compose.yml's `context: ../..`):
# the 2026-08 merge moved this from flat common/producer/scorer/api/monitor/
# training/ directories into src/ml_platform/fraud/, so the image now installs
# the whole ml-platform package (fraud extra) rather than copying loose dirs.
#
# TODO(pin): `-slim-bookworm` pins the Debian release but still floats the
# patch version. Digest-pinning is stronger — replace with
# `python:3.11-slim-bookworm@sha256:<digest>` from `docker inspect` after a
# `docker pull`, and refresh it deliberately.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libpq-dev \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[fraud]"


FROM ${PYTHON_IMAGE} AS runtime

# libpq5 is the runtime half of libpq-dev: psycopg2 needs the shared library,
# not the headers or the compiler it was built against. curl is for the
# healthcheck below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
USER appuser

EXPOSE 8000

# Compose overrides this for producer/monitor. Having a real default means
# `docker run` on the image alone does something sensible.
CMD ["uvicorn", "ml_platform.fraud.api.main:app", "--host", "0.0.0.0", "--port", "8000"]


# docker-compose mounts ./models into the container, but a Kubernetes pod has no
# host directory to mount, so the Helm path builds this target instead and ships
# the bundle in the image.
#
# Baking a model into the application image couples two lifecycles that should be
# independent -- a retrain should not need an app rebuild. It is the right trade
# for a self-contained demo and the wrong one for production, where you set
# MLFLOW_TRACKING_URI and the registry becomes the source of truth (see
# common/registry.py); the baked file is then unused.
#
# ponytail: image-baked model, switch to the MLflow registry or an initContainer
# when the model needs to roll independently of the code.
FROM runtime AS runtime-with-model
COPY --chown=appuser deploy/fraud/models/ /models/
