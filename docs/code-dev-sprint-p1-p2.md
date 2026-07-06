# Code Dev Sprint P1/P2 实现文档

> **实现状态(2026-07-02):P1 + P2 已全部落地并上线。**
> - P1:`CodeDevTaskPlan` + `dev_backlog_planner_service`(normalize 断环/去重/lane 过滤/指纹防漂移)+ `code_dev_backlog_planner` workflow(模型失败→账本回退)+ `task-plans` 六端点 + `DevTaskPlannerPanel`(生成/编辑/排序/删除/应用/放弃)。apply 复用 `dev_sprint_service.bulk_write_tasks`(与 `tasks/bulk` 同一保护路径)。
> - P2:`asset_lane.py` 共享模块(一次性 fe-agent 与 Dev 容器同源注入,诊断契约不变);dev 容器 `/tmp/dev-assets` 状态目录 + `write_asset_context`/`asset_diagnostics`/`validate_resource_outputs`;`code_dev_turn` 对 asset 任务前置风格上下文+计费(`CODE_DEV_ASSET_IMAGE`×required,环境死时退款)、验收期容器内实测产物并覆写评审判定(缺文件→retry budget;缺 key/codex/非法路径→直接 blocked)。
> - 测试:`test_dev_backlog_planner.py` 19 条 + `test_dev_asset_lane.py` 20 条,全量 559 过;prompt 校验 29/29。与本文差异:`render_bootstrap` 未采用 `state_dir`/`work_root_expr` 参数(实际不需要——工具装在 `$HOME` 下,Codex 从 claude 的 cwd 运行);`start_sprint=true`(P1.1)未做,前端手动点启动。

本文基于当前已交付的 Dev Mode Sprint P0：`CodeDevTask` 持久任务板、`CodeDevSprint` 串行调度器、`tasks/bulk` 输入、`code_dev_sprint` 编排 run、`code_dev_turn` 单任务验收。P1/P2 不重做调度器，也不把 backlog 放进容器；DB 仍是任务状态唯一真相，容器只保存代码现场和可执行工具。

## 0. 当前基线

P0 已具备：

- 任务状态机：`pending -> queued -> in_progress -> verifying -> done`，失败后按 `retry_count/max_retries` 回 `pending` 或进 `blocked`。
- 派生 ready：`pending` 且 `depends_on` 全部 `done`，不入库。
- 原子认领：`UPDATE ... WHERE status IN (...)`，避免多调度器重复 claim。
- 单任务投喂：`build_task_brief(task)` 只包含当前任务、AC、依赖、失败原因、禁止事项。
- 任务级验收：当前任务 AC 初始 false，已 done 任务作为回归集初始 true。
- Sprint API：`tasks/bulk`、`sprints` create/get/pause/resume/cancel。
- 前端控制面：`DevSprintPanel` 监听 sprint run 的 SSE，折叠 board/sprint 快照。

当前缺口：

- P1 缺自动 backlog planner。现在必须外部把任务列表喂给 `tasks/bulk`。
- P2 资源任务只进入了状态机和 brief。Dev 容器只创建了 `~/.claude/skills/image-assets` 目录，未注入完整 `gen-assets`、`genimage.mjs`、Codex 登录与诊断链路，也没有在 `code_dev_turn` 后对 `resource_spec.outputs` 做强验收。

## 1. P1：Backlog Planner

### 1.1 目标

P1 目标是把项目文档、ledger、当前任务板和用户补充目标拆成可确认的 Dev Sprint 任务列表，然后由用户确认后写入 P0 的 `CodeDevTask` 任务板。

必须做到：

- 由系统自动生成任务草案，不直接启动 sprint。
- 任务粒度适配 P0：一个 task 应该能被一次 `code_dev_turn` 实现或明确失败。
- 输出字段完全兼容当前 `tasks/bulk`：`title`、`feature_id`、`parent_feature_id`、`lane`、`category`、`description`、`acceptance_criteria`、`depends_on`、`resource_spec`、`priority`、`max_retries`。
- 用户能查看、编辑、删除、排序、应用任务草案。
- 应用任务草案时仍走现有 bulk 写入逻辑，不绕开 P0 的 replace/upsert/active task 保护。
- Planner 本身是 AI 消耗操作，必须注册 pricing，默认 0。

暂不做：

- 不自动跨前后端生成全栈任务。P0 sprint endpoint 仅支持 frontend session，P1 默认只产 `frontend` 和 `asset` lane。
- 不启动并行 sprint。P3 再做。
- 不让 planner 覆盖已完成或正在执行的任务。

