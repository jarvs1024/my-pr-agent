# Review telemetry

Collects /improve, /review, /describe events for downstream dashboards.

## What gets collected

| Kind      | When                                   | Fields                                  |
|-----------|----------------------------------------|-----------------------------------------|
| `mr_activity`    | MR opened / updated / merged via webhook    | id, project, branches, author, state    |
| `review_run`     | Every `/describe` / `/review` / `/improve` invocation | run_id, command, status, duration, suggestion_count, rule_keys_cited |
| `suggestion`     | Every `code_suggestions` entry that goes to publish_code_suggestions | file, line, label, importance, rule_keys, score, note_id, state |
| `action_event`   | Reserved — emitted by hooks when GitLab resolves / dismisses a suggestion thread | action, suggestion_id, actor, at |

## Storage backends

Default: **SQLite** at `/tmp/pr-agent-telemetry.db`. Override with `REVIEW_TELEMETRY_DB_PATH`.

Opt-in JSONL sink via `REVIEW_TELEMETRY_BACKEND=jsonl` (path via `REVIEW_TELEMETRY_JSONL_PATH`).

Disable entirely with `REVIEW_TELEMETRY_BACKEND=off`.

All three settings can also live under `[telemetry]` in `configuration.toml` — env vars win.

## HTTP API (mounted on the existing pr-agent FastAPI app)

Base path: `/api/v1/telemetry`. Bearer-token auth from `REVIEW_TELEMETRY_HTTP_TOKEN`;
empty/unset means open access (local dev only).

| Method | Path                                            | Description                                  |
|--------|-------------------------------------------------|----------------------------------------------|
| GET    | `/health`                                       | `{status: ok, backend: sqlite}`              |
| GET    | `/metrics/overview`                             | MR / suggestion / run counts + adoption rate |
| GET    | `/metrics/rules`                                | Per-rule adoption and dismissal counts       |
| GET    | `/mrs?limit=50&project_id=34`                   | List MRs (latest activity first)             |
| GET    | `/mrs/{project_id}/{mr_id}`                     | Single MR detail                             |
| GET    | `/mrs/{project_id}/{mr_id}/suggestions`         | All suggestions posted on the MR             |
| GET    | `/mrs/{project_id}/{mr_id}/runs`                | All review runs against the MR               |
| GET    | `/mrs/{project_id}/{mr_id}/timeline`            | MR + suggestions + runs + actions in one call |
| GET    | `/mrs/{project_id}/{mr_id}/stats`               | Adoption-rate / distinct-rule list           |

## Wiring your FastAPI / dashboard

The data is plain JSON. Recommended calls from the front-end:

```bash
# Top-of-page headline numbers
curl http://pr-agent:5050/api/v1/telemetry/metrics/overview

# Per-rule chart (which AGENTS.md rules the team adopts vs dismisses)
curl http://pr-agent:5050/api/v1/telemetry/metrics/rules

# Drill-down for one MR
curl http://pr-agent:5050/api/v1/telemetry/mrs/34/40/timeline
```

Set `REVIEW_TELEMETRY_HTTP_TOKEN` on the pr-agent container to require:

```bash
curl -H "Authorization: Bearer $TOKEN" http://pr-agent:5050/api/v1/telemetry/...
```

## Applying it in production

The store uses `INSERT OR REPLACE` on the suggestion PK (so a re-run of `/improve`
overwrites the prior state). For multi-instance pr-agent deployments, point all
instances at a shared `REVIEW_TELEMETRY_DB_PATH` on a writable volume (SQLite
handles single-writer concurrency via file locks). For real scale, switch
`REVIEW_TELEMETRY_BACKEND=jsonl` and tail the file into your warehouse.
