"""SQLite (default) / JSONL (opt-in) event store for review telemetry.

The store is process-local. When REVIEW_TELEMETRY_BACKEND=jsonl, every record
is appended to a JSONL file. When =sqlite (default), records go into a small
SQLite database with one table per event kind.

The store is intentionally simple: no schema migrations, no transactions
beyond the per-write ones. Reads are best-effort and tolerate partial rows.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from pr_agent.log import get_logger
from pr_agent.telemetry import models
from pr_agent.algo.repo_context import resolve_severity, SEVERITY_LEVELS


def _severity_config() -> dict:
    """Read the [telemetry.severity] config block, with sane defaults."""
    from pr_agent.config_loader import get_settings
    cfg = get_settings().get("telemetry.severity", {}) or {}
    patterns = cfg.get("critical_rule_patterns", []) or []
    high_pats = cfg.get("high_rule_patterns", []) or []
    return {
        "critical_min": int(cfg.get("critical_min", 8)),
        "high_min": int(cfg.get("high_min", 6)),
        "medium_min": int(cfg.get("medium_min", 4)),
        "critical_rule_patterns": list(patterns),
        "high_rule_patterns": list(high_pats),
        "rule_files": list(cfg.get("rule_files", []) or []),
    }


def _rule_severity_map_for_pr(pr_url: Optional[str] = None) -> dict[str, str]:
    """Load the project's rule-severity map from .agents/rules/*.md files.

    The PR's git provider is resolved from ``pr_url`` (or the most recent MR
    if omitted) and the configured rule files are read via its ``get_pr_file_content``
    helper. Missing / unreadable files are silently skipped — severity
    resolution is a soft signal and must never break the API.
    """
    cfg = _severity_config()
    paths = cfg.get("rule_files") or []
    if not paths:
        return {}
    try:
        from pr_agent.git_providers import get_git_provider_with_context
        if pr_url:
            provider = get_git_provider_with_context(pr_url)
        else:
            return {}
    except Exception:
        return {}
    contents = []
    for p in paths:
        try:
            # Honour branch from the PR if available
            branch = None
            try:
                branch = provider.get_pr_branch()
            except Exception:
                branch = None
            if branch:
                c = provider.get_pr_file_content(p, branch)
            else:
                c = provider.get_pr_file_content(p, provider.get_pr_branch() or "main")
            if c:
                contents.append(c)
        except Exception:
            continue
    if not contents:
        return {}
    from pr_agent.algo.repo_context import parse_rule_files
    return parse_rule_files(contents)


def _resolve_severity_for_suggestion(rule_keys, importance, *, rule_severity_map=None):
    cfg = _severity_config()
    sev, src = resolve_severity(
        rule_keys or [],
        importance,
        rule_severity_map=rule_severity_map or {},
        critical_patterns=cfg["critical_rule_patterns"],
        high_patterns=cfg["high_rule_patterns"],
        critical_min=cfg["critical_min"],
        high_min=cfg["high_min"],
        medium_min=cfg["medium_min"],
    )
    return sev, src


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS mr_activity (
    mr_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    source_branch TEXT,
    target_branch TEXT,
    title TEXT,
    author TEXT,
    state TEXT,
    opened_at TEXT,
    last_seen_at TEXT,
    merged_at TEXT,
    url TEXT,
    head_sha TEXT,
    PRIMARY KEY (project_id, mr_id)
);

CREATE TABLE IF NOT EXISTS review_runs (
    run_id TEXT PRIMARY KEY,
    mr_id INTEGER,
    project_id INTEGER,
    command TEXT,
    status TEXT,
    model TEXT,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    duration_ms INTEGER,
    suggestion_count INTEGER DEFAULT 0,
    rule_keys_cited TEXT,
    triggered_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_mr ON review_runs(mr_id);

CREATE TABLE IF NOT EXISTS suggestions (
    suggestion_id TEXT PRIMARY KEY,
    mr_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    file TEXT,
    line INTEGER,
    label TEXT,
    importance INTEGER,
    one_sentence_summary TEXT,
    rule_keys TEXT,
    score INTEGER,
    posted_at TEXT,
    state TEXT,
    applied_at TEXT,
    dismissed_at TEXT,
    dismissed_by TEXT,
    dismissed_reason TEXT,
    note_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_sug_mr ON suggestions(mr_id);
CREATE INDEX IF NOT EXISTS idx_sug_state ON suggestions(state);

CREATE TABLE IF NOT EXISTS action_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    suggestion_id TEXT,
    mr_id INTEGER,
    actor TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_act_mr ON action_events(mr_id);
"""


