# Review telemetry

pr-agent 评审数据收集 + FastAPI 接口模块。供前端 / 外部 BI 直接拉数据。

## 架构

```
┌──────────────────────────────────────────────┐
│ GitLab webhook → pr-agent 容器                │
│                                               │
│   ┌──────────────────────────────────┐       │
│   │ FastAPI app (单实例)              │       │
│   │  • POST /webhook   ← GitLab 推   │       │
│   │  • GET  /api/v1/telemetry/* ← 外部│      │
│   └──────────────┬───────────────────┘       │
│                  │                            │
│   ┌──────────────▼───────────────────┐       │
│   │ pr_agent/telemetry/{models,events,│      │
│   │  store,api}                        │      │
│   └──────────────┬───────────────────┘       │
│                  │ 写                        │
│   ┌──────────────▼───────────────────┐       │
│   │ SQLite 文件                        │      │
│   │ 路径: REVIEW_TELEMETRY_DB_PATH    │      │
│   │ 默认 /tmp/pr-agent-telemetry.db   │      │
│   └──────────────────────────────────┘       │
└──────────────────────────────────────────────┘

外部 FastAPI 调:  GET http://pr-agent:5050/api/v1/telemetry/...
                 └── (可选) Authorization: Bearer <REVIEW_TELEMETRY_HTTP_TOKEN>
```

**写入方**: pr-agent webhook 处理器 / `/improve` 工具 (单进程)
**读取方**: 任意外部 FastAPI / 前端 (多客户端并发安全, WAL 模式读不阻塞写)
**不在容器内**: 单独的 "telemetry 镜像"。SQLite 是单文件, 走 volume 挂载做持久化。

## 数据模型

四类事件, 各存一张表:

### `mr_activity` — MR 生命周期

Webhook 处理 MR 事件时 upsert (以 `project_id + mr_id` 为 PK):

| 字段            | 类型     | 说明                                          |
|----------------|---------|----------------------------------------------|
| `mr_id`         | int     | GitLab MR iid (project 内的局部 id)           |
| `project_id`    | int     | GitLab project id                             |
| `source_branch` | text    | 源分支                                       |
| `target_branch` | text    | 目标分支                                     |
| `title`         | text    | MR 标题                                       |
| `author`        | text    | 作者 user id (`user:<id>`) 或 username         |
| `state`         | text    | `opened` / `updated` / `merged`              |
| `opened_at`     | text    | 首次见到的时间 (ISO 8601)                     |
| `last_seen_at`  | text    | 最近一次活动 (ISO 8601)                      |
| `merged_at`     | text?   | merge 事件的时间                              |
| `url`           | text?   | MR web URL                                    |
| `head_sha`      | text?   | 当前 head commit                              |

### `review_run` — 每次 /describe / /review / /improve 调用

| 字段                 | 类型   | 说明                                            |
|---------------------|-------|-------------------------------------------------|
| `run_id`            | text PK | `run-<uuid12>`                                  |
| `mr_id`             | int   | 该 MR 的 iid                                     |
| `project_id`        | int   | GitLab project id                                |
| `command`           | text  | `describe` / `review` / `improve`                |
| `status`            | text  | `started` / `success` / `empty` / `failed`      |
| `model`             | text? | 调用的 LLM 模型名                                |
| `started_at`        | text  | ISO 8601                                         |
| `finished_at`       | text? | ISO 8601                                         |
| `error`             | text? | 失败时的错误信息                                 |
| `duration_ms`       | int?  | 从 started 到 finished 的毫秒数                  |
| `suggestion_count`  | int   | 产出的建议数 (success 时)                        |
| `rule_keys_cited`   | text  | JSON 数组, 本次运行里 LLM cite 的规则键          |
| `triggered_by`      | text  | `user` / `auto` (webhook 自动触发)              |

### `suggestions` — 每条 publish_code_suggestions 输出一条

