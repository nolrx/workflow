# Code 域交接文档（设计 / 定义 / 全流程 / 已知问题）

> 面向**接手 Code 域使用与二次开发**的团队。本文是当前代码的权威说明，凡与 `README.md`、`AGENTS.md` 冲突，以本文和 `CLAUDE.md` 为准（那两份已落后于代码）。
> 适用范围：`/api/code` + 共享的 `/api/agent`（Agent Swarm）+ 前端 `frontend/src/{pages,components,stores,api}` 的 code/agent 部分 + 共享底座（auth/team/credit/ai）。PPT、RedBook 两域当前版本不启用，仅作镜像参考。

---

## 0. TL;DR（一页认知）

- Code 域有**两条生成入口**，对同一份 `CodeProject` 操作：
  1. **同步路由** `/api/code/projects/...`：一次 HTTP 请求 = 一次模型调用，立即返回（无回放、无时间线）。适合单步重算与脚本化。
  2. **Agent Swarm** `/api/agent/runs`（当前前端主用）：把整条流水线变成可观测、可回放、可人工确认、可恢复的 `AgentRun`，通过 SSE 实时推送。
- **三个已注册 workflow**：
  | workflow key | 步骤 | 产物 | 状态 |
  |---|---|---|---|
  | `code_full_generation` | planner→requirements→flow→documents→style→preview→publisher（7 步，**人工确认门控 + 可恢复**） | 需求/流程/文档/风格/预览图，写回 `CodeProject`/`CodeDocument` | ✅ 当前主流程 |
  | `code_frontend_project_generation` | fe_planner→fe_project_build→fe_publish（3 步，**容器化 agent**） | 多文件 React+Vite+TS 工程（源码 zip + 可预览 dist） | ✅ 当前前端生成默认 |
  | `code_frontend_generation` | fe_planner→fe_build→fe_critic→(fe_repair)→fe_publish | **单文件 HTML** app | ⚠️ 遗留，仅供旧 run 回放，前端不再主动触发 |
- **⚠️ CLAUDE.md 关于前端生成的描述已过时**：它写 `code_frontend_generation` 产出「Sandpack 文件 map（React+TS+纯 CSS）」——实际上该 workflow 产出**单文件 HTML**，且已被容器化的 `code_frontend_project_generation` 取代为默认。预览也不是 Sandpack，而是 `<iframe>` 指向 `/api/agent/runs/<id>/site/`。详见 §5、§11。
- **Code 域当前计费默认全为 0**（免费），但所有扣费都走 `charge()`/`refund_credits()`，设 `PRICE_CODE_*` 环境变量即可开启计量，无需改代码。详见 §9。

---

## 1. 架构分层

```
┌─────────────────────────────────────────────────────────────────┐
│ 前端 React/Vite                                                   │
│  pages/code/CodeStudio.tsx  ── 会话式工作台（单列对话 + 预览栏）  │
│  stores/codeStore.ts        ── /api/code 同步 CRUD/生成           │
│  stores/agentStore.ts       ── /api/agent run 生命周期 + SSE      │
│  components/code/*, agent/*  ── 时间线、产物卡、划选修订、版本弹窗 │
└───────────────┬─────────────────────────────┬───────────────────┘
                │ /api/code (同步)              │ /api/agent (SSE/回放)
┌───────────────▼──────────────┐  ┌────────────▼────────────────────┐
│ routes/code/project_routes.py│  │ routes/agent_routes.py           │
│  CodeProject CRUD / 各 stage │  │  POST/GET runs, stream, cancel,  │
│  生成 / 版本历史 / 局部修订  │  │  resume, artifacts/file, site/   │
└───────────────┬──────────────┘  └────────────┬────────────────────┘
                │                                │ runtime.start(app, run_id)
┌───────────────▼───────────────┐  ┌────────────▼────────────────────┐
│ services/code/*               │  │ services/agent/                 │
│  generation_service           │  │  runtime(线程池) / recorder /   │
│  version_service              │  │  bus(SSE) / context_ledger /    │
│  frontend_build_service       │  │  context_verifier / files /     │
│  frontend_project_service     │  │  workflows/code_*               │
│  styles                       │  │                                 │
└───────────────┬───────────────┘  └────────────┬────────────────────┘
                └──────────────┬────────────────┘
                ┌──────────────▼────────────────┐
                │ services/ai (capability 路由) │  services/pricing + credit_service
                │  get_text_provider/image      │  services/prompt_library (角色前缀)
                └───────────────────────────────┘
                ┌──────────────▼────────────────┐
                │ models: code/{project,        │
                │  stage_version}, agent/{run,  │
                │  step,event,artifact}         │
                └───────────────────────────────┘
```

蓝图注册（`backend/app.py`）：`agent_bp → /api/agent`、`code_project_bp → /api/code`。

两条入口的关系：**Agent Swarm 内部也是调用 `services/code` 的同一批方法**（`get_code_generation_service()` 等），只是包了一层 step/event/artifact 记录 + ledger + 人工门控。所以业务生成逻辑只有一份，不会漂移。

---

## 2. 数据模型

主键统一为 `String(36)` UUID；多租户字段 `user_id`(必填) + `team_id`(可空) + `visibility`(`private|team|public`)；结构化内容以 **JSON-in-Text** 存储，**必须走模型上的 getter/setter**，不要直接读裸列。

