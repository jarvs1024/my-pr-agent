"""Unit tests for the AGENTS.md-rule enforce-coverage scanner.

The scanner parses a unified diff and emits (file, line, def_name) hits for
functions that are missing a docstring or type hints. It is used as a
post-processing step after the LLM emits code_suggestions, so that
coverage of the SSD-RULE-DOCSTRING-REQUIRED / SSD-RULE-TYPEHINTS rules
is guaranteed even when the LLM only emits one representative suggestion.
"""
import re
import textwrap

from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions

# We test the two module-level helpers directly. They are pure functions
# (no I/O, no git access) so no fixtures are needed.


def _get_helpers():
    """Resolve the scanner helpers from pr_code_suggestions module."""
    from pr_agent.tools import pr_code_suggestions as mod
    scan_fn = getattr(mod, "_enforce_scan_diff_for_missing_def_attrs", None)
    enrich_fn = getattr(mod, "_enforce_enrich_docstring_findings", None)
    assert scan_fn is not None, "scanner helper not exposed; check that the patch landed"
    assert enrich_fn is not None, "enrich helper not exposed; check that the patch landed"
    return scan_fn, enrich_fn


def test_detects_missing_docstring_and_typehints():
    scan, enrich = _get_helpers()
    diff = textwrap.dedent("""\
        --- a/foo.py
        +++ b/foo.py
        @@ -0,0 +1,12 @@
        +def plain(a, b):
        +    return a + b
        +
        +def documented(x: int) -> int:
        +    \"\"\"Returns x.\"\"\"
        +    return x
        +
        +def typed_only(name):
        +    return name
        +
        +def fully_typed(name: str) -> str:
        +    return name.upper()
        """)
    findings = scan(diff)
    findings = enrich(diff, findings)
    by_name = {f["def"]: f for f in findings}

    # plain: missing docstring + missing typehints
    assert by_name["plain"]["missing_docstring"] is True
    assert by_name["plain"]["missing_typehint"] is True

    # documented: has docstring AND typehints -> both False
    assert by_name["documented"]["missing_docstring"] is False
    assert by_name["documented"]["missing_typehint"] is False

    # typed_only: missing docstring AND missing typehints
    assert by_name["typed_only"]["missing_docstring"] is True
    assert by_name["typed_only"]["missing_typehint"] is True

    # fully_typed: both False
    # fully_typed has no docstring in this test -> True
    assert by_name["fully_typed"]["missing_docstring"] is True
    assert by_name["fully_typed"]["missing_typehint"] is False


def test_ignores_unchanged_lines():
    scan, _ = _get_helpers()
    diff = textwrap.dedent("""\
        --- a/foo.py
        +++ b/foo.py
        @@ -1,3 +1,5 @@
         def existing(x):
        +    # new line inside existing function, not a def
             return x
        """)
    findings = scan(diff)
    # Only "def existing" appears as a " " line (unchanged) so should NOT appear
    assert findings == []


def test_handles_multifile_diff():
    scan, enrich = _get_helpers()
    diff = textwrap.dedent("""\
        --- a/a.py
        +++ b/a.py
        @@ -0,0 +1,3 @@
        +def f1(x):
        +    return x
        --- a/b.py
        +++ b/b.py
        @@ -0,0 +1,3 @@
        +def f2(y: int) -> int:
        +    \"\"\"Doc.\"\"\"
        +    return y
        """)
    findings = scan(diff)
    findings = enrich(diff, findings)
    by_file = {}
    for f in findings:
        by_file.setdefault(f["file"], []).append(f)
    assert "a.py" in by_file and "b.py" in by_file
    assert by_file["a.py"][0]["def"] == "f1"
    assert by_file["a.py"][0]["missing_docstring"] is True
    assert by_file["b.py"][0]["def"] == "f2"
    assert by_file["b.py"][0]["missing_docstring"] is False
    assert by_file["b.py"][0]["missing_typehint"] is False


