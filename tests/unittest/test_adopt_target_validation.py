import copy
import importlib
from types import SimpleNamespace

import pytest

from pr_agent.config_loader import get_settings
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


class _Provider:
    def __init__(self, *, current_sha="current-sha", contents=None):
        self.mr = SimpleNamespace(sha=current_sha, diff_refs={"head_sha": current_sha})
        self.contents = contents or {}
        self.replies = []
        self.resolved = []

    def get_pr_file_content(self, file_path, ref):
        return self.contents.get((file_path, ref), "")

    def reply_to_comment_from_comment_id(self, discussion_id, body):
        self.replies.append((discussion_id, body))

    def resolve_discussion(self, discussion_id):
        self.resolved.append(discussion_id)
        return True


def _suggestion(**overrides):
    return {
        "suggestion_id": "sug-import",
        "mr_id": 99,
        "file": "service.py",
        "line": 1,
        "line_end": 1,
        "posted_head_sha": "posted-sha",
        "state": "open",
        **overrides,
    }


def test_adopt_rejects_same_head(gitlab_webhook_module):
    provider = _Provider(current_sha="posted-sha")

    allowed, reason = gitlab_webhook_module._validate_adopt_target_change(provider, _suggestion())

    assert allowed is False
    assert reason == "same-head"


def test_adopt_rejects_unrelated_change_in_same_file(gitlab_webhook_module):
    provider = _Provider(contents={
        ("service.py", "posted-sha"): "import sqlite3\n\n\ndef run():\n    return 1\n",
        ("service.py", "current-sha"): "import sqlite3\n\n\ndef run():\n    return 2\n",
    })

    allowed, reason = gitlab_webhook_module._validate_adopt_target_change(provider, _suggestion())

    assert allowed is False
    assert reason == "target-unchanged"


def test_adopt_accepts_adjacent_import_insertion(gitlab_webhook_module):
    provider = _Provider(contents={
        ("service.py", "posted-sha"): "import sqlite3\n\n\ndef run():\n    return 1\n",
        ("service.py", "current-sha"): "import sqlite3\nimport logging\n\n\ndef run():\n    return 1\n",
    })

    allowed, reason = gitlab_webhook_module._validate_adopt_target_change(provider, _suggestion())

    assert allowed is True
    assert reason == "changed"


def test_adopt_accepts_target_function_rewrite(gitlab_webhook_module):
    provider = _Provider(contents={
        ("service.py", "posted-sha"): "def run():\n    return 1\n",
        ("service.py", "current-sha"): "def run():\n    return 2\n",
    })

    allowed, reason = gitlab_webhook_module._validate_adopt_target_change(
        provider,
        _suggestion(line=1, line_end=2),
    )

    assert allowed is True
    assert reason == "changed"


def test_adopt_rejects_when_posted_head_sha_is_missing(gitlab_webhook_module):
    """A suggestion with no recorded ``posted_head_sha`` (legacy rows, or
    rows emitted before the metadata plumbing landed) cannot be verified
    for target-code change, so /adopt must be rejected — not silently
    allowed as a transitional safety net."""
    provider = _Provider()

    allowed, reason = gitlab_webhook_module._validate_adopt_target_change(
        provider,
        _suggestion(posted_head_sha=None),
    )

    assert allowed is False
    assert reason == "posted-sha-unavailable"


def test_adopt_rejects_new_suggestion_when_posted_head_was_unavailable(gitlab_webhook_module):
    provider = _Provider()

    allowed, reason = gitlab_webhook_module._validate_adopt_target_change(
        provider,
        _suggestion(posted_head_sha="__unavailable__"),
    )

    assert allowed is False
    assert reason == "posted-head-unavailable"


def test_adopt_rejects_when_file_content_is_unavailable(gitlab_webhook_module):
    provider = _Provider(contents={})

    allowed, reason = gitlab_webhook_module._validate_adopt_target_change(provider, _suggestion())

    assert allowed is False
    assert reason == "content-unavailable"


def test_rejected_adopt_replies_without_resolving_or_recording(
    gitlab_webhook_module,
    monkeypatch,
):
    provider = _Provider(contents={
        ("service.py", "posted-sha"): "import sqlite3\n\n\ndef run():\n    return 1\n",
        ("service.py", "current-sha"): "import sqlite3\n\n\ndef run():\n    return 2\n",
    })
    recorded = []
    monkeypatch.setattr(
        telemetry_events,
        "mark_suggestion_adopted",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )

    result = gitlab_webhook_module._process_adopt_reply(
        provider=provider,
        discussion_id="discussion-1",
        suggestion=_suggestion(),
        actor="alice",
        reason="manual fix",
        mr_id=99,
    )

    assert result == "adopt-validation-failed"
    assert provider.resolved == []
    assert recorded == []
    assert provider.replies and "请先提交修改" in provider.replies[0][1]


def test_adopt_rejects_line_drift_when_target_appears_verbatim(gitlab_webhook_module):
    """Scenario D: user pushed an unrelated commit that adds a header line
    at the top of the file. The suggestion's line=1 now points to the
    header (was 'import sqlite3' before), but the actual target content
    'import sqlite3' is still present at line 2. The user did NOT modify
    the suggestion's target code, so /adopt must be rejected."""
    provider = _Provider(contents={
        ("service.py", "posted-sha"): "import sqlite3\n\n\ndef run():\n    return 1\n",
        ("service.py", "current-sha"): "# header\nimport sqlite3\n\n\ndef run():\n    return 1\n",
    })

    allowed, reason = gitlab_webhook_module._validate_adopt_target_change(provider, _suggestion())

    assert allowed is False
    assert reason == "target-unchanged"


def test_adopt_accepts_real_edit_in_target_region_with_drift(gitlab_webhook_module):
    """Sanity: user adds a header AND edits the target. The verbatim drift
    check must NOT mask the real edit — the target content no longer
    matches verbatim, so /adopt is accepted."""
    provider = _Provider(contents={
        ("service.py", "posted-sha"): "import sqlite3\n\n\ndef run():\n    return 1\n",
        ("service.py", "current-sha"): "# header\nimport sqlite3\n\n\ndef run():\n    return 2\n",
    })

    allowed, reason = gitlab_webhook_module._validate_adopt_target_change(
        provider,
        _suggestion(line=5, line_end=5),  # target = "    return 1"
    )

    assert allowed is True
    assert reason == "changed"
