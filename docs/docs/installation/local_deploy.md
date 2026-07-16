# 部署说明（GitLab + pr-agent 本地栈）

> 目标：在本机用 docker compose 把 `http://127.0.0.1:8929` 的 GitLab 和
> `http://127.0.0.1:5050/webhook` 的 pr-agent 跑起来，并在 MR 里用
> `/describe` `/review` `/improve` 触发评审机器人。
>
> 适用：macOS / Linux + Docker Desktop 或 docker engine + git + curl。

---

## 目录约定

整个部署拆成 **3 个相互独立的目录**：

| 目录 | 角色 | 是否进 git | 关键内容 |
|---|---|---|---|
| `<source-root>/` | pr-agent fork 源码 | 是 | `Dockerfile`, `pr_agent/`, `docs/` |
| `<gitlab-root>/` | GitLab CE + Runner 部署目录 | 否 | `docker-compose.yml`, `.env`, `gitlab-runner/config.toml` |
| `<deploy-root>/` | pr-agent 部署目录 | 否 | `docker-compose.yml`, `settings/*.toml`, `settings/.secrets.toml` |

按本机习惯命名（仅供参考）：

- `<source-root>`  → `~/my-pr-agent`（git clone 出来）
- `<gitlab-root>`  → `~/gitlab-stack`（含 gitlab + gitlab-runner 两个服务）
- `<deploy-root>`  → `~/my-pr-agent-deploy`（只含 pr-agent 一个服务）

> 三者解耦的好处：
> - 升级 fork 不影响秘钥/部署配置；
> - `docker-compose.yml` 里的 `build.context` 引用 `<source-root>`，源码改动后
>   在 `<deploy-root>` 里 `docker compose build` 就出新镜像，不用动 fork 仓本身的目录结构。

---

## 步骤 1 — 把 fork 源码 clone 下来

```bash
git clone <your-fork-url>/my-pr-agent.git ~/my-pr-agent
cd ~/my-pr-agent
git status                          # 工作区干净
git log --oneline -1                # HEAD 跟你要跑的版本一致
```

> 后续所有命令假定你在 `~/my-pr-agent/` 下。`<source-root>` 即指此目录。

---

## 步骤 2 — 拉 baseline 配置文件到 `<deploy-root>`

`<deploy-root>` 是 pr-agent 的部署目录，**不进 git**，放秘钥和非秘钥配置：

```bash
mkdir -p ~/my-pr-agent-deploy/settings

# 把源码仓里的 20 个 .toml 拷一份作为 baseline
cp ~/my-pr-agent/pr_agent/settings/*.toml        ~/my-pr-agent-deploy/settings/
cp ~/my-pr-agent/pr_agent/settings/code_suggestions/*.toml \
                                                  ~/my-pr-agent-deploy/settings/code_suggestions/
```

> `cp` 一次后即可。后续升级 fork 源码时，**手动对比新 baseline 与现有 toml**，
> 不要无脑覆盖（详见最后"配置漂移管理"小节）。

---

## 步骤 3 — 写 `settings/.secrets.toml`

```bash
cat > ~/my-pr-agent-deploy/settings/.secrets.toml <<'EOF'
[openai]
key = "<你的 LLM API key, OpenAI 兼容格式 sk-...>"

[gitlab]
personal_access_token = "<GitLab PAT, 用户至少是 reviewer, 需要 api + read_repository>"
shared_secret = "<32 字节 hex, GitLab webhook secret, 与 gitlab-root 里的 webhook secret 一致>"
EOF
chmod 600 ~/my-pr-agent-deploy/settings/.secrets.toml
```

`shared_secret` 必须跟 `<gitlab-root>/.env` 里的 `GITLAB_WEBHOOK_SECRET` 值相同——
两边独立部署、各持一份，但内容相同（GitLab → pr-agent 的 webhook payload 用它做 HMAC）。

`GITLAB_PERSONAL_ACCESS_TOKEN` / `MINIMAX_API_KEY` 之后填，**先留空**进到步骤 5 创建 review-bot。

---

## 步骤 4 — 改 `settings/configuration.toml`

只改 `<deploy-root>/settings/configuration.toml`，**4 类高频配置示例**：

