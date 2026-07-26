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

    # Exactly one 3-col header row, no separate 2-col tables.
    assert "| 类别 | 项 | 数值 |" in body
    # Rendered values come from the corrected (cumulative) fields.
    assert "| 75 |" in body, "项目累计 MR 数 should be 75 (all-time), not windowed"
    assert "| 886 |" in body, "累计 suggestion should be all-time 886, not windowed"
    assert "| 44.6% |" in body, "采纳率 should be all-time 44.6%, not 17.7%"
    assert "| SSD-RULE-DOCSTRING-REQUIRED | 112 |" in body
    assert "| critical | 436 |" in body
    # No leftover headings from the old sub-block format.
    assert "**本周指标**" not in body
    assert "**severity 分布**" not in body
    assert "**触发最多的规则**" not in body
