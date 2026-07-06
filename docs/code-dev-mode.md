# 开发模式（Dev Mode）改造文档

> 状态：设计草案（改造文档，实施前评审用）
> 作者范围：Code 域 + Agent Swarm + 共享底座（不涉及其它域）
> 关联文档：[code-iterative-generation.md](./code-iterative-generation.md)、[code-eval-and-gating.md](./code-eval-and-gating.md)、[code-fullstack-generation.md](./code-fullstack-generation.md)、[agent-context-ledger.md](./agent-context-ledger.md)、[code-dev-sprint-p1-p2.md](./code-dev-sprint-p1-p2.md)、[platform-deploy.md](./platform-deploy.md)、[smoke-test-flow.md](./smoke-test-flow.md)

---

## 0. 一句话概述

在「应用预览」阶段新增**开发模式**：一个类 Claude Code / Codex CLI 的 **web 交互式编码工作台**——左栏对话、右栏实时预览（HMR 热更新），背后是一个**长运行 dev 容器**（`npm run dev`）+ 一个**会话式交互运行时**，让用户以对话方式驱动、打断、并行控制 agent 的增量开发，并把开发进度以**实时功能点 checklist** 呈现；除非用户明确部署，dev 容器不中断；开发过程全程由 harness 的验证→修复/验收驱动/防漂移/人在环等机制托底，保证最终产物可部署可用。

---

## 1. 背景与目标

### 1.1 当前产品链路（现状）

Code 域现有两种「生产代码」的形态，**都不是交互式开发**：

1. **批处理生成**：`code_full_generation`（7 步文档流水）→ `code_frontend_project_generation`（fe-agent 容器 `docker run --rm` 一次性构建多文件 React 工程）→ `code_fullstack_*`（后端/中间件/部署）。每个都是「一次 run 跑完即终态」，产物是**静态 dist**。
2. **二次开发（App Space）**：`CodeAppIteration` 状态机——「陈述一句变更 → 影响分析 run → 用户确认执行计划 → 按 lane 重跑生成 → 重新部署 → 懒重对账 released」。这是**请求驱动的一次性批处理**，不是对话式、不是长运行、无实时预览。

预览是**静态的**：`/preview/<project_id>/` 永远服务某个前端生成 run 落盘的静态 `dist`（`agent_runs/<run_id>/site`），不是运行中的 dev server（`preview_routes.py::serve_project_preview`）。用户「应用容器」只在**部署** run 里才创建（`deploy_service._run_container`，`docker run -d --restart unless-stopped`，接入共享 `worksflow-net`，无宿主端口，经 `/app/<pid>/api` 反代）。

### 1.2 目标（本次改造）

把「预览完 → 提工单/重跑」的粗粒度交互，升级为「**边聊边看边改**」的开发工作台：

- 用户在应用预览阶段点「开发模式」，进入本会话的开发态；
- 右栏是**运行中**的 dev server 实时预览（改了立即热更），不再是静态 dist；
- 左栏对话驱动 agent 增量开发，可**打断**、可**继续**、可**并行**多个功能；
- 功能点以持久 **checklist** 呈现，完成/新增实时提醒；
- agent 产出严格受**前期文档 + context ledger + 契约**约束，不跳框架（除非需求变化，则联动同步文档/契约）；
- 触发任意错误 / 容器终止时 agent **自愈**，保证最终可部署；
- 前端开发模式部署后产出 **API 契约**，作为后端生成的前置。

### 1.3 设计原则（贯穿全文）

- **能复用绝不新建**（仓库既定约定，见 CLAUDE.md「可组合工作流扩展 canvas」先例）。本改造 ≈ 90% 复用现有底座，新增仅两块硬骨头：长运行 dev 容器 + 会话式交互层。
- **全部新增开关 env-gated、默认逐字不变**（沿用 `test_verify_gate.py` 范式），不破坏现有 7 条 workflow 的判定。
- **客观缺陷才阻断**，AI/度量失败一律 fail-soft / fail-open，绝不拖垮主流程。
- **harness 理论显式落点**（§5 逐条）——用户明确要求。

---

## 2. 术语

| 术语 | 定义 |
|------|------|
| **Dev 会话（DevSession）** | 一个 `CodeProject` 上的一段长运行开发态：绑定一个长运行 dev 容器 + 一份持久 checklist + 一串开发回合。**新增持久实体**。 |
| **开发回合（Dev Turn）** | 用户一次指令触发的一次「编辑 → 验证 → 更新 checklist」闭环。**每回合复用一个有界 `AgentRun`**（`code_dev_turn` workflow）。 |
| **Dev 容器** | 长运行、跑 `npm run dev`（Vite + HMR）的容器，源码常驻其工作区。**新增,复用 deploy 的 `-d --restart` 范式**。 |
| **Checklist（功能点清单）** | 从 ledger 的 FR/NFR 派生、但**持久化 + 用户可编辑/可勾选**的功能进度看板。**新增持久实体**（现有 `features` 是每轮现算的临时态,无表）。 |
| **编辑模式（edit-mode）** | agent 基于既有源码原地增量改而非重生成的现成机制（`frontend_project_service._seed_base` + `_CONTAINER_SCRIPT` 的 `/out/_base` cp）。 |

---

## 3. 现状盘点：能复用什么 / 缺什么

> 这一节是改造的地基。每条都标注了权威代码位置，实施时以此为准。

### 3.1 Agent Swarm 运行时（`backend/services/agent/`）

| 能力 | 现状 | 对开发模式的意义 |
|------|------|------------------|
| Run 生命周期 | `runtime.AgentRuntime`：进程内 `ThreadPoolExecutor(16)`，单 gunicorn worker，`_execute` 在 `app.app_context()` 里顺序跑 step | **一个回合 = 一次有界 run** 可直接复用 |
| 可回放 SSE | `recorder.emit`/`emit_delta` + `bus.event_bus` + `agent_routes._event_stream`（先 DB 重放再实时推，`?last_sequence=` 断线补齐，MODEL 事件只存字符数） | 左栏对话 + checklist 实时推送**零传输层改动** |
| 协作式取消 | `runtime.request_cancel/is_cancelled`（进程内 set，step 边界 + 容器 stdout 循环检查，命中 `proc.kill()` 容器子进程） | **「打断」的现成基础**（打断当前回合的 claude 进程，不杀 dev 容器） |
| 人在环 PAUSED | workflow 返回 `status=paused` → worker 退出 → 用户 `POST /resume` 把 directive 写进 `config._resume` → 重启同 run_id | **「继续 / 修订」的现成基础**（但注意是「冷重启」语义，见 §3.1 缺口） |
| run 内并行 | 仅评审 panel：`_verify_support.run_reviewers` 用临时 `ThreadPoolExecutor` 并发 N 个只读模型调用（DB 写在主线程、子线程无 session） | **「多 subagent 并行」的安全范式模板** |
| run 间并行 | 全栈 trio（3 独立 run），`MAX_CONCURRENT_RUNS=12` | 多功能并行开发的容量底座 |
| 重启续跑 | `reconcile_orphaned_runs` 启动时续孤儿 run（`code_full_generation` retry 续跑、FE/BE/MW 从头、deploy 失败退款） | dev 回合的抗重启 |

**缺口（需新建薄层）**：
- **常驻交互 ≠ 冷重启 resume**。现有 resume 每次重启 worker + 从 cursor/ledger 重建上下文，语义是「暂停到边界等审批」，不是「Claude-Code 式连续会话」。→ 我们用「**持久容器 + 每回合独立有界 run**」规避这个语义冲突（不改 resume 机制，见 §6）。
- **退款判定只看有无 artifact**（`_run_produced_nothing`）。dev 回合的计费/退款语义要重定义（§9）。
- **进程内状态**（event_bus / cancel set / resume 计数）不可多副本。dev 会话是长连接、更吃单 worker，须配套 `GUNICORN_THREADS` / SSE 流数控制（见 §11 风险，对齐 memory `sse-thread-starvation`）。

### 3.2 fe-agent 容器 + Claude Code CLI（`frontend_project_service.py`）

- 现状：`docker run --rm`（**一次性 build-then-die**）→ 容器内 `claude -p`（headless，stream-json，prompt 走 stdin）→ 自愈构建 5 级梯队 → 收集 source+dist → 容器销毁。镜像 `fe-agent:latest`（`backend/docker/fe-agent/Dockerfile`：node:22 + `@anthropic-ai/claude-code` + `@openai/codex` + 隔离 Playwright/Chromium）。
- **编辑模式已完备**：`_seed_base`（防路径穿越）把上版源码写进 `/out/_base` → 容器 `cp -a /out/_base/. $WORK/` → agent 原地改。`_build_prompt` 选 `frontend_project_edit_prompt.txt`，塞 `CHANGE_INSTRUCTION/CHANGE_PLAN/BASE_FILES`。
- **现成的长运行范式**：`deploy_service._run_container` 的 `docker run -d --name --restart unless-stopped --network worksflow-net`。
- **缺口**：完全没有 dev server / HMR 形态。→ dev 容器 = 复用 fe-agent 镜像 + `-d --restart` 范式 + 容器内跑 `npm run dev`（Vite :5173），指令注入走 `docker exec` 触发 edit-mode 回合（§6、§10）。**镜像无需重建**（已含 claude/codex/node，dev server 只是多跑 `npm run dev`）。

### 3.3 部署 / 预览 / 反代（`deploy_service.py`、`preview_routes.py`、`fullstack_routes.py`、`frontend/nginx/default.conf`）

- 用户容器：`docker run -d --restart unless-stopped --network worksflow-net --memory 512m`，无 `-p`，靠容器名在共享网络互通；登记到 `CodeDeployment`（`container_name/internal_port/api_base_path/status`）。
- 反代双通道：HTTP 走 Flask `proxy_to_backend`（`requests` 流式转发 + `/api` 前缀 fallback）；ws 走 nginx `@app_ws`（`map $http_upgrade` + `error_page 418` + `auth_request /_app_ws_authz` + 变量 `proxy_pass` + `resolver 127.0.0.11`）。
- 预览鉴权：`/preview/<pid>/?token=` → `preview_identity` 校验 → 种 path-scoped cookie（`fe_preview_token`/`fs_app_token`）→ 302 去 token；SPA 兜底回 `index.html`。**CSP 已置空且无 `X-Frame-Options` → 同源可 iframe 内嵌**（右栏实时预览的关键前提）。
- **缺口**：`/preview/<pid>/` 现在 `send_from_directory` 静态 dist。开发模式要新增一条分支：**存在 RUNNING dev 容器时,反代到 dev server（Vite :5173）而非静态 dist**；HMR websocket 走 ws 分流（复用 `@app_ws` 模式）。**nginx 是静态模板（构建进 frontend 镜像），新增 location 须重建 frontend 镜像 + `make redeploy`**（现体系刻意不运行时动态生成 nginx）。

### 3.4 前端（`frontend/src/`）

