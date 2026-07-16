<br />

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://codium.ai/images/pr_agent/logo-dark.png" width="330">
  <source media="(prefers-color-scheme: light)" srcset="https://codium.ai/images/pr_agent/logo-light.png" width="330">
  <img src="https://codium.ai/images/pr_agent/logo-light.png" alt="logo" width="330">
</picture>

<br>

# my-pr-agent — GitLab 中文评审 fork

`codiumai/pr-agent` 的维护分支，针对 GitLab webhook + 中文评审场景定制，
并加了面向前端的 telemetry API。

</div>

---

## 这是什么

一个跑在 Docker 里的 AI 代码评审 webhook 服务：

```
GitLab MR event ──▶ pr-agent (Docker, :5050) ──┬──▶ GitLab MR note (中文 /describe /review /improve)
                                                │
                                                └──▶ SQLite telemetry.db
                                                       └──▶ FastAPI :5050 ──▶ 你的前端 dashboard
```

**默认部署覆盖**：

| 能力 | 命令 | 触发方式 | 说明 |
|---|---|---|---|
| MR 描述自动生成 | `/describe` | MR open / reopen 自动跑 | 中文 PR 描述（`publish_labels=false`，不输出 PR 类型/标签） |
| 评审 + 安全检查 | `/review` | MR open 自动跑 | "Estimated effort" / "Security concerns" / "Recommended focus areas" 中文输出 |
| 行内建议 + Apply 按钮 | `/improve` | MR open 自动跑 | GitLab DiffNote + `\`\`\`suggestion` 块，GitLab UI 可一键应用 |
| 忽略某条建议 | `/dismiss [原因]` | 在 suggestion DiffNote thread 回复 | 标记 `state=dismissed`，存原因到 telemetry |
| 应用某条建议 | GitLab UI Apply 按钮 | 用户点 Apply | 触发 commit，telemetry 自动检测 `Apply N suggestion(s)` 消息 |
| 评审数据 | telemetry API | 持续 | 给前端 dashboard 用 |

---

## 快速启动（GitLab webhook）

> 完整部署步骤：`docs/docs/installation/local_deploy.md`。

**1. 准备 `.env`**（`/Users/jarvs/gitlab-stack/.env`）：

```bash
# LLM (OpenAI-compatible, 这里用 MiniMax)
MINIMAX_API_KEY=sk-cp-...

# GitLab
GITLAB_PERSONAL_ACCESS_TOKEN=glpat-...
GITLAB_WEBHOOK_SECRET=...

# telemetry API 鉴权 (前端调 :5050 时带)
REVIEW_TELEMETRY_HTTP_TOKEN=$(openssl rand -hex 16)
```

**2. `docker-compose.yml`**：

```yaml
services:
  pr-agent:
    image: my-pr-agent:zh        # 本地构建
    container_name: pr-agent
    restart: unless-stopped
    volumes:
      - pr-agent-data:/var/lib/pr-agent   # 持久化 telemetry.db + suggestion state
    ports:
      - "5050:3000"
    environment:
      CONFIG__LOG_LEVEL: INFO
      CONFIG__MODEL: openai/MiniMax-M3
      CONFIG__MODEL_URL: https://api.minimaxi.com/v1
      CONFIG__MODEL_API_KEY: ${MINIMAX_API_KEY}
      CONFIG__RESPONSE_LANGUAGE: zh-CN
      CONFIG__ALLOWED_BOT_USERNAMES: review-bot
      REVIEW_TELEMETRY_DB_PATH: /var/lib/pr-agent/telemetry.db
      REVIEW_TELEMETRY_HTTP_TOKEN: ${REVIEW_TELEMETRY_HTTP_TOKEN:-}
volumes:
  pr-agent-data:
```

**3. 构建 + 启动**：

```bash
docker build -t my-pr-agent:zh -f Dockerfile .
docker compose up -d pr-agent
curl http://127.0.0.1:5050/api/v1/telemetry/health
# {"status":"ok","backend":"sqlite"}
```

**4. GitLab 项目 → Settings → Webhooks**：

- URL: `http://host.docker.internal:5050/webhook`
- Secret token: `.env` 里的 `GITLAB_WEBHOOK_SECRET`
- Trigger: ☑️ Merge request events

---

## AGENTS.md 项目自定义规则

把规则定义放仓库根 `AGENTS.md`（或者拆到 `.agents/rules/*.md`），格式：

```markdown
# Project Review Rules (<PREFIX>-*)

- `<PREFIX>-RULE-NO-LOG-EXC`         — replace `except Exception: pass` with `logging.exception(...)` + re-raise
- `<PREFIX>-RULE-DOCSTRING-REQUIRED` — every new `def` must have a single-line docstring
- `<PREFIX>-RULE-NO-BARE-PRINT`      — no `print(...)` in production code
- `<PREFIX>-RULE-TYPEHINTS`          — every new function must have type annotations
- `<PREFIX>-RULE-FORBIDDEN-COMMENT`  — commits must not contain `<PREFIX>-VIOLATION-MARKER` comments
```

`<PREFIX>` 默认是 `ZLG`。**项目方可以在 `pr_agent/settings/configuration.toml` 改**：

```toml
[config]
rule_key_prefix = "SSD"  # 或 MYAPP / YOURTEAM / ...
```

规则键前缀改了之后，正则抽取、telemetry rule_keys 索引、severity 规则文件匹配全部跟着改，不需要动 pr-agent 代码。

---

## telemetry API（前端 dashboard 用）