| 字段                  | 类型    | 说明                                          |
|----------------------|--------|-----------------------------------------------|
| `suggestion_id`      | text PK | `sug-<uuid12>`                                 |
| `mr_id`              | int    |                                                |
| `project_id`         | int    |                                                |
| `file`               | text   | 相对仓库路径                                   |
| `line`               | int?   | 触发 DiffNote 的行号                            |
| `label`              | text   | `possible issue` / `security` / `general` 等   |
| `importance`         | int    | LLM 给的分数 (1-10)                            |
| `one_sentence_summary` | text | 一句话摘要                                     |
| `rule_keys`          | text   | JSON 数组, 从 suggestion_content 抽出的规则键  |
| `score`              | int?   | 自反思后的最终分数                             |
| `posted_at`          | text   | ISO 8601                                        |
| `state`              | text   | `open` / `applied` / `dismissed` / `superseded` |
| `applied_at`         | text?  | (reserved, 见 "已知缺口")                       |
| `dismissed_at`       | text?  | (reserved)                                      |
| `dismissed_by`       | text?  | (reserved)                                      |
| `note_id`            | text?  | GitLab discussion id (40-char SHA1 哈希, publish 时捕获, /dismiss 反查) |

### `action_events` — 用户对建议的操作

`/dismiss` 在讨论回复时由 webhook 钩入 (`pr_agent/servers/gitlab_webhook.py`), 通过 note_id 反查 suggestion 并写一行 action event + 更新 state:

| 字段            | 类型   | 说明                                   |
|----------------|-------|----------------------------------------|
| `id`           | int PK | autoincrement                           |
| `at`           | text  | ISO 8601                                |
| `action`       | text  | `applied` / `dismissed` / `replied`     |
| `suggestion_id` | text  | FK → suggestions.suggestion_id          |
| `mr_id`        | int   |                                        |
| `actor`        | text  | username / user id                      |
| `note`         | text  | 备注                                   |

## Severity 分级

每条 suggestion 在写入时由 pr-agent 打一个 `severity` (`critical` / `high` / `medium` / `low` / `unknown`), 用于 dashboard 上"重要建议是否被不当丢弃"的失守告警.

### 三层 fallback 决策树

```
suggestion 进来
  ↓
Layer 1: rule_keys 命中项目规则文件 (e.g. .agents/rules/*.md)
         → 用文件里显式写的 severity
  ↓ (没命中)
Layer 2: rule_keys 命中 config.critical_rule_patterns (子串匹配)
         → critical
  ↓ (没命中)
Layer 2b: rule_keys 命中 config.high_rule_patterns
         → high
  ↓ (没命中)
Layer 3: 按 LLM importance 数字分桶
         importance >= 8 → critical
         importance >= 6 → high
         importance >= 4 → medium
         importance <  4 → low
         importance 缺失 → unknown
```

**优先级**: AGENTS.md / 规则文件 > config pattern > LLM importance. 项目方对自己代码质量分级最有发言权, LLM 是兜底.

### 为什么需要这个

LLM 的 `importance` 数字不稳定:

- 同一段代码两次跑 importance 可能差 ±3
- 截图实例: LLM 给 `ZLG-RULE-NO-LOG-EXC` (静默吞异常) 打了 7, 但业务上这是 **致命规则** (破坏线上故障定位)
- 项目方在规则文件里写 `ZLG-RULE-NO-LOG-EXC: critical`, dashboard 立刻按 critical 桶算, 不会因为 LLM 给 7 被划到 high 桶

### 规则文件格式

在仓库任意位置 (默认约定 `.agents/rules/`) 放 markdown 文件, pr-agent 通过 `pr_agent.git_providers` 拉取. 支持两种行格式:

```markdown
# .agents/rules/severity.md

ZLG-RULE-NO-LOG-EXC: critical
ZLG-RULE-FORBIDDEN-COMMENT: critical
ZLG-RULE-DOCSTRING-REQUIRED: low
ZLG-RULE-TYPEHINTS: low
ZLG-RULE-NO-BARE-PRINT: low
```

或 markdown bullet 形式:

```markdown
- [critical] ZLG-RULE-NO-LOG-EXC
- [critical] ZLG-RULE-FORBIDDEN-COMMENT
- [low] ZLG-RULE-DOCSTRING-REQUIRED
```

**配置入口** (`pr_agent/settings/configuration.toml`):

