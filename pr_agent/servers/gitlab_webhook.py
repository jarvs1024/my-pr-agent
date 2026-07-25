import copy
import json
import os
import re
from datetime import datetime

import uvicorn
from fastapi import APIRouter, FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTasks
from starlette.middleware import Middleware
from starlette_context import context
from starlette_context.middleware import RawContextMiddleware

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.algo.utils import update_settings_from_args
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.log import LoggingFormat, get_logger, setup_logger
from pr_agent.secret_providers import get_secret_provider
from pr_agent.servers.apply_pipeline_coordinator import (
    ApplyPipelineCoordinator,
    ApplyPipelineJob,
)
from pr_agent.servers.gitlab_suggestion_matcher import (
    find_applied_suggestion_candidates,
    target_region_changed,
)
from pr_agent.telemetry import events as telemetry_events, api as telemetry_api
from pr_agent.git_providers import get_git_provider_with_context

setup_logger(fmt=LoggingFormat.JSON, level=get_settings().get("CONFIG.LOG_LEVEL", "DEBUG"))
router = APIRouter()

secret_provider = get_secret_provider() if get_settings().get("CONFIG.SECRET_PROVIDER") else None



def _resolve_author(data: dict, fallback_author_id=None) -> str:
    """Build a display author in the form 'name@username'.

    The MR creator is the right identity for the telemetry store, NOT
    whoever triggered the most recent webhook. We prefer the nested
    ``data['object_attributes']['author']`` dict (always populated on
    MR open/merge/close). When that's missing (note events where
    ``object_attributes`` only has ``author_id`` pointing at the note
    author, not the MR creator), we fall back to that ``author_id``
    (or the caller-supplied ``fallback_author_id``) FIRST, and only
    then to the top-level ``data['user']`` (the webhook trigger actor,
    often a bot such as ``codebot``).

      * both name + username present  -> '贾克江@2268'
      * only username                 -> '2268'
      * only name                     -> '贾克江'
      * neither, only id              -> 'user:<id>'  (prefer MR-creator id)
      * no useful data                -> ''
    """
    oa = (data or {}).get('object_attributes') or {}
    author_obj = oa.get('author') or {}
    # Primary: nested author dict (the MR's creator).
    name = (author_obj.get('name') or '').strip()
    username = (author_obj.get('username') or '').strip()
    # Secondary: object_attributes.author_id or fallback_author_id
    # (the MR creator's id, used when author dict is absent).
    primary_uid = author_obj.get('id') or fallback_author_id
    # Tertiary: top-level user (the webhook trigger actor).
    user = (data or {}).get('user') or {}
    user_uid = user.get('id')
    if not name:
        name = (user.get('name') or '').strip()
    if not username:
        username = (user.get('username') or '').strip()
    if name and username:
        return f"{name}@{username}"
    if username:
        return username
    if name:
        return name
    # Last resort: prefer the MR-creator id (primary_uid) over the
    # webhook-actor id (user_uid). The actor is often a bot and not
    # the right identity for telemetry.
    uid = primary_uid if (primary_uid is not None and primary_uid != '') else user_uid
    if uid is not None and uid != '':
        return f"user:{uid}"
    return ''



def _emit_mr_activity(data: dict, state: str = "opened") -> None:
    try:
        object_attributes = data.get("object_attributes", {})
        project = data.get("project", {})
        mr_id = object_attributes.get("iid") or object_attributes.get("id")
        project_id = project.get("id")
        if not mr_id or not project_id:
            return
        last_commit = (object_attributes.get("last_commit") or {})
        head_sha = last_commit.get("id")
        author = _resolve_author(data, fallback_author_id=object_attributes.get('author_id'))
        telemetry_events.emit_mr_activity(
            mr_id=int(mr_id),
            project_id=int(project_id),
            source_branch=object_attributes.get("source_branch", ""),
            target_branch=object_attributes.get("target_branch", ""),
            title=object_attributes.get("title", ""),
            author=author,
            state=state,
            url=object_attributes.get("url"),
            head_sha=head_sha,
        )
    except Exception as e:
        get_logger().warning(f"_emit_mr_activity failed: {e}")


def _emit_mr_merged(data: dict) -> None:
    try:
        object_attributes = data.get("object_attributes", {})
        project = data.get("project", {})
        mr_id = object_attributes.get("iid") or object_attributes.get("id")
        project_id = project.get("id")
        if not mr_id or not project_id:
            return
        telemetry_events.emit_mr_activity(
            mr_id=int(mr_id),
            project_id=int(project_id),
            source_branch=object_attributes.get("source_branch", ""),
            target_branch=object_attributes.get("target_branch", ""),
            title=object_attributes.get("title", ""),
            author=_resolve_author(data, fallback_author_id=object_attributes.get('author_id')),
            state="merged",
            url=object_attributes.get("url"),
            head_sha=(object_attributes.get("last_commit") or {}).get("id"),
            merged_at=datetime.utcnow().isoformat() + "Z",
        )
    except Exception as e:
        get_logger().warning(f"_emit_mr_merged failed: {e}")


import re as _re_apply

_APPLY_COMMIT_RE = _re_apply.compile(r"Apply\s+(\d+)\s+suggestion", _re_apply.IGNORECASE)
_APPLY_PIPELINE_COORDINATOR = ApplyPipelineCoordinator()


def _resolve_apply_event(webhook_data: dict):
    """Normalize an Apply-suggestion webhook into a uniform event payload.

    Returns a dict with sha/msg/project_id/ref/mr_iid/actor/files_hint/pr_url
    when the webhook is triggered by GitLab's "Apply N suggestion(s) to N
    file(s)" commit. Returns ``None`` otherwise.

    GitLab sends two webhooks for the same Apply click:
      * ``object_kind=push`` carrying the new commit in ``commits[]`` (ref +
        file list available, but no MR iid)
      * ``object_kind=merge_request`` with ``action=update`` (iid + url
        available, no file list — fetched from GitLab commit API)

    The discriminator is the commit message itself: ``Apply N suggestion(s)``.
    """
    object_kind = webhook_data.get("object_kind")
    project = webhook_data.get("project", {}) or {}
    project_id = project.get("id")
    # push hooks expose the actor via top-level ``user_*`` fields, while
    # merge_request hooks nest it under ``user``. Fall back so we always
    # capture the pusher when present.
    actor = (
        (webhook_data.get("user") or {}).get("username")
        or webhook_data.get("user_username")
        or ""
    )

    if object_kind == "push":
        ref = webhook_data.get("ref") or ""
        commits = webhook_data.get("commits") or []
        apply_commit = None
        apply_match = None
        for c in reversed(commits):
            match = _APPLY_COMMIT_RE.search((c.get("message") or ""))
            if match:
                apply_commit = c
                apply_match = match
                break
        if apply_commit is None:
            return None
        files_hint = []
        for key in ("added", "modified", "removed"):
            for f in (apply_commit.get(key) or []):
                if f and f not in files_hint:
                    files_hint.append(f)
        return {
            "sha": apply_commit.get("id"),
            "parent_sha": webhook_data.get("before") if len(commits) == 1 else None,
            "msg": (apply_commit.get("message") or "").strip(),
            "suggestion_count": int(apply_match.group(1)),
            "project_id": project_id,
            "ref": ref,
            "mr_iid": None,
            "actor": actor,
            "files_hint": files_hint,
            "pr_url": None,
        }

    if object_kind == "merge_request":
        object_attributes = webhook_data.get("object_attributes", {}) or {}
        if object_attributes.get("action") != "update":
            return None
        last_commit = (object_attributes.get("last_commit") or {}) or {}
        sha = last_commit.get("id")
        msg = (last_commit.get("message") or "").strip()
        apply_match = _APPLY_COMMIT_RE.search(msg)
        if not (sha and apply_match):
            return None
        return {
            "sha": sha,
            "parent_sha": object_attributes.get("oldrev"),
            "msg": msg,
            "suggestion_count": int(apply_match.group(1)),
            "project_id": project_id or object_attributes.get("target_project_id"),
            "ref": None,
            "mr_iid": object_attributes.get("iid") or object_attributes.get("id"),
            "actor": actor,
            "files_hint": [],
            "pr_url": object_attributes.get("url") or object_attributes.get("web_url") or (f"{project.get('web_url','').rstrip('/')}/-/merge_requests/{object_attributes.get('iid')}" if object_attributes.get("iid") else None),
        }

    return None