### 1.2 新增数据模型

新增模型放在 `backend/models/code/fullstack.py`，并从 `backend/models/code/__init__.py` 导出。

```python
class DevTaskPlanStatus:
    PLANNING = "planning"
    DRAFT = "draft"
    APPLYING = "applying"
    APPLIED = "applied"
    REJECTED = "rejected"
    STALE = "stale"
    FAILED = "failed"

    ACTIVE = {PLANNING, DRAFT, APPLYING}
    TERMINAL = {APPLIED, REJECTED, STALE, FAILED}
```

新增表：

```python
class CodeDevTaskPlan(db.Model):
    __tablename__ = "code_dev_task_plans"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), db.ForeignKey("code_projects.id"), nullable=False, index=True)
    session_id = db.Column(db.String(36), db.ForeignKey("code_dev_sessions.id"), nullable=False, index=True)
    run_id = db.Column(db.String(36), nullable=True, index=True)

    status = db.Column(db.String(20), nullable=False, default=DevTaskPlanStatus.PLANNING, index=True)
    mode = db.Column(db.String(30), nullable=False, default="from_project")
    created_by = db.Column(db.String(36), nullable=False)

    input_fingerprint = db.Column(db.String(64), nullable=True)
    target_lanes_raw = db.Column(db.Text, nullable=True)       # list[str]
    plan_raw = db.Column(db.Text, nullable=True)               # normalized plan JSON
    warnings_raw = db.Column(db.Text, nullable=True)           # list[str]
    error_message = db.Column(db.Text, nullable=True)

    inserted_count = db.Column(db.Integer, nullable=True)
    updated_count = db.Column(db.Integer, nullable=True)
    skipped_count = db.Column(db.Integer, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    applied_at = db.Column(db.DateTime, nullable=True)
```

建议给既有 `CodeDevTask` 增加两个可空字段：

```python
plan_id = db.Column(db.String(36), nullable=True, index=True)
planner_meta_raw = db.Column(db.Text, nullable=True)  # risk/files_hint/estimated_turns 等不参与调度的信息
```

并新增来源：

```python
DevTaskSource.PLANNER = "planner"
```

表结构策略：

- 新表由 `db.create_all()` 创建。
- 既有表新增列必须 nullable，由 `schema_guard.ensure_model_columns()` 回填。
- 不引入强约束外键到 `AgentRun`，保持当前 AgentRun 关联风格。

### 1.3 Plan JSON 契约

Planner workflow 最终必须写入规范化后的 JSON，而不是原始模型输出。

```json
{
  "version": "dev-backlog-plan.v1",
  "summary": "本轮任务拆分摘要",
  "assumptions": ["必要假设"],
  "target_lanes": ["frontend", "asset"],
  "tasks": [
    {
      "feature_id": "FR1.T1",
      "parent_feature_id": "FR1",
      "lane": "frontend",
      "category": "functional",
      "title": "实现订单列表基础视图",
      "description": "包含数据状态、空状态、加载状态和错误状态。",
      "acceptance_criteria": [
        "订单列表能展示至少 3 条 mock 数据",
        "加载/空/错误状态均有可见 UI",
        "不破坏已完成导航和主题样式"
      ],
      "depends_on": ["FR1.ASSET1"],
      "resource_spec": {},
      "priority": 20,
      "max_retries": 2,
      "planner_meta": {
        "risk": "medium",
        "estimated_turns": 1,
        "files_hint": ["src/pages/Orders.tsx"]
      }
    }
  ],
  "warnings": []
}
```

字段规范：

- `feature_id`：稳定、唯一、最多 60 字符。建议格式：`FR1.T1`、`NFR2.T1`、`ASSET.FR1.1`。
- `parent_feature_id`：回指 FR/NFR，例如 `FR1`。后续 P3 会用它避免同一 feature 并行冲突。
- `lane`：P1 只允许 `frontend`、`asset`。出现 `backend/fullstack` 时保留在 draft warning 中，默认不应用到当前 sprint。
- `category`：沿用 P0：`functional`、`nonfunctional`、`asset`、`chore`、`test`。
- `acceptance_criteria`：1 到 8 条为宜，最多仍受 `tasks/bulk` 20 条上限保护。每条必须可验收。
- `depends_on`：只能引用已 done 任务或本 plan 内任务。不能有环。
- `resource_spec`：仅 `asset` task 使用，结构见 P2。
- `planner_meta`：只用于 UI 和追踪，不参与 P0 调度。

### 1.4 Planner 状态机

