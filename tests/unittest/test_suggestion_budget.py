import pytest

from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


def test_get_suggestion_limit_prefers_global_setting():
    paths = (
        "pr_code_suggestions.num_code_suggestions",
        "pr_code_suggestions.num_code_suggestions_per_chunk",
    )
    snapshot = snapshot_settings(paths)
    settings = get_settings()
    settings.set("pr_code_suggestions.num_code_suggestions", 6)
    settings.set("pr_code_suggestions.num_code_suggestions_per_chunk", 3)
    try:
        assert PRCodeSuggestions._get_suggestion_limit() == 6
    finally:
        restore_settings(snapshot)


def test_rank_and_limit_keeps_stable_highest_six():
    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    suggestions = [
        {"id": index, "original_suggestion": {"score": score}}
        for index, score in enumerate([5, 9, 7, 9, 8, 6, 10, 4])
    ]

    kept, dropped = tool._rank_and_limit_suggestions(suggestions, limit=6)

    assert [item["id"] for item in kept] == [6, 1, 3, 4, 2, 5]
    assert dropped == 2


class _PublishingProvider:
    id_mr = 97
    id_project = 34
    pr_url = "http://gitlab/root/repo/-/merge_requests/97"

    def __init__(self):
        self.published = []

    def publish_code_suggestions(self, suggestions):
        self.published.extend(suggestions)
        return True


@pytest.mark.asyncio
async def test_push_inline_suggestions_publishes_only_six_and_returns_count(monkeypatch):
    from pr_agent.telemetry import events as telemetry_events

    store = type("Store", (), {})()
    store.list_suggestions = lambda *args, **kwargs: []
    store.record_suggestion = lambda suggestion: None
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)

    tool = PRCodeSuggestions.__new__(PRCodeSuggestions)
    tool.git_provider = _PublishingProvider()
    tool.progress_response = None
    tool.dedent_code = lambda relevant_file, relevant_lines_start, snippet: snippet
    data = {
        "code_suggestions": [
            {
                "relevant_file": "src/x.py",
                "relevant_lines_start": index,
                "relevant_lines_end": index,
                "suggestion_content": f"fix {index}",
                "one_sentence_summary": f"fix {index}",
                "existing_code": f"value = {index}",
                "improved_code": f"value = {index + 1}",
                "label": "bug",
                "score": index,
            }
            for index in range(1, 9)
        ]
    }

    published_count = await tool.push_inline_code_suggestions(data)

    assert published_count == 6
    assert len(tool.git_provider.published) == 6
    assert [item["original_suggestion"]["score"] for item in tool.git_provider.published] == [8, 7, 6, 5, 4, 3]
