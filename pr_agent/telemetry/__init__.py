"""Review telemetry — collects /improve, /review, /describe events for downstream BI.

Module surface:
    store        — SQLite/JSONL event store
    models       — dataclasses + dict-shaped payloads
    events       — thin emitter used by hooks in tools/* and servers/*
    api          — FastAPI routes mounted by the gitlab_webhook server
"""
from pr_agent.telemetry.store import TelemetryStore, get_default_store

__all__ = ["TelemetryStore", "get_default_store"]