### 2.1 `CodeProject`（表 `code_projects`，`models/code/project.py`）
业务主体，承载五个 stage 的「当前生效内容」。

关键列：`title`、`requirement_input`(初始需求)、`requirements_doc`、`development_flow`、`style_prompt`、`ui_baseline_prompt`、`confirmed_preview_url`、`selected_style_ids_raw`(JSON)、`preview_images_raw`(JSON，含 base64 data URL)、`status`。

- JSON helpers：`get/set_selected_style_ids()`、`get/set_preview_images()`。
- 状态枚举 `CodeProjectStatus`：`REQUIREMENT_READY → FLOW_READY → DOCUMENTS_READY → STYLE_READY → PREVIEW_READY → UI_CONFIRMED`。
- 关系：`documents`（→`CodeDocument`，cascade delete-orphan，按 `order_index` 排序）、`stage_versions`（→`CodeStageVersion`，cascade delete-orphan）。
- ⚠️ `project.documents` 带 `order_by`，在 SQLAlchemy 2.x 下对它直接 `.delete()` 会抛错——工作流里改用 `CodeDocument.query.filter_by(project_id=...).delete()`（见 `code_workflow.py:570`、`version_service.py:265`）。同步路由 `split_documents` 仍用 `project.documents.delete()`（`project_routes.py:225`），是潜在隐患（见 §16）。

### 2.2 `CodeDocument`（表 `code_documents`）
拆分后的可编辑开发文档。列：`document_type`、`title`、`content`、`prompt_expert`(每篇附带的提示词专家建议)、`order_index`。无版本表，历史靠 `CodeStageVersion` 的 DOCUMENTS stage 整组快照。

### 2.3 `CodeStageVersion`（表 `code_stage_versions`，`models/code/stage_version.py`）—— 分阶段版本历史
镜像 PPT 的 `PPTPageImageVersion` 模式。**不变式**：每个 `(project_id, stage)` 至多一行 `is_current=True`，其内容与 `CodeProject` 对应字段同步。

- `stage` ∈ `CodeStage.ALL = (requirements, flow, documents, style, preview)`。
- `version_number`：同一 `(project, stage)` 内自增，用 `MAX(version_number)+1` 计算（`version_service.py:165`）。
- `source` ∈ `CodeStageVersionSource`：`generated|manual_edit|partial_revision|rollback|import`。
- `content_text`（requirements/flow/style 文本）/ `content_json_raw`（documents/preview/style-id 结构化）/ `summary` / `run_id` / `step_id` / `note`。
- JSON helper：`get/set_content_json()`。

### 2.4 Agent Swarm 模型（`models/agent/*`）—— 跨域共享
```
AgentRun (1) ──< AgentStep   (order_index, 可 parent_step_id 嵌套)
            ──< AgentEvent   (sequence 单调递增，append-only 真相源)
            ──< AgentArtifact (产物：markdown/json/text/image)
```
- `AgentRun`：`domain`/`workflow`/`resource_type`/`resource_id`/`status`/`progress_raw`(JSON)/`context_ledger_raw`(JSON)/`input_snapshot_raw`/`config_raw`/`credit_reserved`/`credit_used`/`error_message`。
  - 状态 `AgentRunStatus`：`QUEUED|RUNNING|PAUSED|COMPLETED|PARTIAL|FAILED|CANCELLED`；集合 `ACTIVE={QUEUED,RUNNING,PAUSED}`、`TERMINAL={COMPLETED,PARTIAL,FAILED,CANCELLED}`。
  - `progress` 形状：`{total_steps, completed_steps, failed_steps, current_step, cursor?, review_stage?}`。`cursor`/`review_stage` 是 human-in-loop 的恢复游标。
- `AgentStep`：人可读摘要(`input/output/reasoning_summary`、`decision_notes`、`self_check`、`next_action`) + 完整调试(`prompt_snapshot`、`model_response`、`model_provider/name`) + `context_snapshot_raw`/`context_check_raw`(账本与校验快照，内部/调试可见)。
- `AgentEvent`：`sequence`(每 run 内单调递增，由 recorder 分配)、`event_type`、`level`、`message`、`payload_raw`。**这是回放的真相源**。
- `AgentArtifact`：`artifact_type`、`content_text`/`content_json_raw` 内联，文件经 `storage_path` 引用，`preview_url`，`domain_ref_type`/`domain_ref_id` 反链业务行（如 `code_project`、`code_document`、`code_frontend_html`、`code_frontend_project_zip`、`code_frontend_project_meta`）。
  - 表结构由 `db.create_all()` 在启动时建（无 Alembic）。改模型重启即生效，无迁移历史。

事件类型全集（`models/agent/event.py`）：`run_started, step_started, model_request, model_response, tool_call, tool_result, artifact_created, file_created, progress, context_updated, context_conflict, warning, error, step_completed, run_completed, step_awaiting_review, user_revision, review_resolved`。

---

## 3. Agent Swarm 运行底座（`services/agent/`）