```text
planning -> draft
planning -> failed
draft -> applying -> applied
draft -> rejected
draft -> stale
applying -> failed
```

规则：

- `planning`：workflow 正在收集上下文或调用模型。
- `draft`：可编辑、可应用。
- `stale`：生成 plan 后，项目文档、session ledger 或当前任务板 fingerprint 改变。用户必须重新生成，或用 `force=true` 应用。
- `applying`：正在调用 bulk 写入，不允许并发编辑。
- `applied`：写入完成，记录 inserted/updated/skipped。
- `rejected`：用户放弃。
- `failed`：模型失败、解析失败且 fallback 也失败，或 apply 阶段异常。

`input_fingerprint` 由以下内容 hash 得出：

- `CodeProject.requirements_doc`
- `CodeProject.development_flow`
- `CodeProject.style_prompt`
- `CodeDevSession.shared_ledger_raw`
- 当前 session 下 `CodeDevTask` 的 `feature_id/status/title/acceptance_criteria/depends_on`
- `target_lanes`、`include_assets`、用户额外规划指令

### 1.5 后端服务

新增 `backend/services/code/dev_backlog_planner_service.py`。

核心职责：

- `build_planner_context(project, session, target_lanes, extra_instruction)`：收集项目文档、ledger、当前任务板、已有 done 任务、活跃 sprint 状态。
- `input_fingerprint(context)`：生成稳定 hash。
- `call_planner_model(context)`：通过 `get_text_provider(force_new=True)` 或当前 workflow 内 provider 调用文本模型。
- `parse_plan_json(text)`：解析 JSON，失败时尝试从 markdown code fence 提取。
- `normalize_plan(raw, session_lane, existing_tasks)`：裁剪、去重、补默认值、校验 deps、检测环、过滤不支持 lane。
- `deterministic_fallback(context)`：AI 不可用时从 ledger FR/NFR 生成保守任务。
- `apply_plan(plan_row, replace=False, force=False)`：内部复用 bulk 写入同等逻辑。

Normalization 必须是强约束：

- 单 plan 最多 200 个 task。
- title 不能为空，最多 300 字符。
- `acceptance_criteria` 最多 20 条，每条最多 500 字符。
- `depends_on` 最多 20 个，每个最多 60 字符。
- `resource_spec` 只接受 dict。
- 不支持的 `category` 归一到 `functional`。
- 不支持的 `lane` 在 P1 过滤，并记录 warning。
- 发现依赖环时必须拆环或失败，不允许把环形依赖写入 DB。
- 与既有 active/done task 同 `feature_id` 时，draft 保留 warning，apply 时不覆盖。

### 1.6 新增 Prompt

新增文件：

- `backend/prompts/code/dev_backlog_planner_prompt.txt`

读取方式必须使用：

```python
template = prompt_store.get("code/dev_backlog_planner_prompt.txt")
```

新增 prompt 后需要：

- 更新 `scripts/validate_code_prompts.py` 的占位符/重复规则。
- 更新 `tests/test_code_prompts.py` 覆盖。
- 用 `scripts/sync_code_prompts.py --key code/dev_backlog_planner_prompt.txt` 同步到 Mongo。若已有 admin override，按现有规则不强制覆盖。

Prompt 结构建议：

```text
# 角色与原则
你是 Code Dev Mode 的 Backlog Planner。你的任务不是写代码，而是把项目文档拆成可由单个 dev turn 完成的小任务。

# 输入
[[PROJECT_CONTEXT]]
[[EXISTING_BOARD]]
[[TARGET_LANES]]
[[USER_PLANNING_INSTRUCTION]]

# 本阶段职责与边界
- 只产任务，不启动 sprint，不写代码。
- 每个任务必须小到单回合可实现。
- P1 仅允许 frontend/asset lane。
- 不覆盖已完成或执行中的任务。
- 需要真实图片时创建 asset task，而不是让功能 task 随手找远程图。

# 输出契约
仅输出 JSON，符合 [[OUTPUT_SCHEMA]]。

# 交付前自检
- feature_id 唯一
- depends_on 无环
- 每个任务有可验收 AC
- asset task 的 outputs 都在 src/assets/
```

注意：此 prompt 使用 `[[KEY]]` replace 模式更适合，避免 `.format()` 花括号转义风险。

### 1.7 新增 Workflow

新增：

- `backend/services/agent/workflows/code_dev_backlog_planner_workflow.py`

注册：

- `runtime._register_builtin_workflows()` 增加 `code_dev_backlog_planner`。
- `agent_routes.WORKFLOW_COSTS` 增加 `code_dev_backlog_planner`。
- `pricing.py` 增加：

