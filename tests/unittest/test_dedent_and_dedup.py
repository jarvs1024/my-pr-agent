"""Unit tests for suggestion-pipeline robustness fixes.

Two related bugs that surface when /improve emits a suggestion whose
``improved_code`` is anchored at a module-level line:

1. ``dedent_code`` only handled ``delta_spaces > 0`` (LLM under-indented
   the snippet). When the LLM over-indents -- e.g. emits a module-level
   ``def`` with 4 leading spaces because the snippet was copy-pasted from
   a nested context -- the snippet is returned as-is. GitLab then applies
   the suggestion verbatim and produces a broken ``def`` nested inside
   the previous function.

2. ``push_inline_code_suggestions`` did not dedupe suggestions emitted
   in the same LLM round. When the model emits two patches for the same
   (file, line, label) -- e.g. one inline ``-0+1`` patch and one full
   ``-0+10`` function replacement -- both get published, both get applied,
   and the telemetry record doubles up.
"""

from __future__ import annotations

import textwrap


# ---------------------------------------------------------------------------
# dedent_code: symmetric indent/dedent
# ---------------------------------------------------------------------------


class _FakeDiffFile:
    def __init__(self, filename, head_file):
        self.filename = filename
        self.head_file = head_file


class _FakeGitProvider:
    def __init__(self, head_file, filename="services/payment_router.py"):
        self._head = head_file
        self._filename = filename
        self.diff_files = [_FakeDiffFile(filename, head_file)]

    def get_diff_files(self):
        return self.diff_files


def _make_suggestion_tool(head_file, filename="services/payment_router.py"):
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    p = PRCodeSuggestions.__new__(PRCodeSuggestions)
    p.git_provider = _FakeGitProvider(head_file, filename)
    return p


def test_dedent_code_strips_extra_leading_whitespace():
    """LLM emitted a module-level def with 4 leading spaces; the original
    anchor is at column 0; dedent_code must strip the extra indent."""
    head = textwrap.dedent(
        '"""\n'
        'import sqlite3\n'
        '\n'
        '\n'
        'def load_payment_history(path):\n'
        '    with open(path, "r", encoding="utf-8") as handle:\n'
        '        data = handle.read()\n'
        '    return json.loads(data)\n'
    )
    p = _make_suggestion_tool(head)
    over_indented = (
        '    def load_payment_history(path: str) -> dict:\n'
        '        """Load and JSON-decode the payment history file at path."""\n'
        '        with open(path, "r", encoding="utf-8") as handle:\n'
        '            data = handle.read()\n'
        '        return json.loads(data)\n'
    )
    out = p.dedent_code("services/payment_router.py", 5, over_indented)
    assert not out.startswith(" "), f"snippet still indented: {out!r}"
    assert out.splitlines()[0] == "def load_payment_history(path: str) -> dict:"
    assert out.splitlines()[1].startswith('    """')


def test_dedent_code_indent_under_indented_path_still_works():
    """Existing behaviour (delta_spaces > 0) is preserved."""
    head = textwrap.dedent(
        '"""\n'
        'class Foo:\n'
        '    def helper(self, x):\n'
        '        return x\n'
    )
    p = _make_suggestion_tool(head, filename="foo.py")
    snippet = "def helper(self, x: int) -> int:\n    return x * 2\n"
    # Anchor on line 3 (`    def helper(self, x):`), which is at column 4.
    out = p.dedent_code("foo.py", 3, snippet)
    assert out.splitlines()[0] == "    def helper(self, x: int) -> int:"
    assert out.splitlines()[1] == "        return x * 2"


def test_dedent_code_zero_delta_is_noop():
    """Snippet already at the right indent must be returned unchanged."""
    head = "def foo():\n    return 1\n"
    p = _make_suggestion_tool(head, filename="foo.py")
    snippet = 'def foo() -> int:\n    """Return 1."""\n    return 1\n'
    out = p.dedent_code("foo.py", 1, snippet)
    assert out == snippet


def test_dedent_code_mixed_indent_uses_common_prefix():
    """When the snippet has inconsistent leading whitespace, dedent only
    removes the common prefix. The fix must not over-strip."""
    head = "def foo():\n    pass\n"
    p = _make_suggestion_tool(head, filename="foo.py")
    snippet = (
        "def foo():\n"
        '    """Summary.\n'
        '    Multi-line body."""\n'
        "    pass\n"
    )
    out = p.dedent_code("foo.py", 1, snippet)
    # Common prefix is 0 (since `def foo()` is at col 0), so the snippet
    # should be unchanged.
    assert out == snippet


# ---------------------------------------------------------------------------
# _dedup_same_round_suggestions
# ---------------------------------------------------------------------------


def _cs(file_, start, end, label, score):
    return {
        "body": f"body for {file_}:{start}",
        "relevant_file": file_,
        "relevant_lines_start": start,
        "relevant_lines_end": end,
        "label": label,
        "original_suggestion": {"score": score, "label": label},
    }


def test_dedup_keeps_single_suggestions():
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    p = PRCodeSuggestions.__new__(PRCodeSuggestions)
    items = [
        _cs("a.py", 10, 12, "best practice", 8),
        _cs("b.py", 20, 25, "critical bug", 10),
    ]
    out = p._dedup_same_round_suggestions(items)
    assert out == items


def test_dedup_collapses_same_file_line_label():
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    p = PRCodeSuggestions.__new__(PRCodeSuggestions)
    items = [
        _cs("a.py", 50, 60, "best practice", 8),
        _cs("a.py", 50, 51, "best practice", 8),
    ]
    out = p._dedup_same_round_suggestions(items)
    assert len(out) == 1
    assert out[0]["relevant_lines_end"] == 60


def test_dedup_keeps_higher_scored_duplicate():
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    p = PRCodeSuggestions.__new__(PRCodeSuggestions)
    items = [
        _cs("a.py", 50, 51, "best practice", 6),
        _cs("a.py", 50, 60, "best practice", 9),
    ]
    out = p._dedup_same_round_suggestions(items)
    assert len(out) == 1
    assert out[0]["original_suggestion"]["score"] == 9


def test_dedup_keeps_different_labels_at_same_line():
    """DOCSTRING + TYPEHINTS on the same function should NOT collapse."""
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    p = PRCodeSuggestions.__new__(PRCodeSuggestions)
    items = [
        _cs("a.py", 8, 9, "maintainability", 7),
        _cs("a.py", 8, 9, "style", 6),
    ]
    out = p._dedup_same_round_suggestions(items)
    assert len(out) == 2


def test_dedup_keeps_same_label_different_files():
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    p = PRCodeSuggestions.__new__(PRCodeSuggestions)
    items = [
        _cs("a.py", 50, 51, "best practice", 8),
        _cs("b.py", 50, 51, "best practice", 8),
    ]
    out = p._dedup_same_round_suggestions(items)
    assert len(out) == 2


def test_dedup_three_duplicates_pick_top_score():
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    p = PRCodeSuggestions.__new__(PRCodeSuggestions)
    items = [
        _cs("a.py", 50, 51, "best practice", 5),
        _cs("a.py", 50, 60, "best practice", 9),
        _cs("a.py", 50, 55, "best practice", 7),
    ]
    out = p._dedup_same_round_suggestions(items)
    assert len(out) == 1
    assert out[0]["original_suggestion"]["score"] == 9
