"""Tests for individual collectors with stubs."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from pr_agent.reporting.collectors.master_merges import MasterMergesCollector
from pr_agent.reporting.collectors.base import CollectorContext


def _ctx(**overrides) -> CollectorContext:
    base = dict(
        target_project_id=42,
        data_dir="/tmp/data",
        llm_model="gpt-test",
        llm_dry_run=True,
        repo_clone_dir="/tmp/clone",
        diff_token_limit=50000,
        timezone="UTC",
        target_branch="",
    )
    base.update(overrides)
    return CollectorContext(**base)


def test_master_merges_returns_failed_when_no_project_id():
    c = MasterMergesCollector()
    result = c.collect(
        week_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        week_end=datetime(2026, 1, 7, tzinfo=timezone.utc),
        ctx=_ctx(target_project_id=0),
    )
    assert result.status == "failed"
    assert "target_project_id" in (result.error or "")


def test_master_merges_handles_api_failure(monkeypatch):
    """When python-gitlab raises, the collector returns a failed SectionResult."""
    fake_gl = MagicMock()
    fake_gl.projects.get.side_effect = RuntimeError("boom")

    monkeypatch.setattr(
        "pr_agent.reporting.collectors.master_merges._gitlab_client",
        lambda url=None, token=None: fake_gl,
    )

    c = MasterMergesCollector()
    result = c.collect(
        week_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        week_end=datetime(2026, 1, 7, tzinfo=timezone.utc),
        ctx=_ctx(),
    )
    assert result.status == "failed"
    assert "boom" in (result.error or "")


def test_telemetry_overview_reads_total_field_and_separates_windowed_from_alltime(monkeypatch):
    """Regression: earlier versions read per_rule_stats().count (which never
    existed) so all top_rules silently rendered as 0. They also pulled
    mr_total / suggestion_count from the windowed overview, so they always
    equalled the weekly counter. This test pins the corrected behavior."""
    from datetime import datetime, timezone
    from pr_agent.reporting.collectors.telemetry_overview import TelemetryOverviewCollector

    # All-time overview (no since) gives the "项目累计" values.
    all_overview = {
        "mrs": {"total": 75, "merged": 6, "open": 44},
        "suggestions": {"total": 886, "applied": 395, "dismissed": 63, "open": 406, "adoption_rate": 0.4458},
    }
    # Windowed overview (with since) had been misused for cumulative fields.
    windowed_overview = {
        "mrs": {"total": 38, "merged": 2, "open": 28},
        "suggestions": {"total": 491, "applied": 87, "dismissed": 26, "open": 360, "adoption_rate": 0.1772},
    }

    class _FakeStore:
        def __init__(self):
            self.calls = {"overview_since": 0, "overview_alltime": 0}

        def overview(self, since=None):
            if since:
                self.calls["overview_since"] += 1
                return windowed_overview
            self.calls["overview_alltime"] += 1
            return all_overview

        def per_author_stats(self, since=None):
            return [{"author": "review-bot", "mr_count": 38}]

        def per_rule_stats(self, since=None):
            return [
                {"rule_key": "SSD-RULE-DOCSTRING-REQUIRED", "total": 112, "applied": 27},
                {"rule_key": "SSD-RULE-NO-BARE-PRINT", "total": 99, "applied": 17},
            ]

        def severity_breakdown(self, since=None):
            return [
                {"severity": "critical", "total": 269},
                {"severity": "high", "total": 183},
            ]

        def list_mrs(self, limit=2000, project_id=None, state=None, since=None):
            # 38 MRs seen "this week" within the window, all at the same ts.
            from datetime import datetime, timezone
            ts = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc).isoformat()
            return [{"mr_id": i, "last_seen_at": ts} for i in range(38)]

    fake = _FakeStore()
    monkeypatch.setattr(
        "pr_agent.reporting.collectors.telemetry_overview.get_default_store",
        lambda: fake,
    )

    c = TelemetryOverviewCollector()
    result = c.collect(
        week_start=datetime(2026, 7, 20, tzinfo=timezone.utc),
        week_end=datetime(2026, 7, 26, 23, 59, 59, tzinfo=timezone.utc),
        ctx=_ctx(),
    )
    assert result.status == "ok", result.error
    data = result.data

    # Windowed counters stay windowed.
    assert data["mr_count"] == 38
    # Cumulative fields now read the all-time overview.
    assert data["mr_total"] == 75, f"expected all-time 75, got {data['mr_total']}"
    assert data["suggestion_count"] == 886
    assert abs(data["adoption_rate"] - 0.4458) < 1e-3
    # top_rules now uses the real 'total' field, not the non-existent 'count'.
    assert data["top_rules"][0] == ["SSD-RULE-DOCSTRING-REQUIRED", 112]
    assert data["top_rules"][1] == ["SSD-RULE-NO-BARE-PRINT", 99]
    # Windowed values for severity / top_rules still come through correctly.
    assert data["severity_breakdown"]["critical"] == 269
    # Both overview() variants were queried.
    assert fake.calls["overview_since"] >= 1
    assert fake.calls["overview_alltime"] >= 1


def test_master_merges_records_llm_description_markdown_when_dry_run(monkeypatch):
    """The collector should populate ``llm_description_markdown`` even when
    ``llm_dry_run`` is True (mock LLM returns a deterministic stub) so the
    renderer can be exercised end-to-end without a real API call."""
    from datetime import datetime, timezone
    from pr_agent.reporting.collectors.master_merges import MasterMergesCollector
    from unittest.mock import MagicMock

    # Fake GitLab: one merged MR with a small description body.
    fake_mr = MagicMock()
    fake_mr.iid = 7
    fake_mr.title = "feat: add ComplianceOrchestrator fixture"
    fake_mr.description = "### Description\n- 新增 fixture 覆盖 class + nested function"
    fake_mr.author = {"username": "review-bot"}
    fake_mr.merged_at = "2026-07-26T07:12:51Z"
    fake_mr.web_url = "http://127.0.0.1:8929/root/auto-review-test/-/merge_requests/7"
    fake_mr.source_branch = "codex/fixture-class-nested"
    fake_mr.target_branch = "main"
    fake_changes = MagicMock()
    fake_changes.additions = 30
    fake_changes.deletions = 5
    fake_mr.changes.return_value = fake_changes

    fake_project = MagicMock()
    fake_project.default_branch = "main"
    fake_project.mergerequests.list.return_value = [fake_mr]

    fake_gl = MagicMock()
    fake_gl.projects.get.return_value = fake_project
    monkeypatch.setattr(
        "pr_agent.reporting.collectors.master_merges._gitlab_client",
        lambda url=None, token=None: fake_gl,
    )

    ctx = _ctx(llm_model="gpt-test", llm_dry_run=True)
    result = MasterMergesCollector().collect(
        week_start=datetime(2026, 7, 20, tzinfo=timezone.utc),
        week_end=datetime(2026, 7, 26, 23, 59, 59, tzinfo=timezone.utc),
        ctx=ctx,
    )
    assert result.status == "ok", result.error
    data = result.data
    # Per-MR description + stats propagated.
    assert data["mr_list"][0]["description"].startswith("### Description")
    assert data["mr_list"][0]["additions"] == 30
    assert data["mr_list"][0]["deletions"] == 5
    # LLM path was exercised (dry-run returns a deterministic stub).
    assert isinstance(data["llm_description_markdown"], str)
    assert data["llm_description_markdown"], "dry-run stub should be non-empty"


def test_diff_line_counts_excludes_header_markers():
    """_diff_line_counts should skip '+++'/'---' file header lines and the
    hunk header, but count every other +/- content line."""
    from pr_agent.reporting.collectors.master_merges import _diff_line_counts
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,2 +1,3 @@\n"
        " keep_unchanged\n"
        "-removed\n"
        "+added1\n"
        "+added2\n"
        "+added3\n"
    )
    adds, dels = _diff_line_counts(diff)
    assert adds == 3
    assert dels == 1
    # Diff without anything
    assert _diff_line_counts(None) == (0, 0)
    assert _diff_line_counts("") == (0, 0)


def test_master_merges_aggregates_real_diff_lines(monkeypatch):
    """Regression: mr.changes() in python-gitlab 4.x returns a dict; the old
    code used getattr(..., "additions") which always returned 0, so the
    weekly report showed '新增代码 0 行, 删除 0 行' even when MRs brought
    real lines in. This test verifies the diff-line aggregator picks up
    the per-file + and - counts from the dict payload."""
    from datetime import datetime, timezone
    from pr_agent.reporting.collectors.master_merges import MasterMergesCollector
    from unittest.mock import MagicMock

    fake_mr = MagicMock()
    fake_mr.iid = 11
    fake_mr.title = "add a new helper"
    fake_mr.description = ""
    fake_mr.author = {"username": "alice"}
    fake_mr.merged_at = "2026-07-26T03:00:00Z"
    fake_mr.web_url = "http://x/!11"
    fake_mr.source_branch = "x"
    fake_mr.target_branch = "main"
    fake_mr.changes.return_value = {
        "changes": [
            {
                "new_path": "services/helper.py",
                "old_path": "services/helper.py",
                "diff": (
                    "--- a/services/helper.py\n"
                    "+++ b/services/helper.py\n"
                    "@@ -0,0 +1,4 @@\n"
                    "+def f():\n"
                    "+    return 1\n"
                    "+def g():\n"
                    "+    return 2\n"
                ),
            }
        ]
    }

    fake_project = MagicMock()
    fake_project.default_branch = "main"
    fake_project.mergerequests.list.return_value = [fake_mr]

    fake_gl = MagicMock()
    fake_gl.projects.get.return_value = fake_project
    monkeypatch.setattr(
        "pr_agent.reporting.collectors.master_merges._gitlab_client",
        lambda url=None, token=None: fake_gl,
    )

    result = MasterMergesCollector().collect(
        week_start=datetime(2026, 7, 20, tzinfo=timezone.utc),
        week_end=datetime(2026, 7, 26, 23, 59, 59, tzinfo=timezone.utc),
        ctx=_ctx(llm_model="", llm_dry_run=True),
    )
    assert result.status == "ok", result.error
    # 4 added, 0 deleted from the bundled diff.
    assert result.data["additions"] == 4
    assert result.data["deletions"] == 0
