"""Lightweight data classes for review telemetry events.

We keep models dict-shaped so the same payload goes to JSONL or SQLite without
a translation layer. Dataclasses exist only to keep call sites typed.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@dataclass
class MRActivity:
    """Created/updated when an MR is opened or new commits land."""

    mr_id: int
    project_id: int
    source_branch: str
    target_branch: str
    title: str
    author: str = ""
    state: str = "opened"
    opened_at: str = field(default_factory=_now_iso)
    last_seen_at: str = field(default_factory=_now_iso)
    merged_at: Optional[str] = None
    url: Optional[str] = None
    head_sha: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewRun:
    """One invocation of /describe, /review, /improve, /ask."""

    run_id: str = field(default_factory=lambda: _new_id("run"))
    mr_id: int = 0
    project_id: int = 0
    command: str = ""  # describe | review | improve | ask | auto
    status: str = "started"  # started | success | failed | empty
    model: Optional[str] = None
    started_at: str = field(default_factory=_now_iso)
    finished_at: Optional[str] = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None
    suggestion_count: int = 0
    rule_keys_cited: list[str] = field(default_factory=list)
    triggered_by: str = "user"  # user | webhook | push

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Suggestion:
    """One code_suggestions entry that was posted as a DiffNote."""

    suggestion_id: str = field(default_factory=lambda: _new_id("sug"))
    mr_id: int = 0
    project_id: int = 0
    file: str = ""
    line: Optional[int] = None
    label: str = ""
    importance: int = 0
    one_sentence_summary: str = ""
    rule_keys: list[str] = field(default_factory=list)
    score: Optional[int] = None
    posted_at: str = field(default_factory=_now_iso)
    state: str = "open"  # open | applied | dismissed | superseded
    applied_at: Optional[str] = None
    dismissed_at: Optional[str] = None
    dismissed_by: Optional[str] = None
    note_id=None  # GitLab discussion id (hash str)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ActionEvent:
    """Discrete user action on a suggestion thread."""

    action: str  # applied | dismissed | replied | resolved
    suggestion_id: str
    mr_id: int
    actor: str = ""
    note: str = ""
    at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
