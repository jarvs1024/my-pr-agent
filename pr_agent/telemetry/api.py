"""FastAPI routes exposing review telemetry.

Mounted by ``pr_agent.servers.gitlab_webhook`` so external dashboards can
read adoption / dismissal / coverage / per-MR drill-down data.

All routes live under ``/api/v1/telemetry``. Authentication uses a Bearer
token from ``REVIEW_TELEMETRY_HTTP_TOKEN``; if the env var is empty or unset
the routes are open (intended for local dev only).
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from pr_agent.log import get_logger
from pr_agent.telemetry.store import get_default_store


def _auth_token() -> Optional[str]:
    return os.environ.get("REVIEW_TELEMETRY_HTTP_TOKEN") or None


def _require_auth(request: Request) -> None:
    expected = _auth_token()
    if not expected:
        return
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = header.split(" ", 1)[1].strip()
    if token != expected:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid token")


router = APIRouter(prefix="/api/v1/telemetry", tags=["telemetry"], dependencies=[Depends(_require_auth)])


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "backend": os.environ.get("REVIEW_TELEMETRY_BACKEND", "sqlite")}


@router.get("/metrics/overview")
def metrics_overview(since: Optional[str] = None) -> dict:
    return get_default_store().overview(since=since)


@router.get("/metrics/rules")
def metrics_rules(since: Optional[str] = None) -> list[dict]:
    return get_default_store().per_rule_stats(since=since)


@router.get("/metrics/authors")
def metrics_authors(since: Optional[str] = None) -> list[dict]:
    return get_default_store().per_author_stats(since=since)


@router.get("/metrics/severity")
def metrics_severity(since: Optional[str] = None, pr_url: Optional[str] = None) -> list[dict]:
    """Suggestion counts grouped by derived severity (critical/high/medium/low/unknown).

    The ``pr_url`` parameter lets the resolver pull per-project rule files
    (e.g. ``.agents/rules/*.md``) for the matching MR. When omitted, only
    the config-level pattern fallback and LLM importance numeric thresholds
    are used.
    """
    return get_default_store().severity_breakdown(since=since, pr_url=pr_url)


@router.get("/mrs")
def list_mrs(limit: int = 50, project_id: Optional[int] = None, state: Optional[str] = None, since: Optional[str] = None) -> list[dict]:
    return get_default_store().list_mrs(limit=limit, project_id=project_id, state=state, since=since)


@router.get("/mrs/{project_id}/{mr_id}")
def get_mr(project_id: int, mr_id: int) -> dict:
    mr = get_default_store().get_mr(project_id, mr_id)
    if not mr:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "MR not found")
    return mr


@router.get("/mrs/{project_id}/{mr_id}/suggestions")
def list_suggestions(project_id: int, mr_id: int, pr_url: Optional[str] = None) -> list[dict]:
    pr = pr_url or f"http://placeholder.local/-/merge_requests/{mr_id}"
    return get_default_store().list_suggestions(mr_id=mr_id, project_id=project_id, pr_url=pr)


@router.get("/mrs/{project_id}/{mr_id}/runs")
def list_runs(project_id: int, mr_id: int, limit: int = 20) -> list[dict]:
    return get_default_store().list_runs(mr_id=mr_id, limit=limit)


@router.get("/mrs/{project_id}/{mr_id}/timeline")
def mr_timeline(project_id: int, mr_id: int) -> dict:
    store = get_default_store()
    return {
        "mr": store.get_mr(project_id, mr_id),
        "suggestions": store.list_suggestions(mr_id=mr_id, project_id=project_id),
        "runs": store.list_runs(mr_id=mr_id),
        "actions": store.list_actions(mr_id=mr_id),
    }


@router.get("/mrs/{project_id}/{mr_id}/stats")
def mr_stats(project_id: int, mr_id: int) -> dict:
    store = get_default_store()
    suggestions = store.list_suggestions(mr_id=mr_id, project_id=project_id)
    runs = store.list_runs(mr_id=mr_id)
    counts = {"applied": 0, "dismissed": 0, "open": 0, "superseded": 0, "total": len(suggestions)}
    for s in suggestions:
        counts[s.get("state", "open")] = counts.get(s.get("state", "open"), 0) + 1
    # adopted_implicitly count is sourced from action_events (not suggestions.state).
    # Both state=applied (GitLab Apply click) and adopted_implicitly (/adopt reply)
    # are treated as adoption; state stays simple (applied | dismissed | open | superseded)
    # so dashboard semantics match the user-facing distinction "采纳 vs 忽略".
    counts["adopted_implicitly"] = store.count_adopted_implicitly(mr_id)
    rule_keys = sorted({k for s in suggestions for k in s.get("rule_keys", [])})
    severity_counts: dict[str, int] = {}
    for s in suggestions:
        sev = s.get("severity", "unknown")
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    if counts["total"]:
        # state=applied already covers BOTH GitLab Apply-click and /adopt
        # (mark_suggestion_adopted writes state="applied"). The
        # drill-down `adopted_implicitly` field is NOT added to the rate to
        # avoid double-counting /adopt entries.
        adoption_rate = counts["applied"] / counts["total"]
    else:
        adoption_rate = 0.0
    return {
        "mr_id": mr_id,
        "project_id": project_id,
        "suggestion_counts": counts,
        "adoption_rate": adoption_rate,
        "distinct_rules": rule_keys,
        "severity_counts": severity_counts,
        "runs": runs,
    }


@router.get("/dismissals")
def list_dismissals(
    since: Optional[str] = None,
    project_id: Optional[int] = None,
    rule_key: Optional[str] = None,
    mr_id: Optional[int] = None,
    limit: int = 200,
) -> list[dict]:
    """Dismissed suggestions, optionally filtered by project / rule_key / mr_id / time.

    Each row includes the user-supplied reason text in ``dismissed_reason``.
    Intended to feed the frontend "why are suggestions being dismissed?" view
    and to surface signals for tuning AGENTS.md rule keys.
    """
    return get_default_store().list_dismissals(
        since=since,
        project_id=project_id,
        rule_key=rule_key,
        mr_id=mr_id,
        limit=limit,
    )


@router.get("/dismissals/by-rule")
def dismissals_by_rule(
    since: Optional[str] = None,
    project_id: Optional[int] = None,
) -> list[dict]:
    """Dismissal counts grouped by rule_key with reason text distribution.

    Output rows: ``[{rule_key, dismissal_count, reasons: [{reason, count}]}]``,
    ordered by ``dismissal_count`` desc. ``(no reason given)`` buckets rows
    where the user dismissed without supplying a reason.

    This is the primary signal the frontend uses to suggest AGENTS.md rule
    refinements: rules with high dismissal counts and concentrated reasons
    ("误报", "重复", "项目不需要") are candidates for adjustment.
    """
    return get_default_store().list_dismissals_by_rule(
        since=since,
        project_id=project_id,
    )


def install_routes(app) -> None:
    """Mount telemetry routes on a FastAPI app (idempotent)."""
    if any(getattr(r, "path", "").startswith("/api/v1/telemetry") for r in app.router.routes):
        get_logger().debug("Telemetry routes already mounted")
        return
    app.include_router(router)