def _lookup_mr_for_push(project_id, ref):
    """Resolve the MR iid + web_url for an Apply-commit pushed to ``ref``.

    Uses GitLab REST directly because the push hook doesn't carry an MR id.

    GitLab branch names can contain ``/`` (e.g. ``codex/fullflow-2026-07-15``),
    so strip only the ``refs/heads/`` prefix instead of taking the last
    ``/``-separated segment, which would drop everything before the final
    ``/`` and fail to match the MR's ``source_branch`` in the API.
    """
    try:
        import requests
        gl_token = get_settings().get("GITLAB.PERSONAL_ACCESS_TOKEN")
        base = get_settings().get("GITLAB.URL", "http://127.0.0.1:8929").rstrip("/")
        branch = ref[len("refs/heads/"):] if isinstance(ref, str) and ref.startswith("refs/heads/") else ""
        if not branch:
            return None, None
        resp = requests.get(
            f"{base}/api/v4/projects/{project_id}/merge_requests",
            params={"state": "opened", "source_branch": branch, "per_page": 5},
            headers={"PRIVATE-TOKEN": gl_token},
            timeout=10,
        )
        if resp.ok:
            for mr in resp.json():
                if mr.get("iid"):
                    return mr["iid"], mr.get("web_url") or mr.get("url")
        else:
            get_logger().warning(
                f"_lookup_mr_for_push: non-200 from GitLab "
                f"(status={resp.status_code} project={project_id} branch={branch!r})"
            )
    except Exception as e:
        get_logger().warning(f"_lookup_mr_for_push exception: {type(e).__name__}: {e}")
    return None, None


def _load_suggestion_notes(provider, discussion_ids: set[str]) -> dict[str, dict]:
    if not discussion_ids:
        return {}
    bot_usernames = _resolve_bot_usernames()
    notes_by_discussion = {}
    for discussion_summary in provider.mr.discussions.list(get_all=True):
        attributes = getattr(discussion_summary, "attributes", {}) or {}
        discussion_id = str(
            getattr(discussion_summary, "id", None) or attributes.get("id") or ""
        ).strip()
        if discussion_id not in discussion_ids:
            continue
        notes = attributes.get("notes")
        if notes is None:
            try:
                discussion = provider.mr.discussions.get(discussion_id)
                notes = (getattr(discussion, "attributes", {}) or {}).get("notes", [])
            except Exception as e:
                get_logger().warning(
                    f"telemetry.on_apply_commit discussion lookup failed: {discussion_id}: {e}"
                )
                continue
        for note in notes or []:
            if "```suggestion" not in (note.get("body") or ""):
                continue
            author = note.get("author") or {}
            author_username = str(author.get("username") or "").strip()
            if bot_usernames and author_username not in bot_usernames:
                # Discussion notes authored by a human (or unknown bot) must
                # never drive apply-attribution matching: only the configured
                # bot is allowed to publish suggestion blocks.
                get_logger().debug(
                    f"skipping non-bot note on discussion {discussion_id} (author={author_username!r})"
                )
                continue
            notes_by_discussion[discussion_id] = note
            break
    return notes_by_discussion


def _load_apply_commit_files(provider, files: list[str], parent_sha: str, current_sha: str):
    parent_files = {}
    current_files = {}
    if not parent_sha or not current_sha:
        return parent_files, current_files
    for file_path in files:
        try:
            parent_content = provider.get_pr_file_content(file_path, parent_sha)
            current_content = provider.get_pr_file_content(file_path, current_sha)
        except Exception as e:
            get_logger().warning(
                f"telemetry.on_apply_commit file lookup failed: {file_path}: {e}"
            )
            continue
        if not parent_content or not current_content:
            get_logger().warning(
                f"telemetry.on_apply_commit file content unavailable: {file_path}"
            )
            continue
        parent_files[file_path] = parent_content
        current_files[file_path] = current_content
    return parent_files, current_files


_POSTED_HEAD_SHA_SENTINEL = "__unavailable__"


def _resolve_bot_usernames() -> set[str]:
    """Resolve the set of usernames that count as the bot for suggestion ownership."""
    gitlab_user = (get_settings().get("gitlab.user", "") or "").strip()
    allowed_raw = (
        get_settings().get("config.allowed_bot_usernames")
        or get_settings().get("config", {}).get("allowed_bot_usernames")
        or []
    )
    if isinstance(allowed_raw, str):
        allowed_raw = [username.strip() for username in allowed_raw.split(",") if username.strip()]
    bot_usernames = {str(username).strip() for username in allowed_raw if str(username).strip()}
    if gitlab_user:
        bot_usernames.add(gitlab_user)
    return bot_usernames


def _validate_adopt_target_change(provider, suggestion: dict) -> tuple[bool, str]:
    posted_head_sha = str(suggestion.get("posted_head_sha") or "").strip()
    if not posted_head_sha:
        # No recorded publication metadata means we cannot compare pre /
        # post content for this suggestion, so /adopt can't be proven safe.
        return False, "posted-sha-unavailable"
    if posted_head_sha == _POSTED_HEAD_SHA_SENTINEL:
        return False, "posted-head-unavailable"
    mr = getattr(provider, "mr", None)
    diff_refs = getattr(mr, "diff_refs", {}) or {}
    current_head_sha = str(
        getattr(mr, "sha", None) or diff_refs.get("head_sha") or ""
    ).strip()
    if not current_head_sha:
        return False, "current-head-unavailable"
    if current_head_sha == posted_head_sha:
        return False, "same-head"
    file_path = str(suggestion.get("file") or "").strip()
    line = int(suggestion.get("line") or 0)
    line_end = int(suggestion.get("line_end") or line or 0)
    if not file_path or line < 1 or line_end < line:
        return False, "target-metadata-unavailable"
    try:
        posted_content = provider.get_pr_file_content(file_path, posted_head_sha)
        current_content = provider.get_pr_file_content(file_path, current_head_sha)
    except Exception as e:
        get_logger().warning(f"/adopt target content lookup failed: {e}")
        return False, "content-unavailable"
    if not posted_content or not current_content:
        return False, "content-unavailable"
    if not target_region_changed(
        posted_content,
        current_content,
        line=line,
        line_end=line_end,
        context_lines=1,
    ):
        return False, "target-unchanged"
    return True, "changed"


