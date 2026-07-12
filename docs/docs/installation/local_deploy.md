# 部署说明（GitLab + pr-agent 本地栈）

> 目标：在本机用 docker compose 把 `http://127.0.0.1:8929` 的 GitLab 和 `http://127.0.0.1:5050/webhook` 的 pr-agent 跑起来，并在 MR 里用 `/describe` `/review` `/improve` 触发评审机器人。
>
> 适用：macOS / Linux + Docker Desktop 或 docker engine + git + curl。

---

## 步骤 1 — 把代码拉下来

```bash
git clone <your-fork-url-of-my-pr-agent>/my-pr-agent.git
cd my-pr-agent
```

后续命令都在这个目录下。

---

## 步骤 2 — 建 secret 文件

新建文件 `/Users/jarvs/gitlab-stack/.env`：

```bash
mkdir -p /Users/jarvs/gitlab-stack
cat > /Users/jarvs/gitlab-stack/.env <<'ENV'
GITLAB_PERSONAL_ACCESS_TOKEN=glpat-PLACEHOLDER
GITLAB_WEBHOOK_SECRET=PLACEHOLDER
MINIMAX_API_KEY=sk-cp-PLACEHOLDER
ENV
```

下面三处填什么：

| 变量 | 怎么拿到 |
|---|---|
| `GITLAB_PERSONAL_ACCESS_TOKEN` | 步骤 4 里给 review-bot 创建 |
| `GITLAB_WEBHOOK_SECRET` | 自己编一个 32 字符十六进制，例如 `openssl rand -hex 16` 的输出 |
| `MINIMAX_API_KEY` | MiniMax 控制台发的 OpenAI 兼容 API key，格式 `sk-cp-…` |

> 三个值之后填，**先留 `PLACEHOLDER`** 进到步骤 4 创建 review-bot。

---

## 步骤 3 — build pr-agent 镜像

```bash
cd /Users/jarvs/my-pr-agent
docker build -f docker/Dockerfile --target gitlab_webhook -t my-pr-agent:zh .
```

首次 ~6 分钟；之后增量改代码再 build ~2 秒。

---

## 步骤 4 — 在 GitLab 建 review-bot 账号并拿 PAT

1. 启动并等 GitLab 跑起来后再回这个步骤（GitLab 第一次启动要 ~3 分钟）。

启动：

```bash
cd /Users/jarvs/gitlab-stack
# 把 docker-compose.yml 放到这个目录；如果从模板拷过来，确保起停命令一致
ls docker-compose.yml   # 必须存在
docker compose up -d
docker compose ps       # 等到 gitlab 列显示 healthy
```

2. 浏览器开 `http://127.0.0.1:8929`，用 `root` 登录（默认密码在容器启动日志里，搜 `Password:`）。
3. 第一次登录强制改 root 密码。
4. 顶栏右上角 → Admin area → Users → New user：
   - Username: `review-bot`
   - Email: `review-bot@local`
   - Password: 自设一个（之后不会再用密码登录，只用 PAT）
5. 点进 review-bot 用户 → Access Tokens → Add new token：
   - Name: `pr-agent`
   - Scopes: 勾 `api`、`read_repository`（如果要 pr-agent 替你推分支再加 `write_repository`）
   - 点 Create。**复制显示的 token**，回头用。
6. 把 token 填进 `/Users/jarvs/gitlab-stack/.env`：

```bash
sed -i '' 's|glpat-PLACEHOLDER|你的token|' /Users/jarvs/gitlab-stack/.env
```

7. 让 pr-agent 重新读 env（env 是在容器启动时注入的，需要重启）：

```bash
cd /Users/jarvs/gitlab-stack
docker compose up -d pr-agent
```

---

## 步骤 5 — 在 GitLab 项目里配 webhook

1. 浏览器进你的 GitLab 项目（比如 `root/auto-review-test`），左侧菜单 → Settings → Webhooks。
2. 填：
   - **URL**: `http://host.docker.internal:5050/webhook`
   - **Secret token**: 步骤 2 里 `GITLAB_WEBHOOK_SECRET` 的值
   - **Trigger**: 勾 Push events、Merge request events、Comments
   - **Enable SSL verification**: 不勾
