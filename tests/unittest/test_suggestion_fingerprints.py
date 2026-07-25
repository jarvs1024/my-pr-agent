import sqlite3
from types import SimpleNamespace

from pr_agent.config_loader import get_settings  # noqa: F401
from pr_agent.telemetry import models
from pr_agent.telemetry.store import TelemetryStore
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


class _Provider:
    id_mr = 97
    id_project = 34
    pr_url = "http://gitlab/root/repo/-/merge_requests/97"


def _candidate(fingerprint):
    return {
        "relevant_file": "src/x.py",
        "relevant_lines_start": 10,
        "relevant_lines_end": 10,
        "fingerprint": fingerprint,
        "body": "fix x",
        "original_suggestion": {},
    }


def test_fingerprint_ignores_line_endings_and_trailing_spaces():
    first = PRCodeSuggestions._suggestion_fingerprint(
        "src/x.py", "def f():\r\n    return 1  \r\n", "def f():\r\n    return 2\r\n"
    )
    second = PRCodeSuggestions._suggestion_fingerprint(
        "src/x.py", "def f():\n    return 1\n", "def f():\n    return 2\n"
    )

    assert first == second


def test_fingerprint_preserves_leading_indentation():
    nested = PRCodeSuggestions._suggestion_fingerprint(
        "src/x.py", "    return 1", "    return 2"
    )
    top_level = PRCodeSuggestions._suggestion_fingerprint(
        "src/x.py", "return 1", "return 2"
    )

    assert nested != top_level


def test_posted_head_sha_uses_sentinel_when_provider_head_is_unavailable():
    provider = SimpleNamespace(pr=SimpleNamespace(sha=None, diff_refs={}))

    assert PRCodeSuggestions._get_posted_head_sha(provider) == "__unavailable__"


def test_posted_head_sha_prefers_provider_head():
    provider = SimpleNamespace(pr=SimpleNamespace(sha="head-123", diff_refs={"head_sha": "head-456"}))

    assert PRCodeSuggestions._get_posted_head_sha(provider) == "head-123"


def test_store_migrates_legacy_suggestions_table(tmp_path):
    path = tmp_path / "telemetry.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE suggestions ("
        "suggestion_id TEXT PRIMARY KEY, mr_id INTEGER, project_id INTEGER, "
        "file TEXT, line INTEGER, label TEXT, importance INTEGER, "
        "one_sentence_summary TEXT, rule_keys TEXT, score INTEGER, posted_at TEXT, "
        "state TEXT, applied_at TEXT, dismissed_at TEXT, dismissed_by TEXT, "
        "dismissed_reason TEXT, note_id TEXT)"
    )
    connection.commit()
    connection.close()

    store = TelemetryStore("sqlite", sqlite_path=str(path))
    columns = {row[1] for row in store._db.execute("PRAGMA table_info(suggestions)")}

    assert {"fingerprint", "line_end", "posted_head_sha"}.issubset(columns)


def test_internal_fingerprint_fields_are_not_exposed_by_public_list(tmp_path):
    store = TelemetryStore("sqlite", sqlite_path=str(tmp_path / "telemetry.db"))
    store.record_suggestion(models.Suggestion(
        suggestion_id="sug-fingerprint",
        mr_id=97,
        project_id=34,
        file="src/x.py",
        line=10,
        line_end=11,
        fingerprint="abc123",
        posted_head_sha="head-123",
        one_sentence_summary="fix x",
        note_id="discussion-123",
        dismissed_reason="not applicable",
    ))

    public = store.list_suggestions(97, 34, attach_severity=False)[0]
    internal = store.list_suggestion_fingerprints(97, 34)[0]

    assert "fingerprint" not in public
    assert "line_end" not in public
    assert "posted_head_sha" not in public
    assert public["note_id"] == "discussion-123"
    assert public["dismissed_reason"] == "not applicable"
    assert internal["fingerprint"] == "abc123"
    assert internal["line_end"] == 11

    by_note = store.get_suggestion_by_note_id("discussion-123")
    open_records = store.list_open_suggestion_records(97, 34)
    assert by_note["posted_head_sha"] == "head-123"
    assert by_note["line_end"] == 11
    assert by_note["fingerprint"] == "abc123"
    assert open_records[0]["posted_head_sha"] == "head-123"


def test_open_suggestion_with_same_fingerprint_is_suppressed(monkeypatch):
    from pr_agent.telemetry import events as telemetry_events

    store = type("Store", (), {})()
    store.list_suggestions = lambda *args, **kwargs: []
    store.list_suggestion_fingerprints = lambda *args, **kwargs: [{
        "file": "src/x.py",
        "line": 10,
        "line_end": 10,
        "state": "open",
        "fingerprint": "same-patch",
    }]
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = _Provider()

    assert tool._suppress_resolved_suggestions([_candidate("same-patch")]) == []


def test_open_suggestion_on_same_line_with_different_fingerprint_is_kept(monkeypatch):
    from pr_agent.telemetry import events as telemetry_events

    store = type("Store", (), {})()
    store.list_suggestions = lambda *args, **kwargs: []
    store.list_suggestion_fingerprints = lambda *args, **kwargs: [{
        "file": "src/x.py",
        "line": 10,
        "line_end": 10,
        "state": "open",
        "fingerprint": "other-patch",
    }]
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = _Provider()
    candidate = _candidate("new-patch")

    assert tool._suppress_resolved_suggestions([candidate]) == [candidate]
