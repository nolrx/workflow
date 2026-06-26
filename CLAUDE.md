# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

claude code only use chinese respone question.

## 项目范围(重要 — 优先级最高)

**本仓库当前是一个聚焦 Code 域的产品。** 产品面 = Code 软件创作工作流(`/api/code` + 共享的 `/api/agent` Agent Swarm + 前端 `frontend .../code`),以及 Code 域依赖的共享底座(认证、团队、积分、AI provider 抽象层)。开发、改动与验证都围绕这一面展开,**默认只动 Code 域 + Agent Swarm + 共享底座(auth/team/credit/ai)**。

> 仓库根还有一份 `AGENTS.md`(给 Codex 用,是本文件的精简镜像)和 `README.md`(面向使用者的快速上手)。三者口径保持一致,但**本文件(CLAUDE.md)最详尽,以本文件为准**。

## 项目概述

Worksflow 是一个 monorepo：后端为 Flask（Python），前端为 React/TypeScript（Vite），由根目录 `package.json` 的 npm 脚本统一编排。当前产品域为 **Code**：

- **Code**（`/api/code` + 共享的 `/api/agent`、`frontend .../code`）—— 软件创作工作流：需求文档 → 开发流程 → 文档拆分 → 风格文档 → UI 预览 → **前端代码生成与预览**，并可进一步做**全栈生成（前端 + 后端 + 中间件）+ 应用部署**。它通过下文的 **Agent Swarm** 运行时执行，产出可回放的 `AgentRun`。

代码分层：Code 域在 `backend/{models,routes,services}/code` 下分子包，Agent Swarm 在 `backend/{models,services}/agent`，共享底座（auth/team/credit/ai）各有独立子包；前端按关注点分目录（`frontend/src/{pages,components,stores}/...`）。新增功能时，请参照既有 Code/Agent 约定，而不要另起一套新做法。

## 常用命令

除特别说明外，所有命令都在仓库根目录运行。Python 用 `uv` 管理，前端用 `npm`。

