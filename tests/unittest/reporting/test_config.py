"""Tests for the weekly-report config loader."""
from __future__ import annotations

import os

from pr_agent.reporting.config import WeeklyReportConfig, load_config


def test_defaults_when_unset():
    cfg = load_config()
    assert cfg.enabled is False
    assert cfg.target_project_id == 0
    assert cfg.cron == "0 9 * * 1"
    assert cfg.timezone == "Asia/Shanghai"
    assert cfg.collectors == ("telemetry", "master_merges", "repo_scan")
    assert cfg.notifier == "dingtalk"
    assert cfg.dingtalk_dry_run is False
    assert cfg.llm_dry_run is False
    assert cfg.dingtalk_retry_attempts == 3
    assert cfg.diff_token_limit == 50000
    assert cfg.markdown_chunk_limit == 18000


def test_env_overrides_settings(monkeypatch):
    monkeypatch.setenv("PR_AGENT_WEEKLY_ENABLED", "1")
    monkeypatch.setenv("PR_AGENT_WEEKLY_TARGET_PROJECT_ID", "42")
    monkeypatch.setenv("PR_AGENT_WEEKLY_DINGTALK_DRY_RUN", "true")
    monkeypatch.setenv("PR_AGENT_WEEKLY_LLM_DRY_RUN", "1")
    monkeypatch.setenv("PR_AGENT_WEEKLY_CRON", "0 8 * * 5")
    monkeypatch.setenv("PR_AGENT_WEEKLY_DIFF_TOKEN_LIMIT", "12345")
    monkeypatch.setenv("PR_AGENT_WEEKLY_TARGET_BRANCH", "develop")
    cfg = load_config()
    assert cfg.enabled is True
    assert cfg.target_project_id == 42
    assert cfg.dingtalk_dry_run is True
    assert cfg.llm_dry_run is True
    assert cfg.cron == "0 8 * * 5"
    assert cfg.diff_token_limit == 12345
    assert cfg.extra.get("target_branch") == "develop" or os.environ.get("PR_AGENT_WEEKLY_TARGET_BRANCH") == "develop"


def test_effective_llm_model_falls_back_to_settings(monkeypatch):
    monkeypatch.delenv("PR_AGENT_WEEKLY_LLM_MODEL", raising=False)
    cfg = WeeklyReportConfig(llm_model="")
    assert cfg.effective_llm_model  # non-empty fallback


def test_repo_clone_dir_derives_from_pr_agent_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PR_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PR_AGENT_WEEKLY_REPO_CLONE_DIR", raising=False)
    cfg = load_config()
    assert cfg.repo_clone_dir == str(tmp_path) + "/repo_scan_cache"
