"""Unit tests for the apply-pipeline loop guard.

Before the guard, a description-only ``merge_request update`` webhook
(carries ``last_commit.message == "Apply N suggestion(s)"`` because the
last push was the Apply commit) would match ``_resolve_apply_event``,
re-run ``/describe`` via the apply pipeline, which rewrites the MR
description, which fires yet another ``merge_request update`` webhook
with the same ``last_commit.message`` — infinite describe loop on the
MR timeline.

The guard requires the update to be push-driven (``object_kind=push``
or ``oldrev`` present) before chaining apply_commands. Description-only
edits only update telemetry and return.
"""
import pytest

from pr_agent.servers.gitlab_webhook import _resolve_apply_event


def _should_chain_apply_commands(data):
    """Mirror the inline guard in
    ``pr_agent.servers.gitlab_webhook.gitlab_webhook``."""
    if _resolve_apply_event(data) is None:
        return False
    return (
        data.get("object_kind") == "push"
        or bool((data.get("object_attributes") or {}).get("oldrev"))
    )


# --- Push hook apply commit: should chain (the original trigger) ---
def test_push_hook_with_apply_commit_chains_apply_commands():
    data = {
        "object_kind": "push",
        "project": {"id": 34},
        "user": {"username": "root"},
        "ref": "refs/heads/codex/foo",
        "commits": [{
            "id": "abc",
            "message": "Apply 1 suggestion(s) to 1 file(s)",
            "added": [], "modified": ["x.py"], "removed": [],
        }],
    }
    assert _should_chain_apply_commands(data) is True


# --- MR update webhook with oldrev (push-driven): should chain ---
def test_mr_update_with_oldrev_and_apply_commit_chains():
    data = {
        "object_kind": "merge_request",
        "project": {"id": 34, "web_url": "http://127.0.0.1:8929/r"},
        "user": {"username": "root"},
        "object_attributes": {
            "iid": 66,
            "action": "update",
            "oldrev": "3ebcf4f",  # push-triggered
            "url": "http://127.0.0.1:8929/r/-/merge_requests/66",
            "last_commit": {
                "id": "fab49390",
                "message": "Apply 1 suggestion(s) to 1 file(s)",
            },
        },
    }
    assert _should_chain_apply_commands(data) is True


# --- MR update webhook WITHOUT oldrev (description-only): must NOT chain ---
def test_mr_update_without_oldrev_does_not_chain():
    """Bot's /describe rewrites the description, fires this webhook; the
    last_commit message still mentions 'Apply N suggestion(s)' but the
    payload has no oldrev (no new push). Must short-circuit."""
    data = {
        "object_kind": "merge_request",
        "project": {"id": 34, "web_url": "http://127.0.0.1:8929/r"},
        "user": {"username": "review-bot"},  # could be anyone, including bot
        "object_attributes": {
            "iid": 66,
            "action": "update",
            # NO oldrev — description-only or label-only edit
            "url": "http://127.0.0.1:8929/r/-/merge_requests/66",
            "last_commit": {
                "id": "fab49390",
                "message": "Apply 1 suggestion(s) to 1 file(s)",
            },
        },
    }
    assert _should_chain_apply_commands(data) is False


# --- Regular MR update without apply commit: _resolve_apply_event returns None ---
def test_mr_update_without_apply_commit_does_not_chain():
    data = {
        "object_kind": "merge_request",
        "project": {"id": 34, "web_url": "http://127.0.0.1:8929/r"},
        "user": {"username": "root"},
        "object_attributes": {
            "iid": 66,
            "action": "update",
            "url": "http://127.0.0.1:8929/r/-/merge_requests/66",
            "last_commit": {"id": "x", "message": "fix: typo"},
        },
    }
    assert _should_chain_apply_commands(data) is False


# --- Push hook without apply commit: never chains ---
def test_push_hook_without_apply_commit_does_not_chain():
    data = {
        "object_kind": "push",
        "project": {"id": 34},
        "user": {"username": "root"},
        "ref": "refs/heads/main",
        "commits": [{"id": "x", "message": "fix: typo", "added": [], "modified": ["x.py"], "removed": []}],
    }
    assert _should_chain_apply_commands(data) is False


# --- /improve posts a suggestion thread; the resulting update webhook
#     fires once but must not re-enter apply_commands. ---
def test_bot_improve_thread_update_does_not_chain():
    data = {
        "object_kind": "merge_request",
        "project": {"id": 34, "web_url": "http://127.0.0.1:8929/r"},
        "user": {"username": "review-bot"},
        "object_attributes": {
            "iid": 66,
            "action": "update",
            "url": "http://127.0.0.1:8929/r/-/merge_requests/66",
            "last_commit": {
                "id": "fab49390",
                "message": "Apply 1 suggestion(s) to 1 file(s)",
            },
        },
    }
    # No oldrev means /improve's note-driven update gets blocked from re-chaining.
    assert _should_chain_apply_commands(data) is False