```bash
npm run setup            # uv sync + 安装前端依赖
npm run dev              # 同时启动后端 + 前端 (npm-run-all --parallel)
npm run dev:backend      # Flask，地址 http://localhost:5001  (cd backend && uv run python -m backend.app)
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
- 后端跑在 **5001**（见 `backend/app.py` 末尾的 `app.run(..., port=5001)` 以及 Vite 代理目标），不是 5000。
- **前端没有 `test` 脚本、也没装 vitest**（`frontend/package.json` 只有 `dev`/`build`/`lint`/`preview`）。因此根目录的 `npm run test:frontend` 与 `npm run test`(它会去跑 `cd frontend && npm test`)**目前会失败**。前端测试视为"未配置"。
- 数据库表结构在启动时由 `backend/app.py` 里的 `db.create_all()` 创建，所以开发环境下改动模型重启即生效。**Alembic 仅是声明的依赖——项目里完全没有 Alembic 配置**（没有 `alembic.ini`、没有 `env.py`、没有 migrations 目录），因此 `npm run db:migrate` / `db:revision` 当前会直接失败，也没有任何迁移历史。在真正搭好迁移之前，把表结构视为由 `create_all()` 驱动。

## 后端架构

**应用工厂**（`backend/app.py`）：`create_app(config_name)` 通过把 `FLASK_ENV` 首字母大写来选择配置类（`development` → `backend/config.py` 中的 `DevelopmentConfig`；另有 `Production`/`Testing`）。开发环境在未设置时会自动生成 `SECRET_KEY`/`JWT_SECRET_KEY`，并默认使用 SQLite；**生产环境会校验 `SECRET_KEY`、`JWT_SECRET_KEY`、`DATABASE_URL` 三者都已设置**，否则启动即抛错。蓝图（blueprint）连同各自的 `url_prefix` 都在这里注册——这是 API 面的权威映射表（`/api/auth`、`/api/users`、`/api/teams`、`/api/credits`、`/api/agent`、`/api/code`（含 `/api/code/figma`、`/api/code/github` 与全栈编排）、`/preview`、`/app`、`/api/admin`）。

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

**后台任务 —— Agent Swarm（Code 域）用进程级 `ThreadPoolExecutor`**（见下一节）：所有耗时的生成 / 部署都作为可回放的 `AgentRun` 跑在这个进程内运行时里，进度与产物经 recorder 落库、经 SSE 事件总线（`bus.py`）实时推送给客户端。

**Agent Swarm（`backend/services/agent/`，路由 `/api/agent`）—— Code 域的执行与回放底座：**
- `runtime.py` —— 进程级 `ThreadPoolExecutor(max_workers=8)`(env `AGENT_MAX_WORKERS`，为支撑全栈三并发容器构建 run 从 4 提到 8;模块级单例 `agent_runtime`)。workflow 用 `register_workflow(key, fn)` 注册进 `_WORKFLOWS` 字典、用 `get_workflow(key)` 取出；`agent_runtime.start(app, run_id)` 把一次 run 提交到线程池，在 `app.app_context()` 里执行。
- **`runtime.py` 模块级 `_register_builtin_workflows()` 注册 7 个独立 workflow**（彼此是独立的 run，不是某个的子步；`agent_routes` 的 `WORKFLOW_COSTS` 既是计费表也是白名单，`create_run` 用 `get_workflow` 校验）：
  - **`code_full_generation`**（`workflows/code_workflow.py`，`run_code_workflow`）——**7 步**：`planner`(规划) → `requirements`(需求) → `flow`(开发流程) → `documents`(文档拆分) → `style`(风格) → `preview`(UI 预览) → `publisher`(发布)。
  - **`code_frontend_project_generation`**（`workflows/code_frontend_project_workflow.py`）——**唯一的前端代码生成路径**：`fe_planner` → `fe_project_build` → `fe_publish`。要求已有一个完成的 Code 项目（按 `resource_id` 复用上一段 full-generation run 的 ledger），在 Docker-out-of-Docker（`fe-agent` 容器）里用自主编码 CLI 产出可构建的多文件 React + TS + Vite 工程。
  - **`code_canvas_generation`**（`workflows/code_canvas_workflow.py`）——独立 run。
  - ⚠️ 旧的**单文件 HTML 生成流程已移除**：`code_frontend_generation`（spec→单文件 index.html）与已退休的 `code_figma_restore`（Figma→单文件 HTML）连同 `frontend_build_service`、`frontend_build/critic/repair/from_figma` 提示词一并删除；前端代码现在只由上面的多文件工程路径产出（历史 run 仍可回放，前端 `previewTabs.ts` 保留旧 key 仅用于回放）。
  - **`code_figma_slice_generation`**（`workflows/code_figma_slice_workflow.py`）——把一张 UI 预览缩略图重建成**可在 Figma 逐元素编辑的 Design IR**：`fe_slice_planner` → `fe_slice_analyze` → `fe_slice_publish`。**分析步在 docker `slicer-agent` 容器里跑 OpenAI Codex CLI**（Docker-out-of-Docker：后端容器挂宿主 `/var/run/docker.sock`、`TMPDIR` 两侧同路径供 `-v {workdir}:/out` bind mount；镜像用 `docker compose --profile setup build` 预构建）。**任何失败（认证/超时/无产物）都静默降级成单图 IR**（整图、run 仍 `completed`，表象像"切片没生效"），排查看 `TMPDIR/slicer-agent-*/` 下的 `codex_stderr.log`/`codex_exit`/`degraded`。Codex 调用契约见 `figma_slice_service.py::_CONTAINER_SCRIPT` 注释（须先 `codex login --with-api-key`，env 里的 `OPENAI_API_KEY` 不会被自动使用；prompt 走 **stdin** 而非位置参数，因 `-i/--image` 是 variadic 会吞掉位置参；Codex 是 reasoning 模型耗时数分钟，`CODEX_TIMEOUT` 默认 300 偏紧，生产已在 compose 设 900）。
  - **`code_backend_project_generation`**（`workflows/code_backend_project_workflow.py`）——**全栈:后端多文件工程**。`be_planner` → `be_project_build` → `be_publish`，镜像前端工程在 `be-agent` 容器里产出含 `Dockerfile`/健康检查的 polyglot 后端工程（产物 `code_backend_project_*`）。
  - **`code_middleware_provisioning`**（`workflows/code_middleware_workflow.py`）——**全栈:中间件**。`mw_planner` → `mw_provision` → `mw_publish`，从中间件清单生成 schema/迁移/seed 产物（**不实建库**，实建库在部署 run；产物 `code_middleware_*`）。
  - **`code_fullstack_deploy`**（`workflows/code_fullstack_deploy_workflow.py`）——**全栈:应用部署**。单计费步 `fs_deploy`（provision→build→start→done 四 phase）：建库/迁移 → `docker build`+`run` 后端容器 → 健康检查 → 注册 `/app/<pid>/api` 反代，**任一步失败有序回滚**（`deploy_service.py`，回滚过程会 narrate 到时间线）。
  - **全栈三 run 由共享 OpenAPI 契约（`CodeProjectLedger`）连接**：编排端点 `POST /api/code/projects/<pid>/fullstack/runs`（`fullstack_routes.py`）先同步合成契约（计费 `CODE_CONTRACT_SYNTHESIS`）再并发启动前端/后端/中间件三 run；三者完成后 `POST /api/code/projects/<pid>/deploy` 起部署 run。`MAX_CONCURRENT_RUNS` 因此提到 6（env `AGENT_MAX_CONCURRENT_RUNS`）。完整设计 / 数据模型 / 实施清单见 **`docs/code-fullstack-generation.md`** 与 **`docs/code-domain-handoff.md`**。
- `recorder.py` 把每步的 prompt/响应、事件（带单调 `sequence`）、产物写入 `AgentRun/AgentStep/AgentEvent/AgentArtifact`（模型在 `backend/models/agent/{run,step,event,artifact}.py`）；`bus.py` 是 SSE 事件总线。客户端经 `GET /api/agent/runs/<id>/stream` 先重放已存事件再接实时推送——**已结束的 run 可被完整逐步回放**（流式 token delta 不持久化，回放时呈现各步完整响应）。
- 主要 HTTP 端点：`POST /api/agent/runs`(创建并启动一次 run、预扣积分、校验 domain/workflow)、`GET /api/agent/runs`(列当前用户的 run，可按 domain/resource_id 过滤)、`GET /api/agent/runs/<id>`(run+steps+events+artifacts 快照)、`GET /api/agent/runs/<id>/stream`(SSE)、`POST /api/agent/runs/<id>/cancel`(协作式取消)、`GET /api/agent/artifacts/<id>/file`(下载产物，仅 owner)。

**会话上下文账本（Session Context Ledger）—— 防口径漂移：** 每个 run 携带一份结构化的"共识口径"(技术栈/术语/关键决策/范围)，由 `context_ledger.py` 维护、`context_verifier.py` 校验。
- 在 `planner` 步 `seed_from_inputs(...)` 播种；之后每个下游步用 `render_for_prompt()` 注入到该步 prompt（模板里的 `{context_ledger}` / `[[CONTEXT_LEDGER]]` 占位符），并在 `requirements/flow/documents/style` 步用 `merge(...)` 增补，每步后 `persist_ledger()` 落库。
- `context_verifier.py` 在每个产文本的步做**确定性检查**（输出非空、文档类型覆盖、必填字段、前端技术栈一致性）；只在**高风险边界**（目前为 `documents` 步）额外跑一次 **AI 一致性闸**——这一步**按 `pricing.CODE_CONTEXT_VERIFY` 计费**，provider 未配置时返回 `None`、出错时 fail-open 并标记 `degraded`。结果以 `CONTEXT_UPDATED` / `CONTEXT_CONFLICT` 事件发出。
- 账本**仅内部/调试可见，不进入用户产出**。详见 `docs/agent-context-ledger.md`。
- `code_frontend_project_generation` 在 `fe_planner` 步会**重新加载**上一段 full-generation run 的 ledger,以保证前后两段口径一致。

**积分**（`backend/services/credit_service.py`）：消耗积分的操作必须走 `deduct_credits()`，它执行**单条原子 SQL `UPDATE ... WHERE balance >= amount`**，并把 `rowcount == 0` 视为余额不足（抛 `InsufficientCreditsError`）——这是防竞态的关键，不要换成"先读后写"。余额是按用户*或*按团队的：传入 `team_id` 则改为从团队余额扣减。每次变动都会写一条 `CreditTransaction` 审计记录。

**所有消耗 AI 的操作都必须计费**，单价统一定义在 `backend/services/pricing.py`（每项可被 `PRICE_*` 环境变量覆盖，常量含 `CODE_FULL_GENERATION`、`CODE_FULL_GENERATION_TOTAL`、`CODE_FRONTEND_PROJECT_GENERATION`、`CODE_CONTEXT_VERIFY`，以及全栈的 `CODE_CONTRACT_SYNTHESIS`、`CODE_BACKEND_PROJECT_GENERATION`、`CODE_MIDDLEWARE_PROVISIONING`、`CODE_FULLSTACK_DEPLOY` 等，默认均为 0/免费）——不要把成本硬编码散落在各处。路由/任务里用两个封装：`charge()`（预检 + 原子扣费，余额不足返回 `False` 而非抛异常，便于循环里优雅停止）与 `refund_credits()`（失败退款，走 `add_credits(transaction_type="refund")`）。计费时机：
- **Code**：在 `agent_routes` 创建 run 时按 workflow 总价**预扣**（`code_full_generation` → `CODE_FULL_GENERATION_TOTAL`，`code_frontend_project_generation` → `CODE_FRONTEND_PROJECT_GENERATION`；全栈三 run 各按 `CODE_BACKEND_PROJECT_GENERATION`/`CODE_MIDDLEWARE_PROVISIONING`/前端价预扣，契约合成按 `CODE_CONTRACT_SYNTHESIS`、部署按 `CODE_FULLSTACK_DEPLOY`），过程中的上下文一致性闸按 `CODE_CONTEXT_VERIFY` **逐次加扣**；仅当 run 失败且**未产出任何核心产物**时由 `runtime` 自动退款。
- `GET /api/credits/balance` 支持 `?team_id=`（带成员校验）查询团队余额；前端各生成完成回调里调 `creditStore.refreshBalance()` 刷新展示。

**统一 API 响应**：接口返回 `{"success": true, "data": ..., "message": ...}` 或 `{"success": false, "error": "CODE", "message": ...}`。权威辅助函数在 `backend/utils/response.py`（`success_response`/`error_response`）——**所有域都应导入共享版本，不要再本地重复定义**。错误码统一词汇：`VALIDATION_ERROR`(400)、`NOT_FOUND`(404)、`FORBIDDEN`(403)、`INSUFFICIENT_CREDITS`(402)、`SERVER_ERROR`(500) 等。`app.py` 里的全局错误处理器已经会把 HTTP 异常和未捕获错误归一成这种结构，并在非 debug 模式下隐藏内部细节。

**模型**：主键为字符串 UUID（`db.String(36)`），带 `user_id` + 可空的 `team_id` 以支持多租户，并有一个取值为 `private|team|public` 的 `visibility` 字段。结构化内容（文档内容、阶段进度、上下文账本）以 **JSON 形式存在 Text 列**里，通过模型上的 getter/setter 辅助方法访问（`get_progress()`、`get/set_context_ledger()` 等）——请走这些方法，不要直接读原始列。各域模型分布：Code 在 `models/code/*`（`CodeProject`、`CodeDocument`、`CodeStageVersion`），全栈在 `models/code/fullstack.py`（`CodeProjectLedger`、`CodeDeployment`），Agent Swarm 在 `models/agent/*`（`AgentRun/AgentStep/AgentEvent/AgentArtifact`）。

**认证**：Flask-JWT-Extended，access token 30 分钟、refresh token 30 天。用 `@jwt_required()` 保护接口，用 `get_jwt_identity()` 读取当前用户。

**Prompt（提示词）**：
- **Code** 的 prompt 是 `backend/prompts/code/` 下的 `.txt` 模板，**按 BMAD 风格组织为「角色与原则 / 输入（视为既定事实）/ 本阶段职责与边界 / 产出契约（唯一权威）/ 交付前自检」五段骨架**，各阶段带稳定 `FR/NFR/M/MS` 编号与明确的「不做(交给下游)」边界：`requirements_prompt`、`requirements_clarify_prompt`、`development_flow_prompt`、`document_split_prompt`、`style_prompt`（及各自的 `*_revision` / `*_section_revision` 修订门）、`frontend_project_prompt` / `frontend_project_repair_prompt`（Docker 前端工程）、`figma_slice_prompt`（切片分析）、`html_to_figma_ir_prompt`（HTML→Figma 导出）。文档拆分/风格等模板用 `backend/services/prompt_library/`（`compose_recipe_prompt(...)`）拼出 `{system_prefix}`——**Code 配方调用时传 `include_output_contract=False`，不再附加通用 `OUTPUT_CONTRACT`，以免与各阶段「产出契约」冲突导致输出漂移**。提示词的占位符 / 花括号转义 / JSON 契约由 `scripts/validate_code_prompts.py` + `tests/test_code_prompts.py` 做 CI 守护；改 `.txt` 后用 `scripts/sync_code_prompts.py` 同步进 Mongo 才会在运行时生效（见下条）。
- 增删改 prompt 请在这些位置进行，不要内联写进业务逻辑。**走 `.format()` 的模板（多数含 JSON 示例的）里花括号要写成 `{{ }}` 转义**，否则报错（`test_code_json_parsing.py` 覆盖了 JSON 解析健壮性）；而走 `_fill` / `[[KEY]]` 占位符替换的模板（如 `figma_slice_prompt`，由 `figma_slice_service._fill` 处理）则**不要**转义花括号。
- **⚠️ 运行时 prompt 由 `backend/services/prompts/`（`prompt_store.get(key)`，key 形如 `code/<file>.txt`）从 MongoDB `prompts` collection 读取——上面的 `.txt` / `internet_roles` 只是 `defaults.py` 的 seed 与 fallback 源。改了模板文件「不会自动生效」**：`seed_defaults()` 只插入缺失的 key、**从不覆盖已存文档**（为保护 admin 后台编辑），所以已 seed 过的 prompt 会一直用 Mongo 里的旧版本，重建镜像也没用。让改动生效要：① 改 `.txt`；② 更新 Mongo 对应 `_id` 文档的 `content`（`is_overridden=True` 表示被 admin 改过，**勿擅自覆盖、先问**；`False` 时可安全同步成新 default）；③ 重启后端清 `prompt_store._cache`（带 TTL）。仅当 Mongo 不可达时才整体走文件 fallback。

## 前端架构

React 19 + TypeScript + Vite。`@/` 是 `frontend/src/` 的别名。

- **状态管理**：`src/stores/` 下每个关注点一个 Zustand store（`authStore`、`teamStore`、`creditStore`、`codeStore`、`agentStore`、`canvasStore`、`fullstackStore`、`exportTasksStore`、`preferenceStore`）。服务端缓存可用 TanStack Query。
- **API 层**：`src/api/client.ts` 是 axios 实例。它会注入 bearer token，并在收到 401 时**透明刷新 token**——并发的 401 会排队等待同一次刷新调用，刷新失败则清除 token 并跳转 `/login`。请使用 `api.get/post/put/patch/delete` 辅助方法（它们会自动解包 `res.data`），而不要直接调用 axios。
- **路由**：`src/App.tsx` 中所有应用路由都包在 `<ProtectedRoute>` 内；`/login` 和 `/register` 是仅有的公开路由。`AuthInitializer` 在加载时引导初始化认证状态。主要页面：Code 入口 `/code`、详情 `/code/:projectId`，以及 `/dashboard`、`/settings`、`/team`、`/admin`。左侧 `components/layout/Sidebar.tsx` 是**会话侧边栏**（Code 历史会话列表 + 新建会话）——"会话"即 `CodeProject` 本身，点击切换、可深链；可借 `agentStore.openLatestRunForResource()` 调出时间线做过程回放。
- **UI**：shadcn 风格组件（Radix 原语 + Tailwind v4）在 `src/components/ui/`；业务组件分目录在 `src/components/{code,agent,common,layout}/` 等。
- **国际化（i18n）**：i18next，按命名空间组织的 JSON 位于 `src/locales/{en,ja,ko,zh-CN}/`（`common`、`auth`、`code`、`codeapp`、`agent`、`fullstack`、`team`、`admin`、`dashboard`、`settings`、`canvas`、`errors`）。新增 key 要同时加到**四种语言**、放进正确的命名空间；不要硬编码面向用户的文案。

## 插件（`plugin/`）

仓库里所有**独立插件**——在外部宿主（如 Figma 桌面端）里运行、独立构建、不属于 `backend`/`frontend` 主应用——都放在顶层 `plugin/<name>/` 下，**一个子目录一个插件**，各自带独立的 `package.json` / 构建脚本 / `README.md` / `.gitignore`。

- **约定**：新增插件时新建 `plugin/<name>/` 目录，不要散落在根目录、也不要塞进 `frontend`。插件**不参与**根目录 `npm run dev`/`npm run build`/`npm run lint` 的编排（根 `package.json` 不引用 `plugin/*`），各插件在自己目录里用自己的脚本构建。插件与平台的耦合只通过 **HTTP API**（`/api/...`）和**共享数据契约**完成，不直接 import 后端/前端代码。

**当前插件：`plugin/figma`（Worksflow Importer）。** 一个 Figma 桌面插件，把平台导出的设计（Code 域 UI 预览 / 生成的前端）在 Figma 里重建为原生图层。

- **构成**：`manifest.json`（插件清单，`main=code.js`、`ui=ui.html`）、`src/code.ts`（主线程，重建图层）、`src/ir.ts`（Design IR 的插件侧镜像）、`ui.html`（插件 UI）。用 **esbuild** 把 `src/code.ts` 打包成 `code.js`（`code.js` 与 `node_modules` 都在 `.gitignore` 内，需本地构建）。
- **构建 / 加载**：`cd plugin/figma && npm install && npm run build`（`npm run watch` 监听、`npm run typecheck` 跑 `tsc --noEmit`）；然后在 Figma 桌面端 **Plugins → Development → Import plugin from manifest…** 选择 `plugin/figma/manifest.json`，改完 `src/` 后重新 `build`。
- **调用流程（运行时）**：① 平台里打开项目点 **导出到 Figma**，拿到 8 位**一次性配对码**（5 分钟有效）；② 在 Figma 运行本插件，填入后端 URL + 配对码；③ 插件 UI 从**免鉴权**的 `GET /api/code/figma/pull?code=…` 拉取导出包（**配对码即凭证**），解码内联图片，主线程把 Design IR 重建为 frames / rectangles / text / image fills。后端侧的导出/拉取/导入逻辑在 `backend/routes/code/figma_routes.py` 与 `backend/services/code/figma/*`。
- **Design IR 契约**：双向桥接的中间表示，权威定义在 `backend/services/code/figma/ir.py`，插件侧镜像 `plugin/figma/src/ir.ts`，规范见 `docs/figma-ir-spec.md`——三者要保持一致。
- **生产注意**：`manifest.json` 的 `networkAccess.allowedDomains` 开发期是 `"*"`，发布前必须收紧到确切的后端域名（如 `["https://studio.example.com"]`）。

> 注：Figma 在产品里是**收进 Code 域 UI 生成阶段**的能力（导出仅在 UI 完成后可用）；这里的 `plugin/figma` 只是其中跑在 Figma 里的那一端。

## 值得遵循的约定

- 代码注释：与所在文件 / 邻近代码保持一致的语言与风格。
- AI/模型失败以结果对象返回（`success=False`），而基础设施/数据库错误则抛异常——保持这种区分。
- 改动积分、Agent run、上下文账本、部署登记相关逻辑时，请保留上文那些原子/线程安全的写法；它们正是为了在 ThreadPoolExecutor 和并发请求下避免竞态而存在的。
- 新增 AI provider：实现 `AIProvider` 两个方法（不支持的能力返回 `success=False`，不要抛），在 `factory.py` 的 `_create_provider()` 里挂上分支，必要时在 `_resolve_text_config()`/`_resolve_image_config()` 加该 provider 的 key 回退链；别绕过 capability 路由直接 new provider。
