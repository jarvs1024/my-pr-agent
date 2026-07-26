# Weekly Report & DingTalk Notification

This feature adds a second container, `pr-agent-reporter`, that runs on a
weekly cron schedule and posts a Chinese project-code-review report to a
DingTalk custom-robot webhook. The structured artifact it produces is
also available via the existing telemetry API for the TestMate frontend
to consume.

The reporter is a **separate process** (separate container, separate
Python interpreter) from the pr-agent webhook handler. This guarantees
that heavy operations in the reporter (shallow clone, LLM review, DingTalk
delivery) can never interfere with webhook handling.

## Quick start

### 1. Enable the feature

In `.pr_agent.toml`:

```toml
[weekly_report]
enabled = true
target_project_id = 34               # your GitLab project id (root/auto-review-test in dev)
target_branch = "main"               # leave empty to use the project's default_branch
cron = "0 9 * * 1"                   # every Monday 09:00 (Asia/Shanghai by default)
collectors = ["telemetry", "master_merges", "repo_scan"]
notifier = "dingtalk"
```

### 2. Provision env vars on the reporter container

```bash
GITLAB_URL=https://gitlab.com
GITLAB_PERSONAL_ACCESS_TOKEN=<token with read_api on the target project>
DINGTALK_WEEKLY_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=<token>
DINGTALK_WEEKLY_SECRET=<加签 secret, optional>
PR_AGENT_WEEKLY_TARGET_PROJECT_ID=34
```

### 3. Run the reporter

Production (via docker-compose — see `docker/docker-compose.weekly-report.example.yml`):

```bash
docker compose -f docker-compose.yml up -d pr-agent-reporter
```

One-off debug:

```bash
PYTHONPATH=. PR_AGENT_WEEKLY_ENABLED=true PR_AGENT_WEEKLY_TARGET_PROJECT_ID=34 \
  PR_AGENT_WEEKLY_DINGTALK_DRY_RUN=true PR_AGENT_WEEKLY_LLM_DRY_RUN=true \
  python -m pr_agent.reporting.cli --run-now
```

`--run-now` produces an artifact and emits the rendered markdown to the
log instead of POSTing (because `DINGTALK_DRY_RUN=true`). The artifact
is at `${PR_AGENT_DATA_DIR}/weekly_reports/<project_id>/<YYYY-WW>.json`.

## What goes into the report

The report has three sections. Each is produced by an independent
collector; one collector failing does not stop the others.

| Section heading | Source | Mockable |
|---|---|---|
| 一、本周检视概况 | `pr_agent.telemetry.store` aggregates | n/a (real telemetry) |
| 二、本周 *\<branch\>* 变更汇总 | GitLab API (`project.mergerequests.list`) | n/a (real GitLab) |
| 三、本周代码质量扫描 | shallow clone + `litellm.completion` | `PR_AGENT_WEEKLY_LLM_DRY_RUN=true` |

The branch placeholder in section 二's title resolves at render time
from the section's `target_branch` (e.g. "本周 main 变更汇总" for a
project merged into `main`).

The shallow clone lives at `${PR_AGENT_DATA_DIR}/repo_scan_cache/<project_id>/`
and is refreshed each run (`git fetch --depth=200 --prune`). Mount
`PR_AGENT_DATA_DIR` on a host volume to keep the cache across container
restarts.

## Reading the report from TestMate

Two read-only endpoints are added to the existing telemetry API:

```
GET /api/v1/telemetry/weekly_reports/latest?project_id=34
GET /api/v1/telemetry/weekly_reports/list?project_id=34&limit=12
```

Both reuse the existing `REVIEW_TELEMETRY_HTTP_TOKEN` auth (set on the
webhook container) — no new auth scheme is added. The list endpoint
returns a lightweight metadata view (no `data` / `markdown` blobs); use
`/latest` (or pass a known `week_label`) to fetch a full artifact.

## Failure modes

The reporter never crashes the pr-agent webhook (different process).
Within the reporter, each collector is wrapped: a failure in collector X
records `status="failed"` in the artifact and other collectors continue.
The DingTalk notifier retries up to `dingtalk_retry_attempts` (default
3) with exponential backoff. If all retries fail, the rendered markdown
+ delivery error is dumped to
`${PR_AGENT_DATA_DIR}/reporting_runs/<ts>.failed.json` for manual
re-send.

