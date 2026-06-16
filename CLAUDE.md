# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 当前版本范围(重要 — 优先级最高)

**当前版本只对 Code 域生效。** 本仓库虽包含三个产品域,但本版本的开发、改动与验证**仅针对 Code 域**(`/api/code` + 共享的 `/api/agent` Agent Swarm + 其前端 `frontend .../code`),以及 Code 域依赖的共享底座(认证、团队、积分、AI provider 抽象层)。

- **PPT 与 RedBook(小红书)域在当前版本中不启用。** 下文中关于 PPT(`/api/ppt`、`backend/{models,routes,services}/ppt/*`、`pptStore`、`frontend .../ppt`)与 RedBook(`/api/redbook`、`backend/{models,routes,services}/redbook/*`、`redbookStore`、`frontend .../redbook`)的内容**仅作历史/架构参考**——不要在这两个域上新增或修改功能,除非用户明确点名要做 PPT 或 RedBook。
- 保留这些描述是为了说明既有约定和"镜像对称"的设计思路:实现 Code 域功能时**可以参考**它们的模式(后台任务、SSE、文件服务、模型约定等),但产物只落在 Code 域。
- 一句话:**默认只动 Code 域 + Agent Swarm + 共享底座(auth/team/credit/ai)。** 读到下文 PPT/RedBook 段落时,按"参考资料"对待,而非当前工作目标。

## 项目概述

AI Creative Studio 是一个 monorepo：后端为 Flask（Python），前端为 React/TypeScript（Vite），由根目录 `package.json` 的 npm 脚本统一编排。项目包含**三个平行的产品域**，它们共享认证、团队和积分体系：

- **PPT**（`/api/ppt`、`frontend .../ppt`）—— AI 生成幻灯片。流程为 大纲 → 每页描述 → 每页配图。 _（⚠️ 当前版本不启用，仅作参考，见上文「当前版本范围」）_
- **RedBook**（`/api/redbook`、`frontend .../redbook`）—— 小红书风格的社交媒体图文生成。 _（⚠️ 当前版本不启用，仅作参考）_
- **Code**（`/api/code` + 共享的 `/api/agent`、`frontend .../code`）—— 软件创作工作流：需求文档 → 开发流程 → 文档拆分 → 风格文档 → UI 预览 → **前端代码生成与预览**。它通过下文的 **Agent Swarm** 运行时执行，产出可回放的 `AgentRun`。 _（✅ 当前版本唯一在研域）_

这些域是刻意做成镜像对称的：各自在 `backend/models/`、`backend/routes/`、`backend/services/` 下有独立子包，前端也各有独立的 Zustand store 和页面。新增功能时，请参照*另一个*域的既有约定，而不要另起一套新做法。

## 常用命令

除特别说明外，所有命令都在仓库根目录运行。Python 用 `uv` 管理，前端用 `npm`。

```bash
npm run setup            # uv sync + 安装前端依赖
npm run dev              # 同时启动后端 + 前端 (npm-run-all --parallel)
npm run dev:backend      # Flask，地址 http://localhost:5001  (cd backend && uv run python -m backend.app)
npm run dev:frontend     # Vite，地址 http://localhost:3000，将 /api 代理到 :5001
npm run build            # 前端生产构建 (tsc -b && vite build)

npm run lint             # ruff（后端）+ eslint（前端）
npm run lint:backend     # uv run ruff check backend/
npm run lint:frontend    # cd frontend && eslint .

npm run test:backend     # uv run pytest
npm run db:migrate                 # cd backend && uv run alembic upgrade head
npm run db:revision -- "message"   # 自动生成迁移
```

运行单个后端测试：`uv run pytest path/to/test_x.py::test_name -v`。

**信任这些脚本前需要知道的坑：**
- README 写后端跑在 5000 端口；实际跑在 **5001**（见 `backend/app.py` 以及 Vite 代理目标）。请用 5001。
- 前端 `package.json` **没有 `test` 脚本**，因此 `npm run test` / `npm run test:frontend` 目前会失败。也还没有 `tests/` 目录——pytest 无可收集的用例。把测试工具链视为"已配置但未启用"。
- 数据库表结构在启动时由 `backend/app.py` 里的 `db.create_all()` 创建，所以开发环境下改动模型重启即生效。**Alembic 仅是声明的依赖——项目里完全没有 Alembic 配置**（没有 `alembic.ini`、没有 `env.py`、没有 migrations 目录），因此 `npm run db:migrate` / `db:revision` 当前会直接失败，也没有任何迁移历史。在真正搭好迁移之前，把表结构视为由 `create_all()` 驱动。

## 后端架构