class TelemetryStore:
    def __init__(self, backend: str, sqlite_path: Optional[str] = None, jsonl_path: Optional[str] = None) -> None:
        self.backend = backend
        self._lock = threading.Lock()
        self._jsonl_fp = None
        self._db = None
        if backend == "sqlite":
            _sqlite_env = os.environ.get("REVIEW_TELEMETRY_DB_PATH")
            path = sqlite_path or _sqlite_env or (os.environ.get("PR_AGENT_DATA_DIR", "/var/lib/pr-agent") + "/telemetry.db")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.executescript(_SQLITE_SCHEMA)
            # One-off migration: older deployments stored note_id as INTEGER but the
            # GitLab discussion id is a 40-char SHA1 hash that overflows SQLite
            # INTEGER. Convert in place via shadow column.
            try:
                cols = [r[1] for r in self._db.execute(
                    "PRAGMA table_info(suggestions)").fetchall()]
                if "note_id" in cols:
                    col_type = next((r[2] for r in self._db.execute(
                        "PRAGMA table_info(suggestions)").fetchall()
                        if r[1] == "note_id"), None)
                    if col_type and col_type.upper() != "TEXT":
                        self._db.execute(
                            "ALTER TABLE suggestions ADD COLUMN note_id_text TEXT")
                        self._db.execute(
                            "UPDATE suggestions SET note_id_text = CAST(note_id AS TEXT)")
                        self._db.execute(
                            "ALTER TABLE suggestions DROP COLUMN note_id")
                        self._db.execute(
                            "ALTER TABLE suggestions RENAME COLUMN note_id_text TO note_id")
                        self._db.commit()
                        get_logger().info(
                            "telemetry: migrated suggestions.note_id -> TEXT")
            except Exception as e:
                get_logger().warning(f"telemetry note_id migration skipped: {e}")
            try:
                cur_cols = [r[1] for r in self._db.execute(
                    "PRAGMA table_info(suggestions)").fetchall()]
                if "dismissed_reason" not in cur_cols:
                    self._db.execute(
                        "ALTER TABLE suggestions ADD COLUMN dismissed_reason TEXT")
                    self._db.commit()
                    get_logger().info(
                        "telemetry: added suggestions.dismissed_reason")
            except Exception as e:
                get_logger().warning(f"telemetry dismissed_reason migration skipped: {e}")
            self._db.commit()
        elif backend == "jsonl":
            _jsonl_env = os.environ.get("REVIEW_TELEMETRY_JSONL_PATH")
            path = jsonl_path or _jsonl_env or (os.environ.get("PR_AGENT_DATA_DIR", "/var/lib/pr-agent") + "/telemetry.jsonl")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._jsonl_fp = open(path, "a", encoding="utf-8")
        elif backend == "off":
            pass
        else:
            raise ValueError(f"Unknown telemetry backend: {backend!r}")

    def _write_jsonl(self, kind: str, payload: dict) -> None:
        if self._jsonl_fp is None:
            return
        line = json.dumps({"_kind": kind, **payload}, ensure_ascii=False)
        with self._lock:
            self._jsonl_fp.write(line + "\n")
            self._jsonl_fp.flush()

    def record_mr(self, mr) -> None:
        d = mr.to_dict()
        if self.backend == "sqlite":
            with self._lock:
                # Sticky first-seen author.
                #
                # GitLab merge / close events drop the
                # `object_attributes.author` dict and keep only `author_id`,
                # so `_resolve_author` falls back to `data["user"]` (the
                # actor who clicked merge / close). Without this guard, the
                # merger / closer would silently overwrite the MR creator.
                # We preserve the first non-empty author we ever saw.
                cur = self._db.execute(
                    "SELECT author FROM mr_activity WHERE project_id=? AND mr_id=?",
                    (d["project_id"], d["mr_id"]),
                )
                existing = cur.fetchone()
                if existing and existing[0] and not d.get("author"):
                    d["author"] = existing[0]
                elif existing and existing[0]:
                    # Existing non-empty author always wins over a new value
                    # (which may be the merge-actor due to GitLab payload quirks).
                    d["author"] = existing[0]
                self._db.execute(
                    "INSERT OR REPLACE INTO mr_activity (mr_id, project_id, source_branch, target_branch, title, author, state, opened_at, last_seen_at, merged_at, url, head_sha) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (d["mr_id"], d["project_id"], d["source_branch"], d["target_branch"], d["title"], d["author"], d["state"], d["opened_at"], d["last_seen_at"], d["merged_at"], d["url"], d["head_sha"]),
                )
                self._db.commit()
        elif self.backend == "jsonl":
            self._write_jsonl("mr_activity", d)

    def update_run_finished(self, run_id: str, status: str, error=None, duration_ms=None) -> None:
        """Backfill mr_id/project_id/command into the existing started row, then set finish fields.

        The started row is created with empty fields (because emit_run_finished runs in a
        fire-and-forget finally block where we don't have the calling tool's context). We
        therefore store whatever fields we know here, and the API caller can update the rest
        through subsequent calls if needed.
        """
        if self.backend != "sqlite" or self._db is None:
            return
        sets = ["status=?", "finished_at=?", "error=?", "duration_ms=?"]
        from datetime import datetime, timezone
        params = [status, datetime.now(timezone.utc).isoformat(timespec="seconds"), error, duration_ms]
        with self._lock:
            self._db.execute(
                f"UPDATE review_runs SET {', '.join(sets)} WHERE run_id=?",
                (*params, run_id),
            )
            self._db.commit()

    def record_run(self, run) -> None:
        d = run.to_dict()
        if self.backend == "sqlite":
            with self._lock:
                self._db.execute(
                    "INSERT OR REPLACE INTO review_runs (run_id, mr_id, project_id, command, status, model, started_at, finished_at, error, duration_ms, suggestion_count, rule_keys_cited, triggered_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (d["run_id"], d["mr_id"], d["project_id"], d["command"], d["status"], d["model"], d["started_at"], d["finished_at"], d["error"], d["duration_ms"], d["suggestion_count"], json.dumps(d["rule_keys_cited"], ensure_ascii=False), d["triggered_by"]),
                )
                self._db.commit()
        elif self.backend == "jsonl":
            self._write_jsonl("review_run", d)

    def record_suggestion(self, suggestion) -> None:
        d = suggestion.to_dict()
        if self.backend == "sqlite":
            with self._lock:
                self._db.execute(
                    "INSERT OR REPLACE INTO suggestions (suggestion_id, mr_id, project_id, file, line, label, importance, one_sentence_summary, rule_keys, score, posted_at, state, applied_at, dismissed_at, dismissed_by, dismissed_reason, note_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (d["suggestion_id"], d["mr_id"], d["project_id"], d["file"], d["line"], d["label"], d["importance"], d["one_sentence_summary"], json.dumps(d["rule_keys"], ensure_ascii=False), d["score"], d["posted_at"], d["state"], d["applied_at"], d["dismissed_at"], d["dismissed_by"], d.get("dismissed_reason"), d["note_id"]),
                )
                self._db.commit()
        elif self.backend == "jsonl":
            self._write_jsonl("suggestion", d)

    def record_action(self, action) -> None:
        d = action.to_dict()
        if self.backend == "sqlite":
            with self._lock:
                self._db.execute(
                    "INSERT INTO action_events (at, action, suggestion_id, mr_id, actor, note) VALUES (?,?,?,?,?,?)",
                    (d["at"], d["action"], d["suggestion_id"], d["mr_id"], d["actor"], d["note"]),
                )
                self._db.commit()
        elif self.backend == "jsonl":
            self._write_jsonl("action", d)

    def mark_lines_applied(self, mr_id: int, project_id: int, file: str,
                        line_ranges: list[tuple[int, int]],
                        *, applied_at: str) -> list[str]:
        """Mark open suggestions in (mr_id, project_id, file) whose ``line`` is
        inside any of ``line_ranges`` as applied.

        Returns the suggestion_ids that were updated. Already-resolved rows
        are left untouched — apply is idempotent and does not flip
        dismissed -> applied.

        ``line_ranges`` is a list of inclusive (start, end) tuples. An empty
        list is treated as a no-op (caller must explicitly opt-in to the
        file-level legacy behaviour via ``mark_file_applied``).
        """
        if self.backend != "sqlite" or self._db is None:
            return []
        if not line_ranges:
            return []
        with self._lock:
            id_clauses = []
            params: list = []
            for start, end in line_ranges:
                id_clauses.append("(line BETWEEN ? AND ?)")
                params.extend([start, end])
            where = " OR ".join(id_clauses)
            cur = self._db.execute(
                f"SELECT suggestion_id FROM suggestions "
                f"WHERE project_id=? AND mr_id=? AND file=? "
                f"AND state='open' AND ({where})",
                (project_id, mr_id, file, *params),
            )
            ids = [row[0] for row in cur.fetchall()]
            if not ids:
                return []
            placeholders = ",".join(["?"] * len(ids))
            self._db.execute(
                "UPDATE suggestions SET state='applied', applied_at=? "
                f"WHERE suggestion_id IN ({placeholders})",
                (applied_at, *ids),
            )
            self._db.commit()
        return ids

    def mark_file_applied(self, mr_id: int, project_id: int, file: str, *, applied_at: str) -> list[str]:
        """Legacy entry point kept for backwards compatibility: marks EVERY
        open suggestion in (mr_id, project_id, file) as applied.

        New code should use ``mark_lines_applied`` with the exact lines
        changed by the commit. ``mark_file_applied`` is preserved for
        webhook paths that cannot fetch a commit diff (e.g. merge_request
        event without files_hint AND without diff access). It is a strict
        superset of mark_lines_applied with line_ranges spanning the whole
        file.
        """
        if self.backend != "sqlite" or self._db is None:
            return []
        # Wide line range that captures all reasonable suggestion lines. The
        # max line we ever see on a Python file in this repo is well under
        # 100_000, so this is a safe upper bound.
        return self.mark_lines_applied(
            mr_id=mr_id, project_id=project_id, file=file,
            line_ranges=[(1, 1_000_000)],
            applied_at=applied_at,
        )

    def update_suggestion_state(self, suggestion_id: str, state: str, **fields) -> None:
        if self.backend != "sqlite" or self._db is None:
            return
        sets = ["state=?", "applied_at=?", "dismissed_at=?", "dismissed_by=?", "dismissed_reason=?"]
        params = [state, fields.get("applied_at"), fields.get("dismissed_at"), fields.get("dismissed_by"), fields.get("dismissed_reason")]
        with self._lock:
            self._db.execute(
                f"UPDATE suggestions SET {', '.join(sets)} WHERE suggestion_id=?",
                (*params, suggestion_id),
            )
            self._db.commit()

    def get_suggestion_by_note_id(self, note_id):
        if self.backend != "sqlite" or self._db is None:
            return None
        with self._lock:
            cur = self._db.execute("SELECT * FROM suggestions WHERE note_id=? ORDER BY posted_at DESC LIMIT 1", (note_id,))
            row = cur.fetchone()
        return _row_to_suggestion(row) if row else None

    def list_dismissals(
        self,
        since: Optional[str] = None,
        project_id: Optional[int] = None,
        rule_key: Optional[str] = None,
        mr_id: Optional[int] = None,
        limit: int = 200,
    ):
        """Return dismissed suggestions, optionally filtered by project / rule / mr / time.

        Used by the telemetry API to surface dismissal reasons so the frontend
        can show "why each rule got dismissed" and feed this back into
        rule-tuning.
        """
        if self.backend != "sqlite" or self._db is None:
            return []
        clauses = ["state='dismissed'"]
        params: list = []
        if since is not None:
            clauses.append("dismissed_at >= ?")
            params.append(since)
        if project_id is not None:
            clauses.append("project_id=?")
            params.append(project_id)
        if mr_id is not None:
            clauses.append("mr_id=?")
            params.append(mr_id)
        if rule_key is not None:
            # rule_keys is a JSON-encoded list; LIKE match on the literal key is
            # good enough for the volume we expect (per-MR rule key cardinality
            # is small). Using LIKE avoids pulling in sqlite JSON1 extension.
            clauses.append("rule_keys LIKE ?")
            params.append(f'%"{rule_key}"%')
        where = " WHERE " + " AND ".join(clauses)
        params.append(limit)
        with self._lock:
            cur = self._db.execute(
                f"SELECT * FROM suggestions{where} ORDER BY dismissed_at DESC LIMIT ?",
                params,
            )
            rows = cur.fetchall()
        return [_row_to_suggestion(r) for r in rows]

    def list_dismissals_by_rule(
        self,
        since: Optional[str] = None,
        project_id: Optional[int] = None,
    ) -> list[dict]:
        """Aggregate dismissals by rule_key with reason text distribution.

        Returns: [{"rule_key": str, "dismissal_count": int, "reasons": [
            {"reason": str, "count": int}
        ]}]
        """
        if self.backend != "sqlite" or self._db is None:
            return []
        clauses = ["state='dismissed'"]
        params: list = []
        if since is not None:
            clauses.append("dismissed_at >= ?")
            params.append(since)
        if project_id is not None:
            clauses.append("project_id=?")
            params.append(project_id)
        where = " WHERE " + " AND ".join(clauses)
        with self._lock:
            cur = self._db.execute(
                f"SELECT rule_keys, dismissed_reason FROM suggestions{where}",
                params,
            )
            rows = cur.fetchall()
        from collections import defaultdict, Counter
        per_rule: dict = defaultdict(lambda: {"dismissal_count": 0, "reasons": Counter()})
        for rk_json, reason in rows:
            try:
                keys = json.loads(rk_json or "[]")
            except Exception:
                keys = []
            reason_norm = (reason or "").strip() or "(no reason given)"
            for k in keys or ["(no rule key)"]:
                per_rule[k]["dismissal_count"] += 1
                per_rule[k]["reasons"][reason_norm] += 1
        out = []
        for k, v in per_rule.items():
            out.append({
                "rule_key": k,
                "dismissal_count": v["dismissal_count"],
                "reasons": [{"reason": r, "count": c} for r, c in v["reasons"].most_common()],
            })
        out.sort(key=lambda x: -x["dismissal_count"])
        return out

    # ---------- reads ----------

    def list_mrs(self, limit: int = 50, project_id: Optional[int] = None, state: Optional[str] = None, since: Optional[str] = None):
        if self.backend != "sqlite" or self._db is None:
            return []
        clauses = []
        params: list = []
        if project_id is not None:
            clauses.append("project_id=?")
            params.append(project_id)
        if state is not None:
            clauses.append("state=?")
            params.append(state)
        if since is not None:
            clauses.append("last_seen_at >= ?")
            params.append(since)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._lock:
            cur = self._db.execute(f"SELECT * FROM mr_activity{where} ORDER BY last_seen_at DESC LIMIT ?", params)
            rows = cur.fetchall()
        return [_row_to_mr(r) for r in rows]

    def get_mr(self, project_id: int, mr_id: int):
        if self.backend != "sqlite" or self._db is None:
            return None
        with self._lock:
            cur = self._db.execute("SELECT * FROM mr_activity WHERE project_id=? AND mr_id=?", (project_id, mr_id))
            row = cur.fetchone()
        return _row_to_mr(row) if row else None

    def list_suggestions(self, mr_id: int, project_id: Optional[int] = None, *, attach_severity: bool = True, pr_url: Optional[str] = None):
        if self.backend != "sqlite" or self._db is None:
            return []
        with self._lock:
            if project_id is not None:
                cur = self._db.execute("SELECT * FROM suggestions WHERE project_id=? AND mr_id=? ORDER BY posted_at", (project_id, mr_id))
            else:
                cur = self._db.execute("SELECT * FROM suggestions WHERE mr_id=? ORDER BY posted_at", (mr_id,))
            rows = cur.fetchall()
        out = [_row_to_suggestion(r) for r in rows]
        if attach_severity:
            rule_map = _rule_severity_map_for_pr(pr_url) if pr_url else {}
            for s in out:
                sev, src = _resolve_severity_for_suggestion(
                    s.get("rule_keys") or [], s.get("importance"), rule_severity_map=rule_map
                )
                s["severity"] = sev
                s["severity_source"] = src
        return out

    def list_runs(self, mr_id: int, limit: int = 20):
        if self.backend != "sqlite" or self._db is None:
            return []
        with self._lock:
            cur = self._db.execute("SELECT * FROM review_runs WHERE mr_id=? ORDER BY started_at DESC LIMIT ?", (mr_id, limit))
            rows = cur.fetchall()
        return [_row_to_run(r) for r in rows]

    def count_adopted_implicitly(self, mr_id: int) -> int:
        """Count distinct suggestion_ids with action=adopted_implicitly for one MR. """
        if self.backend != "sqlite" or self._db is None:
            return 0
        with self._lock:
            cur = self._db.execute(
                "SELECT COUNT(DISTINCT suggestion_id) FROM action_events WHERE mr_id=? AND action=? AND suggestion_id IS NOT NULL",
                (mr_id, "adopted_implicitly"))
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def list_actions(self, mr_id: int, limit: int = 50):
        if self.backend != "sqlite" or self._db is None:
            return []
        with self._lock:
            cur = self._db.execute("SELECT * FROM action_events WHERE mr_id=? ORDER BY at DESC LIMIT ?", (mr_id, limit))
            rows = cur.fetchall()
        return [_row_to_action(r) for r in rows]

    def overview(self, since: Optional[str] = None):
        if self.backend != "sqlite" or self._db is None:
            return {}
        mr_clause = " WHERE last_seen_at >= ?" if since else ""
        sug_clause = " WHERE posted_at >= ?" if since else ""
        run_clause = " WHERE started_at >= ?" if since else ""
        mr_params = (since,) if since else ()
        sug_params = (since,) if since else ()
        run_params = (since,) if since else ()
        with self._lock:
            mr_count = self._db.execute(f"SELECT COUNT(*) FROM mr_activity{mr_clause}", mr_params).fetchone()[0]
            merged = self._db.execute(f"SELECT COUNT(*) FROM mr_activity WHERE state='merged'{(' AND last_seen_at >= ?' if since else '')}", mr_params).fetchone()[0]
            open_mrs = self._db.execute(f"SELECT COUNT(*) FROM mr_activity WHERE state='opened'{(' AND last_seen_at >= ?' if since else '')}", mr_params).fetchone()[0]
            sug_total = self._db.execute(f"SELECT COUNT(*) FROM suggestions{sug_clause}", sug_params).fetchone()[0]
            sug_applied = self._db.execute(f"SELECT COUNT(*) FROM suggestions WHERE state='applied'{(' AND posted_at >= ?' if since else '')}", sug_params).fetchone()[0]
            sug_dismissed = self._db.execute(f"SELECT COUNT(*) FROM suggestions WHERE state='dismissed'{(' AND posted_at >= ?' if since else '')}", sug_params).fetchone()[0]
            sug_open = self._db.execute(f"SELECT COUNT(*) FROM suggestions WHERE state='open'{(' AND posted_at >= ?' if since else '')}", sug_params).fetchone()[0]
            run_total = self._db.execute(f"SELECT COUNT(*) FROM review_runs{run_clause}", run_params).fetchone()[0]
            run_failed = self._db.execute(f"SELECT COUNT(*) FROM review_runs WHERE status='failed'{(' AND started_at >= ?' if since else '')}", run_params).fetchone()[0]
        return {
            "since": since,
            "mrs": {"total": mr_count, "merged": merged, "open": open_mrs},
            "suggestions": {
                "total": sug_total,
                "applied": sug_applied,
                "dismissed": sug_dismissed,
                "open": sug_open,
                "adoption_rate": (sug_applied / sug_total) if sug_total else 0.0,
                "dismissal_rate": (sug_dismissed / sug_total) if sug_total else 0.0,
            },
            "runs": {"total": run_total, "failed": run_failed, "success_rate": ((run_total - run_failed) / run_total) if run_total else 0.0},
            "severity_breakdown": self.severity_breakdown(since=since),
        }

    def per_author_stats(self, since: Optional[str] = None):
        if self.backend != "sqlite" or self._db is None:
            return []
        # MR per author: group by mr_activity.author
        mr_clause = " WHERE last_seen_at >= ?" if since else ""
        sug_clause = " WHERE posted_at >= ?" if since else ""
        run_clause = " WHERE started_at >= ?" if since else ""
        params = (since,) if since else ()
        # sug_params shares the single timestamp placeholder used by the
        # joined suggestions sub-query (mirrors overview()).
        sug_params = params
        with self._lock:
            mr_rows = self._db.execute(
                f"SELECT author, COUNT(*) AS mr_count, "
                f"SUM(CASE WHEN state='merged' THEN 1 ELSE 0 END) AS merged "
                f"FROM mr_activity{mr_clause} GROUP BY author", params
            ).fetchall()
            sug_rows = self._db.execute(
                f"SELECT mr.author, s.state, COUNT(*) FROM suggestions s "
                f"JOIN mr_activity mr ON mr.project_id = s.project_id AND mr.mr_id = s.mr_id "
                f"{sug_clause.replace('WHERE', 'WHERE s.', 1) if since else ''} "
                f"GROUP BY mr.author, s.state", sug_params if since else ()
            ).fetchall() if since else self._db.execute(
                "SELECT mr.author, s.state, COUNT(*) FROM suggestions s "
                "JOIN mr_activity mr ON mr.project_id = s.project_id AND mr.mr_id = s.mr_id "
                "GROUP BY mr.author, s.state"
            ).fetchall()
            run_rows = self._db.execute(
                f"SELECT mr.author, r.command, COUNT(*), "
                f"SUM(CASE WHEN r.status='failed' THEN 1 ELSE 0 END) "
                f"FROM review_runs r JOIN mr_activity mr ON mr.project_id = r.project_id AND mr.mr_id = r.mr_id "
                f"{run_clause.replace('WHERE', 'WHERE r.', 1) if since else ''} "
                f"GROUP BY mr.author, r.command", params if since else ()
            ).fetchall() if since else self._db.execute(
                "SELECT mr.author, r.command, COUNT(*), "
                "SUM(CASE WHEN r.status='failed' THEN 1 ELSE 0 END) "
                "FROM review_runs r JOIN mr_activity mr ON mr.project_id = r.project_id AND mr.mr_id = r.mr_id "
                "GROUP BY mr.author, r.command"
            ).fetchall()

        # Build per-author aggregate
        agg: dict = {}
        for author, mr_count, merged in mr_rows:
            agg.setdefault(author or "unknown", {"mr_count": 0, "merged_count": 0, "suggestion_total": 0, "suggestion_applied": 0, "suggestion_dismissed": 0, "runs_by_command": {}})
            agg[author or "unknown"]["mr_count"] = mr_count
            agg[author or "unknown"]["merged_count"] = merged
        for author, state, count in sug_rows:
            entry = agg.setdefault(author or "unknown", {"mr_count": 0, "merged_count": 0, "suggestion_total": 0, "suggestion_applied": 0, "suggestion_dismissed": 0, "runs_by_command": {}})
            entry["suggestion_total"] += count
            if state == "applied":
                entry["suggestion_applied"] += count
            elif state == "dismissed":
                entry["suggestion_dismissed"] += count
        for author, command, count, failed in run_rows:
            entry = agg.setdefault(author or "unknown", {"mr_count": 0, "merged_count": 0, "suggestion_total": 0, "suggestion_applied": 0, "suggestion_dismissed": 0, "runs_by_command": {}})
            entry["runs_by_command"][command or "unknown"] = {"total": count, "failed": failed or 0}

        result = []
        for author, s in sorted(agg.items()):
            total = s["suggestion_total"]
            applied = s["suggestion_applied"]
            result.append({
                "author": author,
                "mr_count": s["mr_count"],
                "merged_count": s["merged_count"],
                "suggestion_total": total,
                "suggestion_applied": applied,
                "suggestion_dismissed": s["suggestion_dismissed"],
                "adoption_rate": (applied / total) if total else 0.0,
                "runs_by_command": s["runs_by_command"],
            })
        return result

    def per_rule_stats(self, since: Optional[str] = None):
        if self.backend != "sqlite" or self._db is None:
            return []
        clause = " WHERE posted_at >= ?" if since else ""
        params = (since,) if since else ()
        with self._lock:
            cur = self._db.execute(f"SELECT rule_keys, state, COUNT(*) FROM suggestions{clause} GROUP BY rule_keys, state", params)
            rows = cur.fetchall()
        from collections import defaultdict
        agg = defaultdict(lambda: {"applied": 0, "dismissed": 0, "open": 0, "superseded": 0, "total": 0})
        for rule_keys_json, state, count in rows:
            try:
                keys = json.loads(rule_keys_json or "[]")
            except Exception:
                keys = []
            for k in keys:
                agg[k][state] = agg[k].get(state, 0) + count
                agg[k]["total"] += count
        out = []
        for rule, stats in sorted(agg.items()):
            total = stats["total"]
            applied = stats.get("applied", 0)
            dismissed = stats.get("dismissed", 0)
            out.append({
                "rule_key": rule,
                "total": total,
                "applied": applied,
                "dismissed": dismissed,
                "open": stats.get("open", 0),
                "superseded": stats.get("superseded", 0),
                "adoption_rate": (applied / total) if total else 0.0,
            })
        return out

    def severity_breakdown(self, since: Optional[str] = None, *, pr_url: Optional[str] = None) -> list[dict]:
        """Group suggestions by derived severity bucket.

        Severity is computed per-suggestion via ``resolve_severity`` using the
        three-layer fallback:
          1. project rule file (e.g. ``.agents/rules/*.md``) explicit map
          2. config-level pattern fallback
          3. LLM importance numeric thresholds
        The result is one row per severity bucket with the usual applied /
        dismissed / open / superseded / total / adoption_rate fields.
        """
        if self.backend != "sqlite" or self._db is None:
            return []
        clause = " WHERE posted_at >= ?" if since else ""
        params = (since,) if since else ()
        with self._lock:
            cur = self._db.execute(
                f"SELECT rule_keys, importance, state FROM suggestions{clause}",
                params,
            )
            rows = cur.fetchall()
        rule_map = _rule_severity_map_for_pr(pr_url)
        from collections import defaultdict
        agg: dict[str, dict[str, int]] = defaultdict(
            lambda: {"applied": 0, "dismissed": 0, "open": 0, "superseded": 0, "total": 0}
        )
        for rule_keys_json, importance, state in rows:
            try:
                keys = json.loads(rule_keys_json or "[]")
            except Exception:
                keys = []
            sev, _src = _resolve_severity_for_suggestion(keys, importance, rule_severity_map=rule_map)
            agg[sev][state] = agg[sev].get(state, 0) + 1
            agg[sev]["total"] += 1
        out = []
        for sev in ("critical", "high", "medium", "low", "unknown"):
            stats = agg.get(sev)
            if not stats or stats.get("total", 0) == 0:
                continue
            total = stats["total"]
            applied = stats.get("applied", 0)
            dismissed = stats.get("dismissed", 0)
            out.append({
                "severity": sev,
                "total": total,
                "applied": applied,
                "dismissed": dismissed,
                "open": stats.get("open", 0),
                "superseded": stats.get("superseded", 0),
                "adoption_rate": (applied / total) if total else 0.0,
                "dismissal_rate": (dismissed / total) if total else 0.0,
            })
        return out

    def close(self) -> None:
        if self._jsonl_fp is not None:
            try:
                self._jsonl_fp.close()
            except Exception:
                pass
            self._jsonl_fp = None
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None


