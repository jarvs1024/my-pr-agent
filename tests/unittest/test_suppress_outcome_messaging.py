"""Unit tests for the 'all-suppressed' messaging path in
``push_inline_code_suggestions``.

When /improve emits N suggestions but ``_suppress_resolved_suggestions``
drops every one of them (because their (file, line) is within the ±3 line
window of a previously applied/dismissed suggestion), the inline-mode
caller used to silently ``return`` — leaving the reviewer with an empty
timeline and the false impression that the LLM found nothing.

The fix: ``push_inline_code_suggestions`` populates
``self._last_suggestion_outcome`` with the LLM-emitted / dedup-dropped /
suppressed / kept counts and the suppressed (file, line, label) tuples,
so the caller can post a "本次 /improve 生成 N 条建议，已自动跳过: ..."
note instead of pretending the LLM was silent.
"""

from __future__ import annotations

import pytest

from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


class _FakeGitProvider:
    def __init__(self):
        self.published = []
        self.diff_files = []
        self.id_mr = 1
        self.id_project = 1
        self.pr = None

    def publish_comment(self, body):
        self.published.append(body)

    def edit_comment(self, note_id, body):
        self.published.append(body)

    def get_diff_files(self):
        return self.diff_files

    def get_files(self):
        return ["x.py"]

    def remove_initial_comment(self):
        return None

    def publish_code_suggestions(self, suggestions):
        self.published.extend(suggestions)
        return True


def _make_tool():
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = _FakeGitProvider()
    tool.progress_response = None
    return tool


# ---------------------------------------------------------------------------
# _last_suggestion_outcome is populated correctly
# ---------------------------------------------------------------------------


def test_outcome_recorded_when_all_suppressed(monkeypatch):
    from pr_agent.telemetry import events as telemetry_events

    store = type("Store", (), {})()
    store.list_suggestions = lambda *a, **kw: [
        {"file": "x.py", "line": 20, "state": "applied"},
    ]
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)

    tool = _make_tool()
    data = {"code_suggestions": [
        {"relevant_file": "x.py", "relevant_lines_start": 21, "relevant_lines_end": 22,
         "suggestion_content": "x", "label": "best practice", "improved_code": "x", "score": 8},
    ]}

    import asyncio
    asyncio.run(tool.push_inline_code_suggestions(data))

    out = getattr(tool, "_last_suggestion_outcome", None)
    assert out is not None
    assert out["llm_emitted"] == 1
    assert out["kept"] == 0
    assert out["suppressed_count"] == 1
    assert out["dedup_dropped"] == 0
    assert len(out["suppressed_lines"]) == 1
    assert out["suppressed_lines"][0][0] == "x.py"
    assert out["suppressed_lines"][0][1] == 21


def test_outcome_recorded_when_kept(monkeypatch):
    from pr_agent.telemetry import events as telemetry_events

    store = type("Store", (), {})()
    store.list_suggestions = lambda *a, **kw: []  # no resolved state
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)

    tool = _make_tool()
    data = {"code_suggestions": [
        {"relevant_file": "x.py", "relevant_lines_start": 1, "relevant_lines_end": 2,
         "suggestion_content": "x", "label": "best practice", "improved_code": "x", "score": 8},
    ]}

    import asyncio
    asyncio.run(tool.push_inline_code_suggestions(data))

    out = tool._last_suggestion_outcome
    assert out["llm_emitted"] == 1
    assert out["kept"] == 1
    assert out["suppressed_count"] == 0


def test_outcome_recorded_dedup_drops(monkeypatch):
    """Same-round dedup is reflected in the outcome dict."""
    from pr_agent.telemetry import events as telemetry_events

    store = type("Store", (), {})()
    store.list_suggestions = lambda *a, **kw: []
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)

    tool = _make_tool()
    # Two suggestions for same (file, line, label) → dedup drops one
    data = {"code_suggestions": [
        {"relevant_file": "x.py", "relevant_lines_start": 1, "relevant_lines_end": 2,
         "suggestion_content": "a", "label": "best practice", "improved_code": "x", "score": 8},
        {"relevant_file": "x.py", "relevant_lines_start": 1, "relevant_lines_end": 2,
         "suggestion_content": "b", "label": "best practice", "improved_code": "y", "score": 8},
    ]}

    import asyncio
    asyncio.run(tool.push_inline_code_suggestions(data))

    out = tool._last_suggestion_outcome
    assert out["llm_emitted"] == 2
    assert out["dedup_dropped"] == 1
    assert out["kept"] == 1
    assert out["suppressed_count"] == 0


# ---------------------------------------------------------------------------
# "No suggestions" branch (LLM emitted 0)
# ---------------------------------------------------------------------------


def test_no_suggestions_published_when_llm_empty():
    """LLM returned 0 → caller publishes the standard 'no suggestions' body."""
    tool = _make_tool()
    data = {"code_suggestions": []}

    import asyncio
    asyncio.run(tool.push_inline_code_suggestions(data))

    assert len(tool.git_provider.published) == 1
    assert "No suggestions found" in tool.git_provider.published[0]


@pytest.mark.asyncio
async def test_run_publishes_notice_when_all_suggestions_are_suppressed(monkeypatch):
    from pr_agent.config_loader import get_settings
    from pr_agent.telemetry import events as telemetry_events
    from pr_agent.tools import pr_code_suggestions as suggestions_module
    from tests.unittest._settings_helpers import restore_settings, snapshot_settings

    settings_paths = (
        "config.publish_output",
        "config.publish_output_progress",
        "pr_code_suggestions.commitable_code_suggestions",
    )
    snapshot = snapshot_settings(settings_paths)
    settings = get_settings()
    settings.set("config.publish_output", True)
    settings.set("config.publish_output_progress", False)
    settings.set("pr_code_suggestions.commitable_code_suggestions", True)

    data = {"code_suggestions": [
        {"relevant_file": "src/x.py", "relevant_lines_start": 21, "relevant_lines_end": 22,
         "suggestion_content": "x", "label": "best practice", "improved_code": "x", "score": 8},
    ]}

    async def fake_retry(*args, **kwargs):
        return data

    store = type("Store", (), {})()
    store.list_suggestions = lambda *args, **kwargs: [
        {"file": "src/x.py", "line": 20, "state": "applied"},
    ]
    monkeypatch.setattr(suggestions_module, "retry_with_fallback_models", fake_retry)
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)
    monkeypatch.setattr(telemetry_events, "emit_run_started", lambda **kwargs: None)

    tool = _make_tool()
    tool.pr_url = "http://example.test/root/project/-/merge_requests/1"
    tool.progress = "Preparing suggestions..."
    tool.vars = {"agents_md_rules": []}

    try:
        await tool.run()
    finally:
        restore_settings(snapshot)

    comments = [item for item in tool.git_provider.published if isinstance(item, str)]
    assert len(comments) == 1
    assert "本次 `/improve` 生成 **1** 条建议" in comments[0]
    assert "x.py:L21" in comments[0]
    assert "已自动跳过" in comments[0]
