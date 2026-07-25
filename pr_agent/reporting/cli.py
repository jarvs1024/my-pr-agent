"""One-off / debug CLI for the weekly-report module.

Run as::

    PYTHONPATH=. python -m pr_agent.reporting.cli --run-now
    PYTHONPATH=. python -m pr_agent.reporting.cli --show-config
    PYTHONPATH=. python -m pr_agent.reporting.cli --print-latest 123

Exits non-zero on failure. Intended for cron-triggered one-shots, manual
debugging, and the end-to-end smoke test.
"""
from __future__ import annotations

from pr_agent import config_loader as _cfg_loader  # noqa: F401  # breaks circular import
import argparse
import json
import os
import sys

from pr_agent.log import LoggingFormat, get_logger, setup_logger

from datetime import datetime, timedelta

from .config import load_config
from .report import latest_artifact_path, read_artifact
from .scheduler import run_weekly_job


_log = get_logger()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pr_agent.reporting.cli", description="Weekly report CLI")
    p.add_argument("--run-now", action="store_true", help="Execute one weekly report cycle now.")
    p.add_argument("--show-config", action="store_true", help="Print resolved config as JSON and exit.")
    p.add_argument("--print-latest", type=int, metavar="PROJECT_ID", help="Print the latest artifact JSON for the project id.")
    p.add_argument("--data-dir", default=os.environ.get("PR_AGENT_DATA_DIR", "/var/lib/pr-agent"), help="PR_AGENT_DATA_DIR override.")
    p.add_argument("--since-days", type=int, default=7, help="Override the lookback window in days (default: 7 = current week).")
    return p


def main(argv: list[str] | None = None) -> int:
    setup_logger(fmt=LoggingFormat.CONSOLE, level=os.environ.get("PR_AGENT_LOG_LEVEL", "INFO"))
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.show_config:
        cfg = load_config()
        print(json.dumps(_config_to_dict(cfg), ensure_ascii=False, indent=2, default=str))
        return 0

    if args.print_latest is not None:
        path = latest_artifact_path(args.data_dir, args.print_latest)
        if not path:
            print(f"no artifact found under {args.data_dir}/weekly_reports/{args.print_latest}", file=sys.stderr)
            return 1
        print(json.dumps(read_artifact(path), ensure_ascii=False, indent=2))
        return 0

    if args.run_now:
        cfg = load_config()
        if not cfg.target_project_id:
            print("target_project_id not configured", file=sys.stderr)
            return 2
        now = datetime.now().astimezone()
        week_start = (now - timedelta(days=args.since_days)).replace(hour=0, minute=0, second=0, microsecond=0)
        week_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        summary = run_weekly_job(cfg, now=now, data_dir=args.data_dir)
        # Re-run with overridden window by patching the artifact is too complex;
        # instead, when since_days differs, drive collectors directly with our window.
        if args.since_days != 7:
            from pr_agent.reporting.collectors.master_merges import MasterMergesCollector
            from pr_agent.reporting.collectors.repo_scan import RepoScanCollector
            from pr_agent.reporting.collectors.telemetry_overview import TelemetryOverviewCollector
            from pr_agent.reporting.collectors.base import CollectorContext
            from pr_agent.reporting.report import build_artifact, write_artifact
            from pr_agent.reporting.renderer import render_markdown, split_markdown
            from pr_agent.reporting.notifiers.dingtalk import DingTalkNotifier
            ctx = CollectorContext(
                target_project_id=cfg.target_project_id,
                data_dir=args.data_dir,
                llm_model=cfg.effective_llm_model,
                llm_dry_run=cfg.llm_dry_run,
                repo_clone_dir=cfg.repo_clone_dir,
                diff_token_limit=cfg.diff_token_limit,
                timezone=cfg.timezone,
                target_branch=os.environ.get("PR_AGENT_WEEKLY_TARGET_BRANCH", ""),
            )
            sections = {}
            for cls in (TelemetryOverviewCollector, MasterMergesCollector, RepoScanCollector):
                c = cls()
                sections[c.name] = c.collect(week_start=week_start, week_end=week_end, ctx=ctx)
            artifact = build_artifact(
                project_id=cfg.target_project_id,
                week_start=week_start,
                week_end=week_end,
                timezone=cfg.timezone,
                sections=sections,
            )
            write_artifact(artifact, args.data_dir)
            notifier = DingTalkNotifier(
                webhook_url=cfg.dingtalk_webhook_url, secret=cfg.dingtalk_secret,
                retry_attempts=cfg.dingtalk_retry_attempts, dry_run=cfg.dingtalk_dry_run,
            )
            body = render_markdown(artifact)
            chunks = split_markdown(body, chunk_limit=cfg.markdown_chunk_limit)
            delivery = notifier.send(f"📊 项目代码检视周报 {artifact.week_label}", chunks)
            summary = {
                "ran_at": now.isoformat(),
                "week_label": artifact.week_label,
                "project_id": cfg.target_project_id,
                "artifact_path": str(write_artifact(artifact, args.data_dir)),
                "section_status": {n: s.status for n, s in sections.items()},
                "delivery": {"success": delivery.success, "chunks_sent": delivery.chunks_sent, "chunks_total": delivery.chunks_total, "error": delivery.error},
                "window": {"since": week_start.isoformat(), "until": week_end.isoformat()},
            }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["delivery"]["success"] else 3

    parser.print_help()
    return 1


def _config_to_dict(cfg) -> dict:
    return {
        "enabled": cfg.enabled,
        "target_project_id": cfg.target_project_id,
        "cron": cfg.cron,
        "timezone": cfg.timezone,
        "collectors": list(cfg.collectors),
        "notifier": cfg.notifier,
        "llm_model": cfg.llm_model,
        "llm_dry_run": cfg.llm_dry_run,
        "effective_llm_model": cfg.effective_llm_model,
        "repo_clone_dir": cfg.repo_clone_dir,
        "dingtalk_webhook_env": cfg.dingtalk_webhook_env,
        "dingtalk_webhook_url_set": bool(cfg.dingtalk_webhook_url),
        "dingtalk_secret_env": cfg.dingtalk_secret_env,
        "dingtalk_dry_run": cfg.dingtalk_dry_run,
        "dingtalk_retry_attempts": cfg.dingtalk_retry_attempts,
        "diff_token_limit": cfg.diff_token_limit,
        "markdown_chunk_limit": cfg.markdown_chunk_limit,
    }


if __name__ == "__main__":
    sys.exit(main())