**应用工厂**（`backend/app.py`）：`create_app(config_name)` 通过把 `FLASK_ENV` 首字母大写来选择配置类（`development` → `backend/config.py` 中的 `DevelopmentConfig`）。开发环境在未设置时会自动生成 `SECRET_KEY`/`JWT_SECRET_KEY`，并默认使用 SQLite；**生产环境会校验 `SECRET_KEY`、`JWT_SECRET_KEY`、`DATABASE_URL` 三者都已设置**，否则启动即抛错。蓝图（blueprint）连同各自的 `url_prefix` 都在这里注册——这是 API 面的权威映射表。

**扩展**（`backend/extensions.py`）：`db`（Flask-SQLAlchemy）和 `jwt` 定义在这里，与 app 分离以避免循环导入。请从 `backend.extensions` 导入 `db`，绝不要新建一个。

**AI provider 抽象层**（`backend/services/ai/`）：所有模型调用的唯一接入点。
- `base.py` —— `AIProvider` 抽象基类，`generate_text()` / `generate_image()` 返回 `TextGenerationResult` / `ImageGenerationResult` 数据类（携带 `success`/`error`，模型失败时不抛异常）。
- `factory.py` —— `get_text_provider()` / `get_image_provider()` 返回线程安全的、带缓存的单例，由环境变量配置（`AI_PROVIDER`、`AI_API_KEY`/`GOOGLE_API_KEY`/`GEMINI_API_KEY`、`AI_TEXT_MODEL`、`AI_IMAGE_MODEL`、`AI_BASE_URL`）。目前只实现了 Gemini；未知 provider 会回退到 Gemini。
- **在后台线程中，调用 factory 时要传 `force_new=True`**，以获得一个全新的 provider 实例而不是共用缓存的那个（参见 task manager 中的写法）。

**后台任务——两种不同模式，按场景选用：**
- **PPT 用 ThreadPoolExecutor**，而非 Celery（`backend/services/ppt/task_manager.py`）。`ppt_task_manager.submit_task(task_id, fn, app, ...)` 在工作线程上运行 `fn`。任务函数接收 Flask `app` 并把主体包在 `with app.app_context():` 中。进度/状态通过 `mark_task_processing/completed/failed` + `update_task_progress` 等辅助函数持久化到 `PPTTask` 模型上；客户端轮询任务状态。图片生成是**逐页串行**执行的，以遵守 Gemini 的速率限制。
- **RedBook 用 SSE 流式输出**（`backend/routes/redbook/*`、`backend/services/redbook/*`）。服务方法是 Python 生成器，逐个 yield `{"event", "data"}` 字典，再通过 `stream_with_context` 以 `text/event-stream` 返回给客户端。事件类型包括 `image`、`content`、`error`、`complete`。

**Agent Swarm（`backend/services/agent/`，路由 `/api/agent`）—— Code 域的执行与回放底座：** `runtime.py` 是进程级 ThreadPoolExecutor，把注册的 workflow（见 `workflows/code_workflow.py`，目前 7 步：规划→需求→开发流程→文档拆分→风格→预览→发布）跑在后台；`recorder.py` 把每步的 prompt/响应、事件（带单调 `sequence`）、产物写入 `AgentRun/AgentStep/AgentEvent/AgentArtifact`；`bus.py` 是 SSE 事件总线。客户端经 `GET /api/agent/runs/<id>/stream` 先重放已存事件再接实时推送——**已结束的 run 可被完整逐步回放**（流式 token delta 不持久化，回放时呈现各步完整响应）。PPT/RedBook 目前**不**走这套运行时。

**积分**（`backend/services/credit_service.py`）：消耗积分的操作必须走 `deduct_credits()`，它执行**单条原子 SQL `UPDATE ... WHERE balance >= amount`**，并把 `rowcount == 0` 视为余额不足（抛 `InsufficientCreditsError`）——这是防竞态的关键，不要换成"先读后写"。余额是按用户*或*按团队的：传入 `team_id` 则改为从团队余额扣减。每次变动都会写一条 `CreditTransaction` 审计记录。

**所有消耗 AI 的操作都必须计费**，单价统一定义在 `backend/services/pricing.py`（每项可被 `PRICE_*` 环境变量覆盖）——不要把成本硬编码散落在各处。路由/任务里用两个封装：`charge()`（预检 + 原子扣费，余额不足返回 `False` 而非抛异常，便于循环里优雅停止）与 `refund_credits()`（失败退款，走 `add_credits(transaction_type="refund")`）。计费时机：**PPT** 大纲在请求线程同步扣费、描述/配图在 ThreadPool 里**逐成功扣费**（部分失败只扣已交付的页）；**RedBook** 大纲/文案同步扣费、图片在 SSE 生成器里逐成功扣费；**Code** 工作流在 `agent_routes` 预扣，仅在产出核心产物前失败时由 `runtime` 自动退款。`GET /api/credits/balance` 支持 `?team_id=`（带成员校验）查询团队余额；前端各生成完成回调里调 `creditStore.refreshBalance()` 刷新展示。