def test_self_and_cls_are_skipped_for_typehint_check():
    scan, _ = _get_helpers()
    diff = textwrap.dedent("""\
        --- a/foo.py
        +++ b/foo.py
        @@ -0,0 +1,5 @@
        +class Foo:
        +    def method(self, x):
        +        return x
        +    @classmethod
        +    def cmk(cls, y):
        +        return y
        """)
    findings = scan(diff)
    # both methods should still be detected
    names = [f["def"] for f in findings]
    assert "method" in names
    assert "cmk" in names
    # missing_typehint should be True (x, y have no annotations)
    for f in findings:
        assert f["missing_typehint"] is True


def test_kwargs_with_defaults_are_typehint_missing():
    scan, _ = _get_helpers()
    diff = textwrap.dedent("""\
        --- a/foo.py
        +++ b/foo.py
        @@ -0,0 +1,3 @@
        +def f(x: int, y=5):
        +    return x + y
        """)
    findings = scan(diff)
    assert len(findings) == 1
    assert findings[0]["missing_typehint"] is True  # y has no annotation


def test_augment_dedups_against_llm_output():
    """If the LLM already produced a docstring suggestion for one function,
    enforce should NOT emit a duplicate skeleton for that same (file, line)
    pair. But it should still emit for the next undefended function."""
    from pr_agent.tools import pr_code_suggestions as mod

    diff = textwrap.dedent("""\
        --- a/foo.py
        +++ b/foo.py
        @@ -0,0 +1,9 @@
        +def f1(x):
        +    return x
        +
        +def f2(y):
        +    return y
        +
        +def f3(z: int) -> int:
        +    return z
        """)

    # Simulate LLM output that already covered f1 with a docstring suggestion
    data = {
        "code_suggestions": [
            {
                "relevant_file": "foo.py",
                "relevant_lines_start": 1,
                "suggestion_content": "请为 f1 添加 docstring",
                "one_sentence_summary": "添加 docstring",
                "label": "possible issue",
                "existing_code": "def f1(x):",
                "improved_code": "def f1(x):\n    '''TODO'''",
            }
        ]
    }
    rule_keys = ["SSD-RULE-DOCSTRING-REQUIRED", "SSD-RULE-TYPEHINTS"]

    out = mod._enforce_augment_suggestions(data, diff, rule_keys)
    cs = out["code_suggestions"]

    def at(line, file="foo.py"):
        return [s for s in cs
                if s["relevant_file"] == file and s["relevant_lines_start"] == line]

    # f1 has LLM docstring suggestion -> enforce should NOT emit a duplicate
    # docstring skeleton (dedup), but should still emit a typehint skeleton
    # because the LLM did not cover that.
    f1_entries = at(1)
    f1_kinds = {s.get("_enforce_kind") for s in f1_entries}
    # The LLM-emitted entry must still be present (placeholder injected).
    f1_llm = [s for s in f1_entries if s.get("_enforce_kind") is None
               or s["suggestion_content"].startswith("请为")]
    assert len(f1_llm) == 1
    f1 = f1_llm[0]
    assert "docstring" in f1["suggestion_content"].lower()
    assert "<AGENTS_MD_RULE_KEY>" in f1["suggestion_content"]
    assert f1["_enforce_kind"] == "missing_docstring"
    # Typehint skeleton also added for f1 (LLM did not cover it).
    assert "missing_typehint" in f1_kinds

    # f2 missing both docstring AND typehints -> enforce emits two skeletons
    f2_entries = at(4)
    f2_kinds = {s["_enforce_kind"] for s in f2_entries}
    assert "missing_docstring" in f2_kinds
    assert "missing_typehint" in f2_kinds

    # f3 has typehints but no docstring -> enforce emits ONLY docstring skeleton
    f3_entries = at(7)
    f3_kinds = {s["_enforce_kind"] for s in f3_entries}
    assert f3_kinds == {"missing_docstring"}