- `CodeStudio.tsx`：已是「按阶段独立窗口」对话工作台（`CodeStepper` 进度条 + `ConversationRail` 对话 + `PreviewThumbnailPanel` 缩略图 rail）。
- `agentStore.ts`：SSE 用 `fetch`+`ReadableStream`（带 Bearer，**非** `EventSource`），sequence 去重合并、lite 快照去抖重拉、自动重连（`?last_sequence=`，上限 40 次退避）。含 `startRun/openRun/openLatestRunForResource/resumeRun/cancelRun/retryRun/deriveConversation`。
- `CodeAppPreview.tsx`：卡片 + `openProjectPreview()` 新标签打开 `/preview/<pid>/?token=`（不内嵌 iframe）。
- `fullstackStore.ts` + `CodeFullstackPanel.tsx`：**多 run 并行状态的唯一现成范本**（4 lane 各自 SSE 流 + LaneCard 进度视觉）——「多 subagent 并行状态展示」照此。
- `IterationPanel.tsx`：二次开发闭环 UI（需求→分析→计划 checklist→确认→分 lane→部署）——「实时 checklist / 打断继续」最接近的形态参考。
- **单 run 绑定冲突风险**：`agentStore` 是单 run 全局单例（module-level `activeRunId`）。开发模式若同页跟多个 run（对话回合 + 预览构建 + 并行子任务），须用 `fullstackStore` 那种多流结构或分工，别让多 run 抢同一个 `activeRunId`。
- **缺口**：新建两栏页面 `CodeDevMode.tsx` + 路由 + 入口按钮；右栏受控 iframe（live preview）；实时 checklist 组件；四语言 i18n。**无需新建 store 或新 SSE 传输层**。

### 3.5 数据模型 / 文档 / context ledger / checklist（`backend/models/code/`、`context_ledger.py`、`_verify_support.py`）

- 四类阶段文档：需求/流程/风格 = `CodeProject` Text 列；文档拆分 = `CodeDocument` 行。每次写经 `version_service.record_stage_version` → `CodeStageVersion`（版本史、`is_current`、可回滚）。
- 需求变更同步 = **已有三级 revision 门**：① `resume={action:revise,stage,instruction}` 重跑该阶段文档；② `ledger.record_user_revision` 把用户指令折成高优先级决策，`render_for_prompt` 带进所有下游；③行内 `revise_section`。
- context ledger（`ContextLedger`）：`seed_from_inputs` 播种 → `render_for_prompt` 注入 → `merge` 增补 → `persist_ledger` 落 `AgentRun.context_ledger_raw`；FE 工程 run 会 reload 上一段 `code_full_generation` run 的 ledger 保口径。
- 全栈契约 `CodeProjectLedger`：`contract_service.synthesize_contract` 从 `development_flow` 合成 OpenAPI（+中间件清单），是前端后、后端前的前置真值源。
- **⚠️ 关键缺口**：**功能点 checklist 当前无持久实体**。`_verify_support.features_from_ledger` 每轮从 `ledger.requirements` 现算 `[{id,category,description,passes:false}]`，评审器回填 `passes`，最终只落 `Verification` 对象 + `CodeQualitySample.feature_passed/feature_total` **计数**。开发模式要「实时可勾选、跨会话持久」的看板 → **必须新建持久表**（§7），初始种子用 `features_from_ledger`，勾选沿用 `apply_feature_results` 语义，落库走乐观锁/原子写。

### 3.6 二次开发状态机（`CodeAppIteration`）——最接近的现有类比

- `code_app_iteration_workflow`（analyze → 确定性 plan）+ `apps_routes` 懒重对账状态机（`draft→analyzing→awaiting_plan_approval→generating→staging_deploying→released`）+ `_iteration_support.load_prior_source`（载入上版源码 zip 作 edit-mode 种子）。
- 满足：计划 + 多泳道生成 + 部署。**缺**：长运行 dev 容器 + HMR、对话式打断/继续、多 subagent 并行、实时 checklist。
- **结论**：开发模式 = **复用 `CodeAppIteration`/fullstack/agent 的数据·生成·部署底座 + 新建交互式长运行执行层**。不能靠拉长 `code_app_iteration_analysis` workflow 实现（那是批处理）。

---

## 4. 架构总览

### 4.1 核心思路：分离「持久会话」与「有界回合」

平台运行时是**批处理**（run 跑完即终态），而开发模式要**长运行 + 交互**。直接把 workflow 拉成常驻事件循环会与现有 retry/resume/reconcile 语义冲突、且长期霸占 worker slot。因此采用**关注点分离**：

```
┌──────────────────────────── CodeProject（一个会话/项目）─────────────────────────┐
│                                                                                  │
│  ┌─ DevSession（持久实体, 新增）────────────────────────────────────────────┐   │
│  │   · 绑定 1 个长运行 dev 容器（docker run -d, npm run dev, Vite :5173）      │   │
│  │   · 绑定 1 份持久 Checklist（CodeDevTask 行, 新增）                         │   │
│  │   · 容器生命周期 独立于回合 run，「除非明确部署否则不中断」                 │   │
│  │   · 由 dev_service 管理：起/停/idle 回收/健康自愈                           │   │
│  │                                                                            │   │
│  │   ┌─ Turn（开发回合 = 有界 AgentRun: code_dev_turn）──────┐  ← 复用全部底座 │   │
│  │   │  用户指令 → docker exec claude -p（edit-mode 增量改）  │                 │   │
│  │   │  → verify（房规+运行时冒烟+评审）→ 更新 checklist       │                 │   │
│  │   │  → SSE 推送对话/进度/checklist 事件                    │                 │   │
│  │   │  打断 = cancel 本回合 run；继续 = 起下一个回合 run      │                 │   │
│  │   └───────────────────────────────────────────────────────┘                 │   │
│  │   （多 subagent 并行 = 多个并发回合 run, 限定独立模块 + 集成回合兜底）      │   │
│  └────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                  │
│  右栏预览：iframe → /preview/<pid>/ → (dev 容器 RUNNING) 反代到 Vite dev server   │
│  显式部署：停 dev 容器 → 走既有 code_fullstack_deploy 原子部署（health/smoke/itest）│
└──────────────────────────────────────────────────────────────────────────────────┘
```

**为什么这样分**：
- **Turn = 有界 run** → 直接复用 recorder/SSE/verify/取消/续跑/计费的全部机制,每回合有明确的开始/结束/计费点(规避「长命 run 退款语义」问题)。
- **Session/容器 = 持久,独立于 run** → 对齐 `deploy_service` 的 `-d --restart` 容器与 run 解耦范式（memory `redeploy-rebuild-worker-isolation`：`-d --restart` 容器不受平台 drain/redeploy 影响）。dev 容器像已部署应用一样独立存活。
- **指令注入走 `docker exec`** → 不需要把容器里的 `claude -p` 改成常驻服务；每回合 exec 一次 headless edit-mode round，源码已在容器磁盘、Vite 监听文件变化自动 HMR。**最小改动即得交互**。

### 4.2 端到端时序（前端开发模式为例）

```
[用户] 应用预览页点「开发模式」
   │
   ▼
POST /api/code/projects/<pid>/dev-sessions      （dev_service.start_session）
   │  · 若从无代码：起 dev 容器(空脚手架) + 触发「脚手架回合」→ 只生成框架 + checklist
   │  · 若已有前端工程：把最新 code_frontend_project_zip 源码物化进容器工作区 + npm install + npm run dev
   │  · 登记 CodeDevSession(status=starting→running, container/port)
   │  · 从 ledger.features_from_ledger 播种持久 Checklist(CodeDevTask, 全 pending)
   ▼
[前端] 进入 /code/<pid>/dev 两栏页
   · 左栏：对话（agentStore SSE）
   · 右栏：<iframe src="/preview/<pid>/?token=..."> → 反代到 dev 容器 Vite → 实时预览
   · checklist 窗口：CodeDevTask 列表 + SSE 实时勾选/新增提醒
   │
   ▼ 用户在左栏输入「把首页加一个数据看板」
POST /api/code/projects/<pid>/dev-sessions/<sid>/turns   （起 code_dev_turn 有界 run）
   │  1. 注入约束上下文：ledger.render_for_prompt() + docs 摘要 + 契约块 + 相关 checklist 条目
   │  2. dev_service.exec_turn：docker exec 容器内 `claude -p`(edit-mode, base=容器现有源码)
   │  3. stream-json → on_event → SSE（左栏对话实时）
   │  4. Vite HMR 自动热更 → 右栏 iframe 秒级刷新
   │  5. verify：house_rules.check_frontend + 运行时冒烟 + 评审 panel + FR 覆盖确定性检查
   │  6. apply_feature_results → 更新 CodeDevTask（完成/新增）→ 发 CHECKLIST_UPDATED 事件
   │  7. should_stop：达标即停；未过则 edit-mode repair 回合（repair_regressed 防越改越坏）
   ▼
（打断）POST .../turns/<tid>/cancel → is_cancelled → 杀容器内 claude 进程（dev 容器不动）
（继续）再起新回合 run
（需求变化）见 §6.4：record_user_revision + revise 门同步文档 + 按 impact 决定是否重合成契约
   │
   ▼ 用户点「部署」
POST /api/code/projects/<pid>/deploy   （既有入口）
   · dev_service.stop_session（停 dev 容器）
   · 前端阶段：contract_service.ensure_contract → 产出/最终化 API 契约（CodeProjectLedger）→ 后端生成前置
   · 走既有 code_fullstack_deploy 原子部署（provision→build→start→health→smoke→itest→done）
```

---

## 5. harness 理论支撑（逐条落点）

> 用户明确要求「整体开发过程的设计需要 harness 的大量理论来支撑」。下表把开发模式的每个设计决策锚定到一条 harness 原则 + 其**现有代码实现** + **开发模式如何扩展它**。这些原则源自仓库已落地的三篇 harness 实践（见 `_verify_support.py`、`house_rules.py`、`context_ledger.py`、`code-iterative-generation.md`）。

