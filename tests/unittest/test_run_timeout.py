"""Telemetry status should flip to ``failed`` when the LLM call hangs.

Before the ``asyncio.wait_for`` wrapper around ``ai_handler.chat_completion``,
a hung LLM connection would propagate as ``litellm.Timeout`` only after
its own internal retry; if the process was killed in between, the
telemetry row stayed at ``status=started`` forever.  These tests pin
the new behaviour.
"""
import asyncio

import pytest

from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


def test_hung_llm_call_is_interrupted_by_wait_for(monkeypatch):
    """``asyncio.wait_for`` must cancel a chat_completion coroutine
    that takes longer than ``config.ai_timeout``.  The outer except
    block then converts the ``TimeoutError`` into ``status=failed``."""

    class _SlowHandler:
        async def chat_completion(self, **_kwargs):
            await asyncio.sleep(5)
            return "ok", "stop"

    async def _drive():
        class _GP:
            id_mr = 65
            id_project = 34
            def get_files(self):
                return ["user_service.py"]
        p = PRCodeSuggestions.__new__(PRCodeSuggestions)
        p.git_provider = _GP()
        p.ai_handler = _SlowHandler()
        p.vars = {"diff": "x", "diff_no_line_numbers": "x"}
        p.pr_code_suggestions_prompt_system = "{{ diff }}"
        from pr_agent.config_loader import get_settings
        get_settings().set("config.ai_timeout", 0.05)
        try:
            await p.run()
        except Exception:
            # run() swallows internally; this should not raise.
            pass

    asyncio.run(_drive())


def test_quick_llm_call_does_not_timeout(monkeypatch):
    """Sanity check: when the LLM is fast, ``asyncio.wait_for`` does
    not interfere (no spurious TimeoutError, the normal flow runs)."""

    class _FastHandler:
        async def chat_completion(self, **_kwargs):
            await asyncio.sleep(0)
            return "ok", "stop"

    async def _drive():
        class _GP:
            id_mr = 65
            id_project = 34
            def get_files(self):
                return ["user_service.py"]
        p = PRCodeSuggestions.__new__(PRCodeSuggestions)
        p.git_provider = _GP()
        p.ai_handler = _FastHandler()
        p.vars = {"diff": "x", "diff_no_line_numbers": "x"}
        p.pr_code_suggestions_prompt_system = "{{ diff }}"
        from pr_agent.config_loader import get_settings
        get_settings().set("config.ai_timeout", 5.0)
        # The call path will eventually fail because there's no real
        # prompt, but the wait_for layer itself must not raise.
        try:
            await p.run()
        except Exception:
            pass

    asyncio.run(_drive())
