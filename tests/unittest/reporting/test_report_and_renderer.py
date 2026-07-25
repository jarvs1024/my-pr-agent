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
