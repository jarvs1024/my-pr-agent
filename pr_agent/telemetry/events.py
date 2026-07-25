"""Thin emitter used by hooks scattered through the pr-agent codebase.

Every function is best-effort: it never raises into the caller, because
telemetry is observability, not a control surface. Failures are logged
but swallowed.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

from pr_agent.log import get_logger
from pr_agent.telemetry import models
from pr_agent.telemetry.store import get_default_store


_RULE_KEY_RE_CACHE: dict[str, "re.Pattern[str]"] = {}


def _rule_key_re():
    """Build the rule-key regex from ``config.rule_key_prefix``.

    Lazy-imports ``get_settings`` so this module stays importable in tests
    that do not boot the full settings stack. Pattern is cached per prefix
    to keep the per-suggestion cost negligible.
    """
    from pr_agent.config_loader import get_settings
    try:
        prefix = get_settings().config.get("rule_key_prefix", "ZLG") or "ZLG"
    except Exception:
        # Tests / first-import before settings are wired fall back to ZLG.
        prefix = "ZLG"
    pat = _RULE_KEY_RE_CACHE.get(prefix)
    if pat is None:
        pat = re.compile(rf"(?<![\w])({re.escape(prefix)}-RULE-[A-Z0-9-]+)(?![\w])")
        _RULE_KEY_RE_CACHE[prefix] = pat
    return pat


def extract_rule_keys_from_text(text: str) -> list[str]:
    if not text:
        return []
    seen: list[str] = []
    for m in _rule_key_re().finditer(text):
        key = m.group(1)
        if key not in seen:
            seen.append(key)
    return seen


def emit_mr_activity(
    mr_id: int,
    project_id: int,
    source_branch: str,
    target_branch: str,
    title: str,
    author: str = "",
    state: str = "opened",
    url: Optional[str] = None,
    head_sha: Optional[str] = None,
    merged_at: Optional[str] = None,
) -> None:
    try:
        mr = models.MRActivity(
            mr_id=mr_id,
            project_id=project_id,
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            author=author,
            state=state,
            url=url,
            head_sha=head_sha,
            merged_at=merged_at,
        )
        get_default_store().record_mr(mr)
    except Exception as e:
        get_logger().warning(f"telemetry.emit_mr_activity failed: {e}")


def emit_run_started(
    mr_id: int,
    project_id: int,
    command: str,
    triggered_by: str = "user",
    model: Optional[str] = None,
) -> str:
    """Return a run_id that the caller MUST pass back to emit_run_finished."""
    run = models.ReviewRun(
        mr_id=mr_id,
        project_id=project_id,
        command=command,
        status="started",
        triggered_by=triggered_by,
        model=model,
    )
    try:
        get_default_store().record_run(run)
    except Exception as e:
        get_logger().warning(f"telemetry.emit_run_started failed: {e}")
    return run.run_id


def emit_run_finished(
    run_id: str,
    status: str,
    suggestion_count: int = 0,
    rule_keys: Optional[Iterable[str]] = None,
    error: Optional[str] = None,
    duration_ms: Optional[int] = None,
    mr_id: Optional[int] = None,
    project_id: Optional[int] = None,
    command: Optional[str] = None,
) -> None:
    """Mark the run as finished.

    If ``mr_id`` / ``project_id`` / ``command`` are passed, we UPDATE the existing
    started row in place so the JOINs in ``per_author_stats`` work. Otherwise we
    fall back to recording a fresh row with empty fields (legacy behaviour).
    """
    try:
        store = get_default_store()
        if mr_id is not None and project_id is not None and command:
            # Backfill into the started row + set finish fields in one UPDATE
            from datetime import datetime, timezone
            sets = [
                "mr_id=?",
                "project_id=?",
                "command=?",
                "status=?",
                "finished_at=?",
                "error=?",
                "duration_ms=?",
                "suggestion_count=?",
                "rule_keys_cited=?",
            ]
            params = [
                int(mr_id),
                int(project_id),
                command,
                status,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                error,
                duration_ms,
                suggestion_count,
                json.dumps(list(rule_keys or []), ensure_ascii=False),
            ]
            if hasattr(store, "_db") and store._db is not None:
                with store._lock:
                    store._db.execute(
                        f"UPDATE review_runs SET {', '.join(sets)} WHERE run_id=?",
                        (*params, run_id),
                    )
                    store._db.commit()
            return
        # Legacy path: insert a fresh row (will overwrite the started row via PK)
        from datetime import datetime, timezone
        run = models.ReviewRun(
            run_id=run_id,
            mr_id=0,
            project_id=0,
            command="",
            status=status,
            finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            error=error,
            duration_ms=duration_ms,
            suggestion_count=suggestion_count,
            rule_keys_cited=list(rule_keys or []),
        )
        store.record_run(run)
    except Exception as e:
        get_logger().warning(f"telemetry.emit_run_finished failed: {e}")


def emit_suggestion(
    mr_id: int,
    project_id: int,
    file: str,
    line: Optional[int],
    label: str,
    importance: int,
    one_sentence_summary: str,
    rule_keys: Iterable[str],
    line_end: Optional[int] = None,
    score: Optional[int] = None,
    note_id=None,
    fingerprint: Optional[str] = None,
    posted_head_sha: Optional[str] = None,
) -> str:
    suggestion = models.Suggestion(
        mr_id=mr_id,
        project_id=project_id,
        file=file,
        line=line,
        line_end=line_end,
        label=label,
        importance=importance,
        one_sentence_summary=one_sentence_summary,
        rule_keys=list(rule_keys),
        score=score,
        note_id=note_id,
        fingerprint=fingerprint,
        posted_head_sha=posted_head_sha,
    )
    try:
        get_default_store().record_suggestion(suggestion)
    except Exception as e:
        get_logger().warning(f"telemetry.emit_suggestion failed: {e}")
    return suggestion.suggestion_id


def emit_action(
    action: str,
    suggestion_id: str,
    mr_id: int,
    actor: str = "",
    note: str = "",
) -> None:
    try:
        get_default_store().record_action(models.ActionEvent(action=action, suggestion_id=suggestion_id, mr_id=mr_id, actor=actor, note=note))
    except Exception as e:
        get_logger().warning(f"telemetry.emit_action failed: {e}")


def mark_suggestions_applied(mr_id: int, project_id: int, file: str, *,
                       applied_at: Optional[str] = None,
                       actor: str = "",
                       apply_event_sha: Optional[str] = None,
                       line_ranges: Optional[list[tuple[int, int]]] = None) -> list[str]:
    """Mark open suggestions matching the file and optional changed lines as applied.

    Returns the list of suggestion_ids that were flipped, so the caller can
    also emit `action_events` rows.
    """
    store = get_default_store()
    if applied_at is None:
        from datetime import datetime, timezone
        applied_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        if line_ranges is None:
            updated = store.mark_file_applied(
                mr_id=mr_id, project_id=project_id, file=file, applied_at=applied_at
            )
        else:
            updated = store.mark_lines_applied(
                mr_id=mr_id,
                project_id=project_id,
                file=file,
                line_ranges=line_ranges,
                applied_at=applied_at,
            )
    except Exception as e:
        get_logger().warning(f"telemetry.mark_suggestions_applied failed: {e}")
        return []
    for sid in updated:
        try:
            store.record_action(models.ActionEvent(
                action="applied",
                suggestion_id=sid,
                mr_id=mr_id,
                actor=actor,
                note=(f"commit {apply_event_sha}" if apply_event_sha else ""),
            ))
        except Exception as e:
            get_logger().warning(f"telemetry.action emit (applied) failed: {e}")
    return updated


def mark_suggestion_ids_applied(mr_id: int, project_id: int, suggestion_ids: list[str], *,
                                applied_at: Optional[str] = None,
                                actor: str = "",
                                apply_event_sha: Optional[str] = None) -> list[str]:
    store = get_default_store()
    if applied_at is None:
        from datetime import datetime, timezone
        applied_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        updated = store.mark_suggestion_ids_applied(
            mr_id=mr_id,
            project_id=project_id,
            suggestion_ids=suggestion_ids,
            applied_at=applied_at,
        )
    except Exception as e:
        get_logger().warning(f"telemetry.mark_suggestion_ids_applied failed: {e}")
        return []
    for suggestion_id in updated:
        try:
            store.record_action(models.ActionEvent(
                action="applied",
                suggestion_id=suggestion_id,
                mr_id=mr_id,
                actor=actor,
                note=(f"commit {apply_event_sha}" if apply_event_sha else ""),
            ))
        except Exception as e:
            get_logger().warning(f"telemetry.action emit (applied) failed: {e}")
    return updated


def mark_suggestion_applied(suggestion_id: str) -> None:
    try:
        from datetime import datetime, timezone
        get_default_store().update_suggestion_state(
            suggestion_id,
            "applied",
            applied_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    except Exception as e:
        get_logger().warning(f"telemetry.mark_suggestion_applied failed: {e}")


def mark_suggestion_dismissed(suggestion_id: str, actor: str = "", reason: str = "") -> None:
    try:
        from datetime import datetime, timezone
        get_default_store().update_suggestion_state(
            suggestion_id,
            "dismissed",
            dismissed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            dismissed_by=actor,
            dismissed_reason=reason or None,
        )
    except Exception as e:
        get_logger().warning(f"telemetry.mark_suggestion_dismissed failed: {e}")

def mark_suggestion_adopted(suggestion_id: str, actor: str = "", reason: str = "", mr_id: int = 0) -> None:
    """State-update helper for /adopt (manual / rewritten adoption).

    After simplification, ``/adopt`` and the GitLab ``Apply suggestion``
    button are both counted as adoption: state is set to ``applied`` so
    ``adoption_rate = state=applied count / total`` covers both flows.
    The distinction (button vs /adopt) is still recorded in
    ``action_events.action`` (``applied`` vs ``adopted_implicitly``) for
    future analysis but is NOT surfaced via the stats API.
    """
    mark_suggestion_applied(suggestion_id)
    if actor or reason:
        try:
            get_default_store().record_action(models.ActionEvent(
                action="adopted_implicitly",
                suggestion_id=suggestion_id,
                mr_id=mr_id,
                actor=actor,
                note=reason or None,
            ))
        except Exception as e:
            get_logger().warning(f"telemetry.adopt_action emit failed: {e}")


def mark_suggestion_superseded(suggestion_id: str) -> None:
    try:
        get_default_store().update_suggestion_state(suggestion_id, "superseded")
    except Exception as e:
        get_logger().warning(f"telemetry.mark_suggestion_superseded failed: {e}")
