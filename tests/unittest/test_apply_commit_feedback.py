"""Unit tests for ``_suppress_resolved_suggestions`` in
``pr_agent.tools.pr_code_suggestions``.

When /improve fires after an Apply or /dismiss (e.g. via the
``gitlab.apply_commands`` re-run loop, or a manual re-invocation), the
LLM has no memory of its prior emissions and tends to re-issue the same
finding on the next (file, line). GitLab then renders the duplicate
DiffNote as "cannot apply" because the lines have moved — pure noise
for the reviewer.

Filter out any new suggestion whose (file, line) — or any line within
±3 to tolerate small re-anchoring — has already been resolved
(state in {applied, dismissed}) in the telemetry store before
publishing. Dismissed suggestions with the same rule key use a wider
configurable window so unrelated edits do not make them reappear.
"""

from __future__ import annotations

import pytest


def _make_provider(mr_id=99, project_id=1, pr_url="http://x/root/x/-/merge_requests/99"):
    return type("GP", (), {
        "id_mr": mr_id,
        "id_project": project_id,
        "pr_url": pr_url,
    })()


def _make_suggestions_store(rows):
    """Return a fake ``default_store`` whose ``list_suggestions`` returns
    the given rows."""
    store = type("FakeStore", (), {})()
    def list_suggestions(mr_id, project_id=None, *, attach_severity=True, pr_url=None):
        return list(rows)
    store.list_suggestions = list_suggestions
    return store


def test_suppress_resolved_drops_exact_and_window_hits(monkeypatch):
    """Suggestions on resolved (file, line) — exact and within ±3 —
    are dropped; unrelated ones pass through."""
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    from pr_agent.telemetry import events as telemetry_events

    store = _make_suggestions_store([
        {"file": "shipment.py", "line": 20, "state": "applied"},
        {"file": "shipment.py", "line": 30, "state": "dismissed"},
    ])
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)

    p = PRCodeSuggestions.__new__(PRCodeSuggestions)
    p.git_provider = _make_provider()

    code_suggestions = [
        {"relevant_file": "shipment.py", "relevant_lines_start": 20,  # already applied
         "body": "**Suggestion:** a"},
        {"relevant_file": "shipment.py", "relevant_lines_start": 30,  # dismissed
         "body": "**Suggestion:** b"},
        {"relevant_file": "shipment.py", "relevant_lines_start": 22,  # within ±3 of applied
         "body": "**Suggestion:** c"},
        {"relevant_file": "shipment.py", "relevant_lines_start": 50,  # NEW (far from any)
         "body": "**Suggestion:** new"},
        {"relevant_file": "other.py",    "relevant_lines_start": 20,  # NEW (different file)
         "body": "**Suggestion:** new-file"},
    ]
    kept = p._suppress_resolved_suggestions(code_suggestions)
    surviving = [cs["relevant_lines_start"] for cs in kept]
    assert surviving == [50, 20], surviving


def test_suppress_resolved_no_resolved_passes_through(monkeypatch):
    """When the telemetry store reports no resolved suggestions, all
    code_suggestions pass through unchanged."""
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    from pr_agent.telemetry import events as telemetry_events

    store = _make_suggestions_store([])
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)

    p = PRCodeSuggestions.__new__(PRCodeSuggestions)
    p.git_provider = _make_provider()

    cs = [{"relevant_file": "a.py", "relevant_lines_start": 1, "body": "x"}]
    assert p._suppress_resolved_suggestions(cs) == cs


def test_suppress_resolved_handles_missing_mr_id(monkeypatch):
    """When the git_provider cannot supply an mr_id, do not crash and
    return the input unchanged without querying telemetry."""
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    from pr_agent.telemetry import events as telemetry_events

    called = {"n": 0}
    store = type("FakeStore", (), {})()
    def list_suggestions(*a, **k):
        called["n"] += 1
        return []
    store.list_suggestions = list_suggestions
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)

    p = PRCodeSuggestions.__new__(PRCodeSuggestions)
    p.git_provider = type("GP", (), {})()  # no mr info

    cs = [{"relevant_file": "a.py", "relevant_lines_start": 1, "body": "x"}]
    assert p._suppress_resolved_suggestions(cs) == cs
    assert called["n"] == 0  # we should not have queried telemetry


