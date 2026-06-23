# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

claude code only use chinese respone question.

## 当前版本范围(重要 — 优先级最高)

**当前版本只对 Code 域生效。** 本仓库虽包含三个产品域,但本版本的开发、改动与验证**仅针对 Code 域**(`/api/code` + 共享的 `/api/agent` Agent Swarm + 其前端 `frontend .../code`),以及 Code 域依赖的共享底座(认证、团队、积分、AI provider 抽象层)。

- **PPT 与 RedBook(小红书)域在当前版本中不启用。** 下文中关于 PPT(`/api/ppt`、`backend/{models,routes,services}/ppt/*`、`pptStore`、`frontend .../ppt`)与 RedBook(`/api/redbook`、`backend/{models,routes,services}/redbook/*`、`redbookStore`、`frontend .../redbook`)的内容**仅作历史/架构参考**——不要在这两个域上新增或修改功能,除非用户明确点名要做 PPT 或 RedBook。
- 保留这些描述是为了说明既有约定和"镜像对称"的设计思路:实现 Code 域功能时**可以参考**它们的模式(后台任务、SSE、文件服务、模型约定等),但产物只落在 Code 域。
- 一句话:**默认只动 Code 域 + Agent Swarm + 共享底座(auth/team/credit/ai)。** 读到下文 PPT/RedBook 段落时,按"参考资料"对待,而非当前工作目标。

> 仓库里还有一份 `AGENTS.md`(给 Codex 用)和 `README.md`——两者都已落后于当前代码(`AGENTS.md` 只描述了 PPT/RedBook 两个域、没有 Code/Agent Swarm,`README.md` 写的端口/默认 provider 也不准)。**以本文件为准。**

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
npm run dev:backend      # Flask，地址 http://localhost:5001  (cd backend && source venv/bin/activate && uv run python -m backend.app)
npm run dev:frontend     # Vite，地址 http://localhost:3000，将 /api 代理到 :5001
npm run build            # 前端生产构建 (tsc -b && vite build)

npm run lint             # ruff（后端）+ eslint（前端）
npm run lint:backend     # uv run ruff check backend/   (line-length 100, 规则 E/F/W/I, 忽略 E501)
npm run lint:frontend    # cd frontend && eslint .