3. 点 Add webhook。
4. 同一页 → Recent events，点 Push events 的 Test，看返回 200。

---

## 步骤 6 — 在项目根放 AGENTS.md（可选但推荐）

pr-agent 会读项目根的 `AGENTS.md` 当评审规约来源。如果你想让评审机器人检查某些规则就放这个文件。

最小例子：

```bash
cd /path/to/your/test-project
cat > AGENTS.md <<'MD'
# Project Review Rules (ZLG-*)

- `ZLG-RULE-NO-LOG-EXC`         — replace `except Exception: pass` with `logging.exception(...)` + re-raise
- `ZLG-RULE-DOCSTRING-REQUIRED` — every new `def` must have a single-line docstring
- `ZLG-RULE-NO-BARE-PRINT`      — no `print(...)` in production code
- `ZLG-RULE-TYPEHINTS`          — every new function must have type annotations
- `ZLG-RULE-FORBIDDEN-COMMENT`  — commits must not contain `ZLG-VIOLATION-MARKER` comments
MD
git add AGENTS.md
git commit -m "docs: add AGENTS.md rule keys"
git push origin main
```

---

## 步骤 7 — 触发机器人

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

---

## 步骤 8 — 跑完关掉

```bash
cd /Users/jarvs/gitlab-stack
docker compose stop    # 停
docker compose down    # 停并删容器（named volume 保留，下次起来数据还在）
```

数据清空（不可逆）：

```bash
cd /Users/jarvs/gitlab-stack
docker compose down -v
rm /Users/jarvs/gitlab-stack/.env
# 之后从步骤 2 重新走
```

---

## 出问题查这里

| 现象 | 看哪里 |
|---|---|
| 机器人没回复 | `docker logs -f pr-agent`，看 webhook 接没接到 |
| `Review effort` label 没出现 | `docker logs -f pr-agent`，搜 `error` `traceback` |
| `Skipping fallback model 'MiniMax-M3'` | 警告，正常。fallback 模型没带 `openai/` 前缀，主模型照常跑 |
| LLM 卡住没有响应 | `ai_timeout` 默认 120s；超过就 `docker compose restart pr-agent` |
| Webhook 测试 502 | `pr-agent` 容器有没有真起来：`docker ps`，URL 用 `host.docker.internal:5050` 别用 `localhost` |
| 评论写了 `/improve` 但机器人说 "Unknown command" | GitLab 评论用户是 `review-bot` 但 `.env` 里 token 是别的账号；或者 token 没 `api` scope |

---

## 一句话总览

```
1.  clone 代码
2.  写 /Users/jarvs/gitlab-stack/.env（3 个变量，先 PLACEHOLDER）
3.  docker build my-pr-agent:zh
4.  docker compose up -d（在 GitLab 建 review-bot + 拿 PAT → 填回 .env）
5.  GitLab 项目 → Webhooks → 配 URL/secret/trigger
6.  项目根放 AGENTS.md（可选）
7.  MR 评论写 /improve /review /describe
8.  不用了 docker compose stop / down
```

---

## 离线 / 内网部署

目标：机器无外网（不能 `pip install` / `docker pull` / `apt update`），但仍然用同一套 docker compose 把栈跑起来。

适合：内网开发机、客户机房、air-gapped 环境。

### 离线前置依赖

| 资产 | 大小 | 怎么来 |
|---|---|---|
| Docker 镜像 `python:3.12.13-slim`（pr-agent 基础层） | ~120 MB | 在能上网的机器 `docker pull python:3.12.13-slim`，`docker save -o python.tar` 导出 |
| Docker 镜像 `gitlab/gitlab-ce:17.5.0-ce.0` | ~1.3 GB | 同上 |
| Docker 镜像 `gitlab/gitlab-runner:v17.5.0` | ~600 MB | 同上 |
| 项目依赖 `requirements.txt` 里的 wheel 包 | ~500 MB | `pip download -r requirements.txt -d wheels/` 离线下载 |
| 项目源码 | ~5 MB | git clone / scp |
| MiniMax API key | — | 提前申请 |

