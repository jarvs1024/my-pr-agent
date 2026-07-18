"""Unit tests for the /dismiss command parser in gitlab_webhook.

Production behavior: the parser strips trivial leading wrappers from the
body, then re.match the dismiss keyword at the start. This stops the
bot from self-resolving its own suggestion bodies, which only mention
``/dismiss`` in the help footer.

Accepts any command form whose FIRST token (after wrappers) is the
``dismiss`` keyword. Rejects bodies where ``dismiss`` only appears in
the middle (review summary, suggestion body, plain mention).
"""
import re

import pytest


_DISMISS_PATTERN = re.compile(
    r'(?<![A-Za-z0-9])dismiss(?![A-Za-z0-9])',
    flags=re.IGNORECASE,
)


# Strip trivial leading wrappers from the body before matching.
# Mirrors pr_agent/servers/gitlab_webhook.py:_wrapper_strip_re.
_LEAD_STRIP_RE = re.compile(
    "^[\\s/\\\\?\u0027\u0027\u2018\u2019\u201c\u201d,;:。,;:!\\-—_()]+"
)


# Mirrors pr_agent/servers/gitlab_webhook.py:_wrapper_strip (used to
# trim both ends of the reason text after dismissing).
_BOTH_STRIP_RE = re.compile(
    "^[\\s/\\\\?\u0027\u0027\u2018\u2019\u201c\u201d,;:。,;:!\\-—_()]+"
    "|"
    "[\\s/\\\\?\u0027\u0027\u2018\u2019\u201c\u201d,;:。,;:!\\-—_()]+$"
)


def _parse_dismiss(body):
    """Mirror the production first-line anchor:
    strip wrappers at the very start of the body, then re.match.
    """
    first_line = _LEAD_STRIP_RE.sub('', body.strip())
    m = _DISMISS_PATTERN.match(first_line)
    if not m:
        return None
    word = m.group(0)
    before, _, after = body.partition(word)
    reason = (before + after).strip()
    reason = _BOTH_STRIP_RE.sub('', reason).strip()
    return reason


@pytest.mark.parametrize(
    "body,expected",
    [
        # Real command forms (must match).
        ("/dismiss 误报",                       "误报"),
        ("/dismiss 忽略原因测试", "忽略原因测试"),
        ("dismiss 忽略",                        "忽略"),
        ("?dismiss 忽略",                       "忽略"),
        ("dismiss忽略",                         "忽略"),
        ("'/dismiss 忽略原因测试'",             "忽略原因测试"),
        ("“dismiss 忽略原因测试”",    "忽略原因测试"),
        ("/dismiss",                            ""),
        ("/dismiss\n多行原因\n第二行",          "多行原因\n第二行"),
        # False positives (must NOT match).
        ("/dismissed",                          None),
        ("dismissal 误报",                      None),
        ("/review",                             None),
        # ---- New (first-line anchored) cases ----
        # suggestion body with /dismiss only in the help footer must NOT match.
        (
            "**Suggestion:** 违反 `SSD-RULE-NO-LOG-EXC`\n\n"
            "```suggestion:-0+4\n"
            "def safe_div(...)\n"
            "```\n\n"
            "👎 不采纳？在下方回复 `/dismiss 忽略原因` 让 pr-agent 关闭本条建议",
            None,
        ),
        # review summary body with dismiss in docs must NOT match.
        (
        "## PR 评审指南 🔍\n\n"
        "如需忽略请回复 `/dismiss 忽略原因`",
            None,
        ),
        # Plain mention of dismiss in the middle of a sentence -> no match.
        ("随便聊聊 dismiss 这个词",             None),
    ],
)
def test_dismiss_parsing(body, expected):
    assert _parse_dismiss(body) == expected


def test_pattern_in_sync_with_production():
    """The test regex must match the production regex literal, and
    production must use re.match (first-line anchored).
    """
    import pr_agent.servers.gitlab_webhook as mod
    src = open(mod.__file__, encoding="utf-8").read()
    m = re.search(
        r"_dismiss_match = re\.(?P<kind>match|search)\(\s*r'(?P<pat>[^']+)'",
        src,
    )
    assert m, "production regex not found in gitlab_webhook.py"
    prod_kind = m.group("kind")
    prod_pat = m.group("pat")
    test_pat = re.sub(r"\s+", "", _DISMISS_PATTERN.pattern)
    prod_norm = re.sub(r"\s+", "", prod_pat)
    assert prod_kind == "match", (
        f"production should use re.match (first-line anchored) but uses {prod_kind}"
    )
    assert test_pat == prod_norm, (
        f"test regex out of sync with production\n"
        f"  test: {test_pat!r}\n"
        f"  prod: {prod_norm!r}"
    )

