"""Tests for the per-rule diff-location evidence rendered by /improve.

When the LLM silently drops an AGENTS.md rule, the uncovered-checklist
should list *concrete* (file:line + snippet) hints instead of just the rule
key. The behaviors covered here are:

  * ``_rule_key_tokens`` ignores stop-words and yields useful anchors.
  * ``_scan_diff_for_rule_locations`` parses ``@@`` + ``+++ b/`` headers and
    returns ``+``-line hits matching ``>= 2`` tokens (or the only token for
    single-token rules).
  * ``render_uncovered_details`` emits the ``📍`` per-rule evidence rows
    when ``diff_text`` is supplied, and stays silent on missing-file rules.
"""

from __future__ import annotations

import importlib

import pytest


mod = importlib.import_module("pr_agent.algo.improve_coverage")


SAMPLE_DIFF = """\
diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -10,3 +10,11 @@ def foo():
     pass

+def parse_records(records):
+    log = []
+    for r in records:
+        try:
+            values = r.fetch()
+        except Exception:
+            pass
+        log.append(values)
+    return log
"""

SAMPLE_DIFF_CLEAN = """\
diff --git a/clean.py b/clean.py
--- a/clean.py
+++ b/clean.py
@@ -1,1 +1,3 @@
+# nothing relevant here
+x = 1
"""




def test_rule_key_tokens_strips_stopwords():
    assert mod._rule_key_tokens("SSD-RULE-NO-LOG-EXC") == ["LOG", "EXC"]
    assert mod._rule_key_tokens("SSD-RULE-DOCSTRING-REQUIRED") == ["DOCSTRING"]
    assert mod._rule_key_tokens("SSD-RULE-TYPEHINTS") == ["TYPEHINTS"]
    # Stop-words are dropped so a rule with only stop-words yields no anchors.
    assert mod._rule_key_tokens("SSD-RULE-NO-FORBIDDEN") == []


def test_scan_diff_finds_log_exc_violation():
    hits = mod._scan_diff_for_rule_locations(SAMPLE_DIFF, "SSD-RULE-NO-LOG-EXC")
    assert hits, "expected at least one hit for log/exc keywords"
    assert all("file" in h and "line" in h and "snippet" in h for h in hits)
    # The first `except Exception:` line is the headline violation.
    files = {h["file"] for h in hits}
    assert files == {"app.py"}


def test_scan_diff_docstring_and_typehint_rules_are_unscannable():
    # DOCSTRING / TYPEHINTS keywords do not appear verbatim in code lines,
    # so the fuzzy anchor can't surface them; rules gracefully report zero
    # hits instead of raising.
    assert mod._scan_diff_for_rule_locations(SAMPLE_DIFF, "SSD-RULE-DOCSTRING-REQUIRED") == []
    assert mod._scan_diff_for_rule_locations(SAMPLE_DIFF, "SSD-RULE-TYPEHINTS") == []


def test_scan_diff_returns_empty_for_clean_diff():
    hits = mod._scan_diff_for_rule_locations(SAMPLE_DIFF_CLEAN, "SSD-RULE-NO-LOG-EXC")
    assert hits == []


def test_scan_diff_respects_max_hits():
    # Patch the function constant to expose a small cap.
    capped = mod._scan_diff_for_rule_locations(SAMPLE_DIFF, "SSD-RULE-NO-LOG-EXC", max_hits=1)
    assert len(capped) == 1


def test_render_uncovered_details_lists_locations():
    body = mod.render_uncovered_details(
        ["SSD-RULE-NO-LOG-EXC"],
        total_required=5,
        diff_text=SAMPLE_DIFF,
    )
    assert "SSD-RULE-NO-LOG-EXC" in body
    assert "📍" in body, "evidence block should render with the pin marker"
    assert "app.py" in body, "evidence should point at the source file"


def test_render_uncovered_details_no_locations_falls_back():
    body = mod.render_uncovered_details(
        ["SSD-RULE-NO-LOG-EXC"],
        total_required=2,
        diff_text=SAMPLE_DIFF_CLEAN,
    )
    # No hits available — render without evidence row, no pin marker.
    assert "📍" not in body


