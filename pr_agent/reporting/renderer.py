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
    # ``{branch}`` is substituted at render time with the section's
    # ``target_branch`` (e.g. "main" / "master") so the heading tracks
    # whatever branch the project actually merged into.
    "master_merges": "二、本周 {branch} 变更汇总",
    "repo_scan": "三、本周代码质量扫描",
}


_HEADING_RE = __import__("re").compile(r"^#{1,6}\s+(.+?)\s*$")


def _demote_llm_headings(md: str | None) -> str:
    """Demote LLM-produced ``#`` heading lines to ``**bold**`` so they
    don't render as oversized titles in narrow DingTalk panels.

    Tolerant: unknown / non-conforming LLM output is unchanged so we
    never throw inside a render path.
    """
    if not md:
        return md or ""
    out_lines: list[str] = []
    for line in md.splitlines():
        m = _HEADING_RE.match(line.rstrip())
        if not m:
            out_lines.append(line)
            continue
        title = m.group(1).strip()
        # Drop any trailing ## / # markers and wrap as bold.
        out_lines.append(f"**{title}**")
    return "\n".join(out_lines)



def _wrap(s: str, width: int = 22) -> str:
    """Wrap a long string at word boundaries, joining lines with ``<br>``.

    DingTalk markdown tables don't auto-wrap long cells in the rendered panel,
    so a 60-char title in one cell looks horizontally truncated. Inserting
    ``<br>`` between roughly ``width``-char chunks forces the cell to break
    across multiple lines and show the full string.
    """
    if not s:
        return ""
    s = s.replace("|", "\\|").replace("\n", " ")
    if len(s) <= width:
        return s
    words = s.split(" ")
    out: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for w in words:
        if not cur:
            cur = [w]
            cur_len = len(w)
            continue
        if cur_len + 1 + len(w) > width:
            out.append(" ".join(cur))
            cur = [w]
            cur_len = len(w)
        else:
            cur.append(w)
            cur_len += 1 + len(w)
    if cur:
        out.append(" ".join(cur))
    return "<br>".join(out)




def render_markdown(artifact: WeeklyArtifact, *, project_name: str | None = None) -> str:
    parts: list[str] = []
    title = "# 📊 项目代码检视周报"
    if project_name:
        title += f" — {project_name}"
    title += f" — {artifact.week_label}\n"

    # Trim the date strings inside blockquotes so they wrap cleanly.
    def _short(iso: str) -> str:
        return iso[:16].replace("T", " ") if iso else ""

    def _date_only(iso: str) -> str:
        return iso[:10] if iso else ""

    parts.append(
        title
        + "\n"
        # Drop the timezone tag from the header (no longer useful in the
        # DingTalk panel — the artifact JSON still carries it). Use a
        # <br> inside the blockquote so the 范围 part always lands on its
        # own line regardless of how DingTalk wraps the preceding text.
        + f"> 生成时间: {_short(artifact.generated_at.isoformat() if artifact.generated_at else '')}"
        # Drop the redundant time-of-day portion of week_start/week_end
        # (always 00:00 / 23:59) so the 数据范围 line fits on a single row.
        + f"<br>> 数据范围: {_date_only(artifact.week_start.isoformat())} ~ {_date_only(artifact.week_end.isoformat())}\n"
    )

    failures: list[str] = []
    for name in ("telemetry", "master_merges", "repo_scan"):
        section = artifact.sections.get(name)
        if section is None:
            parts.append(f"\n## {SECTION_TITLES.get(name, name)}\n\n> 本节未启用\n")
            continue

        title = SECTION_TITLES.get(name, name)
        if name == "master_merges":
            branch = (section.data or {}).get("target_branch") or "main"
            title = title.format(branch=branch)
        parts.append(f"\n## {title}\n")
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
        # Defensively demote any `#` headings the LLM emitted so they
        # render as bold rather than oversized titles.
        return _demote_llm_headings(section.markdown).strip()

    data = section.data or {}
    if name == "telemetry":
        return _render_telemetry(data)
    if name == "master_merges":
        # Pre-rendered LLM Description markdown — demote headings inline.
        cleaned = (data or {}).get("llm_description_markdown", "")
        data = {**(data or {}), "llm_description_markdown": _demote_llm_headings(cleaned)}
        return _render_master_merges(data)
    return ""


def _render_telemetry(data: dict[str, Any]) -> str:
    """Render the telemetry section as a single 2-column ``| 指标 | 数值 |`` table.

    Severity and ``触发最多规则`` are multi-entry blocks — render each
    entry on its own line inside the ``数值`` cell by joining with
    ``<br>`` (DingTalk does not auto-wrap table cells; ``<br>`` forces
    the line break and shows the full content without horizontal
    truncation).
    """
    mr_count = data.get("mr_count", 0)
    mr_total = data.get("mr_total", 0)
    suggestion_count = data.get("suggestion_count", 0)
    adoption_pct = round(float(data.get("adoption_rate", 0)) * 100, 1)
    sev = data.get("severity_breakdown") or {}
    rules = data.get("top_rules") or []

    sev_value = "<br>".join(f"{k}={v}" for k, v in sev.items()) if sev else "(无)"

    if rules:
        rules_value = "<br>".join(f"{rk} ×{n}" for rk, n in rules[:5])
    else:
        rules_value = "(无)"

    rows: list[tuple[str, str]] = [
        ("本周窗口 MR 数", str(mr_count)),
        ("项目累计 MR 数", str(mr_total)),
        ("累计 suggestion 数", str(suggestion_count)),
        ("采纳率", f"{adoption_pct}%"),
        ("severity 分布", sev_value),
        ("触发最多规则", rules_value),
    ]

    lines: list[str] = [
        "| 指标 | 数值 |",
        "|---|---|",
    ]
    for label, value in rows:
        lines.append(f"| {label} | {value} |")
    return "\n".join(lines)


def _render_master_merges(data: dict[str, Any]) -> str:
    merge_count = int(data.get("merge_count", 0))
    if merge_count == 0:
        return (
            f"目标分支 `{data.get('target_branch', '?')}` 本周窗口内无 MR 合并。"
        )

    mr_list = data.get("mr_list") or []
    summary = data.get("llm_description_markdown") or ""
    table = (
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
        # Wrap the full title with <br> at word boundaries so DingTalk
        # shows the complete title on multiple lines instead of
        # horizontally truncating it. The full title is preserved in the
        # JSON artifact for TestMate.
        rows.append(f"| {cell} | {_wrap(title, width=22)} | {author} | {merged_at} |")
    table_md = table + "\n" + "\n".join(rows)

    if summary:
        # LLM-generated project-level 变更摘要 block, then MR list.
        head_line = (
            f"本周合并到 `{data.get('target_branch', '?')}` 的 MR 共 **{merge_count}** 个, "
            f"涉及作者 **{data.get('author_count', 0)}** 位, "
            f"新增代码 **{data.get('additions', 0)}** 行, "
            f"删除 **{data.get('deletions', 0)}** 行。"
        )
        return (
            head_line
            + "\n\n### 变更摘要\n\n"
            + summary.strip()
            + "\n\n#### 涉及 MR 列表\n\n"
            + table_md
        )
    # Fall back: LLM was disabled or failed — just the headline + table.
    head_line = (
        f"本周合并到 `{data.get('target_branch', '?')}` 的 MR 共 **{merge_count}** 个, "
        f"涉及作者 **{data.get('author_count', 0)}** 位, "
        f"新增代码 **{data.get('additions', 0)}** 行, "
        f"删除 **{data.get('deletions', 0)}** 行。"
    )
    return head_line + "\n\n" + table_md


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
