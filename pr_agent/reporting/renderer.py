"""Render a :class:`WeeklyArtifact` to Markdown for delivery.

Each section is rendered as a ``## <name>`` block. Sections that failed
render as a warning line. Body content comes from either the section's
pre-rendered ``markdown`` (e.g. the LLM review block) or a structured
format of ``data``.

The resulting Markdown is split into ``##``-aligned chunks when it
exceeds ``chunk_limit`` bytes so the IM notifier can deliver it as
multiple messages.
"""
from __future__ import annotations

import re
from typing import Any

from .collectors.base import SectionResult
from .report import WeeklyArtifact


SECTION_TITLES = {
    "telemetry": "一、本周检视概况",
    "master_merges": "二、本周 master 变更汇总",
    "repo_scan": "三、本周代码质量扫描",
}


def render_markdown(artifact: WeeklyArtifact, *, project_name: str | None = None) -> str:
    parts: list[str] = []
    title = "# 📊 项目代码检视周报"
    if project_name:
        title += f" — {project_name}"
    title += f" — {artifact.week_label}\n"

    parts.append(
        title
        + "\n"
        + f"> 生成时间: {artifact.generated_at.isoformat() if artifact.generated_at else ''} ({artifact.timezone})\n"
        + f"> 数据范围: {artifact.week_start.isoformat()} ~ {artifact.week_end.isoformat()}\n"
    )

    failures: list[str] = []
    for name in ("telemetry", "master_merges", "repo_scan"):
        section = artifact.sections.get(name)
        if section is None:
            parts.append(f"\n## {SECTION_TITLES.get(name, name)}\n\n> 本节未启用\n")
            continue

        parts.append(f"\n## {SECTION_TITLES.get(name, name)}\n")
        if section.status != "ok":
            failures.append(name)
            parts.append(f"\n⚠️ 数据缺失: {section.error or '未知原因'}\n")
            continue

        body = _render_section(name, section)
        parts.append("\n" + body + "\n")

    if failures:
        parts.append("\n---\n⚠️ 本次报告部分数据缺失: " + ", ".join(failures) + "\n")

    return "\n".join(parts).rstrip() + "\n"


def _render_section(name: str, section: SectionResult) -> str:
    if section.markdown:
        return section.markdown.strip()

    data = section.data or {}
    if name == "telemetry":
        return _render_telemetry(data)
    if name == "master_merges":
        return _render_master_merges(data)
    return ""


def _render_telemetry(data: dict[str, Any]) -> str:
    lines: list[str] = []
    sev = data.get("severity_breakdown") or {}
    sev_str = " / ".join(f"{k}={v}" for k, v in sev.items()) if sev else "无"
    rules = data.get("top_rules") or []
    rules_str = ", ".join(f"`{rk}` ×{n}" for rk, n in rules[:3]) if rules else "无"

    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| 本周窗口 MR 数 | {data.get('mr_count', 0)} |")
    lines.append(f"| 项目累计 MR 数 | {data.get('mr_total', 0)} |")
    lines.append(f"| 累计 suggestion 数 | {data.get('suggestion_count', 0)} |")
    lines.append(f"| 采纳率 | {round(float(data.get('adoption_rate', 0)) * 100, 1)}% |")
    lines.append(f"| severity 分布 | {sev_str} |")
    lines.append(f"| 触发最多的规则 | {rules_str} |")
    return "\n".join(lines)


def _render_master_merges(data: dict[str, Any]) -> str:
    merge_count = int(data.get("merge_count", 0))
    if merge_count == 0:
        return (
            f"目标分支 `{data.get('target_branch', '?')}` 本周窗口内无 MR 合并。"
        )

    mr_list = data.get("mr_list") or []
    head = (
        f"本周合并到 `{data.get('target_branch', '?')}` 的 MR 共 **{merge_count}** 个, "
        f"涉及作者 **{data.get('author_count', 0)}** 位, "
        f"新增代码 **{data.get('additions', 0)}** 行, "
        f"删除 **{data.get('deletions', 0)}** 行。\n\n"
        "| MR | 标题 | 作者 | 合并时间 |\n"
        "|---|---|---|---|"
    )
    rows: list[str] = []
    for mr in mr_list[:50]:
        iid = mr.get("iid", "?")
        title = (mr.get("title") or "").replace("|", "\\|").replace("\n", " ")
        author = mr.get("author") or "?"
        merged_at = (mr.get("merged_at") or "")[:16].replace("T", " ")
        url = mr.get("url") or ""
        cell = f"[!{iid}]({url})" if url else f"!{iid}"
        rows.append(f"| {cell} | {title[:60]} | {author} | {merged_at} |")
    return head + "\n" + "\n".join(rows)


def split_markdown(body: str, chunk_limit: int = 18000) -> list[str]:
    """Split ``body`` into chunks each under ``chunk_limit`` bytes.

    The split happens at ``## `` (level-2 heading) boundaries so each
    chunk remains a coherent section. Falls back to a hard byte-split
    when no headings exist or the first chunk already exceeds the limit.
    """
    if not body:
        return [""]
    encoded = body.encode("utf-8")
    if len(encoded) <= chunk_limit:
        return [body]

    # Try splitting at level-2 headings; keep the heading with its body.
    sections: list[str] = []
    current = ""
    for line in body.splitlines(keepends=True):
        candidate = current + line
        if line.startswith("## ") and current and len(candidate.encode("utf-8")) > chunk_limit:
            sections.append(current.rstrip())
            current = line
        else:
            current = candidate
    if current.strip():
        sections.append(current.rstrip())

    # If any section is still too large, hard-split it.
    out: list[str] = []
    for sec in sections:
        if len(sec.encode("utf-8")) <= chunk_limit:
            out.append(sec)
            continue
        for i in range(0, len(sec.encode("utf-8")), chunk_limit):
            out.append(sec.encode("utf-8")[i:i + chunk_limit].decode("utf-8", errors="ignore"))
    return out


__all__ = ["render_markdown", "split_markdown", "SECTION_TITLES"]
