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
def list_suggestions(project_id: int, mr_id: int) -> list[dict]:
    return get_default_store().list_suggestions(mr_id=mr_id, project_id=project_id)


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
    rule_keys = sorted({k for s in suggestions for k in s.get("rule_keys", [])})
    return {
        "mr_id": mr_id,
        "project_id": project_id,
        "suggestion_counts": counts,
        "adoption_rate": (counts["applied"] / counts["total"]) if counts["total"] else 0.0,
        "distinct_rules": rule_keys,
        "runs": runs,
    }


def install_routes(app) -> None:
    """Mount telemetry routes on a FastAPI app (idempotent)."""
    if any(getattr(r, "path", "").startswith("/api/v1/telemetry") for r in app.router.routes):
        get_logger().debug("Telemetry routes already mounted")
        return
    app.include_router(router)
