"""Minimal runtime i18n helper.

pr-agent already supports ``config.response_language`` to drive LLM output
language. This module translates the hardcoded UI strings (status messages,
headers) that are rendered by Python code.

Adding a new locale: add a new entry to ``_STRINGS`` keyed by the same prefix
that ``response_language`` uses (case-insensitive). Locale matching is done
prefix-based so ``zh-CN``, ``zh-TW`` and ``zh`` all map to the ``zh`` block.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from pr_agent.config_loader import get_settings


_STRINGS: Dict[str, Dict[str, str]] = {
    "zh": {
        # /improve status messages and overview header
        "pr_code_suggestions.header": "## PR 代码建议 ✨",
        "pr_code_suggestions.no_suggestions": "本次 MR 未发现需要改进的代码建议。",
        "pr_code_suggestions.intro_auto": "浏览以下可选的代码建议：",
        "pr_code_suggestions.failed": "生成代码建议失败, 请查看服务日志。",
        "pr_code_suggestions.thinking": "正在生成代码建议…",
        # Inline score-details block in summarized mode
        "suggestion.why_prefix": "理由",
    },
}


def _locale_key() -> str:
    """Return the active locale key (e.g. ``"zh"``) or ``"en"`` if unset/non-Chinese."""
    try:
        lang = str(get_settings().config.get("response_language", "") or "")
    except Exception:
        return "en"
    lang = lang.strip().lower()
    if not lang:
        return "en"
    if lang.startswith("zh"):
        return "zh"
    return "en"


def t(key: str, default: Optional[str] = None, **fmt: Any) -> str:
    """Look up a translated string.

    Falls back to ``default`` (or the key itself) when the active locale is
    English or the key is missing. Format placeholders use ``str.format`` and
    are applied to whichever string is returned.
    """
    locale = _locale_key()
    if locale == "en":
        raw = default if default is not None else key
    else:
        raw = _STRINGS.get(locale, {}).get(key)
        if raw is None:
            raw = default if default is not None else key
    if fmt and raw:
        try:
            return raw.format(**fmt)
        except (KeyError, IndexError):
            return raw
    return raw
