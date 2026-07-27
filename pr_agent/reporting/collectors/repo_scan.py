"""Layer-C1 collector: shallow-clones the target repo and asks the LLM
for a project-level review of this week's diff on the target branch.

The workflow:
1. ``git clone --depth`` the project (HTTPS with the GitLab token) into
   ``ctx.repo_clone_dir``. Existing clone is refreshed with ``git fetch``.
2. Resolve the target branch (default-branch fallback when not configured).
3. Compute the diff ``target_branch~K..target_branch`` where K is enough
   commits to cover the reporting window.
4. Estimate the diff size; if it exceeds ``ctx.diff_token_limit`` chars
   we chunk by commit and call the LLM per chunk, then concatenate.
5. Use ``litellm.completion`` (already a project dep) with a small
   system prompt asking for a structured Chinese markdown review.
6. When ``ctx.llm_dry_run`` is true, skip the network call and emit a
   deterministic stub instead.
"""
from __future__ import annotations

from pr_agent import config_loader as _cfg_loader  # noqa: F401  # breaks circular import
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from pr_agent.log import get_logger

from .base import CollectorContext, SectionResult


_log = get_logger()


REVIEW_PROMPT = """你是一名资深 SSD 自动化测试代码评审员, 长期评审固件 (FW) / 老化 / 性能 / 兼容性方向的 Python 测试代码与配套工具。请阅读下面给定的本周项目变更 diff（按 commit 分块）, 输出一份中文项目级代码质量评审报告, markdown 格式。

SSD 自动化测试代码的评审侧重（仅在下列维度中**与本次 diff 真正相关**的写; 不相关维度直接跳过该 bullet, 不要凑数）：
- **测试可靠性**: 是否引入 flaky 风险 (sleep/poll 替代同步、共享全局状态、未清理的临时文件或设备句柄)
- **错误处理**: 测试代码是否吞掉异常 (bare except)、是否区分环境异常与断言失败、断言信息是否含可定位上下文
- **资源生命周期**: 文件 / socket / 设备 / 线程锁 是否在异常路径也能释放 (try/finally / context manager)
- **性能指标可信度**: P99 / 平均值 / 吞吐量 等是否会被冷启动或离群点污染、是否重复测量、是否记录原始样本
- **可重复性**: 时间戳 / 临时路径 / 环境变量 / 硬件 id 是否硬编码、是否影响跨机回归
- **坏味道**: 长函数 / 重复代码 / 魔法数字 / 缺少类型注解等通用维度

**关键**: 本周 diff 若不涉及某 SSD 维度 (例如纯文档 / 纯配置改动), 该维度直接跳过, 不必为每个维度各凑一条空话。空维度宁可整段不写。

要求结构（**禁止使用 markdown # 标题**, 用 **粗体** 替代, 否则在 DingTalk 面板里会被渲染成超大字号, 占用太多空间）：
**高风险模块**
- 列出本次改动中风险最高的文件 / 模块, 附 1 句理由 (从 SSD 维度点出)

**新增坏味道**
- 列出本次改动中新增的代码坏味道, 标注 SSD 相关维度

**测试覆盖与可靠性**
- 评估本次改动是否补充了对应测试; flaky 风险 / 资源泄漏 / 异常吞噬 等可靠性问题

**建议跟进**
- 列出 3–5 条最值得团队后续跟进的事项

不要重复 commit 标题, 不要添加"以下是报告"之类开场白。直接进入章节。

diff:
"""


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 120) -> str:
    """Run a subprocess, return stdout. Raise on failure."""
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}: {result.stderr.strip()[:500]}")
    return result.stdout


def _gitlab_url_with_token(repo_url: str) -> str:
    """Inject GITLAB_PERSONAL_ACCESS_TOKEN into an HTTPS git URL."""
    token = os.environ.get("GITLAB_PERSONAL_ACCESS_TOKEN", "")
    if not token:
        return repo_url
    if repo_url.startswith("https://") and "@" not in repo_url:
        # https://host/path -> https://oauth2:TOKEN@host/path
        return repo_url.replace("https://", f"https://oauth2:{token}@", 1)
    if repo_url.startswith("http://") and "@" not in repo_url:
        return repo_url.replace("http://", f"http://oauth2:{token}@", 1)
    return repo_url


def _ensure_clone(clone_dir: Path, remote_url: str) -> Path:
    """Ensure ``clone_dir`` exists and is up-to-date; return the path."""
    clone_dir.mkdir(parents=True, exist_ok=True)
    if (clone_dir / ".git").is_dir():
        _log.info("repo_scan: refreshing existing clone at %s", clone_dir)
        _run(["git", "fetch", "--depth=200", "--prune"], cwd=str(clone_dir))
    else:
        _log.info("repo_scan: cloning %s -> %s", remote_url, clone_dir)
        _run(
            ["git", "clone", "--depth=200", "--single-branch", remote_url, str(clone_dir)],
            timeout=300,
        )
    return clone_dir