```toml
[config]
model = "openai/<你的模型名>"                    # OpenAI 兼容 provider + 模型
fallback_models = ["openai/<你的模型名>"]        # 主模型挂了自动降级
response_language = "zh-CN"                      # 评审输出语言 (zh-CN / en-US / ja-JP ...)
rule_key_prefix = "<你的项目前缀>"                # AGENTS.md 规则键前缀, 例如 SSD / MYAPP
log_level = "INFO"                               # DEBUG 适合排查; INFO 适合生产
custom_model_max_tokens = 32000                  # 不在 PR-Agent 内置 token 表的模型必填
allowed_bot_usernames = ["<review-bot 用户名>"]   # 自评论触发的 service account

[openai]
api_base = "<你的 LLM base URL, OpenAI 兼容>"     # 留空用官方 https://api.openai.com/v1

[gitlab]
url = "<pr-agent 调 GitLab REST 用的地址>"        # 容器内访问宿主机用 host.docker.internal
user = "<review-bot 用户名>"
```

`rule_key_prefix` 决定 AGENTS.md 里规则键的格式：前缀是 `SSD` 就写 `SSD-RULE-NO-LOG-EXC`，
前缀是 `ZLG`（默认）就写 `ZLG-RULE-NO-LOG-EXC`。

---

## 步骤 5 — 写 `<deploy-root>/docker-compose.yml`

```yaml
name: my-pr-agent-deploy

services:
  pr-agent:
    build:
      context: <source-root 绝对路径>     # 例如 /Users/jarvs/my-pr-agent 或 ~/my-pr-agent
      dockerfile: Dockerfile
    image: my-pr-agent:zh
    container_name: pr-agent
    restart: unless-stopped
    entrypoint: ["python", "-m", "pr_agent.servers.gitlab_webhook"]
    environment:
      # 下面 4 个变量必须用 env: FastAPI/uvicorn 与持久化路径都直接读 os.environ,
      # 走 Dynaconf 不通。其他配置一律从 settings/configuration.toml 拿。
      PORT: "3000"
      PR_AGENT_DATA_DIR: /var/lib/pr-agent
      REVIEW_TELEMETRY_DB_PATH: /var/lib/pr-agent/telemetry.db
      REVIEW_TELEMETRY_HTTP_TOKEN: <前端调 telemetry API 用的 bearer token, 32 字节 hex>
    volumes:
      - pr-agent-data:/var/lib/pr-agent
      - ./settings:/app/pr_agent/settings:ro    # bind-mount 覆盖镜像同名文件
    ports:
      - "5050:3000"
    extra_hosts:
      - "host.docker.internal:host-gateway"     # 让容器能访问宿主机的 GitLab

volumes:
  pr-agent-data:
```

`build.context` 一定要写 `<source-root>` 的**绝对路径**——compose 文件在
`<deploy-root>` 下，源码在另一个目录，build 找不到相对路径。

---

## 步骤 6 — 启动 GitLab + pr-agent

先把 GitLab 跑起来（用 `<gitlab-root>/docker-compose.yml`，自带 GitLab + Runner）：

```bash
cd <gitlab-root>
docker compose up -d
docker compose ps                          # 等 gitlab 列显示 healthy
```

再 build + 启动 pr-agent：

```bash
cd <deploy-root>
docker compose build pr-agent              # 首次 build ~6 分钟, 增量 ~2 秒
docker compose up -d pr-agent
curl http://127.0.0.1:5050/api/v1/telemetry/health
# {"status":"ok","backend":"sqlite"}
```

---

## 步骤 7 — 在 GitLab 建 review-bot 账号并拿 PAT

1. 浏览器开 `http://127.0.0.1:8929`，用 `root` 登录（默认密码在容器启动日志里，搜 `Password:`）。
2. 第一次登录强制改 root 密码。
3. 顶栏右上角 → Admin area → Users → New user：
   - Username: `review-bot`
   - Email: `review-bot@local`
   - Password: 自设一个（之后不会再用密码登录，只用 PAT）
4. 点进 review-bot 用户 → Access Tokens → Add new token：
   - Name: `pr-agent`
   - Scopes: 勾 `api`、`read_repository`（如果要让 pr-agent 替你推分支再加 `write_repository`）
   - 点 Create。**复制显示的 token**，回头用。
5. 把 token 填进 `<deploy-root>/settings/.secrets.toml` 的 `[gitlab].personal_access_token`。
6. 让 pr-agent 重读配置（settings toml 是 bind-mount，但 `<deploy-root>` 改了文件后
   `LiteLLMAIHandler` 不会自动 reload，需要重启容器）：

```bash
cd <deploy-root>
docker compose restart pr-agent
```

---

## 步骤 8 — 在 GitLab 项目里配 webhook

1. 浏览器进你的 GitLab 项目（比如 `root/auto-review-test`），左侧菜单 → Settings → Webhooks。
2. 填：
   - **URL**: `http://host.docker.internal:5050/webhook`
   - **Secret token**: 步骤 3 里 `[gitlab].shared_secret` 的值
   - **Trigger**: 勾 Push events、Merge request events、Comments
   - **Enable SSL verification**: 不勾