所有接口都在 `http://pr-agent:5050/api/v1/telemetry/*`，需要 `Authorization: Bearer ${REVIEW_TELEMETRY_HTTP_TOKEN}`。

| Method | Path | 返回 |
|---|---|---|
| GET | `/health` | `{status, backend}` |
| GET | `/metrics/overview` | 汇总：total / applied / dismissed / adoption_rate / severity_breakdown |
| GET | `/metrics/rules` | 按 `rule_key` 聚合 |
| GET | `/metrics/authors` | 按作者聚合 |
| GET | `/metrics/severity` | 按 `severity` 桶聚合 |
| GET | `/mrs?limit=50` | MR 列表 |
| GET | `/mrs/{project_id}/{mr_id}` | 单个 MR 详情 |
| GET | `/mrs/{project_id}/{mr_id}/suggestions` | 该 MR 的所有 suggestion |
| GET | `/mrs/{project_id}/{mr_id}/runs` | /describe /review /improve 运行记录 |
| GET | `/mrs/{project_id}/{mr_id}/timeline` | 事件流 |
| GET | `/mrs/{project_id}/{mr_id}/stats` | 单 MR 统计 |
| GET | `/dismissals?since=...&rule_key=...` | 被 dismiss 的建议 + 原因 |
| GET | `/dismissals/by-rule` | 按 rule_key 聚合 dismiss 原因分布 |

示例：

```bash
TOKEN=db2f763001d72ab103680f0b92aaff4a

curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:5050/api/v1/telemetry/metrics/overview" | python3 -m json.tool

curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:5050/api/v1/telemetry/mrs/34/52/suggestions" | python3 -m json.tool
```

完整字段定义、DB schema、severity 三层 fallback：`pr_agent/telemetry/README.md`。

---

## 数据持久化

`/var/lib/pr-agent` 在容器里挂命名卷，存：

- `telemetry.db` — SQLite，suggestion / run / action_event / dismissal
- `suggestions/<project>-mr-<iid>.json` — suggestion state 文件（block-merge 用）
- `.secrets.toml` cache — 不写到这里（运行时从 env 读）

容器 `docker compose up -d --force-recreate` 不会丢数据；想清干净就 `docker compose down -v`。

---

## 工程结构

```
pr_agent/
├── algo/
│   ├── i18n.py                  # 中文 locale (response_language=zh-CN)
│   ├── repo_context.py          # AGENTS.md 渲染 + 规则键抽取
│   ├── improve_coverage.py      # 规则覆盖率检查
│   └── utils.py                 # get_model 守卫
├── tools/
│   ├── pr_description.py        # /describe
│   ├── pr_reviewer.py           # /review
│   ├── pr_code_suggestions.py   # /improve
│   └── pr_add_docs.py           # /add_docs
├── servers/
│   └── gitlab_webhook.py        # GitLab webhook 入口 + Apply detection + /dismiss
├── git_providers/
│   └── gitlab_provider.py       # GitLab REST + python-gitlab
├── telemetry/                   # telemetry API + SQLite + FastAPI
│   ├── api.py
│   ├── store.py
│   ├── events.py
│   ├── models.py
│   └── README.md                # 详细文档
└── settings/
    └── configuration.toml       # 默认配置 (响应 language, AGENTS.md 规则, severity map)

docs/docs/installation/
├── gitlab.md                    # GitLab 部署 (上游文档)
└── local_deploy.md              # 当前 fork 的部署步骤 (推荐)

tests/
├── unittest/                    # pytest 单测
└── health_test/                 # /describe + /review + /improve 烟雾测试
```

---

## 上游

fork 自 [`codiumai/pr-agent`](https://github.com/the-pr-agent/pr-agent)。
本分支合并的 fork 改动：

- 中文 locale + i18n (`pr_agent/algo/i18n.py`, `response_language=zh-CN`)
- `/dismiss` slash 命令（在 suggestion DiffNote thread 回复 `/dismiss [原因]`，自动 resolve discussion + 存原因到 telemetry）
- telemetry FastAPI + SQLite + 命名卷持久化（`/var/lib/pr-agent`）
- AGENTS.md 规则键前缀可配置 (`config.rule_key_prefix` 默认 `ZLG`，可改 `SSD` / `MYAPP` 等)
- `severity` 三层 fallback（rule / pattern / llm numeric） + severity_bucket API
- Apply-suggestion 自动检测（push hook + merge_request hook + 含 `/` 分支名 MR 查找修复）
- litellm temperature 强转等 LLM 兼容垫片（适配 MiniMax 等 OpenAI-compatible 接口）

完整 commit history：`git log --oneline main ^upstream/main`。

---

## 已知约束

- **只测过 GitLab**。GitHub / Bitbucket / Azure 链路在 fork 里没改也没验证。
- **LLM 走 OpenAI-compatible 协议**。默认配 MiniMax (api.minimaxi.com/v1)，切换到 OpenAI / Anthropic / DeepSeek 只要改 `CONFIG__MODEL_URL` 和 `CONFIG__MODEL_KEY`。
- **arm64 macOS 上跑 amd64 image** 会有 platform mismatch warning，CI 没优化。
- **分含 `/` 的分支名**（如 `codex/fullflow-2026-07-15`）push Apply 走 telemetry 的修复在 `170386b` 之后才生效，更早版本会漏记 apply。
- **代码评审 / 中文 / telemetry 是当前唯一在用的能力**，其他上游工具（CLI / GitHub Action / Azure DevOps app 等）都没在本 fork 验证。

---

## License

继承上游 [Apache 2.0](LICENSE)。
