# AGENTS.md

This file provides guidance to Codex (and other coding agents) when working with code in this repository.

> 这是 `CLAUDE.md` 的精简镜像。两者口径保持一致，但 **`CLAUDE.md` 最详尽，遇到分歧以 `CLAUDE.md` 为准**。本文聚焦本仓库当前的 **Code 域**产品，已据当前代码重写。

## 项目范围

**本仓库当前是一个聚焦 Code 域的产品。** 产品面 = Code 软件创作工作流（`/api/code` + 共享的 `/api/agent` Agent Swarm + 前端 `frontend .../code`），以及 Code 域依赖的共享底座（认证、团队、积分、AI provider 抽象层）。开发、改动与验证都围绕这一面展开。

- **Code** —— 软件创作工作流：需求文档 → 开发流程 → 文档拆分 → 风格文档 → UI 预览 → **前端代码生成与预览**，并可进一步做**全栈生成（前端 + 后端 + 中间件）+ 应用部署**。它通过 **Agent Swarm** 运行时执行，产出可回放的 `AgentRun`。
- 代码分层：Code 域在 `backend/{models,routes,services}/code`，Agent Swarm 在 `backend/{models,services}/agent`，共享底座（auth/team/credit/ai）各有独立子包；前端按关注点分目录（`frontend/src/{pages,components,stores}/...`）。

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

npm run test:backend     # uv run pytest