def test_render_uncovered_details_full_clean_branch():
    # uncovered == total_required AND no diff evidence → informational branch.
    body = mod.render_uncovered_details(
        ["SSD-RULE-FORBIDDEN-COMMENT"],
        total_required=1,
        diff_text=SAMPLE_DIFF,
    )
    assert "未触发" in body or "ℹ️" in body


def test_render_uncovered_details_partial_clean_branch_omits_when_no_evidence():
    # uncovered is a proper subset of total_required AND none of those keys have
    # diff evidence — we silently return "" so reviewers don't see a misleading
    # warning about rules that didn't apply.
    body = mod.render_uncovered_details(
        ["SSD-RULE-FORBIDDEN-COMMENT"],
        total_required=2,
        diff_text=SAMPLE_DIFF,
    )
    assert body == ""


def test_render_uncovered_details_segregates_no_match_rules():
    # diff has a high-confidence hit for NO-LOG-EXC but FORBIDDEN-COMMENT
    # has no evidence → renderer shows the warning + evidence for the first,
    # and a separate "no violation evidence" list for the second so reviewers
    # know the latter is intentional, not missed.
    body = mod.render_uncovered_details(
        ["SSD-RULE-NO-LOG-EXC", "SSD-RULE-FORBIDDEN-COMMENT"],
        total_required=3,
        diff_text=SAMPLE_DIFF,
    )
    assert "📍" in body, "expected evidence markers for the high-confidence rule"
    assert "SSD-RULE-NO-LOG-EXC" in body
    assert "SSD-RULE-FORBIDDEN-COMMENT" in body
    assert "未见明确违规迹象" in body


SAMPLE_DIFF_EXC = """\
diff --git a/x.py b/x.py
--- /dev/null
+++ b/x.py
@@ -0,0 +1,8 @@
+def foo():
+    try:
+        risky_call()
+    except Exception:
+        pass
"""

SAMPLE_DIFF_COMMENT = """\
diff --git a/y.py b/y.py
--- /dev/null
+++ b/y.py
@@ -0,0 +1,3 @@
+# TODO forbidden-style comment marker
+x = 1
"""




def test_scan_diff_matches_exc_substring():
    hits = mod._scan_diff_for_rule_locations(SAMPLE_DIFF_EXC, "SSD-RULE-NO-LOG-EXC")
    assert hits, "should at least find except/Exception line"
    snippets = "\n".join(h["snippet"] for h in hits)
    # Either 'except' or 'Exception' line could match (and with MAX=5 likely both)
    assert "except" in snippets or "Exception" in snippets


def test_scan_diff_matches_comment_line():
    # FORBIDDEN-COMMENT anchors on the literal word "COMMENT" (case
    # insensitive, no word-boundary requirement for short tokens). A
    # genuinely forbidden-style comment line ``# TODO forbidden-style
    # comment marker`` should still hit the anchor because the renderer
    # treats that as a fuzzy breadcrumb, not a verdict.
    hits = mod._scan_diff_for_rule_locations(SAMPLE_DIFF_COMMENT, "SSD-RULE-FORBIDDEN-COMMENT")
    assert hits, "should hit the comment line via substring search"
    assert hits[0]["snippet"].startswith("#")


def test_renderer_surfaces_separated_evidence_block_for_combined_diff():
    required = [
        "SSD-RULE-NO-LOG-EXC",
        "SSD-RULE-DOCSTRING-REQUIRED",
        "SSD-RULE-NO-BARE-PRINT",
        "SSD-RULE-TYPEHINTS",
        "SSD-RULE-FORBIDDEN-COMMENT",
    ]
    suggestions = [
        {"suggestion_content": "引用 `SSD-RULE-DOCSTRING-REQUIRED`"},
        {"suggestion_content": "引用 `SSD-RULE-NO-BARE-PRINT`"},
        {"suggestion_content": "引用 `SSD-RULE-TYPEHINTS`"},
    ]
    uncovered = mod.compute_uncovered_rules(required, suggestions)
    body = mod.render_uncovered_details(
        uncovered, total_required=len(required),
        diff_text=SAMPLE_DIFF_EXC + SAMPLE_DIFF_COMMENT,
    )
    assert "x.py" in body
    assert "y.py" in body
    assert "📍" in body
    assert "可能违反" in body or "可能" in body
