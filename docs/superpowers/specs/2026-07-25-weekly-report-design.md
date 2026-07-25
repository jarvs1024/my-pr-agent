# 周报与通知功能 — 设计 spec

- **状态**: Draft
- **作者**: jarvs
- **日期**: 2026-07-25
- **目标版本**: 下一轮 fork release

---

## 1. 背景与目标

现有 `my-pr-agent` 在 GitLab webhook 触发下自动跑 `/describe /review /improve`,
评审数据落到本地 telemetry store, 前端平台 TestMate 通过 `/api/v1/telemetry/*`
读取这些数据展示。

当前缺口:
1. 没有项目级视角 — 团队看不到「这一周整体代码质量如何」
2. 没有 master 变更汇总 — MR 合入 master 的脉络散在各 MR 评论里
3. 没有主动通知 — 数据只有人去 dashboard 拉才会被发现

本 spec 在不干扰现有 pr-agent webhook 处理的前提下, 新增一个独立 service,
每周定时产出项目级周报并推送到钉钉机器人, 结构化产物同时供 TestMate 消费。

参考实现: [AI-Codereview-Gitlab](https://github.com/sunmh207/AI-Codereview-Gitlab)
的 daily report 模块 (scheduler + DingTalk + LLM 总结), 但改为周报 + 项目级
全量扫描, 并扩展数据消费端。

---

## 2. 范围

### 2.1 In scope

- 每周定时生成一份项目级代码检视周报
- 周报三段内容:
  - **检视概况**: 本周 PR-Agent 评审 MR / suggestion / 采纳率 / severity 分布
    (来源: 现有 telemetry store 聚合)
  - **master 变更汇总**: 本周合并到 master 的 MR 列表 + 单条摘要
    (来源: GitLab API)
  - **代码质量扫描**: 本周 master diff 走 LLM 项目级评审, 输出高风险模块 /
    新增坏味道 / 跟进建议 (来源: shallow clone + LLM)
- 推送目标: 钉钉机器人 webhook (Markdown)
- 结构化产物: `$PR_AGENT_DATA_DIR/weekly_reports/<project_id>/<YYYY-WW>.json`
- TestMate 接入: 通过 webhook 进程新增 2 个只读路由读取周报 JSON

### 2.2 Out of scope

- Manual trigger HTTP endpoint (按 A3 = 不要, 纯 cron 触发)
- 多项目 rollup (按 S1 = 单项目, 每个 pr-agent 部署只监控一个 target_project)
- 飞书 / 企微推送 (notifier protocol 留口子, 后续单独加)
- HTML / PDF 归档 (TestMate 通过 JSON 自渲染)
- 周报历史清理 / 归档 (外层 logrotate / volume 备份管)

---

## 3. 关键决策

| #  | 决策       | 选项                          | 理由                          |
|----|------------|-------------------------------|-------------------------------|
| D1 | 数据范围   | A+B+C 三层                   | 用户要求; B 是主焦点 (master 汇总) |
| D2 | Layer C 策略 | C1 (全 diff 走 LLM)          | 用户指定; 评估深度优先        |
| D3 | 项目范围   | S1 (单 target project)       | 用户指定; 跟 "本周 master" 措辞匹配 |
| D4 | 部署形态   | 同 image, 不同 service (同 compose project) | 用户要求部署一起, 代码解耦 |
| D5 | TestMate 接入 | T1 (webhook 进程加只读路由) | 复用现有接入点, 前端改动最小 |
| D6 | cron 时间  | 每周一 09:00 本地时区 (Asia/Shanghai) | 用户选 A1=1                |
| D7 | 报告语言   | 中文 (zh-CN)                 | 用户选 A2=1                   |
| D8 | 失败兜底   | 钉钉重试 3 次后写本地失败日志 | 用户选 A4                     |
| D9 | 配置入口   | `.pr_agent.toml` 新增 `[weekly_report]` block | 跟随项目现有 Dynaconf 约定 |
| D10 | 失败隔离  | scheduler 跑独立进程, 共享 volume | 满足 "代码检视不受影响" 原则  |

---

## 4. 架构

### 4.1 部署形态

```
docker-compose.yml
├── service: pr-agent              # 现状, command 不变 (gitlab_webhook)
└── service: pr-agent-reporter     # 新, command: python -m pr_agent.reporting.scheduler
```

两个 service 共用同一个 Docker image (`Dockerfile.reporter` 用同 base image 加
`ENTRYPOINT ["python", "-m", "pr_agent.reporting.scheduler"]`), 共用
`PR_AGENT_DATA_DIR` volume (用于 telemetry DB + weekly_reports/ 产物 +
repo_scan_cache/)。

### 4.2 进程边界

```
+-----------------------------+     +---------------------------------+
|  pr-agent 容器              |     |  pr-agent-reporter 容器         |
|  -------------------------  |     |  ---------------------------   |
|  FastAPI: uvicorn           |     |  APScheduler (独立线程池)       |
|   +-- /webhook (GitLab)     |     |   +-- cron 触发 weekly_job     |
|   +-- /api/v1/telemetry/*   |     |       +-- collectors/*         |
|   |   +-- (新增)            |     |       |   +-- telemetry_overview |
|   |       /weekly_reports/* |     |       |   +-- master_merges     |
|   +-- 健康检查              |     |       |   +-- repo_scan (clone+LLM)|
|                             |     |       +-- report.build_artifact |
|  import: pr_agent (核心)    |     |       +-- renderer.render_md   |
|                             |     |       +-- dingtalk.send (重试3)|
|  NOT import: pr_agent.      |     |                               |
|             reporting       |     |  import: pr_agent (核心)       |
|                             |     |          + pr_agent.reporting |
+-----------------------------+     +---------------------------------+
                |                                |
                +---------- $PR_AGENT_DATA_DIR ---+
                              (共享 volume)
                              +- telemetry.db
                              +- weekly_reports/<pid>/<YYYY-WW>.json
                              +- repo_scan_cache/<pid>/
                              +- reporting_runs/<ts>.*.json
```

**关键边界**:
- `gitlab_webhook.py` **不** import `pr_agent.reporting`
- `pr_agent/reporting/` **不** import webhook handler / server 模块
- 共享通过文件系统 volume, 不通过内存 / RPC

### 4.3 与现有 pr-agent 核心的修改面

| 文件                                    | 修改                  | 说明                              |
|-----------------------------------------|-----------------------|-----------------------------------|
| `pr_agent/servers/gitlab_webhook.py`    | **不改**              | webhook 进程零侵入               |
| `pr_agent/telemetry/store.py`           | **不改**              | reporter 以 consumer 身份读 SQLite |
| `pr_agent/git_providers/*`              | **不改**              | reporter 复用 `get_git_provider_with_context` |
| `pr_agent/algo/i18n.py`                 | **不改**              | reporter 复用 `t()`               |
| `pr_agent/telemetry/api.py`             | **追加** ~30 行只读路由 | 新 endpoint 不影响现有端点行为    |
| `pr_agent/settings/configuration.toml`  | **追加** `[weekly_report]` 默认块 | 默认 `enabled = false`, 旧用户零行为变化 |

---

## 5. 模块布局

```
pr_agent/reporting/                              # 新增
+-- __init__.py
+-- config.py                                    # 读 [weekly_report] block + env
+-- scheduler.py                                 # APScheduler + weekly_job 主流程
+-- notifiers/
|   +-- __init__.py
|   +-- base.py                                  # Notifier protocol
|   +-- dingtalk.py                              # 加签 / 项目路由 / 3 次重试 / 失败日志
+-- collectors/
|   +-- __init__.py
|   +-- base.py                                  # Collector protocol
|   +-- telemetry_overview.py                    # A 层
|   +-- master_merges.py                         # B 层
|   +-- repo_scan.py                             # C1 层 (clone + diff + LLM)
+-- report.py                                    # 拼结构化 artifact + 渲染 Markdown
+-- renderer.py                                  # Markdown 分块 (按 ## 章节, >18KB 拆)
+-- prompts/
    +-- weekly_repo_scan.toml                    # C1 LLM 项目级评审 prompt
    +-- master_merge_summary.toml                # 批量 LLM 摘要 prompt (一次调用给本周所有 MR)

pr_agent/telemetry/api.py                        # 追加 ~30 行
+-- + GET /api/v1/telemetry/weekly_reports/latest?project_id=...
+-- + GET /api/v1/telemetry/weekly_reports/list?project_id=...&limit=12

pr_agent/settings/configuration.toml             # 追加 ~20 行
+-- + [weekly_report] 块 (默认 enabled = false)

docker/
+-- Dockerfile.reporter                          # 新, FROM 同 base, ENTRYPOINT 走 scheduler

docs/
+-- docs/usage-guide/
|   +-- weekly_report.md                         # 用户文档: 配置 + cron + 故障排查
+-- mkdocs.yml                                   # nav 加一行

tests/unittest/reporting/                        # 新增测试目录
+-- __init__.py
+-- test_telemetry_overview_collector.py
+-- test_master_merges_collector.py              # stub GitLab provider
+-- test_repo_scan_collector.py                  # fixture repo + stub LLM
+-- test_dingtalk_notifier.py                    # stub HTTP server
+-- test_report_markdown.py                      # golden snapshot
+-- test_scheduler_lifecycle.py

pyproject.toml / requirements.txt                # 加: apscheduler, requests
                                                  # (钉钉不用 SDK, 直接 requests.post)
```

---

## 6. 数据流

### 6.1 一次周报运行

```
+----------------------------------------------------------------+
| APScheduler cron 触发 (周一 09:00 Asia/Shanghai)              |
+-----------------------------+----------------------------------+
                              v
         scheduler.run_weekly_job(week_start, week_end)
                              |
       +----------------------+----------------------+
       v                      v                      v
 telemetry_overview      master_merges          repo_scan
 .collect(...)            .collect(...           .collect(...)
                          批量 LLM 摘要)         (clone+diff+LLM)
       |                      |                      |
       |  失败? 标 failed      |  失败? 标 failed      |  失败? 标 failed
       |                      |                      |
       +----------------------+----------------------+
                              v
                 report.build_artifact(sections)
                              |
                +-------------+-------------+
                v                           v
   json.dump 到                  renderer.render_markdown(artifact)
   $PR_AGENT_DATA_DIR/                |
   weekly_reports/<pid>/<W>.json      | 按 ## 拆 chunk
                |                     v
                |            dingtalk.send(title, chunks)
                |                     |
                |                     | 重试 3 次 (指数退避 1s/4s/16s)
                |                     |
                |                     v
                |            成功 / 失败
                |              |          |
                |              v          v
                |          done     写 $PR_AGENT_DATA_DIR/
                |                    reporting_runs/<ts>.failed.json
                |                    (人工补发, 不抛回)
                v
         写 $PR_AGENT_DATA_DIR/
         reporting_runs/<ts>.ok.json
         (含每个 collector 状态 + 推送结果)
```

### 6.2 TestMate 读取

```
GET /api/v1/telemetry/weekly_reports/latest?project_id=123
        |
        v
  读 $PR_AGENT_DATA_DIR/weekly_reports/123/<current_week>.json
        |
        v
  返回完整结构化 JSON

GET /api/v1/telemetry/weekly_reports/list?project_id=123&limit=12
        |
        v
  列出 weekly_reports/123/*.json 文件名, 按文件名逆序
```

### 6.3 Collector 协议

```python
class Collector(Protocol):
    name: str

    def collect(
        self,
        *,
        week_start: datetime,
        week_end: datetime,
        target_project_id: int,
        ctx: CollectorContext,
    ) -> SectionResult: ...
```

`SectionResult` dataclass:
- `status: Literal["ok", "failed"]`
- `data: dict | None`  # 结构化数据, 渲染时 dump
- `markdown: str | None`  # collector 自己产出 (e.g. C1 LLM 输出)
- `error: str | None`

任意 collector 抛异常 → scheduler 捕获 → `status="failed"`, 其他继续。

---

## 7. 配置

### 7.1 `.pr_agent.toml` 增量

```toml
[weekly_report]
enabled = true
target_project_id = 123                          # GitLab project ID
cron = "0 9 * * 1"                               # 每周一 09:00
timezone = "Asia/Shanghai"
collectors = ["telemetry", "master_merges", "repo_scan"]
notifier = "dingtalk"
llm_model = ""                                   # 空 = 跟随 config.model
repo_clone_dir = "/var/lib/pr-agent/repo_scan_cache"
dingtalk_webhook_env = "DINGTALK_WEEKLY_WEBHOOK_URL"
dingtalk_secret_env = "DINGTALK_WEEKLY_SECRET"
diff_token_limit = 50000
markdown_chunk_limit = 18000
dingtalk_retry_attempts = 3
```

### 7.2 环境变量 (`PR_AGENT_WEEKLY_*` 前缀)

| 变量                                | 默认              | 说明                              |
|-------------------------------------|-------------------|-----------------------------------|
| `PR_AGENT_WEEKLY_ENABLED`           | `false`           | 总开关                            |
| `PR_AGENT_WEEKLY_TARGET_PROJECT_ID` | (必填)            | GitLab project ID                |
| `PR_AGENT_WEEKLY_CRON`              | `0 9 * * 1`       | 标准 cron 表达式                  |
| `PR_AGENT_WEEKLY_TIMEZONE`          | `Asia/Shanghai`   | APScheduler timezone             |
| `DINGTALK_WEEKLY_WEBHOOK_URL`       | (必填)            | 钉钉 webhook (custom robot)       |
| `DINGTALK_WEEKLY_SECRET`            | (可选)            | 加签 secret, 启用加签时必填      |

### 7.3 配置加载

- 复用 `pr_agent.config_loader.get_settings()`, 不新建 loader
- Dynaconf 自动 merge `[weekly_report]` block 到全局 settings
- env 覆盖优先级: env > `.pr_agent.toml` > `pr_agent/settings/configuration.toml`

---

## 8. 报告内容 Schema

`weekly_reports/<project_id>/<YYYY-WW>.json`:

```json
{
  "schema_version": 1,
  "project_id": 123,
  "week_label": "2026-W30",
  "week_start": "2026-07-21T00:00:00+08:00",
  "week_end": "2026-07-27T23:59:59+08:00",
  "generated_at": "2026-07-28T09:00:01+08:00",
  "timezone": "Asia/Shanghai",
  "sections": {
    "telemetry_overview": {
      "status": "ok",
      "data": {
        "mr_count": 23,
        "suggestion_count": 87,
        "adoption_rate": 0.64,
        "severity_breakdown": {"critical": 2, "high": 11, "medium": 38, "low": 36},
        "top_rules": [["ZLG-RULE-NO-LOG-EXC", 12], ["ZLG-RULE-X", 9]],
        "per_author": []
      }
    },
    "master_merges": {
      "status": "ok",
      "data": {
        "merge_count": 17,
        "author_count": 8,
        "additions": 2341,
        "deletions": 876,
        "mr_list": [
          {"iid": 234, "title": "...", "author": "...", "merged_at": "...", "url": "...", "summary": "..."}
        ]
      }
    },
    "repo_scan": {
      "status": "ok",
      "data": {
        "diff_stats": {"files_changed": 31, "additions": 3217, "deletions": 1043},
        "llm_review_markdown": "### 高风险模块\n- ...\n### 建议跟进\n- ...",
        "truncated": false
      }
    }
  }
}
```

任何 section `status="failed"`:
```json
{
  "sections": {
    "master_merges": {
      "status": "failed",
      "error": "GitLab API rate limit exceeded (429)",
      "data": null,
      "markdown": null
    }
  }
}
```

---

## 9. 失败处理

| 失败点                                              | 行为                                                                                  |
|-----------------------------------------------------|---------------------------------------------------------------------------------------|
| 单 collector 抛异常                                 | scheduler 捕获 → 该 section 标 `failed`, 其他继续, 不抛回                            |
| `repo_scan` clone 失败                              | collector 内部捕获 → `status="failed"`, error 含 git stderr 摘要                      |
| `repo_scan` LLM 超时 (> 5min)                       | collector 捕获 → `status="failed"`, error 含 "LLM timeout"                           |
| `report.build_artifact` 失败 (所有 collector 都 failed) | 跳过渲染, 写空 artifact + reporting_runs error log                                  |
| DingTalk 推送失败 (网络 / 4xx / 5xx)               | 重试 3 次 (指数退避 1s / 4s / 16s); 全失败写 `$PR_AGENT_DATA_DIR/reporting_runs/<ts>.failed.json`, **不抛回**, 等人工补发 |
| Markdown body > 18KB                                | renderer 按 `##` 章节边界自动拆多条, 标题加 `(1/N)`                                   |
| JSON dump 失败 (磁盘满 / 权限)                      | scheduler 主流程捕获, 写 error log, 跳过本次推送                                     |
| webhook 进程不可用                                  | reporter 完全不感知, 独立运行                                                          |

**永不抛回主流程的异常**: DingTalk 重试耗尽 / 单 collector 失败 / JSON dump 失败。
这些只写日志, 让 weekly_job 本身能稳定完成 (写 reporting_runs/<ts>.ok.json 标记本次运行结束)。

---

## 10. API Surface for TestMate

### 10.1 新增路由 (`pr_agent/telemetry/api.py` 末尾追加)

```
GET /api/v1/telemetry/weekly_reports/latest
    Query: project_id (int, required)
    Response: 完整周报 JSON (§8 schema)
    404: 该 project 无任何周报

GET /api/v1/telemetry/weekly_reports/list
    Query: project_id (int, required), limit (int, default 12)
    Response: [{week_label, generated_at, sections_status, has_failures}, ...]
             按文件名逆序 (最新在前)
```

### 10.2 鉴权

复用 `REVIEW_TELEMETRY_HTTP_TOKEN` (现有机制), 不新增鉴权方案。

### 10.3 错误码

| Code | 含义                                          |
|------|-----------------------------------------------|
| 200  | 成功                                          |
| 400  | 缺 `project_id`                              |
| 404  | 周报目录不存在 / 该 project 无周报            |
| 500  | 文件 I/O 错误                                |

---

## 11. 部署

### 11.1 新增 `docker/Dockerfile.reporter`

复用 root `Dockerfile` 生成的 image 作为 base (即 `Dockerfile` 的最终 stage,
当前是 `python:3.12-slim-bookworm` + `pip install -r requirements.txt` + ADD
pr_agent 源码)。reporter 容器只需要额外装两个包:

```dockerfile
FROM pr-agent:latest
ENV PYTHONPATH=/app
RUN pip install --no-cache-dir apscheduler requests
ENTRYPOINT ["python", "-m", "pr_agent.reporting.scheduler"]
```

docker-compose.yml 里给 reporter service 显式 `image: pr-agent:latest` + 同
project 内的 build context, 保证 base image 跟 pr-agent 容器一致; 或者直接
`image: ${PR_AGENT_IMAGE:-pr-agent:latest}` 让用户跟 pr-agent service 用同一
tag。

### 11.2 `docker-compose.yml` 增量

```yaml
  pr-agent-reporter:
    build:
      context: .
      dockerfile: docker/Dockerfile.reporter
    environment:
      PR_AGENT_WEEKLY_ENABLED: "true"
      PR_AGENT_WEEKLY_TARGET_PROJECT_ID: "${PR_AGENT_WEEKLY_TARGET_PROJECT_ID}"
      GITLAB_PERSONAL_ACCESS_TOKEN: "${GITLAB_PERSONAL_ACCESS_TOKEN}"
      DINGTALK_WEEKLY_WEBHOOK_URL: "${DINGTALK_WEEKLY_WEBHOOK_URL}"
      DINGTALK_WEEKLY_SECRET: "${DINGTALK_WEEKLY_SECRET}"
      OPENAI_API_KEY: "${OPENAI_API_KEY}"
      REVIEW_TELEMETRY_DB_PATH: /var/lib/pr-agent/telemetry.db
    volumes:
      - pr-agent-data:/var/lib/pr-agent
    restart: unless-stopped
    mem_limit: 2g
    cpus: '1.0'
```

### 11.3 `docs/docs/installation/local_deploy.md`

末尾 append "周报 reporter" 一节 (env 变量 + 启动 + 故障排查)。

---

## 12. 测试

### 12.1 单元测试 (`tests/unittest/reporting/`)

| 文件                                      | 覆盖                                                                |
|-------------------------------------------|---------------------------------------------------------------------|
| `test_telemetry_overview_collector.py`    | stub TelemetryStore, 验证聚合逻辑; 各 severity / author 维度正确   |
| `test_master_merges_collector.py`         | stub GitLab provider 返回 mock MR 列表, 验证筛选 + LLM 摘要调用     |
| `test_repo_scan_collector.py`             | 用 fixture repo (git init + 几次 commit), 验证 diff + LLM + 截断   |
| `test_dingtalk_notifier.py`               | 启本地 HTTP server 模拟钉钉, 验证 payload / 重试 / 加签             |
| `test_report_markdown.py`                 | golden snapshot: 给定 fixture sections → 期望 Markdown            |
| `test_scheduler_lifecycle.py`             | start / stop / 触发 / 异常隔离                                     |

### 12.2 现有测试不受影响

`tests/unittest/`, `tests/e2e_tests/`, `tests/health_test/` 全部不动。
CI 现有 `build-and-test` workflow 自动覆盖新单测。

### 12.3 不引入 e2e 测试

E2E 需要真实 GitLab API + 真实 LLM + 真实钉钉, 不在本 spec 范围内。

---

## 13. Rollout 与兼容性

- 默认 `[weekly_report].enabled = false`, fork 现有用户零行为变化
- 启用方式: 用户在 `.pr_agent.toml` 加 `[weekly_report]` block + 配 env
- 升级路径: git pull → 不影响运行; 想用周报再 deploy reporter service
- 不修改 telemetry 现有路由, 不修改 webhook handler, 不修改 GitLab provider

---

## 14. Open Items / 后续

- ❌ Manual trigger HTTP endpoint (本期不做, 留口子)
- ❌ 多项目 rollup (每个 pr-agent 部署只管一个 project)
- ❌ 飞书 / 企微 notifier 实现 (notifier base.py 留 protocol)
- ❌ 周报历史清理 / 归档 (外层 volume 备份管)
- ❌ HTML / PDF 输出 (TestMate 自己渲染 JSON)
- ❌ E2E 测试 (本期只覆盖单测 + golden snapshot)
- 后续可考虑: 报告模板可配置 (不同团队风格不同) / 周报对比 (本周 vs 上周 diff)

---

## 15. 参考

- [AI-Codereview-Gitlab daily_report](https://github.com/sunmh207/AI-Codereview-Gitlab/blob/main/biz/api/routes/daily_report.py)
- [AI-Codereview-Gitlab scheduler](https://github.com/sunmh207/AI-Codereview-Gitlab/blob/main/biz/api/scheduler.py)
- [AI-Codereview-Gitlab dingtalk](https://github.com/sunmh207/AI-Codereview-Gitlab/blob/main/biz/utils/im/dingtalk.py)
- 现有 telemetry API: `pr_agent/telemetry/api.py`
- 现有 telemetry store: `pr_agent/telemetry/store.py`
- 现有 webhook server: `pr_agent/servers/gitlab_webhook.py`
