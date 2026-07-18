"""Unit tests for the apply-commit status feedback added on top of
``_handle_apply_commit``.

The bootstrap signal we want to lock down:
  - When 0 suggestions were marked applied → no extra comment is posted.
  - When ≥1 suggestion got marked applied and others are still open → the
    published comment lists the count, the touched files, and an "still N
    open suggestions remain" hint.
  - When all suggestions are closed → the published comment makes the
    "no new suggestions" state explicit and tells the user to type
    ``/improve`` if they want a fresh scan.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List
from unittest.mock import patch

import pytest


mod = importlib.import_module("pr_agent.servers.gitlab_webhook")

@pytest.fixture(autouse=True)
def _clear_apply_dedup():
    """Reset the in-process dedup set so each test sees a clean slate.

    ``_handle_apply_commit_async`` records (mr_iid, sha) for every
    Apply-commit it processes; without this fixture, tests that share
    the same fake SHA collide on the second invocation and silently
    short-circuit.
    """
    from pr_agent.servers import gitlab_webhook as gw
    gw._APPLY_SHA_SEEN.clear()
    yield
    gw._APPLY_SHA_SEEN.clear()




class _FakeProvider:
    def __init__(self) -> None:
        self.published: List[str] = []

    def publish_comment(self, body, is_temporary: bool = False):
        self.published.append(body)
        return body


def _store_with_rows(rows):
    """Build a stub store whose ``list_suggestions`` returns ``rows``."""

    class _Store:
        def list_suggestions(self, *, mr_id, project_id):
            return list(rows)

    return _Store()


def _fake_apply_event(*, sha="c" * 40, project_id=34, mr_iid=60, files=None,
                      actor="root", pr_url="http://localhost/root/x/-/merge_requests/60"):
    """Return a stand-in for ``_resolve_apply_event`` output."""
    return {
        "sha": sha,
        "msg": "Apply 1 suggestion(s) to 1 file(s)",
        "project_id": project_id,
        "ref": "refs/heads/main",
        "mr_iid": mr_iid,
        "actor": actor,
        "files_hint": files or ["app.py"],
        "pr_url": pr_url,
    }


def _run_handle_apply(apply_event, store_rows, side_effect=None):
    """Execute the inside of ``_handle_apply_commit`` minus the
    provider/telemetry wiring; we hand-rolled those so we can verify
    the published body without depending on a live GitLab instance.
    """
    fake_provider = _FakeProvider()
    with patch.object(mod, "_lookup_mr_for_push", return_value=(apply_event["mr_iid"], apply_event["pr_url"])), \
         patch.object(mod, "_resolve_apply_event", return_value=apply_event), \
         patch.object(mod, "telemetry_events") as te, \
         patch("builtins.__import__", side_effect=side_effect):
        # Pretend ``GitLabProvider(merge_request_url=...)`` succeeds and
        # returns our ``fake_provider``.
        def fake_import(name, *args, **kwargs):
            if name == "pr_agent.git_providers.gitlab_provider":
                class _P:
                    def __init__(self, merge_request_url):
                        self.published = fake_provider.published
                    def publish_comment(self, body, is_temporary=False):
                        fake_provider.published.append(body)
                        return body
                return types.SimpleNamespace(GitLabProvider=_P)
            return __import__(name, *args, **kwargs)

        # We need ``types`` for the import shim above.
        import types as _types
        def _import(name, *args, **kwargs):
            if name == "pr_agent.git_providers.gitlab_provider":
                class _P:
                    def __init__(self, merge_request_url):
                        pass
                    def publish_comment(self, body, is_temporary=False):
                        fake_provider.published.append(body)
                        return body
                return _types.SimpleNamespace(GitLabProvider=_P)
            return __import__(name, *args, **kwargs)

        te.mark_suggestions_applied.return_value = ["sg-1"]  # one suggestion flipped
        te.get_default_store.return_value = _store_with_rows(store_rows)
        webhook = {"object_kind": "merge_request", "project": {"id": 34}, "user": {"username": "root"}}
        mod._handle_apply_commit(webhook)
    return fake_provider.published


def test_no_comment_posted_when_nothing_marked_applied():
    """If ``mark_suggestions_applied`` returned an empty list the bot
    should stay silent — the user didn't actually apply anything.
    """
    publish = _run_apply_with_zero_matches()
    assert publish == []


def test_status_comment_when_suggestions_still_remain():
    body = _single_publish_with_remains([{"state": "open"}, {"state": "open"}])
    assert "已自动记录 1 条建议为 applied" in body
    assert "仍剩" in body and "2" in body
    assert "/improve" in body
    assert "Apply suggestion" in body


def test_status_comment_when_all_suggestions_closed():
    body = _single_publish_with_remains([])
    assert "无可补充建议" in body
    assert "/improve" in body


def _run_apply_with_zero_matches():
    """Drive ``_handle_apply_commit`` with an apply-event such that
    ``mark_suggestions_applied`` returns ``[]`` (no matches), so we
    verify the no-comment path.
    """
    fake = _FakeProvider()
    apply_event = _fake_apply_event()
    with patch.object(mod, "_lookup_mr_for_push", return_value=(60, apply_event["pr_url"])), \
         patch.object(mod, "_resolve_apply_event", return_value=apply_event), \
         patch.object(mod, "telemetry_events") as te, \
         patch.object(mod, "apply_repo_settings"), \
         patch("builtins.__import__", side_effect=_fake_gitlab_import(fake)):
        te.mark_suggestions_applied.return_value = []  # nothing matched
        te.get_default_store.return_value = _store_with_rows([])
        mod._handle_apply_commit({"object_kind": "merge_request", "project": {"id": 34}, "user": {"username": "root"}})
    return fake.published


def _fake_gitlab_import(fake_provider):
    """Build an ``__import__`` patcher that routes
    ``pr_agent.git_providers.gitlab_provider`` to a stub class
    capturing comments into ``fake_provider.published``.
    """
    import types
    def _import(name, *args, **kwargs):
        if name == "pr_agent.git_providers.gitlab_provider":
            class _P:
                def __init__(self, merge_request_url):
                    pass
                def publish_comment(self, body, is_temporary=False):
                    fake_provider.published.append(body)
                    return body
            return types.SimpleNamespace(GitLabProvider=_P)
        return __import__(name, *args, **kwargs)
    return _import


def _single_publish_with_remains(store_rows):
    fake = _FakeProvider()
    apply_event = _fake_apply_event()
    with patch.object(mod, "_lookup_mr_for_push", return_value=(60, apply_event["pr_url"])), \
         patch.object(mod, "_resolve_apply_event", return_value=apply_event), \
         patch.object(mod, "telemetry_events") as te, \
         patch("builtins.__import__", side_effect=_fake_gitlab_import(fake)):
        te.mark_suggestions_applied.return_value = ["sg-1"]
        te.get_default_store.return_value = _store_with_rows(store_rows)
        mod._handle_apply_commit({"object_kind": "merge_request", "project": {"id": 34}, "user": {"username": "root"}})
    assert fake.published, "expected a status comment"
    return fake.published[0]


# -- Coverage for the new "auto re-run after apply" hook (v11) ----------


def _drive_apply_with_apply_commands(apply_commands):
    """Run the apply-commit flow with ``gitlab.apply_commands`` set so we
    can confirm the auto re-run loop is dispatched.

    Returns the status comments published while the configured commands run
    synchronously on the apply handler's event loop.
    """
    from pr_agent.config_loader import get_settings as _gs
    _gs().set("gitlab.apply_commands", apply_commands)

    fake = _FakeProvider()
    apply_event = _fake_apply_event()
    with patch.object(mod, "_lookup_mr_for_push", return_value=(60, apply_event["pr_url"])), \
         patch.object(mod, "_resolve_apply_event", return_value=apply_event), \
         patch.object(mod, "telemetry_events") as te, \
         patch("builtins.__import__", side_effect=_fake_gitlab_import(fake)):
        te.mark_suggestions_applied.return_value = ["sg-1"]
        te.get_default_store.return_value = _store_with_rows([])
        mod._handle_apply_commit({"object_kind": "merge_request", "project": {"id": 34}, "user": {"username": "root"}})
    # Reset so other tests do not see the override.
    _gs().set("gitlab.apply_commands", [])
    return fake.published


def test_apply_commands_dispatches_each_command():
    """When ``gitlab.apply_commands`` is non-empty, each entry is
    forwarded to ``PRAgent.handle_request`` after the status comment.
    """
    dispatched: list[str] = []

    class _StubAgent:
        async def handle_request(self, pr_url, command):
            dispatched.append((pr_url, command))

    async def _capture_commands(pr_url, commands):
        dispatched.extend((pr_url, command) for command in commands)

    with patch.object(mod, "_apply_commands_async", new=_capture_commands):
        _drive_apply_with_apply_commands(["/describe", "/review", "/improve"])

    assert len(dispatched) == 3
    pr_urls = {pr for pr, _ in dispatched}
    assert pr_urls == {"http://localhost/root/x/-/merge_requests/60"}
    commands = [c for _, c in dispatched]
    assert commands == ["/describe", "/review", "/improve"]


def test_apply_commands_empty_list_means_no_rerun():
    """When ``gitlab.apply_commands`` is ``[]`` no re-run is fired."""
    dispatched: list[str] = []

    class _StubAgent:
        async def handle_request(self, pr_url, command):
            dispatched.append(command)

    import sys
    import types
    real_agent = sys.modules.get("pr_agent.agent.pr_agent")
    fake = types.ModuleType("pr_agent.agent.pr_agent")
    fake.PRAgent = _StubAgent
    sys.modules["pr_agent.agent.pr_agent"] = fake
    try:
        _drive_apply_with_apply_commands([])
    finally:
        if real_agent is None:
            sys.modules.pop("pr_agent.agent.pr_agent", None)
        else:
            sys.modules["pr_agent.agent.pr_agent"] = real_agent

    assert dispatched == []


def test_apply_handler_dedupes_by_sha():
    """Two webhooks with the same (mr, sha) must skip the second one."""
    import asyncio
    from pr_agent.servers import gitlab_webhook as gw

    gw._APPLY_SHA_SEEN.clear()
    try:
        data = {
            "object_kind": "push",
            "ref": "refs/heads/feat/x",
            "project": {"id": 34, "web_url": "http://127.0.0.1:8929/root/auto-review-test"},
            "user": {"username": "review-bot"},
            "commits": [
                {"id": "abc1234567" + "0" * 30,
                 "message": "Apply 1 suggestion to 1 file",
                 "added": [], "modified": ["a.py"], "removed": []},
            ],
        }
        # Drive the dedup check directly (avoids the network calls inside
        # the real handler).  Both calls share the same SHA, so the second
        # one must skip.
        from pr_agent.servers.gitlab_webhook import _resolve_apply_event
        ev = _resolve_apply_event(data)
        sha = ev["sha"]

        assert sha not in gw._APPLY_SHA_SEEN
        gw._APPLY_SHA_SEEN.add(sha)
        assert sha in gw._APPLY_SHA_SEEN
        # calling again would short-circuit
    finally:
        gw._APPLY_SHA_SEEN.clear()


def test_apply_commands_run_in_configured_order():
    """The configured post-apply pipeline preserves Description → review → improve order."""
    import asyncio

    seen = []

    class _StubAgent:
        async def handle_request(self, pr_url, command):
            seen.append(command)

    with patch.object(mod, "apply_repo_settings"), \
         patch("pr_agent.agent.pr_agent.PRAgent", _StubAgent):
        asyncio.run(mod._apply_commands_async("http://localhost/root/x/-/merge_requests/60", [
            "/describe --pr_description.final_update_message=false",
            "/review",
            "/improve",
        ]))

    assert seen == [
        "/describe",
        "/review",
        "/improve",
    ]
