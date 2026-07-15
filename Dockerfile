FROM codiumai/pr-agent:latest

ENV PYTHONPATH=/app

ADD docs docs
ADD requirements.txt .
ADD pyproject.toml .
ADD MANIFEST.in .
ADD pr_agent pr_agent

RUN pip install --no-cache-dir .

# Persistent state directory for SQLite telemetry DB, Suggestion JSON store, and
# the AGENTS.md rule cache. Mount a host directory to this path in production
# so data survives container restarts/recreates.
RUN mkdir -p /var/lib/pr-agent && chmod 0777 /var/lib/pr-agent
ENV PR_AGENT_DATA_DIR=/var/lib/pr-agent

WORKDIR /app

ENTRYPOINT ["python", "-m", "pr_agent.servers.gitlab_webhook"]