| harness 原则 | 现有实现锚点 | 开发模式如何扩展 |
|---|---|---|
| **Steerability（可操纵/打断继续）** | `context_ledger.merge`（revise 折进账本）+ `code_workflow` REVIEW_STAGES 的 approve/revise 门 + `is_cancelled` 协作式取消 | 把「仅在 REVIEW_STAGES 边界暂停」升级为「**任意回合可打断→注入指令→下一回合吸收**」：打断 = cancel 当前回合 run（杀容器内 claude,dev 容器存活）；继续/修订 = 起新回合并 `record_user_revision` 折进 ledger,让后续对齐新意图 |
| **Checkpoint-and-recover（检查点与恢复）** | `reconcile_orphaned_runs` + `_resume_count` + progress cursor 重建 + **git checkpoint**（`_CONTAINER_SCRIPT` 每轮 build+verify commit）+ recorder 事件重放 | 把 git checkpoint 从「每轮匿名 commit」升级为「**每个用户可确认里程碑一个命名 tag**」,支持「回退到某检查点再走另一条路」；回合 run 沿用 `MAX_RESUME_ATTEMPTS` 防交互 crash-loop |
| **Verification-in-the-loop（闭环验证）** | `_verify_support.Verification`（房规+运行时冒烟+评审 panel 合成裁决）+ `for _round: should_stop` | **每个用户回合改动后复用整套 `_verify`**,把 `Verification.summary_line` 作为**回合回执**呈现给用户（右栏/checklist 即时反馈） |
| **Acceptance-driven（验收驱动）** | `features_from_ledger`（初始全 false）+ `apply_feature_results` + `should_stop(to_acceptance)` 三终止条件（达标/预算/停滞 `_stalled`） | 把 features 做成**用户可见/可编辑的持久 checklist 看板**（§7）；`should_stop` 的「达标即停」天然适配交互式「做到满意为止但有预算闸」 |
| **Self-healing（自愈）** | `repair_instruction`（增量修复简报）+ `repair_regressed`（pass→fail/新房规错/新运行时错则回退更优产物）+ `should_pivot→REPAIR_PIVOT_PLAN` + `deploy_service` comprehensive Codex repair + 分级回滚 | dev 容器崩溃/进程终止 → 探活 → 抓容器日志 → 喂 edit-mode repair 回合（复用 `_CONTAINER_SCRIPT` 的 repair_prompt+build log 套路,§8.4）；`pivot` 阈值可暴露给用户手动触发「这块重写吧」 |
| **Context-as-source-of-truth（文档即真值源）** | `context_ledger`（内部共识）+ `scaffold.ensure_scaffold`（`AGENTS.md`+`docs/contract.md`+`docs/db-schema.md`,progressive disclosure）+ `CodeProjectLedger` 共享契约 + prompt 版本钉定 | 「agent 不跳文档框架」= 每回合动手前 agent 先读 `AGENTS.md`/`docs`（scaffold 已注入容器）+ prompt 注入 `ledger.render_for_prompt()`；用户交互产生的新决策回写 ledger+docs（复用 `ledger_writeback.merge_stage_doc_into_ledger`）成为下次真值源 |
| **Human-in-the-loop（人在环）** | REVIEW_STAGES pause_at + approve/revise + 需求澄清 clarify + `CodeAppIteration` 的 `awaiting_plan_approval`（analyze→展示 impact+plan→confirm/覆盖 scope/勾选允许契约·DB 变更） | 开发模式 = 把 `CodeAppIteration` 的「analyze→approve-plan→generate→deploy」单回合,做成**可连续多回合的对话会话**；高风险变更（登录/支付/权限/DB schema）复用 `_HIGH_RISK_HINTS` 强制二次确认（`allow_contract_change`/`allow_db_change` 门是范式） |
| **Subagent 编排/隔离** | `run_reviewers` 临时线程池（DB 在主线程、子线程无 session）+ Docker-out-of-Docker 隔离 + composable typed 契约节点 + **明确否决「功能内并行 subagent」**（`code-iterative-generation.md` §2.3：共享 `App.tsx`/路由/store = 合并地狱） | 「用户控制多 subagent 并行开发多功能」= **只在真正独立的页面/模块边界并行 + 强制集成验证回合兜底**；每个并行子任务作为独立可回放回合 run；沿用「机制通用、策略专用」（§8.5） |
| **度量评判者（eval / no silent caps）** | `quality_metrics.record_quality_sample`（每 run 落 `CodeQualitySample`）+ `eval_review.py` + `harness_ablation.py` + `eval/baseline.json` 退化闸 | dev 回合末尾 fail-soft 落质量样本（新增 `kind='dev_turn'`）,度量开发模式本身是否真提升；任何 checklist 截断（`_MAX_FEATURES=60`）显式 `log`/提示,不静默 |

---

## 6. 功能点逐条改造方案

> 严格对应用户提出的 5 个功能点。每条给出：需要什么、复用什么、新建什么。

### 6.1 功能点 1：应用预览阶段的「开发模式」按钮 + 进入开发态

**入口**：`CodeStudio.tsx` 顶部 header 操作区（与 GitHub/详情按钮同排,canvas 入口是现成位置范本）加「开发模式」按钮 → `navigate('/code/${pid}/dev')`；也可在 `AppDetail.tsx` 顶部操作区加入口。前提是 `project` 处于「已到 app 阶段/已有风格与 UI 预览」。

#### 6.1.a 无代码 → 只生成「框架 + checklist」；有产出 → 直接用代码 + 审计清单

**判定**:`start_session` 用 `is_runnable_vite(load_prior_source(pid,'frontend'))` 判断原会话是否有**可运行的**前端产出(有 vite dep/dev script + 源码模块;脚手架残桩不算)。

- **无可用产出 → 脚手架回合**:跑一个**脚手架回合**——只让 agent 产出**目录骨架 + 关键文件桩 + `AGENTS.md`/`docs/`**,**不做完整实现**;checklist 从 ledger FR/NFR 派生 → 落**持久 `CodeDevTask`(全 pending)**。复用 `scaffold.ensure_scaffold`、batch0 scaffold 语义、`features_from_ledger`/`render_features_block`。
- **有可运行产出 → 直接用该项目代码 + 启动审计**:dev 容器由 `_resolve_source` 直接以现有源码 seed(不重生成);引导回合携带 `audit:true`(无 instruction、无编辑、无修复),`dev_verify` 步**对现有仓库代码跑一次只读验收评审**(`review_project` + `house_rules`)→ `apply_feature_results` → `sync_checklist` **把清单校准成现状**(哪些功能已实现标 done,未实现留 pending),发 `CHECKLIST_UPDATED`(`payload.audit=true`,消息「已按现有仓库代码校准功能清单:X/Y 已实现」)。之后用户在已知真实进度的清单上继续开发。

#### 6.1.b 长运行 dev 容器（`npm run dev`，除非明确部署否则不中断）

- **新建 `dev_service.py`**：复用 `deploy_service._run_container` 的 `docker run -d --name dev-<pid> --network worksflow-net --restart unless-stopped` 骨架,改为跑 `fe-agent` 镜像的 `npm install && npm run dev`（Vite 监听 :5173），源码物化进容器工作区（bind-mount `.fe-agent-work` 或首回合写入）。
- 生命周期：dev 容器**独立于任何回合 run**,不进 deploy rollback 栈,不被 `stop_deployment` 误杀。「除非明确部署否则不中断」= 只有 `POST /deploy` 或显式 `stop-session` 才停它。
- 登记：新建 `CodeDevSession` 表（仿 `CodeDeployment`,§7）记容器名/端口/status。

#### 6.1.c 两栏页面（左对话 + 右实时预览）

- **新建 `frontend/src/pages/code/CodeDevMode.tsx`** + `App.tsx` 加 `<Route path="/code/:projectId/dev">`。
- 左栏：复用 `ConversationRail`（`deriveConversation` + 上下文化 footer）+ `agentStore`。
- 右栏：**受控 `<iframe src="/preview/<pid>/?token=...">`**——依赖 `preview_routes` 已置空 CSP/无 `X-Frame-Options`;HMR 使右栏无需手动 remount 即自动热更（Vite 客户端 ws 连回 dev server）。首帧带 `?token=` 种 cookie 后可去 token。

#### 6.1.d 需求变化 → 同步文档 + checklist 实时提醒

- 需求/文档同步：见 §6.4。
- checklist 实时窗口：**新建 `DevChecklistPanel` 组件**,消费 `agentStore.events` 里的 `CHECKLIST_UPDATED` 事件（新增事件类型,§8.6）+ `step.verification`;视觉照 `CodeStepper`（勾选/脉冲/红点）+ `IterationDetail`（清单渲染）。完成/新增条目触发 toast/高亮提醒。

#### 6.1.e agent 产出必须有文档支持,不跳框架（除非需求变化）

- 每回合 prompt **强制注入**：`ledger.render_for_prompt()`（要求按 FR/NFR 编号引用不改述）+ 相关 docs 摘要 + 契约块 + 命中的 checklist 条目。
- **确定性护栏**：回合 verify 时复用 `context_verifier.run_deterministic_checks`,并**新增一条 FR 覆盖检查**（产出是否落在已登记 FR/文档范围内；越界 → WARNING + 提示用户「这看起来是新需求,是否登记?」)。
- 只有用户显式表达「新需求」时才 `ledger.merge(requirements_add=...)` 扩充框架（§6.4）。

#### 6.1.f 打断 / 继续 + 多 subagent 并行

- **打断**：`POST .../turns/<tid>/cancel` → 复用 `runtime.request_cancel` → `is_cancelled` 在容器 stdout 循环命中 → `proc.kill()` 杀**容器内 claude 进程**（dev 容器存活）。前端复用 `agentStore.cancelRun`。
- **继续**：起新回合 run（携带上文）。修订走 `resume(revise)` 语义或直接新回合 + `record_user_revision`。
- **多 subagent 并行**：起多个并发回合 run（run 间并行已有,`MAX_CONCURRENT_RUNS`）。**硬约束**（harness 隔离原则,`code-iterative-generation.md` §2.3）：
  1. 每个并行回合**限定在互相独立的页面/模块**（由用户在 UI 勾选功能点分组,或 planner 判定无共享文件）；
  2. 并行回合各自在**独立 git worktree 或独立 dev 容器工作区副本**里改,避免同时写 `App.tsx`/路由/store 的合并地狱;
  3. 并行结束后**强制一个「集成回合」**：合并 + 集成 verify（`run_reviewers` 共识 + 运行时冒烟），冲突则串行化重做。
  - 前端多流状态展示复用 `fullstackStore` + `CodeFullstackPanel`/`LaneCard` 范式（每个并行子任务一张 LaneCard）。

### 6.2 功能点 2：docker 容器依然使用 Claude Code 开发

- 直接达成：dev 容器复用 `fe-agent:latest` 镜像（已内置 `@anthropic-ai/claude-code`）。每回合 `docker exec` 里跑 `claude -p`（edit-mode,复用 `_build_prompt` 的 edit 分支 + `frontend_project_edit_prompt.txt`）。
- 后端开发模式对称：复用 `be-agent` 镜像 + `backend_project_service` 的 edit-mode。
- 凭证注入复用 `docker_env.anthropic_agent_credentials`（+ gateway 重试代理 `ANTHROPIC_RETRY_PROXY_BOOTSTRAP`,注意长命容器里该 localhost:8788 代理要保活,§11 风险）。

### 6.3 功能点 3：错误 / 容器终止 → agent 自愈,保证可部署可用

- **探活**：新建后台探针（复用 `deploy_service.probe_health` 的 `docker inspect .State.Running .RestartCount` + 端口/进程探测思路;`--restart unless-stopped` 已给内核级重启,应用级自愈需平台探针,现无,须新建周期任务）。
- **自愈**：dev server 崩溃/构建失败 → 抓容器日志（`deploy_service.container_logs`）→ 喂 edit-mode **repair 回合**（复用 `_CONTAINER_SCRIPT` 5 级自愈梯队 + `repair_instruction` + `repair_regressed` 回归守护）→ 热重启 Vite。narrate 到时间线（对齐 `_fail_unhealthy`）。
- **保证可部署**：显式部署时走既有 `code_fullstack_deploy` 全套闸——`provision→build→start→health→empty-schema guard→contract smoke→itest→comprehensive Codex repair→分级有序回滚`（`deploy_service`）。dev 态的「能跑」与部署态的「可交付」由这道原子部署闸对齐。

### 6.4 功能点 4：前端阶段部署后产出 API 契约,作为后端生成前置