npm run test:backend     # uv run pytest（见下方测试说明）
```

**测试（已启用——这是相对旧文档最大的变化）：** `tests/` 目录现在真实存在并可被 pytest 收集（`pyproject.toml` 配置 `testpaths=["tests"]`、`python_files=["test_*.py"]`）。测试分两类：

- **单元测试**（无网络，mock/fake provider）：`test_ai_factory.py`(capability 路由)、`test_ai_providers_unit.py`(媒体类型/base64/Panlaxy 配置归一化)、`test_code_json_parsing.py`(模型输出 JSON 解析的健壮性回归)、`test_prompt_library.py`(prompt 组装/角色路由/system 前缀顺序)。
- **集成测试**（打真实 AI API）：`test_ai_integration.py`,带 `@pytest.mark.integration` 标记,会调用**线上 Claude + Panlaxy**。需要 `.env` 里有 `ANTHROPIC_API_KEY` / `PANLAXY_API_KEY` 且有网络,否则会优雅跳过。

```bash
uv run pytest -m "not integration"                  # 只跑单元测试（CI/日常默认,无需 API key）
uv run pytest -m integration -s                     # 只跑集成测试（需 API key + 网络,会产生真实费用）
uv run pytest path/to/test_x.py::test_name -v       # 跑单个测试
```

`tests/conftest.py` 有一个 autouse fixture,在每个用例前后重置 `factory.py` 的 provider 单例缓存（`reset_providers()`），保证改环境变量后能立即生效。

**信任这些脚本前需要知道的坑：**
- README 写后端跑在 5000 端口；实际跑在 **5001**（见 `backend/app.py` 末尾以及 Vite 代理目标）。请用 5001。
- **前端没有 `test` 脚本、也没装 vitest**（`frontend/package.json` 只有 `dev`/`build`/`lint`/`preview`）。因此根目录的 `npm run test:frontend` 与 `npm run test`(它会去跑 `cd frontend && npm test`)**目前会失败**——README 关于 vitest 的说法是错的。前端测试视为"未配置"。
- 数据库表结构在启动时由 `backend/app.py` 里的 `db.create_all()` 创建，所以开发环境下改动模型重启即生效。**Alembic 仅是声明的依赖——项目里完全没有 Alembic 配置**（没有 `alembic.ini`、没有 `env.py`、没有 migrations 目录），因此 `npm run db:migrate` / `db:revision` 当前会直接失败，也没有任何迁移历史。在真正搭好迁移之前，把表结构视为由 `create_all()` 驱动。

## 后端架构

**应用工厂**（`backend/app.py`）：`create_app(config_name)` 通过把 `FLASK_ENV` 首字母大写来选择配置类（`development` → `backend/config.py` 中的 `DevelopmentConfig`；另有 `Production`/`Testing`）。开发环境在未设置时会自动生成 `SECRET_KEY`/`JWT_SECRET_KEY`，并默认使用 SQLite；**生产环境会校验 `SECRET_KEY`、`JWT_SECRET_KEY`、`DATABASE_URL` 三者都已设置**，否则启动即抛错。蓝图（blueprint）连同各自的 `url_prefix` 都在这里注册——这是 API 面的权威映射表（`/api/auth`、`/api/users`、`/api/teams`、`/api/credits`、`/api/agent`、`/api/code`，以及 PPT/RedBook 的一组）。

**扩展**（`backend/extensions.py`）：`db`（Flask-SQLAlchemy）和 `jwt` 定义在这里，与 app 分离以避免循环导入。请从 `backend.extensions` 导入 `db`，绝不要新建一个。

**AI provider 抽象层**（`backend/services/ai/`）：所有模型调用的唯一接入点，现在是**按能力（capability）路由的多 provider 体系**——文本生成与图像生成各自独立配置，可以走不同的 API。
- `base.py` —— `AIProvider` 抽象基类，`generate_text()` / `generate_image()` 返回 `TextGenerationResult` / `ImageGenerationResult` 数据类（携带 `success`/`error`，**模型失败时返回 `success=False` 而不抛异常**）。还有一个流式接口 `generate_text_stream()`（逐 token yield，出错时抛异常由调用方兜底）。
- `factory.py` —— 路由与缓存的核心。`get_text_provider()` 永远按**文本配置**构建 provider，`get_image_provider()` 永远按**图像配置**构建；两者都是线程安全的带锁缓存单例。
  - **在后台线程中调用 factory 要传 `force_new=True`**，拿一个全新实例而不是共享缓存的那个（参见 task manager / runtime 的写法）。
  - factory 直接读 `os.getenv`(不依赖 Flask app config),因此在任意线程/上下文都能工作。改完配置可用 `reset_providers()` 清缓存（测试 fixture 就是这么做的）。
  - **未知 provider 会回退到 Gemini。** 各 provider 都实现了两个方法,但对不支持的能力会返回 `success=False`(如 Claude 的 `generate_image()`、Panlaxy 的 `generate_text()`)。
- 三个已实现的 provider（能力矩阵）：

  | provider | 文本 | 图像 | SDK | 默认模型 |
  |----------|------|------|-----|----------|
  | `claude` (`claude.py`)   | ✅（流式 + thinking） | ❌ | `anthropic` | `claude-opus-4-8`（`max_tokens` 默认 16000） |
  | `gemini` (`gemini.py`)   | ✅ | ✅ | `google-genai`（新版 SDK，非 `google-generativeai`） | `gemini-3-flash-preview` / `gemini-3.1-flash-image`（原生图像模型；勿用 imagen-*，其走不同的 predict API） |
  | `openai` (`openai_image.py`) | ❌ | ✅（`images.generate` / `images.edit`，真正的 OpenAI/ChatGPT API） | `openai`（`PanlaxyProvider` 子类，指向 `api.openai.com`） | `gpt-image-2`（实测可用；另有 gpt-image-1 / 1.5） |
  | `panlaxy` (`panlaxy.py`) | ❌ | ✅（`images.generate` / `images.edit`，OpenAI 兼容） | `openai`（指向 Panlaxy base url） | `gpt-image-2` |

- **默认接线（见 `.env.example`）**：文本 → Claude，图像 → **OpenAI（`gpt-image-2`）**，`AI_PROVIDER=gemini` 作为两者的回退。Gemini 原生图像、Panlaxy 仅作可选备选。
- 环境变量（capability 优先，未设则回退到 provider 专属 key）：
  - 通用：`AI_PROVIDER`(回退)、`AI_API_KEY`、`AI_BASE_URL`。
  - 文本：`AI_TEXT_PROVIDER`、`AI_TEXT_MODEL`、`AI_TEXT_API_KEY`、`AI_TEXT_BASE_URL`、`AI_TEXT_MAX_TOKENS`；Claude 专属 `ANTHROPIC_API_KEY`/`CLAUDE_API_KEY`(可加 `ANTHROPIC_BASE_URL`)。
  - 图像：`AI_IMAGE_PROVIDER`、`AI_IMAGE_MODEL`、`AI_IMAGE_API_KEY`、`AI_IMAGE_BASE_URL`；OpenAI 专属 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_IMAGE_MODEL`、`OPENAI_IMAGE_QUALITY`、`OPENAI_IMAGE_SIZE`、`OPENAI_TIMEOUT`、`OPENAI_MAX_RETRIES`；Panlaxy 专属 `PANLAXY_API_KEY`、`PANLAXY_BASE_URL`、`PANLAXY_IMAGE_MODEL`、`PANLAXY_IMAGE_QUALITY`、`PANLAXY_IMAGE_SIZE`、`PANLAXY_TIMEOUT`、`PANLAXY_MAX_RETRIES`。