uv run pytest -m "not integration"             # 只跑单元测试（CI/日常默认，无需 API key）
uv run pytest -m integration -s                # 集成测试（需 ANTHROPIC_API_KEY + PANLAXY_API_KEY + 网络，会产生真实费用）
uv run pytest path/to/test_x.py::test_name -v  # 跑单个测试
```

**信任脚本前需要知道的坑：**
- 后端跑在 **5001**（见 `backend/app.py` 末尾的 `app.run(..., port=5001)` 与 Vite 代理目标），不是 5000。
- `tests/` 真实存在并可被 pytest 收集（`pyproject.toml` 配 `testpaths=["tests"]`）。单元测试无网络、用 fake provider；`@pytest.mark.integration` 的集成测试会打线上 Claude + Panlaxy，缺 key/网络时优雅跳过。`tests/conftest.py` 有 autouse fixture 在每例前后 `reset_providers()` 清 provider 缓存。
- **前端没有 `test` 脚本、也没装 vitest**（`frontend/package.json` 只有 `dev`/`build`/`lint`/`preview`）。因此 `npm run test:frontend` 与 `npm run test` **会失败**——前端测试视为"未配置"。
- 表结构在启动时由 `backend/app.py` 的 `db.create_all()` 创建，改模型重启即生效。**项目没有任何 Alembic 配置**（无 `alembic.ini`/`env.py`/migrations），故 `npm run db:migrate` / `db:revision` **会直接失败**；表结构以 `create_all()` 为准。

## 后端架构

**应用工厂**（`backend/app.py`）：`create_app(config_name)` 把 `FLASK_ENV` 首字母大写来选配置类（`development` → `DevelopmentConfig`；另有 `Production`/`Testing`）。开发环境在未设置时自动生成 `SECRET_KEY`/`JWT_SECRET_KEY` 并默认 SQLite；**生产环境会校验 `SECRET_KEY`、`JWT_SECRET_KEY`、`DATABASE_URL` 三者都已设置**，否则启动即抛错。蓝图（连同 `url_prefix`）都在这里注册——这是 API 面的权威映射表：`/api/auth`、`/api/users`、`/api/teams`、`/api/credits`、`/api/agent`、`/api/code`（含 `/api/code/figma`、`/api/code/github` 与全栈编排）、`/preview`、`/app`、`/api/admin`。

**扩展**（`backend/extensions.py`）：`db`（Flask-SQLAlchemy）和 `jwt` 定义在此，与 app 分离以避免循环导入。请从 `backend.extensions` 导入 `db`，绝不新建。

**AI provider 抽象层**（`backend/services/ai/`）：所有模型调用的唯一接入点，**按能力（capability）路由的多 provider 体系**——文本与图像各自独立配置、可走不同 API。
- `base.py` —— `AIProvider` 抽象基类，`generate_text()` / `generate_image()` 返回 `TextGenerationResult` / `ImageGenerationResult`（携带 `success`/`error`，**模型失败时返回 `success=False` 而不抛异常**）；另有流式 `generate_text_stream()`（逐 token yield，出错抛异常由调用方兜底）。
- `factory.py` —— `get_text_provider()` 按**文本配置**、`get_image_provider()` 按**图像配置**构建，皆为线程安全的带锁缓存单例。**后台线程里调用要传 `force_new=True`** 拿全新实例；factory 直接读 `os.getenv`（不依赖 app config），改配置后用 `reset_providers()` 清缓存。**未知 provider 回退到 Gemini。**
- 已实现 provider：`claude`（文本，流式 + thinking，默认 `claude-opus-4-8`）、`gemini`（文本 + 原生图像，勿用 imagen-*）、`openai`（图像，真正的 OpenAI/ChatGPT API，默认 `gpt-image-2`）、`panlaxy`（图像，OpenAI 兼容）。不支持的能力返回 `success=False`（如 Claude 的 `generate_image()`）。
- **默认接线（`.env.example`）**：文本 → Claude，图像 → OpenAI（`gpt-image-2`），`AI_PROVIDER=gemini` 作为两者回退。
- 环境变量：通用 `AI_PROVIDER`/`AI_API_KEY`/`AI_BASE_URL`；文本 `AI_TEXT_PROVIDER`/`AI_TEXT_MODEL`/`AI_TEXT_MAX_TOKENS` + Claude 专属 `ANTHROPIC_API_KEY`(/`CLAUDE_API_KEY`)；图像 `AI_IMAGE_PROVIDER`/`AI_IMAGE_MODEL` + OpenAI 专属 `OPENAI_API_KEY`/`OPENAI_IMAGE_*` / Panlaxy 专属 `PANLAXY_*`。

**Agent Swarm（`backend/services/agent/`，路由 `/api/agent`）—— Code 域的执行与回放底座：**
- `runtime.py` —— 进程级 `ThreadPoolExecutor(max_workers=8)`（env `AGENT_MAX_WORKERS`；模块级单例 `agent_runtime`）。`register_workflow(key, fn)` 注册、`get_workflow(key)` 取出；`agent_runtime.start(app, run_id)` 把一次 run 提交到线程池，在 `app.app_context()` 里执行。`reconcile_orphaned_runs` 在启动时续跑跨重启的在飞 run（崩溃循环护栏 `AGENT_MAX_RESUME_ATTEMPTS`，默认 3）。
- 模块级 `_register_builtin_workflows()` 注册 **7 个独立 workflow**（彼此独立的 run；`agent_routes` 的 `WORKFLOW_COSTS` 既是计费表也是白名单）：`code_full_generation`（7 步：planner→requirements→flow→documents→style→preview→publisher）、`code_frontend_project_generation`（唯一前端代码路径，Docker `fe-agent` 容器产多文件 React+TS+Vite 工程）、`code_canvas_generation`、`code_figma_slice_generation`（`slicer-agent` 容器跑 Codex CLI 把 UI 缩略图切成可编辑 Design IR；失败静默降级整图）、`code_backend_project_generation`（`be-agent` 容器产含 Dockerfile 的 polyglot 后端工程）、`code_middleware_provisioning`（从清单生成 schema/迁移/seed，**不实建库**）、`code_fullstack_deploy`（应用部署：建库/迁移 → docker build+run → 健康检查 → 注册 `/app/<pid>/api` 反代，任一步失败有序回滚）。
- **全栈三 run 由共享 OpenAPI 契约（`CodeProjectLedger`）连接**：`POST /api/code/projects/<pid>/fullstack/runs` 先合成契约再并发启动前端/后端/中间件三 run；三者完成后 `POST /api/code/projects/<pid>/deploy` 起部署 run。并发上限 `MAX_CONCURRENT_RUNS=6`（env `AGENT_MAX_CONCURRENT_RUNS`）。完整设计见 `docs/code-fullstack-generation.md` 与 `docs/code-domain-handoff.md`。
- `recorder.py` 把每步 prompt/响应、事件（带单调 `sequence`）、产物写入 `AgentRun/AgentStep/AgentEvent/AgentArtifact`；`bus.py` 是 SSE 事件总线。`GET /api/agent/runs/<id>/stream` 先重放已存事件再接实时推送——**已结束的 run 可完整逐步回放**。
- 主要端点：`POST /api/agent/runs`、`GET /api/agent/runs`(可按 domain/resource_id/workflow 过滤)、`GET /api/agent/runs/<id>`、`GET /api/agent/runs/<id>/stream`、`POST /api/agent/runs/<id>/cancel`、`GET /api/agent/artifacts/<id>/file`。

**会话上下文账本（Session Context Ledger）—— 防口径漂移：** 每个 run 携带结构化"共识口径"（技术栈/术语/决策/范围），`context_ledger.py` 维护、`context_verifier.py` 校验：`planner` 步播种，下游步经 `{context_ledger}` / `[[CONTEXT_LEDGER]]` 占位符注入并 `merge` 增补；`documents` 高风险边界额外跑一次 **AI 一致性闸**（按 `CODE_CONTEXT_VERIFY` 计费，fail-open）。**仅内部/调试可见，不进入用户产出**。详见 `docs/agent-context-ledger.md`。

**积分**（`backend/services/credit_service.py`）：消耗积分必须走 `deduct_credits()`——**单条原子 SQL `UPDATE ... WHERE balance >= amount`**，`rowcount == 0` 视为余额不足（抛 `InsufficientCreditsError`），不要换成"先读后写"。余额按用户*或*团队（传 `team_id` 改扣团队）；每次变动写一条 `CreditTransaction` 审计记录。

**所有消耗 AI 的操作都必须计费**，单价统一在 `backend/services/pricing.py`（每项可被 `PRICE_*` 环境变量覆盖，**默认均为 0/免费**）：`CODE_FULL_GENERATION(_TOTAL)`、`CODE_FRONTEND_PROJECT_GENERATION`、`CODE_CONTEXT_VERIFY`，全栈的 `CODE_CONTRACT_SYNTHESIS`/`CODE_BACKEND_PROJECT_GENERATION`/`CODE_MIDDLEWARE_PROVISIONING`/`CODE_FULLSTACK_DEPLOY`。两个封装：`charge()`（预检 + 原子扣费，余额不足返回 `False` 而非抛异常）与 `refund_credits()`（失败退款）。Code 域在创建 run 时按 workflow 总价**预扣**，上下文一致性闸**逐次加扣**；仅当 run 失败且未产出任何核心产物时由 `runtime` 自动退款。

**统一 API 响应**：`{"success": true, "data": ..., "message": ...}` 或 `{"success": false, "error": "CODE", "message": ...}`。权威辅助函数在 `backend/utils/response.py`（`success_response`/`error_response`）——**所有域都导入共享版本，不要本地重复定义**。错误码：`VALIDATION_ERROR`(400)、`NOT_FOUND`(404)、`FORBIDDEN`(403)、`INSUFFICIENT_CREDITS`(402)、`SERVER_ERROR`(500) 等。`app.py` 的全局错误处理器会把 HTTP 异常和未捕获错误归一成这种结构，并在非 debug 模式下隐藏内部细节。

**模型**：主键为字符串 UUID（`db.String(36)`），带 `user_id` + 可空 `team_id`（多租户）+ `visibility`(`private|team|public`)。结构化内容（文档、阶段进度、上下文账本）以 **JSON 存在 Text 列**里，走模型 getter/setter（`get_progress()`、`get/set_context_ledger()` 等），别直接读原始列。模型分布：Code 在 `models/code/*`（`CodeProject`/`CodeDocument`/`CodeStageVersion`），全栈在 `models/code/fullstack.py`（`CodeProjectLedger`/`CodeDeployment`），Agent Swarm 在 `models/agent/*`。

**认证**：Flask-JWT-Extended，access token 30 分钟、refresh token 30 天。`@jwt_required()` 保护接口、`get_jwt_identity()` 读当前用户。

**Prompt（提示词）**：Code 的 prompt 是 `backend/prompts/code/` 下的 `.txt` 模板，**按 BMAD 五段骨架组织**（角色与原则 / 输入 / 本阶段职责与边界 / 产出契约 / 交付前自检），带稳定 `FR/NFR/M/MS` 编号。文档拆分/风格等用 `backend/services/prompt_library/`（`compose_recipe_prompt(...)`，Code 配方传 `include_output_contract=False`）。
- **走 `.format()` 的模板里花括号要 `{{ }}` 转义**；走 `_fill`/`[[KEY]]` 的（如 `frontend_project_prompt`、`figma_slice_prompt`）**不要**转义，且同一 `[[KEY]]` 只能出现一次（`_fill` 是 replace-all）。
- **⚠️ 运行时 prompt 由 `backend/services/prompts/`（`prompt_store.get(key)`，key 形如 `code/<file>.txt`）从 MongoDB `prompts` collection 读取**——`.txt` 只是 `defaults.py` 的 seed/fallback 源。`seed_defaults()` 只插入缺失 key、**从不覆盖已存文档**，所以改 `.txt`「不会自动生效」：要 ① 改 `.txt`；② 用 `scripts/sync_code_prompts.py` / `scripts/sync_backend_prompts.py` 同步进 Mongo（`is_overridden=True` 表示被 admin 改过，**勿擅自覆盖、先问**）；③ 清 `prompt_store._cache`（带 TTL）。占位符/转义/JSON 契约由 `scripts/validate_code_prompts.py` + `tests/test_code_prompts.py` 做 CI 守护。

## 前端架构

React 19 + TypeScript + Vite。`@/` 是 `frontend/src/` 的别名。

- **状态管理**：`src/stores/` 下每个关注点一个 Zustand store（`authStore`、`teamStore`、`creditStore`、`codeStore`、`agentStore`、`canvasStore`、`fullstackStore`、`exportTasksStore`、`preferenceStore`）。服务端缓存可用 TanStack Query。
- **API 层**：`src/api/client.ts` 是 axios 实例，注入 bearer token，收到 401 时**透明刷新 token**（并发 401 排队等同一次刷新，刷新失败则清 token 跳 `/login`）。用 `api.get/post/put/patch/delete`（自动解包 `res.data`），不要直接调 axios。
- **路由**：`src/App.tsx` 所有应用路由都包在 `<ProtectedRoute>` 内；`/login`、`/register` 是仅有的公开路由。主要页面：Code 入口 `/code`、详情 `/code/:projectId`，以及 `/dashboard`、`/settings`、`/team`、`/admin`。左侧 `components/layout/Sidebar.tsx` 是会话侧边栏（Code 历史会话 + 新建会话），可借 `agentStore.openLatestRunForResource()` 调出时间线回放。
- **UI**：shadcn 风格组件（Radix 原语 + Tailwind v4）在 `src/components/ui/`；业务组件分目录在 `src/components/{code,agent,common,layout}/` 等。
- **国际化（i18n）**：i18next，JSON 位于 `src/locales/{en,ja,ko,zh-CN}/`（`common`、`auth`、`code`、`codeapp`、`agent`、`fullstack`、`team`、`admin`、`dashboard`、`settings`、`canvas`、`errors`）。新增 key 要同时加到**四种语言**、放进正确命名空间；不要硬编码面向用户的文案。

## 插件（`plugin/`）

独立插件（在外部宿主里运行、独立构建）放在 `plugin/<name>/`，各自带 `package.json`/构建脚本/`README.md`，**不参与**根目录 `npm run dev`/`build`/`lint`，只通过 HTTP API + 共享数据契约与平台耦合。当前插件：`plugin/figma`（把 Code 域 UI 预览 / 生成的前端在 Figma 里重建为原生图层）。Design IR 双向桥接，权威定义在 `backend/services/code/figma/ir.py`，插件侧镜像 `plugin/figma/src/ir.ts`，规范见 `docs/figma-ir-spec.md`——三者保持一致。

## 值得遵循的约定

- 代码注释：与所在文件 / 邻近代码保持一致的语言与风格。
- AI/模型失败以结果对象返回（`success=False`），基础设施/数据库错误则抛异常——保持这种区分。
- 改动积分、Agent run、上下文账本、部署登记相关逻辑时，保留那些原子/线程安全的写法（为 ThreadPoolExecutor 与并发请求下避免竞态而存在）。
- 新增 AI provider：实现 `AIProvider` 两个方法（不支持的能力返回 `success=False`，不要抛），在 `factory.py` 的 `_create_provider()` 挂分支，必要时在 `_resolve_text_config()`/`_resolve_image_config()` 加 key 回退链；别绕过 capability 路由直接 new provider。