def _process_adopt_reply(provider, discussion_id: str, suggestion: dict,
                         actor: str, reason: str, mr_id: int) -> str:
    allowed, validation_reason = _validate_adopt_target_change(provider, suggestion)
    if not allowed:
        # ``posted-sha-unavailable`` joins the user-facing "未检测到代码修改"
        # bucket: from the reviewer's perspective, we don't know whether
        # their commit landed, so asking them to retry-without-changes is
        # the only actionable thing to say.
        if validation_reason in {"content-unavailable", "current-head-unavailable"}:
            message = "暂时无法验证这条建议对应位置的代码修改，请稍后重试 `/adopt`。"
        else:
            message = (
                "未检测到这条建议对应位置的代码修改，暂不能标记为手工采纳。"
                "请先提交修改，再回复 `/adopt [说明]`。"
            )
        try:
            provider.reply_to_comment_from_comment_id(discussion_id, message)
        except Exception as e:
            get_logger().warning(f"/adopt validation reply failed for {discussion_id}: {e}")
        get_logger().info(
            f"Rejected /adopt for suggestion {suggestion.get('suggestion_id')} "
            f"(reason={validation_reason})"
        )
        return "adopt-validation-failed"
    ok = provider.resolve_discussion(discussion_id)
    if not ok:
        get_logger().warning(f"/adopt failed to resolve discussion {discussion_id}")
        return "adopt fail"
    get_logger().info(
        f"/adopt resolved MR discussion {discussion_id}"
        + (f" (reason={reason!r})" if reason else "")
    )
    try:
        telemetry_events.mark_suggestion_adopted(
            suggestion["suggestion_id"],
            actor=actor,
            reason=reason,
            mr_id=int(mr_id or suggestion["mr_id"] or 0),
        )
    except Exception as e:
        get_logger().warning(f"telemetry.on_adopt failed: {e}")
    return "adopted"


def _handle_apply_commit(webhook_data: dict) -> None:
    """Detect GitLab's "Apply N suggestion(s)" commit and mark matching open
    suggestions as ``state=applied``.

    GitLab sends either a ``push`` hook or a ``merge_request update`` hook
    when a user clicks "Apply suggestion" on an MR thread. The commit title
    carries "Apply N suggestion(s) to N file(s)" — match that, then resolve
    the touched file(s) and flip the matching ``open`` rows to ``applied``.
    """
    try:
        ev = _resolve_apply_event(webhook_data)
        if ev is None:
            return
        project_id = ev["project_id"]
        sha = ev["sha"]
        parent_sha = ev.get("parent_sha")
        msg = ev["msg"]
        mr_id = ev.get("mr_iid")
        files_hint = ev.get("files_hint") or []
        pr_url = ev.get("pr_url")
        if not (project_id and sha):
            return
        get_logger().info(
            f"telemetry.on_apply_commit: kind={webhook_data.get('object_kind')} MR={mr_id} sha={sha[:10]} msg={msg[:60]!r}"
        )
        if not (pr_url and mr_id):
            mr_id, pr_url = _lookup_mr_for_push(project_id, ev.get("ref") or "")
        if not (pr_url and mr_id):
            get_logger().warning(
                f"telemetry.on_apply_commit: could not resolve MR for kind={webhook_data.get('object_kind')} ref={ev.get('ref')}"
            )
            return
        # Build the GitLab provider directly. ``apply_repo_settings`` +
        # ``get_git_provider_with_context`` would route via ``config.git_provider``
        # which defaults to ``github`` and crashes for GitLab URLs, so we bypass
        # the dispatcher and instantiate the right provider class.
        try:
            from pr_agent.git_providers.gitlab_provider import GitLabProvider
            provider = GitLabProvider(merge_request_url=pr_url)
        except Exception as e:
            get_logger().warning(f"telemetry.on_apply_commit provider setup: {e}")
            return
        files = list(files_hint)
        line_ranges_by_file: dict[str, list[tuple[int, int]]] = {}
        # Pull the commit directly via GitLab REST even when the push payload
        # contains file hints: hints identify files, but not which suggestion
        # lines the Apply button changed.
        try:
            import requests
            gl_token = get_settings().get("GITLAB.PERSONAL_ACCESS_TOKEN")
            base = get_settings().get("GITLAB.URL", "http://127.0.0.1:8929").rstrip("/")
            headers = {"PRIVATE-TOKEN": gl_token}
            if not parent_sha:
                commit_resp = requests.get(
                    f"{base}/api/v4/projects/{project_id}/repository/commits/{sha}",
                    headers=headers,
                    timeout=10,
                )
                if commit_resp.ok:
                    parent_ids = commit_resp.json().get("parent_ids") or []
                    parent_sha = parent_ids[0] if parent_ids else None
                else:
                    get_logger().warning(
                        f"telemetry.on_apply_commit parent lookup status={commit_resp.status_code} sha={sha[:10]}"
                    )
            url = f"{base}/api/v4/projects/{project_id}/repository/commits/{sha}/diff"
            resp = requests.get(
                url,
                headers=headers,
                params={"per_page": 50},
                timeout=10,
            )
            if resp.ok:
                for diff_entry in resp.json():
                    file = diff_entry.get("new_path") or diff_entry.get("old_path")
                    if not file:
                        continue
                    if file not in files:
                        files.append(file)
                    line_ranges_by_file[file] = _changed_new_line_ranges(diff_entry.get("diff") or "")
            else:
                get_logger().warning(
                    f"telemetry.on_apply_commit commit lookup status={resp.status_code} sha={sha[:10]}"
                )
                return
        except Exception as e:
            get_logger().warning(f"telemetry.on_apply_commit commit lookup: {e}")
            return
        if not parent_sha or not line_ranges_by_file:
            get_logger().warning(
                f"telemetry.on_apply_commit exact evidence unavailable: sha={sha[:10]} "
                f"parent={bool(parent_sha)} files={len(line_ranges_by_file)}"
            )
            return
        actor = ev.get("actor") or ""
        store = telemetry_events.get_default_store()
        open_suggestions = store.list_open_suggestion_records(int(mr_id), int(project_id))
        discussion_ids = {
            str(suggestion.get("note_id") or "").strip()
            for suggestion in open_suggestions
            if suggestion.get("note_id")
        }
        notes_by_discussion = _load_suggestion_notes(provider, discussion_ids)
        evidence_files = list(line_ranges_by_file)
        parent_files, current_files = _load_apply_commit_files(
            provider,
            evidence_files,
            parent_sha,
            sha,
        )
        candidates = find_applied_suggestion_candidates(
            open_suggestions=open_suggestions,
            notes_by_discussion=notes_by_discussion,
            parent_files=parent_files,
            current_files=current_files,
            changed_ranges_by_file=line_ranges_by_file,
        )
        expected_count = int(ev.get("suggestion_count") or 0)
        if expected_count < 1:
            match = _APPLY_COMMIT_RE.search(msg)
            expected_count = int(match.group(1)) if match else 0
        # Tolerant match: the user clicked Apply N times, but several
        # historical /improve rounds may have posted duplicate open
        # suggestions for the same line. Mark exactly ``expected_count``
        # of the candidates (oldest first) and leave the duplicates for
        # the next /improve to retire as superseded.
        if expected_count < 1:
            get_logger().warning(
                "telemetry.on_apply_commit expected count unresolved: "
                f"project={project_id} mr={mr_id} sha={sha[:10]}"
            )
            return
        if len(candidates) < expected_count:
            get_logger().warning(
                "telemetry.on_apply_commit exact match failed: "
                f"project={project_id} mr={mr_id} sha={sha[:10]} "
                f"expected={expected_count} matched={len(candidates)}"
            )
            return
        apply_ids = candidates[:expected_count]
        updated = telemetry_events.mark_suggestion_ids_applied(
            mr_id=int(mr_id),
            project_id=int(project_id),
            suggestion_ids=apply_ids,
            actor=actor,
            apply_event_sha=sha,
        )
        if updated:
            get_logger().info(
                f"telemetry.on_apply_commit: marked {len(updated)} suggestion(s) applied "
                f"(expected={expected_count}, candidates={len(candidates)}): {updated[:3]}"
            )
    except Exception as e:
        get_logger().warning(f"telemetry.on_apply_commit outer: {e}")