**后台任务——多种模式，按场景选用：**
- **Agent Swarm（Code 域）用进程级 ThreadPoolExecutor**，见下一节。这是当前版本的重点。
- **PPT 用 ThreadPoolExecutor**，而非 Celery（`backend/services/ppt/task_manager.py`）。`ppt_task_manager.submit_task(task_id, fn, app, ...)` 在工作线程上运行 `fn`。任务函数接收 Flask `app` 并把主体包在 `with app.app_context():` 中。进度/状态通过 `mark_task_processing/completed/failed` + `update_task_progress` 等辅助函数持久化到 `PPTTask` 模型上；客户端轮询任务状态。图片生成是**逐页串行**执行的，以遵守速率限制。 _（参考资料，当前版本不动）_
- **RedBook 用 SSE 流式输出**（`backend/routes/redbook/*`、`backend/services/redbook/*`）。服务方法是 Python 生成器，逐个 yield `{"event", "data"}` 字典，再通过 `stream_with_context` 以 `text/event-stream` 返回给客户端。事件类型包括 `image`、`content`、`error`、`complete`。 _（参考资料，当前版本不动）_

**Agent Swarm（`backend/services/agent/`，路由 `/api/agent`）—— Code 域的执行与回放底座：**
- `runtime.py` —— 进程级 `ThreadPoolExecutor(max_workers=4)`(模块级单例 `agent_runtime`)。workflow 用 `register_workflow(key, fn)` 注册进 `_WORKFLOWS` 字典、用 `get_workflow(key)` 取出；`agent_runtime.start(app, run_id)` 把一次 run 提交到线程池，在 `app.app_context()` 里执行。
- **`runtime.py` 模块级 `_register_builtin_workflows()` 注册 5 个独立 workflow**（彼此是独立的 run，不是某个的子步；`agent_routes` 的 `WORKFLOW_COSTS` 既是计费表也是白名单，`create_run` 用 `get_workflow` 校验）：
  - **`code_full_generation`**（`workflows/code_workflow.py`，`run_code_workflow`）——**7 步**：`planner`(规划) → `requirements`(需求) → `flow`(开发流程) → `documents`(文档拆分) → `style`(风格) → `preview`(UI 预览) → `publisher`(发布)。
  - **`code_frontend_generation`**（`workflows/code_frontend_workflow.py`，`run_code_frontend_workflow`）——前端代码生成：`fe_planner` → `fe_build` → `fe_critic` →（`fe_repair`，仅 critic 不通过时执行、不计入进度）→ `fe_publish`。它要求已有一个完成的 Code 项目（按 `resource_id` 复用上一段 full-generation run 的 ledger），产出 **Sandpack 可直接预览的文件 map**（React + TS + 纯 CSS）。
  - **`code_frontend_project_generation`**（`workflows/code_frontend_project_workflow.py`）、**`code_canvas_generation`**（`workflows/code_canvas_workflow.py`）——各为独立 run。前者同样走 Docker-out-of-Docker（`fe-agent` 容器）。
  - **`code_figma_slice_generation`**（`workflows/code_figma_slice_workflow.py`）——把一张 UI 预览缩略图重建成**可在 Figma 逐元素编辑的 Design IR**：`fe_slice_planner` → `fe_slice_analyze` → `fe_slice_publish`。**分析步在 docker `slicer-agent` 容器里跑 OpenAI Codex CLI**（Docker-out-of-Docker：后端容器挂宿主 `/var/run/docker.sock`、`TMPDIR` 两侧同路径供 `-v {workdir}:/out` bind mount；镜像用 `docker compose --profile setup build` 预构建）。**任何失败（认证/超时/无产物）都静默降级成单图 IR**（整图、run 仍 `completed`，表象像"切片没生效"），排查看 `TMPDIR/slicer-agent-*/` 下的 `codex_stderr.log`/`codex_exit`/`degraded`。Codex 调用契约见 `figma_slice_service.py::_CONTAINER_SCRIPT` 注释（须先 `codex login --with-api-key`，env 里的 `OPENAI_API_KEY` 不会被自动使用；prompt 走 **stdin** 而非位置参数，因 `-i/--image` 是 variadic 会吞掉位置参；Codex 是 reasoning 模型耗时数分钟，`CODEX_TIMEOUT` 默认 300 偏紧，生产已在 compose 设 900）。