- **runtime.py**：进程级单例 `agent_runtime = AgentRuntime(max_workers=4)`（`ThreadPoolExecutor`，**全进程同时最多 4 个 run**）。
  - workflow 用 `register_workflow(key, fn)` 注册进 `_WORKFLOWS`，模块加载时 `_register_builtin_workflows()` 注册上述三个 key。
  - `start(app, run_id)` → `executor.submit(self._execute, ...)` 立即返回；`_execute` 在 `with app.app_context():` 内跑，首启发 `RUN_STARTED`、置 `RUNNING`/`started_at`，调 `workflow_fn(ctx, recorder)`。
  - workflow 返回 `{status, resource_id?, extra_credits}`。结算：`credit_used = credit_reserved + extra_credits`。
  - **PAUSED** 分支：不发 `RUN_COMPLETED`、不置 `completed_at`，worker 退出，等用户 resume 重启。
  - **失败退款**：异常时置 `FAILED`，**仅当未产出任何 artifact（`_run_produced_nothing`）** 才 `refund_credits(credit_reserved)`；否则视为有交付不退。
  - **协作式取消**：`request_cancel(run_id)` 只打标记，workflow 在步骤边界 `ctx.is_cancelled()` 自检停止（不会中断进行中的模型调用）。
  - 收尾 `db.session.remove()` 清线程会话，避免线程复用串状态。
  - **后台线程内调 AI factory 要 `force_new=True`**（拿全新非缓存实例），见 `generate_preview_images` 里 `get_image_provider(force_new=True)`。
- **recorder.py**：分配单调 `sequence`（加锁）；落库 step 的 prompt/response/summary/context；**token delta 不落库**（只推 live bus，`{"kind":"delta",...}`，回放时用 `step.model_response` 还原完整文本）。带**自动密钥脱敏**（Bearer/sk-/AIza 等）。
- **bus.py**：`AgentEventBus`，每订阅一个 `Queue(maxsize=2000)`；满了丢**最旧**事件（保证 `run_completed` 等终态不丢，丢的中间事件靠 DB 重放补）。
- **SSE 流（agent_routes `_event_stream`）**：先按 `last_sequence` 重放 DB 已存事件，再进 `q.get(timeout=15)` 实时循环（超时发 keepalive 并复查 run 状态），终态做最后一次 DB 扫描后发 `done`。
- **files.py**：产物文件存 `{UPLOAD_FOLDER}/agent_runs/{run_id}/{step_id}/{filename}`，DB 存相对路径。
- **schemas.py**：`AgentContext` dataclass（run/user/team/domain/workflow/resource/config/input_snapshot/is_cancelled）。

---

## 4. `code_full_generation` 全流程（7 步，human-in-the-loop，可恢复）

实现：`services/agent/workflows/code_workflow.py`，`TOTAL_STEPS=7`。

步骤序列与每步要点：

| # | step key | 角色 | 输入 | prompt/服务 | 写回 | ledger 动作 | 校验 |
|---|---|---|---|---|---|---|---|
| 1 | planner | planner | requirement/title/style_ids | 无模型调用 | 创建/定位 `CodeProject`，绑定到 run | `seed_from_inputs()` 播种（**不预设技术栈**） | — |
| 2 | requirements | generator | requirement | `stream_requirements`/`revise_requirements` | `requirements_doc` + stage version | 合并定位/用户/范围/技术约束/待确认 | 确定性(非空) |
| 3 | flow | generator | requirements_doc | `stream_development_flow` | `development_flow` | 合并技术假设 | 确定性(非空+account one_liner) |
| 4 | documents | generator | req+flow | `stream_documents` | 重建 `CodeDocument[]` + version | 术语表 | 确定性(覆盖 6 类基线文档) + **AI 一致性闸** |
| 5 | style | generator | requirement+style_ids | `stream_style_prompt` | `style_prompt`+style_ids+version | UI 基调决策+约束 | 确定性(非空) |
| 6 | preview | generator | style_prompt | `generate_preview_images`(图像 provider) | `preview_images`+`PREVIEW_READY`+version | — | — |
| 7 | publisher | publisher | 全部 | 无模型调用 | 产出 `project.json`+`context_ledger.json` artifact | — | — |

**人工确认门控**（`REVIEW_STAGES = {requirements, flow, documents, style}`）：每生成完一个被审查 stage 的文档，run 进入 `PAUSED` 并发 `step_awaiting_review`，worker 退出。用户两条路：
- **approve** → `resume` 从 `cursor` 续跑下一 stage（`_run_from`）。
- **revise** → 把指令记入 ledger（`record_user_revision`，作为高优先级 decision 携带进后续 prompt），重生成该 stage 文档，再次 `PAUSED`。

可恢复：每次启动从 persisted `progress.cursor` + ledger + project 重建状态，所以关页面/重启服务后仍能继续。三条启动路径由 config 里一次性的 `_resume` 指令决定：fresh（planner+requirements 后停在第一道门）/ revise / approve。

附带能力：
- requirements 步还会best-effort生成**需求澄清问卷**（`generate_clarifications`，作为 step 上的 JSON artifact，不新增表列），失败不影响主文档（见 §8、`docs/requirements-clarify-spec.md`）。
- preview 步：图片**逐张串行**生成，每张落盘+确认可读后再生成下一张（`_await_artifact_on_disk` + `CODE_PREVIEW_SETTLE_SECONDS` 间隔），且 `preview_url` 只存短文件路由（base64 data URL 存在 `CodeProject.preview_images_raw` 这个 Text 列，避免撑爆 artifact `varchar(1000)`）。

---

## 5. 前端代码生成 workflow（两套，注意取舍）

UI-baseline 确认（`status=ui_confirmed`）之后才生成前端。两个 workflow 共享「重载上一段 `code_full_generation` run 的 ledger 以保持口径」的做法。

