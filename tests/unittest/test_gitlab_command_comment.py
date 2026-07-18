from pr_agent.servers.gitlab_webhook import _is_gitlab_command_comment


def test_accepts_slash_commands_with_whitespace():
    assert _is_gitlab_command_comment("/improve")
    assert _is_gitlab_command_comment("  /review")


def test_rejects_bot_suggestion_and_plain_comments():
    assert not _is_gitlab_command_comment("**Suggestion:** fix this\n/dismiss reason")
    assert not _is_gitlab_command_comment("looks good")
    assert not _is_gitlab_command_comment("")