- `recorder.py` 把每步的 prompt/响应、事件（带单调 `sequence`）、产物写入 `AgentRun/AgentStep/AgentEvent/AgentArtifact`（模型在 `backend/models/agent/{run,step,event,artifact}.py`）；`bus.py` 是 SSE 事件总线。客户端经 `GET /api/agent/runs/<id>/stream` 先重放已存事件再接实时推送——**已结束的 run 可被完整逐步回放**（流式 token delta 不持久化，回放时呈现各步完整响应）。
- 主要 HTTP 端点：`POST /api/agent/runs`(创建并启动一次 run、预扣积分、校验 domain/workflow)、`GET /api/agent/runs`(列当前用户的 run，可按 domain/resource_id 过滤)、`GET /api/agent/runs/<id>`(run+steps+events+artifacts 快照)、`GET /api/agent/runs/<id>/stream`(SSE)、`POST /api/agent/runs/<id>/cancel`(协作式取消)、`GET /api/agent/artifacts/<id>/file`(下载产物，仅 owner)。

**会话上下文账本（Session Context Ledger）—— 防口径漂移：** 每个 run 携带一份结构化的"共识口径"(技术栈/术语/关键决策/范围)，由 `context_ledger.py` 维护、`context_verifier.py` 校验。
- 在 `planner` 步 `seed_from_inputs(...)` 播种；之后每个下游步用 `render_for_prompt()` 注入到该步 prompt（模板里的 `{context_ledger}` / `[[CONTEXT_LEDGER]]` 占位符），并在 `requirements/flow/documents/style` 步用 `merge(...)` 增补，每步后 `persist_ledger()` 落库。
- `context_verifier.py` 在每个产文本的步做**确定性检查**（输出非空、文档类型覆盖、必填字段、前端技术栈一致性）；只在**高风险边界**（`documents` 与 `fe_build`）额外跑一次 **AI 一致性闸**——这一步**按 `pricing.CODE_CONTEXT_VERIFY` 计费**，provider 未配置时返回 `None`、出错时 fail-open 并标记 `degraded`。结果以 `CONTEXT_UPDATED` / `CONTEXT_CONFLICT` 事件发出。
- 账本**仅内部/调试可见，不进入用户产出**。详见 `docs/agent-context-ledger.md`。
- `code_frontend_generation` 在 `fe_planner` 步会**重新加载**上一段 full-generation run 的 ledger,以保证前后两段口径一致。