```python
CODE_DEV_BACKLOG_PLANNER = _credits("PRICE_CODE_DEV_BACKLOG_PLANNER", 0)
OPERATION["code_dev_backlog_planner"] = ("agent_run", CODE_DEV_BACKLOG_PLANNER)
```

workflow 步骤：

1. `planner_prepare`：校验 project/session 权限、读取 ledger、当前 board、活跃 sprint 状态。
2. `planner_generate`：调用文本模型生成 plan JSON。无 provider 或失败时走 deterministic fallback。
3. `planner_normalize`：解析、裁剪、去重、检测依赖、写 warning。
4. `planner_publish`：创建/更新 `CodeDevTaskPlan`，写 artifact `dev_backlog_plan.json`，emit plan snapshot。

返回：

```json
{
  "status": "completed",
  "resource_id": "<project_id>",
  "plan_id": "<plan_id>"
}
```

失败时：

- 如果模型失败但 fallback 成功，run 仍 completed，plan warnings 记录 `degraded:fallback`。
- 如果 fallback 也失败，plan status `failed`，run failed。

### 1.8 API

挂在现有 `dev_bp` 下。

```text
POST /api/code/projects/<pid>/dev-sessions/<sid>/task-plans
GET  /api/code/projects/<pid>/dev-sessions/<sid>/task-plans
GET  /api/code/projects/<pid>/dev-sessions/<sid>/task-plans/<plan_id>
PATCH /api/code/projects/<pid>/dev-sessions/<sid>/task-plans/<plan_id>
POST /api/code/projects/<pid>/dev-sessions/<sid>/task-plans/<plan_id>/apply
POST /api/code/projects/<pid>/dev-sessions/<sid>/task-plans/<plan_id>/reject
```

`POST task-plans` body：

```json
{
  "mode": "from_project",
  "target_lanes": ["frontend", "asset"],
  "include_assets": true,
  "max_tasks": 80,
  "instruction": "优先把核心用户路径拆出来"
}
```

返回：

```json
{
  "success": true,
  "data": {
    "plan": {"id": "...", "status": "planning"},
    "run_id": "..."
  }
}
```

`PATCH task-plan` 只允许在 `draft` 修改：

- `tasks`
- `summary`
- `assumptions`
- `warnings` 只能追加用户编辑 warning，不能清空系统 warning

`POST apply` body：

```json
{
  "replace": false,
  "force": false,
  "start_sprint": false
}
```

规则：

- `replace=true` 时复用当前 `tasks/bulk` 的保护：活跃 sprint 或 in-flight task 存在则拒绝。
- fingerprint 变化且 `force=false` 时返回 409/400，提示 plan stale。
- `start_sprint=true` 可以作为 P1.1，小步实现时先不做，前端让用户手动点 start sprint。

### 1.9 前端

新增组件：

- `frontend/src/components/code/DevTaskPlannerPanel.tsx`
- 或并入 `DevSprintPanel` 上方，但建议独立，避免 sprint 控制面变臃肿。

store/API：

- `frontend/src/api/dev.ts`
  - `DevTaskPlan`
  - `createTaskPlan`
  - `getTaskPlan`
  - `updateTaskPlan`
  - `applyTaskPlan`
  - `rejectTaskPlan`
- `frontend/src/stores/devStore.ts`
  - `taskPlan`
  - `plannerBusy`
  - `startTaskPlanner`
  - `applyTaskPlan`
  - `editTaskPlan`

UI 行为：

- 空任务板时显示“生成任务列表”主按钮。
- 任务草案按 `parent_feature_id` 分组。
- 每个 task 可编辑 title、description、AC、deps、priority、category。
- `asset` task 额外展示 outputs 列表。
- 应用后刷新 board，并清理 draft 或标记 applied。
- 所有文案放入 `frontend/src/locales/{en,ja,ko,zh-CN}/code.json`。

### 1.10 P1 验收标准

- 用户能在已有 frontend dev session 中点击“生成任务列表”，得到 draft。
- draft 中每个 task 都能直接通过现有 `tasks/bulk` 写入。
- 应用后 sprint 可逐个 claim 并执行。
- 已完成和执行中的 task 不会被 planner 覆盖。
- 模型失败时能产生 fallback plan 或清晰 failed 状态。
- 重新生成/应用时不产生重复 `feature_id`。
- 任务依赖无环，缺失依赖会在 draft 阶段暴露，不等 sprint 执行时才 blocked。

### 1.11 P1 测试

新增 `tests/test_dev_backlog_planner.py`：