Each run also writes a JSON summary to
`${PR_AGENT_DATA_DIR}/reporting_runs/<ts>.<status>.json`. These are
useful for the host log shipper (filebeat / vector / loki) to track
health.

## Configuration reference

All settings can be overridden by environment variables prefixed
`PR_AGENT_WEEKLY_*` or `DINGTALK_WEEKLY_*`.

| Setting | Env | Default | Notes |
|---|---|---|---|
| `enabled` | `PR_AGENT_WEEKLY_ENABLED` | `false` | total kill switch |
| `target_project_id` | `PR_AGENT_WEEKLY_TARGET_PROJECT_ID` | `0` | GitLab project id |
| `target_branch` | `PR_AGENT_WEEKLY_TARGET_BRANCH` | `""` (→ `default_branch`) | override only if you want a non-default branch |
| `cron` | `PR_AGENT_WEEKLY_CRON` | `0 9 * * 1` | standard 5-field cron |
| `timezone` | `PR_AGENT_WEEKLY_TIMEZONE` | `Asia/Shanghai` | APScheduler / display |
| `collectors` | `PR_AGENT_WEEKLY_COLLECTORS` | `[telemetry, master_merges, repo_scan]` | comma-separated in env |
| `notifier` | `PR_AGENT_WEEKLY_NOTIFIER` | `dingtalk` | only `dingtalk` today |
| `llm_model` | `PR_AGENT_WEEKLY_LLM_MODEL` | `""` (→ `config.model`) | override only for the weekly report |
| `llm_dry_run` | `PR_AGENT_WEEKLY_LLM_DRY_RUN` | `false` | returns a stub instead of calling LLM |
| `dingtalk_dry_run` | `PR_AGENT_WEEKLY_DINGTALK_DRY_RUN` | `false` | logs payload instead of POSTing |
| `dingtalk_retry_attempts` | `PR_AGENT_WEEKLY_DINGTALK_RETRY` | `3` | per chunk |
| `diff_token_limit` | `PR_AGENT_WEEKLY_DIFF_TOKEN_LIMIT` | `50000` | bytes; larger diffs are truncated |
| `markdown_chunk_limit` | `PR_AGENT_WEEKLY_MARKDOWN_CHUNK_LIMIT` | `18000` | bytes; bodies split at `## ` headings |
| `report_title` | `PR_AGENT_WEEKLY_REPORT_TITLE` | `项目代码检视周报` | top-level title in the report header |
| `report_emoji` | `PR_AGENT_WEEKLY_REPORT_EMOJI` | `📊` | emoji prefix for the title (e.g. set to `🛠️`) |

The reporter container also needs OpenAI-compatible LLM credentials so
the `master_merges` / `repo_scan` collectors can call the model:

| Env | Purpose |
|---|---|
| `OPENAI_API_KEY` | forwarded straight to `litellm.completion` |
| `OPENAI_API_BASE` | e.g. `https://api.minimaxi.com/v1` (any OpenAI-compatible endpoint) |

If those are empty the report falls back to deterministic stubs
(`(mock)` markers in the rendered markdown + warning in the artifact)
rather than failing the whole run.

## Local development

```bash
# Show resolved config (handy to confirm env wins)
PYTHONPATH=. python -m pr_agent.reporting.cli --show-config

# Run a one-off cycle against the dev GitLab
PYTHONPATH=. PR_AGENT_WEEKLY_DINGTALK_DRY_RUN=true PR_AGENT_WEEKLY_LLM_DRY_RUN=true \
  python -m pr_agent.reporting.cli --run-now --since-days 30

# Print the most recent artifact for a project
PYTHONPATH=. PR_AGENT_DATA_DIR=/tmp/dev-data \
  python -m pr_agent.reporting.cli --print-latest 34

# Run unit tests
PYTHONPATH=. pytest tests/unittest/reporting -v
```

## Operational notes

* The reporter container has `mem_limit: 2g` and `cpus: 1.0` by default
  in the example compose. Tune to your project's diff size.
* When upgrading, the reporter pulls the same image as the webhook
  container, so a single `docker compose pull && up -d` keeps both on
  the same code revision.
* The reporter does not listen on any port; health-check it by
  inspecting `${PR_AGENT_DATA_DIR}/reporting_runs/` or by curling the
  webhook container's `/api/v1/telemetry/weekly_reports/list` endpoint
  and checking the `generated_at` timestamps.