**积分**（`backend/services/credit_service.py`）：消耗积分的操作必须走 `deduct_credits()`，它执行**单条原子 SQL `UPDATE ... WHERE balance >= amount`**，并把 `rowcount == 0` 视为余额不足（抛 `InsufficientCreditsError`）——这是防竞态的关键，不要换成"先读后写"。余额是按用户*或*按团队的：传入 `team_id` 则改为从团队余额扣减。每次变动都会写一条 `CreditTransaction` 审计记录。

**所有消耗 AI 的操作都必须计费**，单价统一定义在 `backend/services/pricing.py`（每项可被 `PRICE_*` 环境变量覆盖，常量含 `CODE_FULL_GENERATION`、`CODE_FULL_GENERATION_TOTAL`、`CODE_FRONTEND_GENERATION`、`CODE_CONTEXT_VERIFY` 等）——不要把成本硬编码散落在各处。路由/任务里用两个封装：`charge()`（预检 + 原子扣费，余额不足返回 `False` 而非抛异常，便于循环里优雅停止）与 `refund_credits()`（失败退款，走 `add_credits(transaction_type="refund")`）。计费时机：
- **Code**：在 `agent_routes` 创建 run 时按 workflow 总价**预扣**（`code_full_generation` → `CODE_FULL_GENERATION_TOTAL`，`code_frontend_generation` → `CODE_FRONTEND_GENERATION`），过程中的上下文一致性闸按 `CODE_CONTEXT_VERIFY` **逐次加扣**；仅当 run 失败且**未产出任何核心产物**时由 `runtime` 自动退款。
- **PPT**：大纲在请求线程同步扣费、描述/配图在 ThreadPool 里**逐成功扣费**（部分失败只扣已交付的页）。 _（参考）_
- **RedBook**：大纲/文案同步扣费、图片在 SSE 生成器里逐成功扣费。 _（参考）_
- `GET /api/credits/balance` 支持 `?team_id=`（带成员校验）查询团队余额；前端各生成完成回调里调 `creditStore.refreshBalance()` 刷新展示。

**统一 API 响应**：接口返回 `{"success": true, "data": ..., "message": ...}` 或 `{"success": false, "error": "CODE", "message": ...}`。权威辅助函数在 `backend/utils/response.py`（`success_response`/`error_response`）——**所有域都应导入共享版本，不要再本地重复定义**。错误码统一词汇：`VALIDATION_ERROR`(400)、`NOT_FOUND`(404)、`FORBIDDEN`(403)、`INSUFFICIENT_CREDITS`(402)、`SERVER_ERROR`(500) 等。RedBook 的错误**消息**仍是中文，放在 `message` 字段（`error` 字段是机器码），前端按 `message` 展示；RedBook 的列表/统计接口（`/tasks`、`/tasks/stats`）刻意返回**未包裹**的原始 dict。`app.py` 里的全局错误处理器已经会把 HTTP 异常和未捕获错误归一成这种结构，并在非 debug 模式下隐藏内部细节。

**模型**：主键为字符串 UUID（`db.String(36)`），带 `user_id` + 可空的 `team_id` 以支持多租户，并有一个取值为 `private|team|public` 的 `visibility` 字段。结构化内容（大纲、描述、进度、上下文账本）以 **JSON 形式存在 Text 列**里，通过模型上的 getter/setter 辅助方法访问（`get_outline_content()`、`set_description_content()`、`get_progress()`、`get/set_context_ledger()` 等）——请走这些方法，不要直接读原始列。各域模型分布：Code 在 `models/code/project.py`(`CodeProject`)、Agent Swarm 在 `models/agent/*`(`AgentRun/AgentStep/AgentEvent/AgentArtifact`)、PPT/RedBook 各自子包。PPT 每页配图通过 `PPTPageImageVersion` 做版本管理（`version_number` 用 `MAX(...)` 查询计算，每页只有一条 `is_current=True`）。

