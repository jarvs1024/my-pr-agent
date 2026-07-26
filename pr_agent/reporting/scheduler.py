"""APScheduler entry point for the weekly-report module.

Run as::

    python -m pr_agent.reporting.scheduler

This module is the production entry for the ``pr-agent-reporter``
container. For one-off / debug runs use :mod:`pr_agent.reporting.cli`
instead.
"""
from __future__ import annotations

from pr_agent import config_loader as _cfg_loader  # noqa: F401  # breaks circular import
import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pr_agent.log import LoggingFormat, get_logger, setup_logger

from .collectors.base import Collector, CollectorContext, SectionResult
from .collectors.master_merges import MasterMergesCollector
from .collectors.repo_scan import RepoScanCollector
from .collectors.telemetry_overview import TelemetryOverviewCollector
from .config import WeeklyReportConfig, load_config
from .notifiers.base import DeliveryResult, Notifier
from .notifiers.dingtalk import DingTalkNotifier
from .notifiers.dingtalk_openapi import DingTalkOpenAPINotifier
from .renderer import render_markdown, split_markdown
from .report import (
    WeeklyArtifact,
    build_artifact,
    iso_week_label,
    write_artifact,
)


_log = get_logger()


def _build_collectors(cfg: WeeklyReportConfig) -> list[Collector]:
    name_to_cls: dict[str, type] = {
        "telemetry": TelemetryOverviewCollector,
        "master_merges": MasterMergesCollector,
        "repo_scan": RepoScanCollector,
    }
    out: list[Collector] = []
    for name in cfg.collectors:
        cls = name_to_cls.get(name)
        if cls is None:
            _log.warning("unknown collector %r in config; skipping", name)
            continue
        out.append(cls())
    return out


def _build_notifier(cfg: WeeklyReportConfig) -> Notifier:
    if cfg.notifier == "dingtalk":
        return DingTalkNotifier(
            webhook_url=cfg.dingtalk_webhook_url,
            secret=cfg.dingtalk_secret,
            retry_attempts=cfg.dingtalk_retry_attempts,
            dry_run=cfg.dingtalk_dry_run,
        )
    if cfg.notifier == "dingtalk_openapi":
        return DingTalkOpenAPINotifier(
            app_key=os.environ.get("DINGTALK_OPENAPI_APP_KEY", ""),
            app_secret=os.environ.get("DINGTALK_OPENAPI_APP_SECRET", ""),
            robot_code=os.environ.get("DINGTALK_OPENAPI_ROBOT_CODE", ""),
            open_conversation_id=os.environ.get(
                "DINGTALK_OPENAPI_OPEN_CONVERSATION_ID", ""
            ),
            retry_attempts=cfg.dingtalk_retry_attempts,
            dry_run=cfg.dingtalk_dry_run,
        )
    raise RuntimeError(f"unknown notifier: {cfg.notifier!r}")