def _changed_new_line_ranges(diff: str) -> list[tuple[int, int]]:
    """Return inclusive ranges for added/replaced lines in a unified diff."""
    ranges: list[tuple[int, int]] = []
    new_line = None
    range_start = None
    deleted_before_addition = False
    deleted_lines_blank = False

    def close_range() -> None:
        nonlocal range_start
        if range_start is not None and new_line is not None:
            ranges.append((range_start, new_line if not deleted_before_addition else new_line - 1))
            range_start = None

    for line in diff.splitlines():
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk:
            close_range()
            new_line = int(hunk.group(1))
            deleted_before_addition = False
            deleted_lines_blank = False
            continue
        if new_line is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if range_start is None:
                if deleted_before_addition and deleted_lines_blank:
                    range_start = max(1, new_line - 1)
                else:
                    range_start = new_line if deleted_before_addition else max(1, new_line - 1)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            close_range()
            if not deleted_before_addition:
                deleted_lines_blank = True
            deleted_lines_blank = deleted_lines_blank and not line[1:].strip()
            deleted_before_addition = True
        else:
            close_range()
            deleted_before_addition = False
            deleted_lines_blank = False
            new_line += 1
    close_range()
    return ranges


def _resolve_superseded_suggestions(pr_url: str) -> list[str]:
    if not pr_url:
        return []
    try:
        gitlab_user = (get_settings().get("gitlab.user", "") or "").strip()
        allowed_raw = (
            get_settings().get("config.allowed_bot_usernames")
            or get_settings().get("config", {}).get("allowed_bot_usernames")
            or []
        )
        if isinstance(allowed_raw, str):
            allowed_raw = [username.strip() for username in allowed_raw.split(",") if username.strip()]
        bot_usernames = set(allowed_raw)
        if gitlab_user:
            bot_usernames.add(gitlab_user)

        provider = get_git_provider_with_context(pr_url)
        resolved_discussions = provider.resolve_superseded_suggestion_discussions(bot_usernames)
        actor = gitlab_user or (sorted(bot_usernames)[0] if bot_usernames else "pr-agent")
        store = telemetry_events.get_default_store()
        for discussion_id in resolved_discussions:
            suggestion = store.get_suggestion_by_note_id(discussion_id)
            # Only resolve supersede for ``open`` rows: applied rows represent
            # a user action (Apply button or /adopt) and must keep their
            # attribution even when GitLab auto-resolves the discussion
            # because the source line is gone.
            if suggestion is None or suggestion.get("state") != "open":
                continue
            telemetry_events.mark_suggestion_superseded(suggestion["suggestion_id"])
            telemetry_events.emit_action(
                action="resolved",
                suggestion_id=suggestion["suggestion_id"],
                mr_id=int(suggestion.get("mr_id") or 0),
                actor=actor,
                note=f"superseded by source update; resolved discussion {discussion_id}",
            )
        return resolved_discussions
    except Exception as error:
        get_logger().warning(f"Failed to resolve superseded suggestions for {pr_url}: {error}")
        return []

async def handle_request(api_url: str, body: str, log_context: dict, sender_id: str, notify=None):
    log_context["action"] = body
    log_context["event"] = "pull_request" if body == "/review" else "comment"
    log_context["api_url"] = api_url
    log_context["app_name"] = get_settings().get("CONFIG.APP_NAME", "Unknown")

    with get_logger().contextualize(**log_context):
        await PRAgent().handle_request(api_url, body, notify)

async def _perform_commands_gitlab(commands_conf: str, agent: PRAgent, api_url: str,
                                   log_context: dict, data: dict):
    apply_repo_settings(api_url)
    if commands_conf == "pr_commands" and get_settings().config.disable_auto_feedback:  # auto commands for PR, and auto feedback is disabled
        get_logger().info(f"Auto feedback is disabled, skipping auto commands for PR {api_url=}", **log_context)
        return
    if not should_process_pr_logic(data): # Here we already updated the configurations
        return
    commands = get_settings().get(f"gitlab.{commands_conf}", {})
    get_settings().set("config.is_auto_command", True)
    for command in commands:
        try:
            split_command = command.split(" ")
            command = split_command[0]
            args = split_command[1:]
            other_args = update_settings_from_args(args)
            new_command = ' '.join([command] + other_args)
            get_logger().info(f"Performing command: {new_command}")
            with get_logger().contextualize(**log_context):
                await agent.handle_request(api_url, new_command)
        except Exception as e:
            get_logger().error(f"Failed to perform command {command}: {e}")


async def _drain_apply_pipeline(initial_job: ApplyPipelineJob) -> None:
    current_job = initial_job
    while current_job is not None:
        try:
            _resolve_superseded_suggestions(current_job.pr_url)
            await _perform_commands_gitlab(
                current_job.commands_conf,
                PRAgent(),
                current_job.pr_url,
                current_job.log_context,
                current_job.data,
            )
        except Exception as e:
            get_logger().warning(f"Apply pipeline re-run failed: {e}")
        finally:
            current_job = _APPLY_PIPELINE_COORDINATOR.complete(current_job)


def is_bot_user(data) -> bool:
    try:
        # logic to ignore bot users (unlike Github, no direct flag for bot users in gitlab)
        sender_name = (data.get("user", {}).get("name") or "unknown").lower()
        sender_username = (data.get("user", {}).get("username") or "").lower()
        # Resolve allowed_bot_usernames from any of the Dynaconf lookup forms
        # used in this repo: ``config.allowed_bot_usernames`` (dotted path),
        # ``config -> allowed_bot_usernames`` (nested), or a comma-separated
        # string from an env var override. ``get_settings().get(...)`` returns
        # ``None`` for missing keys and an empty ``BoxList`` for an explicitly
        # empty list, so coerce both to ``[]`` before iterating.
        allowed_raw = (
            get_settings().get("config.allowed_bot_usernames")
            or get_settings().get("config", {}).get("allowed_bot_usernames")
            or []
        )
        if isinstance(allowed_raw, str):
            allowed_raw = [a for a in allowed_raw.split(",") if a.strip()]
        allowed = {a.lower() for a in allowed_raw}
        # Whitelist check first: a CI job / service account whose username is
        # explicitly allowed must run regardless of bot-like naming, so the
        # agent never drops a legitimate trigger.
        if sender_username and sender_username in allowed:
            return False
        # Generic bot detection runs after the whitelist so an allowed
        # username like ``review-bot`` is never matched by the trailing
        # ``-bot`` rule.
        bot_indicators = ['codium', 'bot_', 'bot-', '_bot', '-bot']
        if any(indicator in sender_name for indicator in bot_indicators):
            get_logger().info(f"Skipping GitLab bot user: {sender_name}")
            return True
        if (sender_username.endswith("-bot") or sender_username.endswith("_bot") or sender_username.endswith("bot")):
            get_logger().info(f"Skipping GitLab bot username: {sender_username}")
            return True
    except Exception as e:
        get_logger().error(f"Failed 'is_bot_user' logic: {e}")
    return False
