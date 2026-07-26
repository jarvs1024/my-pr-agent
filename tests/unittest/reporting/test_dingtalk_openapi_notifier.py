"""Tests for the DingTalk OpenAPI notifier (AppKey + robotCode flow)."""
from __future__ import annotations

# Preload config_loader to break the circular import dance between
# pr_agent.config_loader and pr_agent.log.
from pr_agent import config_loader as _cfg_loader  # noqa: F401

from unittest.mock import MagicMock, patch

import pytest

from pr_agent.reporting.notifiers.dingtalk_openapi import DingTalkOpenAPINotifier


def test_dry_run_returns_success_without_calling_api():
    n = DingTalkOpenAPINotifier(
        app_key="dummy", app_secret="dummy", robot_code="rc", open_conversation_id="oc",
        dry_run=True,
    )
    result = n.send("title", ["# hello", "## a", "more"])
    assert result.success is True
    assert result.chunks_sent == 3
    assert result.meta.get("dry_run") is True


def test_missing_config_returns_failure():
    n = DingTalkOpenAPINotifier(
        app_key="", app_secret="", robot_code="", open_conversation_id="",
        dry_run=False,
    )
    result = n.send("title", ["body"])
    assert result.success is False
    assert "missing one of" in (result.error or "")


def test_empty_chunks_is_success():
    n = DingTalkOpenAPINotifier(
        app_key="k", app_secret="s", robot_code="r", open_conversation_id="c", dry_run=True
    )
    result = n.send("title", [])
    assert result.success is True
    assert result.chunks_sent == 0


def test_sends_each_chunk_with_access_token(monkeypatch):
    """Stub requests to verify the access-token + send flow."""
    import requests

    call_log = []

    def fake_post(url, **kwargs):
        call_log.append((url, kwargs))
        resp = MagicMock()
        resp.headers = {"content-type": "application/json"}

        if url.endswith("/v1.0/oauth2/accessToken"):
            resp.json.return_value = {"accessToken": "tok-123", "expireIn": 7200}
            resp.raise_for_status = lambda: None
        else:
            # First chunk success; second chunk transient errcode then success.
            chunk_idx = sum(1 for u, _ in call_log if "groupMessages/send" in u)
            if chunk_idx == 1:
                resp.json.return_value = {"errcode": 30013, "errmsg": "rate limit"}
            else:
                resp.json.return_value = {"errcode": 0}
            resp.raise_for_status = lambda: None
        return resp

    monkeypatch.setattr("requests.post", fake_post)
    # Disable retry backoff sleep
    monkeypatch.setattr("time.sleep", lambda *_: None)

    n = DingTalkOpenAPINotifier(
        app_key="k", app_secret="s", robot_code="rc-1",
        open_conversation_id="conv-1",
        retry_attempts=3,
        dry_run=False,
    )
    result = n.send("📊 test", ["chunk one", "chunk two"])
    # Token fetched once (cached), 2 send calls (one retried once)
    token_calls = [u for u, _ in call_log if u.endswith("/v1.0/oauth2/accessToken")]
    send_calls = [u for u, _ in call_log if "groupMessages/send" in u]
    assert len(token_calls) == 1
    assert len(send_calls) == 3  # chunk1 ok, chunk2: 1 retry + 1 success = 2
    assert result.success is True
    assert result.chunks_sent == 2


def test_total_failure_when_all_retries_exhausted(monkeypatch):
    import requests

    def fake_post(url, **kwargs):
        resp = MagicMock()
        resp.headers = {"content-type": "application/json"}
        if url.endswith("/v1.0/oauth2/accessToken"):
            resp.json.return_value = {"accessToken": "tok", "expireIn": 7200}
        else:
            resp.json.return_value = {"errcode": 999, "errmsg": "boom"}
        resp.raise_for_status = lambda: None
        return resp

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    n = DingTalkOpenAPINotifier(
        app_key="k", app_secret="s", robot_code="rc", open_conversation_id="conv",
        retry_attempts=2,
    )
    result = n.send("title", ["x"])
    assert result.success is False
    assert result.chunks_sent == 0
    assert "boom" in (result.error or "")

# Ensure the module imports cleanly (also covers the typing imports above).
def test_module_imports():
    from pr_agent.reporting.notifiers import dingtalk_openapi  # noqa: F401
    assert hasattr(dingtalk_openapi, "DingTalkOpenAPINotifier")