def test_augment_returns_unchanged_when_diff_empty():
    from pr_agent.tools import pr_code_suggestions as mod
    data = {"code_suggestions": [{"existing_code": "x"}]}
    out = mod._enforce_augment_suggestions(data, "", ["RULE-1"])
    assert out is data
    assert out["code_suggestions"] == [{"existing_code": "x"}]
    assert out["_repo_rule_keys"] == ["RULE-1"]


def test_rule_key_placeholder_does_not_couple_to_specific_keys():
    """Verify the skeleton text never embeds the project rule-key name.
    The placeholder is the only hint that needs to be filled in later.
    """
    from pr_agent.tools import pr_code_suggestions as mod

    diff = textwrap.dedent("""\
        --- a/x.py
        +++ b/x.py
        @@ -0,0 +1,3 @@
        +def f(x):
        +    return x
        """)
    data = {"code_suggestions": []}
    out = mod._enforce_augment_suggestions(data, diff, rule_keys=["ZZZ-RULE-WHATEVER"])
    skel = out["code_suggestions"][0]
    # Must NOT mention ZZZ-RULE-WHATEVER (decoupled from specific rule set)
    assert "ZZZ-RULE-WHATEVER" not in skel["suggestion_content"]
    # Must contain the placeholder for self-reflection to fill in
    assert "<AGENTS_MD_RULE_KEY>" in skel["suggestion_content"]


def test_get_agents_md_rule_keys_extracts_from_context():
    """_get_agents_md_rule_keys should call extract_rule_keys on the
    repo_context text and return the list. We patch both the git_provider
    path and the settings to avoid any network / FS I/O."""
    from pr_agent.tools import pr_code_suggestions as mod

    # Build a stub PRCodeSuggestions instance without running __init__.
    inst = mod.PRCodeSuggestions.__new__(mod.PRCodeSuggestions)

    class _StubGitProvider:
        pass

    inst.git_provider = _StubGitProvider()

    # Patch build_repo_context + extract_rule_keys on the module
    captured = {}

    def fake_build(provider):
        captured["provider"] = provider
        return "rules: SSD-RULE-DOCSTRING-REQUIRED, SSD-RULE-TYPEHINTS, SSD-RULE-NO-LOG-EXC"

    def fake_extract(text):
        captured["text"] = text
        return ["SSD-RULE-DOCSTRING-REQUIRED", "SSD-RULE-TYPEHINTS", "SSD-RULE-NO-LOG-EXC"]

    orig_build = mod.build_repo_context
    orig_extract = mod.extract_rule_keys
    mod.build_repo_context = fake_build
    mod.extract_rule_keys = fake_extract
    try:
        keys = inst._get_agents_md_rule_keys()
    finally:
        mod.build_repo_context = orig_build
        mod.extract_rule_keys = orig_extract

    assert captured["provider"] is inst.git_provider
    assert keys == ["SSD-RULE-DOCSTRING-REQUIRED", "SSD-RULE-TYPEHINTS", "SSD-RULE-NO-LOG-EXC"]


def test_get_agents_md_rule_keys_handles_empty_context():
    """When AGENTS.md is missing or empty, _get_agents_md_rule_keys must
    return [] without raising — enforcement still emits skeletons, just
    without the rule-key upgrade."""
    from pr_agent.tools import pr_code_suggestions as mod

    inst = mod.PRCodeSuggestions.__new__(mod.PRCodeSuggestions)
    inst.git_provider = object()

    orig_build = mod.build_repo_context
    mod.build_repo_context = lambda p: ""
    try:
        keys = inst._get_agents_md_rule_keys()
    finally:
        mod.build_repo_context = orig_build
    assert keys == []