def _is_sender_bot_account(data) -> bool:
    """Return True when the webhook actor's username looks like a bot account.

    Unlike :func:`is_bot_user` this helper is **not** short-circuited by the
    ``config.allowed_bot_usernames`` whitelist. ``allowed_bot_usernames`` is
    there to let a bot account trigger /review /improve etc. (the early return
    in the comment handler), not to decide whether the bot can speak for the
    user when parsing /dismiss commands. A bot replying to its own inline
    suggestion must never trigger ``resolve_discussion`` — the suggestion body
    it posted already contains the literal word ``dismiss`` ("回复 ``/dismiss``
    忽略原因"), which would otherwise be matched and resolved.

    Returns ``True`` if the sender username ends with ``-bot`` / ``_bot`` or
    the sender display name contains ``codium`` / ``bot_`` / ``bot-`` / ``_bot``
    / ``-bot`` substrings. Returns ``False`` for a regular human username such
    as ``jarvs`` so a real ``/dismiss 误报`` reply is still resolved.
    """
    try:
        sender_name = (data.get("user", {}).get("name") or "").lower()
        sender_username = (data.get("user", {}).get("username") or "").lower()
        if not sender_username:
            return False
        if (sender_username.endswith("-bot") or sender_username.endswith("_bot") or sender_username.endswith("bot")):
            return True
        bot_indicators = ['codium', 'bot_', 'bot-', '_bot', '-bot']
        if any(indicator in sender_name for indicator in bot_indicators):
            return True
    except Exception:
        pass
    return False


def is_draft(data) -> bool:
    try:
        if 'draft' in data.get('object_attributes', {}):
            return data['object_attributes']['draft']

        # for gitlab server version before 16
        elif 'Draft:' in data.get('object_attributes', {}).get('title'):
            return True
    except Exception as e:
        get_logger().error(f"Failed 'is_draft' logic: {e}")
    return False

def is_draft_ready(data) -> bool:
    try:
        if 'draft' in data.get('changes', {}):
            # Handle both boolean values and string values for compatibility
            previous = data['changes']['draft']['previous']
            current = data['changes']['draft']['current']

            # Convert to boolean if they're strings
            if isinstance(previous, str):
                previous = previous.lower() == 'true'
            if isinstance(current, str):
                current = current.lower() == 'true'

            if previous is True and current is False:
                return True

        # for gitlab server version before 16
        elif 'title' in data.get('changes', {}):
            if 'Draft:' in data['changes']['title']['previous'] and 'Draft:' not in data['changes']['title']['current']:
                return True
    except Exception as e:
        get_logger().error(f"Failed 'is_draft_ready' logic: {e}")
    return False

def should_process_pr_logic(data) -> bool:
    try:
        # ``object_attributes`` is only present on merge_request payloads,
        # not on push hooks. Allow push hooks through so the apply-pipeline
        # can run when an Apply-suggestion commit is pushed; the per-MR
        # ignore rules (title, labels, target_branch) simply won't apply.
        # Without this guard, the apply-pipeline silently no-ops on push
        # hooks because ``_perform_commands_gitlab`` short-circuits on
        # ``should_process_pr_logic == False`` and never logs the run.
        if not data.get('object_attributes', {}) and data.get('object_kind') != 'push':
            return False
        title = data.get('object_attributes', {}).get('title')
        sender = data.get("user", {}).get("username", "")
        repo_full_name = data.get('project', {}).get('path_with_namespace', "")

        # logic to ignore PRs from specific repositories
        ignore_repos = get_settings().get("CONFIG.IGNORE_REPOSITORIES", [])
        if ignore_repos and repo_full_name:
            if any(re.search(regex, repo_full_name) for regex in ignore_repos):
                get_logger().info(f"Ignoring MR from repository '{repo_full_name}' due to 'config.ignore_repositories' setting")
                return False

        # logic to ignore PRs from specific users
        ignore_pr_users = get_settings().get("CONFIG.IGNORE_PR_AUTHORS", [])
        if ignore_pr_users and sender:
            if any(re.search(regex, sender) for regex in ignore_pr_users):
                get_logger().info(f"Ignoring PR from user '{sender}' due to 'config.ignore_pr_authors' settings")
                return False

        # logic to ignore MRs for titles, labels and source, target branches.
        ignore_mr_title = get_settings().get("CONFIG.IGNORE_PR_TITLE", [])
        ignore_mr_labels = get_settings().get("CONFIG.IGNORE_PR_LABELS", [])
        ignore_mr_source_branches = get_settings().get("CONFIG.IGNORE_PR_SOURCE_BRANCHES", [])
        ignore_mr_target_branches = get_settings().get("CONFIG.IGNORE_PR_TARGET_BRANCHES", [])

        #
        if ignore_mr_source_branches:
            source_branch = data.get('object_attributes', {}).get('source_branch')
            if any(re.search(regex, source_branch) for regex in ignore_mr_source_branches):
                get_logger().info(
                    f"Ignoring MR with source branch '{source_branch}' due to gitlab.ignore_mr_source_branches settings")
                return False

        if ignore_mr_target_branches:
            target_branch = data.get('object_attributes', {}).get('target_branch')
            if any(re.search(regex, target_branch) for regex in ignore_mr_target_branches):
                get_logger().info(
                    f"Ignoring MR with target branch '{target_branch}' due to gitlab.ignore_mr_target_branches settings")
                return False

        if ignore_mr_labels:
            labels = [label['title'] for label in data.get('object_attributes', {}).get('labels', [])]
            if any(label in ignore_mr_labels for label in labels):
                labels_str = ", ".join(labels)
                get_logger().info(f"Ignoring MR with labels '{labels_str}' due to gitlab.ignore_mr_labels settings")
                return False

        if ignore_mr_title:
            # title is None for push hooks (no MR object); skip the
            # regex match so we don't TypeError on ``re.search(None, ...)``
            # and end up returning False from the outer except.
            if title and any(re.search(regex, title) for regex in ignore_mr_title):
                get_logger().info(f"Ignoring MR with title '{title}' due to gitlab.ignore_mr_title settings")
                return False
    except Exception as e:
        get_logger().error(f"Failed 'should_process_pr_logic': {e}")
    return True


