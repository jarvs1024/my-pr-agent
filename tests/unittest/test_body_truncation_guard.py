"""Unit tests for ``validate_suggestion_does_not_truncate_body`` in
``pr_agent.tools.pr_code_suggestions``.

The guard exists because the LLM sometimes hallucinates function bodies
into a single ``...`` (Ellipsis) when asked to add docstrings or refactor.
Left unchecked, the resulting DiffNote destroys real code the moment the
reviewer clicks Apply (see MR 78 note 2060: 4 functions lost their bodies
+ ``report_daily_payments`` was deleted).

The guard sets ``score=0`` on suggestions that:
- replace an existing block of >= 4 non-blank lines with
- an improved block containing >= 1 standalone ``...`` line, where the
- improved block dropped to <= 60% of the original line count.

The score=0 then gets filtered by the downstream ``score_threshold=1`` in
``pr_code_suggestions.py``, dropping the suggestion before publication.
"""

from __future__ import annotations

import pytest


def _make_tool():
    from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
    return PRCodeSuggestions.__new__(PRCodeSuggestions)


def _suggestion(existing, improved):
    return {
        "relevant_file": "x.py",
        "relevant_lines_start": 1,
        "suggestion_content": "refactor",
        "existing_code": existing,
        "improved_code": improved,
        "label": "best practice",
        "one_sentence_summary": "...",
    }


# ---------------------------------------------------------------------------
# Rejection: classic body-truncation case (the MR 78 bug)
# ---------------------------------------------------------------------------


def test_rejects_mr78_body_truncation():
    p = _make_tool()
    # Exact shape from MR 78 note 2060: 4 functions, last 3 have bodies
    # replaced with `...`. We collapse to a single representative call.
    existing = (
        "def load_payment_history(path):\n"
        "    with open(path, 'r', encoding='utf-8') as handle:\n"
        "        data = handle.read()\n"
        "    return json.loads(data)\n"
    )
    improved = (
        "def load_payment_history(path):\n"
        "    \"\"\"Read JSON-encoded payment history from the given file path.\"\"\"\n"
        "    ...\n"
    )
    out = p.validate_suggestion_does_not_truncate_body(_suggestion(existing, improved))
    assert out["score"] == 0
    assert "body-truncation" in out["score_why"]
    assert "'...'" in out["score_why"]


# ---------------------------------------------------------------------------
# Acceptance: legitimate suggestions must NOT be rejected
# ---------------------------------------------------------------------------


def test_accepts_legitimate_full_body_rewrite():
    """Refactor with a real body (not just `...`) must pass."""
    p = _make_tool()
    existing = (
        "def load_payment_history(path):\n"
        "    with open(path, 'r') as handle:\n"
        "        data = handle.read()\n"
        "    return json.loads(data)\n"
    )
    improved = (
        "def load_payment_history(path: str) -> list:\n"
        "    \"\"\"Load payment history from a JSON file.\"\"\"\n"
        "    with open(path, 'r', encoding='utf-8') as handle:\n"
        "        return json.load(handle)\n"
    )
    out = p.validate_suggestion_does_not_truncate_body(_suggestion(existing, improved))
    assert out.get("score") != 0, "legitimate rewrite was wrongly rejected"


def test_accepts_when_no_ellipsis():
    """If the improved code has no `...` line, it cannot be body-truncation."""
    p = _make_tool()
    existing = (
        "def foo(x):\n"
        "    a = x + 1\n"
        "    b = a * 2\n"
        "    return b\n"
    )
    improved = (
        "def foo(x: int) -> int:\n"
        "    \"\"\"Increment then double.\"\"\"\n"
        "    a = x + 1\n"
        "    b = a * 2\n"
        "    return b\n"
    )
    out = p.validate_suggestion_does_not_truncate_body(_suggestion(existing, improved))
    assert out.get("score") != 0


def test_accepts_small_function_with_ellipsis():
    """A tiny function (1-2 line body) where the original has NO real body
    to preserve must NOT be rejected even if `...` appears in improved."""
    p = _make_tool()
    # Original body is 1 line — under the >= 4 threshold.
    existing = (
        "def stub():\n"
        "    pass\n"
    )
    improved = (
        "def stub() -> None:\n"
        "    \"\"\"Stub function.\"\"\"\n"
        "    ...\n"
    )
    out = p.validate_suggestion_does_not_truncate_body(_suggestion(existing, improved))
    assert out.get("score") != 0, "tiny stub was wrongly rejected"


def test_accepts_long_function_with_ellipsis_when_no_truncation():
    """If the improved code preserves line count, `...` is fine."""
    p = _make_tool()
    existing = (
        "def big():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = 3\n"
        "    d = 4\n"
        "    e = 5\n"
    )
    improved = (
        "def big() -> None:\n"
        "    \"\"\"Big function.\"\"\"\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = 3\n"
        "    d = 4\n"
        "    ...  # intentionally left as TODO\n"
    )
    out = p.validate_suggestion_does_not_truncate_body(_suggestion(existing, improved))
    assert out.get("score") != 0, "long function with embedded '...' was wrongly rejected"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_accepts_when_existing_code_is_empty():
    p = _make_tool()
    out = p.validate_suggestion_does_not_truncate_body(_suggestion("", "def foo(): ...\n"))
    assert out.get("score") != 0


def test_accepts_when_improved_code_is_empty():
    p = _make_tool()
    out = p.validate_suggestion_does_not_truncate_body(_suggestion("def foo():\n    ...\n", ""))
    assert out.get("score") != 0


def test_rejects_only_when_all_three_conditions_match():
    """If only one of the three conditions holds, must NOT reject.
    Three conditions: orig_lines>=4, ellipsis_count>=1, len(new)<=60%."""
    p = _make_tool()
    # Condition 1 holds (orig 5 lines), but no ellipsis in improved.
    existing = (
        "def foo():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = 3\n"
        "    d = 4\n"
    )
    improved = (
        "def foo() -> None:\n"
        "    \"\"\"Foo.\"\"\"\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = 3\n"
        "    d = 4\n"
    )
    out = p.validate_suggestion_does_not_truncate_body(_suggestion(existing, improved))
    assert out.get("score") != 0


def test_does_not_mutate_suggestion_on_passing_cases():
    p = _make_tool()
    existing = "def f():\n    return 1\n"
    improved = "def f() -> int:\n    \"\"\"Return 1.\"\"\"\n    return 1\n"
    s = _suggestion(existing, improved)
    p.validate_suggestion_does_not_truncate_body(s)
    assert "score" not in s
    assert "score_why" not in s
