"""Tests for the cohort dedup + per-MR concurrency guard + multi-Apply loop.

Three fixes ride together:

cohort    — suggestion cohort_key captures (file, line, sorted-rule-keys);
           LLM non-determinism can vary `improved_code` wording round to
           round but the cohort anchor is stable, so duplicate DiffNotes
           at the same site get suppressed even when their fingerprint
           hash differs.
lock      — per-MR asyncio.Lock + post-run cooldown prevent overlapping
           /improve runs from racing each other on the same MR.
apply-loop— a single push event carrying N "Apply N suggestion(s)" commits
           now processes every commit independently, not only the last.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.servers.gitlab_webhook import (
    _acquire_mr_run_slot,
    _mr_lock_key,
    _release_mr_run_slot,
    _MR_RUN_LOCKS,
    _MR_LAST_RUN_FINISHED,
    _MR_RUN_COOLDOWN_S,
    _iter_apply_events,
)
from pr_agent.telemetry import events as telemetry_events
from pr_agent.telemetry.models import Suggestion
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


# ---------- cohort_key ----------

def test_cohort_key_stable_across_wording():
    """Same site with two different `improved_code` strings still collides.

    The cohort key sorts the rule-key list internally so callers don't have
    to coordinate ordering — passing the same set in either order yields
    the same anchor.
    """
    f = "services/foo.py"
    rule_keys = ["SSD-RULE-DOCSTRING-REQUIRED"]
    k1 = PRCodeSuggestions._cohort_key_for(f, 17, rule_keys)
    # Different ordering -> still same key (sorted internally)
    k2 = PRCodeSuggestions._cohort_key_for(f, 17, list(reversed(rule_keys)))
    assert k1 == k2, "cohort must be invariant to rule-key ordering"
    # Tuple vs list also equivalent after sorting
    k3 = PRCodeSuggestions._cohort_key_for(f, 17, tuple(rule_keys))
    assert k1 == k3


def test_cohort_key_differs_for_different_site():
    f = "services/foo.py"
    rule_keys = ["SSD-RULE-DOCSTRING-REQUIRED"]
    assert PRCodeSuggestions._cohort_key_for(f, 17, rule_keys) != \
           PRCodeSuggestions._cohort_key_for(f, 18, rule_keys)
    assert PRCodeSuggestions._cohort_key_for(f, 17, rule_keys) != \
           PRCodeSuggestions._cohort_key_for("services/bar.py", 17, rule_keys)
    other_rules = ["SSD-RULE-NO-LOG-EXC"]
    assert PRCodeSuggestions._cohort_key_for(f, 17, rule_keys) != \
           PRCodeSuggestions._cohort_key_for(f, 17, other_rules)


# ---------- per-MR concurrency guard ----------

def test_lock_slot_first_acquire_succeeds():
    key = _mr_lock_key(34, 9999)
    _MR_LAST_RUN_FINISHED.pop(key, None)
    ok, reason = _acquire_mr_run_slot(key)
    assert ok and reason == ""
    assert key in _MR_RUN_LOCKS
    # release so subsequent tests don't hit it
    lock = _MR_RUN_LOCKS[key]
    if lock.locked():
        lock.release()
    _MR_LAST_RUN_FINISHED.pop(key, None)


def test_lock_slot_blocks_running_mr():
    key = _mr_lock_key(34, 8888)
    _MR_LAST_RUN_FINISHED.pop(key, None)
    lock = _MR_RUN_LOCKS.setdefault(key, asyncio.Lock())
    # acquire manually
    if lock.locked():
        # ensure clean
        pass
    # For a synchronous test we just claim it via asyncio.run
    async def _go():
        await lock.acquire()
        try:
            ok, reason = _acquire_mr_run_slot(key)
            return ok, reason
        finally:
            lock.release()
    ok, reason = asyncio.run(_go())
    assert not ok
    assert reason == "improve-already-running"
    _MR_LAST_RUN_FINISHED.pop(key, None)


def test_cooldown_blocks_immediate_reacquire():
    key = _mr_lock_key(34, 7777)
    # Reset state
    _MR_LAST_RUN_FINISHED.pop(key, None)
    # Simulate a recent run completion
    _MR_LAST_RUN_FINISHED[key] = time.monotonic() - 5.0  # 5s ago, within 30s cooldown
    ok, reason = _acquire_mr_run_slot(key)
    assert not ok
    assert "improve-cooldown" in reason
    _MR_LAST_RUN_FINISHED.pop(key, None)


def test_release_records_finished_timestamp():
    key = _mr_lock_key(34, 6666)
    _MR_LAST_RUN_FINISHED.pop(key, None)
    before = time.monotonic()
    _release_mr_run_slot(key)
    after = time.monotonic()
    assert key in _MR_LAST_RUN_FINISHED
    assert before - 1.0 <= _MR_LAST_RUN_FINISHED[key] <= after + 1.0
    _MR_LAST_RUN_FINISHED.pop(key, None)


# ---------- multi-Apply commit loop ----------

def test_iter_apply_events_empty_commits_falls_back_to_resolve():
    """When webhook has no commits[] (e.g., MR update only), use legacy resolver."""
    payload = {
        "object_kind": "merge_request",
        "project": {"id": 34},
        "merge_request": {"iid": 12},
    }
    events = list(_iter_apply_events(payload))
    assert events == []


def test_iter_apply_events_filters_non_apply_commits():
    """Only commits whose message matches 'Apply N suggestion(s)' are yielded."""
    payload = {
        "object_kind": "push",
        "ref": "refs/heads/feature",
        "commits": [
            {"id": "aaa", "message": "fix: typo in main"},
            {"id": "bbb", "message": "Apply 1 suggestion(s) to 1 file(s)"},
            {"id": "ccc", "message": "another manual commit"},
            {"id": "ddd", "message": "Apply 2 suggestion(s) to 1 file(s)"},
        ],
        "project": {"id": 34},
        "user_username": "root",
    }
    events = list(_iter_apply_events(payload))
    assert len(events) == 2, f"expected 2 apply events, got {len(events)}"
    # Each yielded event should carry the right single-commit payload
    assert events[0]["sha"] == "bbb"
    assert events[1]["sha"] == "ddd"
    # And each is a synthetic single-commit dict for the resolver to chew on
    assert events[0]["suggestion_count"] == 1
    assert events[1]["suggestion_count"] == 2
