# AI Creative Studio

AI 驱动的创意内容生成平台，支持 PPT 生成和社交媒体内容创作。

## 技术栈

- **后端**: Flask + SQLAlchemy + PostgreSQL/SQLite
- **前端**: React + TypeScript + Vite
- **缓存**: Redis
- **AI 服务**: Google Gemini / OpenAI
- **包管理**: uv (Python) / npm (Node.js)

## 快速开始

### 环境要求

- Python >= 3.10
- Node.js >= 18.0.0
- Docker & Docker Compose (用于数据库服务)

### 1. 克隆项目

```bash
git clone <repository-url>
cd ai-creative-studio
```

### 2. 安装依赖

```bash
# 一键安装所有依赖
npm run setup

# 或分别安装
uv sync                      # Python 依赖
cd frontend && npm install   # 前端依赖
```

### 3. 配置环境变量

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env 文件，填入必要配置
```

### 4. 启动数据库服务(仅本地开发)

本地开发只需起数据库容器，应用本身用 `npm run dev` 跑在宿主机上：

```bash
docker compose up -d postgres
```

> 整套应用容器化部署见下方 [部署(单机 Docker)](#部署单机-docker)。

### 5. 初始化数据库

表结构在后端首次启动时由 `db.create_all()` 自动创建，无需手动迁移。

### 6. 启动开发服务器

```bash
# 同时启动前后端
npm run dev

# 或分别启动
npm run dev:backend    # 后端 (localhost:5001)
npm run dev:frontend   # 前端 (localhost:3000，/api 代理到 :5001)
```

## 环境变量配置

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `DATABASE_URL` | PostgreSQL 连接字符串 | `postgresql://ai_studio:...@localhost:5432/ai_creative_studio` |
| `REDIS_URL` | Redis 连接字符串 | `redis://localhost:6379/0` |
| `FLASK_ENV` | Flask 环境 | `development` |
| `SECRET_KEY` | Flask 密钥 | 需要设置 |
| `JWT_SECRET_KEY` | JWT 签名密钥 | 需要设置 |
| `AI_PROVIDER` | AI 提供商 | `gemini` |
| `AI_API_KEY` | AI API 密钥 (也支持 `GOOGLE_API_KEY`/`GEMINI_API_KEY`) | - |
| `AI_TEXT_MODEL` | 文本模型名称 | `gemini-3-flash-preview` |
| `AI_IMAGE_MODEL` | 图像模型名称 | `imagen-3.0-generate-002` |
| `AI_BASE_URL` | 自定义 API 地址 (可选) | - |
| `STRIPE_SECRET_KEY` | Stripe 密钥 (可选) | - |
| `STRIPE_WEBHOOK_SECRET` | Stripe Webhook 密钥 (可选) | - |

## 常用命令

### 开发

```bash
npm run dev              # 启动前后端开发服务器
npm run dev:backend      # 仅启动后端
npm run dev:frontend     # 仅启动前端
```

### 测试

```bash
npm run test             # 运行所有测试
npm run test:backend     # 后端测试 (pytest)
npm run test:frontend    # 前端测试 (vitest)
```

### 代码检查

```bash
npm run lint             # 运行所有 lint
npm run lint:backend     # 后端 lint (ruff)
npm run lint:frontend    # 前端 lint (eslint)
```

### 数据库

```bash
npm run db:migrate                    # 应用迁移
npm run db:revision -- "描述信息"      # 创建新迁移
```

### 构建

```bash
npm run build            # 构建前端生产版本
```

## 项目结构

```
ai-creative-studio/
├── backend/
│   ├── app.py           # Flask 应用入口
│   ├── config.py        # 配置管理
│   ├── extensions.py    # Flask 扩展
│   ├── models/          # 数据模型
│   ├── routes/          # API 路由
│   ├── services/        # 业务逻辑
│   ├── middleware/      # 中间件
│   ├── 
s/         # AI 提示词模板
│   └── utils/           # 工具函数
├── frontend/
│   ├── src/
│   │   ├── api/         # API 客户端
│   │   ├── components/  # 组件
│   │   ├── pages/       # 页面
│   │   └── store/       # 状态管理
│   └── ...
├── instance/            # SQLite 数据库 (开发)
├── uploads/             # 上传文件
├── docker-compose.yml   # Docker 服务配置
├── package.json         # npm scripts
├── pyproject.toml       # Python 项目配置
└── .env.example         # 环境变量模板
```

## 部署(单机 Docker)

整套应用都能用 Docker Compose 起在一台机器上：**nginx 前端 + Flask 后端(gunicorn) + PostgreSQL**。后端镜像内置 Docker CLI 客户端并挂载宿主机的 `/var/run/docker.sock`，因此 Code 域的 `code_frontend_project_generation` 工作流能从后端容器里 `docker run` 起一个一次性的 `fe-agent` 沙箱容器(Docker-out-of-Docker)。

### 前置条件

- 宿主机已装 Docker(含 Compose 插件)且 daemon 正在运行
- `/var/run/docker.sock` 可访问(后端容器以 root 运行，无需额外配 docker group)
- 仓库位于 `/data/workflow`：compose 里 `.fe-agent-work` 用**同一绝对路径**挂载来交接沙箱产物；换目录需同步改 `docker-compose.yml` 中的 `TMPDIR` 与该挂载

### 一键部署

```bash
make env       # 没有 .env 时从 .env.example 复制，然后填入下列 key
make deploy    # = docker compose --profile setup build + up -d
```

部署后访问 `http://<宿主机>/`(前端 nginx 监听 80，`/api` 反代到 `backend:5001`)。

`.env` 必填项：

| 变量 | 用途 |
|------|------|
| `ANTHROPIC_API_KEY` | 文本生成 + fe-agent CLI 都要 |
| `PANLAXY_API_KEY` | 图像生成 |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | compose 建库，并据此拼出后端容器内的 `DATABASE_URL`(自动指向 `postgres` 服务，无需手改) |
| `SECRET_KEY` / `JWT_SECRET_KEY` | 不设则开发模式自动生成(重启后已签发的 JWT 失效) |

### fe-agent 沙箱镜像

`fe-agent:latest` 是 `code_frontend_project_generation` 工作流要 `docker run` 的镜像，必须存在于宿主机镜像库。它在 compose 里是一个 `setup` profile 的 build-only 服务：`make build`(即 `docker compose --profile setup build`)会连它一起构建，单独重建用 `make fe-agent`。其余两个 Code 工作流(`code_full_generation`、`code_frontend_generation`)是后端进程内线程，**不需要**这个镜像。

### 常用运维命令(Makefile)

| 命令 | 作用 |
|------|------|
| `make deploy` | 构建全部镜像并后台启动(首次/完整部署) |
| `make build` / `make fe-agent` | 构建全部镜像(含沙箱)/ 仅构建 fe-agent 沙箱 |
| `make up` / `make down` / `make restart` | 启动 / 停止 / 重启 |
| `make rebuild` | down → 重建镜像 → up |
| `make logs S=backend` | 跟踪日志(`S=` 限定服务，缺省看全部) |
| `make ps` / `make config` | 查看状态 / 校验并渲染最终 compose 配置 |
| `make destroy` | ⚠️ down 并删除数据卷(**会清空数据库**) |

> 老机器若只有带连字符的 `docker-compose`：`make up COMPOSE="docker-compose"`。运行 `make` 或 `make help` 可列出全部命令。

## 许可证

MIT