- 前端 dev 会话「部署」时：调 `contract_service.ensure_contract(force=需要时重合成)` 从 `development_flow`（+ 前端已实现的接口调用面）合成/最终化 OpenAPI + 中间件清单 → 写 `CodeProjectLedger`（`contract_status=ready`,乐观锁 version）。
- 该契约成为后端 dev 会话 / `code_backend_project_generation` 的**只读前置**（后端 workflow 已 `render_contract_for_prompt` 注入）。
- **需求变化联动**（贯穿开发全程）：用户改需求 → `record_user_revision` 折进 ledger + 触发受影响 stage 的 revise 门（`generation_service.revise_*`）同步需求/流程/风格文档 → 若变更触及 API 面（复用 `CodeAppIteration._deterministic_analysis` 的 `_API_HINTS`/`_DB_HINTS` 关键词判定 impact）则 `ensure_contract(force=True)` 重合成契约 → 契约变更事件 + checklist 增补。**校验**：把契约当后端「唯一权威」前须确认 `contract_status==ready` 且非纯 fallback（§11）。

### 6.5 功能点 5：harness 理论支撑

见 §5 全表。设计的每个决策都锚定一条现有 harness 实现,不是空谈。

---

## 7. 数据模型（新增）

> schema 演进无 Alembic（memory `schema-evolution-no-alembic`）：新表靠 `create_all` 自动建,加列只加 nullable 靠 `schema_guard` 自愈。以下两张新表都走 `create_all` 约定。

### 7.1 `CodeDevSession`（`backend/models/code/fullstack.py` 或新 `dev.py`）

仿 `CodeDeployment`。一 project 可有多段 dev 会话（历史留痕），但同时至多一个 `running`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | String(36) UUID | 主键 |
| `project_id` / `user_id` / `team_id` | String(36) | 归属（多租户,team_id 可空） |
| `lane` | String | `frontend` / `backend`（对称支持） |
| `status` | String | `starting`/`running`/`repairing`/`stopped`/`failed`（新状态机） |
| `container_name` / `internal_port` | String / Int | dev 容器句柄（Vite 5173） |
| `workdir` | Text | 容器源码工作区（DooD 宿主路径） |
| `preview_path` | String | `/preview/<pid>/`（反代目标由 status 解析） |
| `base_source_run_id` | String(36) | 物化进容器的源码来自哪个前端生成 run |
| `health` / `restart_count` | String / Int | 探活状态 |
| `last_active_at` | DateTime | idle 回收依据 |
| `created_at` / `stopped_at` | DateTime | |

### 7.2 `CodeDevTask`（持久 checklist / 功能点）

**填补最关键缺口**（现有 `features` 是临时派生态,无表）。初始种子用 `features_from_ledger`,勾选沿用 `apply_feature_results` 的 by-id 折叠语义。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | String(36) UUID | 主键 |
| `project_id` / `session_id` | String(36) | 归属会话 |
| `feature_id` | String | 对应 ledger FR/NFR id（`FR-01` 等）,便于回填 |
| `category` | String | `functional` / `nonfunctional` / `chore` |
| `title` / `description` | String / Text | |
| `status` | String | `pending`/`in_progress`/`done`/`skipped`/`user_added` |
| `source` | String | `ledger_seed` / `user_added` / `agent_discovered` |
| `origin_turn_run_id` | String(36) | 由哪个回合完成/新增（provenance） |
| `note` | Text | 验收备注/评审回填 |
| `order_index` | Int | 展示序 |
| `created_at` / `updated_at` | DateTime | |

**并发写约束**：checklist 是多回合并发写的派生态,**必须像 `CreditService.deduct_credits` 那样原子写**（`UPDATE ... WHERE status=?` 或乐观锁 version），不能先读后写（memory 反复强调的竞态陷阱）。

---

## 8. 后端改造

### 8.1 新增 `backend/services/code/dev_service.py`

会话与容器生命周期的 owner（**不塞进 `frontend_project_service.build_project` 的 `--rm` 批处理路径**,避免污染现有验证循环）：

- `start_session(project, lane)`：物化源码 → `docker run -d --restart unless-stopped`（复用 `_run_container` 骨架）→ `npm install && npm run dev` → 登记 `CodeDevSession` → 播种 `CodeDevTask`。
- `exec_turn(session, prompt, is_cancelled, on_event)`：`docker exec` 容器内 `claude -p`（edit-mode,stdin 喂 prompt）→ 流式读 stdout（复用 `on_event` stream-json→AgentEvent 翻译）→ 命中 `is_cancelled` 杀 claude 进程。**串行化每会话的回合指令队列**（长命交互要自己加锁,现 `build_project` 天然串行）。
- `probe_and_heal(session)`：探活 + 崩溃自愈（§6.3）。
- `stop_session(session)`：`docker rm -f` + status→stopped（显式部署或用户停时调）。
- `reap_idle_sessions()`：idle 回收 + 每用户上限（长命容器资源治理,现无 dev 容器 GC,必建）。

### 8.2 新增 workflow `code_dev_turn`

- 在 `runtime._register_builtin_workflows` 注册 + `agent_routes.WORKFLOW_COSTS` 加白名单/计费 + `pricing.py` 加 `CODE_DEV_TURN`（默认 0）。
- 步骤：`dev_edit`（exec_turn 增量改）→ `dev_verify`（`_verify_support._verify`：房规 + 运行时冒烟 + 评审 panel + FR 覆盖）→ `dev_checklist`（`apply_feature_results` → 更新 `CodeDevTask` → emit `CHECKLIST_UPDATED`）→ 循环 `should_stop`（达标即停/预算/停滞,edit-mode repair + `repair_regressed`）。
- **ledger reload**：回合 run 用 `resource_id=pid` 取最新 ledger（注意 memory `composable P1#4`：**续跑时优先用本会话累积的 ledger,别被 full-generation reload 覆盖**）。

### 8.3 新增 HTTP 端点（`backend/routes/code/`,建议新 `dev_routes.py` 挂 `/api/code`）

| 端点 | 作用 |
|---|---|
| `POST /projects/<pid>/dev-sessions` | 起会话（起容器 + 播种 checklist） |
| `GET /projects/<pid>/dev-sessions/<sid>` | 会话快照（含 checklist + 活跃回合） |
| `POST /projects/<pid>/dev-sessions/<sid>/turns` | 起开发回合（`code_dev_turn` run） |
| `POST /.../turns/<tid>/cancel` | 打断当前回合（复用 `cancel_run`） |
| `PATCH /.../tasks/<task_id>` | 用户手动勾选/编辑/新增 checklist 条目 |
| `POST /projects/<pid>/dev-sessions/<sid>/stop` | 显式停会话（独立于 deploy 的 stop,仿 `apps_routes.stop_app`） |
| 复用 `GET /api/agent/runs/<id>/stream` | 回合 SSE（**零改动**） |

权限：写 owner-only（`_owned_project`），读 owner/团队成员/admin（`_accessible_project`），对齐 App Space 现有闸。

### 8.4 崩溃自愈（详见 §6.3）

复用 `deploy_service.container_logs` + `_CONTAINER_SCRIPT` 自愈梯队 + `repair_regressed`。触发时机是**周期探针**（新建）,不同于 deploy 的部署期一次性闸。

### 8.5 多 subagent 并行（详见 §6.1.f）

范式复用 `run_reviewers` 的临时线程池约束（**DB 写在主线程、子线程只跑无 session 的 subprocess**）。策略层新增：模块独立性判定 + worktree 隔离 + 集成回合 barrier。

### 8.6 事件与计费

- 新增 `AgentEventType`：`CHECKLIST_UPDATED`、`DEV_PREVIEW_READY`、`DEV_CONTAINER_HEALTH`（加进 `event.py`;大文本务必只存字符数或走 `emit_delta`,重键加进 `_HEAVY_PAYLOAD_KEYS` 免撑爆 lite 快照）。
- 计费：**按回合计费**（每 `code_dev_turn` 预扣 `CODE_DEV_TURN`,默认 0）,规避「长命 run 退款语义」问题。容器运行时长可选另计（默认 0）。退款仍走「回合失败且无 artifact」的现有语义（回合是有界 run,天然适配）。

---

## 9. 前端改造

| 项 | 复用 | 新建 |
|---|---|---|
| 入口按钮 | `CodeStudio` header 按钮范式 | 「开发模式」按钮 → `/code/<pid>/dev` |
| 两栏页面 | `CodeStudio` 的 store 绑定/深链/SSE 跟随 | `pages/code/CodeDevMode.tsx` + `App.tsx` 路由 |
| 左栏对话 | `ConversationRail` + `agentStore`（`deriveConversation`/`cancelRun`/`resumeRun`/`retryRun`） | 会话式（可不按阶段过滤的连续对话变体） |
| 右栏实时预览 | `preview_routes` 已可 iframe 内嵌 | 受控 `<iframe src="/preview/<pid>/?token=">`（HMR 自动热更） |
| checklist 窗口 | `CodeStepper`（勾选/脉冲/红点视觉）+ `IterationDetail`（清单渲染） | `DevChecklistPanel`（消费 `CHECKLIST_UPDATED` + `CodeDevTask`） |
| 打断/继续 | `agentStore.cancelRun/resumeRun/retryRun` + `ConversationRail.renderFooter` | 上下文化的打断/继续按钮 |
| 多 subagent 并行 | `fullstackStore` + `CodeFullstackPanel`/`LaneCard` | 每并行子任务一张 LaneCard |
| i18n | `codeapp` 已有 openInBrowser/generate 等 | 新 key 四语言（en/ja/ko/zh-CN）,命名空间 `code`/`codeapp`,并行复用 `fullstack` |

**注意**（memory 踩过的坑）：
- SSE 用 `fetch`+`ReadableStream`+Bearer,**不用** `EventSource`。
- **单 run 绑定冲突**：`agentStore` 是单 run 全局单例。同页跟「回合对话」+「并行子任务」多流时,用 `fullstackStore` 多流结构或分工,别抢 `activeRunId`。
- `openLatestRunForResource` 必须按 workflow 过滤（`code_dev_turn`），否则绑到无 review 事件的辅助 run 导致转录空。
- 自动跟随只在 live 位置真变化时抢焦点（复刻 `CodeStudio` 的 `prevFocusRef`），别每次 refresh 都跳,打断用户回看。

---

## 10. 容器 / 预览 / 反代改造

### 10.1 dev 容器（新建执行器,复用 deploy 底座）

`docker run -d --name dev-<pid> --network worksflow-net --restart unless-stopped fe-agent:latest`（容器内 `npm install && npm run dev -- --host --port 5173`）。源码物化：首回合把 `code_frontend_project_zip` 解进容器工作区（或 bind-mount `.fe-agent-work` 子目录,注意 DooD 的 `host_workdir` mountinfo 翻译在容器重建后会失效,`mount_failure_hint` 已描述,长命跨重启可达性要验证）。

### 10.2 预览反代：静态 dist → 运行容器

`preview_routes.serve_project_preview` 加分支：**存在 RUNNING `CodeDevSession` 时,反代到 dev 容器 `http://dev-<pid>:5173/` 而非 `send_from_directory` 静态 dist**。两条实现路：

