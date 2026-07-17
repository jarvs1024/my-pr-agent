"""Unit tests for the permissive /dismiss command parser in gitlab_webhook.

The parser must accept any DiffNote-style reply whose body contains the
word ``dismiss`` (case-insensitive), regardless of whether it is prefixed
with ``/``, ``?``, wrapped in straight/curly quotes, or glued to the
following reason text without whitespace.
"""
import re

import pytest


_DISMISS_PATTERN = re.compile(
    r'(?<![A-Za-z0-9])dismiss(?![A-Za-z0-9])',
    flags=re.IGNORECASE,
)

# Mirrors the wrapper strip in pr_agent/servers/gitlab_webhook.py.
# Keep these two definitions in sync.
_WRAPPER_STRIP = re.compile(
    r'^[\s/\\?"\'\u2018\u2019\u201c\u201d,;:。,;:!\-—_()]+'
    r'|'
    r'[\s/\\?"\'\u2018\u2019\u201c\u201d,;:。,;:!\-—_()]+$'
)


def _parse_dismiss(body: str):
    m = _DISMISS_PATTERN.search(body)
    if not m:
        return None
    word = m.group(0)
    before, _, after = body.partition(word)
    reason = (before + after).strip()
    reason = _WRAPPER_STRIP.sub("", reason).strip()
    return reason


@pytest.mark.parametrize(
    "body,expected",
    [
        # Original strict forms still work.
        ("/dismiss 误报",                       "误报"),
        ("/dismiss 忽略原因测试",               "忽略原因测试"),
        # Prefix-less / question-prefixed.
        ("dismiss 忽略",                        "忽略"),
        ("?dismiss 忽略",                       "忽略"),
        # No whitespace between dismiss and reason (CJK boundary).
        ("dismiss忽略",                         "忽略"),
        # Straight / curly quote wrapping.
        ("'/dismiss 忽略原因测试'",             "忽略原因测试"),
        ("\u201cdismiss 忽略原因测试\u201d",    "忽略原因测试"),
        # Bare command, no reason.
        ("/dismiss",                            ""),
        # Multi-line reason.
        ("/dismiss\n多行原因\n第二行",          "多行原因\n第二行"),
        # False positives that MUST NOT match.
        ("/dismissed",                          None),
        ("dismissal 误报",                      None),
        ("/review",                             None),
        # Permissive: body just mentions the word dismiss — also triggers,
        # reason is whatever's left after stripping wrappers.
        ("随便聊聊 dismiss 这个词",             "随便聊聊  这个词"),
    ],
)
def test_dismiss_parsing(body, expected):
    assert _parse_dismiss(body) == expected


def test_pattern_in_sync_with_production():
    """Smoke check: the test's regex literal must match the one in the
    gitlab_webhook production code. If this fails, update the regex in
    BOTH places."""
    import pr_agent.servers.gitlab_webhook as mod
    src = open(mod.__file__, encoding="utf-8").read()
    m = re.search(
        r"_dismiss_match = re\.search\(\s*r'(?P<pat>[^']+)'\s*,\s*body",
        src,
    )
    assert m, "production regex not found in gitlab_webhook.py"
    prod_pat = m.group("pat")
    # Compare normalised forms (collapse whitespace).
    test_pat = re.sub(r"\s+", "", _DISMISS_PATTERN.pattern)
    prod_norm = re.sub(r"\s+", "", prod_pat)
    assert test_pat == prod_norm, (
        f"test regex out of sync with production\n"
        f"  test: {test_pat!r}\n"
        f"  prod: {prod_norm!r}"
    )
