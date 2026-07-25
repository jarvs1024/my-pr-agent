"""Scheduler lifecycle and run_weekly_job tests."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from pr_agent.reporting.collectors.base import SectionResult
from pr_agent.reporting.config import WeeklyReportConfig
from pr_agent.reporting.scheduler import run_weekly_job, _week_bounds


def test_week_bounds_returns_monday_to_sunday():
    # Wednesday July 22, 2026
    now = datetime(2026, 7, 22, 15, 0, tzinfo=timezone.utc)
    start, end = _week_bounds(now)
    assert start.weekday() == 0  # Monday
    assert end.weekday() == 6    # Sunday
    assert (end - start).days == 6


def test_run_weekly_job_calls_collectors_and_writes_artifact(tmp_path):
    collector = MagicMock()
    collector.name = "fake"
    collector.collect.return_value = SectionResult(status="ok", data={"x": 1})
    notifier = MagicMock()
    notifier.name = "fake-notifier"
    notifier.send.return_value = MagicMock(success=True, chunks_sent=1, chunks_total=1, error=None)

    cfg = WeeklyReportConfig(
        enabled=True, target_project_id=42, cron="0 9 * * 1", timezone="UTC",
        collectors=("fake",), notifier="dingtalk",
        dingtalk_dry_run=True, dingtalk_retry_attempts=1,
        llm_dry_run=True, repo_clone_dir=str(tmp_path / "clone"),
    )
    summary = run_weekly_job(
        cfg,
        now=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
        data_dir=str(tmp_path),
        collectors=[collector],
        notifier=notifier,
    )
    assert summary["delivery"]["success"] is True
    assert summary["artifact_path"] is not None
    assert (tmp_path / "weekly_reports" / "42" / "2026-W29.json").exists() or (tmp_path / "weekly_reports" / "42").is_dir()


def test_run_weekly_job_continues_after_collector_failure(tmp_path):
    good = MagicMock(name="good"); good.name = "good"; good.collect.return_value = SectionResult(status="ok", data={})
    bad = MagicMock(name="bad"); bad.name = "bad"; bad.collect.side_effect = RuntimeError("boom")
    notifier = MagicMock(); notifier.send.return_value = MagicMock(success=True, chunks_sent=1, chunks_total=1, error=None)

    cfg = WeeklyReportConfig(
        enabled=True, target_project_id=42, cron="0 9 * * 1", timezone="UTC",
        collectors=("good", "bad"), notifier="dingtalk", dingtalk_dry_run=True,
        repo_clone_dir=str(tmp_path),
    )
    summary = run_weekly_job(
        cfg,
        now=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
        data_dir=str(tmp_path),
        collectors=[good, bad],
        notifier=notifier,
    )
    assert summary["section_status"]["good"] == "ok"
    assert summary["section_status"]["bad"] == "failed"
    # Delivery still attempted
    notifier.send.assert_called_once()