- **推荐**：新增 nginx `location`（dev 变体）+ Flask 代理（仿 `proxy_to_backend`）转 `/preview/<pid>/` → dev 容器；HMR websocket 走 ws 分流（复用 `@app_ws` 的 `map $http_upgrade` + `error_page 418` + `auth_request` + 变量 `proxy_pass` + `resolver 127.0.0.11`)。**须重建 frontend 镜像 + `make redeploy`**（nginx 是静态模板）。
- 鉴权**复用** `preview_identity` + path-scoped cookie（`fe_preview_token`/`fs_app_token`），别新造。

### 10.3 Vite dev server 子路径反代配置（生成工程侧）

生成的前端工程 `vite.config` 须配 `server.hmr`（clientPort/path 对齐反代）+ `base` + `server.allowedHosts`,否则 HMR 在子路径 `/preview/<pid>/` 下静默失败（类比 memory `fe-gen-hash-routing` 的 basename 坑）。**需在 `frontend_project_prompt` 里加硬规则 + validator 守护 + sync Mongo**。

### 10.4 dev → 正式部署过渡(dev 改动毕业到发布版)

`POST /projects/<pid>/deploy` 入口先 `_stop_dev_sessions`（停 dev 容器,不进 rollback 栈）→ 走既有 `code_fullstack_deploy` 原子部署。DB 状态从 `CodeDevSession` 切到 `CodeDeployment RUNNING`。

**关键:部署从「最新源码快照」构建/服务,而不是「dev 之前那版生成 run」**(env `CODE_DEPLOY_FROM_DEV`,默认开;设 `0` 恢复旧「只认生成 run」语义)。dev 每回合/停止会话把工作区源码快照成 `code_frontend_project_zip` / `code_backend_project_zip`(挂在 `code_dev_turn` / `code_dev_backend_turn` run 上,`domain_ref_id=pid`)。部署时:

- **后端**:`deploy_service._resolve_backend_source` 从 {部署修复版 zip、最新 `code_backend_project_zip`(含 dev 快照)} 里**按 `created_at` 取最新**,再 `docker build`。dev 后端改动因此直接进镜像。
- **前端(秒级发布 = 停止前热构建缓存)**:preview 服务的是**构建好的 `site` dist**,而 dev 快照是源码(不含 dist),故需构建。为免每次部署冷装 `npm install`(几分钟),采用**热构建缓存**:
  - **构建发生在温 dev 容器里**(`dev_service.build_dist_in_container`:容器 node_modules 已装,只跑 `vite build --base=/preview/<pid>/` → tar `dist/`,**秒级**),产物缓存成 `code_frontend_dist_zip` 产物(挂 `code_dev_turn` run;新鲜度按 `created_at`)。两个触发点:①**部署 run 首阶段** `deploy_service._harvest_dev_frontend_dist`——趁前端 dev 容器还活着热构建+缓存,然后才拆容器(在**部署 run**内、非 HTTP 请求,不阻塞);②**显式停会话** `dev_routes.stop_session`——停容器前先热构建缓存(「停止前构建」)。
  - **部署取 dist**:`deploy_service._maybe_build_dev_frontend_dist` 优先用**新鲜缓存**(`code_frontend_dist_zip` 的 `created_at ≥ 最新源码快照`)→ 直接落 `site`,**零构建、秒级**;仅当无新鲜缓存(如会话被 idle 回收未缓存)才回退**冷构建** `frontend_dist_builder.build_dist`(精简 `docker run --rm fe-agent` 纯构建,无 claude/无凭证)。产物落**部署 run 的 `site` 目录** + 记 `CodeDeployment.frontend_site_run_id`。**全程 fail-soft**:构建不出 → 回退上次 dist,部署不因此失败。
  - **请求侧**:`fullstack_routes._stop_dev_sessions` 只拆**后端** dev 容器(其源码已逐轮快照,无需热构建);前端容器**留给部署 run** 热构建后再拆——否则会阻塞部署请求且逼成冷构建。
- **preview 选址**:`serve_project_preview` 经 `_resolve_site_dir` 服务「生成 run dist」与「部署构建的 dev dist」里 `created_at` **更新**的那个——所以部署后预览反映的是 dev 里调好的代码,而重新生成前端后又切回最新生成版。**不新增生成 run**(遵循「构建 dist 而非毕业成 run」的取舍)。
- 守护:`tests/test_dev_deploy_promotion.py`(14 条:后端取源优先 dev/开关关回退、前端构建落 `site`+记 `frontend_site_run_id`、无 dev 改动/开关关/构建失败三种跳过、**热缓存命中跳过冷构建 / 陈旧缓存回退冷构建 / harvest 热构建+缓存+停容器 / 无改动跳过热构建仍停 / 缓存产物持久化**、preview 选新址/缺失回退、路由级服务 dev dist)。`CodeDeployment.frontend_site_run_id` 走 `create_all`+`schema_guard` 自动加列(无 Alembic)。
- **生效**:host `.py` 改动 `make redeploy`(热构建在 dev 容器内、冷构建复用 `fe-agent` 镜像,均无需重建;preview 反代逻辑在 Flask,非 nginx,无需重建 frontend 镜像)。docker 端到端构建须在有 Docker 的部署环境验证(单元层已用 monkeypatch 覆盖判定/落盘/选址/缓存新鲜度/harvest 生命周期)。

---

## 11. 风险与权衡

| 风险 | 说明 | 缓解 |
|---|---|---|
| **SSE / gthread 饥饿** | dev 会话是长连接,更吃单 worker;每 iframe/额外流占一条 gthread（memory `sse-thread-starvation`） | 每会话严控流数（对话 + checklist 合流）;配套 `GUNICORN_THREADS`/`PG max_connections`;`maybeReconnect` 40 次退避已有 |
| **长命容器资源累积** | node+vite 比 deploy 的 512m 更吃内存;workdir node_modules 常驻磁盘持续增长 | `reap_idle_sessions` idle 回收 + 每用户上限 + workdir 配额;`--memory` 限额 |
| **进程内状态不可多副本** | event_bus/cancel set/resume 计数全在进程内存（`bus.py` 明说单进程） | 现阶段单 worker 可行;横向扩展前须外置（Redis pub/sub）——列为**非本期** |
| **协作式取消不是抢占** | 打断只在 step 边界/容器 stdout 循环生效,跑一半的模型调用不立即停 | 用户点打断后要等下个检查点;UI 明示「正在停止」;dev 容器不受影响故体感可接受 |
| **合并地狱** | 并行 subagent 改同一 `App.tsx`/路由/store 无自动合并（`code-iterative-generation.md` §2.3） | 只在独立模块并行 + worktree 隔离 + 强制集成回合;冲突则串行 |
| **HMR over 子路径反代** | Vite HMR ws + Host/allowedHosts 校验在子路径常失败 | §10.3 工程侧配置 + ws 分流;上线前真项目验证（对齐 memory `ws-generated-app-support`） |
| **契约 fail-open** | provider 未配时契约走确定性 fallback,可能很粗 | 把契约当后端权威前校验 `contract_status==ready` 且非纯 fallback |
| **凭证长驻暴露面** | 长命容器里 ANTHROPIC/OPENAI key 长期驻留 env + codex auth.json | 评估暴露窗口;gateway 重试代理保活;会话停即清 |
| **checklist 并发覆盖** | 多回合并发写派生态 | 原子写/乐观锁（§7.2） |
| **prompt 生效链路** | 新增/改 prompt 须 sync Mongo（seed 不覆盖已存文档,60s TTL） | 改 `.txt` → `sync_code_prompts.py` → 清缓存;host `.py`/容器脚本改 `make redeploy` 免重建 |
| **nginx 静态模板** | 加 dev location 须重建 frontend 镜像 | 纳入发布清单 |

---

### 11.1 对抗式审查修订（must-fix，已并入实现）

5 视角对抗式审查裁定**无致命缺陷（PROCEED）**,但下列 8 条 serious 必须在实现中落实(已全部并入 P0 代码):

