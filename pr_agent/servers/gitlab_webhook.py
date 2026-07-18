import asyncio
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
_APPLY_SHA_SEEN: "set[str]" = set()


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
    actor = (webhook_data.get("user") or {}).get("username", "") or ""

    if object_kind == "push":
        ref = webhook_data.get("ref") or ""
        commits = webhook_data.get("commits") or []
        apply_commit = None
        for c in reversed(commits):
            if _APPLY_COMMIT_RE.search((c.get("message") or "")):
                apply_commit = c
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
            "msg": (apply_commit.get("message") or "").strip(),
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
        if not (sha and _APPLY_COMMIT_RE.search(msg)):
            return None
        return {
            "sha": sha,
            "msg": msg,
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


def _handle_apply_commit(webhook_data: dict) -> None:
    """Schedule the async handler for an Apply-suggestion commit.

    The actual work touches the GitLab REST API and may take a few seconds
    (LLM re-review, if enabled). FastAPI request handlers must remain
    non-blocking, so we delegate to the async version via the running
    event loop.
    """
    # De-dup happens inside ``_handle_apply_commit_async`` once we have
    # a real SHA from the resolved event (skip here so unit tests that
    # patch _resolve_apply_event on the async path can still drive the
    # synchronous entry point cleanly).
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_handle_apply_commit_async(webhook_data))
            return
    except RuntimeError:
        # No running loop in this thread — fall back to the sync path so
        # unit tests (which drive the function directly) still work.
        pass
    _handle_apply_commit_sync(webhook_data)


def _handle_apply_commit_sync(webhook_data: dict) -> None:
    """Synchronous runner for unit tests (or any non-async context).

    Production traffic always goes through ``_handle_apply_commit`` →
    ``_handle_apply_commit_async`` (queued on the FastAPI event loop).
    Here we materialize a private event loop so we can drive the same
    coroutine end-to-end without needing pytest-asyncio glue.
    """
    try:
        asyncio.run(_handle_apply_commit_async(webhook_data))
    except RuntimeError as _e:
        # asyncio.run cannot be called from inside a running loop either —
        # catch the "asyncio.run() cannot be called from a running event
        # loop" error and just fall through (the calling webhook handler
        # already scheduled us as a task, so this should never trigger).
        get_logger().debug(f"_handle_apply_commit_sync.run fall-through: {_e}")