### 5.1 `code_frontend_project_generation`（✅ 当前默认，容器化多文件）
实现：`workflows/code_frontend_project_workflow.py` + `services/code/frontend_project_service.py`。`TOTAL_STEPS=3`：`fe_planner → fe_project_build → fe_publish`。

- `fe_project_build` 在**一次性 Docker 容器**里跑无头 Claude Code CLI（`claude -p ... --permission-mode bypassPermissions`），自主创建 + 构建 React+Vite+TS 多文件工程。CLI 的 stream-json 被实时翻译成 `file_created`/`tool_call`/`tool_result`/`fe_phase` 事件，时间线逐文件回放。
- **自愈构建梯队**（保证一定有可预览 dist）：① `npm run build` → ② 一轮 AI 定向修复（喂构建日志）→ ③ 确定性补桩缺失相对引用（`repair.mjs`）→ ④ 跳过 tsc 直接 `vite build` → ⑤ 合成降级预览页。到达的 rung 记入 `degraded_reason`（`ai-repair|stub|vite-only|fallback`）。**降级但有产物 ≠ 失败**；只有 agent 完全没产出源码才 hard-fail（→ runtime 退款）。
- 产物：源码 `frontend_project.zip`（artifact）+ 构建 `dist` 写入 `agent_run_dir/site/`，预览 URL `/api/agent/runs/<id>/site/index.html`。返回 `files/dist_files/usage/cost_usd/degraded`。
- **运维强约束**：需要宿主有 Docker + 镜像 `fe-agent:latest`（`FE_AGENT_IMAGE`）+ `ANTHROPIC_API_KEY`（CLI 自带 Anthropic 后端，**绕开** capability-routed provider 抽象）。容器以非 root `node` 用户运行，仅挂载 `/out`，`--rm` 即焚。

### 5.2 `code_frontend_generation`（⚠️ 遗留，单文件 HTML，仅回放）
实现：`workflows/code_frontend_workflow.py` + `services/code/frontend_build_service.py`。`TOTAL_STEPS=4`：`fe_planner → fe_build → fe_critic →（fe_repair，仅 critic 不通过且有 issues 时，不计进度）→ fe_publish`。

- `build_app` 走文本 provider 产出**单文件 `index.html`**（内联 CSS + 浏览器原生 JS），`critique_app` 对抗式复检可交互性/完整性，不通过则 `repair_app` 修一轮。
- critic 判定：模型返回 `{passed, issues:[...], summary}`；`passed` 为真或无 issues 则通过。审查/修复模型不可用时**fail-open**（视为通过/保留原 HTML）。
- 产物：`index.html`（artifact，`domain_ref_type=code_frontend_html`）。
- 前端 `previewTabs.ts` 把它标为 legacy（`FRONTEND_WORKFLOWS` 含两者，但 `CodeAppPreview` 只主动触发 project 版）。新需求别在这条上加功能，除非要兼容旧 run 回放。

> **CLAUDE.md 偏差**：CLAUDE.md 描述 `code_frontend_generation` 产出「Sandpack 文件 map（React+TS+纯 CSS）」并把它当默认——这与代码不符。实际默认是 5.1 的容器多文件工程，预览是 iframe 而非 Sandpack。

---

## 6. 会话上下文账本 + 一致性校验

防"口径漂移"。详见 `docs/agent-context-ledger.md`。

- **ContextLedger**（`services/agent/context_ledger.py`，纯 Python，无 DB）：结构 `{project{title,one_liner,target_users,scope_in/out}, glossary[], tech_stack{frontend,backend,data,constraints[]}, decisions[], constraints[], open_questions[], provenance[]}`。
  - `seed_from_inputs()` 在 planner 播种（**故意不预设技术栈**，由 requirements 步从真实需求推导，避免把所有项目锚到单一栈）。
  - `merge(...)` 幂等增补（glossary 按 term、decisions 按 id、其余按规范化字符串去重，空输入不抹除）。
  - `render_for_prompt()` 渲染成 markdown 共识块注入每个下游 prompt；**输出不含单个 `{`/`}`**，可安全作为 `str.format` 参数或 `str.replace` 值。
  - `record_user_revision()` 把用户在确认门的调整变成高优先级 decision，携带进后续所有 prompt。
  - 账本**仅内部/调试可见，绝不进入用户产出**。
- **context_verifier.py**：两层，**都永不向上抛**（校验失败不能中断 run）。
  - `run_deterministic_checks`：非空、文档类型覆盖、必填账本字段、前端栈一致性——失败只发 `warning`。
  - `run_ai_consistency_gate`：仅在**高风险边界**（`documents` 与 `frontend_build`）跑一次模型一致性闸，按 `pricing.CODE_CONTEXT_VERIFY` **逐次计费**。provider 未配置返回 `None`（跳过，不计费）；调用/解析出错 fail-open 返回无冲突并标 `degraded`。
  - 结果经 `emit_context_events` 发 `CONTEXT_UPDATED`/`CONTEXT_CONFLICT` 事件，并写入 step 的 context_snapshot/context_check。

---

## 7. 分阶段版本历史 + 划选局部修订 + 需求澄清

### 7.1 版本历史（`services/code/version_service.py`）
所有对可版本化 stage 内容的写入（agent 步 / 同步路由 / 人工编辑）都过 `record_stage_version`，追加 append-only 历史：
- `safe_record_stage_version`：best-effort 包装，历史写失败不影响主产物（已落 `CodeProject`）。
- 去重：`_signature` 指纹，与最新版相同则复用不新建。
- `activate_stage_version`：回滚——把历史版内容写回 `CodeProject`（documents stage 会重建 `CodeDocument` 行）并移动 `is_current` 指针。
- `list_stage_versions(seed=True)`：旧项目无历史时惰性补一条 `import` 基线版。

