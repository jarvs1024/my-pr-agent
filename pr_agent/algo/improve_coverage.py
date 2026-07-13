"""Compute which AGENTS.md rule keys are NOT cited by /improve LLM output.

Used by :mod:`pr_agent.tools.pr_code_suggestions` to inject an `<details>`
checklist into the persistent review body so that human reviewers can spot
rules that the LLM silently dropped. DiffNote generation is intentionally
NOT done here: an empty ``improved_code`` would be filtered out by
``_prepare_pr_code_suggestions`` and a TODO-only suggestion would produce a
useless commit if accepted.
"""

from __future__ import annotations

from typing import Iterable, Mapping

from pr_agent.algo.repo_context import RULE_KEY_PATTERN


def _scan_text(text: str) -> set[str]:
    """Return every rule key found in ``text`` (de-duplicated)."""
    if not text:
        return set()
    return {m.group(1) for m in RULE_KEY_PATTERN.finditer(text)}


def _collect_cited_keys(suggestions: Iterable[Mapping] | None) -> set[str]:
    """Union the rule keys cited anywhere in a suggestion payload."""
    cited: set[str] = set()
    for s in suggestions or []:
        if not isinstance(s, Mapping):
            continue
        for field in ("suggestion_content", "one_sentence_summary", "improved_code"):
            cited |= _scan_text(str(s.get(field) or ""))
    return cited


def compute_uncovered_rules(
    required_rules: Iterable[str] | None,
    suggestions: Iterable[Mapping] | None,
) -> list[str]:
    """Return the subset of ``required_rules`` not cited by any suggestion.

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
) -> str:
    """Render the checklist block; empty string when nothing to show.

    When ``uncovered`` covers every required rule and the LLM produced zero
    suggestions (``uncovered == total_required``), the diff likely doesn't
    violate any rule — surface that as an informational note rather than a
    warning so reviewers don't read a green MR as a fault.
    """
    keys = list(uncovered or [])
    if not keys or not total_required:
        return ""

    items = "\n".join(f"- `{k}`" for k in keys)
    if len(keys) == total_required:
        # LLM gave no suggestions AND no rule keys were cited → diff is clean
        # of AGENTS.md violations. Don't make it look like a failed review.
        return (
            "\n\n<details>\n"
            f"<summary>ℹ️ 规则覆盖: 本次 diff 未触发 {total_required} 条 AGENTS.md 规则中的任何一条</summary>\n\n"
            "本仓库 AGENTS.md / .agents/rules/ 中定义的所有规则键本次都没有违规迹象, "
            "机器人也不会给出针对性的 Apply 建议:\n\n"
            f"{items}\n\n"
            "如果你认为 diff 里**确实**违反了某条规则, 请人工补一条对应建议或重跑 `/improve`.\n\n"
            "</details>\n"
        )

    return (
        "\n\n<details>\n"
        f"<summary>⚠️ 规则覆盖检查: {len(keys)} 条 AGENTS.md 规则未在本次 /improve 的 Apply 建议里被引用</summary>\n\n"
        "下列规则键在本仓库的 AGENTS.md / .agents/rules/ 中定义, 但本次评审里没有对应的可应用代码建议:\n\n"
        f"{items}\n\n"
        "可能原因: LLM 选择了更显眼的通用 bug 作为建议, 或当前 diff 不存在该类违规. "
        "如确实有违规未覆盖, 请人工补一条 Apply 建议或重跑 `/improve`.\n\n"
        "</details>\n"
    )