def test_enforce_does_not_couple_to_specific_rule_prefix():
    """End-to-end: change the project's rule-key prefix from SSD to FOO
    and verify enforce still emits skeletons with the placeholder, not
    the FOO-RULE-* literal."""
    from pr_agent.tools import pr_code_suggestions as mod

    diff = textwrap.dedent("""\
        --- a/x.py
        +++ b/x.py
        @@ -0,0 +1,3 @@
        +def f(x):
        +    return x
        """)

    # Simulate the project's AGENTS.md using FOO- prefix
    data = {"code_suggestions": []}
    rule_keys = mod._extract_rule_keys_for_test = []
    # Manually extract from a fake context
    rule_keys = ["FOO-RULE-DOC", "FOO-RULE-TYPES"]

    out = mod._enforce_augment_suggestions(data, diff, rule_keys)
    skel = out["code_suggestions"][0]
    # Must NOT mention FOO-RULE-* literally (decoupled)
    assert "FOO-RULE-DOC" not in skel["suggestion_content"]
    assert "FOO-RULE-TYPES" not in skel["suggestion_content"]
    # Must contain placeholder
    assert "<AGENTS_MD_RULE_KEY>" in skel["suggestion_content"]
    # Rule keys are stamped on data for downstream prompt builders
    assert out["_repo_rule_keys"] == rule_keys


# ---------------------------------------------------------------------------
# Aggressive edge-case tests (commit 2)
# ---------------------------------------------------------------------------


def test_no_agents_md_still_emits_skeletons():
    """Even with an empty rule_keys list (no AGENTS.md), enforce must still
    emit skeletons with the placeholder. This proves the language-level
    detector does not depend on AGENTS.md content existing at all."""
    from pr_agent.tools import pr_code_suggestions as mod

    diff = textwrap.dedent("""\
        --- a/foo.py
        +++ b/foo.py
        @@ -0,0 +1,6 @@
        +def foo(x):
        +    return x
        +
        +def bar(y):
        +    return y
        """)
    out = mod._enforce_augment_suggestions(
        {"code_suggestions": []}, diff, rule_keys=[],
    )
    kinds = {s["_enforce_kind"] for s in out["code_suggestions"]}
    # 2 defs * 2 kinds = 4 skeletons
    assert len(out["code_suggestions"]) == 4
    assert kinds == {"missing_docstring", "missing_typehint"}
    # All carry the placeholder so self-reflection can still attempt citation
    for s in out["code_suggestions"]:
        assert "<AGENTS_MD_RULE_KEY>" in s["suggestion_content"]
    # rule_keys (empty) is still stamped on data
    assert out["_repo_rule_keys"] == []


def test_renamed_prefix_does_not_break_enforce():
    """Rename the project from SSD to FROBOZZ. Detector must not know
    nor care. Skeleton text must not contain FROBOZZ literals."""
    from pr_agent.tools import pr_code_suggestions as mod

    diff = textwrap.dedent("""\
        --- a/foo.py
        +++ b/foo.py
        @@ -0,0 +1,3 @@
        +def foo(x):
        +    return x
        """)
    out = mod._enforce_augment_suggestions(
        {"code_suggestions": []}, diff, rule_keys=["FROBOZZ-RULE-DOC", "FROBOZZ-RULE-TYPES"],
    )
    skel = out["code_suggestions"][0]
    assert "FROBOZZ" not in skel["suggestion_content"]
    assert "FROBOZZ" not in skel["one_sentence_summary"]
    assert "FROBOZZ" not in skel["improved_code"]
    assert "FROBOZZ" not in skel["existing_code"]
    assert "<AGENTS_MD_RULE_KEY>" in skel["suggestion_content"]
    # rule_keys are passed through verbatim for downstream consumers
    assert out["_repo_rule_keys"] == ["FROBOZZ-RULE-DOC", "FROBOZZ-RULE-TYPES"]