### 一次性：在能上网的机器打 asset 包

```bash
# 在有网机器上执行
mkdir -p assets/{pr-agent,gitlab,runner,wheels}

# 镜像
docker pull python:3.12.13-slim   && docker save -o assets/pr-agent/python.tar      python:3.12.13-slim
docker pull gitlab/gitlab-ce:17.5.0-ce.0 && docker save -o assets/gitlab/gitlab.tar gitlab/gitlab-ce:17.5.0-ce.0
docker pull gitlab/gitlab-runner:v17.5.0 && docker save -o assets/runner/runner.tar gitlab/gitlab-runner:v17.5.0

# Python wheels（与系统 platform 同；通常是 manylinux x86_64）
pip download -r requirements.txt -d assets/wheels

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

把 `wheels/` 放进项目根、传过去：

```bash
cd my-pr-agent
scp -r ../assets/wheels ./wheels
```

改 `docker/Dockerfile`：build 时让 pip 走本机 wheels。先准备一个本地 index：
build 离线版 Dockerfile（不改源码、更不易损坏）—— 把单行 `RUN pip install --no-cache-dir .` 替换成走本地 wheels：

```bash
# 在能上网的机器上，一次性导出 wheels
pip download \
  --dest wheels \
  --requirement requirements.txt \
  --requirement <(grep -E "^[a-zA-Z0-9_-]" pyproject.toml | sed 's/^/-e /' | head -3) || true

# 内网机器：把 wheels/ 放在仓库根
# 改 docker/Dockerfile 第 11 行成：
#   RUN pip install --no-cache-dir --no-index --find-links=/app/wheels .
```

如果你不想改 Dockerfile，用 `docker buildx` + 本地 `--cache-from` 也行——但最简单的还是 export pip wheels 进项目。

### 离线 docker-compose 启动

把 `assets/`、`my-pr-agent/`、`docker-compose.yml`、`.env` 全部拷到内网机器同一目录：

```bash
# 内网机器上的目录结构
/serve/
  ├─ docker-compose.yml
  ├─ .env
  ├─ my-pr-agent/         # 含 DEPLOY.md, src, Dockerfile
  └─ assets/              # 已经 docker load 进本地镜像缓存

cd /serve
docker compose up -d
```

跟正常流程从 **步骤 4** 之后的命令一致。

### 离线关键踩坑

1. **GitLab Omnibus 启动会去 `gitlab.com` 拉证书链**：在能上网机器上启动一次 `gitlab` 让它把根证书缓存到 volume，再 `docker commit` / `docker save` 出来；或者把 GitLab 顶层 config `external_url` 配成纯 IP（`http://127.0.0.1:8929`，不要带域名）。
2. **`docker build` 在内网可能被 proxy 拦截**：检查 `/etc/systemd/system/docker.service.d/http-proxy.conf` 或者 `~/.docker/config.json`，去掉 `proxies`。
3. **LLM 调用要求内网能访问 `api.minimaxi.com`**：如果连 LLM 都没外网，整个 `pr-agent` 服务无法工作，必须替换 LLM 为内网 / Ollama / vLLM 之类。
4. **GitLab Runner 注册需要 `gitlab.example.com` 可达**：内网要建 DNS 或者在 host 加 `/etc/hosts`。
5. **apt update 在 `python:3.12-slim` 里是必须的**：基础镜像装 `git`、`curl` 时要拉 `debian-security` 索引。建议在能上网机器预先 `apt update && apt install` 把 layers 缓存好，再 `docker save` 整个 base。

---

## 一句话总览 — 离线版

```
能上网机器:  docker pull + docker save + pip download wheels
                          ↓ zip / scp
内网机器:    docker load + 改 Dockerfile 走 local wheels + docker compose up -d
                          ↓
            步骤 4~8 同正常流程 (GitLab UI / webhook / 评论)
```

完整字数：`/Users/jarvs/my-pr-agent/DEPLOY.md` 共 ~270 行。
