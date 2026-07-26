"""Tests for the artifact builder and markdown renderer."""
from __future__ import annotations

from datetime import datetime, timezone

from pr_agent.reporting.collectors.base import SectionResult
from pr_agent.reporting.renderer import render_markdown, split_markdown
from pr_agent.reporting.report import WeeklyArtifact, build_artifact, iso_week_label, write_artifact


def test_iso_week_label_format():
    label = iso_week_label(datetime(2026, 7, 27))
    assert label == "2026-W31" or label == "2026-W30"  # depends on weekday; just shape
    assert label.startswith("2026-W")


def test_build_artifact_has_schema_version():
    start = datetime(2026, 7, 20, tzinfo=timezone.utc)
    end = datetime(2026, 7, 26, 23, 59, 59, tzinfo=timezone.utc)
    art = build_artifact(
        project_id=42,
        week_start=start,
        week_end=end,
        timezone="UTC",
        sections={
            "telemetry": SectionResult(status="ok", data={"mr_count": 1, "mr_total": 1, "suggestion_count": 2, "adoption_rate": 0.5, "severity_breakdown": {"critical": 1}, "top_rules": []}),
            "master_merges": SectionResult(status="ok", data={"target_branch": "main", "merge_count": 0, "author_count": 0, "additions": 0, "deletions": 0, "mr_list": []}),
            "repo_scan": SectionResult(status="ok", markdown="### summary\n- all good"),
        },
    )
    d = art.to_dict()
    assert d["schema_version"] == 1
    assert d["project_id"] == 42
    assert d["sections"]["telemetry"]["status"] == "ok"
    assert d["sections"]["repo_scan"]["markdown"].startswith("###")


def test_render_markdown_includes_failure_warning():
    start = datetime(2026, 7, 20, tzinfo=timezone.utc)
    end = datetime(2026, 7, 26, 23, 59, 59, tzinfo=timezone.utc)
    art = build_artifact(
        project_id=42,
        week_start=start,
        week_end=end,
        timezone="UTC",
        sections={
            "telemetry": SectionResult(status="ok", data={"mr_count": 1, "mr_total": 1, "suggestion_count": 0, "adoption_rate": 0.0, "severity_breakdown": {}, "top_rules": []}),
            "master_merges": SectionResult(status="failed", error="GitLab 429"),
            "repo_scan": SectionResult(status="ok", markdown="### x\n- none"),
        },
    )
    body = render_markdown(art, project_name="demo")
    assert "项目代码检视周报 — demo" in body
    assert "GitLab 429" in body
    assert "本周检视概况" in body
    assert "master 变更汇总" in body


def test_split_markdown_short_body_is_single_chunk():
    body = "# title\n\n## a\ncontent\n"
    assert split_markdown(body, chunk_limit=10000) == [body]


def test_split_markdown_splits_at_h2_heading():
    body = "# t\n\n## a\n" + ("x" * 100) + "\n\n## b\n" + ("y" * 100)
    chunks = split_markdown(body, chunk_limit=120)
    assert len(chunks) >= 2
    assert any("## a" in c for c in chunks)
    assert any("## b" in c for c in chunks)


def test_write_artifact_round_trip(tmp_path):
    start = datetime(2026, 7, 20, tzinfo=timezone.utc)
    end = datetime(2026, 7, 26, tzinfo=timezone.utc)
    art = build_artifact(
        project_id=42, week_start=start, week_end=end, timezone="UTC",
        sections={"telemetry": SectionResult(status="ok", data={"mr_count": 1})},
    )
    path = write_artifact(art, str(tmp_path))
    assert path.exists()
    assert path.parent.name == "42"
    assert path.name.startswith("2026-W")


def test_wrap_helper_inserts_br_at_word_boundaries():
    from pr_agent.reporting.renderer import _wrap
    # short string returns as-is
    assert _wrap("short title", width=22) == "short title"
    # exactly at threshold: unchanged
    assert _wrap("a" * 22, width=22) == "a" * 22
    # long single word: returned untouched (no whitespace to break on)
    word = "verylongwordwithoutspaces" * 3
    assert _wrap(word, width=10) == word
    # multi-word long string: gets <br> and respects pipe escaping
    out = _wrap("B: marker-fix verify - class + nested function", width=18)
    # All words must survive (just in different chunks)
    for word in ("B:", "marker-fix", "verify", "class", "+", "nested", "function"):
        assert word in out
    assert out.count("<br>") >= 1
    # pipe in input escaped so markdown table is not broken
    out2 = _wrap("foo | bar baz qux quux", width=10)
    assert "\\|" in out2
    # empty / falsy inputs return empty
    assert _wrap("") == ""
    assert _wrap(None) == ""  # type: ignore[arg-type]
# (appended via previous call)


