from unittest.mock import MagicMock

from pr_agent.git_providers.gitlab_provider import GitLabProvider
from pr_agent.servers import gitlab_webhook


class _Discussion:
    def __init__(self, discussion_id, note):
        self.id = discussion_id
        self.attributes = {"id": discussion_id, "notes": [note]}


class _DiscussionManager:
    def __init__(self, discussions):
        self._discussions = {discussion.id: discussion for discussion in discussions}

    def list(self, get_all=False):
        return list(self._discussions.values())

    def get(self, discussion_id):
        return self._discussions[discussion_id]


def _note(
    *,
    body="```suggestion:-0+2\nnew code\n```",
    username="review-bot",
    old_sha="old-sha",
    path="app.py",
    line=2,
    resolved=False,
):
    return {
        "id": 101,
        "body": body,
        "author": {"username": username},
        "resolvable": True,
        "resolved": resolved,
        "position": {
            "head_sha": old_sha,
            "new_path": path,
            "new_line": line,
        },
    }


def _provider(note, old_content, current_content):
    provider = GitLabProvider.__new__(GitLabProvider)
    provider.id_mr = 80
    provider.mr = MagicMock()
    provider.mr.sha = "current-sha"
    discussion = _Discussion("discussion-1", note)
    provider.mr.discussions = _DiscussionManager([discussion])
    contents = {
        ("app.py", "old-sha"): old_content,
        ("app.py", "current-sha"): current_content,
    }
    provider.get_pr_file_content = lambda path, ref: contents[(path, ref)]
    provider.resolve_discussion = MagicMock(return_value=True)
    return provider


def test_resolves_bot_suggestion_when_target_range_changed():
    old_content = "header\ndef normalise(serial):\n    return serial\nfooter\n"
    current_content = "header\ndef normalise(serial: str) -> str:\n    return serial\nfooter\n"
    provider = _provider(_note(), old_content, current_content)

    resolved = provider.resolve_superseded_suggestion_discussions({"review-bot"})

    assert resolved == ["discussion-1"]
    provider.resolve_discussion.assert_called_once_with("discussion-1")


def test_keeps_suggestion_when_target_only_shifted_by_unrelated_insert():
    old_content = "header\ndef normalise(serial):\n    return serial\nfooter\n"
    current_content = "new header\nheader\ndef normalise(serial):\n    return serial\nfooter\n"
    provider = _provider(_note(), old_content, current_content)

    resolved = provider.resolve_superseded_suggestion_discussions({"review-bot"})

    assert resolved == []
    provider.resolve_discussion.assert_not_called()


def test_keeps_suggestion_when_only_code_outside_target_changed():
    old_content = "old header\ndef normalise(serial):\n    return serial\nfooter\n"
    current_content = "new header\ndef normalise(serial):\n    return serial\nfooter\n"
    provider = _provider(_note(), old_content, current_content)

    resolved = provider.resolve_superseded_suggestion_discussions({"review-bot"})

    assert resolved == []
    provider.resolve_discussion.assert_not_called()


def test_ignores_non_bot_suggestion_even_when_target_changed():
    old_content = "header\ndef normalise(serial):\n    return serial\nfooter\n"
    current_content = "header\ndef normalise(serial: str) -> str:\n    return serial\nfooter\n"
    provider = _provider(_note(username="developer"), old_content, current_content)

    resolved = provider.resolve_superseded_suggestion_discussions({"review-bot"})

    assert resolved == []
    provider.resolve_discussion.assert_not_called()


def test_webhook_marks_auto_resolved_suggestion_superseded(monkeypatch):
    provider = MagicMock()
    provider.resolve_superseded_suggestion_discussions.return_value = ["discussion-1"]
    monkeypatch.setattr(gitlab_webhook, "get_git_provider_with_context", lambda pr_url: provider)
    settings = MagicMock()
    settings.get.side_effect = lambda key, default=None: {
        "gitlab.user": "review-bot",
        "config.allowed_bot_usernames": ["review-bot"],
    }.get(key, default)
    monkeypatch.setattr(gitlab_webhook, "get_settings", lambda: settings)

    store = MagicMock()
    store.get_suggestion_by_note_id.return_value = {
        "suggestion_id": "suggestion-1",
        "mr_id": 80,
        "state": "open",
    }
    monkeypatch.setattr(gitlab_webhook.telemetry_events, "get_default_store", lambda: store)
    mark_superseded = MagicMock()
    emit_action = MagicMock()
    monkeypatch.setattr(gitlab_webhook.telemetry_events, "mark_suggestion_superseded", mark_superseded)
    monkeypatch.setattr(gitlab_webhook.telemetry_events, "emit_action", emit_action)

    resolved = gitlab_webhook._resolve_superseded_suggestions(
        "http://127.0.0.1:8929/root/auto-review-test/-/merge_requests/80"
    )

    assert resolved == ["discussion-1"]
    mark_superseded.assert_called_once_with("suggestion-1")
    emit_action.assert_called_once_with(
        action="resolved",
        suggestion_id="suggestion-1",
        mr_id=80,
        actor="review-bot",
        note="superseded by source update; resolved discussion discussion-1",
    )


def test_webhook_does_not_overwrite_user_dismissed_state(monkeypatch):
    provider = MagicMock()
    provider.resolve_superseded_suggestion_discussions.return_value = ["discussion-1"]
    monkeypatch.setattr(gitlab_webhook, "get_git_provider_with_context", lambda pr_url: provider)
    settings = MagicMock()
    settings.get.side_effect = lambda key, default=None: {
        "gitlab.user": "review-bot",
        "config.allowed_bot_usernames": ["review-bot"],
    }.get(key, default)
    monkeypatch.setattr(gitlab_webhook, "get_settings", lambda: settings)

    store = MagicMock()
    store.get_suggestion_by_note_id.return_value = {
        "suggestion_id": "suggestion-1",
        "mr_id": 80,
        "state": "dismissed",
    }
    monkeypatch.setattr(gitlab_webhook.telemetry_events, "get_default_store", lambda: store)
    mark_superseded = MagicMock()
    emit_action = MagicMock()
    monkeypatch.setattr(gitlab_webhook.telemetry_events, "mark_suggestion_superseded", mark_superseded)
    monkeypatch.setattr(gitlab_webhook.telemetry_events, "emit_action", emit_action)

    resolved = gitlab_webhook._resolve_superseded_suggestions(
        "http://127.0.0.1:8929/root/auto-review-test/-/merge_requests/80"
    )

    assert resolved == ["discussion-1"]
    mark_superseded.assert_not_called()
    emit_action.assert_not_called()