def test_decorated_methods_detected():
    """Methods with decorators (``@staticmethod``, ``@classmethod``) are
    still detected. The decorator line is NOT a def-line, so the next
    line containing ``def name(...)`` is what we match."""
    from pr_agent.tools import pr_code_suggestions as mod

    diff = textwrap.dedent("""\
        --- a/foo.py
        +++ b/foo.py
        @@ -0,0 +1,9 @@
        +class Foo:
        +    @staticmethod
        +    def helper(x):
        +        return x
        +
        +    @classmethod
        +    def cmk(cls, y):
        +        return y
        +
        +    async def amethod(z):
        +        return z
        """)
    findings = mod._enforce_scan_diff_for_missing_def_attrs(diff)
    findings = mod._enforce_enrich_docstring_findings(diff, findings)
    names = {f["def"] for f in findings}
    assert names == {"helper", "cmk", "amethod"}
    # All three are missing both docstring and typehints
    for f in findings:
        assert f["missing_docstring"] is True
        assert f["missing_typehint"] is True


def test_partial_typed_signature_partially_covered():
    """If a function has annotations on SOME params but not all, the
    detector must still flag it as missing-typehint."""
    from pr_agent.tools import pr_code_suggestions as mod

    diff = textwrap.dedent("""\
        --- a/foo.py
        +++ b/foo.py
        @@ -0,0 +1,3 @@
        +def partial(x: int, y):  # y lacks annotation
        +    return x, y
        """)
    findings = mod._enforce_scan_diff_for_missing_def_attrs(diff)
    assert len(findings) == 1
    assert findings[0]["missing_typehint"] is True


def test_return_typehint_present_but_params_missing_still_flagged():
    """Def has ``-> int`` but no param annotations -> still flagged."""
    from pr_agent.tools import pr_code_suggestions as mod

    diff = textwrap.dedent("""\
        --- a/foo.py
        +++ b/foo.py
        @@ -0,0 +1,3 @@
        +def f(x, y) -> int:
        +    return x + y
        """)
    findings = mod._enforce_scan_diff_for_missing_def_attrs(diff)
    assert len(findings) == 1
    assert findings[0]["missing_typehint"] is True


def test_method_inside_class_does_not_include_class_def_as_missing():
    """``class Foo:`` line should not itself be flagged. Only the def
    statements inside the class should appear in findings."""
    from pr_agent.tools import pr_code_suggestions as mod

    diff = textwrap.dedent("""\
        --- a/foo.py
        +++ b/foo.py
        @@ -0,0 +1,5 @@
        +class Foo:
        +    def m1(self):
        +        return 1
        +    def m2(self):
        +        return 2
        """)
    findings = mod._enforce_scan_diff_for_missing_def_attrs(diff)
    names = {f["def"] for f in findings}
    # "Foo" must NOT appear
    assert "Foo" not in names
    assert names == {"m1", "m2"}


def test_unicode_aware_def_name_skipped():
    """Python 3 identifiers can include unicode letters (PEP 3131). The
    regex currently requires the ASCII identifier class, so unicode-named functions are
    skipped — that's an acceptable limitation, not a crash."""
    from pr_agent.tools import pr_code_suggestions as mod

    diff = textwrap.dedent("""\
        --- a/foo.py
        +++ b/foo.py
        @@ -0,0 +1,3 @@
        +def 中文函数(x):
        +    return x
        """)
    findings = mod._enforce_scan_diff_for_missing_def_attrs(diff)
    # Currently skipped (regex limitation); if the detector is later
    # upgraded to support PEP 3131, this test should be updated.
    assert findings == []