def _week_bounds(now: datetime, tz: timezone | None = None) -> tuple[datetime, datetime]:
    """Return ``(week_start, week_end)`` for the week *containing* ``now``."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz or timezone(timedelta(hours=8)))
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_end = (week_start + timedelta(days=6)).replace(
        hour=23, minute=59, second=59, microsecond=999999
    )
    return week_start, week_end


def run_weekly_job(
    cfg: WeeklyReportConfig,
    *,
    now: datetime | None = None,
    data_dir: str | None = None,
    collectors: list[Collector] | None = None,
    notifier: Notifier | None = None,
) -> dict[str, Any]:
    """Execute a single weekly report cycle. Returns the run summary dict.

    Exposed for the CLI and for tests; the scheduler loop just calls this
    on each cron tick.
    """
    now = now or datetime.now().astimezone()
    week_start, week_end = _week_bounds(now)
    data_dir = data_dir or os.environ.get("PR_AGENT_DATA_DIR", "/var/lib/pr-agent")
    collectors = collectors or _build_collectors(cfg)
    notifier = notifier or _build_notifier(cfg)

    ctx = CollectorContext(
        target_project_id=cfg.target_project_id,
        data_dir=data_dir,
        llm_model=cfg.effective_llm_model,
        llm_dry_run=cfg.llm_dry_run,
        repo_clone_dir=cfg.repo_clone_dir,
        diff_token_limit=cfg.diff_token_limit,
        timezone=cfg.timezone,
        target_branch=os.environ.get("PR_AGENT_WEEKLY_TARGET_BRANCH", cfg.extra.get("target_branch", "")),
    )

    sections: dict[str, SectionResult] = {}
    for c in collectors:
        try:
            _log.info("running collector %s", c.name)
            t0 = time.monotonic()
            result = c.collect(week_start=week_start, week_end=week_end, ctx=ctx)
            elapsed = time.monotonic() - t0
            result.meta.setdefault("elapsed_seconds", round(elapsed, 2))
            sections[c.name] = result
            _log.info("collector %s %s in %.2fs", c.name, result.status, elapsed)
        except Exception as exc:  # noqa: BLE001
            _log.error("collector %s raised: %s", c.name, exc)
            _log.debug(traceback.format_exc())
            sections[c.name] = SectionResult(
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    artifact = build_artifact(
        project_id=cfg.target_project_id,
        week_start=week_start,
        week_end=week_end,
        timezone=cfg.timezone,
        sections=sections,
    )

    artifact_path: Path | None = None
    try:
        artifact_path = write_artifact(artifact, data_dir)
    except Exception as exc:  # noqa: BLE001
        _log.error("artifact dump failed: %s", exc)
        _log.debug(traceback.format_exc())

    markdown_body = render_markdown(artifact)
    chunks = split_markdown(markdown_body, chunk_limit=cfg.markdown_chunk_limit)
    delivery: DeliveryResult | None = None
    try:
        title = f"📊 项目代码检视周报 {artifact.week_label}"
        delivery = notifier.send(title, chunks)
        _log.info(
            "delivery: %s, chunks_sent=%d/%d, error=%s",
            "ok" if delivery.success else "failed",
            delivery.chunks_sent,
            delivery.chunks_total,
            delivery.error,
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("notifier raised: %s", exc)
        _log.debug(traceback.format_exc())
        delivery = DeliveryResult(success=False, error=f"{type(exc).__name__}: {exc}")

    summary = {
        "ran_at": now.isoformat(),
        "week_label": artifact.week_label,
        "project_id": cfg.target_project_id,
        "artifact_path": str(artifact_path) if artifact_path else None,
        "section_status": {name: sr.status for name, sr in sections.items()},
        "delivery": {
            "success": delivery.success if delivery else False,
            "chunks_sent": delivery.chunks_sent if delivery else 0,
            "chunks_total": delivery.chunks_total if delivery else 0,
            "error": delivery.error if delivery else "notifier did not return",
        },
    }

    _write_run_log(data_dir, summary, status="ok" if (delivery and delivery.success) else "completed_with_errors")
    return summary


def _write_run_log(data_dir: str, summary: dict[str, Any], *, status: str) -> None:
    runs_dir = Path(data_dir) / "reporting_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    suffix = "ok" if status == "ok" else "failed"
    path = runs_dir / f"{ts}.{suffix}.json"
    try:
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        _log.error("run log write failed: %s", exc)


def _install_signal_handlers(stop_event_predicate) -> None:
    def _handler(signum, _frame):  # noqa: ARG001
        _log.info("received signal %s; shutting down", signum)
        stop_event_predicate()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


def main() -> int:
    """Blocking scheduler loop. Returns process exit code."""
    setup_logger(fmt=LoggingFormat.CONSOLE, level=os.environ.get("PR_AGENT_LOG_LEVEL", "INFO"))
    cfg = load_config()
    if not cfg.enabled:
        _log.warning(
            "weekly_report disabled (set PR_AGENT_WEEKLY_ENABLED=1 or [weekly_report] enabled=true)"
        )
        # Stay alive but idle so the container restart policy keeps us up if enabled later.
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            return 0

    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as exc:  # pragma: no cover
        _log.error("apscheduler not installed: %s", exc)
        return 1

    scheduler = BlockingScheduler(timezone=cfg.timezone)
    trigger = CronTrigger.from_crontab(cfg.cron, timezone=cfg.timezone)

    def _job():
        try:
            run_weekly_job(cfg)
        except Exception:  # noqa: BLE001
            _log.exception("weekly_job crashed; will retry next cron tick")

    scheduler.add_job(_job, trigger=trigger, id="weekly_report", replace_existing=True, max_instances=1, coalesce=True)
    _log.info("scheduler started: cron=%r tz=%s target_project=%d", cfg.cron, cfg.timezone, cfg.target_project_id)

    _install_signal_handlers(lambda: scheduler.shutdown(wait=False))

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        _log.info("scheduler exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
