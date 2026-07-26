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

import textwrap

from pr_agent.reporting.collectors.repo_scan import _call_llm  # shared litellm wrapper

_log = get_logger()


DESCRIBE_PROMPT_TEMPLATE = textwrap.dedent("""\
你是一名代码审查经理。下面是本周 ({since} ~ {until}) 合并到目标分支 `{target_branch}` 的 MR 列表。
请你基于每个 MR 的标题、描述(detail)和源分支名,撰写一段**项目层** Description 摘要。

要求 (markdown 格式, 中文):
- 一段概述 (1-3 句): 本周变更主题、涉及的主要模块、整体方向。
- 分类要点 (bullet),按需给出,空类别省略:
  - 新增: 新文件 / 新模块 / 新接口
  - 修改: 既有功能调整
  - 删除: 移除的文件 / 代码 / 逻辑
  - 测试: 测试相关变更
  - 重构: 结构 / 命名调整
  - 文档: 文档 / 标记文件
- 每条 bullet 含文件路径或符号,不超过 60 字。
- 不要原样复制单个 MR 标题;语义相近的条目合并一条。

若列表为空,只输出 `本周无 MR 合并`。若只有 1 条 MR,简要复述即可。

输入 MR 列表:
{mr_block}

直接输出 markdown,不要加开场白。
""")

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


def _diff_line_counts(diff_text: str | None) -> tuple[int, int]:
    """Count ``+``/``-`` lines in a unified-diff blob.

    Excludes the ``+++``/``---`` file markers (header lines starting with
    3 signs so they don't count as content additions/deletions). Used
    because python-gitlab's ``mr.changes()`` returns a dict with raw diffs
    rather than aggregated ``additions``/``deletions`` fields.
    """
    if not diff_text:
        return 0, 0
    adds = dels = 0
    for line in diff_text.splitlines():
        if not line:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            adds += 1
        elif line.startswith("-"):
            dels += 1
    return adds, dels


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
            per_add = per_del = 0
            try:
                changes = mr.changes()  # noqa: SLF001 — python-gitlab detail API
                # python-gitlab 4.x returns a dict: {'changes': [{'diff': '...'}], ...}
                if isinstance(changes, dict):
                    for entry in changes.get("changes") or []:
                        a, d = _diff_line_counts(entry.get("diff"))
                        per_add += a
                        per_del += d
                elif changes is not None:
                    # Fallback for older python-gitlab attribute-based API.
                    per_add = int(getattr(changes, "additions", 0) or 0)
                    per_del = int(getattr(changes, "deletions", 0) or 0)
                additions += per_add
                deletions += per_del
            except Exception as exc:  # noqa: BLE001
                _log.debug("master_merges: changes() failed for !%s: %s", mr.iid, exc)
            mr_list.append(
                {
                    "iid": mr.iid,
                    "title": mr.title,
                    "author": author,
                    "merged_at": mr.merged_at,
                    "url": mr.web_url,
                    "source_branch": mr.source_branch,
                    "target_branch": mr.target_branch,
                    "description": (mr.description or "").strip(),
                    "additions": per_add,
                    "deletions": per_del,
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

        # Optional LLM Description block. Builds a per-MR markdown block
        # (title + description body + branch + author) and asks the LLM to
        # synthesize a project-level summary in PR-/describe style.
        llm_md = ""
        if mr_list:
            ctx_model = getattr(ctx, "llm_model", "") or ""
            ctx_dry = bool(getattr(ctx, "llm_dry_run", True))
            if ctx_model:
                try:
                    mr_block_lines = []
                    for mr_item in mr_list:
                        body = (mr_item.get("description") or "").strip()
                        body = body if len(body) <= 800 else (body[:800] + "\n... (truncated)")
                        mr_block_lines.append(
                            f"### !{mr_item['iid']} {mr_item['author']} merged {mr_item['merged_at'][:10]}\n"
                            f"- title: {mr_item['title']}\n"
                            f"- source_branch: {mr_item['source_branch']}\n"
                            f"- description:\n{body or '(空)'}"
                        )
                    prompt = DESCRIBE_PROMPT_TEMPLATE.format(
                        since=week_start_iso[:10],
                        until=week_end.isoformat()[:10],
                        target_branch=target_branch,
                        mr_block="\n\n".join(mr_block_lines),
                    )
                    llm_md = _call_llm(prompt, ctx_model, ctx_dry)
                except Exception as exc:  # noqa: BLE001
                    _log.warning("master_merges LLM description failed: %s", exc)
                    llm_md = ""
        data["llm_description_markdown"] = llm_md.strip() if llm_md else ""

        _log.info(
            "master_merges: target=%s mr_count=%s authors=%s +%s/-%s llm_desc=%d_chars",
            target_branch,
            data["merge_count"],
            data["author_count"],
            additions,
            deletions,
            len(data["llm_description_markdown"]),
        )

        return SectionResult(status="ok", data=data, meta={"project_default_branch": project.default_branch})


__all__ = ["MasterMergesCollector"]
