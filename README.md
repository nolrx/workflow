# AI Creative Studio

AI 驱动的软件创作平台（**Code 域**）：从需求文档 → 开发流程 → 文档拆分 → 风格文档 → UI 预览 → **前端代码生成与预览**，并可进一步做**全栈生成（前端 + 后端 + 中间件）+ 应用部署**。所有耗时步骤通过可回放的 **Agent Swarm** 运行时执行。

> 面向开发者的权威说明见 `CLAUDE.md`（最详尽）；Codex 用 `AGENTS.md`。本文件是面向使用者的快速上手。

## 技术栈

- **后端**: Flask + SQLAlchemy + PostgreSQL/SQLite
- **前端**: React 19 + TypeScript + Vite
- **缓存**: Redis
- **提示词存储**: MongoDB（可在线编辑系统 prompt；不可达时回退到内置默认 prompt）
- **AI 服务**: 文本 → Claude（Anthropic），图像 → OpenAI（`gpt-image-2`）；`AI_PROVIDER=gemini` 作为回退。按能力（capability）路由，文本/图像可各走不同 provider。
- **沙箱**: Docker（容器化代码生成 / 部署用的 `fe-agent` / `be-agent` / `slicer-agent` 镜像）
- **包管理**: uv (Python) / npm (Node.js)

## 快速开始

### 环境要求

- Python >= 3.10
- Node.js >= 18.0.0
- Docker & Docker Compose（数据库服务；容器化代码生成 / 部署亦需要）

### 1. 安装依赖

```bash
# 一键安装所有依赖
npm run setup

# 或分别安装
uv sync                      # Python 依赖
cd frontend && npm install   # 前端依赖
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，至少填入 ANTHROPIC_API_KEY（文本）与 OPENAI_API_KEY（图像）
```

### 3. 启动依赖服务（仅本地开发）

本地开发只需起数据库（与可选的 MongoDB）容器，应用本身用 `npm run dev` 跑在宿主机上：

```bash
docker compose up -d postgres        # 必需
docker compose up -d mongo           # 可选：在线编辑 prompt；不起则用内置默认 prompt
```