### 7.2 划选局部修订（section revision）
用户选中某 stage 产物的一段文字 + 调整意见 → 模型**只返回该段替换文本**，后端按选区 offset 拼回，快照新版本（`source=partial_revision`），并返回精确改动区间供前端高亮。
- 路由：`POST /projects/<id>/stages/<stage>/revise-section`（requirements/flow/style）与 `POST /projects/<id>/documents/<doc_id>/revise-section`。
- 服务：`generation_service.revise_section(kind, current_doc, selected_text, instruction, context_ledger)`，按 kind 选 `*_section_revision_prompt.txt`。
- 选区定位 `_resolve_span`：优先用客户端 offset（仍精确括住选中文本时），否则退回首次出现；找不到则要求用户重选（文档已变）。
- 失败兜底：模型/流式失败时返回原 `selected_text`（no-op 拼接，不丢用户已确认内容）+ 退款。
- 注入只读 ledger：独立路由没有自己的 run，靠 `_load_ledger_for_project` 从最近一段 `code_full_generation` run 重载共识。

### 7.3 需求澄清（clarify）
`generate_clarifications` 产出**快速确认问卷**（单/多选 + 自定义），前端在需求确认门渲染对话框；未答项回落到模型推荐 `default`；答案编译成一条 revise 指令再迭代需求文档。规范化逻辑（封顶 6 题/5 选项、丢弃畸形题、保证 ≥2 选项与连贯 default）见 `_normalize_clarifications`。契约见 `docs/requirements-clarify-spec.md`。

---

## 8. HTTP API 速查

### 8.1 `/api/code`（同步，`@jwt_required`，owner 校验 `_get_owned_project`）
| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/styles` | UI 风格列表 |
| GET/GET/POST/POST | `/prompt-prefixes`, `/prompt-prefixes/<id>`, `/prompt-prefixes/route`, `/prompt-prefixes/compose` | 角色提示词前缀库（prompt_library）|
| GET | `/projects` | 列项目（limit/offset/has_more）|
| POST | `/projects` | 创建项目 + 同步生成需求文档（201）|
| GET/PATCH | `/projects/<id>` | 读 / 改项目级字段（PATCH 改动字段会记版本）|
| POST | `/projects/<id>/flow` | 生成开发流程 |
| POST | `/projects/<id>/documents` | 拆分开发文档 |
| PATCH | `/projects/<id>/documents/<doc_id>` | 编辑单篇文档（记 documents 版本）|
| POST | `/projects/<id>/style-prompt` | 生成风格文档（body: `style_ids[]`）|
| POST | `/projects/<id>/previews` | 生成预览缩略图（图像服务不可用时**优雅跳过**：用风格提示词当 UI 基调并直接 `ui_confirmed`）|
| POST | `/projects/<id>/confirm-preview` | 确认 UI 基调 |
| POST | `/projects/<id>/stages/<stage>/revise-section` | 文本 stage 局部修订（**计费 + 失败退款**）|
| POST | `/projects/<id>/documents/<doc_id>/revise-section` | 单篇文档局部修订（计费 + 退款）|
| GET | `/projects/<id>/versions` | 所有 stage 版本元数据 |
| GET | `/projects/<id>/stages/<stage>/versions` | 单 stage 版本列表 |
| GET | `/projects/<id>/stages/<stage>/versions/<vid>` | 单版本（含完整内容）|
| POST | `/projects/<id>/stages/<stage>/versions/<vid>/activate` | 回滚到该版本 |

### 8.2 `/api/agent`（Agent Swarm，`@jwt_required`，owner 校验 `_get_owned_run`）
| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/runs` | 创建并启动 run（校验 domain/workflow，预扣 `WORKFLOW_COSTS[workflow]`；每用户活跃 run 上限 `MAX_CONCURRENT_RUNS=2`，超限 429 `CONCURRENCY_LIMIT`）|
| GET | `/runs` | 列当前用户 run（按 domain/resource_id 过滤）|
| GET | `/runs/<id>` | run + steps + events + artifacts 快照 |
| GET | `/runs/<id>/stream` | SSE（`?last_sequence=` 断点续传）|
| POST | `/runs/<id>/cancel` | 协作式取消（终态返回 400 `INVALID_STATE`）|
| POST | `/runs/<id>/resume` | 恢复暂停 run（`action=approve|revise`，revise 需 `instruction`）|
| GET | `/artifacts/<id>/file` | 下载产物（owner-only，`?download=1`）|
| GET | `/runs/<id>/site/<path>` | iframe 预览容器构建产物（token 走 query→path-scoped cookie，带 `connect-src 'none'` CSP）|

统一响应：`{success, data, message}` / `{success:false, error, message}`，辅助函数 `backend/utils/response.py`。错误码：`VALIDATION_ERROR(400)`、`NOT_FOUND(404)`、`FORBIDDEN(403)`、`INSUFFICIENT_CREDITS(402)`、`CONCURRENCY_LIMIT(429)`、`INVALID_STATE(400)`、`SERVER_ERROR(500)`。

---

## 9. 计费与积分