def test_huge_diff_handled_quickly():
    """Scan a 200-function diff in well under a second."""
    import time
    from pr_agent.tools import pr_code_suggestions as mod

    lines = ["--- a/big.py", "+++ b/big.py",
             "@@ -0,0 +1,401 @@"]
    for i in range(200):
        lines.append(f"+def f{i}(x, y):")
        lines.append(f"+    return x + y")
        lines.append("+")
    diff = "\n".join(lines) + "\n"
    t0 = time.perf_counter()
    findings = mod._enforce_scan_diff_for_missing_def_attrs(diff)
    findings = mod._enforce_enrich_docstring_findings(diff, findings)
    elapsed = time.perf_counter() - t0
    assert len(findings) == 200
    for f in findings:
        assert f["missing_docstring"] is True
        assert f["missing_typehint"] is True
    assert elapsed < 1.0, f"scan took {elapsed:.2f}s for 200 defs"


def test_does_not_touch_existing_llm_suggestion_when_kind_does_not_match():
    """If LLM emits a suggestion whose kind does NOT match any enforceable
    pattern (e.g. a refactor suggestion), enforce must not add the
    placeholder suffix to it."""
    from pr_agent.tools import pr_code_suggestions as mod

    data = {
        "code_suggestions": [{
            "relevant_file": "x.py",
            "relevant_lines_start": 10,
            "suggestion_content": "建议将魔术数字提取为常量",
            "one_sentence_summary": "提取常量",
            "label": "possible issue",
            "existing_code": "...",
            "improved_code": "...",
        }]
    }
    mod._enforce_inject_placeholders_into_existing(data)
    s = data["code_suggestions"][0]
    # Content unchanged
    assert s["suggestion_content"] == "建议将魔术数字提取为常量"
    assert "<AGENTS_MD_RULE_KEY>" not in s["suggestion_content"]
    assert s.get("_enforce_kind") is None


def test_augment_skips_when_diff_text_is_whitespace_only():
    """A diff with only hunks but no ``+`` lines should produce no
    skeletons (nothing to enforce against)."""
    from pr_agent.tools import pr_code_suggestions as mod

    diff = textwrap.dedent("""\
        --- a/foo.py
        +++ b/foo.py
        @@ -1,2 +1,2 @@
         unchanged context
        """)
    out = mod._enforce_augment_suggestions(
        {"code_suggestions": []}, diff, rule_keys=["ANY-RULE"],
    )
    assert out["code_suggestions"] == []
    # rule_keys still stamped on data even though nothing matched
    assert out["_repo_rule_keys"] == ["ANY-RULE"]


def test_extract_rule_keys_handles_multiple_projects():
    """Verify that rule_keys are passed through as-is so the same enforce
    code can serve projects with completely different rule conventions.
    This is the contract test for the whole '通用' requirement."""
    from pr_agent.tools import pr_code_suggestions as mod

    diff = textwrap.dedent("""\
        --- a/foo.py
        +++ b/foo.py
        @@ -0,0 +1,3 @@
        +def f(x):
        +    return x
        """)

    for rule_set in [
        [],                                          # no rules at all
        ["SSD-RULE-DOCSTRING-REQUIRED"],             # single rule
        ["XYZ-RULE-NO-DOC"],                         # different prefix
        ["COMPANY-A-RULE-TYPEHINT", "PERSONAL-RULE-X"],  # mixed
        ["日本語-RULE-特殊"],                          # unicode rule key
        ["a" * 200],                                 # absurdly long rule key
    ]:
        out = mod._enforce_augment_suggestions(
            {"code_suggestions": []}, diff, rule_keys=rule_set,
        )
        skel = out["code_suggestions"][0]
        # None of the literal rule keys from the input should appear in the
        # skeleton text. The placeholder is the only rule-key reference.
        for k in rule_set:
            if len(k) > 5:  # avoid matching against <AGENTS_MD_RULE_KEY> itself
                assert k not in skel["suggestion_content"], (
                    f"rule key {k!r} leaked into skeleton text"
                )
        assert "<AGENTS_MD_RULE_KEY>" in skel["suggestion_content"]
        assert out["_repo_rule_keys"] == rule_set


# ---------------------------------------------------------------------------
# Realistic existing_code/improved_code from diff hunks
# ---------------------------------------------------------------------------

