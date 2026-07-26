"""Tests for the dedup + race condition fixes:

fix1: update_suggestion_state added allowed_from guard so late events
      can't clobber a more recent transition.
fix2: /dismiss mirrors /adopt's state guard, skipping when the suggestion
      is already in a terminal state.
fix3: cs dict now carries a content fingerprint so subsequent /improve
      rounds suppress duplicate suggestions via fingerprint-existing.
"""
import os
import sqlite3
import tempfile
from types import SimpleNamespace

import pytest

from pr_agent.config_loader import get_settings
from pr_agent.telemetry import events as telemetry_events
from pr_agent.telemetry.models import Suggestion
from pr_agent.telemetry.store import TelemetryStore


@pytest.fixture
def tmp_store(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = TelemetryStore(backend="sqlite", sqlite_path=path)
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)
    yield store
    os.unlink(path)


def _seed(store, *, state="open", fingerprint=None):
    """Insert a minimal Suggestion row at the right mr_id."""
    sug = Suggestion(
        mr_id=99,
        project_id=34,
        file="src/x.py",
        line=10,
        label="security",
        importance=8,
        one_sentence_summary="test",
        rule_keys=["SSD-RULE-X"],
        score=8,
        note_id="note-abc",
        fingerprint=fingerprint,
        posted_head_sha="posted-sha",
    )
    store.record_suggestion(sug)
    # Force the initial state for the test
    store.update_suggestion_state(
        sug.suggestion_id, state, allowed_from=("open", "applied", "dismissed", "superseded")
    )
    return sug


# ---------------------------------------------------------------------------
# fix1 — update_suggestion_state allowed_from guard
# ---------------------------------------------------------------------------

def test_update_suggestion_state_with_allowed_from_succeeds(tmp_store):
    sug = _seed(tmp_store, state="open")
    rows = tmp_store.update_suggestion_state(
        sug.suggestion_id, "applied", allowed_from=("open",)
    )
    assert rows == 1
    cur = tmp_store._db.execute(
        "SELECT state FROM suggestions WHERE suggestion_id=?", (sug.suggestion_id,)
    )
    assert cur.fetchone()[0] == "applied"


def test_update_suggestion_state_with_allowed_from_rejects(tmp_store):
    sug = _seed(tmp_store, state="superseded")
    rows = tmp_store.update_suggestion_state(
        sug.suggestion_id, "dismissed", allowed_from=("open", "applied")
    )
    assert rows == 0
    cur = tmp_store._db.execute(
        "SELECT state FROM suggestions WHERE suggestion_id=?", (sug.suggestion_id,)
    )
    # State preserved — late /dismiss cannot clobber the already-superseded record.
    assert cur.fetchone()[0] == "superseded"


def test_update_suggestion_state_without_allowed_from_still_works(tmp_store):
    """Backwards compatibility: no allowed_from → unconditional update."""
    sug = _seed(tmp_store, state="open")
    rows = tmp_store.update_suggestion_state(sug.suggestion_id, "dismissed")
    assert rows == 1
    cur = tmp_store._db.execute(
        "SELECT state FROM suggestions WHERE suggestion_id=?", (sug.suggestion_id,)
    )
    assert cur.fetchone()[0] == "dismissed"


# ---------------------------------------------------------------------------
# fix1 — mark_suggestion_dismissed / superseded / adopted actually enforce
# ---------------------------------------------------------------------------

def test_mark_suggestion_dismissed_refuses_to_overwrite_superseded(tmp_store):
    sug = _seed(tmp_store, state="superseded")
    telemetry_events.mark_suggestion_dismissed(sug.suggestion_id, actor="root", reason="late")
    cur = tmp_store._db.execute(
        "SELECT state, dismissed_by, dismissed_reason FROM suggestions WHERE suggestion_id=?",
        (sug.suggestion_id,),
    )
    state, dismissed_by, dismissed_reason = cur.fetchone()
    # Stays superseded; the dismissed_* fields are NOT touched either.
    assert state == "superseded"
    assert dismissed_by is None
    assert dismissed_reason is None


def test_mark_suggestion_superseded_refuses_to_overwrite_dismissed(tmp_store):
    sug = _seed(tmp_store, state="dismissed")
    telemetry_events.mark_suggestion_superseded(sug.suggestion_id)
    cur = tmp_store._db.execute(
        "SELECT state FROM suggestions WHERE suggestion_id=?", (sug.suggestion_id,)
    )
    assert cur.fetchone()[0] == "dismissed"


def test_mark_suggestion_superseded_allows_open_and_applied(tmp_store):
    for from_state in ("open", "applied"):
        sug = _seed(tmp_store, state=from_state)
        telemetry_events.mark_suggestion_superseded(sug.suggestion_id)
        cur = tmp_store._db.execute(
            "SELECT state FROM suggestions WHERE suggestion_id=?", (sug.suggestion_id,)
        )
        assert cur.fetchone()[0] == "superseded", f"failed from {from_state}"


def test_mark_suggestion_adopted_returns_false_when_not_open(tmp_store):
    sug = _seed(tmp_store, state="superseded")
    result = telemetry_events.mark_suggestion_adopted(
        sug.suggestion_id, actor="root", reason="already superseded"
    )
    assert result is False
    cur = tmp_store._db.execute(
        "SELECT state FROM suggestions WHERE suggestion_id=?", (sug.suggestion_id,)
    )
    assert cur.fetchone()[0] == "superseded"
    # adopted_implicitly action must NOT be recorded for a no-op transition.
    cur = tmp_store._db.execute(
        "SELECT COUNT(*) FROM action_events WHERE suggestion_id=?", (sug.suggestion_id,)
    )
    assert cur.fetchone()[0] == 0


def test_mark_suggestion_adopted_returns_true_when_open(tmp_store):
    sug = _seed(tmp_store, state="open")
    result = telemetry_events.mark_suggestion_adopted(sug.suggestion_id, actor="root", reason="manual")
    assert result is True
    cur = tmp_store._db.execute(
        "SELECT state FROM suggestions WHERE suggestion_id=?", (sug.suggestion_id,)
    )
    assert cur.fetchone()[0] == "applied"
    cur = tmp_store._db.execute(
        "SELECT action, actor FROM action_events WHERE suggestion_id=?", (sug.suggestion_id,)
    )
    row = cur.fetchone()
    assert row == ("adopted_implicitly", "root")


# ---------------------------------------------------------------------------
# fix3 — fingerprint is actually set on emitted suggestions
# ---------------------------------------------------------------------------

def test_suggestion_fingerprint_is_recorded(tmp_store):
    """The new code path computes cs['fingerprint'] and threads it into
    telemetry_events.emit_suggestion. Verify emit_suggestion records it."""
    telemetry_events.emit_suggestion(
        mr_id=99,
        project_id=34,
        file="src/x.py",
        line=10,
        label="security",
        importance=8,
        one_sentence_summary="fix X",
        rule_keys=["SSD-RULE-X"],
        score=8,
        note_id="note-fp",
        fingerprint="abc123def456",
        posted_head_sha="posted-sha",
    )
    cur = tmp_store._db.execute(
        "SELECT fingerprint FROM suggestions WHERE note_id=?", ("note-fp",)
    )
    assert cur.fetchone()[0] == "abc123def456"


# ---------------------------------------------------------------------------
# fix3 — same patch across two /improve rounds suppresses the second
# ---------------------------------------------------------------------------