**统一 API 响应**：接口返回 `{"success": true, "data": ..., "message": ...}` 或 `{"success": false, "error": "CODE", "message": ...}`。权威辅助函数在 `backend/utils/response.py`（`success_response`/`error_response`）——**所有域都应导入共享版本，不要再本地重复定义**（PPT 各路由此前的本地副本已移除）。错误码统一词汇：`VALIDATION_ERROR`(400)、`NOT_FOUND`(404)、`FORBIDDEN`(403)、`INSUFFICIENT_CREDITS`(402)、`SERVER_ERROR`(500) 等。RedBook 的错误**消息**仍是中文，放在 `message` 字段（`error` 字段是机器码），前端按 `message` 展示；RedBook 的列表/统计接口（`/tasks`、`/tasks/stats`）刻意返回**未包裹**的原始 dict，前端直接读取，不要硬塞进 `success_response`。`app.py` 里的全局错误处理器已经会把 HTTP 异常和未捕获错误归一成这种结构，并在非 debug 模式下隐藏内部细节。

**模型**：主键为字符串 UUID（`db.String(36)`），带 `user_id` + 可空的 `team_id` 以支持多租户，并有一个取值为 `private|team|public` 的 `visibility` 字段。结构化内容（大纲、描述、进度）以 **JSON 形式存在 Text 列**里，通过模型上的 getter/setter 辅助方法访问（`get_outline_content()`、`set_description_content()`、`get_progress()` 等）——请走这些方法，不要直接读原始列。PPT 每页配图通过 `PPTPageImageVersion` 做版本管理（`version_number` 用 `MAX(...)` 查询计算，每页只有一条 `is_current=True`）。

**认证**：Flask-JWT-Extended，access token 30 分钟、refresh token 30 天。用 `@jwt_required()` 保护接口，用 `get_jwt_identity()` 读取当前用户。

**Prompt（提示词）**：PPT 的 prompt 构造器是从 `backend.services.ppt.prompts` 导入的函数；RedBook 与 Code 的 prompt 是 `backend/prompts/{redbook,code}/` 下的 `.txt` 模板（Code 的 split/refine/style 模板还会用 `prompt_library.compose_recipe_prompt(...)` 拼一个 `{system_prefix}`），在服务初始化/调用时加载。增删改 prompt 请在这些位置进行，不要内联写进业务逻辑。**注意 Code 模板含 JSON 示例，里面的花括号要写成 `{{ }}` 转义**，否则 `.format()` 会报错。

## 前端架构

React 19 + TypeScript + Vite。`@/` 是 `frontend/src/` 的别名。

- **状态管理**：`src/stores/` 下每个关注点一个 Zustand store（`authStore`、`teamStore`、`creditStore`、`pptStore`、`redbookStore`、`codeStore`、`agentStore`、`exportTasksStore`）。服务端缓存可用 TanStack Query。
- **API 层**：`src/api/client.ts` 是 axios 实例。它会注入 bearer token，并在收到 401 时**透明刷新 token**——并发的 401 会排队等待同一次刷新调用，刷新失败则清除 token 并跳转 `/login`。请使用 `api.get/post/put/patch/delete` 辅助方法（它们会自动解包 `res.data`），而不要直接调用 axios。
- **路由**：`src/App.tsx` 中所有应用路由都包在 `<ProtectedRoute>` 内；`/login` 和 `/register` 是仅有的公开路由。`AuthInitializer` 在加载时引导初始化认证状态。三个域各有入口（`/code`、`/ppt`、`/redbook`）、详情（`/code/:projectId`、`/ppt/project/:projectId`、`/redbook/task/:id`）与历史页。左侧 `components/layout/Sidebar.tsx` 是统一的**会话侧边栏**（域标签切换 + 当前域历史会话列表 + 新建会话）——"会话"即各域的 project/task 本身，点击切换、可深链；Code 域可借 `agentStore.openLatestRunForResource()` 调出时间线做过程回放。
- **UI**：shadcn 风格组件（Radix 原语 + Tailwind v4）在 `src/components/ui/`；业务组件在 `src/components/ppt/` 等目录。
- **国际化（i18n）**：i18next，按命名空间组织的 JSON 位于 `src/locales/{en,ja,ko,zh-CN}/`（`common`、`auth`、`ppt`、`redbook`、`code`、`agent`、`team`、`errors` 等）。新增 key 要同时加到**四种语言**、放进正确的命名空间；不要硬编码面向用户的文案。

## 值得遵循的约定

- 代码注释：**PPT/核心模块用英文，RedBook 模块用中文**——与所在文件保持一致。
- AI/模型失败以结果对象返回（`success=False`），而基础设施/数据库错误则抛异常——保持这种区分。
- 改动积分、任务、图片版本相关逻辑时，请保留上文那些原子/线程安全的写法；它们正是为了在 ThreadPoolExecutor 和并发请求下避免竞态而存在的。