- 单价集中在 `services/pricing.py`，每项可被 `PRICE_*` 环境变量覆盖：
  - `CODE_FULL_GENERATION`(`PRICE_CODE_FULL`,默认0)、`CODE_FULL_GENERATION_TOTAL`(=前者，预扣)、`CODE_CONTEXT_VERIFY`(`PRICE_CODE_CONTEXT_VERIFY`,0)、`CODE_FRONTEND_GENERATION`(`PRICE_CODE_FRONTEND`,0)、`CODE_FRONTEND_PROJECT_GENERATION`(`PRICE_CODE_FRONTEND_PROJECT`,0)、`CODE_SECTION_REVISION`(`PRICE_CODE_SECTION_REVISE`,0)。
  - **当前 Code 域默认全 0（免费）**，但全部经 `charge()`/`refund_credits()`，0 当 no-op；设环境变量即可开计费，无需改码。
- 时机：
  - 创建 run 时按 workflow 总价**预扣**（`deduct_credits`，原子 `UPDATE ... WHERE balance>=amount`，余额不足删 run 返 402）。
  - 过程中 AI 一致性闸按 `CODE_CONTEXT_VERIFY` **逐次加扣**（汇总进 workflow 返回的 `extra_credits`）。
  - 局部修订在路由同步 `charge`，异常 `refund_credits`。
  - run 失败且**未产出任何 artifact**时 runtime 自动退预扣款。
- `credit_service.deduct_credits` 用原子 SQL 防竞态；传 `team_id` 则从团队余额扣。

---

## 10. AI Provider 抽象与超时/重试（共享底座）

- 唯一接入点 `services/ai/`，**按能力路由**：`get_text_provider()` 走文本配置、`get_image_provider()` 走图像配置，均线程安全缓存单例；后台线程传 `force_new=True`。
- 默认接线：文本→Claude（`claude-opus-4-8`，流式+adaptive thinking），图像→OpenAI(`gpt-image-2`)，`AI_PROVIDER=gemini` 兜底。
- 模型失败返回 `success=False`（不抛）；基础设施/DB 错误才抛。
- **per-request 超时（最近改动，两端都有）**：
  - 后端 Claude：`AI_TEXT_READ_TIMEOUT`（默认 120s）→ `httpx.Timeout(read=120, connect=10, write=30, pool=10)`，并对传输层错误整生成重试。**防止 stalled stream 永久占用 4 个 runtime 槽之一**（`services/ai/claude.py`）。
  - 前端 axios：全局超时 30s 会误杀长生成，故 `api/code.ts` 给所有 AI 生成端点单独设 `AI_GENERATION_TIMEOUT=180000`（180s）。
  - 容器前端生成另有独立预算：`FE_AGENT_TIMEOUT`(720)、`FE_AGENT_REPAIR_TIMEOUT`(300)、`FE_AGENT_NPM_TIMEOUT`(240)、`FE_AGENT_BUILD_TIMEOUT`(180)、`FE_AGENT_TOTAL_TIMEOUT`(2400)。

---

## 11. 前端

- **状态**：`codeStore`（同步 CRUD/各 stage 生成/局部修订/版本，错误经 toast；生成完回调刷新积分由 agentStore 负责）、`agentStore`（run 生命周期）。
- **SSE 实现**：`agentStore` 用 **fetch + ReadableStream**（不是 `EventSource`，因为要带 bearer token）。`last_sequence` 断点续传；`agent_delta`（token 流）累加进 `streamingByStep`（不落库）；结构性事件去抖 350ms 拉 `fetchRun` 快照；`run_completed`/`done` 收尾并刷新余额一次。
  - `openLatestRunForResource(resourceId)`：调出某 Code 项目最近的 run 做过程回放；整条对话由事件回放派生（`deriveConversation`）。
- **CodeStudio**（`pages/code/CodeStudio.tsx`）：单列会话式工作台。顶部 `CodeStepper` 跟踪五个 stage；对话区是主界面，构建通过聊天驱动，预览作为内联可折叠产物卡折进对话。`handleStart` 用 `startRun({workflow:"code_full_generation", config:{requirement,title,style_ids}})`；`approve/revise` 走 `resumeRun`。预览缩略图在右侧滑入栏 `PreviewThumbnailPanel`。
- **组件树**：`CodeStudio → ConversationRail(+RequirementsClarifyDialog +StageArtifactCard) + PreviewThumbnailPanel + AgentRunPanel + CodeStepper`；`StageArtifactCard →（SelectionReviseTextarea 划选修订 + StageHistoryDialog 版本弹窗 + CodeAppPreview 前端预览）`。
- **前端工程预览**（`CodeAppPreview`）：`FRONTEND_WORKFLOW="code_frontend_project_generation"`；从当前 run 或回放最近 project run 取 `code_frontend_project_meta` 的 `preview_url`，用 `<iframe sandbox="allow-forms allow-modals allow-scripts allow-same-origin">` 指向 `/api/agent/runs/<id>/site/index.html?token=...`（token 进 query，后端镜像到 path-scoped cookie；站点 `connect-src 'none'` CSP 防外联）。源码 zip 经鉴权 `downloadArtifact` 下载。
- **i18n**：i18next，命名空间 JSON 在 `src/locales/{en,ja,ko,zh-CN}/`（`code`、`agent`、`codeapp`、`errors` 等）。新 key 要四语言齐全，别硬编码文案。

---