3. 点 Add webhook。
4. 同一页 → Recent events，点 Push events 的 Test，看返回 200。

---

## 步骤 9 — 在项目根放 AGENTS.md（可选但推荐）

pr-agent 会读项目根的 `AGENTS.md` 当评审规约来源。规则键格式是
``<PREFIX>-RULE-<KEY>``; 前缀来自 `settings/configuration.toml` 里的
`[config].rule_key_prefix`。

最小例子（假设前缀是 `SSD`）：

```bash
cd /path/to/your/test-project
cat > AGENTS.md <<'MD'
# Project Review Rules (SSD-*)

- `SSD-RULE-NO-LOG-EXC`         — replace `except Exception: pass` with `logging.exception(...)` + re-raise
- `SSD-RULE-DOCSTRING-REQUIRED` — every new `def` must have a single-line docstring
- `SSD-RULE-NO-BARE-PRINT`      — no `print(...)` in production code
- `SSD-RULE-TYPEHINTS`          — every new function must have type annotations
MD
git add AGENTS.md
git commit -m "docs: add AGENTS.md rule keys"
git push origin main
```

---

## 步骤 10 — 触发机器人

在任意一个已开的 MR 里写评论：

```
/improve
```

几秒后机器人会回复一条 persistent 主题评论，包含：

- `## PR Code Suggestions ✨` 主块
- 如果有违规：diff 行上出现 `Apply suggestion` 按钮
- 一段 `<details>` 块告诉你哪些 AGENTS.md 规则这次没被覆盖到

其他可用命令：

| 评论文本 | 干什么 |
|---|---|
| `/describe` | 生成 PR 描述（中文） |
| `/review` | 整体评审（不修改代码） |
| `/improve` | 评审 + 给可应用代码建议 |
| `/ask <问题>` | 对这个 MR 提问 |
| `/help` | 列出帮助 |

> 自动触发：MR 创建 / push / 重新打开时，pr-agent 会按
> `[gitlab].pr_commands` 列表自动跑一组命令（默认含 `/describe` `/review` `/improve`）。

---

## 步骤 11 — 跑完关掉

```bash
cd <deploy-root> && docker compose stop        # 停 pr-agent, 保留数据卷
cd <gitlab-root> && docker compose stop        # 停 GitLab
# 完全清理 (含数据卷):
cd <deploy-root> && docker compose down -v
cd <gitlab-root> && docker compose down -v
```

---

## 配置漂移管理

**升级 fork 源码时（`git pull` 之后）**：

1. 重新从 `<source-root>/pr_agent/settings/` 拷一份 baseline 到 `<deploy-root>/settings/`：

   ```bash
   diff -ru ~/my-pr-agent/pr_agent/settings/ ~/my-pr-agent-deploy/settings/ | less
   ```

2. 只把 baseline 里**新增的 key** 合到本地——**不要覆盖你已经改过的值**：

   - 同一行 key 你改过 → 保留本地版本
   - baseline 新加的 key → 拷过来
   - baseline 删掉的 key → 如果你没用上, 直接删; 如果你依赖它, 升级到上游新 key

3. 改完 `<deploy-root>/settings/*.toml` 后：

   ```bash
   cd <deploy-root>
   docker compose restart pr-agent
   ```

**原则**: `<deploy-root>/settings/` 是部署的真值源 (source of truth)；
`<source-root>/pr_agent/settings/` 只是 baseline。`<deploy-root>` 不进 git，
所以漂移只在你脑子里——靠上面 diff 流程保证升级时不出岔。

---

## 一句话总览

```
1.  git clone fork 源码到 <source-root>
2.  cp baseline toml 到 <deploy-root>/settings/
3.  写 <deploy-root>/settings/.secrets.toml (3 个秘钥, 0600)
4.  改 <deploy-root>/settings/configuration.toml (model / language / gitlab URL)
5.  写 <deploy-root>/docker-compose.yml (build.context 指向 <source-root>)
6.  docker compose up -d (GitLab + pr-agent)
7.  在 GitLab 建 review-bot + 拿 PAT → 填回 .secrets.toml
8.  GitLab 项目 → Webhooks → 配 URL/secret/trigger
9.  项目根放 AGENTS.md (可选)
10. MR 评论写 /improve /review /describe
11. 不用了 docker compose stop
```

---

## 离线 / 内网部署

目标：机器无外网（不能 `pip install` / `docker pull` / `apt update`），但仍然用
同一套 docker compose 把栈跑起来。

适合：内网开发机、客户机房、air-gapped 环境。

