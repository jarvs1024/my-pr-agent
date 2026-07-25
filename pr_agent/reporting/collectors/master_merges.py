"""Layer-B collector: lists MRs merged into the target branch this week.

Uses python-gitlab (already a project dependency) directly instead of the
``pr_agent.git_providers.*`` machinery — we are not in a PR context and
do not need the full provider surface.

The target branch defaults to the project's ``default_branch`` so the
collector works for both ``master`` and ``main`` repositories. Override
with ``ctx.target_branch`` (set via ``[weekly_report].target_branch`` in
``.pr_agent.toml``).
"""
from __future__ import annotations

from pr_agent import config_loader as _cfg_loader  # noqa: F401  # breaks circular import
import os
from datetime import datetime
from typing import Any

import gitlab

from pr_agent.log import get_logger

from .base import CollectorContext, SectionResult


_log = get_logger()


def _gitlab_client(url: str | None = None, token: str | None = None) -> gitlab.Gitlab:
    base = url or os.environ.get("GITLAB_URL", "https://gitlab.com")
    tk = token or os.environ.get("GITLAB_PERSONAL_ACCESS_TOKEN", "")
    return gitlab.Gitlab(base, private_token=tk, ssl_verify=False)


def _resolve_project(gl: gitlab.Gitlab, project_id: int):
    """Return the GitLab project handle; supports both id and path-like."""
    try:
        return gl.projects.get(project_id)
    except gitlab.exceptions.GitlabGetError:
        return gl.projects.get(f"root/auto-review-test")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class MasterMergesCollector:
    """Collect MRs merged into the target branch during the reporting window."""

    name = "master_merges"

    def __init__(self, *, gitlab_url: str | None = None, gitlab_token: str | None = None) -> None:
        self.gitlab_url = gitlab_url
        self.gitlab_token = gitlab_token

    def collect(
        self,
        *,
        week_start: datetime,
        week_end: datetime,
        ctx: CollectorContext,
    ) -> SectionResult:
        if not ctx.target_project_id:
            return SectionResult(
                status="failed",
                error="target_project_id not configured",
            )

        gl = _gitlab_client(self.gitlab_url, self.gitlab_token)
        try:
            project = _resolve_project(gl, ctx.target_project_id)
        except Exception as exc:  # noqa: BLE001
            return SectionResult(status="failed", error=f"project resolve failed: {exc}")

        target_branch = getattr(ctx, "target_branch", None) or project.default_branch or "main"
        week_start_iso = week_start.isoformat()

        try:
            mrs = project.mergerequests.list(
                state="merged",
                target_branch=target_branch,
                updated_after=week_start_iso,
                get_all=True,
                order_by="updated_at",
                sort="desc",
            )
        except Exception as exc:  # noqa: BLE001
            return SectionResult(status="failed", error=f"GitLab API failed: {exc}")

        mr_list: list[dict[str, Any]] = []
        author_set: set[str] = set()
        additions = 0
        deletions = 0

        for mr in mrs:
            merged_at = _parse_iso(mr.merged_at)
            if not merged_at or merged_at > week_end:
                continue
            author = mr.author.get("username", "") if mr.author else ""
            author_set.add(author)
            try:
                changes = mr.changes()  # noqa: SLF001 — python-gitlab detail API
                additions += int(getattr(changes, "additions", 0) or 0)
                deletions += int(getattr(changes, "deletions", 0) or 0)
            except Exception:
                pass
            mr_list.append(
                {
                    "iid": mr.iid,
                    "title": mr.title,
                    "author": author,
                    "merged_at": mr.merged_at,
                    "url": mr.web_url,
                    "source_branch": mr.source_branch,
                    "target_branch": mr.target_branch,
                    "summary": "",
                }
            )

        data: dict[str, Any] = {
            "target_branch": target_branch,
            "merge_count": len(mr_list),
            "author_count": len(author_set),
            "additions": additions,
            "deletions": deletions,
            "authors": sorted(author_set),
            "mr_list": mr_list,
            "window": {"since": week_start.isoformat(), "until": week_end.isoformat()},
        }

        _log.info(
            "master_merges: target=%s mr_count=%s authors=%s +%s/-%s",
            target_branch,
            data["merge_count"],
            data["author_count"],
            additions,
            deletions,
        )

        return SectionResult(status="ok", data=data, meta={"project_default_branch": project.default_branch})


__all__ = ["MasterMergesCollector"]