## 12. Prompt 体系（`backend/prompts/code/*.txt` + `prompt_library`）

- **两种占位符风格，别混用**：
  - `generation_service`（文本文档）用 **`str.format`** → 占位符 `{system_prefix}`、`{context_ledger}`、`{requirement}`、`{requirements_doc}`、`{development_flow}`、`{current_doc}`、`{instruction}`、`{selected_text}`、`{styles}`、`{current_documents}`。**模板里的 JSON 示例花括号必须写成 `{{ }}` 转义**，否则 `.format()` 报错（`test_code_json_parsing.py` 覆盖了模型 JSON 解析健壮性）。
  - `frontend_build_service` / `frontend_project_service`（含 JSX `{ }`）用 **`str.replace`** → 占位符 `[[CONTEXT_LEDGER]]`、`[[REQUIREMENT]]`、`[[REQUIREMENTS_DOC]]`、`[[DEVELOPMENT_FLOW]]`、`[[DOCUMENTS]]`、`[[STYLE_PROMPT]]`、`[[UI_BASELINE]]`、`[[HTML]]`、`[[ISSUES]]`。这样模板正文里的 `{ }` 不会被误解析。
- 模板清单：`requirements_prompt`、`requirements_revision_prompt`、`requirements_clarify_prompt`、`requirements_section_revision_prompt`、`development_flow_prompt`(+`_revision`/`_section_revision`)、`document_split_prompt`(+`_revision`/`_section_revision`)、`style_prompt`(+`_revision`/`_section_revision`)、`frontend_build_prompt`、`frontend_critic_prompt`、`frontend_repair_prompt`、`frontend_project_prompt`、`frontend_project_repair_prompt`。
- **prompt_library**（`services/prompt_library/internet_roles.py`）：角色前缀库。`compose_recipe_prompt(recipe_id)` 把一组角色拼成 `{system_prefix}`；当前用到的 recipe：`product_requirement`（需求/风格/澄清）、`engineering_implementation`（流程/文档）。`compose_system_prompt(primary, secondary, ...)` 供 `/prompt-prefixes/compose` 端点组装完整 system prompt（base 前缀 + 角色 + 输出契约）。`route_prefixes(task)` 确定性路由任务到角色。
- JSON 解析健壮性（`generation_service`）：`_strip_code_fence`（只剥整体围栏，不误伤内部代码块）、`_loads_tolerant`（`strict=False` + 尾逗号修复重试）、`_extract_json_objects`（截断时逐对象抢救）。

---

## 13. 环境变量清单（Code 域相关）

| 变量 | 用途 | 默认 |
|---|---|---|
| `ANTHROPIC_API_KEY` / `CLAUDE_API_KEY` | 文本 provider + 容器前端 CLI | — |
| `AI_TEXT_READ_TIMEOUT` | Claude 单请求读超时(s) | 120 |
| `OPENAI_API_KEY` / `AI_IMAGE_*` | 图像 provider（预览图）| — |
| `CODE_PREVIEW_SETTLE_SECONDS` | 预览图逐张间隔(s, 0–30) | 2.0 |
| `PRICE_CODE_FULL` / `PRICE_CODE_CONTEXT_VERIFY` / `PRICE_CODE_FRONTEND` / `PRICE_CODE_FRONTEND_PROJECT` / `PRICE_CODE_SECTION_REVISE` | 各操作积分单价 | 0 |
| `FE_AGENT_IMAGE` | 容器镜像 | `fe-agent:latest` |
| `DOCKER_BIN` | docker 可执行 | `docker` |
| `FE_AGENT_TIMEOUT/REPAIR_TIMEOUT/NPM_TIMEOUT/BUILD_TIMEOUT/TOTAL_TIMEOUT` | 容器各阶段超时(s) | 720/300/240/180/2400 |

后端跑在 **5001** 端口（不是 README 说的 5000）；前端 3000 代理 `/api`→5001。

---

## 14. 测试（`tests/`，pytest）

- 单元（无网络）：`test_code_json_parsing.py`（模型 JSON 解析健壮性回归）、`test_code_clarifications.py`（需求澄清规范化）、`test_code_section_revision.py`（划选修订选区/拼接）、`test_prompt_library.py`（角色路由/system 前缀拼装）、`test_ai_factory.py`、`test_ai_providers_unit.py`。
- 集成（打真实 Claude+图像 API，`@pytest.mark.integration`，需 key+网络，会产生费用）：`test_ai_integration.py`。
- `conftest.py` autouse fixture 每用例前后 `reset_providers()` 清 provider 缓存。
- 跑法：`uv run pytest -m "not integration"`（默认）/ `uv run pytest -m integration -s`。
- ⚠️ **前端无测试**（无 vitest）；根目录 `npm run test:frontend`/`npm run test` 当前会失败。

---

## 15. 上手建议（给新团队）

1. 先跑通最小闭环：`npm run setup` → `npm run dev`，登录后在 `/code` 输需求，观察 `code_full_generation` 七步在对话里流式 + 四道确认门。
2. 想读懂"一步发生了什么"：盯 `code_workflow.py` 的某个 `_do_*` + 对应 `generation_service` 方法 + `prompts/code/*.txt`。三者一一对应。
3. **加 stage 字段/产物**：参考另一个 stage 的既有写法（写回 `CodeProject` → `safe_record_stage_version` → step.add_artifact → ledger.merge → 校验）。
4. **加 AI provider**：实现 `AIProvider` 两方法（不支持的能力返回 `success=False` 不抛），在 `factory._create_provider` 挂分支，别绕过 capability 路由直接 new。
5. **改计费**：只动 `pricing.py` 常量或环境变量，别把成本散落各处。
6. **容器前端生成上线前**：确认部署环境能 `docker run`，镜像 `fe-agent:latest` 已构建，`ANTHROPIC_API_KEY` 在后端进程可见。

