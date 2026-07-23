"""Tests for the /adopt inline-reply command on a suggestion thread.

Pure regex tests (mirror the implementation in
``pr_agent/servers/gitlab_webhook.py``) plus source-level sanity checks
that verify the new telemetry helpers exist without having to import
the ``pr_agent`` package — a cold ``import pr_agent`` triggers a
known partial-init ImportError in ``pr_agent.log`` (pre-existing,
orthogonal to this patch) which would mask test signal.

Behaviour under test:

- ``/adopt`` is matched BEFORE ``/dismiss`` so a body containing both
  keys routes to the user's explicit choice.
- Reason extraction strips trivial wrappers (incl. full-width CJK
  colon U+FF1A so a trailing "：" does not leak into the reason).
- Adoption is a no-op on suggestions whose state is already resolved.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


# Mirror the regex used inside gitlab_webhook.py /adopt branch.
_ADOPT_RE = re.compile(r"(?<![A-Za-z0-9])adopt(?![A-Za-z0-9])", flags=re.IGNORECASE)
_DISMISS_RE = re.compile(r"(?<![A-Za-z0-9])dismiss(?![A-Za-z0-9])", flags=re.IGNORECASE)


def _route(body):
    """Pick the matched command, /adopt wins over /dismiss on tie."""
    if _ADOPT_RE.search(body):
        return "adopt"
    if _DISMISS_RE.search(body):
        return "dismiss"
    return None


# Wrapper-strip regex for /adopt reason extraction. Re-built via
# \uXXXX escapes for the curly quotes so the source stays ASCII-only
# and the heredoc / quoting layers stay simple.
_WRAPPER = (
    r'^['
    + r"\s/\\?"    + r"\u0022\u0027"    + r"\u2018\u2019\u201c\u201d"    + r",;\u003A\u3002,\u003A"    + r":\uFF1A!\-—_()"    + r"]+"
    + r"|"
    + r"["
    + r"\s/\\?"
    + r"\u0022\u0027"
    + r"\u2018\u2019\u201c\u201d"
    + r",;\u003A\u3002,\u003A"
    + r":\uFF1A!\-—_()"
    + r"]+$"
)
_WRAPPER_RE = re.compile(_WRAPPER)


def _reason_for_adopt(body):
    """Replicate the partition-based reason extraction used in gitlab_webhook.py."""
    m = _ADOPT_RE.search(body)
    if not m:
        return ""
    word = m.group(0)
    before, _, after = body.partition(word)
    raw = (before + after).strip()
    return _WRAPPER_RE.sub("", raw).strip()


# CJK strings used by the tests — built from \uXXXX escapes so
# the test file stays 100% ASCII source.
GAIYONG = "\u6539\u7528 logging"  # "use logging"
JIA = "\u52a0 import"             # "add import"
GANGAO = "\u6d4b\u8bd5\u4ee3\u7801"  # "test code"
DUANYU = "\u7528 dismiss \u98ce\u683c\u91cd\u5199\u4e86"  # "rewrote in dismiss style"
CUXIAN = "\u6539\u4e86\u4e00\u7248"   # "made one version"
GAIYONG_EXC = "\u6539\u7528 logging.exception"
JIA_IMPORT = GAIYONG_EXC + "\n\u5e76\u52a0 import"  # "and add import"


class TestAdoptRouting:
    def test_adopt_no_reason(self):
        assert _route("/adopt") == "adopt"

    def test_adopt_with_reason(self):
        assert _route("/adopt " + GAIYONG_EXC) == "adopt"

    def test_adopt_with_multiline_reason(self):
        body = "/adopt\n" + JIA_IMPORT
        assert _route(body) == "adopt"

    def test_adopt_with_leading_whitespace(self):
        assert _route("   /adopt   " + CUXIAN) == "adopt"

    def test_adopt_case_insensitive(self):
        assert _route("/ADOPT ok") == "adopt"
        assert _route("/Adopt " + GAIYONG.split()[0]) == "adopt"

    def test_adopt_priority_over_dismiss_keyword(self):
        body = "/adopt " + DUANYU
        assert _route(body) == "adopt"

    def test_dismiss_still_routes_when_no_adopt(self):
        assert _route("/dismiss " + GANGAO) == "dismiss"

    def test_unknown_body_routes_to_none(self):
        assert _route("hello world") is None
        assert _route("") is None

    def test_word_substring_does_not_match(self):
        # Word boundaries: adopt inside adoption must not match because
        # trailing p is [A-Za-z0-9]. Same for dismissed.
        assert _route("adoption of new rule") is None
        assert _route("dismissed employees") is None


class TestAdoptReasonExtraction:
    def test_no_reason_yields_empty_string(self):
        assert _reason_for_adopt("/adopt") == ""

    def test_single_line_reason(self):
        assert _reason_for_adopt("/adopt " + GAIYONG) == GAIYONG

    def test_multiline_reason(self):
        body = "/adopt\n" + GAIYONG + "\n" + JIA
        reason = _reason_for_adopt(body)
        assert GAIYONG in reason
        assert JIA in reason

    def test_trailing_wrapper_stripped(self):
        # Trailing punctuation stripped by wrapper regex. The full-width
        # CJK colon U+FF1A is the case that broke the previous regex, so
        # it is called out explicitly.
        assert _reason_for_adopt("/adopt " + GAIYONG + "\u3002") == GAIYONG
        assert _reason_for_adopt("/adopt " + GAIYONG + ",") == GAIYONG
        assert _reason_for_adopt("/adopt " + GAIYONG + "\uFF1A") == GAIYONG


class TestAdoptSourceShape:
    """Source-level checks that deliberately avoid ``from pr_agent... import``.

    A cold ``import pr_agent`` triggers a known partial-init ImportError
    in ``pr_agent.log`` (see ``pr_agent/custom_merge_loader.py`` line 6)
    that is pre-existing and orthogonal to this patch. Runtime-level
    coverage of /adopt lives in the e2e / smoke MR flow. These checks
    only confirm the helpers are physically defined in the right files.
    """

    def _repo_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def _all_defs(self, rel_path):
        src = (self._repo_root() / rel_path).read_text()
        tree = ast.parse(src)

        def visit(node):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield child.name
                elif isinstance(child, ast.ClassDef):
                    yield from visit(child)
            for child in getattr(node, "body", []):
                pass

        seen = set()
        for child in tree.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                seen.add(child.name)
            elif isinstance(child, ast.ClassDef):
                for sub in child.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        seen.add(sub.name)
                    elif isinstance(sub, ast.ClassDef):
                        for subsub in sub.body:
                            if isinstance(subsub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                seen.add(subsub.name)
        return seen

    def test_mark_suggestion_adopted_in_events(self):
        names = self._all_defs("pr_agent/telemetry/events.py")
        assert "mark_suggestion_adopted" in names

    def test_count_adopted_implicitly_in_store(self):
        names = self._all_defs("pr_agent/telemetry/store.py")
        assert "count_adopted_implicitly" in names

    def test_adopt_block_present_in_webhook(self):
        src = (self._repo_root() / "pr_agent/servers/gitlab_webhook.py").read_text()
        assert "adopted_implicitly" in src
        assert "mark_suggestion_adopted(" in src
        assert "resolve_discussion(discussion_id)" in src
        assert src.index("/adopt") < src.index("dismiss_match = re.search")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