1. **打断留孤儿进程**：`docker exec` 跑 claude 时,`proc.kill()` 只杀本地 exec 客户端,容器内 claude 继续跑 → 取消改为 `docker exec <c> pkill -f 'claude'`(记录/匹配进程) + **会话级串行队列**,起新回合前确认上一回合容器内进程已退出。
2. **网关重试代理在 exec 里失效**：`ANTHROPIC_RETRY_PROXY_BOOTSTRAP` 只在一次 `_CONTAINER_SCRIPT` bash 里起,dev 容器主进程是 `npm run dev`、每回合是独立 `docker exec` → 拿不到改写后的 `127.0.0.1:8788`,直连 flaky 网关(~50% 403)。→ **dev 容器 entrypoint 常驻起一次重试代理 + 容器级写死 `ANTHROPIC_BASE_URL=127.0.0.1:8788`**,后续所有 exec 的 claude 都经代理。
3. **HMR ws 分流是硬前提(非可选)**：Flask `requests` 代理无法 upgrade websocket → 不改 nginx 则 HMR 100% 连不上、实时预览退化为静态。→ nginx `/preview/` **必须**加 `if ($http_upgrade) { return 418; } error_page 418 = @preview_ws;`,`@preview_ws` 用 `resolver 127.0.0.11` + 变量 `proxy_pass http://dev-<pid>:5173`;HTTP 分支仍留 Flask(零 nginx 改动)。**发布清单钉死重建 frontend 镜像**。
4. **DevSession 缺 ledger 持久列**：ledger 只在 `AgentRun.context_ledger_raw`(per-run),FE reload 从 `code_full_generation` 取 → 多回合 `record_user_revision` 累积会被旧 ledger 覆盖清零(composable P1#4 坑)。→ **`CodeDevSession` 加 `shared_ledger_raw` 列**,每回合结束写回会话;reload 顺序 **本会话 session.shared_ledger 优先 → 最新 dev_turn run → 才 fallback full_generation**,永不被 full_generation 覆盖。
5. **决策段无限膨胀挤掉约束**：`record_user_revision` 只 append、`render_for_prompt` max_chars=2400 从底裁剪 → 长会话下 decisions 段膨胀,constraints/框架约束先失效。→ dev 回合注入时**同 stage 相似决策去重/合并 + 加大 max_chars + 把技术栈/框架约束提级为不可裁剪置顶段**。
6. **public 免鉴权放大攻击面**：`/preview/<pid>/` 对 `visibility==public` 匿名放行,今天只暴露静态 dist;若反代到 dev 容器的 Vite dev server 会经 `/@fs/`/source map 泄源码。→ **dev 反代分支强制 owner-only**(is_public 也要 owner token/cookie);public 匿名者只看静态 dist 或 403。
7. **凭证长驻**：`-d --restart` 长命容器里 ANTHROPIC/OPENAI key 长期驻留 + codex auth.json 落盘。→ **per-project 独占容器 `dev-<pid>` + owner-only 反代 + 停即 `docker rm -f` 清 env/auth.json + `reap_idle_sessions` idle 回收**缩短暴露窗口。
8. **每会话单一合并 SSE 流**:对话 + checklist + 容器健康**同一条流**(避免多流放大 gthread 占用);HMR ws 走 nginx `@preview_ws` 直连容器,**不吃 Flask 线程**;idle 自动断开。

## 12. 分期实施建议

> 追加设计:新增能力对现有 7 条 workflow **零改动**（新 workflow / 新表 / 新路由;451 后端单测全过、判定逐字不变）;`CODE_DEV_MODE=0` 为 ops 杀开关（默认开）。

### 12.0 实现状态（本次已交付 P0）

**已实现并验证**(451 后端单测通过 + 前端 tsc/vite 构建 + eslint 通过 + 11 条 dev-mode 专项测试):

| 层 | 交付物 |
|---|---|
| 数据模型 | `models/code/fullstack.py`:`CodeDevSession`(含 `shared_ledger_raw`)、`CodeDevTask`、状态枚举(`create_all` 自动建表) |
| 定价 | `pricing.CODE_DEV_TURN`(env `PRICE_CODE_DEV_TURN`,默认 0)+ `OPERATION` |
| 容器服务 | `services/code/dev_service.py`:长运行容器起/停、`docker exec` 回合、pkill 取消、源码收集、健康探测、日志;含 8 条审查修订(entrypoint 常驻重试代理、exec 覆盖 base_url、只读 seed 防 node_modules 落宿主、merged Vite config) |
| 回合 workflow | `code_dev_turn_workflow.py`:准备(会话优先 ledger + reconcile checklist + 起容器)→ 编辑(exec claude)→ 验证+checklist(房规+验收判定+原子更新);已注册 |
| HTTP | `dev_routes.py`:start/get/turns/checklist/tasks/patch/stop + owner-only `preview-ws/authz` |
| 预览反代 | `preview_routes.py`:owner-only dev server HTTP 反代分支(public 只见静态 dist);`nginx/default.conf`:`/preview` 正则化 + `@preview_ws` HMR 分流 |
| 事件 | `CHECKLIST_UPDATED`/`DEV_PREVIEW_READY`/`DEV_CONTAINER_HEALTH` |
| 前端 | `api/dev.ts`、`stores/devStore.ts`、`DevChecklistPanel.tsx`、`pages/code/CodeDevMode.tsx`(两栏:左对话+右内嵌 iframe 实时预览+「在浏览器打开」按钮+checklist)、`CodeStudio` 入口按钮、`/code/:projectId/dev` 路由、i18n×4 |
| 部署过渡 | `fullstack_routes.start_deploy` 起部署前 `_stop_dev_sessions`(「部署是唯一中断点」) |
| **多 subagent 并行** | `code_dev_parallel_turn` workflow + `dev_service` git worktree 助手(`git_ready`/`create_worktree`/`merge_lane`/`cleanup_worktrees`)+ `POST .../parallel-turns` 端点 + 前端「并行模式」(多行=多分片)。**worktree 隔离 fan-out(队列消费,DB 只主线程写)→ 集成 barrier 合并 → 冲突/失败串行兜底 → 无 git 降级串行**;entrypoint 建 git baseline |
| **运行时硬化** | `dev_maintenance.py`:idle 会话回收(`DEV_MODE_IDLE_REAP_SECONDS`)+ 崩溃探针→有界自愈重启(`DEV_MODE_MAX_HEAL`,超限置 failed)+ 共享 `reconcile_session`(route+daemon 复用);守护线程仅 `serve()` boot 启动。`context_ledger`:`record_user_revision` 去重 + `render_for_prompt` 决策段封顶(`_MAX_RENDER_DECISIONS=30`,防长会话挤掉约束,审查 #5 深修) |
| 测试/文档 | `tests/test_dev_mode.py`(22 条,含并行隔离/降级/冲突/git 命令/idle 回收/崩溃自愈/放弃/ledger 去重封顶);本文档 |

> ⚠️ **运行环境说明**:容器/nginx 链路(dev 容器、HMR ws、反代)按仓库既有 `deploy_service` / `@app_ws` 已验证范式实现,单元层已验证(注册/建表/路由/helper/ledger/checklist);端到端 docker+nginx 联调须在有 Docker 的部署环境 `make redeploy`(重建 frontend 镜像使 nginx `@preview_ws` 生效;host 侧 `.py` 免重建;无新增 prompt 无需 sync Mongo)。

**运行时硬化已交付**:idle 容器回收 + 崩溃探针有界自愈 + ledger 决策去重/渲染封顶(审查 #5 深修)。

**尚未实现(刻意延后):** 命名检查点回退(git tag 里程碑)、后端开发模式对称(be-agent lane)、前后端双会话并发、dev 回合质量 eval。

> **多 subagent 并行开发已交付**(原 P2):env `DEV_MODE_MAX_PARALLEL`(默认 4)封顶。真正并行须容器内有 git —— fe-agent 基础镜像 `node:22`(buildpack-deps)**已内置 git,开箱即用**;若改用 `node:22-slim`/`alpine` 需自行装 git,否则**安全降级为串行**(仍全部完成,不报错)。前端「并行」开关:每行一个功能 = 一个隔离分片。

**P0 — 前端开发模式最小闭环（单会话、串行回合、实时预览）—— 已交付 ✅**
1. `CodeDevSession` + `CodeDevTask` 两张表（`create_all`）。
2. `dev_service.start_session/exec_turn/stop_session`（dev 容器 + edit-mode 回合,复用 `_run_container` + `frontend_project_service` edit-mode）。
3. `code_dev_turn` workflow（edit → verify → checklist,复用 `_verify_support`）+ 注册/白名单/pricing。
4. `dev_routes.py` 端点 + 预览反代分支（nginx dev location + ws 分流）+ Vite 子路径配置 + prompt 硬规则。
5. 前端 `CodeDevMode.tsx` 两栏页 + 入口按钮 + `DevChecklistPanel` + i18n。
6. 无代码 → 脚手架 + checklist 播种。

**P1 — 交互增强**
1. 打断/继续/修订完整语义（含 `record_user_revision` 联动文档同步 + 按 impact 重合成契约）。
2. 崩溃自愈周期探针（§6.3）。
3. 命名检查点/回退（git tag 里程碑）。
4. 后端开发模式对称（`be-agent` + 后端 edit-mode + 契约前置）。

**P2 — 并行与治理**
1. 多 subagent 并行（独立模块 + worktree 隔离 + 集成 barrier）。
2. `reap_idle_sessions` 资源治理 + 每用户上限。
3. dev 会话质量样本（`CodeQualitySample kind='dev_turn'`）+ eval。
4. （若上生产多副本）进程内状态外置。

---

## 13. 待决策问题（实施前需确认）

1. **右栏预览默认形态**：**已定 —— 受控 iframe 内嵌 + 顶部「在浏览器打开」按钮**（`window.open('/preview/<pid>/?token=')`，复用 `CodeAppPreview.openProjectPreview`）。iframe 提供沉浸式实时预览，按钮兜底新标签全屏查看。
2. **dev 容器与源码物化方式**：bind-mount `.fe-agent-work` 子目录（跨重启可达性需验证）vs 首回合解 zip 进容器内 fs（重启丢失,需回灌）。建议 bind-mount + 验证。
3. **并行 subagent 隔离粒度**：独立 dev 容器副本（重,但彻底隔离）vs 同容器多 git worktree（轻,但共享 node_modules/端口）。建议 worktree,P2 再定。
4. **计费模型**：按回合（清晰,建议）vs 按容器运行时长（更贴成本,但需时长计量）。P0 按回合、默认 0。
5. **一 project 多并发 dev 会话**：是否允许（前端+后端同时开发模式）。建议 P0 单会话、P2 放开。

---

## 13.9 真机 E2E 验证记录（2026-07-01）

在平台**真实运行环境**(docker + 全栈)对 Dev Mode 做端到端验证,抓到并修复 **3 个真机 bug**:

1. **`/work` 权限崩溃(容器崩溃循环)**：`_WORK="/work"` 在根目录,非 root `node` 用户建不了 → npm install 找不到 package.json → `npx --no-install vite` 取消 → 崩溃循环。**修**:工作区移到可写的 `/tmp/work`(worktree `/tmp/work-lanes`)。构建容器一直用 `/tmp/work` 正是此因。
2. **`collect_source` 损坏 dotfile**：`member.name.lstrip("./")` 会把 `.gitignore`→`gitignore`、`.seeded`→`seeded`。**修**:精确剥 `./` 前缀 + 排除内部 `.seeded` 哨兵。
3. **崩溃循环不自愈(设计 gap)**：`dev_maintenance` 只自愈"容器消失",对"容器还在但崩溃循环"的旧 entrypoint 永远接管不了(代码修复后旧容器仍靠 `--restart` 循环);且 `--restart` 容器有**瞬时 running**会让检测误判。**修**:崩溃循环判据改 `restart_count>=DEV_MODE_CRASHLOOP_RESTARTS && !health_check()` → `start_container` 删旧重建(用当前代码接管);heal 放弃时 `stop_container` 终止循环。
4. **非 Vite 源码 seed 崩溃**：真实项目的上一段 frontend 产物**不一定是可运行的 Vite 工程**(脚手架残桩:只有 package.json+docs,无 `src/`、无 vite),`_resolve_source` 见非空就当"有代码" → 装不上 vite → `npx vite` 取消 → 崩溃循环。**修**:`_is_runnable_vite()` 校验(有 vite dep/dev script + 有源码模块),不合格回退 `_MINIMAL_SCAFFOLD`(这正是"无代码→脚手架"应走的路);entrypoint 再加"装完无 vite 则补装 vite@^5"兜底。
5. **【用户实际撞到】预览反代分支顺序错**:dev 反代分支放在了 `?token=→cookie→302` 交换**之前**,导致 owner 带 token 进来时直接被反代给 Vite 返回 index.html 却**从未种 cookie**;随后 index.html 的模块子请求(`/preview/<pid>/src/App.tsx`、`/@vite/client`)既无 token 又无 cookie → 落到 public/静态路径 → 404(用户看到的"跨域/资源不存在")。**修**:把 token→cookie→302 交换移到 dev 反代之前;重定向后的(带 cookie)请求及所有模块请求都以 owner 身份走 dev 反代。**验证**:入口 `?token=`→302+Set-Cookie,`/preview/<pid>/`→Vite HTML,`/preview/<pid>/src/App.tsx`→**200 text/javascript**(全程经真实 nginx,已通)。

**已验证通过(生产真机)**:dev_service 全链路(宿主 + 后端容器内 DooD:TMPDIR→bind mount→`host_workdir` 翻译,6~9s 健康零重启)、`exec_turn` 真实 claude 编辑(精确改文件)、git worktree 并行(node:22 内置 git,真并行开箱即用)、**Vite dev server 真实服务**(`/`→302 base、`/preview/<pid>/` 返回含 `/@vite/client` 的页面,HMR 客户端已注入)、崩溃会话**自愈恢复 PASS**(原崩溃 40+ 次的真实会话 3a3223 → 6s 健康)、后端 `make redeploy`(后端代码**打包进镜像非挂载**,改 host `.py` 须重建)、前端 dev UI + nginx `@preview_ws` 已在线、nginx `/preview` 正则路由。dev 测试增至 **24 条**。

**认证预览全链路已服务端验证**(经真实 nginx,用运行后端配置签发的 owner token):入口 `?token=`→302+双 cookie、`/preview/<pid>/`→Vite HTML、模块 `src/App.tsx`→200 text/javascript、`preview-ws/authz`→204+`X-App-Upstream=dev-<pid>:5173`。

**【用户反馈:停止会话再进入会开始重新写代码】第 9 个真机 bug — 已修复**。真凶:`stop_container` 做 `docker rm -f`,**dev 容器 `/work`(用户在会话里写的代码)在容器文件系统里被彻底删除**;再进会话时 `_resolve_source`→`load_prior_source` 只能从**旧的** `code_frontend_project_zip`(dev 前的构建/脚手架残桩)恢复 → 看起来"从头重写"。根因:**dev 容器工作产物从没落到 durable 存储**。**修**:①每回合(`dev_verify`,有编辑时)把 `dev.collect_source` 的源码 zip 成 **`code_frontend_project_zip` 产物**(`persist_source_snapshot`,`domain_ref_id=pid`)——`load_prior_source` 取 newest,故再进即恢复用户所写;②`stop_session` 停容器**前**也 `persist_snapshot_standalone`(安全网,无 step);③并行 workflow 同样持久化。**关键约束:dev 容器 `/work` 在容器 fs,`docker restart` 保留、`docker rm` 丢失**——所以自愈/重启用 restart,而停止会话必须先快照再 rm。回归 `test_dev_source_snapshot_zip_and_artifact`。(现存会话的游戏已一次性抢救持久化。)

**【用户反馈:`@react-refresh` 500 / 改了框架配置预览报错】第 8 个真机 bug — 已修复**。场景:agent 把项目从 React 改成 vanilla JS(改 `vite.config.ts` 去掉 react 插件、`package.json` 移除 `@vitejs/plugin-react`),但**运行中的 Vite 仍挂着旧 react 插件**(读已卸载的 `refresh-runtime.js` → `/@react-refresh` 500)。根因:Vite 只在**进程重启**时重读配置,且我的 merged dev config 经 `loadConfigFromFile` **间接**加载项目 `vite.config`(Vite 不监听它),故 config 改了 Vite 不重启。**修**:①entrypoint 改为**总是 `npm install`**(重启即装新/去旧依赖);②`dev_service.restart_dev_server`(**先显式 `npm install` 再 `docker restart`**——restart 保留容器 fs/`.seeded` 守卫,游戏代码不丢;rm 才会丢,故绝不用 rm);③`code_dev_turn`/并行 workflow 在 `on_event` 里检测本回合是否写了 `package.json`/`vite.config.*`(`_is_restart_trigger`),是则编辑后 `restart_dev_server` + 发 `DEV_PREVIEW_READY`。回归 `test_is_restart_trigger` + `test_config_change_triggers_dev_server_restart`。

**【用户反馈:代码改动不体现在预览】第 7 个真机 bug — 已修复**。真凶:预览 iframe 的 `?token=` 是**页面挂载时**的 access token,30 分钟后过期(或被 axios 401 自动刷新替换)。`preview_routes` 旧逻辑 `token = query_token or cookie` 让**过期的 query_token 盖过了仍有效的 cookie** → `is_owner=false` → dev 反代分支(owner-only)跳过 → **掉到旧静态 dist**(上一段 `code_frontend_project_generation` 的 built 产物,标题/内容都是旧的),于是"dev 改动不显示"。**修**:query_token 认证失败时**回退用 cookie**(`preview_identity(query_token) or preview_identity(cookie_token)`);token→cookie→302 交换仅当 query_token 自身认证为 owner 时才做(过期 token 走 cookie 分支不再触发,避免重定向循环)。验证:有效 cookie + 无效 query_token → 现服务实时 dev(`@vite/client`),此前是旧静态。回归 `test_stale_query_token_falls_back_to_cookie`。

**HMR 即时热更 ws — 已修复并端到端验证**(第 6 个真机 bug)。根因两层:
1. **`hmr.path` 把 ws 服务器挪到错端口**:Vite 5 里设了 `hmr.path` 但没设 `hmr.port` → 为 HMR 建**独立 ws 服务器**,端口默认成 `hmr.port=clientPort=443`(不在 dev 端口 5173),nginx 转发到 5173 的 ws 全部落空(直连各路径握手均超时)。**修**:从 Vite 配置**移除 `hmr.path`** → ws 挂主 dev-server 端口(5173),按 `vite-hmr` 子协议 + `base` 派生路径工作;客户端仍连 `wss://<host>:443/preview/<pid>/`(clientPort/protocol/base 决定)。
2. **Vite CVE-2025-24010 的 ws CSRF token 校验**:`shouldHandle` 里凡带 `Origin` 头(浏览器)的 ws 必须 URL 带 Vite 自签的 `?token=<wsToken>`(注入在 `/@vite/client`),否则 400。真实 Vite 客户端**自动带**该 token;此前 CLI 探针因未带 token 全部 400,一度误判为"连不上"。
**验证**(node `ws` 浏览器级握手,走完整真实路径):`fetch /@vite/client`(cookie)→拿 wsToken → `ws://…/preview/<pid>/?token=<wsToken>`(Origin + fe_preview_token cookie)→ **101 CONNECTED**(nginx `@preview_ws` → authz → dev 容器 → Vite 全通)。`allowedHosts:true` 令 Host 校验放行。此外 `CodeDevMode` 每回合完成仍会 remount iframe 作双保险。

## 13.10 通用规范化(2026-07-01)——从「修游戏这个会话」提升到「所有 web 产出都受保护」

前述真机 bug 的修复里,有几处最初只覆盖了「当时撞到的那一种技术栈/那一个会话」。按「其他产出的 web 产品可能也有同样问题」的要求,把它们**从个案补丁提升为通用规范**,不再依赖「恰好是 Vite+React 游戏」或「恰好是用户这个会话」:

1. **重启触发器改为「框架无关」(第 8 个 bug 的通用化)**。原 `_is_restart_trigger` 只认 `package.json` / `vite.config.*`——只有生成 Vite 工程才受保护。现 `_RESTART_TRIGGER_BASENAMES` 覆盖:
   - 依赖清单 + 各包管理器锁文件(`package.json` / `package-lock.json` / `pnpm-lock.yaml` / `yarn.lock` / `bun.lockb`);
   - 各主流 dev-server / 构建配置(Vite / Vue / Svelte / Astro / Nuxt / Next / Remix / Webpack / Rollup 的 `*.config.{js,ts,mjs,cjs}`);
   - CSS 管线(`tailwind.config.*` / `postcss.config.*`,dev-server 启动时读取);
   - TS/JS 配置(`tsconfig.json` / `jsconfig.json`);
   - `.env` / `.env.*`(dev-server 只在启动时读一次,改了必须重启才生效)。
   任何产物只要改了这些「进程启动时读一次」的文件,回合结束都会 `restart_dev_server`(先 `npm install` 再 `docker restart`,保留容器 fs),预览立即反映新配置——而不再是「只有游戏这类 Vite 项目才会重启」。

2. **快照保护改为「每个运行中的 dev 会话」(第 9 个 bug 的通用化)**。每回合 + 停止会话的持久化本就是通用的;缺口在于「**在持久化上线之前**就已经在开发的存量会话」——它们的 `/work` 仍只活在容器 fs 里,一旦 idle 回收 / 崩溃重建就丢。原先只对游戏那个会话做了一次性抢救。现 `dev_maintenance.backfill_snapshots(app)` 在**守护进程启动时对每个 RUNNING 会话各快照一次**(best-effort,`_snapshot_session` → `collect_source` → `code_frontend_project_zip` 产物),且 idle 回收分支在 `stop_container`(`rm -f`)**之前**也先 `_snapshot_session`。于是「任何」在飞会话的工作都在被回收/重建前落到 durable 存储,再进入即恢复——不限某一个项目。

3. **(既有的通用面复述)** 非 Vite 源码回退 `_MINIMAL_SCAFFOLD`(`is_runnable_vite`)、过期 query_token 回退 cookie(`preview_routes`)、崩溃循环判据 `restart_count>=阈值 && !health_check` 本就是按「任意项目」实现的,无需再改。

**测试**:`test_is_restart_trigger` 扩充为覆盖 Next/Nuxt/Svelte/Vue/Astro/Tailwind/PostCSS/tsconfig/`.env.*` 等全套(并反证 `src/App.tsx`/`index.html`/`src/env.ts` 不误触发);新增 `test_backfill_snapshots_running_sessions`。dev 测试 **29 条**、后端全量 **470 passed** 零回归。

## 13.11 刷新页面重连在飞回合(2026-07-01)

**用户反馈:开发阶段刷新页面不能重连执行过程,对结果无感知。** 真凶:`start_session`/`start_backend_session` 在已有会话时**无条件新起一个 bootstrap「恢复」run**(`{bootstrap:true}`),而 `_session_view.latest_run_id` 与前端 attach 目标都取「最新的 turn run」——bootstrap 一起就成了最新,把**正在跑的回合 run 盖住**;前端 `devStore.start` 又优先 attach `run_id`(=bootstrap),于是刷新后 SSE 挂到一个空 bootstrap,在飞回合的过程与结果全看不到(回合仍在后端跑,只是 UI 失联)。

**修**(`dev_routes`):resume 分支先 `_reconcile_session`(docker inspect 同步真实状态),**容器仍在(status∈ACTIVE)→ 重挂到在飞/最新回合 run**(新 helper `_active_or_latest_turn_run_id`:优先 IN-FLIGHT 的 `code_dev_turn`/`code_dev_parallel_turn`/`code_dev_backend_turn`,否则最新一条 → SSE 重放其已存事件=完整过程+结果),**不再起 bootstrap**(否则自我遮蔽);仅当**容器已消失**(idle 回收/崩溃 → reconcile 置 STOPPED)才 revive+bootstrap 重启容器(复用同一会话+清单)。`_latest_turn_run_id` 同步扩为覆盖并行回合。前端 `CodeDevMode` 补一个从 `run.title` 派生的「本次请求」气泡(刷新后本地 `userMsgs` 丢失,恢复对话上下文)。

**守护**:`tests/test_dev_reconnect.py`(4 条:前端在飞回合/最新完成回合/在飞并行回合、后端在飞回合——均断言 `run_id`==该回合且**不新增 run**;docker 经 monkeypatch)。后端全量 **499 passed** 零回归;前端 tsc 净。**生效**:纯 host `.py`+前端,`make redeploy` 即生效,无需重建镜像 / 无新 prompt。

## 14. 后端开发模式(全栈联调 · 最小闭环,2026-07-01)

前端开发模式(§1–§13)是一个长运行 Vite 容器 + HMR。**后端开发模式是它的「对称孪生」**:一个长运行的后端容器 `dev-be-<pid>`,以**原生热重载**方式(uvicorn --reload / nodemon / flask --debug / go build / mvn spring-boot:run)运行**生成的后端源码**,而不是构建生产镜像 —— 后端版的 HMR。部署(`deploy_service`)仍是「毕业到生产」的独立路径(`docker build` → `app-<pid>`),契约(`CodeProjectLedger`)始终是前后端边界的唯一真值源。

**为什么不挂进前端容器**:容器只有一个主进程(前端那个就是 `npm run dev`),前端镜像是纯 Node;后端是 polyglot(Python/Go/Java/Node)且需要数据库。所以后端有**自己的容器**(用工具链齐全的 `be-agent` 镜像),在共享网络上,连一个**隔离的 dev 数据库命名空间**(`app_<hex>dev`,与部署库 `app_<hex>` 分开,dev 实验绝不污染线上数据)。

**数据模型零新增**:`CodeDevSession` 早有 `lane`(`frontend|backend`)列,后端 dev 会话 = 一条 `lane="backend"` 的会话,持有 `dev-be-<pid>` 容器。每 (project, lane) 至多一个 ACTIVE 会话,前后端会话可并存。

**落点**:
- `services/code/dev_backend_service.py` —— 后端 dev 容器生命周期(方法名对齐 `DevService`,便于按 lane 统一驱动)。per-stack entrypoint:探测栈→装依赖→热重载启动,**项目自带的 `dev-start.sh` 永远优先**(让编码 agent 自行规范化可运行的 dev 入口,是覆盖 polyglot 车队最可靠的做法);无栈时起一个占位 `/health` server 让容器活着等 bootstrap。注入 deploy 同款 env(`PORT`/`DATABASE_URL`+async 驱动探测/`REDIS_*`)。无先前后端产物时 seed 一个最小可运行的 Node+Express 脚手架(带 `/health`+读 `$PORT`)。
- `workflows/code_dev_backend_turn_workflow.py` —— 后端回合(prepare→edit→verify)。edit 以**共享 OpenAPI 契约**为准编辑后端;写了 `dev-start.sh`/依赖清单(`requirements.txt`/`go.mod`/`pom.xml`/`package.json`…)/`.env`/`Dockerfile` 触发容器重启(`_is_be_restart_trigger`,框架无关)。verify = 后端房规 + FR 编码的验收评审(复用 `backend_project_service.review_project` → checklist)+ **契约驱动的集成测试**(复用 `integration_test_service.run_integration_tests`,把靶子换成 dev 容器)+ 快照 `code_backend_project_zip`(再进即恢复,也供部署)。
- `routes/code/preview_routes.py` —— `/preview/<pid>/api/*` 反代到 `dev-be-<pid>`(owner-only,剥前缀 + 404 时 `/api` 前缀重试);当后端 dev 会话在跑时,把前端 dev 容器返回的 HTML 注入 `window.__API_BASE__=/preview/<pid>/api` → **运行中的前端直接调用运行中的后端**(全栈联调闭环)。`_running_dev_session` 改 lane 感知。
- `routes/code/dev_routes.py` —— `POST .../dev-backend-sessions`(启/恢复后端会话)、`POST .../run-tests`(纯测试回合)、共享端点按 `session.lane` 选 workflow/service/快照;HMR ws authz 钉死 frontend lane。
- `services/code/dev_maintenance.py` —— reap/heal/snapshot/backfill 全部 `_dev_for(session)` 按 lane 选服务;heal 用 lane 对应的 seed 源。
- 前端 `pages/code/CodeDevMode.tsx` —— 底部「后端联调」条:启用/停止、发后端指令、运行测试、后端功能进度;后端 checklist 事件按 `payload.lane` 路由到独立的后端进度,不覆盖前端清单。`api/dev.ts`+`devStore.ts`+i18n×4。

**取舍(诚实说明)**:后端热载可靠性 per-language —— Node/Python/Go 秒级,**Java/Maven 重启偏慢**(十几秒,但仍是容器内重启、非镜像重建,远快于 deploy)。**业务 WebSocket 在 dev 联调下未接**(nginx `@preview_ws` 只路由到前端 Vite HMR;后端业务 ws 的前缀重写留待后续)——dev 迭代以 HTTP REST 为主,ws 业务在部署后验证。

**测试/验证**:`test_dev_mode.py` 新增 10 条(注册/命名/DB 隔离/entrypoint polyglot/脚手架/重启触发器/lane 选择/回合编辑+验证+快照/纯测试回合);后端全量 **480 passed** 零回归;前端 tsc+build+eslint 通过。

## 15. Sprint 调度器(多回合持久开发器 · P0 串行,2026-07-02)

把「一次性生成工程 zip」升级为**多回合持久开发器**的第一期:代码现场在长运行 dev 容器里,**任务状态在 DB 里**(容器不维护 backlog),调度器按任务列表逐项喂给 `code_dev_turn`,每轮验收后更新任务状态、继续下一回合。这是对「产出规模被单会话钳位」根因的结构性解法:规模不再受一段 CLI 会话的产出预算限制,而是随任务板线性增长。

**任务状态机(`CodeDevTask`,持久化)**:`pending → queued → in_progress → verifying → done`;验收未过且有重试预算 → 回 `pending`(retry_count+1,失败原因写 note 供下轮定向修复);重试用尽 → `blocked`;run 级异常 → `failed`;用户取消 → `cancelled`。`ready` 是派生态(pending 且 depends_on 全部 done),不入库。所有流转都是 **`UPDATE … WHERE status IN (expected)` 原子条件更新**(认领防双发;sqlite/PG 通用,等效 SKIP LOCKED)。新增字段:`parent_feature_id`/`lane`/`acceptance_criteria_raw`/`depends_on_raw`/`resource_spec_raw`/`priority`/`retry_count`/`max_retries`/`blocked_reason`/`last_attempt_run_id`(全部 nullable,schema_guard 自愈加列)。

**Sprint(`CodeDevSprint` + workflow `code_dev_sprint`)**:`planned → running → completed|blocked|failed`,`running → pausing → paused → running`(pause 等当前回合收尾),任意非终态可 `cancelled`。调度器是**无状态编排 run**:每轮循环从 DB 重读 sprint+任务板,认领一个 ready 任务(priority DESC、同优先级 asset 先行、order_index ASC)→ 创建子 `code_dev_turn` run → **在本线程同步驱动**(`agent_runtime.run_sync`,不占第二个 executor 槽、不轮询,完整复用 recorder/SSE/计费/退款)→ 按结果推进状态机。保护:回合预算 `max_turns`(env `CODE_DEV_SPRINT_MAX_TURNS`,默认 24)、停滞闸(连续 N 回合无新 done → blocked,`CODE_DEV_SPRINT_STALL` 默认 4)、连续 run 级失败 ≥2 → sprint failed(系统性故障不烧 backlog)。因无状态,`code_dev_sprint` 加入 `RESUME_FROM_SCRATCH`:服务重启自动重入续跑(在飞子回合被 orphan-fail 后由 `reconcile_stale_tasks` 打回重试)。

**每轮只喂一个小任务**:`build_task_brief` 生成「任务 ID/标题/验收标准/依赖(已完成,勿重做)/上次失败原因/禁止事项(不重写工程、不破坏已完成任务、不引远程资源)」的定向简报,绝不把整个 backlog 塞给 agent。`code_dev_turn` 带 `task_id` 时走**按任务验收**:评审清单 = 本任务的逐条验收标准(`<fid>.ACn`,初始全 false)+ 已 done 任务的回归集(初始 true,被评审判 false 即回归);done 条件 = AC 全过 + 无回归 + 无阻断(房规/运行时/评审 blocking)。验收未过触发一次回合内定向修复(原 `_DEV_REPAIR` 扩展),仍未过才消耗跨回合重试。资源任务(`category=asset`,`resource_spec` 带 skill+outputs 清单)进同一状态机,简报明确要求调 image-assets 技能并 ls 验证产物。

**API(`dev_routes`)**:`POST …/tasks/bulk`(批量写入/按 feature_id upsert;done/在飞行任务不被覆盖;replace 整板覆盖在 sprint 活跃或有在飞任务时拒绝)、`POST …/sprints`(创建并启动;每会话仅一个非终态 sprint;P0 仅 serial+frontend lane)、`GET …/sprints/<id>`、`pause`(→pausing,回合收尾后 paused,run 进 PAUSED 不占并发额度)、`resume`(pausing 撤回/paused 重派;编排 run 已死则换绑新 run)、`cancel`(级联取消在飞子回合+释放认领)。任务 PATCH 只接受用户词汇(pending/in_progress/done/skipped;设 pending 清除 blocked_reason = 人工解锁重排),调度器态不可伪造。

**分期**:P0(本期)= 状态机 + 串行 sprint + 按任务验收;P1 = backlog planner(FR/NFR/契约自动拆任务,用户确认后 bulk 入库);P2 = asset 任务的 gen-assets 共享注入与产物校验;P3 = 并行 sprint(复用 `code_dev_parallel_turn` + 冲突判定 + barrier 全局回归);P4 = 全栈 sprint(前后端会话联动 + 契约/集成测试任务)。

**测试**:`test_dev_sprint.py` 21 条(注册/原子认领/依赖 ready 与死依赖自动 block/验收 done-retry-blocked/回归拦截/简报内容/对账/串行循环端到端(fake 子回合)/暂停/连续失败熔断/bulk 与 sprint API/PATCH 越权拒绝);全量 **520 passed** 零回归。

## 附录 A：关键代码锚点速查

| 关注点 | 权威位置 |
|---|---|
| 运行时/回合 run | `backend/services/agent/runtime.py`（`_execute`/`request_cancel`/`register_workflow`/`reconcile_orphaned_runs`） |
| SSE/回放 | `recorder.py`（`emit`/`emit_delta`）、`bus.py`、`agent_routes.py::_event_stream` |
| 人在环/取消 | `agent_routes.py::resume_run/cancel_run`、`AgentRunStatus.PAUSED` |
| 编辑模式续改 | `frontend_project_service.py::_seed_base`/`_build_prompt`/`_CONTAINER_SCRIPT`、`_iteration_support.load_prior_source` |
| 长运行容器范式 | `deploy_service.py::_run_container`/`resolve_proxy_target`/`probe_health`/`container_logs`/`stop_deployment` |
| 预览/反代/鉴权 | `preview_routes.py::serve_project_preview`、`fullstack_routes.py::proxy_to_backend`/`app_ws_authz`、`frontend/nginx/default.conf` |
| 验证→修复/验收/评审 | `_verify_support.py`（`Verification`/`features_from_ledger`/`should_stop`/`split_batches`/`run_reviewers`/`repair_regressed`） |
| 房规 linter | `house_rules.py`（`check_frontend`/`check_backend`） |
| context ledger | `context_ledger.py`（`seed_from_inputs`/`merge`/`render_for_prompt`/`record_user_revision`）、`context_verifier.py` |
| 文档版本/修订 | `version_service.py`、`generation_service.py::revise_*`、`code_workflow.py` revise 门 |
| 契约前置 | `contract_service.py::ensure_contract`/`render_contract_for_prompt`、`CodeProjectLedger` |
| 二次开发状态机 | `code_app_iteration_workflow.py`、`apps_routes.py::_reconcile_iteration`、`CodeAppIteration` |
| 前端工作台 | `CodeStudio.tsx`、`agentStore.ts`、`ConversationRail.tsx`、`CodeAppPreview.tsx`、`fullstackStore.ts`、`CodeFullstackPanel.tsx`、`IterationPanel.tsx` |
| 质量 eval | `quality_metrics.py`、`models/code/quality.py`、`eval_review.py` |
| Sprint 调度器 | `dev_sprint_service.py`（状态机/认领/验收折叠/简报）、`code_dev_sprint_workflow.py`（串行循环）、`runtime.py::run_sync`、`dev_routes.py`（tasks/bulk + sprints CRUD）、`CodeDevSprint`/`DevTaskStatus`/`DevSprintStatus` |