- workflow/pricing/registration。
- prompt parse 成功。
- markdown fence JSON 提取。
- invalid JSON -> fallback。
- duplicate `feature_id` 去重。
- unsupported lane 过滤并 warning。
- dependency cycle 拒绝或自动打断。
- fingerprint stale 拒绝 apply。
- apply 调用 bulk 等价逻辑，不覆盖 done/active。
- `replace=true` 在 active sprint 下拒绝。
- API 权限校验。

前端最小验证：

- `npm run lint:frontend`
- `npm run build`

## 2. P2：Asset Skill 共享注入与产物校验

### 2.1 目标

P2 目标是让 `category=asset` 的任务真正可执行：agent 在 Dev 容器内能调用 `image-assets` skill，skill 能通过 `gen-assets` 触发 Codex 和图像模型生成真实位图，并且 sprint 能根据 `resource_spec.outputs` 判定任务通过或重试/阻塞。

必须做到：

- 一次性前端生成和 Dev Mode 共用同一份 asset lane bootstrap，避免两套脚本漂移。
- Dev 容器启动时安装完整 `image-assets` skill、`gen-assets`、`genimage.mjs`。
- `gen-assets` 不再硬编码 `/out/prompt.txt`，必须支持 Dev 容器的长运行路径。
- asset task 开始前写入当前项目风格上下文，保证资源视觉统一。
- asset task 结束后强校验 outputs：路径安全、文件存在、非 0 字节、扩展名/mime 合理。
- 缺 key、缺 Codex、缺 `gen-assets` 等不可通过重试解决的问题，直接 blocked 并给清晰原因。
- 图像/skill AI 消耗进入 `pricing.py`，默认 0。

暂不做：

- 不做图像语义质量自动评分。P2 验收以真实文件、路径安全、构建可引用、review 无阻断为主。
- 不强制所有 UI 任务都先有 asset task。是否生成 asset 由 P1 planner 和用户决定。
- 不把 asset backlog 放进容器。

### 2.2 Resource Spec 契约

P2 使用现有 `CodeDevTask.resource_spec_raw`，不新增表。

```json
{
  "skill": "image-assets",
  "style_brief": "统一视觉风格补充说明，可选",
  "outputs": [
    {
      "path": "src/assets/hero-dashboard.png",
      "size": "1536x1024",
      "prompt": "现代 SaaS 仪表盘主视觉，冷静专业，呼应品牌蓝绿色",
      "required": true,
      "used_by": ["src/pages/Home.tsx"]
    }
  ],
  "fallback_allowed": false
}
```

规则：

- `skill` 默认 `image-assets`。
- `outputs[].path` 必须在 `src/assets/` 下，禁止绝对路径、`..`、`public/`、远程 URL。
- `size` 限定为 `1024x1024`、`1536x1024`、`1024x1536`，未设置则使用 env 默认。
- `prompt` 是给 asset 子 agent 的语义描述，P1 planner 应尽量填。
- `required=false` 的 output 缺失只产生 warning，不阻断 task。
- `fallback_allowed=false` 时，缺图必须重试/blocked，不能用 SVG/emoji 假通过。

### 2.3 共享 Asset Lane 模块

新增：

- `backend/services/code/asset_lane.py`

建议 API：

```python
IMAGE_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_IMAGE_MODEL",
    "OPENAI_IMAGE_QUALITY",
    "OPENAI_IMAGE_SIZE",
    "FE_CODEX_TIMEOUT",
    "FE_GENIMAGE_TIMEOUT",
)

def docker_env_flags() -> list[str]:
    ...

def render_bootstrap(
    *,
    state_dir: str,
    style_context_path: str,
    diagnostics_dir: str,
    work_root_expr: str,
) -> str:
    ...

def normalize_outputs(resource_spec: dict) -> tuple[list[dict], list[str]]:
    ...

def validate_output_paths(outputs: list[dict]) -> tuple[bool, list[str]]:
    ...
```

`render_bootstrap` 负责生成 shell 片段：

- 创建 `$HOME/bin`、`$HOME/.fe-assets`、`$HOME/.claude/skills/image-assets`、`$CODEX_HOME`。
- 写入 `genimage.mjs`。
- 写入 `gen-assets`。
- 写入 `SKILL.md`。
- 写诊断文件：
  - `codex_path`
  - `gen_assets_path`
  - `asset_gen.log`
  - `codex_login.log`
- 若有 `OPENAI_API_KEY` 和 `codex`，执行 `codex login --with-api-key`。

关键改造：