```toml
[telemetry.severity]
rule_files = [
    ".agents/rules/severity.md",
    ".agents/rules/security.md",
]
```

如果 `rule_files` 为空或文件读不到, 静默 fallback 到 config pattern + LLM importance, 不会报错.

### severity_source 字段

每条 suggestion 在 API 返回里多一个 `severity_source` 字段, 标识等级由哪一层决定:

| 取值 | 含义 |
|------|------|
| `rule:ZLG-RULE-NO-LOG-EXC` | 命中规则文件 |
| `pattern:NO-LOG-EXC` | 命中 config pattern |
| `llm:7` | LLM importance 数字 |
| `unknown` | 都没命中 |

前端可悬浮展示, reviewer 能透明地看到分级依据.

### 实际效果

```
$ curl /api/v1/telemetry/mrs/34/48/suggestions | head
[
  {"importance": 4, "severity": "critical", "severity_source": "pattern:NO-LOG-EXC",  "rule_keys": ["ZLG-RULE-NO-LOG-EXC", ...]},
  {"importance": 8, "severity": "critical", "severity_source": "pattern:FORBIDDEN",   "rule_keys": ["ZLG-RULE-FORBIDDEN-COMMENT"]},
  {"importance": 7, "severity": "critical", "severity_source": "pattern:NO-LOG-EXC",  "rule_keys": ["ZLG-RULE-NO-LOG-EXC"]},
  ...
]
```

截图里的 `importance: 7` 配合 `ZLG-RULE-NO-LOG-EXC` 自动归到 critical, 不再被 LLM 错划到 high.

## HTTP API

Base path: `/api/v1/telemetry`. 所有 endpoint 返回 JSON. 默认端口是 pr-agent 的 webhook 端口 (docker-compose 里映射到 host 5050, 容器内 3000).

