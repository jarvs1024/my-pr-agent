"""Unit tests for the dismiss-sender guard in gitlab_webhook.

The ``review-bot`` account posts DiffNotes (inline code suggestions) whose
body intentionally contains the literal word ``dismiss`` — the helper
text is ``... 回复 `/dismiss` 忽略原因 ...``. A naive substring match
against any DiffNote body therefore self-resolves every freshly posted
suggestion as soon as the bot publishes it.

These tests pin down the sender-guard behavior so a future refactor
doesn't regress to the "match anywhere" rule:

* A DiffNote whose sender username looks like a bot (``review-bot``,
  ``codex``) is **never** treated as a /dismiss command, unless the note
  body itself explicitly starts with ``/dismiss`` / ``dismiss`` /
  ``?dismiss``.
* A DiffNote whose sender is a regular human is still matched by the
  existing permissive ``dismiss`` regex.
"""
import pytest

from pr_agent.servers.gitlab_webhook import _is_sender_bot_account


def _should_skip_dismiss_for_sender(body, sender_username):
    """Mirror the inline guard logic in gitlab_webhook._handle_comment_dismiss."""
    sender_is_bot = _is_sender_bot_account({"user": {"username": sender_username}})
    body_stripped = body.lstrip()
    body_looks_like_explicit_dismiss = (
        body_stripped.lower().startswith("/dismiss")
        or body_stripped.lower().startswith("dismiss")
        or body_stripped.startswith("?dismiss")
    )
    return sender_is_bot and not body_looks_like_explicit_dismiss


def test_bot_sender_with_suggestion_body_is_skipped():
    """The bot's own suggestion body mentions `/dismiss` in the helper
    text; the guard must prevent self-resolution."""
    body = (
        "**[General, importance: 5]**\n\n"
        "建议替换为 logging.exception。\n\n"
        "---\n\n"
        "**Suggestion:**\n\n"
        "```python\nraise ValueError(\"x\")\n```\n\n"
        "👎 不采纳？回复 `/dismiss` 忽略原因"
    )
    assert _should_skip_dismiss_for_sender(body, "review-bot") is True


@pytest.mark.parametrize(
    "sender_username",
    ["review-bot", "codium-bot", "ci_bot", "codex-bot"],
)
def test_bot_sender_with_suggestion_body_is_skipped_param(sender_username):
    body = (
        "**Suggestion:**\n\n"
        "```python\npass\n```\n\n"
        "👎 不采纳？回复 `/dismiss` 忽略原因"
    )
    assert _should_skip_dismiss_for_sender(body, sender_username) is True


def test_bot_sender_with_explicit_slash_dismiss_is_still_resolved():
    """If the bot account (or any bot-shaped username) sends an explicit
    `/dismiss ...` note we still treat it as a dismiss."""
    assert _should_skip_dismiss_for_sender("/dismiss 误报：测试用例", "review-bot") is False


def test_bot_sender_with_bare_dismiss_keyword_is_still_resolved():
    """`dismiss忽略` (no separator) also counts as explicit."""
    assert _should_skip_dismiss_for_sender("dismiss忽略", "review-bot") is False


def test_human_sender_with_dismiss_keyword_resolves():
    """A real human posting a normal `dismiss 忽略` reply must still
    trigger the dismiss flow — that's the whole point of this handler."""
    assert _should_skip_dismiss_for_sender("dismiss 忽略原因", "jarvs") is False


def test_human_sender_with_dismiss_inside_long_body_still_resolves():
    """Permissive matching: a human reply like `这条建议不靠谱，dismiss` must
    still resolve even though dismiss isn't at the start."""
    assert _should_skip_dismiss_for_sender("这条建议不靠谱，dismiss 误报", "jarvs") is False


def test_human_sender_unaffected_by_dismiss_keyword_in_helper_text():
    """Humans don't get gated by the bot guard — only bot accounts do."""
    body = (
        "**Suggestion:**\n\n"
        "```python\npass\n```\n\n"
        "👎 不采纳？回复 `/dismiss` 忽略原因"
    )
    assert _should_skip_dismiss_for_sender(body, "jarvs") is False


@pytest.mark.parametrize(
    "sender_username", ["review-bot", "codium-bot", "ci_bot", "codex-bot"],
)
def test_is_sender_bot_account_detects_known_bots(sender_username):
    assert _is_sender_bot_account({"user": {"username": sender_username, "name": "x"}}) is True


@pytest.mark.parametrize(
    "sender_username", ["jarvs", "alice", "bob", "developer"],
)
def test_is_sender_bot_account_passes_through_humans(sender_username):
    assert _is_sender_bot_account({"user": {"username": sender_username, "name": "x"}}) is False


def test_is_sender_bot_account_handles_missing_user():
    """Edge case: webhook payload without a ``user`` field must not raise."""
    assert _is_sender_bot_account({}) is False
    assert _is_sender_bot_account({"user": {}}) is False