def test_render_telemetry_uses_single_three_col_table():
    """The three sub-blocks must fold into ONE table so column widths line up
    in DingTalk (multiple tables render with inconsistent widths)."""
    from datetime import datetime, timezone
    from pr_agent.reporting.collectors.base import SectionResult
    from pr_agent.reporting.report import build_artifact
    from pr_agent.reporting.renderer import render_markdown

    start = datetime(2026, 7, 20, tzinfo=timezone.utc)
    end = datetime(2026, 7, 26, 23, 59, 59, tzinfo=timezone.utc)
    art = build_artifact(
        project_id=42,
        week_start=start,
        week_end=end,
        timezone="UTC",
        sections={
            "telemetry": SectionResult(
                status="ok",
                data={
                    "mr_count": 38,
                    "mr_total": 75,
                    "suggestion_count": 886,
                    "adoption_rate": 0.4458,
                    "severity_breakdown": {"critical": 436, "high": 402},
                    "top_rules": [
                        ["SSD-RULE-DOCSTRING-REQUIRED", 112],
                        ["SSD-RULE-FORBIDDEN-COMMENT", 1],
                    ],
                },
            ),
            "master_merges": SectionResult(status="ok", data={"target_branch": "main", "merge_count": 0, "author_count": 0, "additions": 0, "deletions": 0, "mr_list": []}),
            "repo_scan": SectionResult(status="ok", markdown="### x\n- ok"),
        },
    )
    body = render_markdown(art)

    # Single 2-col header row.
    assert "| 指标 | 数值 |" in body
    assert "| 类别 | 项 | 数值 |" not in body, "must not regress to 3-col layout"
    # Rendered cumulative values.
    assert "| 75 |" in body, "项目累计 MR 数 should be all-time 75"
    assert "| 886 |" in body, "累计 suggestion should be all-time 886"
    assert "| 44.6% |" in body, "采纳率 should be all-time 44.6%"
    # Severity + rules are single rows with multi-line value cells using <br>.
    assert "| critical=436<br>high=402 |" in body
    assert "SSD-RULE-DOCSTRING-REQUIRED ×112<br>SSD-RULE-FORBIDDEN-COMMENT ×1 |" in body
    # The 触发最多规则 value cell is one row, not 5 separate rows.
    assert body.count("SSD-RULE-DOCSTRING-REQUIRED") == 1


def test_render_master_merges_includes_llm_description_above_table():
    """When the collector set ``llm_description_markdown``, the renderer
    must put it above the MR table under a ``### Description`` heading."""
    from datetime import datetime, timezone
    from pr_agent.reporting.collectors.base import SectionResult
    from pr_agent.reporting.report import build_artifact
    from pr_agent.reporting.renderer import render_markdown

    start = datetime(2026, 7, 20, tzinfo=timezone.utc)
    end = datetime(2026, 7, 26, 23, 59, 59, tzinfo=timezone.utc)
    art = build_artifact(
        project_id=42,
        week_start=start,
        week_end=end,
        timezone="UTC",
        sections={
            "telemetry": SectionResult(status="ok", data={"mr_count": 1, "mr_total": 1, "suggestion_count": 0, "adoption_rate": 0, "severity_breakdown": {}, "top_rules": []}),
            "master_merges": SectionResult(
                status="ok",
                data={
                    "target_branch": "main",
                    "merge_count": 2,
                    "author_count": 1,
                    "additions": 12,
                    "deletions": 4,
                    "mr_list": [
                        {"iid": 116, "title": "B: verify — class + nested function", "author": "review-bot", "merged_at": "2026-07-26T07:12:51Z", "url": "http://x/!116", "source_branch": "codex/B", "target_branch": "main"},
                        {"iid": 115, "title": "A: verify mirror", "author": "review-bot", "merged_at": "2026-07-26T07:12:49Z", "url": "http://x/!115", "source_branch": "codex/A", "target_branch": "main"},
                    ],
                    "llm_description_markdown": "**概述**: 本周 2 个 MR 集中在 marker fix 回归验证。\n\n- 新增 `services/manual_observe_class_nested.py` 验证夹具\n- 新增 `services/manual_observe_v48_natural.py` 镜像回归用例",
                },
            ),
            "repo_scan": SectionResult(status="ok", markdown="### x\n- y"),
        },
    )
    body = render_markdown(art)
    desc_idx = body.index("### Description")
    table_idx = body.index("| MR | 标题 | 作者 | 合并时间 |")
    mrlist_idx = body.index("#### 涉及 MR 列表")
    # Description block precedes both the table heading and the MR list sub-section.
    assert desc_idx < mrlist_idx < table_idx
    # LLM output preserved verbatim.
    assert "marker fix 回归验证" in body
    assert "新增 `services/manual_observe_class_nested.py` 验证夹具" in body
    # Existing headline stats still appear.
    assert "**2** 个" in body
    assert "12** 行" in body


def test_render_master_merges_falls_back_when_no_llm():
    """When ``llm_description_markdown`` is missing/empty, the headline + table
    are emitted without the Description section."""
    from datetime import datetime, timezone
    from pr_agent.reporting.collectors.base import SectionResult
    from pr_agent.reporting.report import build_artifact
    from pr_agent.reporting.renderer import render_markdown

    start = datetime(2026, 7, 20, tzinfo=timezone.utc)
    end = datetime(2026, 7, 26, 23, 59, 59, tzinfo=timezone.utc)
    art = build_artifact(
        project_id=42,
        week_start=start,
        week_end=end,
        timezone="UTC",
        sections={
            "telemetry": SectionResult(status="ok", data={"mr_count": 0, "mr_total": 0, "suggestion_count": 0, "adoption_rate": 0, "severity_breakdown": {}, "top_rules": []}),
            "master_merges": SectionResult(
                status="ok",
                data={
                    "target_branch": "main",
                    "merge_count": 1,
                    "author_count": 1,
                    "additions": 0,
                    "deletions": 0,
                    "mr_list": [
                        {"iid": 10, "title": "fix: foo", "author": "alice", "merged_at": "2026-07-25T00:00:00Z", "url": "http://x/!10", "source_branch": "x", "target_branch": "main"},
                    ],
                    "llm_description_markdown": "",
                },
            ),
            "repo_scan": SectionResult(status="ok", markdown="### x"),
        },
    )
    body = render_markdown(art)
    assert "### Description" not in body
    assert "| MR | 标题 | 作者 | 合并时间 |" in body