| Method | Path                                                | 说明                                                          |
|--------|-----------------------------------------------------|--------------------------------------------------------------|
| GET    | `/health`                                           | `{status: ok, backend: sqlite\|jsonl\|off}`                  |
| GET    | `/metrics/overview?since=YYYY-MM-DDTHH:MM:SS±HH:MM` | 全局统计 (MR / suggestions / runs 三个块的总数 + 比率); 可选 `since` 时间窗 |
| GET    | `/metrics/rules?since=...`                          | 每条 AGENTS.md 规则的采纳 / 忽略 / 开放数; 可选 `since` 时间窗 |
| GET    | `/metrics/authors?since=...`                        | 按作者聚合 (MR 数 / 合并数 / 建议采纳率 / 命令级 runs)         |
| GET    | `/metrics/severity?since=...&pr_url=...`             | 按严重度分桶 (critical/high/medium/low/unknown), 见下 [Severity 分级](#severity-分级); `pr_url` 用于拉项目规则文件 |
| GET    | `/mrs?limit=50&project_id=34&state=merged&since=...`| MR 列表, 按 `last_seen_at` desc, 可按 `state` 过滤 / `since` 截断 |
| GET    | `/mrs/{project_id}/{mr_id}`                         | 单个 MR 详情                                                  |
| GET    | `/mrs/{project_id}/{mr_id}/suggestions`             | 该 MR 的所有 suggestions                                       |
| GET    | `/mrs/{project_id}/{mr_id}/runs`                    | 该 MR 的所有 runs (limit 默认 20)                              |
| GET    | `/mrs/{project_id}/{mr_id}/timeline`                | MR + suggestions + runs + actions 一次性返回                  |
| GET    | `/mrs/{project_id}/{mr_id}/stats`                   | suggestion_counts (各状态数) + adoption_rate + distinct_rules  |

### 鉴权

环境变量 `REVIEW_TELEMETRY_HTTP_TOKEN` 控制:

- **空 / 未设置**: 所有 endpoint 开放访问 (本地开发 / 内网信任场景)
- **非空**: 强制 `Authorization: Bearer <token>` 头

不带 token / 带错 token → 401 / 403.

### 示例响应

**`GET /api/v1/telemetry/metrics/overview`**

```json
{
  "mrs": {"total": 2, "merged": 1, "open": 1},
  "suggestions": {"total": 2, "applied": 0, "dismissed": 0, "open": 2, "adoption_rate": 0.0, "dismissal_rate": 0.0},
  "runs": {"total": 3, "failed": 0, "success_rate": 1.0}
}
```

**`GET /api/v1/telemetry/metrics/rules`**

```json
[
  {"rule_key": "ZLG-RULE-NO-LOG-EXC", "total": 3, "applied": 2, "dismissed": 0, "open": 1, "superseded": 0, "adoption_rate": 0.667},
  {"rule_key": "ZLG-RULE-TYPEHINTS", "total": 1, "applied": 0, "dismissed": 0, "open": 1, "superseded": 0, "adoption_rate": 0.0}
]
```

**`GET /api/v1/telemetry/metrics/authors`**

```json
[
  {
    "author": "user:35",
    "mr_count": 1,
    "merged_count": 0,
    "suggestion_total": 2,
    "suggestion_applied": 0,
    "suggestion_dismissed": 0,
    "adoption_rate": 0.0,
    "runs_by_command": {"improve": {"total": 2, "failed": 0}}
  }
]
```

**`GET /api/v1/telemetry/mrs/34/40/timeline`**

```json
{
  "mr": {"mr_id": 40, "project_id": 34, "title": "...", "state": "merged", ...},
  "suggestions": [{"suggestion_id": "sug-...", "file": "healthcheck.py", "line": 19, "rule_keys": [], "state": "open", "severity": "high", "severity_source": "llm:7", ...}],
  "runs": [{"run_id": "run-...", "command": "improve", "status": "success", "duration_ms": 48000, "suggestion_count": 1, "rule_keys_cited": []}],
  "actions": []
}
```

**`GET /api/v1/telemetry/metrics/severity`**

```json
[
  {"severity": "critical", "total": 14, "applied": 14, "dismissed": 0, "open": 0, "superseded": 0, "adoption_rate": 1.0, "dismissal_rate": 0.0},
  {"severity": "high",     "total": 3,  "applied": 2,  "dismissed": 1, "open": 0, "superseded": 0, "adoption_rate": 0.667, "dismissal_rate": 0.333}
]
```

`GET /api/v1/telemetry/metrics/overview` 在 v0.3 之后内嵌 `severity_breakdown` 字段, 一次拉取即可在 dashboard 顶部显示 4 桶卡片.

`GET /api/v1/telemetry/mrs/{p}/{m}/suggestions` 每条 suggestion 多两个字段:
- `severity` (`critical` / `high` / `medium` / `low` / `unknown`)
- `severity_source` (哪一层给的等级, 例 `rule:ZLG-RULE-NO-LOG-EXC` / `pattern:NO-LOG-EXC` / `llm:7` / `unknown`)

`GET /api/v1/telemetry/mrs/{p}/{m}/stats` 多一个 `severity_counts` 字段 (各等级 suggestion 数).

## 配置

### 环境变量 (优先级最高)

| 变量                              | 默认值                          | 说明                                   |
|---------------------------------|-------------------------------|----------------------------------------|
| `REVIEW_TELEMETRY_BACKEND`      | `sqlite`                       | `sqlite` / `jsonl` / `off`             |
| `REVIEW_TELEMETRY_DB_PATH`      | `/tmp/pr-agent-telemetry.db`   | SQLite 文件路径                          |
| `REVIEW_TELEMETRY_JSONL_PATH`   | `/tmp/pr-agent-telemetry.jsonl`| JSONL 文件路径                          |
| `REVIEW_TELEMETRY_HTTP_TOKEN`   | (空)                           | API 鉴权 token, 空 = 开放               |

### `[telemetry]` 段 in `pr_agent/settings/configuration.toml`

```toml
[telemetry]
backend = "sqlite"
sqlite_path = "/tmp/pr-agent-telemetry.db"
jsonl_path = "/tmp/pr-agent-telemetry.jsonl"
http_token = ""

[telemetry.severity]
# LLM importance 数字兜底: importance >= 8 → critical, >= 6 → high, >= 4 → medium, else → low
critical_min = 8
high_min = 6
medium_min = 4
# 子串匹配: rule_key 包含任意 pattern → 直接落对应桶 (无视 LLM 数字)
critical_rule_patterns = [
    "NO-LOG-EXC",      # 静默吞异常
    "FORBIDDEN",       # 禁止标记
    "SECURITY",
    "INJECTION",
    "AUTH",
]
high_rule_patterns = [
    "DOCSTRING",
    "TYPEHINT",
    "BARE-PRINT",
]
# 项目级规则文件, 从 MR 分支读, 每行 ``RULE_KEY: severity`` 或 ``- [severity] RULE_KEY``
# rule_files = [".agents/rules/severity.md", ".agents/rules/security.md"]
```

(Dynaconf 解析: env 变量 > TOML > 内置默认.)

## 部署

### 1. 单实例 + 持久化 (推荐起步方案)

`/Users/jarvs/gitlab-stack/docker-compose.yml` 的 pr-agent service 加:

```yaml
services:
  pr-agent:
    image: my-pr-agent:zh
    # ... 其他配置 ...
    volumes:
      - ./pr-agent-data:/var/lib/pr-agent    # 持久化 telemetry
    environment:
      REVIEW_TELEMETRY_DB_PATH: /var/lib/pr-agent/telemetry.db
      # 可选:
      # REVIEW_TELEMETRY_HTTP_TOKEN: "your-long-random-secret"
```

host 上 mkdir 数据目录:

```bash
mkdir -p /Users/jarvs/gitlab-stack/pr-agent-data
cd /Users/jarvs/gitlab-stack && docker compose up -d pr-agent
```

数据落 `/Users/jarvs/gitlab-stack/pr-agent-data/telemetry.db`, 容器重启不丢.

### 2. 多实例 (scale out)

SQLite 单写者模型不支持多 pr-agent 实例同时写. 二选一:

**(a) 升级到 Postgres**: 改 `store.py` 用 `psycopg`/`asyncpg`, 其他模块不动 (API 契约不变).

**(b) 切 JSONL + 外部 sink**:

```yaml
REVIEW_TELEMETRY_BACKEND: "jsonl"
REVIEW_TELEMETRY_JSONL_PATH: /var/log/pr-agent/telemetry.jsonl
```

pr-agent 写 JSONL (多实例 append-only, 无冲突), 你的消费端 (Fluent Bit / Vector / 自写 tail) 把 JSONL 灌进 ClickHouse / Postgres / 任何时序库.

### 3. 完全关掉

```yaml
REVIEW_TELEMETRY_BACKEND: "off"
```

不写 SQLite 也不写 JSONL, 所有 hook 变成 no-op, 不影响评审主流程.

## 接入示例 (外部 FastAPI)

### Python (httpx 异步)

```python
import os
import httpx

BASE = os.environ.get("PR_AGENT_BASE", "http://127.0.0.1:5050/api/v1/telemetry")
TOKEN = os.environ.get("PR_AGENT_TELEMETRY_TOKEN", "")

def _headers():
    return {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}

async def get_overview() -> dict:
    r = await httpx.AsyncClient().get(f"{BASE}/metrics/overview", headers=_headers())
    r.raise_for_status()
    return r.json()

async def list_mrs(project_id: int | None = None, limit: int = 50) -> list[dict]:
    params = {"limit": limit}
    if project_id is not None:
        params["project_id"] = project_id
    r = await httpx.AsyncClient().get(f"{BASE}/mrs", params=params, headers=_headers())
    r.raise_for_status()
    return r.json()

async def mr_timeline(project_id: int, mr_id: int) -> dict:
    r = await httpx.AsyncClient().get(
        f"{BASE}/mrs/{project_id}/{mr_id}/timeline", headers=_headers()
    )
    r.raise_for_status()
    return r.json()

async def per_rule_adoption() -> list[dict]:
    r = await httpx.AsyncClient().get(f"{BASE}/metrics/rules", headers=_headers())
    r.raise_for_status()
    return r.json()
```

### JS / 前端 fetch

```js
const BASE = "http://127.0.0.1:5050/api/v1/telemetry";
const TOKEN = "your-token";  // 可选

async function getOverview() {
  const r = await fetch(`${BASE}/metrics/overview`, {
    headers: TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
```

### 前端展示推荐数据流

1. **首页 / 总览卡片** → `/metrics/overview` (1 个 fetch)
2. **规则柱图** → `/metrics/rules` (1 个 fetch)
3. **MR 列表** → `/mrs?project_id=<X>&limit=50` (1 个 fetch, 可分页加 offset)
4. **MR 详情页** → `/mrs/{p}/{m}/timeline` (1 个 fetch 拿全)
5. **单条建议的下钻** → `/mrs/{p}/{m}/suggestions` (按需)

## 已接入的钩子

- `note_id`: `GitLabProvider.send_inline_comment` 在 publish 路径返回 GitLab discussion id, primary / fallback 两条路都回填 (`pr_agent/git_providers/gitlab_provider.py`). 写入 `suggestions.note_id`.
- `/review`: `PRReviewer.run` 围着 `_run_id = emit_run_started()` 加了同样的 finally / 错误处理 (`pr_agent/tools/pr_reviewer.py`). `/describe` 复用同一条管道, command 字段区分.
- `/dismiss` (inline 建议的 thread reply): webhook 调 `resolve_discussion` 成功后, 反查 `note_id` 关联的 suggestion, 写 `action_events` 一行 (`action=dismissed`), 同步 `mark_suggestion_dismissed` 更新 `state=dismissed` / `dismissed_at` / `dismissed_by` (`pr_agent/servers/gitlab_webhook.py`).
- **Severity 分级**: 每条 suggestion 写入时通过 `pr_agent.algo.repo_context.resolve_severity` 计算 severity (三层 fallback: 规则文件 → config pattern → LLM importance). API 返回时附在 `severity` + `severity_source` 字段. 详见 [Severity 分级](#severity-分级).

## 已知缺口 / 后续可补的钩子

| 数据 | 现状 | 补法 |
|------|------|------|
| `applied_at` / `state=applied` | 字段已建, 仅在 GitLab 端点 `Apply suggestion` 后由 GitLab 推 `discussion.unresolve` 时才会回写 (目前只接 resolve → dismissed) | 接 GitLab `merge_request_event` 推送, 在 webhook 拦 `applied` 事件 |
| 与 GitLab Apply 同源的 event 也填 action_events | `action=applied` 没接 | 同上 |
(已完成: 时间窗过滤, 按作者聚合, note_id, /review telemetry, /dismiss telemetry, **severity 三层 fallback + /metrics/severity 端点**)

数据模型已经预留了这些字段的列, schema 不会变, 后续只是补 hook / endpoint, 不破坏现有数据.

## 本地开发与测试

不动 pr-agent 容器也能跑 (走单独的 Python 进程):

```python
import os
os.environ["REVIEW_TELEMETRY_DB_PATH"] = "/tmp/dev-telemetry.db"

from pr_agent.telemetry import get_default_store
from pr_agent.telemetry import events

events.emit_mr_activity(
    mr_id=1, project_id=34, source_branch="feat", target_branch="main",
    title="demo", state="opened",
)
events.emit_suggestion(
    mr_id=1, project_id=34, file="x.py", line=10,
    label="possible issue", importance=5,
    one_sentence_summary="missing type hint",
    rule_keys=["ZLG-RULE-TYPEHINTS"],
)

store = get_default_store()
print(store.overview())
print(store.per_rule_stats())
print(store.severity_breakdown())

# 测试 resolve_severity 三层 fallback
from pr_agent.algo.repo_context import resolve_severity

# Layer 1: 规则文件命中
sev, src = resolve_severity(["ZLG-RULE-NO-LOG-EXC"], 7, rule_severity_map={"ZLG-RULE-NO-LOG-EXC": "critical"})
print(sev, src)  # critical rule:ZLG-RULE-NO-LOG-EXC

# Layer 2: config pattern
sev, src = resolve_severity(["ZLG-RULE-FOO-BAR"], 7, critical_patterns=["FOO-BAR"])
print(sev, src)  # critical pattern:FOO-BAR

# Layer 3: LLM 数字
sev, src = resolve_severity(["X"], 8)
print(sev, src)  # critical llm:8
```