### 离线前置依赖

| 资产 | 大小 | 怎么来 |
|---|---|---|
| Docker 镜像 `python:3.12.13-slim`（pr-agent 基础层） | ~120 MB | 在能上网的机器 `docker pull python:3.12.13-slim`，`docker save -o python.tar` 导出 |
| Docker 镜像 `gitlab/gitlab-ce:17.5.0-ce.0` | ~1.3 GB | 同上 |
| Docker 镜像 `gitlab/gitlab-runner:v17.5.0` | ~600 MB | 同上 |
| 项目依赖 `requirements.txt` 里的 wheel 包 | ~500 MB | `pip download -r requirements.txt -d wheels/` 离线下载 |
| 项目源码 | ~5 MB | git clone / scp |
| LLM API key | — | 提前申请 |

### 一次性：在能上网的机器打 asset 包

```bash
# 在有网机器上执行
mkdir -p assets/{pr-agent,gitlab,runner,wheels}

# 镜像
docker pull python:3.12.13-slim             && docker save -o assets/pr-agent/python.tar      python:3.12.13-slim
docker pull gitlab/gitlab-ce:17.5.0-ce.0    && docker save -o assets/gitlab/gitlab.tar        gitlab/gitlab-ce:17.5.0-ce.0
docker pull gitlab/gitlab-runner:v17.5.0    && docker save -o assets/runner/runner.tar         gitlab/gitlab-runner:v17.5.0

# Python wheels（与系统 platform 同；通常是 manylinux x86_64）
pip download -r <source-root>/requirements.txt -d assets/wheels

# 打 zip 整个传到内网机器
zip -r assets.zip assets/
```

### 在内网机器上加载 asset

```bash
unzip assets.zip

# 加载镜像
docker load -i assets/pr-agent/python.tar
docker load -i assets/gitlab/gitlab.tar
docker load -i assets/runner/runner.tar

# 验证
docker images | grep -E "python:3.12|gitlab-ce|gitlab-runner"
```

### build 镜像（内网，无 pip 拉源）

把 `wheels/` 放进源码根、传过去：

```bash
cd <source-root>
scp -r ../assets/wheels ./wheels
```

改 `Dockerfile` 让 pip 走本机 wheels——把单行 `RUN pip install --no-cache-dir .`
替换成走本地 wheels：

```dockerfile
RUN pip install --no-cache-dir --no-index --find-links=/app/wheels .
```

### 离线 docker-compose 启动

把 `assets/`、`<source-root>/`、`<deploy-root>/` 全部拷到内网机器同一目录：

```bash
# 内网机器上的目录结构
/serve/
  ├─ <deploy-root>/         # 含 docker-compose.yml, settings/, .secrets.toml
  ├─ <source-root>/         # 含 Dockerfile, pr_agent/, requirements.txt
  └─ assets/                # 已经 docker load 进本地镜像缓存

cd /serve/<deploy-root>
docker compose build pr-agent
docker compose up -d

cd /serve/<gitlab-root>
docker compose up -d
```

跟正常流程从 **步骤 6** 之后的命令一致。

### 离线关键踩坑

1. **GitLab Omnibus 启动会去 `gitlab.com` 拉证书链**：在能上网机器上启动一次
   `gitlab` 让它把根证书缓存到 volume，再 `docker commit` / `docker save` 出来；
   或者把 GitLab 顶层 config `external_url` 配成纯 IP（`http://127.0.0.1:8929`，
   不要带域名）。
2. **`docker build` 在内网可能被 proxy 拦截**：检查
   `/etc/systemd/system/docker.service.d/http-proxy.conf` 或者
   `~/.docker/config.json`，去掉 `proxies`。
3. **LLM 调用要求内网能访问 `<你的 LLM base URL>`**：如果连 LLM 都没外网，
   整个 `pr-agent` 服务无法工作，必须替换 LLM 为内网 / Ollama / vLLM 之类。
4. **GitLab Runner 注册需要 `<gitlab.example.com>` 可达**：内网要建 DNS 或者
   在 host 加 `/etc/hosts`。
5. **apt update 在 `python:3.12-slim` 里是必须的**：基础镜像装 `git`、`curl` 时要拉
   `debian-security` 索引。建议在能上网机器预先 `apt update && apt install`
   把 layers 缓存好，再 `docker save` 整个 base。

---

## 一句话总览 — 离线版

```
能上网机器:  docker pull + docker save + pip download wheels
                          ↓ zip / scp
内网机器:    docker load + 改 Dockerfile 走 local wheels + docker compose build
                          ↓
            步骤 6~11 同正常流程 (GitLab UI / webhook / 评论)