@router.post("/webhook")
async def gitlab_webhook(background_tasks: BackgroundTasks, request: Request):
    start_time = datetime.now()
    request_json = await request.json()
    context["settings"] = copy.deepcopy(global_settings)

    async def inner(data: dict):
        log_context = {"server_type": "gitlab_app"}
        get_logger().debug("Received a GitLab webhook")
        if request.headers.get("X-Gitlab-Token") and secret_provider:
            request_token = request.headers.get("X-Gitlab-Token")
            secret = secret_provider.get_secret(request_token)
            if not secret:
                get_logger().warning(f"Empty secret retrieved, request_token: {request_token}")
                return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED,
                                    content=jsonable_encoder({"message": "unauthorized"}))
            try:
                secret_dict = json.loads(secret)
                gitlab_token = secret_dict["gitlab_token"]
                log_context["token_id"] = secret_dict.get("token_name", secret_dict.get("id", "unknown"))
                context["settings"].gitlab.personal_access_token = gitlab_token
            except Exception as e:
                get_logger().error(f"Failed to validate secret {request_token}: {e}")
                return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content=jsonable_encoder({"message": "unauthorized"}))
        elif get_settings().get("GITLAB.SHARED_SECRET"):
            secret = get_settings().get("GITLAB.SHARED_SECRET")
            if not request.headers.get("X-Gitlab-Token") == secret:
                get_logger().error("Failed to validate secret")
                return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content=jsonable_encoder({"message": "unauthorized"}))
        else:
            get_logger().error("Failed to validate secret")
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content=jsonable_encoder({"message": "unauthorized"}))
        gitlab_token = get_settings().get("GITLAB.PERSONAL_ACCESS_TOKEN", None)
        if not gitlab_token:
            get_logger().error("No gitlab token found")
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content=jsonable_encoder({"message": "unauthorized"}))

        get_logger().info("GitLab data", artifact=data)
        sender = data.get("user", {}).get("username", "unknown")
        sender_id = data.get("user", {}).get("id", "unknown")

        # ignore bot users
        if is_bot_user(data):
            return JSONResponse(status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "success"}))

        log_context["sender"] = sender
        # Detect GitLab "Apply N suggestion(s)" commits from either push or
        # merge_request-update webhooks. Runs before the main object_kind
        # dispatcher so it covers push hooks (which have no merge_request
        # branch in this handler). Returns success on hit to avoid the
        # dispatcher's normal push/MR handling running again on the same event.
        try:
            apply_event = _resolve_apply_event(data)
            if apply_event is not None:
                _handle_apply_commit(data)
                # Description-only / label-only MR updates carry ``last_commit``
                # whose message still matches the original "Apply N suggestion(s)"
                # commit. Re-running ``apply_commands`` on those would call
                # ``/describe``, which rewrites the description, which fires
                # yet another MR update webhook, which matches the regex
                # again — an infinite ``/describe`` loop on the timeline.
                # Require the update to be push-driven (``oldrev`` set on the
                # ``merge_request`` payload, OR the payload is a ``push`` hook)
                # before chaining the apply pipeline.
                apply_is_push_driven = (
                    data.get("object_kind") == "push"
                    or bool((data.get("object_attributes") or {}).get("oldrev"))
                )
                if not apply_is_push_driven:
                    get_logger().info(
                        "apply-pipeline: skipping apply_commands re-run for "
                        "non-push-driven MR update (description-only change); "
                        "telemetry already updated."
                    )
                    return JSONResponse(
                        status_code=status.HTTP_200_OK,
                        content=jsonable_encoder({"message": "apply-telemetry-only"})
                    )
                # Also keep the MR activity row in telemetry consistent
                # with the ``updated`` state the dispatcher would have
                # emitted had we let control fall through.
                try:
                    mr_url = (
                        (data.get("object_attributes") or {}).get("url")
                    )
                    if mr_url:
                        _emit_mr_activity(data, state='updated')
                except Exception as _emit_apply_err:
                    get_logger().warning(
                        f"apply-path _emit_mr_activity failed: {_emit_apply_err}"
                    )
                apply_mr_iid = apply_event.get("mr_iid")
                apply_url = apply_event.get("pr_url")
                if not apply_url:
                    lookup_mr_id, lookup_url = _lookup_mr_for_push(
                        apply_event.get("project_id") or (data.get("project") or {}).get("id"),
                        apply_event.get("ref") or "",
                    )
                    if lookup_mr_id:
                        apply_mr_iid = lookup_mr_id
                    if lookup_url:
                        apply_url = lookup_url
                    elif apply_mr_iid:
                        project_web = (data.get("project") or {}).get("web_url", "").rstrip("/")
                        if project_web:
                            apply_url = f"{project_web}/-/merge_requests/{apply_mr_iid}"
                apply_cmds = get_settings().get("gitlab.apply_commands", []) or []
                push_cmds = get_settings().get("gitlab.push_commands", []) or []
                pipeline_commands = apply_cmds or push_cmds
                if pipeline_commands and apply_url:
                    if not apply_mr_iid:
                        match = re.search(r"/merge_requests/(\d+)(?:$|[/?#])", apply_url)
                        if match:
                            apply_mr_iid = int(match.group(1))
                    if not apply_mr_iid:
                        get_logger().warning(
                            f"apply-pipeline: unable to resolve MR iid for {apply_url}; skipping commands"
                        )
                        return JSONResponse(
                            status_code=status.HTTP_200_OK,
                            content=jsonable_encoder({"message": "apply-target-unresolved"}),
                        )
                    job = ApplyPipelineJob(
                        project_id=int(apply_event.get("project_id") or 0),
                        mr_iid=int(apply_mr_iid),
                        sha=str(apply_event.get("sha") or ""),
                        pr_url=apply_url,
                        data=copy.deepcopy(data),
                        log_context=copy.deepcopy(log_context),
                        commands_conf="apply_commands" if apply_cmds else "push_commands",
                    )
                    decision = _APPLY_PIPELINE_COORDINATOR.enqueue(job)
                    if decision == "duplicate":
                        get_logger().info(
                            "apply-pipeline: duplicate webhook for the same Apply commit; skipping commands."
                        )
                        return JSONResponse(
                            status_code=status.HTTP_200_OK,
                            content=jsonable_encoder({"message": "apply-duplicate-skip"}),
                        )
                    if decision == "queued":
                        get_logger().info(
                            f"apply-pipeline: queued latest Apply commit for MR !{apply_mr_iid}: {job.sha[:10]}"
                        )
                        return JSONResponse(
                            status_code=status.HTTP_200_OK,
                            content=jsonable_encoder({"message": "apply-pipeline-queued"}),
                        )
                    await _drain_apply_pipeline(job)
                return JSONResponse(status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "apply-handled"}))
        except Exception as e:
            # Catch any unexpected error from the apply-pipeline so we
            # don't fall through to the main dispatcher (which would
            # re-run push_commands on the same event and produce
            # duplicate review comments + /improve suggestions).
            # Each inner step (_handle_apply_commit, _emit_mr_activity,
            # the apply_commands chain) already has its own try/except,
            # so this is a defence-in-depth net for genuinely
            # unexpected exceptions (e.g. JSON encoding failures).
            get_logger().warning(f"apply-pipeline failed: {e}")
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=jsonable_encoder({"message": "apply-error"}),
            )
        if data.get('object_kind') == 'merge_request':
            # ignore MRs based on title, labels, source and target branches
            if not should_process_pr_logic(data):
                return JSONResponse(status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "success"}))
            object_attributes = data.get('object_attributes', {})
            if object_attributes.get('action') in ['open', 'reopen']:
                url = object_attributes.get('url')
                get_logger().info(f"New merge request: {url}")
                _emit_mr_activity(data, state='opened')
                if is_draft(data):
                    get_logger().info(f"Skipping draft MR: {url}")
                    return JSONResponse(status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "success"}))

                await _perform_commands_gitlab("pr_commands", PRAgent(), url, log_context, data)

            # action=update: a push happened on the MR source branch. The
            # Apply-suggestion path is handled by the early-exit hook above
            # (it matches the commit message regardless of object_kind).
            elif object_attributes.get('action') == 'update':
                url = object_attributes.get('url')
                # Skip purely cosmetic MR updates that don't carry a push:
                # title / description / label / assignee edits don't have an
                # ``oldrev`` and don't add new code to review. Running
                # ``pr_commands`` or ``push_commands`` here causes the bot
                # to post a fresh ``/describe``/``/review``/``/improve``
                # cycle every time the bot's own previous ``/describe``
                # writes the description — an infinite loop. Real code
                # pushes always include ``oldrev``; we only re-run commands
                # in that case (and the apply-suggestion chain still runs
                # via the early-exit hook above).
                if not object_attributes.get('oldrev'):
                    # Still record the activity so the dashboard reflects
                    # the description edit, but don't re-run the full
                    # /describe + /review + /improve pipeline (which would
                    # create the infinite-loop cycle described above).
                    try:
                        _emit_mr_activity(data, state='updated')
                    except Exception as _emit_meta_err:
                        get_logger().warning(
                            f"non-push update _emit_mr_activity failed: {_emit_meta_err}"
                        )
                    get_logger().debug(
                        f"Skipping non-push MR update for {url} "
                        f"(no oldrev; description-only or label-only change)"
                    )
                    return JSONResponse(status_code=status.HTTP_200_OK,
                                        content=jsonable_encoder({"message": "non-push-update-skip"}))
                # Apply-suggestion detection moved to the early-exit hook above
                # so it also fires for object_kind=push webhooks.
                if is_draft(data):
                    get_logger().info(f"Skipping draft MR: {url}")
                    return JSONResponse(status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "success"}))

                # Real push (oldrev set) — apply repo settings before checking
                # push commands or handle_push_trigger.
                if object_attributes.get('oldrev'):
                    apply_repo_settings(url)
                    _resolve_superseded_suggestions(url)
                    commands_on_push = get_settings().get(f"gitlab.push_commands", {})
                    handle_push_trigger = get_settings().get(f"gitlab.handle_push_trigger", False)
                    if not commands_on_push or not handle_push_trigger:
                        get_logger().info("Push event, but no push commands found or push trigger is disabled")
                        return JSONResponse(status_code=status.HTTP_200_OK,
                                            content=jsonable_encoder({"message": "success"}))
                    get_logger().debug(f'A push event has been received: {url}')
                    await _perform_commands_gitlab("push_commands", PRAgent(), url, log_context, data)
                
            elif object_attributes.get('action') == 'merge':
                url = object_attributes.get('url')
                _emit_mr_merged(data)

            elif object_attributes.get('action') == 'close':
                url = object_attributes.get('url')
                get_logger().info(f"MR closed: {url}")
                # Reflect the new state in the telemetry store so the
                # /api/v1/telemetry/mrs endpoint surfaces 'closed' instead
                # of the stale 'updated' we previously captured.
                try:
                    _emit_mr_activity(data, state='closed')
                except Exception as e:
                    get_logger().warning(f"close-handler emit failed: {e}")

            # for draft to ready triggered merge requests
            elif object_attributes.get('action') == 'update' and is_draft_ready(data):
                url = object_attributes.get('url')
                get_logger().info(f"Draft MR is ready: {url}")

                # same as open MR
                await _perform_commands_gitlab("pr_commands", PRAgent(), url, log_context, data)

        elif data.get('object_kind') == 'note' and data.get('event_type') == 'note': # comment on MR
            if 'merge_request' in data:
                mr = data['merge_request']
                url = mr.get('url')
                obj_attrs = data.get('object_attributes', {})
                comment_id = obj_attrs.get('id')
                body = obj_attrs.get('note', '')
                note_type = obj_attrs.get('type')
                # Upsert the MR row into telemetry so /api/v1/telemetry/mrs
                # surfaces comment-triggered runs. _emit_mr_activity reads
                # `data['object_attributes']['iid']` and `data['project']['id']`,
                # so we re-shape the comment payload before delegating.
                try:
                    mr_id = mr.get('iid') or mr.get('id')
                    project_id = data.get('project', {}).get('id')
                    if mr_id and project_id:
                        _emit_mr_activity({
                            "object_attributes": {
                                "iid": mr_id,
                                "id": mr_id,
                                "source_branch": mr.get('source_branch', ''),
                                "target_branch": mr.get('target_branch', ''),
                                "title": mr.get('title', ''),
                                "url": url,
                                "last_commit": mr.get('last_commit') or {},
                                "author_id": mr.get('author_id'),
                            },
                            "project": {"id": project_id},
                            # NOTE: deliberately NOT forwarding data["user"] here.
                            # The webhook actor (data["user"]) is usually the note
                            # commenter (often a bot like "codebot"), NOT the MR
                            # creator. We want the MR creator, so _resolve_author
                            # will fall back to object_attributes.author_id above.
                        }, state=mr.get('state') or 'updated')
                except Exception as _emit_err:
                    get_logger().warning(f"comment-path _emit_mr_activity failed: {_emit_err}")

                # /dismiss on a reply to an inline suggestion → resolve that MR discussion.
                # GitLab marks inline comments as DiffNote whether they start a new thread
                # or reply to an existing one; the discriminator is that a reply carries a
                # discussion_id pointing at the parent thread.
                #
                # Matching is intentionally permissive so the user can write any of:
                #   /dismiss 误报
                #   /dismiss 忽略原因测试
                #   dismiss 忽略
                #   ?dismiss 忽略
                #   '/dismiss 忽略原因测试'   (note wrapped in curly/straight quotes by the UI)
                #   dismiss忽略               (no separator)
                # The rule: any DiffNote whose body contains the word `dismiss` (case-
                # insensitive) is treated as a dismiss command. The reason is whatever
                # non-empty text remains after stripping the trigger keyword and trivial
                # wrappers around it (leading/trailing `/`, `?`, quotes, whitespace,
                # and common CJK / ASCII punctuation).
                # Sender guard: a bot account (e.g. ``review-bot``) must not trigger
                # ``resolve_discussion`` when its own DiffNote body happens to contain
                # the literal word ``dismiss``. Inline suggestions posted by the bot
                # include the helper text "回复 ``/dismiss`` 忽略原因", so a naive
                # substring match against the bot's own note would self-resolve every
                # freshly posted suggestion. ``is_bot_user`` returns False for
                # ``review-bot`` because ``allowed_bot_usernames`` is whitelisted (so
                # the bot can still trigger /review etc.), so we use the whitelist-
                # independent ``_is_sender_bot_account`` here. A real human /dismiss
                # reply (note body starts with /dismiss or dismiss) is still matched.
                _sender_is_bot = _is_sender_bot_account(data)
                _body_stripped = body.lstrip()
                # /adopt: accept the suggestion (optionally with a reason), even if
                # the user rewrote it manually. Matched BEFORE /dismiss so a body
                # like "/adopt 用 dismiss 风格重写" still routes to /adopt (the
                # user explicitly chose /adopt, so their intent wins). When
                # neither /adopt nor /dismiss keywords appear, the body is
                # treated as a normal comment and falls through to the regular
                # handler_request path. Bot guard mirrors /dismiss: a bot user's
                # own DiffNote containing the literal word `adopt` must not
                # trigger anything. The reason is the non-empty text that remains
                # after stripping the trigger keyword and trivial wrappers
                # (leading/trailing `/`, `?`, quotes, whitespace and the usual
                # CJK / ASCII punctuation, including the full-width colon
                # U+FF1A so a trailing "：" does not pollute the reason).
                _adopt_match = re.search(r"(?<![A-Za-z0-9])adopt(?![A-Za-z0-9])", body, flags=re.IGNORECASE)
                if note_type == 'DiffNote' and _adopt_match and not _sender_is_bot:
                    _adopt_word = _adopt_match.group(0)
                    _before, _sep, _after = body.partition(_adopt_word)
                    _reason = (_before + _after).strip()
                    _wrapper_strip = re.compile(
                        r'^[\s/\\?"\'\u2018\u2019\u201c\u201d,;:。,;:：!\-—_()]+'
                        r'|'
                        r'[\s/\\?"\'\u2018\u2019\u201c\u201d,;:。,;:：!\-—_()]+$'
                    )
                    _reason = _wrapper_strip.sub('', _reason).strip()
                    discussion_id = obj_attrs.get('discussion_id')
                    if discussion_id:
                        suggestion = telemetry_events.get_default_store().get_suggestion_by_note_id(discussion_id)
                        if suggestion is None or suggestion.get('state') not in ('open', 'applied'):
                            get_logger().info(
                                f"Ignoring /adopt for non-open suggestion {discussion_id} "
                                f"(state={suggestion and suggestion.get('state')})"
                            )
                            return JSONResponse(
                                status_code=status.HTTP_200_OK,
                                content=jsonable_encoder({'message': 'adopt-skipped-state'}),
                            )
                        provider = get_git_provider_with_context(pr_url=url)
                        author = data.get('user', {}).get('username', '')
                        mr_iid = data.get('merge_request', {}).get('iid')
                        result = _process_adopt_reply(
                            provider=provider,
                            discussion_id=discussion_id,
                            suggestion=suggestion,
                            actor=author,
                            reason=_reason,
                            mr_id=int(mr_iid or suggestion['mr_id'] or 0),
                        )
                        return JSONResponse(
                            status_code=status.HTTP_200_OK,
                            content=jsonable_encoder({'message': result}),
                        )

                _body_looks_like_explicit_dismiss = (
                    _body_stripped.lower().startswith('/dismiss')
                    or _body_stripped.lower().startswith('dismiss')
                    or _body_stripped.startswith('?dismiss')
                )
                if _sender_is_bot and not _body_looks_like_explicit_dismiss:
                    get_logger().info(
                        f"Ignoring dismiss keyword in bot-authored DiffNote from "
                        f"{data.get('user', {}).get('username')!r} "
                        f"(body does not look like an explicit /dismiss command)."
                    )
                    _dismiss_match = None
                else:
                    _dismiss_match = re.search(r'(?<![A-Za-z0-9])dismiss(?![A-Za-z0-9])', body, flags=re.IGNORECASE)
                if note_type == 'DiffNote' and _dismiss_match:
                    # Split on the matched keyword and strip wrappers on both halves so
                    # `/dismiss 忽略原因测试` → `忽略原因测试` and `dismiss忽略` → `忽略`.
                    _dismiss_word = _dismiss_match.group(0)
                    _before, _sep, _after = body.partition(_dismiss_word)
                    _reason = (_before + _after).strip()
                    _wrapper_strip = re.compile(
                        r'^[\s/\\?"\'\u2018\u2019\u201c\u201d,;:。,;:：!\-—_()]+'
                        r'|'
                        r'[\s/\\?"\'\u2018\u2019\u201c\u201d,;:。,;:：!\-—_()]+$'
                    )
                    _reason = _wrapper_strip.sub('', _reason).strip()
                    discussion_id = obj_attrs.get('discussion_id')
                    if discussion_id:
                        provider = get_git_provider_with_context(pr_url=url)
                        ok = provider.resolve_discussion(discussion_id)
                        if ok:
                            get_logger().info(
                                f"/dismiss resolved MR discussion {discussion_id}"
                                + (f" (reason={_reason!r})" if _reason else ""))
                            # Telemetry: attribute the dismiss to the suggestion it resolved.
                            # The discussion id is the GitLab-side suggestion id stored on the
                            # suggestion row at publish time (see pr_code_suggestions.py).
                            # The user-supplied reason is persisted on the suggestion row
                            # (dismissed_reason) and surfaced via the dismissals API for
                            # downstream rule-tuning.
                            try:
                                author = data.get("user", {}).get("username", "")
                                mr_iid = data.get("merge_request", {}).get("iid")
                                suggestion = telemetry_events.get_default_store().get_suggestion_by_note_id(discussion_id)
                                if suggestion is not None:
                                    _action_note = f"resolved discussion {discussion_id}"
                                    if _reason:
                                        _action_note = f"{_action_note}; reason: {_reason}"
                                    telemetry_events.emit_action(
                                        action="dismissed",
                                        suggestion_id=suggestion["suggestion_id"],
                                        mr_id=int(mr_iid or suggestion["mr_id"] or 0),
                                        actor=author,
                                        note=_action_note,
                                    )
                                    telemetry_events.mark_suggestion_dismissed(
                                        suggestion["suggestion_id"],
                                        actor=author,
                                        reason=_reason,
                                    )
                            except Exception as e:
                                get_logger().warning(f"telemetry.on_dismiss failed: {e}")
                        else:
                            get_logger().warning(f"/dismiss failed to resolve discussion {discussion_id}")
                        return JSONResponse(status_code=status.HTTP_200_OK,
                                            content=jsonable_encoder({"message": "dismissed" if ok else "dismiss fail"}))
                    # top-level /dismiss on a new inline note — fall through to normal handling

                provider = get_git_provider_with_context(pr_url=url)
                get_logger().info(f"A comment has been added to a merge request: {url}")
                if note_type == 'DiffNote' and '/ask' in body: # /ask_line
                    body = handle_ask_line(body, data)

                await handle_request(url, body, log_context, sender_id, notify=lambda: provider.add_eyes_reaction(comment_id))

    background_tasks.add_task(inner, request_json)
    end_time = datetime.now()
    get_logger().info(f"Processing time: {end_time - start_time}", request=request_json)
    return JSONResponse(status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "success"}))