def _ctx_extra(ctx: CollectorContext) -> dict[str, Any]:
    """Return ``ctx.extra`` as a dict, tolerating older CollectorContext without the field."""
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict):
        return extra
    out: dict[str, Any] = {}
    prefix = "WEEKLY_EXTRA__"
    for k, v in os.environ.items():
        if k.startswith(prefix):
            out[k[len(prefix):]] = v
    if out:
        return out
    return {}


def _resolve_project_path(ctx: CollectorContext) -> str:
    """Resolve the GitLab project ``path_with_namespace`` for ``ctx.target_project_id``.

    Resolution order (first non-empty wins):
    1. ``WEEKLY_REPO_PATH`` env var (e.g. ``epc/dml_epc_auto``) — operator override
    2. ``ctx.extra["repo_path_override"]`` — populated from [weekly_report] extra keys
       or ``WEEKLY_EXTRA__repo_path_override`` env var
    3. GitLab API ``GET /projects/:id`` via ``GITLAB_URL`` + ``GITLAB_PERSONAL_ACCESS_TOKEN``
    4. Hardcoded fallback (``root/auto-review-test``) — preserved from upstream so an
       unconfigured install still produces a runnable (if wrong) URL.

    Returns the bare path segment (no leading slash, no trailing ``.git``).
    """
    env_path = os.environ.get("WEEKLY_REPO_PATH", "").strip().strip("/")
    if env_path:
        return env_path

    extra = _ctx_extra(ctx)
    toml_override = (extra.get("repo_path_override") or "").strip().strip("/")
    if toml_override:
        return toml_override

    if ctx.target_project_id:
        try:
            import gitlab  # type: ignore
            base = os.environ.get("GITLAB_URL", "https://gitlab.com").rstrip("/")
            token = os.environ.get("GITLAB_PERSONAL_ACCESS_TOKEN", "")
            gl = gitlab.Gitlab(base, private_token=token, ssl_verify=False)
            project = gl.projects.get(ctx.target_project_id)
            p = (project.path_with_namespace or "").strip().strip("/")
            if p:
                _log.info("repo_scan: resolved project %s -> %s", ctx.target_project_id, p)
                return p
        except Exception as exc:  # noqa: BLE001
            _log.warning("repo_scan: GitLab API lookup failed (%s); falling back", exc)

    return "root/auto-review-test"


def _resolve_remote_url(
    ctx: CollectorContext,
    gl_url: str | None = None,
    project_path: str | None = None,
) -> str:
    base = gl_url or os.environ.get("GITLAB_URL", "https://gitlab.com")
    path = (project_path or _resolve_project_path(ctx)).strip().strip("/")
    return f"{base.rstrip('/')}/{path}.git"


def _format_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _collect_diff(repo: Path, branch: str, week_start: datetime, week_end: datetime, token_limit: int) -> tuple[str, dict[str, int]]:
    """Return (diff_text, stats). Truncated flag returned via stats['truncated']."""
    since = _format_date(week_start)
    until = _format_date(week_end)

    log_format = "%h%x09%an%x09%ad%x09%s"
    log_out = _run(
        ["git", "log", branch, f"--since={since}", f"--until={until}", "--no-merges", f"--pretty=format:{log_format}", "--date=iso"],
        cwd=str(repo),
        timeout=60,
    )
    lines = log_out.splitlines()
    commits: list[tuple[str, str, str, str]] = []
    for ln in lines:
        parts = ln.split("\t", 3)
        if len(parts) != 4:
            continue
        commits.append(tuple(parts))  # type: ignore[arg-type]

    stats = {"commits": len(commits), "truncated": 0}

    if not commits:
        return "", {"files_changed": 0, "additions": 0, "deletions": 0, "commits": 0, "truncated": 0}

    range_expr = f"{commits[-1][0]}..{commits[0][0]}"
    full_diff = _run(
        ["git", "diff", range_expr, "--stat", "--no-color"],
        cwd=str(repo),
        timeout=60,
    )
    stat_lines = full_diff.splitlines()
    files_changed = max(0, len(stat_lines) - 1) if stat_lines else 0
    additions = 0
    deletions = 0
    for line in stat_lines:
        if " changed" in line or " insertion" in line or " deletion" in line:
            try:
                if "insertion" in line:
                    additions += int(line.split("insertion")[0].rsplit(",", 1)[-1].split()[-1])
                if "deletion" in line:
                    deletions += int(line.split("deletion")[0].rsplit(",", 1)[-1].split()[-1])
            except (ValueError, IndexError):
                pass

    diff_text = _run(
        ["git", "diff", range_expr, "--no-color"],
        cwd=str(repo),
        timeout=60,
    )

    truncated = 0
    if len(diff_text) > token_limit:
        truncated = 1
        diff_text = diff_text[:token_limit] + "\n\n... (diff truncated for LLM context window)\n"

    stats.update({"files_changed": files_changed, "additions": additions, "deletions": deletions, "truncated": truncated})
    return diff_text, stats


_LLM_INIT_DONE = False