**认证**：Flask-JWT-Extended，access token 30 分钟、refresh token 30 天。用 `@jwt_required()` 保护接口，用 `get_jwt_identity()` 读取当前用户。

**Prompt（提示词）**：
- **Code** 的 prompt 是 `backend/prompts/code/` 下的 `.txt` 模板：`requirements_prompt`、`development_flow_prompt`、`document_split_prompt`、`style_prompt`、`frontend_build_prompt`、`frontend_critic_prompt`、`frontend_repair_prompt`、`figma_slice_prompt`（切片分析）。其中文档拆分/风格等模板会用 `backend/services/prompt_library/`（`internet_roles.py` 的 `compose_recipe_prompt(...)` / `compose_system_prompt(...)`）拼出一个 `{system_prefix}`。
- **RedBook** 的 prompt 是 `backend/prompts/redbook/` 下的 `.txt` 模板；**PPT** 的 prompt 构造器是从 `backend.services.ppt.prompts` 导入的函数。
- 增删改 prompt 请在这些位置进行，不要内联写进业务逻辑。**走 `.format()` 的模板（多数含 JSON 示例的）里花括号要写成 `{{ }}` 转义**，否则报错（`test_code_json_parsing.py` 覆盖了 JSON 解析健壮性）；而走 `_fill` / `[[KEY]]` 占位符替换的模板（如 `figma_slice_prompt`，由 `figma_slice_service._fill` 处理）则**不要**转义花括号。
- **⚠️ 运行时 prompt 由 `backend/services/prompts/`（`prompt_store.get(key)`，key 形如 `code/<file>.txt`）从 MongoDB `prompts` collection 读取——上面的 `.txt` / `internet_roles` 只是 `defaults.py` 的 seed 与 fallback 源。改了模板文件「不会自动生效」**：`seed_defaults()` 只插入缺失的 key、**从不覆盖已存文档**（为保护 admin 后台编辑），所以已 seed 过的 prompt 会一直用 Mongo 里的旧版本，重建镜像也没用。让改动生效要：① 改 `.txt`；② 更新 Mongo 对应 `_id` 文档的 `content`（`is_overridden=True` 表示被 admin 改过，**勿擅自覆盖、先问**；`False` 时可安全同步成新 default）；③ 重启后端清 `prompt_store._cache`（带 TTL）。仅当 Mongo 不可达时才整体走文件 fallback。

## 前端架构

React 19 + TypeScript + Vite。`@/` 是 `frontend/src/` 的别名。

- **状态管理**：`src/stores/` 下每个关注点一个 Zustand store（`authStore`、`teamStore`、`creditStore`、`pptStore`、`redbookStore`、`codeStore`、`agentStore`、`exportTasksStore`）。服务端缓存可用 TanStack Query。
- **API 层**：`src/api/client.ts` 是 axios 实例。它会注入 bearer token，并在收到 401 时**透明刷新 token**——并发的 401 会排队等待同一次刷新调用，刷新失败则清除 token 并跳转 `/login`。请使用 `api.get/post/put/patch/delete` 辅助方法（它们会自动解包 `res.data`），而不要直接调用 axios。
- **路由**：`src/App.tsx` 中所有应用路由都包在 `<ProtectedRoute>` 内；`/login` 和 `/register` 是仅有的公开路由。`AuthInitializer` 在加载时引导初始化认证状态。三个域各有入口（`/code`、`/ppt`、`/redbook`）、详情（`/code/:projectId`、`/ppt/project/:projectId`、`/redbook/task/:id`）与历史页。左侧 `components/layout/Sidebar.tsx` 是统一的**会话侧边栏**（域标签切换 + 当前域历史会话列表 + 新建会话）——"会话"即各域的 project/task 本身，点击切换、可深链；Code 域可借 `agentStore.openLatestRunForResource()` 调出时间线做过程回放。
- **UI**：shadcn 风格组件（Radix 原语 + Tailwind v4）在 `src/components/ui/`；业务组件分目录在 `src/components/{code,agent,ppt,common,layout}/`。
- **国际化（i18n）**：i18next，按命名空间组织的 JSON 位于 `src/locales/{en,ja,ko,zh-CN}/`（`common`、`auth`、`ppt`、`redbook`、`code`、`agent`、`team`、`errors` 等）。新增 key 要同时加到**四种语言**、放进正确的命名空间；不要硬编码面向用户的文案。

