FROM codiumai/pr-agent:latest

ENV PYTHONPATH=/app

ADD docs docs
ADD requirements.txt .
ADD pyproject.toml .
ADD MANIFEST.in .
ADD pr_agent pr_agent

RUN pip install --no-cache-dir .

WORKDIR /app

ENTRYPOINT ["python", "-m", "pr_agent.servers.gitlab_webhook"]