_DEFAULT: Optional[TelemetryStore] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_store() -> TelemetryStore:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            backend = os.environ.get("REVIEW_TELEMETRY_BACKEND", "sqlite").strip().lower()
            if backend not in {"sqlite", "jsonl", "off"}:
                get_logger().warning(f"Unknown REVIEW_TELEMETRY_BACKEND={backend!r}, falling back to 'off'")
                backend = "off"
            _DEFAULT = TelemetryStore(backend)
    return _DEFAULT


def _row_to_mr(row):
    cols = ["mr_id", "project_id", "source_branch", "target_branch", "title", "author", "state", "opened_at", "last_seen_at", "merged_at", "url", "head_sha"]
    return dict(zip(cols, row))


def _row_to_suggestion(row):
    # Column order MUST match the live SQLite schema (PRAGMA table_info):
    #   ... dismissed_by(14), note_id(15), dismissed_reason(16).
    # The CREATE TABLE in this file lists them in the opposite order, but the
    # deployed DB was built when ``note_id`` already existed (added in an
    # earlier version as INTEGER, then migrated to TEXT, then ``dismissed_reason``
    # was ADDed later). A mismatched SELECT * column order would silently swap
    # the two fields' values in the JSON response (note_id ↔ dismissed_reason).
    cols = ["suggestion_id", "mr_id", "project_id", "file", "line", "label", "importance", "one_sentence_summary", "rule_keys", "score", "posted_at", "state", "applied_at", "dismissed_at", "dismissed_by", "note_id", "dismissed_reason"]
    d = dict(zip(cols, row))
    try:
        d["rule_keys"] = json.loads(d["rule_keys"] or "[]")
    except Exception:
        d["rule_keys"] = []
    return d


def _row_to_run(row):
    cols = ["run_id", "mr_id", "project_id", "command", "status", "model", "started_at", "finished_at", "error", "duration_ms", "suggestion_count", "rule_keys_cited", "triggered_by"]
    d = dict(zip(cols, row))
    try:
        d["rule_keys_cited"] = json.loads(d["rule_keys_cited"] or "[]")
    except Exception:
        d["rule_keys_cited"] = []
    return d


def _row_to_action(row):
    cols = ["id", "at", "action", "suggestion_id", "mr_id", "actor", "note"]
    return dict(zip(cols, row))