async def _handle_apply_commit_async(webhook_data: dict) -> None:
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
        # De-dupe by SHA only: GitLab emits both a push hook and a
        # merge_request update hook for the same Apply click, so the
        # same SHA can land here twice (and again after /describe
        # rewrites the description, triggering yet another MR update).
        # GitLab SHAs are globally unique within a project, so SHA is
        # sufficient to de-dupe across object_kind boundaries.
        sha = ev.get("sha")
        if sha:
            global _APPLY_SHA_SEEN
            if sha in _APPLY_SHA_SEEN:
                get_logger().debug(f"apply-handler skip duplicate sha={sha[:10]}")
                return
            _APPLY_SHA_SEEN.add(sha)
        project_id = ev["project_id"]
        sha = ev["sha"]
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
        if not files:
            # merge_request hook has no files_hint. Pull the commit directly via
            # GitLab REST — python-gitlab's mr.commits.get(sha) returns a
            # generator method, not a commit object, so we bypass the SDK here.
            try:
                import requests
                gl_token = get_settings().get("GITLAB.PERSONAL_ACCESS_TOKEN")
                base = get_settings().get("GITLAB.URL", "http://127.0.0.1:8929").rstrip("/")
                url = f"{base}/api/v4/projects/{project_id}/repository/commits/{sha}/diff"
                resp = requests.get(
                    url,
                    headers={"PRIVATE-TOKEN": gl_token},
                    params={"per_page": 50},
                    timeout=10,
                )
                if resp.ok:
                    for d in resp.json():
                        for key in ("new_path", "old_path"):
                            f = d.get(key)
                            if f and f not in files:
                                files.append(f)
            except Exception as e:
                get_logger().warning(f"telemetry.on_apply_commit commit lookup: {e}")
        if not files:
            try:
                recent = telemetry_events.get_default_store().list_suggestions(
                    mr_id=int(mr_id), project_id=int(project_id)
                )
                seen = set()
                for s in sorted(recent, key=lambda x: x.get("posted_at") or "", reverse=True):
                    if s.get("state") != "open":
                        continue
                    if s.get("file") in seen:
                        continue
                    files.append(s["file"])
                    seen.add(s["file"])
                    if len(files) >= 1:
                        break
            except Exception as e:
                get_logger().warning(f"telemetry.on_apply_commit fallback: {e}")
        actor = ev.get("actor") or ""
        _applied_total = 0
        _applied_files: set[str] = set()
        for file in files:
            try:
                updated = telemetry_events.mark_suggestions_applied(
                    mr_id=int(mr_id),
                    project_id=int(project_id),
                    file=file,
                    actor=actor,
                    apply_event_sha=sha,
                )
                if updated:
                    get_logger().info(
                        f"telemetry.on_apply_commit: marked {len(updated)} suggestion(s) applied on {file}: {updated[:3]}"
                    )
                    _applied_total += len(updated)
                    _applied_files.add(file)
            except Exception as e:
                get_logger().warning(f"telemetry.on_apply_commit per-file: {e}")

        # Post-apply user feedback: previously the bot stayed silent after
        # marking telemetry rows, so users couldn't tell whether their click
        # was recognized, whether the suggestion moved to ``applied``, or
        # whether /improve would re-fire on its own. We now publish a short
        # status note that closes the loop and explicitly states we did NOT
        # re-run /improve. Operators that want a re-review can either wire
        # ``gitlab.push_commands`` (auto re-run) or type ``/improve`` in the
        # MR thread.
        if _applied_total:
            try:
                _files_sorted = ", ".join(sorted(_applied_files)) or "(unknown)"
                # Distinguish three end-states so the user does not have to
                # guess whether more suggestions are still pending:
                #   * still open  -> tell them how many are pending
                #   * all closed  -> state explicitly that no new suggestions remain
                _remaining_open = 0
                try:
                    _store = telemetry_events.get_default_store()
                    _all_for_mr = _store.list_suggestions(
                        mr_id=int(mr_id), project_id=int(project_id)
                    )
                    _remaining_open = sum(
                        1 for s in _all_for_mr if (s.get("state") or "") == "open"
                    )
                except Exception as _e:
                    get_logger().debug(f"apply-commit remaining-open lookup failed: {_e}")
                if _remaining_open:
                    _extra = (
                        f"\n\n📋 仍剩 **{_remaining_open}** 条建议处于 open 状态, "
                        "等待你 Apply 或 `/dismiss` 关闭."
                    )
                else:
                    _extra = (
                        "\n\n✅ 现存所有 review-bot 建议已全部 closed, **当前无可补充建议**. "
                        "如需重新检视最新 diff, 请手动输入 `/improve`."
                    )
                _status = (
                    "✅ 已自动记录 {n} 条建议为 applied (commit {sha}).\n\n"
                    "本次 commit 是 GitLab 的「Apply suggestion」操作, "
                    "机器人会按 `gitlab.apply_commands` 配置自动重跑 `/describe → /review → /improve`, 保持 MR 描述 / 评审指南 / 建议列表与最新 diff 同步."
                ).format(n=_applied_total, sha=sha[:10])
                _status += f"\n\n涉及文件: {_files_sorted}"
                _status += _extra
                try:
                    provider.publish_comment(_status)
                    get_logger().info(
                        f"apply-commit status comment posted: {sha[:10]} count={_applied_total}"
                        f" remaining_open={_remaining_open}"
                    )
                except Exception as e:
                    get_logger().warning(f"apply-commit status publish failed: {e}")
            except Exception as e:
                get_logger().warning(f"apply-commit status build failed: {e}")

        # ``gitlab.apply_commands`` lets operators opt into a
        # ``/describe + /review + /improve`` re-run after each Apply so
        # the bot keeps the MR Description, "PR 评审指南", and the
        # pending-suggestion list in sync with the latest diff. The list
        # is intentionally a settings-level list (just like
        # ``gitlab.pr_commands``) — empty to disable.
        #
        try:
            apply_commands = get_settings().get("gitlab.apply_commands", []) or []
            # ``pr_url`` and ``mr_id`` are already populated above (either
            # from the resolved event or from ``_lookup_mr_for_push``).
            if apply_commands and pr_url and mr_id:
                try:
                    get_settings().set("config.is_auto_command", True)
                except Exception:
                    pass
                get_logger().info(
                    f"apply-commit auto re-run started for {pr_url}: {apply_commands}"
                )
                await _apply_commands_async(pr_url, list(apply_commands))
        except Exception as _e_apply:
            get_logger().warning(f"apply-commit auto re-run dispatch failed: {_e_apply}")
    except Exception as e:
        get_logger().warning(f"telemetry.on_apply_commit outer: {e}")