def test_skeleton_existing_code_matches_diff():
    """The skeleton ``existing_code`` must be reconstructed from the actual
    diff content (def header + first body lines), not a hard-coded
    ``def example(x):`` placeholder.
    """
    from pr_agent.tools.pr_code_suggestions import _enforce_augment_suggestions
    diff_text = (
        "diff --git a/bug_buried.py b/bug_buried.py\n"
        "--- a/bug_buried.py\n"
        "+++ b/bug_buried.py\n"
        "@@ -1,2 +1,7 @@\n"
        "+import logging\n"
        "+def record_latency(name, ms):\n"
        "+    bucket = name.split(\".\")[0]\n"
        "+    print(bucket, ms)\n"
    )
    data = {"code_suggestions": []}
    _enforce_augment_suggestions(data, diff_text, ["SSD-RULE-DOCSTRING-REQUIRED"])
    assert data["code_suggestions"], "expected at least one skeleton"
    skel = data["code_suggestions"][0]
    # existing_code must contain the real def + body, not a placeholder
    assert "def example" not in skel["existing_code"]
    assert "def record_latency(name, ms):" in skel["existing_code"]
    assert "print(bucket, ms)" in skel["existing_code"]


def test_skeleton_typehint_uses_any_for_unannotated_params():
    """For missing_typehint the improved code should annotate every
    parameter with ``: Any`` if the original had no annotation.
    """
    from pr_agent.tools.pr_code_suggestions import _enforce_augment_suggestions
    diff_text = (
        "diff --git a/bug_buried.py b/bug_buried.py\n"
        "--- a/bug_buried.py\n"
        "+++ b/bug_buried.py\n"
        "@@ -1,2 +1,6 @@\n"
        "+def record_latency(name, ms):\n"
        "+    bucket = name.split(\".\")[0]\n"
    )
    data = {"code_suggestions": []}
    _enforce_augment_suggestions(data, diff_text, ["SSD-RULE-TYPEHINTS"])
    typehint = [s for s in data["code_suggestions"] if s.get("_enforce_kind") == "missing_typehint"]
    assert len(typehint) == 1
    improved = typehint[0]["improved_code"]
    assert "name: Any" in improved
    assert "ms: Any" in improved
    assert "-> None" in improved


def test_skeleton_handles_def_without_body_lines():
    """If the diff hunk ends right after ``def foo():`` (no body),
    the skeleton should still produce a valid existing_code snippet
    using ``pass`` as a placeholder body.
    """
    from pr_agent.tools.pr_code_suggestions import _enforce_augment_suggestions
    diff_text = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,2 +1,4 @@\n"
        "+def stub(x):\n"
    )
    data = {"code_suggestions": []}
    _enforce_augment_suggestions(data, diff_text, ["SSD-RULE-DOCSTRING-REQUIRED"])
    skel = data["code_suggestions"][0]
    assert "def stub(x):" in skel["existing_code"]
    # body should fall back to ``pass``
    assert "pass" in skel["existing_code"]


def test_skeleton_does_not_inject_None_for_missing_return_annotation():
    """When the def has no return annotation, the skeleton header
    must not end up rendering ``def foo(x)None:`` — that was the
    pre-fix bug where ``f"{ret}"`` rendered ``None`` literally.
    """
    from pr_agent.tools.pr_code_suggestions import _enforce_augment_suggestions
    diff_text = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,2 +1,4 @@\n"
        "+def stub(x):\n"
        "+    return x\n"
    )
    data = {"code_suggestions": []}
    _enforce_augment_suggestions(data, diff_text, ["SSD-RULE-DOCSTRING-REQUIRED"])
    skel = data["code_suggestions"][0]
    assert ")None:" not in skel["existing_code"]
    # Header line keeps its trailing colon even when body lines are
    # spliced in below; the resulting snippet's first line is the def header.
    assert skel["existing_code"].splitlines()[0].endswith("):")