- 现有一次性前端生成中 asset lane 使用 `/out/prompt.txt` 和 `/out/asset_gen.log`。
- Dev Mode 没有 `/out` mount，且 `exec_turn` 通过 stdin 喂 prompt。
- 因此 shared bootstrap 必须参数化：
  - 一次性生成：`state_dir=/out`，`style_context_path=/out/prompt.txt`，`diagnostics_dir=/out`。
  - Dev Mode：`state_dir=/tmp/dev-assets`，`style_context_path=/tmp/dev-assets/style_context.txt`，`diagnostics_dir=/tmp/dev-assets`。

`gen-assets` 读取风格上下文的逻辑应改为：

```bash
STYLE_CONTEXT_FILE="${ASSET_STYLE_CONTEXT_FILE:-/tmp/dev-assets/style_context.txt}"
if [ -f "$STYLE_CONTEXT_FILE" ]; then
  STYLE_CTX="$(head -c 6000 "$STYLE_CONTEXT_FILE")"
fi
```

不再写死 `/out/prompt.txt`。

### 2.4 接入一次性前端生成

修改 `backend/services/code/frontend_project_service.py`：

- 删除内嵌 asset lane 大段脚本。
- 替换为 `asset_lane.render_bootstrap(...)` 生成的 shell 片段。
- 保持现有行为和诊断字段不变：
  - `codex_available`
  - `gen_assets_available`
  - `openai_key`
  - `calls`
  - `assets`
  - `log`

这是为了保证 P2 不回归现有一次性前端项目生成。

### 2.5 接入 Dev 容器

修改 `backend/services/code/dev_service.py`：

1. `_DEV_ENTRYPOINT` 中注入 shared asset bootstrap。

位置建议：

- `export HOME/PATH` 后。
- `ANTHROPIC_PROXY_BOOTSTRAP` 后或前都可，但必须在 `npm run dev` 前完成。

Dev 参数：

```python
asset_lane.render_bootstrap(
    state_dir="/tmp/dev-assets",
    style_context_path="/tmp/dev-assets/style_context.txt",
    diagnostics_dir="/tmp/dev-assets",
    work_root_expr='$(cat /tmp/dev_project_root 2>/dev/null || echo /tmp/work)',
)
```

2. `start_container` 传入 image env。

当前已传：

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_IMAGE_MODEL`
- `OPENAI_IMAGE_QUALITY`
- `OPENAI_IMAGE_SIZE`

P2 补齐：

- `FE_CODEX_TIMEOUT`
- `FE_GENIMAGE_TIMEOUT`
- 如后续支持 Panlaxy/Gemini 原生图像，需要在 genimage 层抽象，P2 先沿用 OpenAI-compatible image endpoint。

3. 新增方法：

```python
def write_asset_context(self, project_id: str, text: str) -> bool:
    """Write style/task context into /tmp/dev-assets/style_context.txt."""
```

实现方式：`docker exec -i <container> bash -lc 'mkdir -p /tmp/dev-assets && cat > /tmp/dev-assets/style_context.txt'`，stdin 写入裁剪后的 context。

4. 新增方法：

```python
def asset_diagnostics(self, project_id: str) -> dict:
    """Read /tmp/dev-assets diagnostics from the running dev container."""
```

返回：

```json
{
  "codex_available": true,
  "gen_assets_available": true,
  "openai_key": true,
  "calls": 1,
  "log": "invoked: ...",
  "codex_login": "..."
}
```

5. 新增方法：

```python
def validate_resource_outputs(self, project_id: str, resource_spec: dict) -> dict:
    ...
```

返回：

```json
{
  "ok": true,
  "blocking": false,
  "reason": "",
  "outputs": [
    {"path": "src/assets/hero.png", "exists": true, "bytes": 183024, "required": true}
  ],
  "diagnostics": {}
}
```

校验策略：

- 先用 Python 归一化路径，拒绝绝对路径、`..`、不在 `src/assets/` 下的路径。
- 再 `docker exec` 到项目 root 执行 `test -s "$path"` 或 `stat -c%s "$path"`。
- 可选：`file --mime-type` 存在时检查 `image/png|image/jpeg|image/webp`。
- required output 缺失时 `ok=false`。
- `gen-assets` 不存在、Codex 不存在、OpenAI key 不存在时 `blocking=true`，不要消耗多次重试。

### 2.6 接入 code_dev_turn

修改 `backend/services/agent/workflows/code_dev_turn_workflow.py`。

在 edit 前：

- 若 `focus_task.category == "asset"` 或 `resource_spec.outputs` 非空：
  - 构造 asset context。
  - 调 `dev.write_asset_context(project_id, context)`。
  - emit `PROGRESS`：资源上下文已写入。

asset context 内容：

```text
# 项目视觉风格
<project.style_prompt 节选>

