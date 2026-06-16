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

### 4. 启动数据库服务

```bash
docker-compose up -d
```

### 5. 初始化数据库

```bash
npm run db:migrate
```

### 6. 启动开发服务器

```bash
# 同时启动前后端
npm run dev

# 或分别启动
npm run dev:backend    # 后端 (localhost:5000)
npm run dev:frontend   # 前端 (localhost:5173)
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

## Docker 服务

项目使用 Docker Compose 管理基础服务：

- **PostgreSQL**: 端口 5432，用户 `ai_studio`
- **Redis**: 端口 6379

```bash
docker-compose up -d      # 启动服务
docker-compose down       # 停止服务
docker-compose logs -f    # 查看日志
```

## 许可证

MIT
