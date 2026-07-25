"""Collects weekly review-activity aggregates from the telemetry store.

This is the Layer-A collector: it does not call any external service. It
re-uses the existing :class:`TelemetryStore` aggregations. The data is
read-only and never writes back into the store.

Shape notes:
- ``overview()`` returns a dict with ``mrs.{total,merged,open}``,
  ``suggestions.{total,applied,dismissed,open,adoption_rate,...}``,
  ``runs.*`` and ``severity_breakdown`` (already a list).
- ``severity_breakdown()`` is also returned as a list of dicts.
- ``list_mrs()`` records have ``last_seen_at`` (not ``updated_at``) and
  ``mr_id`` (not ``id``).
"""
from __future__ import annotations

from pr_agent import config_loader as _cfg_loader  # noqa: F401  # breaks circular import
from datetime import datetime
from typing import Any

from pr_agent.log import get_logger
from pr_agent.telemetry import get_default_store

from .base import CollectorContext, SectionResult


_log = get_logger()


def _parse_iso(value: Any) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class TelemetryOverviewCollector:
    """Aggregate review metrics for the reporting window."""

    name = "telemetry"

    def collect(
        self,
        *,
        week_start: datetime,
        week_end: datetime,
        ctx: CollectorContext,
    ) -> SectionResult:
        store = get_default_store()
        since = week_start.isoformat()
        until = week_end.isoformat()

        overview: dict[str, Any] = store.overview(since=since)
        per_author: list[dict[str, Any]] = store.per_author_stats(since=since)
        per_rule: list[dict[str, Any]] = store.per_rule_stats(since=since)
        severity_rows: list[dict[str, Any]] = store.severity_breakdown(since=since)

        # Count MRs whose last_seen_at falls inside the reporting window.
        mr_list = store.list_mrs(limit=2000, since=since)
        ws_ts = week_start.timestamp()
        we_ts = week_end.timestamp()
        mr_count_window = sum(
            1
            for m in mr_list
            if ws_ts <= _parse_iso(m.get("last_seen_at")) <= we_ts
        )

        mrs_block = overview.get("mrs", {}) or {}
        sugs_block = overview.get("suggestions", {}) or {}

        severity_breakdown: dict[str, int] = {}
        for row in severity_rows:
            sev = str(row.get("severity", "unknown"))
            cnt = int(row.get("total", 0) or 0)
            severity_breakdown[sev] = severity_breakdown.get(sev, 0) + cnt

        top_rules = [
            [str(r.get("rule_key", "unknown")), int(r.get("count", 0) or 0)]
            for r in per_rule[:5]
            if r.get("rule_key")
        ]

        adoption_rate = float(sugs_block.get("adoption_rate", 0.0) or 0.0)

        data: dict[str, Any] = {
            "mr_count": mr_count_window,
            "mr_total": int(mrs_block.get("total", 0) or 0),
            "suggestion_count": int(sugs_block.get("total", 0) or 0),
            "adoption_rate": round(adoption_rate, 4),
            "severity_breakdown": severity_breakdown,
            "top_rules": top_rules,
            "per_author": per_author[:10],
            "window": {"since": since, "until": until},
        }

        meta = {
            "store_overview_keys": sorted(overview.keys()),
            "per_author_count": len(per_author),
            "per_rule_count": len(per_rule),
        }

        _log.info(
            "telemetry_overview: mr_window=%s mr_total=%s suggestions=%s adoption_rate=%.3f",
            mr_count_window,
            data["mr_total"],
            data["suggestion_count"],
            adoption_rate,
        )

        return SectionResult(status="ok", data=data, meta=meta)


__all__ = ["TelemetryOverviewCollector"]
