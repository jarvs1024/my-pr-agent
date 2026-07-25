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