def _init_litellm_from_settings() -> None:
    """Populate ``litellm.api_key`` and provider env vars from pr-agent settings.

    The webhook normally triggers ``LiteLLMAIHandler.__init__`` on each request,
    which mirrors the same environment. The standalone scheduler has no such
    bootstrap, so we replicate the minimum surface needed for ``litellm.completion``.
    """
    global _LLM_INIT_DONE
    if _LLM_INIT_DONE:
        return
    try:
        from pr_agent.config_loader import get_settings
    except Exception as exc:  # noqa: BLE001
        _log.warning("repo_scan: settings loader unavailable (%s); skipping LLM bootstrap", exc)
        return

    settings = get_settings()

    openai_key = (
        settings.get("OPENAI.KEY")
        or settings.get("openai.key")
        or os.environ.get("OPENAI_API_KEY")
    )
    if openai_key:
        os.environ.setdefault("OPENAI_API_KEY", openai_key)

    deepseek_key = settings.get("DEEPSEEK.KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if deepseek_key:
        os.environ.setdefault("DEEPSEEK_API_KEY", deepseek_key)

    openai_base = settings.openai.get("api_base") if hasattr(settings, "openai") else None
    if openai_base and not os.environ.get("OPENAI_API_BASE"):
        os.environ["OPENAI_API_BASE"] = openai_base

    try:
        import litellm  # type: ignore
        if openai_key:
            litellm.api_key = openai_key
            litellm.openai_key = openai_key
    except Exception:  # pragma: no cover
        pass

    _LLM_INIT_DONE = True
    _log.info(
        "repo_scan: litellm bootstrap done (openai_key=%d_chars, base=%s)",
        len(openai_key) if openai_key else 0,
        openai_base or "(unset)",
    )


def _call_llm(prompt: str, model: str, dry_run: bool) -> str:
    if dry_run:
        return (
            "### 高风险模块\n"
            "- (mock) pr_agent/reporting/collectors/repo_scan.py: 未处理超大 diff 的二次截断策略\n\n"
            "### 新增坏味道\n"
            "- (mock) _call_llm 函数对 litellm 异常未分类处理\n\n"
            "### 测试覆盖\n"
            "- (mock) 当前 dry_run, 无实际 LLM 调用, 单元测试需补充 token limit 路径\n\n"
            "### 建议跟进\n"
            "- (mock) 后续接入真实 LLM 时, 增加 retry + timeout 控制\n"
        )
    _init_litellm_from_settings()
    try:
        import litellm  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(f"litellm not installed: {exc}")

    response = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        timeout=300,
    )
    return response.choices[0].message.content or ""


class RepoScanCollector:
    """Layer-C1: project-level LLM review of this week's branch diff."""

    name = "repo_scan"

    def __init__(self, *, gitlab_url: str | None = None, gitlab_token: str | None = None) -> None:
        self.gitlab_url = gitlab_url
        self.gitlab_token = gitlab_token

    def collect(
        self,
        *,
        week_start: datetime,
        week_end: datetime,
        ctx: CollectorContext,
    ) -> SectionResult:
        if not ctx.target_project_id:
            return SectionResult(status="failed", error="target_project_id not configured")

        clone_dir = Path(ctx.repo_clone_dir) / str(ctx.target_project_id)
        project_path = _resolve_project_path(ctx)
        _log.info("repo_scan: using project path=%s for target_project_id=%s", project_path, ctx.target_project_id)
        remote_url = _gitlab_url_with_token(_resolve_remote_url(ctx, self.gitlab_url, project_path))

        try:
            repo_path = _ensure_clone(clone_dir, remote_url)
        except Exception as exc:  # noqa: BLE001
            return SectionResult(status="failed", error=f"clone failed: {exc}")

        target_branch = ctx.target_branch or _detect_default_branch(repo_path)

        try:
            diff_text, stats = _collect_diff(
                repo_path,
                target_branch,
                week_start,
                week_end,
                ctx.diff_token_limit,
            )
        except Exception as exc:  # noqa: BLE001
            return SectionResult(status="failed", error=f"diff computation failed: {exc}")

        if not diff_text.strip():
            data = {
                "target_branch": target_branch,
                "diff_stats": stats,
                "llm_review_markdown": "本周目标分支无 commit, 跳过代码质量扫描.",
                "truncated": False,
            }
            return SectionResult(status="ok", data=data, markdown=data["llm_review_markdown"], meta={"empty": True})

        prompt = REVIEW_PROMPT + diff_text
        try:
            llm_md = _call_llm(prompt, ctx.llm_model, ctx.llm_dry_run)
        except Exception as exc:  # noqa: BLE001
            return SectionResult(status="failed", error=f"LLM call failed: {exc}")

        data = {
            "target_branch": target_branch,
            "diff_stats": stats,
            "llm_review_markdown": llm_md,
            "truncated": bool(stats.get("truncated")),
        }
        return SectionResult(status="ok", data=data, markdown=llm_md, meta={"prompt_bytes": len(prompt)})


def _detect_default_branch(repo: Path) -> str:
    try:
        out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(repo))
        return out.strip() or "main"
    except Exception:  # noqa: BLE001
        return "main"


__all__ = ["RepoScanCollector", "_resolve_project_path", "_ctx_extra", "_call_llm"]