def test_suppress_resolved_empty_input(monkeypatch):
    """Empty input short-circuits without touching telemetry."""
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    from pr_agent.telemetry import events as telemetry_events

    called = {"n": 0}
    store = type("FakeStore", (), {})()
    def list_suggestions(*a, **k):
        called["n"] += 1
        return []
    store.list_suggestions = list_suggestions
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)

    p = PRCodeSuggestions.__new__(PRCodeSuggestions)
    p.git_provider = _make_provider()

    assert p._suppress_resolved_suggestions([]) == []
    assert called["n"] == 0


def test_suppress_resolved_handles_telemetry_failure(monkeypatch):
    """If the telemetry lookup itself raises, fall back to passing the
    input through (do not crash the publish path)."""
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    from pr_agent.telemetry import events as telemetry_events

    store = type("FakeStore", (), {})()
    def list_suggestions(*a, **k):
        raise RuntimeError("db locked")
    store.list_suggestions = list_suggestions
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)

    p = PRCodeSuggestions.__new__(PRCodeSuggestions)
    p.git_provider = _make_provider()

    cs = [{"relevant_file": "a.py", "relevant_lines_start": 1, "body": "x"}]
    assert p._suppress_resolved_suggestions(cs) == cs


def test_suppress_resolved_window_is_three_lines(monkeypatch):
    """±3 line window: lines 4 apart should NOT be suppressed (outside
    the window). Edge: line exactly 3 away should be suppressed."""
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    from pr_agent.telemetry import events as telemetry_events

    store = _make_suggestions_store([
        {"file": "f.py", "line": 10, "state": "applied"},
    ])
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)

    p = PRCodeSuggestions.__new__(PRCodeSuggestions)
    p.git_provider = _make_provider()

    cs = [
        {"relevant_file": "f.py", "relevant_lines_start": 13, "body": "x"},  # +3 in window
        {"relevant_file": "f.py", "relevant_lines_start": 14, "body": "x"},  # +4 outside window
        {"relevant_file": "f.py", "relevant_lines_start": 7,  "body": "x"},  # -3 in window
        {"relevant_file": "f.py", "relevant_lines_start": 6,  "body": "x"},  # -4 outside window
    ]
    kept = p._suppress_resolved_suggestions(cs)
    surviving = [c["relevant_lines_start"] for c in kept]
    assert surviving == [14, 6], surviving


def test_suppress_resolved_matches_same_rule_after_larger_line_drift(monkeypatch):
    """A dismissed rule finding stays suppressed when unrelated edits move it
    beyond the generic ±3-line window; nearby findings for another rule remain."""
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    from pr_agent.telemetry import events as telemetry_events

    store = _make_suggestions_store([
        {
            "file": "review_fixture_v19.py",
            "line": 9,
            "state": "dismissed",
            "rule_keys": ["ZLG-RULE-NO-LOG-EXC"],
        },
    ])
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)

    suggestions = PRCodeSuggestions.__new__(PRCodeSuggestions)
    suggestions.git_provider = _make_provider()

    code_suggestions = [
        {
            "relevant_file": "review_fixture_v19.py",
            "relevant_lines_start": 14,
            "body": "违反 `ZLG-RULE-NO-LOG-EXC`: except Exception: pass",
        },
        {
            "relevant_file": "review_fixture_v19.py",
            "relevant_lines_start": 14,
            "body": "违反 `ZLG-RULE-DOCSTRING-REQUIRED`: missing docstring",
        },
        {
            "relevant_file": "review_fixture_v19.py",
            "relevant_lines_start": 20,
            "body": "违反 `ZLG-RULE-NO-LOG-EXC`: another function",
        },
    ]

    kept = suggestions._suppress_resolved_suggestions(code_suggestions)

    assert [suggestion["body"] for suggestion in kept] == [
        "违反 `ZLG-RULE-DOCSTRING-REQUIRED`: missing docstring",
        "违反 `ZLG-RULE-NO-LOG-EXC`: another function",
    ]