## 插件（`plugin/`）

仓库里所有**独立插件**——在外部宿主（如 Figma 桌面端）里运行、独立构建、不属于 `backend`/`frontend` 主应用——都放在顶层 `plugin/<name>/` 下，**一个子目录一个插件**，各自带独立的 `package.json` / 构建脚本 / `README.md` / `.gitignore`。

- **约定**：新增插件时新建 `plugin/<name>/` 目录，不要散落在根目录、也不要塞进 `frontend`。插件**不参与**根目录 `npm run dev`/`npm run build`/`npm run lint` 的编排（根 `package.json` 不引用 `plugin/*`），各插件在自己目录里用自己的脚本构建。插件与平台的耦合只通过 **HTTP API**（`/api/...`）和**共享数据契约**完成，不直接 import 后端/前端代码。

**当前插件：`plugin/figma`（AI Creative Studio Importer）。** 一个 Figma 桌面插件，把平台导出的设计（Code 域 UI 预览 / 生成的前端）在 Figma 里重建为原生图层。

- **构成**：`manifest.json`（插件清单，`main=code.js`、`ui=ui.html`）、`src/code.ts`（主线程，重建图层）、`src/ir.ts`（Design IR 的插件侧镜像）、`ui.html`（插件 UI）。用 **esbuild** 把 `src/code.ts` 打包成 `code.js`（`code.js` 与 `node_modules` 都在 `.gitignore` 内，需本地构建）。
- **构建 / 加载**：`cd plugin/figma && npm install && npm run build`（`npm run watch` 监听、`npm run typecheck` 跑 `tsc --noEmit`）；然后在 Figma 桌面端 **Plugins → Development → Import plugin from manifest…** 选择 `plugin/figma/manifest.json`，改完 `src/` 后重新 `build`。
- **调用流程（运行时）**：① 平台里打开项目点 **导出到 Figma**，拿到 8 位**一次性配对码**（5 分钟有效）；② 在 Figma 运行本插件，填入后端 URL + 配对码；③ 插件 UI 从**免鉴权**的 `GET /api/code/figma/pull?code=…` 拉取导出包（**配对码即凭证**），解码内联图片，主线程把 Design IR 重建为 frames / rectangles / text / image fills。后端侧的导出/拉取/导入逻辑在 `backend/routes/code/figma_routes.py` 与 `backend/services/code/figma/*`。
- **Design IR 契约**：双向桥接的中间表示，权威定义在 `backend/services/code/figma/ir.py`，插件侧镜像 `plugin/figma/src/ir.ts`，规范见 `docs/figma-ir-spec.md`——三者要保持一致。
- **生产注意**：`manifest.json` 的 `networkAccess.allowedDomains` 开发期是 `"*"`，发布前必须收紧到确切的后端域名（如 `["https://studio.example.com"]`）。

> 注：Figma 在产品里是**收进 Code 域 UI 生成阶段**的能力（导出仅在 UI 完成后可用）；这里的 `plugin/figma` 只是其中跑在 Figma 里的那一端。

## 值得遵循的约定

- 代码注释：**PPT/核心模块用英文，RedBook 模块用中文**——与所在文件保持一致。
- AI/模型失败以结果对象返回（`success=False`），而基础设施/数据库错误则抛异常——保持这种区分。
- 改动积分、任务、图片版本、Agent run 相关逻辑时，请保留上文那些原子/线程安全的写法；它们正是为了在 ThreadPoolExecutor 和并发请求下避免竞态而存在的。
- 新增 AI provider：实现 `AIProvider` 两个方法（不支持的能力返回 `success=False`，不要抛），在 `factory.py` 的 `_create_provider()` 里挂上分支，必要时在 `_resolve_text_config()`/`_resolve_image_config()` 加该 provider 的 key 回退链；别绕过 capability 路由直接 new provider。
