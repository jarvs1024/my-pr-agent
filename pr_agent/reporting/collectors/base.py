"""Collector protocol and result types for the weekly-report module.

Each collector pulls a slice of data for the reporting window and returns
a :class:`SectionResult`. Failures are caught by the scheduler and
serialised into ``status='failed'``; collectors should raise on real
errors rather than swallowing them so the scheduler's failure isolation
logic can record the reason.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class CollectorContext:
    """Shared context passed to each collector."""

    target_project_id: int
    data_dir: str
    llm_model: str
    llm_dry_run: bool
    repo_clone_dir: str
    diff_token_limit: int
    timezone: str
    target_branch: str = ""  # empty -> collector falls back to project default


@dataclass
class SectionResult:
    """A single section of a weekly report.

    Attributes:
        status: ``"ok"`` or ``"failed"``. ``"failed"`` indicates the
            collector raised and the scheduler converted it.
        data: structured payload that the report renderer can format.
            ``None`` when ``status="failed"``.
        markdown: optional collector-produced markdown (e.g. the LLM
            project-review block). When set, the renderer prefers this
            over a generic formatting of ``data``.
        error: short human-readable failure reason when ``status="failed"``.
        meta: free-form extra metadata (timing, token usage, etc.).
    """

    status: str = "ok"
    data: Optional[dict[str, Any]] = None
    markdown: Optional[str] = None
    error: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "data": self.data,
            "markdown": self.markdown,
            "error": self.error,
        }


@runtime_checkable
class Collector(Protocol):
    """Protocol every collector must satisfy."""

    name: str

    def collect(
        self,
        *,
        week_start: datetime,
        week_end: datetime,
        ctx: CollectorContext,
    ) -> SectionResult: ...


__all__ = ["Collector", "CollectorContext", "SectionResult"]
