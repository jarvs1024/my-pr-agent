"""Build the structured weekly-report artifact and render its markdown.

The artifact is the source of truth: it is dumped to disk as JSON and
also rendered into a Markdown body that the notifier delivers.

Structure (see spec §8):

    {
        "schema_version": 1,
        "project_id": int,
        "week_label": "2026-W30",
        "week_start": iso,
        "week_end": iso,
        "generated_at": iso,
        "timezone": "Asia/Shanghai",
        "sections": {
            "telemetry":      {status, data, markdown, error},
            "master_merges":  {...},
            "repo_scan":      {...},
        }
    }
"""
from __future__ import annotations

from pr_agent import config_loader as _cfg_loader  # noqa: F401  # breaks circular import
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from pr_agent.log import get_logger

from .collectors.base import SectionResult


_log = get_logger()


SCHEMA_VERSION = 1


@dataclass
class WeeklyArtifact:
    """A structured weekly report, persisted to disk and rendered to MD."""

    project_id: int
    week_label: str
    week_start: datetime
    week_end: datetime
    timezone: str
    sections: dict[str, SectionResult]
    generated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "project_id": self.project_id,
            "week_label": self.week_label,
            "week_start": self.week_start.isoformat(),
            "week_end": self.week_end.isoformat(),
            "generated_at": (self.generated_at or datetime.now().astimezone()).isoformat(),
            "timezone": self.timezone,
            "sections": {name: sr.to_dict() for name, sr in self.sections.items()},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def iso_week_label(dt: datetime) -> str:
    """Return ISO-week label like ``2026-W30``."""
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def build_artifact(
    *,
    project_id: int,
    week_start: datetime,
    week_end: datetime,
    timezone: str,
    sections: Mapping[str, SectionResult],
) -> WeeklyArtifact:
    return WeeklyArtifact(
        project_id=project_id,
        week_label=iso_week_label(week_start),
        week_start=week_start,
        week_end=week_end,
        timezone=timezone,
        sections=dict(sections),
        generated_at=datetime.now().astimezone(),
    )


def write_artifact(artifact: WeeklyArtifact, data_dir: str) -> Path:
    """Dump the artifact to ``data_dir/weekly_reports/<pid>/<week>.json``."""
    base = Path(data_dir) / "weekly_reports" / str(artifact.project_id)
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"{artifact.week_label}.json"
    target.write_text(artifact.to_json(), encoding="utf-8")
    _log.info("artifact written: %s", target)
    return target


def latest_artifact_path(data_dir: str, project_id: int) -> Path | None:
    """Return the path to the most recent weekly artifact for ``project_id``."""
    base = Path(data_dir) / "weekly_reports" / str(project_id)
    if not base.is_dir():
        return None
    files = sorted(base.glob("*-W*.json"), reverse=True)
    return files[0] if files else None


def list_artifact_paths(data_dir: str, project_id: int, limit: int = 12) -> list[Path]:
    base = Path(data_dir) / "weekly_reports" / str(project_id)
    if not base.is_dir():
        return []
    return sorted(base.glob("*-W*.json"), reverse=True)[:limit]


def read_artifact(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = [
    "WeeklyArtifact",
    "SCHEMA_VERSION",
    "build_artifact",
    "write_artifact",
    "latest_artifact_path",
    "list_artifact_paths",
    "read_artifact",
    "iso_week_label",
]