> 整套应用容器化部署见下方 [部署（单机 Docker）](#部署单机-docker)。

### 4. 初始化数据库

表结构在后端首次启动时由 `db.create_all()` 自动创建，无需手动迁移（项目未使用 Alembic）。

### 5. 启动开发服务器

```bash
# 同时启动前后端
npm run dev

# 或分别启动
npm run dev:backend    # 后端 (localhost:5001)
npm run dev:frontend   # 前端 (localhost:3000，/api 代理到 :5001)
```

## 环境变量配置

按能力路由：文本与图像各自独立配置；`AI_PROVIDER` 是两者都未单独配置时的回退 provider。完整清单见 `.env.example`。

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `DATABASE_URL` | PostgreSQL 连接串（裸机/开发） | `postgresql://ai_studio:...@localhost:5432/ai_creative_studio` |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | compose 建库 + 拼后端容器内 `DATABASE_URL` | `ai_studio` / … / `ai_creative_studio` |
| `REDIS_URL` | Redis 连接串 | `redis://localhost:6379/0` |
| `MONGODB_URI` / `MONGODB_DB` | 系统 prompt 存储（可选；不可达则用内置默认） | `mongodb://localhost:27017` / `ai_creative_studio` |
| `FLASK_ENV` | Flask 环境 | `development` |
| `SECRET_KEY` / `JWT_SECRET_KEY` | Flask / JWT 密钥（生产必填；开发不填则自动生成） | 需要设置 |
| `AI_TEXT_PROVIDER` / `AI_TEXT_MODEL` | 文本 provider / 模型 | `claude` / `claude-opus-4-8` |
| `ANTHROPIC_API_KEY` | Claude API key（文本生成 + fe/be-agent CLI 用） | 需要设置 |
| `AI_IMAGE_PROVIDER` / `AI_IMAGE_MODEL` | 图像 provider / 模型 | `openai` / `gpt-image-2` |
| `OPENAI_API_KEY` | OpenAI API key（图像生成 + Codex 资源/切片用） | 需要设置 |
| `AI_PROVIDER` / `AI_API_KEY` | 回退 provider / 其 key（默认走 Gemini） | `gemini` / - |
| `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` | Stripe（可选） | - |

> 其它可选项：GitHub App 集成（把每个 Code 会话产物自动同步到私有仓库）、各沙箱 agent 的超时预算、`DEPLOY_CONTROL_TOKEN`（优雅重发布的 ops 端点鉴权）等，详见 `.env.example`。

## 常用命令

### 开发

```bash
npm run dev              # 启动前后端开发服务器
npm run dev:backend      # 仅启动后端 (localhost:5001)
npm run dev:frontend     # 仅启动前端 (localhost:3000)
npm run build            # 构建前端生产版本 (tsc -b && vite build)
```

### 测试

```bash
npm run test:backend                 # 后端全部测试 (pytest)
uv run pytest -m "not integration"   # 只跑单元测试（无需 API key，CI/日常默认）
uv run pytest -m integration -s      # 集成测试（需 ANTHROPIC_API_KEY + PANLAXY_API_KEY + 网络，产生真实费用）
```

> ⚠️ 前端未配置测试框架（没有 vitest）；`npm run test` / `npm run test:frontend` 当前会失败。

### 代码检查

```bash
npm run lint             # 后端 ruff + 前端 eslint
npm run lint:backend     # uv run ruff check backend/
npm run lint:frontend    # cd frontend && eslint .
```

> ⚠️ 项目未使用 Alembic：`npm run db:migrate` / `npm run db:revision` 当前会失败；表结构由 `db.create_all()` 在启动时创建。

## 项目结构

```
ai-creative-studio/
├── backend/
│   ├── app.py           # Flask 应用入口（蓝图注册、错误处理、端口 5001）
│   ├── config.py        # 配置管理
│   ├── extensions.py    # Flask 扩展（db / jwt）
│   ├── models/          # 数据模型（code / agent / …）
│   ├── routes/          # API 路由
│   ├── services/        # 业务逻辑（code / agent / ai / credit / prompts / …）
│   ├── prompts/         # AI 提示词模板（code/*.txt，运行时由 MongoDB 提供）
│   └── utils/           # 工具函数（统一响应等）
├── frontend/
│   ├── src/
│   │   ├── api/         # API 客户端（axios，401 透明刷新）
│   │   ├── components/  # 组件（ui / code / agent / layout / …）
│   │   ├── pages/       # 页面（code / dashboard / settings / team / admin / auth）
│   │   ├── stores/      # 状态管理（Zustand）
│   │   └── locales/     # i18n（en / ja / ko / zh-CN）
│   └── ...
├── plugin/figma/        # Figma 导入插件（独立构建）
├── docs/                # 设计 / 交接文档
├── docker-compose.yml   # Docker 服务配置
├── Makefile             # 部署 / 运维命令
├── package.json         # npm scripts
├── pyproject.toml       # Python 项目配置
└── .env.example         # 环境变量模板
```

## 部署（单机 Docker）

整套应用都能用 Docker Compose 起在一台机器上：**nginx 前端 + Flask 后端(gunicorn) + PostgreSQL + MongoDB + Redis**。后端镜像内置 Docker CLI 并挂载宿主机 `/var/run/docker.sock`，因此 Code 域的容器化工作流（前端工程 / 后端工程 / 切片分析 / 全栈部署）能从后端容器里 `docker run` 起一次性沙箱容器（Docker-out-of-Docker）。

### 前置条件

- 宿主机已装 Docker（含 Compose 插件）且 daemon 运行中
- `/var/run/docker.sock` 可访问（后端容器以 root 运行，无需额外配 docker group）
- 仓库位于 `/data/workflow`：compose 里 `.fe-agent-work` 用**同一绝对路径**挂载来交接沙箱产物；换目录需同步改 `docker-compose.yml` 的 `TMPDIR` 与该挂载

### 一键部署

```bash
make env       # 没有 .env 时从 .env.example 复制，然后填入下列 key
make deploy    # = docker compose --profile setup build + up -d
```

部署后访问 `http://<宿主机>/`（前端 nginx 监听 80，`/api` 反代到 `backend:5001`）。

`.env` 必填项：

| 变量 | 用途 |
|------|------|
| `ANTHROPIC_API_KEY` | 文本生成 + fe-agent / be-agent 的 Claude Code CLI |
| `OPENAI_API_KEY` | 图像生成 + Codex CLI（前端资源、Figma 切片分析） |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | compose 建库，并据此拼后端容器内的 `DATABASE_URL`（自动指向 `postgres` 服务） |
| `SECRET_KEY` / `JWT_SECRET_KEY` | 不设则开发模式自动生成（重启后已签发的 JWT 失效） |

### 沙箱镜像

容器化工作流要 `docker run` 的镜像必须存在于宿主机镜像库，它们在 compose 里是 `setup` profile 的 build-only 服务，`make build`（即 `docker compose --profile setup build`）会一并构建：

| 镜像 | 用于工作流 |
|------|-----------|
| `fe-agent:latest` | `code_frontend_project_generation`（Claude Code + Codex，多文件前端工程 + 资源生成） |
| `be-agent:latest` | `code_backend_project_generation` / `code_fullstack_deploy`（多语言工具链，后端工程 + 部署期自愈） |
| `slicer-agent:latest` | `code_figma_slice_generation`（Codex CLI 切片分析） |

`code_full_generation`、`code_canvas_generation` 是后端进程内线程，**不需要**这些镜像。

### 常用运维命令（Makefile）

| 命令 | 作用 |
|------|------|
| `make deploy` | 构建全部镜像并后台启动（首次/完整部署） |
| `make redeploy` | **优雅重发布**：drain → 重建 backend → 等 `/health/ready` → reload nginx（在飞 run 自动续跑） |
| `make build` / `make fe-agent` | 构建全部镜像（含沙箱）/ 仅构建 fe-agent 沙箱 |
| `make up` / `make down` / `make restart` | 启动 / 停止 / 重启 |
| `make rebuild` | down → 重建镜像 → up |
| `make drain` / `make undrain` / `make drain-status` | 暂停接收新 run / 恢复 / 查看排空状态（需 `DEPLOY_CONTROL_TOKEN`） |
| `make logs S=backend` | 跟踪日志（`S=` 限定服务，缺省看全部） |
| `make ps` / `make config` | 查看状态 / 校验并渲染最终 compose 配置 |
| `make destroy` | ⚠️ down 并删除数据卷（**会清空数据库**） |

> 优雅重发布的设计与坑见 `docs/platform-deploy.md`。老机器若只有带连字符的 `docker-compose`：`make up COMPOSE="docker-compose"`。运行 `make` 或 `make help` 可列出全部命令。

## 许可证

MIT