# 共识账本
<ledger.render_for_prompt 节选>

# 当前资源任务
任务 ID: ...
标题: ...
说明: ...
style_brief: ...
outputs:
- src/assets/hero.png 1536x1024: ...
```

在 verify 前或 verify 后：

- 对 asset task 调 `dev.validate_resource_outputs(project_id, resource_spec)`。
- 如果 `blocking=true`：
  - 直接 `dev_sprint_service.mark_blocked(task.id, reason, from_statuses={VERIFYING, IN_PROGRESS, QUEUED})`。
  - emit warning。
  - 本 turn 返回 completed 还是 failed 需要谨慎：建议 turn completed，任务 blocked。因为这是业务任务阻塞，不是 workflow 崩溃。
- 如果 `ok=false` 且非 blocking：
  - 将当前 task 的 AC 对应 feature 标为 false，note 写缺失 output。
  - 交给 `apply_verify_outcome` 走 retry budget。
- 如果 `ok=true`：
  - 把 outputs 结果写回 `resource_spec["verified_outputs"]`。
  - 正常执行 `apply_verify_outcome`。

这样 asset task 仍然复用 P0 状态机：

```text
pending -> queued -> in_progress -> verifying
  -> done       所有 required outputs 存在 + review 无阻断
  -> pending    图像调用/文件缺失等可重试问题
  -> blocked    缺 key/缺 codex/路径非法/重试耗尽