---

## 16. 已知问题 / 坑 / 技术债（重点）

### 数据一致性 / 并发
- **`CodeStageVersion.version_number` 竞态**：`MAX+1` 读-算-写非原子，无 `UNIQUE(project_id,stage,version_number)` 约束，并发可能重号（`version_service.py:165`）。
- **`is_current` 切换非原子**：先 `UPDATE ...=False` 再置 `True`，无锁，理论上可能出现 0 行或 >1 行 current（`version_service.py:122`）。
- **`parent_step_id` / `CodeStageVersion.run_id` 无外键约束**：仅 index，无级联/完整性保障。
- **`add_credits`（退款路径）非原子**：`deduct_credits` 是原子 SQL，但 `add_credits` 是"先读后写"，高并发退款理论有竞态（实际多为单失败 run 调用，影响小）。
- 无 Alembic：表结构靠 `create_all()`，生产改模型需手工迁移。

### 同步路由与工作流的不对称
- 同步 `split_documents`（`project_routes.py:225`）用 `project.documents.delete()`，而工作流/回滚已知该写法在 SQLAlchemy 2.x（带 order_by 的 relationship）会抛错，改用了 `CodeDocument.query...delete()`。**同步路由这处是潜在 bug**，建议统一。
- 同步路由不注入 ledger（除局部修订外），与 Agent Swarm 的口径一致性能力不对等。

### 退款 / 计费边界
- "产出了 artifact 就不退款"的粒度较粗：需求文档刚产出就失败也不退（已有 artifact）。可考虑给 artifact 加"可交付"标记。
- `refund_credits` 自身失败时降级为"按全额消耗记账"（`credit_used=credit_reserved`），用户可能显示已扣但余额未动。
- run 创建始终按 `user_id` 计费，**团队积分未真正接入**（传了 `team_id` 仅记录）。

### SSE / 回放
- token delta 不落库：断网重连后逐字动画丢失（完整文本仍可由 `model_response` 还原，信息不丢）。
- bus 队列满丢最旧事件，依赖 DB 重放补；若客户端无法重连（token 过期等）则中间事件永久丢失。
- `q.get(timeout=15)` keepalive 硬编码；长连接可能被代理中断，靠客户端 reconnect。

### 运行时
- `ThreadPoolExecutor(max_workers=4)` 是全进程硬上限：4 个长 run 会排队；stalled 流靠 Claude 超时释放槽（已加），但容器前端生成可跑很久。
- executor 无优雅 shutdown，进程退出不等在跑的 run。
- 容器前端生成 hard 依赖宿主 Docker + 镜像 + key；CLI 绕过 provider 抽象，成本/用量靠 CLI 自报。`bypassPermissions` 在沙箱内运行，安全性依赖容器隔离（非 root node 用户、仅挂 `/out`、`--rm`、`connect-src 'none'` 预览 CSP）。

### 文档偏差（接手前务必知道）
- **`code_frontend_generation` ≠ Sandpack/React+TS**：实为单文件 HTML，且已被容器多文件工程取代为默认；CLAUDE.md 此处过时。
- **CLAUDE.md 只列了两个前端 workflow**，实际有三个（漏了 `code_frontend_project_generation`，它才是当前默认）。
- `README.md`/`AGENTS.md` 整体落后（端口、默认 provider、域覆盖均不准），别依赖。
- CLAUDE.md 未提 `CodeStageVersion`（分阶段版本历史）这张表。

---

## 17. 关键文件索引

| 关注点 | 文件 |
|---|---|
| 主流程 7 步 | `backend/services/agent/workflows/code_workflow.py` |
| 容器前端生成 | `backend/services/agent/workflows/code_frontend_project_workflow.py` + `backend/services/code/frontend_project_service.py` |
| 单文件 HTML（遗留）| `backend/services/agent/workflows/code_frontend_workflow.py` + `backend/services/code/frontend_build_service.py` |
| 文本生成/解析/澄清/局部修订 | `backend/services/code/generation_service.py` |
| 版本历史/回滚 | `backend/services/code/version_service.py` |
| 上下文账本 / 校验 | `backend/services/agent/context_ledger.py` / `context_verifier.py` |
| 运行底座 | `backend/services/agent/{runtime,recorder,bus,files,schemas}.py` |
| HTTP | `backend/routes/code/project_routes.py` / `backend/routes/agent_routes.py` |
| 计费 | `backend/services/pricing.py` / `backend/services/credit_service.py` |
| 角色提示词 | `backend/services/prompt_library/internet_roles.py` |
| 模型 | `backend/models/code/{project,stage_version}.py` / `backend/models/agent/*` |
| 前端 | `frontend/src/pages/code/CodeStudio.tsx` / `frontend/src/stores/{codeStore,agentStore}.ts` / `frontend/src/api/{code,agent}.ts` / `frontend/src/components/{code,agent}/*` |
| 既有文档 | `docs/agent-context-ledger.md` / `docs/requirements-clarify-spec.md` |
</content>
</invoke>
