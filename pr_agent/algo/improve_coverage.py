"""Compute which AGENTS.md rule keys are NOT cited by /improve LLM output.

Used by :mod:`pr_agent.tools.pr_code_suggestions` to inject an `<details>`
checklist into the persistent review body so that human reviewers can spot
rules that the LLM silently dropped. DiffNote generation is intentionally
NOT done here: an empty ``improved_code`` would be filtered out by
``_prepare_pr_code_suggestions`` and a TODO-only suggestion would produce a
useless commit if accepted.

When a unified diff text is supplied, the renderer can additionally point
reviewers at the *lines* in the diff where each missing rule might apply.
We use the rule key's keyword fragments (e.g. ``SSD-RULE-NO-LOG-EXC`` →
``LOG``, ``EXC``) as fuzzy anchors: a ``+`` line that contains at least one
keyword is treated as a potential violation site. This gives reviewers a
concrete breadcrumb (file:line + snippet) when the LLM dropped a rule,
instead of a bare list of rule names.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Optional

from pr_agent.algo.repo_context import _rule_key_pattern


# Stop words that appear in rule keys but match too much of any code diff.
# Excluding them keeps the fuzzy anchor narrow enough to be useful.
_RULE_KEY_STOPWORDS = frozenset({
    # Connectors / modal verbs that show up in every rule name
    "NO", "REQUIRED", "MUST", "NOT", "FORBIDDEN",
    "ENABLE", "AVOID",
})


def _rule_key_tokens(rule_key: str) -> list[str]:
    """Extract fuzzy anchor tokens from a single rule key.

    The convention ``<PREFIX>-RULE-<verb>-<keyword1>-<keyword2>`` is
    fixed by ``pr_agent.algo.repo_context``; we drop the leading
    ``<PREFIX>-RULE-`` part and any stop-words, keeping the keyword
    fragments as fuzzy anchors. Examples::

        SSD-RULE-NO-LOG-EXC        -> ["LOG", "EXC"]
        SSD-RULE-TYPEHINTS         -> ["TYPEHINTS"]
        SSD-RULE-DOCSTRING-REQUIRED -> ["DOCSTRING"]
        SSD-RULE-NO-BARE-PRINT     -> ["BARE", "PRINT"]
        SSD-RULE-NO-FORBIDDEN      -> []    # no anchor after stripping
    """
    parts = rule_key.upper().split("-")
    # drop the project prefix segment + the literal "RULE" segment
    tail = parts[2:] if len(parts) >= 3 else parts
    return [t for t in tail if t and t not in _RULE_KEY_STOPWORDS]


def _scan_text(text: str) -> set[str]:
    """Return every rule key found in ``text`` (de-duplicated).

    Uses the configurable prefix from ``config.rule_key_prefix`` so projects
    with non-ZLG prefixes (e.g. ``SSD-RULE-*``) get correct coverage checks.
    """
    if not text:
        return set()
    return {m.group(1) for m in _rule_key_pattern().finditer(text)}


def _collect_cited_keys(suggestions: Iterable[Mapping] | None) -> set[str]:
    """Union the rule keys cited anywhere in a suggestion payload."""
    cited: set[str] = set()
    for s in suggestions or []:
        if not isinstance(s, Mapping):
            continue
        for field in ("suggestion_content", "one_sentence_summary", "improved_code"):
            cited |= _scan_text(str(s.get(field) or ""))
    return cited


def _scan_diff_for_rule_locations(
    diff_text: Optional[str],
    rule_key: str,
    *,
    max_hits: int = 5,
) -> list[dict]:
    """Walk a unified diff and return potential violation sites for ``rule_key``.

    Returns a list of ``{"file": str, "line": int, "snippet": str}`` up to
    ``max_hits`` entries. Each +line in the diff whose text contains at
    least one keyword token (from the rule key) is recorded. Empty hits
    list means the diff appears clean for this rule.

    A line is only a *hit* if it contains at least one full keyword in a
    word boundary (so ``LOG`` does not match ``LOGIN``). Rules whose
    anchor token list is empty (e.g. ``<PREFIX>-RULE-NO-FORBIDDEN``) are
    treated as unscannable and return ``[]`` — the renderer falls back to
    listing the key without evidence.
    """
    if not diff_text or not rule_key:
        return []
    tokens = _rule_key_tokens(rule_key)
    if not tokens:
        return []
    # Even a single-anchor rule is enough of a hint on its own.
    required_hits = 1
    # Word-boundary matching is too strict for short fragments: ``EXC``
    # would never match ``except`` / ``Exception`` and ``LOG`` would skip
    # ``logging``. We only require a real alphanumeric boundary (start of
    # line or preceded by a non-word char) for tokens that are long enough
    # to be unambiguous, and fall back to plain substring for the rest.
    parts = []
    for t in tokens:
        if len(t) >= 5:
            parts.append(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])")
        else:
            parts.append(re.escape(t))
    needle_re = re.compile("|".join(parts), re.IGNORECASE)

    current_file: Optional[str] = None
    plus_offset: Optional[int] = None
    hits: list[dict] = []
    for line in diff_text.splitlines():
        m_file = re.match(r"^\+\+\+ b/(.+)$", line)
        if m_file:
            current_file = m_file.group(1)
            continue
        m_hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if m_hunk:
            plus_offset = int(m_hunk.group(1))
            continue
        if not line.startswith("+") or line.startswith("+++"):
            # context / removed line / file header
            continue
        if plus_offset is None or current_file is None:
            continue
        body = line[1:].strip()
        if not body:
            plus_offset += 1
            continue
        matched = needle_re.findall(body)
        if len({m.upper() for m in matched}) >= required_hits:
            hits.append({
                "file": current_file,
                "line": int(plus_offset),
                "snippet": body[:160],
            })
            if len(hits) >= max_hits:
                break
        plus_offset += 1
    return hits


def compute_uncovered_rules(
    required_rules: Iterable[str] | None,
    suggestions: Iterable[Mapping] | None,
    *,
    diff_text: Optional[str] = None,
) -> list[str]:
    """Return the subset of ``required_rules`` not cited by any suggestion.

    ``diff_text`` is accepted for API symmetry with ``render_uncovered_details``;
    callers that want per-rule violation sites should use
    :func:`render_uncovered_details` with the same ``diff_text`` argument.
    Order follows ``required_rules`` so callers can render a stable checklist.
    """
    required = list(required_rules or [])
    if not required:
        return []
    cited = _collect_cited_keys(suggestions)
    return [k for k in required if k not in cited]


def render_uncovered_details(
    uncovered: Iterable[str] | None,
    total_required: int = 0,
    *,
    diff_text: Optional[str] = None,
) -> str:
    """Render the checklist block; empty string when nothing to show.

    When ``uncovered`` covers every required rule and the LLM produced zero
    suggestions (``uncovered == total_required``), the diff likely doesn't
    violate any rule — surface that as an informational note rather than a
    warning so reviewers don't read a green MR as a fault.

    When ``diff_text`` is supplied, each uncovered rule is enriched with
    concrete diff locations (``file:line`` + snippet) so reviewers can see
    *where* the violation might live instead of just the rule name.

    Critical UX rule: a rule that has no violation evidence in the diff is
    not a defect — it just means the rule did not apply. We surface it in a
    separate "no-violation-evidence" sub-list (or omit the block entirely
    when every uncovered rule falls in that bucket) so reviewers don't
    mistake "rule silently dropped" for "rule violated, no suggestion".
    """
    keys = list(uncovered or [])
    if not keys or not total_required:
        return ""

    evidence: dict[str, list[dict]] = {}
    no_match: list[str] = []
    if diff_text:
        for k in keys:
            hits = _scan_diff_for_rule_locations(diff_text, k)
            if hits:
                evidence[k] = hits
            else:
                no_match.append(k)
    else:
        no_match = list(keys)

    def _render_items(items: list[str]) -> str:
        lines: list[str] = []
        for k in items:
            lines.append(f"- `{k}`")
            for hit in evidence.get(k, []):
                loc = f"{hit['file']}:{hit['line']}"
                snippet = hit["snippet"].replace("|", "\\|")
                lines.append(f"    - 📍 `{loc}` — `{snippet}`")
        return "\n".join(lines)

    flagged = [k for k in keys if k in evidence]

    # Case 1: every uncovered rule has zero evidence in the diff → no warning.
    if not flagged:
        if len(keys) == total_required:
            return (
                "\n\n<details>\n"
                f"<summary>ℹ️ 规则覆盖: 本次 diff 未触发 {total_required} 条 AGENTS.md 规则中的任何一条</summary>\n\n"
                "本仓库 AGENTS.md / .agents/rules/ 中定义的所有规则键本次都没有违规迹象, "
                "机器人也不会给出针对性的 Apply 建议:"
                + ("" if not no_match else "\n\n" + _render_items(no_match))
                + "\n\n如果你认为 diff 里**确实**违反了某条规则, 请人工补一条对应建议或重跑 `/improve`.\n\n"
                "</details>\n"
            )
        # Partial set, but all clean — surface a small informational footer only.
        # No change to the warning text reviewers were going to see.
        return ""

    # Case 2: at least one rule has evidence → warn with 📍 anchors AND list
    # the remaining "no-evidence" rules separately so the reviewer can see
    # they are intentionally dropped, not silently missed.
    flagged_block = (
        "下列规则键在本仓库的 AGENTS.md / .agents/rules/ 中定义, "
        "本次评审里没有对应的可应用代码建议 (📍 标注的是 diff 中**可能**违反该规则的位置, "
        "仅供人工核对, 不代表违规):"
    )
    no_match_block = (
        "另外有 {} 条规则键本 diff 未见明确违规迹象, LLM 没生成对应建议属正常:"
        .format(len(no_match))
        if no_match else ""
    )
    flagged_section = _render_items(flagged)
    no_match_section = _render_items(no_match) if no_match else ""
    return (
        "\n\n<details>\n"
        f"<summary>⚠️ 规则覆盖检查: {len(flagged)} 条 AGENTS.md 规则在 diff 中可能违反但未见对应 Apply 建议</summary>\n\n"
        f"{flagged_block}\n\n"
        f"{flagged_section}\n\n"
        + (f"{no_match_block}\n\n{no_match_section}\n\n" if no_match else "")
        + "如确实有违规未覆盖, 请人工补一条 Apply 建议或重跑 `/improve`.\n\n"
        "</details>\n"
    )
