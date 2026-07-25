"""Tests for the DingTalk notifier, including dry-run mode."""
from __future__ import annotations

from pr_agent.reporting.notifiers.dingtalk import DingTalkNotifier


def test_dry_run_returns_success_without_posting():
    n = DingTalkNotifier(webhook_url="https://example.com/hook", secret="", dry_run=True)
    result = n.send("title", ["# hello", "## section", "more text"])
    assert result.success is True
    assert result.chunks_sent == 3
    assert result.chunks_total == 3
    assert result.meta.get("dry_run") is True


def test_missing_webhook_returns_failure():
    n = DingTalkNotifier(webhook_url="", secret="", dry_run=False)
    result = n.send("title", ["body"])
    assert result.success is False
    assert "DINGTALK_WEEKLY_WEBHOOK_URL not configured" in (result.error or "")


def test_empty_chunks_is_success():
    n = DingTalkNotifier(webhook_url="https://example.com/hook", dry_run=True)
    result = n.send("title", [])
    assert result.success is True
    assert result.chunks_sent == 0


def test_sign_url_injects_timestamp_and_sign():
    signed = DingTalkNotifier._sign_url if hasattr(DingTalkNotifier, "_sign_url") else None
    # The module exports a module-level _sign_url; import it directly.
    from pr_agent.reporting.notifiers import dingtalk
    url = dingtalk._sign_url("https://oapi.dingtalk.com/robot/send?access_token=abc", "secret123")
    assert "timestamp=" in url
    assert "sign=" in url
    assert "access_token=abc" in url
