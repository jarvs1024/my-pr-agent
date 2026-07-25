"""Weekly-report configuration loader.

Reads the ``[weekly_report]`` block from the existing Dynaconf settings
plus ``PR_AGENT_WEEKLY_*`` environment variables. Reuses the project-wide
``pr_agent.config_loader.get_settings()`` so behaviour matches the rest
of the codebase (config merging order, env override precedence, secrets).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from pr_agent.config_loader import get_settings


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class WeeklyReportConfig:
    """Resolved configuration for the weekly-report module."""

    enabled: bool = False
    target_project_id: int = 0
    cron: str = "0 9 * * 1"
    timezone: str = "Asia/Shanghai"
    collectors: tuple[str, ...] = ("telemetry", "master_merges", "repo_scan")
    notifier: str = "dingtalk"
    llm_model: str = ""
    llm_dry_run: bool = False
    repo_clone_dir: str = "/var/lib/pr-agent/repo_scan_cache"
    dingtalk_webhook_env: str = "DINGTALK_WEEKLY_WEBHOOK_URL"
    dingtalk_secret_env: str = "DINGTALK_WEEKLY_SECRET"
    dingtalk_dry_run: bool = False
    dingtalk_retry_attempts: int = 3
    diff_token_limit: int = 50000
    markdown_chunk_limit: int = 18000
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def dingtalk_webhook_url(self) -> str:
        return os.environ.get(self.dingtalk_webhook_env, "") or ""

    @property
    def dingtalk_secret(self) -> str:
        return os.environ.get(self.dingtalk_secret_env, "") or ""

    @property
    def effective_llm_model(self) -> str:
        if self.llm_model:
            return self.llm_model
        try:
            cfg = get_settings()
            model = getattr(cfg, "config", None)
            if model is not None:
                m = getattr(model, "model", None)
                if m:
                    return str(m)
        except Exception:
            pass
        return "gpt-4o-mini"


def load_config() -> WeeklyReportConfig:
    """Resolve config from settings + env.

    Settings under ``[weekly_report]`` are the canonical defaults. Env
    variables prefixed ``PR_AGENT_WEEKLY_*`` override the settings.
    """
    block: dict[str, Any] = {}
    try:
        cfg = get_settings()
        block = dict(cfg.get("weekly_report", {}) or {})
    except Exception:
        block = {}

    enabled_raw = os.environ.get("PR_AGENT_WEEKLY_ENABLED")
    if enabled_raw is None:
        enabled = bool(block.get("enabled", False))
    else:
        enabled = enabled_raw.strip().lower() in {"1", "true", "yes", "on"}

    target_raw = os.environ.get("PR_AGENT_WEEKLY_TARGET_PROJECT_ID")
    if target_raw is None or target_raw == "":
        target_project_id = int(block.get("target_project_id", 0) or 0)
    else:
        target_project_id = int(target_raw)

    collectors_raw = block.get("collectors", ["telemetry", "master_merges", "repo_scan"])
    if isinstance(collectors_raw, str):
        collectors = tuple(c.strip() for c in collectors_raw.split(",") if c.strip())
    else:
        collectors = tuple(str(c) for c in collectors_raw)

    # Derive repo_clone_dir from PR_AGENT_DATA_DIR when caller didn't override.
    _default_clone = block.get("repo_clone_dir")
    if not _default_clone:
        _dd = os.environ.get("PR_AGENT_DATA_DIR", "/var/lib/pr-agent")
        _default_clone = f"{_dd.rstrip('/')}/repo_scan_cache"

    return WeeklyReportConfig(
        enabled=enabled,
        target_project_id=target_project_id,
        cron=_env_str("PR_AGENT_WEEKLY_CRON", str(block.get("cron", "0 9 * * 1"))),
        timezone=_env_str("PR_AGENT_WEEKLY_TIMEZONE", str(block.get("timezone", "Asia/Shanghai"))),
        collectors=collectors,
        notifier=_env_str("PR_AGENT_WEEKLY_NOTIFIER", str(block.get("notifier", "dingtalk"))),
        llm_model=_env_str("PR_AGENT_WEEKLY_LLM_MODEL", str(block.get("llm_model", ""))),
        llm_dry_run=_env_bool("PR_AGENT_WEEKLY_LLM_DRY_RUN", bool(block.get("llm_dry_run", False))),
        repo_clone_dir=_env_str(
            "PR_AGENT_WEEKLY_REPO_CLONE_DIR",
            str(_default_clone),
        ),
        dingtalk_webhook_env=_env_str(
            "PR_AGENT_WEEKLY_DINGTALK_WEBHOOK_ENV",
            str(block.get("dingtalk_webhook_env", "DINGTALK_WEEKLY_WEBHOOK_URL")),
        ),
        dingtalk_secret_env=_env_str(
            "PR_AGENT_WEEKLY_DINGTALK_SECRET_ENV",
            str(block.get("dingtalk_secret_env", "DINGTALK_WEEKLY_SECRET")),
        ),
        dingtalk_dry_run=_env_bool(
            "PR_AGENT_WEEKLY_DINGTALK_DRY_RUN",
            bool(block.get("dingtalk_dry_run", False)),
        ),
        dingtalk_retry_attempts=_env_int(
            "PR_AGENT_WEEKLY_DINGTALK_RETRY",
            int(block.get("dingtalk_retry_attempts", 3)),
        ),
        diff_token_limit=_env_int(
            "PR_AGENT_WEEKLY_DIFF_TOKEN_LIMIT",
            int(block.get("diff_token_limit", 50000)),
        ),
        markdown_chunk_limit=_env_int(
            "PR_AGENT_WEEKLY_MARKDOWN_CHUNK_LIMIT",
            int(block.get("markdown_chunk_limit", 18000)),
        ),
        extra={k: v for k, v in block.items() if k not in {
            "enabled", "target_project_id", "cron", "timezone", "collectors",
            "notifier", "llm_model", "llm_dry_run", "repo_clone_dir",
            "dingtalk_webhook_env", "dingtalk_secret_env", "dingtalk_dry_run",
            "dingtalk_retry_attempts", "diff_token_limit", "markdown_chunk_limit",
        }},
    )
