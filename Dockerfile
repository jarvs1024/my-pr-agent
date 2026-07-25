FROM python:3.12-slim-bookworm

ENV PYTHONPATH=/app

# System deps for gitPython + pip source builds
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pinned Python deps from requirements.txt
ADD requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Local package source. No `pip install .` — PYTHONPATH=/app already exposes
# /app/pr_agent, so the source tree is the runtime. (Earlier `pip install .`
# was hanging on wheel-build because pyproject.toml declares
# `dynamic = ["dependencies"]` which forces setuptools to re-read
# requirements.txt at build time.)
ADD docs docs
ADD pyproject.toml .
ADD MANIFEST.in .
ADD pr_agent pr_agent

# Persistent state directory for SQLite telemetry DB, Suggestion JSON store, and
# the AGENTS.md rule cache. Mount a host directory to this path in production
# so data survives container restarts/recreates.
RUN mkdir -p /var/lib/pr-agent && chmod 0777 /var/lib/pr-agent
ENV PR_AGENT_DATA_DIR=/var/lib/pr-agent

WORKDIR /app

ENTRYPOINT ["python", "-m", "pr_agent.servers.gitlab_webhook"]