def handle_ask_line(body, data):
    try:
        line_range_ = data['object_attributes']['position']['line_range']
        # if line_range_['start']['type'] == 'new':
        start_line = line_range_['start']['new_line']
        end_line = line_range_['end']['new_line']
        # else:
        #     start_line = line_range_['start']['old_line']
        #     end_line = line_range_['end']['old_line']
        question = body.replace('/ask', '').strip()
        path = data['object_attributes']['position']['new_path']
        side = 'RIGHT'  # if line_range_['start']['type'] == 'new' else 'LEFT'
        comment_id = data['object_attributes']["discussion_id"]
        get_logger().info("Handling line ")
        body = f"/ask_line --line_start={start_line} --line_end={end_line} --side={side} --file_name={path} --comment_id={comment_id} {question}"
    except Exception as e:
        get_logger().error(f"Failed to handle ask line comment: {e}")
    return body


@router.get("/")
async def root():
    return {"status": "ok"}

gitlab_url = get_settings().get("GITLAB.URL", None)
if not gitlab_url:
    raise ValueError("GITLAB.URL is not set")
get_settings().config.git_provider = "gitlab"
middleware = [Middleware(RawContextMiddleware)]
app = FastAPI(middleware=middleware)
app.include_router(router)
telemetry_api.install_routes(app)


def start():
    """
    Start the GitLab webhook server.

    The server port can be configured via the PORT environment variable.
    Defaults to 3000 if PORT is not set or invalid.
    """
    raw_port = os.environ.get("PORT")
    try:
        port = int(raw_port) if raw_port else 3000
        if not (1 <= port <= 65535):
            raise ValueError(f"Port {port} is out of valid range")
        if raw_port:
            get_logger().info(f"Using custom PORT from environment: {port}")
    except ValueError as e:
        get_logger().warning(f"Invalid PORT environment variable ({e}), using default port 3000")
        port = 3000
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == '__main__':
    start()