async def _apply_commands_async(pr_url: str, commands: list) -> None:
    """Re-run a list of slash commands on a thread-private event loop.

    Mirrors ``_perform_commands_gitlab`` (``pr_commands`` / ``push_commands``)
    so the same control-flow applies: ``is_auto_command`` is set, every
    command is forwarded to ``PRAgent.handle_request``, and per-command
    failures only log a warning without killing the loop.
    """
    apply_repo_settings(pr_url)
    from pr_agent.agent.pr_agent import PRAgent as _PRAgent
    _agent = _PRAgent()
    for new_command in commands:
        try:
            cmd, _, args = new_command.partition(" ")
            from pr_agent.algo.utils import update_settings_from_args as _upd
            _args_norm = _upd(args.split()) if args.strip() else []
            command_str = " ".join([cmd] + list(_args_norm)) if _args_norm else cmd
            get_logger().info(
                f"apply-commit auto re-run: {pr_url} → {command_str}"
            )
            await _agent.handle_request(pr_url, command_str)
        except Exception as _e:
            get_logger().warning(
                f"apply-commit auto re-run cmd={new_command!r} failed: {_e}"
            )


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
        if sender_username.endswith("-bot") or sender_username.endswith("_bot"):
            get_logger().info(f"Skipping GitLab bot username: {sender_username}")
            return True
    except Exception as e:
        get_logger().error(f"Failed 'is_bot_user' logic: {e}")
    return False


def _is_gitlab_command_comment(body: str) -> bool:
    """Return whether an MR note should enter the slash-command router."""
    return isinstance(body, str) and body.lstrip().startswith("/")

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
        if not data.get('object_attributes', {}):
            return False
        title = data['object_attributes'].get('title')
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
            source_branch = data['object_attributes'].get('source_branch')
            if any(re.search(regex, source_branch) for regex in ignore_mr_source_branches):
                get_logger().info(
                    f"Ignoring MR with source branch '{source_branch}' due to gitlab.ignore_mr_source_branches settings")
                return False

        if ignore_mr_target_branches:
            target_branch = data['object_attributes'].get('target_branch')
            if any(re.search(regex, target_branch) for regex in ignore_mr_target_branches):
                get_logger().info(
                    f"Ignoring MR with target branch '{target_branch}' due to gitlab.ignore_mr_target_branches settings")
                return False

        if ignore_mr_labels:
            labels = [label['title'] for label in data['object_attributes'].get('labels', [])]
            if any(label in ignore_mr_labels for label in labels):
                labels_str = ", ".join(labels)
                get_logger().info(f"Ignoring MR with labels '{labels_str}' due to gitlab.ignore_mr_labels settings")
                return False

        if ignore_mr_title:
            if any(re.search(regex, title) for regex in ignore_mr_title):
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
            if _resolve_apply_event(data) is not None:
                _handle_apply_commit(data)
                return JSONResponse(status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "apply-handled"}))
        except Exception as e:
            get_logger().warning(f"_handle_apply_commit early-exit outer: {e}")
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
                _emit_mr_activity(data, state='updated')
                # Apply-suggestion detection moved to the early-exit hook above
                # so it also fires for object_kind=push webhooks.
                if is_draft(data):
                    get_logger().info(f"Skipping draft MR: {url}")
                    return JSONResponse(status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "success"}))

                # Real push (oldrev set) — apply repo settings before checking
                # push commands or handle_push_trigger.
                if object_attributes.get('oldrev'):
                    apply_repo_settings(url)
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
                #
                # IMPORTANT: the keyword MUST be at the start of the body (after
                # stripping trivial wrappers). Earlier this only checked whether the
                # substring appeared anywhere in the body, which caused review-bot's
                # own suggestion bodies to be matched — they contain the literal
                # ``/dismiss 忽略原因`` instruction inside the help footer. GitLab
                # pushes a webhook event for the bot's own note, so the bot ended
                # up resolving its own suggestion right after publishing it. The
                # first-line anchor below stops that self-tripping loop while still
                # accepting every command form listed above.
                _body_stripped = body.strip()
                _wrapper_strip_re = re.compile(
                    r"^[\\\s/\?" + "‘’“”" + r"\'\",;:。,;:!\-—_()]+"
                )
                _first_line = _wrapper_strip_re.sub('', _body_stripped)
                _dismiss_match = re.match(
                    r'(?<![A-Za-z0-9])dismiss(?![A-Za-z0-9])',
                    _first_line,
                    flags=re.IGNORECASE,
                )
                if note_type == 'DiffNote' and _dismiss_match:
                    # Split on the matched keyword and strip wrappers on both halves so
                    # `/dismiss 忽略原因测试` → `忽略原因测试` and `dismiss忽略` → `忽略`.
                    _dismiss_word = _dismiss_match.group(0)
                    _before, _sep, _after = body.partition(_dismiss_word)
                    _reason = (_before + _after).strip()
                    _wrapper_strip = re.compile(
                        r'^[\s/\\?"\'\u2018\u2019\u201c\u201d,;:。,;:!\-—_()]+'
                        r'|'
                        r'[\s/\\?"\'\u2018\u2019\u201c\u201d,;:。,;:!\-—_()]+$'
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

                if not _is_gitlab_command_comment(body):
                    get_logger().debug("Ignoring non-command GitLab MR comment")
                    return JSONResponse(status_code=status.HTTP_200_OK,
                                        content=jsonable_encoder({"message": "ignored non-command"}))

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