```

### 2.7 计费

修改 `backend/services/pricing.py`：

```python
CODE_DEV_ASSET_IMAGE = _credits("PRICE_CODE_DEV_ASSET_IMAGE", 0)
OPERATION["code_dev_asset_image"] = ("agent_run", CODE_DEV_ASSET_IMAGE)
```

计费点：

- `code_dev_turn` 仍按每回合预扣 `CODE_DEV_TURN`。
- asset task 在执行前按 required outputs 数量追加扣费：

```python
amount = pricing.CODE_DEV_ASSET_IMAGE * required_output_count
charge(
    user_id=ctx.user_id,
    team_id=run.team_id,
    amount=amount,
    operation="code_dev_asset_image",
    resource_type="agent_run",
    resource_id=ctx.run_id,
    description=f"Dev asset generation: {required_output_count} images",
)
```

扣费失败：

- 若 amount > 0 且余额不足，任务直接 blocked，reason 为 `图片资源生成积分不足`。
- 默认 amount=0 时无行为变化。

退款：

- 若未调用 `gen-assets` 且未产出任何 required output，且失败属于缺 key/缺 Codex 这类环境问题，可以退 asset image 追加扣费。
- 若 `gen-assets` 已调用图像 API 但质量/验收失败，不自动退款。

### 2.8 P1 Planner 与 P2 的配合

P1 生成 asset task 时必须：

- `category="asset"`。
- `lane="asset"`。
- `priority` 高于依赖它的 UI task。
- UI task 的 `depends_on` 引用 asset task 的 `feature_id`。
- `resource_spec.outputs[].path` 全部在 `src/assets/`。
- `acceptance_criteria` 至少包含：
  - 每个 required output 文件存在且非 0 字节。
  - 不使用远程图片 URL。
  - 后续 UI 任务通过 import 使用资源。

示例：

```json
[
  {
    "feature_id": "ASSET.FR1.1",
    "parent_feature_id": "FR1",
    "lane": "asset",
    "category": "asset",
    "title": "生成首页主视觉图片",
    "acceptance_criteria": [
      "src/assets/home-hero.png 存在且非 0 字节",
      "图片为真实位图资源，不是 SVG 占位或远程 URL"
    ],
    "resource_spec": {
      "skill": "image-assets",
      "outputs": [
        {
          "path": "src/assets/home-hero.png",
          "size": "1536x1024",
          "prompt": "A polished SaaS operations dashboard hero image, calm professional tone",
          "required": true
        }
      ]
    },
    "priority": 50
  },
  {
    "feature_id": "FR1.T1",
    "parent_feature_id": "FR1",
    "lane": "frontend",
    "category": "functional",
    "title": "实现首页 hero 区域",
    "depends_on": ["ASSET.FR1.1"],
    "acceptance_criteria": [
      "首页 hero 区域 import 并展示 src/assets/home-hero.png",
      "图片在 /preview/<project_id>/ 子路径下能正常加载"
    ],
    "priority": 20
  }
]
```

### 2.9 前端展示

P2 前端最小改动：

- `DevChecklistPanel` 或任务列表中对 `category=asset` 显示资源图标和 outputs 数量。
- task detail 展示：
  - output path
  - size
  - generated/verified 状态
  - 缺 key/缺 Codex/缺文件的 blocked reason
- Sprint log 中展示 asset lane diagnostics warning。

无需新增独立 asset 页面。

### 2.10 P2 验收标准

- Dev 容器启动后，`docker exec dev-xxx command -v gen-assets` 成功。
- Claude/Codex 可通过 `image-assets` skill 调用 `gen-assets`。
- `category=asset` task 能生成 `src/assets/*.png`。
- 缺 `OPENAI_API_KEY` 时 task blocked，原因清晰，不会无限 retry。
- 缺 Codex CLI 时 task blocked，原因清晰。
- output 路径非法时 task blocked。
- required output 缺失时按 retry budget 回 `pending`，耗尽后 `blocked`。
- output 存在且 review 无阻断时 task `done`。
- 一次性前端生成的 asset lane 行为不回归。

### 2.11 P2 测试

新增 `tests/test_dev_asset_lane.py`：

- `render_bootstrap` 包含 `gen-assets`、`genimage.mjs`、`image-assets/SKILL.md`。
- `render_bootstrap` 不硬编码 `/out/prompt.txt`。
- `normalize_outputs` 拒绝绝对路径、`..`、非 `src/assets/`。
- `validate_resource_outputs` 对 fake docker output 返回 ok/missing/blocking。
- 缺 Codex/缺 key diagnostics 能映射成 blocking。

扩展 `tests/test_dev_sprint.py`：

- asset task missing outputs -> retry。
- asset task missing outputs 且 retry exhausted -> blocked。
- asset task unavailable diagnostics -> blocked，不 retry。
- asset task ok -> done。

扩展一次性前端生成测试：

- 共享 bootstrap 后仍能读回 asset lane diagnostics。

不在单测中真实调用 OpenAI 图像 API；集成测试可单独标记 `@pytest.mark.integration`。

## 3. 推荐实现顺序

1. P1 backend model + service + prompt + workflow registration。
2. P1 routes + tests，先不做复杂前端编辑器，只返回 draft/apply。
3. P1 frontend panel，完成用户确认闭环。
4. P2 抽出 `asset_lane.py`，先保持一次性前端生成完全等价。
5. P2 注入 Dev 容器，并补 env/context/diagnostics。
6. P2 接入 `code_dev_turn` 的 asset output 校验。
7. P2 前端展示 diagnostics。

这样排序的原因：

- P0 已能执行任务板，P1 先解决“任务从哪里来”。
- P2 的最大价值来自 P1 自动生成 `asset` task 和依赖关系。
- `asset_lane.py` 先抽共用模块，可以降低 Dev 注入时破坏一次性生成的风险。

## 4. 风险与控制

| 风险 | 控制 |
|---|---|
| Planner 输出任务太大 | prompt 强制单 turn 粒度；normalizer 限 AC 和描述；UI 可拆分 |
| Planner 生成依赖环 | normalize 阶段检测，拒绝 apply |
| Planner 覆盖已完成任务 | apply 复用 `tasks/bulk` 保护，不覆盖 done/active |
| Prompt Mongo 未同步 | 修改 `.txt` 后必须跑 sync 脚本；测试覆盖 prompt key |
| Dev 容器无 `/out` | P2 bootstrap 参数化 state/diagnostics path |
| 缺图像 key 导致无限 retry | diagnostics 映射 blocking，不走 retry |
| 资源路径逃逸 | Python 归一化 + shell 侧二次检查，只允许 `src/assets/` |
| 图像 API 慢 | `FE_CODEX_TIMEOUT`、`FE_GENIMAGE_TIMEOUT` 有上限；失败按任务状态机处理 |
| 一次性前端生成回归 | 先抽共享模块并保持原 diagnostics contract，补单测 |

## 5. 完成定义

P1 完成定义：

- 用户无需手写 JSON，即可由项目文档生成可确认任务草案。
- 草案应用后，P0 sprint 能逐个执行。
- Planner 全链路有 AgentRun 回放、pricing、API、前端入口和单测。

P2 完成定义：

- `asset` task 不再只是 prompt 描述，而是能在 Dev 容器内真实调用 skill 产出位图。
- 产物存在性和路径安全由后端强校验，不依赖模型自述。
- 缺配置、缺工具、缺产物都能落到明确任务状态，不会让 sprint 假完成或无限运行。
