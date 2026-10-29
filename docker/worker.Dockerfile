# syntax=docker/dockerfile:1
# Turns a Debian- or Ubuntu-based image into an sbx template for a subagents-docker-sbx worker,
# so `WorkerSpec(template=...)` starts without installing anything.
#
# Build with the worker directory (langgraph.json, optional requirements.txt) as context:
#
#   docker build -f docker/worker.Dockerfile -t my-worker:1 path/to/worker
#   docker build -f docker/worker.Dockerfile --build-arg BASE_IMAGE=ubuntu:24.04 -t my-worker:1 path/to/worker
#
# Then hand it to sbx (local image store only, nothing is pushed):
#
#   docker save my-worker:1 -o my-worker.tar && sbx template load my-worker.tar
#
# What a worker needs is defined in src/subagents_docker_sbx/requirements.py; keep the venv
# path and WORKER_REQUIREMENTS below in sync with it (a unit test checks this).

ARG BASE_IMAGE=python:3.12-slim-bookworm

FROM ghcr.io/astral-sh/uv:0.13.4 AS uv

FROM ${BASE_IMAGE}

USER root

# The sbx base-image floor: sh and bash, curl, git and CA certificates.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

# sbx runs as `agent` with UID 1000 and home /home/agent. Reuse a UID-1000 user the base image
# already has (for example `ubuntu` or `node`) by renaming it.
RUN set -eu; \
    existing="$(getent passwd 1000 | cut -d: -f1 || true)"; \
    if [ -z "$existing" ]; then \
        useradd --create-home --uid 1000 --shell /bin/bash agent; \
    elif [ "$existing" != agent ]; then \
        group="$(id -gn "$existing")"; \
        usermod --login agent --home /home/agent --move-home --shell /bin/bash "$existing"; \
        if [ "$group" = "$existing" ]; then groupmod --new-name agent "$group"; fi; \
    fi

COPY --from=uv /uv /usr/local/bin/uv

USER agent
WORKDIR /home/agent

# The worker venv with WORKER_REQUIREMENTS. It uses the image's python3 if that is >= 3.11, and
# otherwise a uv-managed PYTHON_VERSION (pinned, because the newest Python often lacks wheels).
ARG PYTHON_VERSION=3.12
ARG WORKER_REQUIREMENTS="deepagents>=0.6.7 langgraph-cli[inmem]>=0.4.38"
RUN set -euf; \
    if python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then \
        python="$(command -v python3)"; \
    else \
        python="${PYTHON_VERSION}"; \
    fi; \
    uv venv --python "$python" /home/agent/venv; \
    uv pip install --no-cache --python /home/agent/venv/bin/python ${WORKER_REQUIREMENTS}

# The worker's own dependencies. The glob keeps the build working without a requirements.txt;
# langgraph.json is always there.
COPY --chown=agent:agent langgraph.json requirements.tx[t] /tmp/worker/
RUN if [ -f /tmp/worker/requirements.txt ]; then \
        uv pip install --no-cache --python /home/agent/venv/bin/python -r /tmp/worker/requirements.txt; \
    fi \
    && rm -rf /tmp/worker
