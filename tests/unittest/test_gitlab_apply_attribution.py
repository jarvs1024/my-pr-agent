import copy
import importlib
from types import SimpleNamespace

import pytest

from pr_agent.config_loader import get_settings  # noqa: F401
from pr_agent.telemetry import events as telemetry_events


@pytest.fixture
def gitlab_webhook_module():
    settings = get_settings()
    original_git_provider = settings.config.get("git_provider", None)
    had_gitlab_settings = "GITLAB" in settings
    original_gitlab_settings = copy.deepcopy(settings.get("GITLAB", None))
    settings.set("GITLAB.URL", "https://gitlab.com")
    try:
        yield importlib.import_module("pr_agent.servers.gitlab_webhook")
    finally:
        settings.config.git_provider = original_git_provider
        if had_gitlab_settings:
            settings.set("GITLAB", original_gitlab_settings)
        else:
            settings.unset("GITLAB", force=True)


class _Response:
    def __init__(self, payload, *, ok=True, status_code=200):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return self._payload


class _Discussions:
    def __init__(self, discussions):
        self._discussions = discussions

    def list(self, get_all=True):
        assert get_all is True
        return [SimpleNamespace(attributes=discussion) for discussion in self._discussions]


class _Provider:
    def __init__(self, discussions, contents):
        self.mr = SimpleNamespace(discussions=_Discussions(discussions))
        self._contents = contents

    def get_pr_file_content(self, file_path, ref):
        return self._contents.get((file_path, ref), "")


class _Store:
    def __init__(self, suggestions):
        self.suggestions = suggestions

    def list_open_suggestion_records(self, mr_id, project_id):
        assert (mr_id, project_id) == (99, 34)
        return self.suggestions


def _discussion(discussion_id, patch):
    return {
        "id": discussion_id,
        "notes": [{
            "body": f"**Suggestion:** fix\n```suggestion:-0+1\n{patch}\n```",
            "type": "DiffNote",
            "author": {"username": "review-bot"},
        }],
    }


def test_load_suggestion_notes_ignores_non_bot_author(gitlab_webhook_module):
    get_settings().set("gitlab.user", "review-bot")
    provider = _Provider(
        discussions=[{
            "id": "discussion-human",
            "notes": [{
                "body": "```suggestion\nreturn 2\n```",
                "author": {"username": "alice"},
            }],
        }],
        contents={},
    )

    notes = gitlab_webhook_module._load_suggestion_notes(provider, {"discussion-human"})

    assert notes == {}


def _event(expected_count=1):
    return {
        "sha": "current-sha",
        "parent_sha": "parent-sha",
        "msg": f"Apply {expected_count} suggestion(s) to 1 file(s)",
        "suggestion_count": expected_count,
        "project_id": 34,
        "ref": "refs/heads/feature",
        "mr_iid": 99,
        "actor": "alice",
        "files_hint": ["service.py"],
        "pr_url": "http://gitlab/root/repo/-/merge_requests/99",
    }


def _install_apply_fakes(monkeypatch, module, provider, store, diff_payload):
    import pr_agent.git_providers.gitlab_provider as gitlab_provider_module
    import requests

    monkeypatch.setattr(module, "_resolve_apply_event", lambda data: _event())
    monkeypatch.setattr(
        gitlab_provider_module,
        "GitLabProvider",
        lambda merge_request_url: provider,
    )
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: _Response(diff_payload))


def test_handle_apply_commit_marks_only_exact_lookup_suggestion(gitlab_webhook_module, monkeypatch):
    lookup_patch = """def lookup_user(name: str) -> list:
    try:
        return query(name)
    finally:
        close()"""
    average_patch = """def average(values: list) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)"""
    parent = """def lookup_user(name):
    return query(name)


def average(values):
    return sum(values) / len(values)
"""
    current = lookup_patch + "\n\n\n" + """def average(values):
    return sum(values) / len(values)
"""
    provider = _Provider(
        discussions=[
            _discussion("discussion-lookup", lookup_patch),
            _discussion("discussion-average", average_patch),
        ],
        contents={
            ("service.py", "parent-sha"): parent,
            ("service.py", "current-sha"): current,
        },
    )
    store = _Store([
        {"suggestion_id": "sug-lookup", "note_id": "discussion-lookup", "file": "service.py", "state": "open"},
        {"suggestion_id": "sug-average", "note_id": "discussion-average", "file": "service.py", "state": "open"},
    ])
    _install_apply_fakes(
        monkeypatch,
        gitlab_webhook_module,
        provider,
        store,
        [{
            "new_path": "service.py",
            "old_path": "service.py",
            "diff": (
                "@@ -1,2 +1,5 @@\n"
                "-def lookup_user(name):\n"
                "-    return query(name)\n"
                "+def lookup_user(name: str) -> list:\n"
                "+    try:\n"
                "+        return query(name)\n"
                "+    finally:\n"
                "+        close()"
            ),
        }],
    )
    marked = []
    monkeypatch.setattr(
        telemetry_events,
        "mark_suggestion_ids_applied",
        lambda **kwargs: marked.extend(kwargs["suggestion_ids"]) or kwargs["suggestion_ids"],
    )
    monkeypatch.setattr(
        telemetry_events,
        "mark_suggestions_applied",
        lambda **kwargs: pytest.fail("line-range apply matching must not be used"),
    )

    gitlab_webhook_module._handle_apply_commit({"object_kind": "push"})

    assert marked == ["sug-lookup"]


def test_handle_apply_commit_keeps_open_when_exact_match_is_ambiguous(gitlab_webhook_module, monkeypatch):
    patch = "return 2"
    provider = _Provider(
        discussions=[
            _discussion("discussion-1", patch),
            _discussion("discussion-2", patch),
        ],
        contents={
            ("service.py", "parent-sha"): "return 1\n",
            ("service.py", "current-sha"): "return 2\n",
        },
    )
    store = _Store([
        {"suggestion_id": "sug-1", "note_id": "discussion-1", "file": "service.py", "state": "open"},
        {"suggestion_id": "sug-2", "note_id": "discussion-2", "file": "service.py", "state": "open"},
    ])
    _install_apply_fakes(
        monkeypatch,
        gitlab_webhook_module,
        provider,
        store,
        [{"new_path": "service.py", "old_path": "service.py", "diff": "@@ -1 +1 @@\n-return 1\n+return 2"}],
    )
    marked = []
    monkeypatch.setattr(
        telemetry_events,
        "mark_suggestion_ids_applied",
        lambda **kwargs: marked.extend(kwargs["suggestion_ids"]),
    )

    gitlab_webhook_module._handle_apply_commit({"object_kind": "push"})

    assert marked == []


def test_handle_apply_commit_keeps_open_when_diff_api_fails(gitlab_webhook_module, monkeypatch):
    import pr_agent.git_providers.gitlab_provider as gitlab_provider_module
    import requests

    provider = _Provider([], {})
    store = _Store([])
    monkeypatch.setattr(gitlab_webhook_module, "_resolve_apply_event", lambda data: _event())
    monkeypatch.setattr(gitlab_provider_module, "GitLabProvider", lambda merge_request_url: provider)
    monkeypatch.setattr(telemetry_events, "get_default_store", lambda: store)
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: _Response({}, ok=False, status_code=503))
    marked = []
    monkeypatch.setattr(
        telemetry_events,
        "mark_suggestion_ids_applied",
        lambda **kwargs: marked.extend(kwargs["suggestion_ids"]),
    )

    gitlab_webhook_module._handle_apply_commit({"object_kind": "push"})

    assert marked == []
