# 可组合工作流(蓝图)设计规范

> 本文是「把当前写死的固定流水线,演进为数据驱动、可组合的蓝图式工作流」的权威设计。
> 与 [`agent-context-ledger.md`](./agent-context-ledger.md)、[`code-fullstack-generation.md`](./code-fullstack-generation.md)、
> [`code-domain-handoff.md`](./code-domain-handoff.md) 同规格,实施以本文为准。
>
> 状态:**实施中(2026-06-26)**。契约层已落地;本文为权威设计。

---

## 重要更正(2026-06-26):复用现有 remix 画布,**不新建** WorkflowGraph

落地前核查发现:平台**已有**一套通用节点图编排子系统(代码注释原话「n8n-style remix canvas」),本文早期草案提出的 `WorkflowGraph` 新模型 + 平行 DAG 引擎 + 平行校验器属**重复建设**,据此更正:

| 早期草案(作废) | 更正为(复用现有) |
|---|---|
| 新建 SQL `WorkflowGraph` 存图 | **复用 `CodeCanvas`**(`code_canvases` 表,nodes/edges JSON,per-project) |
| 新写拓扑/环检测/边引擎 | **复用 `dag_engine.CanvasGraph`**(已有 `topo_order`/环检测/incoming/outgoing) |
| 新写节点执行循环 | **扩展 `code_canvas_workflow` 的循环**(已有剪枝/计费/落库) |
| §6 的 `WorkflowGraph` / `WorkflowGraphNodeUsage` | **作废**,见 §6 顶部标注 |

**本设计的真正增量** = 给现有 freeform 画布(节点仅 `source/agent/merge/branch`,边无类型)补上它缺的**typed 层**:把真实生成阶段(requirements/flow/…/deploy)做成**typed 契约节点**,加**端口类型**与**校验**,加**提示词版本钉定**。

**已实现(2026-06-26,零迁移,后端 281 测试通过 + 前端 build/lint 通过):**
1. **契约层** `backend/services/agent/contracts/` —— `ports.py`(端口类型注册表)、`node_contract.py`(`NodeContract`/`spec_hash`/`to_catalog`)、`defaults.py`(9 阶段→typed 契约)、`validate.py`(`validate_graph` 基于 `CanvasGraph`,typed 与 freeform 共存)。`tests/test_node_contracts.py`。
2. **提示词版本钉定**(§5)—— `prompt_store` 加 `prompt_versions` + `head_pin`/`get_pinned`(内容寻址;HEAD `get` 不变)。`tests/test_prompt_versioning.py`。
3. **接入画布**(§6,扩展 `CodeCanvas`)—— `STAGE` 节点类型 + `run_stage_text_node`(契约+钉定)+ 画布循环 + `validate_graph` 闸 + `GET /api/code/node-contracts` + 前端 `StageNode`/面板/四语言 i18n。`tests/test_canvas_workflow.py`、`tests/test_canvas_routes.py`。
4. **冻结/发布(§5.4)** —— `contracts/freeze.py::freeze_stage_prompts` + `POST .../canvases/<id>/freeze`(把每个阶段节点的 `head_pin` 戳进 `config.prompt_pin`,存图 JSON,**零迁移**)+ 前端「冻结」按钮 + `StageNode` 钉定标识。这一步**接通了** `head_pin → prompt_pin → resolve_prompt → get_pinned` 全链路:冻结后的画布即便 prompt 被改也跑旧版本。`tests/test_canvas_routes.py::test_freeze_canvas_pins_stage_prompts`。

5. **`stage_preview` 执行器** —— `canvas_nodes.run_stage_preview_node` 复用 `generate_preview_images`(尊重 `AI_IMAGE_PROVIDER`),从上游风格文本出 UI 预览图并存 IMAGE artifact;`WIRED_EXECUTORS={stage_text,stage_preview}` 统一驱动「可执行」标记与画布 dispatch。`tests/test_canvas_workflow.py::test_typed_preview_stage_generates_image_artifacts`。
6. **typed PortValue 引用 I/O(§3.3)** —— `ports.make_port_value`(引用信封,非字节);画布循环维护 `port_outputs`(stage 节点产出携带 `code_document`/`agent_artifact` ref);stage 节点**按端口名解析输入**(`sourceHandle→targetHandle`,携带上游 PortValue),取代「拼接全部上游文本」;freeform 节点仍走文本回退。`tests/test_canvas_workflow.py`(断言需求文档落在 flow 的 `requirements` 端口)。
7. **绑定持久化(§7)+ 引入 Alembic** —— `AgentStep.port_bindings_raw` 加列(getter/setter + `StepHandle.set_port_bindings`),画布循环把每个 stage 节点的 typed 输入(按端口→ref)/输出/`prompt_pin` 记进 step,可回放数据血缘。**已引入 Alembic**:`backend/alembic.ini` + `migrations/env.py` + `0001_port_bindings`(**守卫式:缺列则加/无表则跳/已有则幂等**,任何顺序安全,新库由 `create_all` 建列、旧库由迁移补列)。已冒烟验证补列+幂等。**注意**:平台早有 `schema_guard.ensure_model_columns()` 在每次 boot(serving 之前)自愈加列,故 `port_bindings_raw` **无坏列窗口**、`redeploy` **不自动跑迁移**;Alembic 仅保留给**非加列**迁移(改名/删列/类型/数据,schema_guard 做不了),**手动**执行 `make migrate-prod`(容器)/`make migrate`(本地)。`tests/test_canvas_workflow.py`(断言 step 记录 typed inputs/outputs ref)。

8. **deploy 契约拆分(后端中心)+ 现有产物 source** —— deploy 输入改 `backend` 必填 + `middleware` 可选,**去掉 frontend**(前端是 `/preview` 静态服务,非 deploy 输入;`deploy_service` 本就是后端容器中心)。「全栈部署」由此退化为画布上「所有 lane 连到 deploy」的一种组合,而非唯一路径:纯前端 = `fe_build`(无需 deploy)、已有前端只部署后端 = `[existing_backend source] → deploy`、全栈 = 三 lane 全连。新增 `existing_frontend/backend/contract/middleware` source 节点 + `_source_port_value` typed 引用解析(复用现有产物不重跑)。`tests/test_node_contracts.py`(deploy 后端中心 + `existing_backend→deploy` 校验通过 + 缺 backend 被拒)。

9. **deploy 执行器接入** —— `canvas_nodes.run_stage_deploy_node` 薄封装 `deploy_service.deploy`(与线性全栈部署**同一引擎**:建库 → 构建后端镜像 → 起容器 → 健康检查 → 注册 `/app/<pid>/api` 反代,失败有序回滚);on_phase 叙事、`STOPPED`→取消、失败→raise(已回滚);发 `code:deployment` PortValue + 部署元数据 artifact。`WIRED_EXECUTORS` 加 `deploy`,前端面板自动放开。部署项目**当前**后端,因此「已有后端 → 部署/重部署」直接在画布可跑。`tests/test_canvas_workflow.py::test_typed_deploy_stage_runs_via_deploy_service`(monkeypatch deploy_service)。

10. **前端「现有产物」source 入口(P0#1)** —— 工具栏「现有产物…」下拉 `addSourceNode` 创建 `existing_frontend/backend/contract/middleware` source 节点(可删,`fromCanvas` deletable 兼容),四语言 i18n。让「已有后端 → deploy」在 UI 上能拼出来。
11. **`container_be` 执行器(P0#2)** —— `canvas_nodes.run_stage_be_node` 复用 `backend_project_service.build_project`(be-agent 容器),产物用与线性一致的 `domain_ref`(`code_backend_project_zip`/`project_id`);`deploy_service` 加**附加 fallback** `_latest_backend_run`(无线性后端 run 时回退到项目最新 backend zip 任意 run,既有行为不变)→ **画布 `be_build → deploy` 端到端打通**。`tests/test_canvas_workflow.py`(be_build 生成 + deploy fallback)。

12. **画布 review_gate 暂停/续跑(P1#3)** —— 给画布 run 加**可恢复性**:review-gated 阶段节点(requirements/flow/documents/style)产出后**暂停**(`pause_at_node`:把已完成节点状态快照存进 run config `_canvas_state` + `progress.review_stage` + 发 `STEP_AWAITING_REVIEW`),**复用现有通用 `/runs/<id>/resume` 端点 + `_resume` 指令**;续跑时跳过已完成节点、approve 继续 / revise 携指令重跑该节点;前端 `canvasStore` 检测 `step_awaitng_review` → 确认条(approve/revise + 四语言 i18n)。`tests/test_canvas_workflow.py`(暂停→approve→再暂停→approve→完成)。

13. **画布 ledger 回写(P1#4)** —— `ledger_writeback.merge_stage_doc_into_ledger` 按阶段类型从产出文档抽取并 merge 进共识账本(复用 `code_workflow` 的 md 助手:requirements→产品定位/FR-NFR/技术架构约束、flow→技术假设、documents→章节术语、style→UI 基调决策),画布循环在 stage_text 节点后调用并 `_persist_ledger`;**并修正账本重载**——续跑时优先用本 run 自己累积的账本(否则 review-gate 暂停后写回会被「从 full-generation run 重载」覆盖丢失)。`tests/test_canvas_workflow.py`(抽取单测 + 写回持久化跨暂停)。

14. **`container_fe` + `provision_mw` 执行器** —— `run_stage_fe_node`(复用 `frontend_project_service`,出 zip + run-scoped site 预览)、`run_stage_mw_node`(复用 `middleware_service`,出 meta/sql),均用线性一致 domain_ref;`deploy_service` 的 fe/mw run 查找泛化为 `_latest_run_for_artifact`(附加 fallback,既有不变)。`tests/test_canvas_workflow.py`(fe/mw 生成 + 产物 domain_ref)。

**🎉 至此全部 9 个阶段执行器接入完毕——「从零全栈在画布上编排(需求→流程→文档→风格→预览→fe/be/mw→deploy)」闭环达成。**

**后续(P2 精化,非阻断):** 前端连线时实时类型校验(`isValidConnection`)、冻结钉契约版本(现仅钉 prompt)、节点契约 Mongo 覆盖、画布模板/预设、`/preview/<pid>/` 会话路由认画布前端、绑定血缘前端展示、计费用契约 `pricing_key`。**Alembic 地基已就位**。

下文 §3/§4/§5/§8 仍然有效;§6 改读「扩展 CodeCanvas」,§11 见本节进展。

---

## 0. 一句话定位

把现在硬编码在 `code_workflow.py`(`_TAIL` / `_STAGE_AFTER` / `_run_from` 的 if-elif)和前端 `stages.ts`(`DISPLAY_STAGES` 常量)里的「阶段流水线」,翻译成**数据**:

- **节点(Node)** = 一个可复用的执行单元(= 现在的一个阶段),由 **节点契约(NodeContract)** 声明其 typed 输入/输出端口、上下文读写边界、提示词钉定、计费与执行器。
- **蓝图(WorkflowGraph)** = 用户把节点连成的图(DAG),保存后**冻结**所有版本钉定,从而可复现、可回放。
- **解释器(Interpreter)** = 一个领域无关的通用引擎,按图执行,替换掉现有的 if-elif 调度器。

设计总纲(详见 §1):**机制通用,策略专用** —— 引擎不认识 Code 域的任何具体阶段;出厂的节点契约就是 Code 域那一套。

---

## 1. 范围与设计原则

### 1.1 通用 vs 专用的边界

| 现在就做通用(便宜,防止把业务焊死进引擎) | 现在**不做**(YAGNI,等出现第二个域再说) |
|---|---|
| 解释器只跑「图 + 端口 + 契约」,不 import Code 域常量 | 用户在 UI 自定义新端口类型 |
| 端口类型用**带命名空间的注册表** | 第三方节点插件 SDK / 沙箱执行 |
| 提示词版本钉定、所有持久化 blob 带 `schema_version` | 跨域节点市场 / 可视化 DSL 导出 |
| 节点契约从 Mongo **数据驱动**(default-seed + override) | 多域并存的权限 / 计费矩阵 |

**判据**:在拿到第二个消费域来证伪之前,不构造「猜出来的通用」。本仓库当前唯一消费方是 Code 域(见 `CLAUDE.md`)。

### 1.2 不可谈判的前置条件

> ✅ **已落实(2026-06-26):Alembic 已引入**(`backend/alembic.ini` + `migrations/`,首个迁移 `0001_port_bindings`)。下文保留当初的论证;运营模型见 §13。

> ⚠️ **(原始论证)本功能进入多租户生产前,必须先引入数据库迁移(Alembic)。**

仓库现状靠 `backend/app.py` 的 `db.create_all()` 建表,**它只创建缺失的整张表,不会给已有表加列、不会改列类型**(`CLAUDE.md` 已确认无 Alembic、无 migrations)。本设计要给 `AgentStep` 加列、给 Mongo `prompts` 加字段——在已有生产库上 `create_all` **不会生效**。

- 1 个客户:可以 drop 重建,无所谓。
- 1000 个客户:你有一个**不能重建**的生产库,这条直接卡死。

因此实施顺序里,**Alembic + 第一个 migration 是 P0 的 P0**,见 §11。

### 1.3 与回放的关系(贯穿全文的硬约束)

`AgentRun` 全流程可回放是产品命根子。任何「运行时按当下状态解析输入」的设计都会破坏可复现性。本设计的对策是**冻结 + 快照**:

1. 蓝图 publish 时把节点契约版本、提示词版本**冻结**进图(内容寻址)。
2. 运行时仍把**实际用到的** prompt 全文、端口绑定记进 `AgentStep`(双保险:即便版本文档将来被清理,回放仍可复现)。

---

## 2. 总体架构(分层)

```
┌─────────────────────────────────────────────────────────────┐
│ 前端画布编辑器(P2) —— 拖节点/连线,实时跑 validate_graph    │
└───────────────┬─────────────────────────────────────────────┘
                │ 读 node_contracts 渲染端口
┌───────────────▼─────────────────────────────────────────────┐
│ 蓝图实例 WorkflowGraph(SQL,用户数据,owner/visibility)      │
│   draft → published(冻结所有 pin)                            │
└───────────────┬─────────────────────────────────────────────┘
                │ get_workflow("graph:<id>") → 通用解释器
┌───────────────▼─────────────────────────────────────────────┐
│ 通用解释器 Interpreter(领域无关,替换 _run_from 的 if-elif)  │
│   拓扑排序 → 逐节点解析端口绑定 → 调执行器 → 写 ledger/产物    │
└───────┬───────────────────────────┬─────────────────────────┘
        │ 读节点定义                 │ 复用现有底座
┌───────▼──────────────┐   ┌────────▼─────────────────────────┐
│ 节点契约目录(Mongo)  │   │ AgentRun/Step/Event/Artifact      │
│ node_contracts(HEAD) │   │ recorder + SSE bus + ContextLedger│
│ node_contract_versions│   │ provider 抽象 + pricing + 积分    │
│ 提示词 prompts(HEAD) │   └───────────────────────────────────┘
│ prompt_versions(冻结)│
└──────────────────────┘
```

复用关系一览见 §10。

---

## 3. Port 类型系统

节点之间**传引用信封,不传字节**(引用 = 可重新解析的 ID,既是 typed 校验基础,也让图保持小、回放友好)。

### 3.1 类型注册表(命名空间,非封闭枚举)

```python
# backend/services/agent/contracts/ports.py —— 纯 Python,无 Flask/DB import
PORT_SCHEMA_VERSION = 1

# 注册表而非中央封闭 set:新域自注册自己的端口类型,不改公共代码。
# key 一律带命名空间 "<domain>:<name>",引擎只校验「已注册 + 字符串相等」。
_PORT_TYPES: dict[str, "PortTypeDef"] = {}

def register_port_type(key: str, *, ref_kinds: set[str], describe: str) -> None: ...
def is_registered(key: str) -> bool: ...

# Code 域出厂注册(策略,不在引擎里):
register_port_type("core:context_ledger", ref_kinds={"inline_json"}, describe="横切共识")
register_port_type("core:user_text",      ref_kinds={"inline_text"}, describe="用户指令/run 输入")
register_port_type("code:requirements_doc",    ref_kinds={"code_document"}, describe="需求文档")
register_port_type("code:development_flow",    ref_kinds={"code_document"}, describe="开发流程")
register_port_type("code:document_set",        ref_kinds={"code_document"}, describe="拆分文档集")
register_port_type("code:style_doc",           ref_kinds={"code_document"}, describe="风格文档")
register_port_type("code:ui_preview",          ref_kinds={"agent_artifact"}, describe="UI 预览图")
register_port_type("code:api_contract",        ref_kinds={"code_ledger_field"}, describe="共享 OpenAPI 契约")
register_port_type("code:frontend_project",    ref_kinds={"agent_artifact"}, describe="前端工程产物")
register_port_type("code:backend_project",     ref_kinds={"agent_artifact"}, describe="后端工程产物")
register_port_type("code:middleware_manifest", ref_kinds={"code_ledger_field"}, describe="中间件清单")
register_port_type("code:asset_manifest",      ref_kinds={"agent_artifact"}, describe="图形资源 manifest")
register_port_type("code:deployment",          ref_kinds={"code_deployment"}, describe="部署登记")
```

> Q1 修正:原方案的封闭 `PORT_TYPES` set 会让每加一类内容都要改代码 + 重新部署;改成注册表后,引擎对类型只做「是否已注册 + 字符串相等」判断,通用性与校验性兼得。

### 3.2 引用种类 `REF_KINDS`

值永远描述「怎么再取到它」,不内联大内容:

```python
REF_KINDS = {
    "code_document",     # ref_id = CodeDocument.id
    "agent_artifact",    # ref_id = AgentArtifact.id
    "code_ledger_field", # ref_id = CodeProjectLedger.id, field = "api_contract"
    "code_deployment",   # ref_id = CodeDeployment.id
    "inline_json",       # value 内联(小结构,如影响分析)
    "inline_text",       # value 内联文本
}
```

### 3.3 PortValue(端口上流动的值)

```jsonc
{
  "type": "code:requirements_doc",   // 已注册类型 key
  "ref_kind": "code_document",       // ∈ REF_KINDS,且 ⊆ 该类型声明的 ref_kinds
  "ref_id": "9f1c…",                 // 按 ref_kind 解释
  "field": null,                     // 仅 code_ledger_field 用
  "value": null,                     // 仅 inline_* 用
  "produced_by": "n1",               // 产出它的节点实例 id(溯源/回放)
  "port_schema_version": 1
}
```

`core:context_ledger` 是**横切端口**:不走普通连线,而是每个节点按契约 `context.reads/writes` 读写 run 级 ledger(沿用 `ContextLedger.render_for_prompt()` 注入 + `merge()` 增补)。

---

## 4. 节点契约 Schema(NodeContract = 蓝图里的「组件」)

存 **MongoDB**,完全镜像 `prompt_store` 的 default-seed + override + 60s TTL 缓存模式。新增两个集合:`node_contracts`(HEAD)、`node_contract_versions`(不可变历史)。`defaults.py` 同款提供 `iter_default_node_contracts()` 作为 seed 源与 fallback。

### 4.1 `node_contracts` 文档(`_id = node_type`,即 HEAD)

```jsonc
{
  "_id": "requirements",            // 节点类型 key(稳定)
  "version": 3,                     // 当前 HEAD 版本(int 标签,见 §5)
  "spec_hash": "sha256:…",          // 本版本 spec 规范化后的内容哈希(冻结身份)
  "name": "需求文档生成",
  "category": "code",
  "role": "generator",              // 对齐 AgentStep.role:planner|generator|critic|publisher

  // —— typed 端口契约(任意重组安全的根基)——
  "inputs": [
    { "name": "brief",  "type": "core:user_text",      "required": true  },
    { "name": "ledger", "type": "core:context_ledger", "required": false }
  ],
  "outputs": [
    { "name": "doc", "type": "code:requirements_doc" }
  ],

  // —— 上下文契约(复用 ContextLedger,声明读写边界)——
  "context": {
    "ledger_schema_version": 1,
    "reads":  ["project", "requirements", "open_questions"],
    "writes": ["requirements", "glossary", "decisions", "tech_stack"]
  },

  // —— 提示词钉定(§5)——
  // 草稿期 version/hash 可为 null = 跟 HEAD;publish 图时冻结成具体值。
  "prompt_ref": { "key": "code/requirements_prompt.txt", "version": null, "hash": null },

  // —— 闸门 / 计费 / 执行器 ——
  "review_gate": true,                     // 对齐 code_workflow.REVIEW_STAGES
  "pricing_key": "CODE_FULL_GENERATION",   // 必须 ∈ pricing.py 常量,publish 时校验
  "executor": "prompt_text",               // ∈ 已注册执行器,见 §4.3

  "is_overridden": false,                  // 与 prompt 文档同义
  "updated_at": "…", "updated_by": null
}
```

### 4.2 纯 Python 校验类型(`contracts/node_contract.py`,无 DB)

```python
@dataclass
class Port:
    name: str
    type: str            # 已注册端口类型 key
    required: bool = False

@dataclass
class PromptRef:
    key: str
    version: int | None = None   # None = 跟 HEAD(仅草稿期允许)
    hash: str | None = None

@dataclass
class NodeContract:
    node_type: str
    version: int
    spec_hash: str
    role: str
    inputs: list[Port]
    outputs: list[Port]
    context_reads: list[str]
    context_writes: list[str]
    prompt_ref: PromptRef
    review_gate: bool
    pricing_key: str
    executor: str

    def output_type(self, name: str) -> str | None:
        return next((p.type for p in self.outputs if p.name == name), None)
```

### 4.3 执行器(executor)枚举

执行器是「节点契约 → 实际干活的代码」的桥,领域无关地注册:

| executor | 干什么 | 复用现有 |
|---|---|---|
| `prompt_text` | 取 prompt → 调 text provider → 解析 → 写产物/ledger | `code_workflow` 的各 `_do_*` 逻辑 |
| `container_fe` | docker `fe-agent` 出前端工程 | `code_frontend_project_workflow` |
| `container_be` | docker `be-agent` 出后端工程 | `code_backend_project_workflow` |
| `provision_mw` | 中间件 schema/迁移/seed 产物 | `code_middleware_workflow` |
| `image_gen` | 调 image provider 出位图资源 | `codex-image` / image factory |
| `analysis` | 出结构化 JSON(如二开影响分析) | 新增 |
| `deploy` | 建库/构建/起容器/健康检查/反代 | `deploy_service` |

> 现有 7 阶段 + fe/be/mw/deploy 一一映射成 NodeContract,就把「写死的调度」彻底翻译成数据。

---

## 5. 提示词版本钉定 Schema

**现状缺口**:`prompt_store.update()` 是**原地覆盖** `content`,无历史、无版本号。蓝图引用某 prompt key,每次跑拿到的都是「当下 HEAD」→ 回放不可复现。

对策:在现有 `prompts` 之上加一层**不可变版本**,HEAD 行为完全不变(在跑的产品零影响)。

### 5.1 `prompts`(HEAD 指针,现有集合,加两字段)

```jsonc
{
  "_id": "code/requirements_prompt.txt",
  "content": "…当前内容…",       // get(key) 仍读这里 —— 现有调用全不动
  "version": 7,                  // 新增:当前 HEAD 版本(int 标签)
  "content_hash": "sha256:…",    // 新增:= 版本表里 v7 的哈希(真身份)
  "default_content": "…", "is_overridden": true, "updated_at": "…"
}
```

### 5.2 `prompt_versions`(新集合,只追加、永不改)

```jsonc
{
  "_id": "code/requirements_prompt.txt@sha256:abcd…",  // key@hash 天然唯一、内容寻址
  "key": "code/requirements_prompt.txt",
  "version": 7,                  // 人看的标签
  "content": "…该版本完整内容(冻结)…",
  "content_hash": "sha256:abcd…",
  "parent_hash": "sha256:…",
  "created_at": "…", "created_by": "admin_user_id"
}
```

> Q1 修正:**`content_hash` 才是冻结身份,`version` int 只是人看的标签。** `_id` 用 `key@hash` 而非 `key@version`,因为单调自增 int 的「算下一个号」是 read-modify-write,多 worker 下会撞号(见 §9)。要展示序号时,序号由 Mongo 原子 `$inc` 维护,失败也不影响内容寻址正确性。

### 5.3 PromptStore 新增方法(其余不动)

```python
class PromptStore:
    def get(self, key) -> str: ...                 # 不变:取 HEAD(在线编辑即时生效)

    def head_pin(self, key) -> dict:               # 返回 {key, version, hash} —— publish 时冻结
        ...
    def get_pinned(self, key: str, content_hash: str) -> str:  # 按内容寻址取冻结内容
        ...

    # update():写 HEAD 的同时 append 一条 prompt_versions;
    #   content_hash 不变则视为 no-op,不新增版本(去重)。
    # seed_defaults():给新 key 建第一个版本。
    # 旧文档无 version/hash → 视为 legacy(version=0);首次编辑生成首个不可变版本。
```

### 5.4 钉定生命周期

```
草稿态:节点 prompt_ref = {key, version:null, hash:null}  →  解析走 get(HEAD)   # 开发期跟最新
   │  用户点击「发布工作流」(freeze)
   ▼
发布态:对每个节点调 head_pin(key),把 {version, hash} 写死进图(§6)
   →  之后该图每次运行 get_pinned(key, hash),内容永远一致 → 回放可复现
```

---

## 6. 蓝图实例:复用 `CodeCanvas`

> ⚠️ **本节早期版本提出的新建 `WorkflowGraph` / `WorkflowGraphNodeUsage` 已作废**(见顶部「重要更正」)。蓝图就是现有的 **`CodeCanvas`**(`backend/models/code/canvas.py`):per-project、`nodes_raw`/`edges_raw` 存 JSON、已有 owner/team、已有 `last_run_id` 供回放。typed 节点只是新增一种节点形态,**不需要新表**。
>
> 下面保留的 `graph_raw` 节点/边形状,在实现里就是写进 `CodeCanvas.nodes_raw`/`edges_raw`;typed 节点的判定 = 节点 `data.config.contract_key` 命中某契约。「哪些画布用了某契约/某 prompt 版本」的治理查询,优先用一张轻量关联行表实现(原 §6.3),需要时再加。

节点契约是「组件库」(Mongo,全局共享);**用户拼出来的图就是 `CodeCanvas` 实例**(已是用户数据,带 owner/team)。

### 6.1 模型

```python
# backend/models/agent/workflow_graph.py
class WorkflowGraph(db.Model):
    __tablename__ = "workflow_graphs"
    id          = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id     = db.Column(db.String(36), nullable=False, index=True)
    team_id     = db.Column(db.String(36), nullable=True, index=True)
    visibility  = db.Column(db.String(20), default="private")   # private|team|public
    title       = db.Column(db.String(200))
    status      = db.Column(db.String(20), default="draft")     # draft|published
    graph_raw   = db.Column(db.Text)        # §6.2;publish 后 pin 全部冻结
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def get_graph(self) -> dict: return self._load_json(self.graph_raw, {})
    def set_graph(self, d):      self.graph_raw = json.dumps(d or {}, ensure_ascii=False)
    # _load_json / to_dict 同 AgentStep 写法
```

### 6.2 `graph_raw`(发布态,pin 已冻结)

```jsonc
{
  "schema_version": 1,
  "ledger_schema_version": 1,            // 钉住 ContextLedger schema
  "nodes": [
    {
      "id": "n1",                        // 图内实例 id
      "node_type": "requirements",
      "contract_version": 3,             // 冻结:节点契约版本
      "contract_hash": "sha256:…",       // 冻结:契约 spec 身份
      "prompt_pin": { "key": "code/requirements_prompt.txt", "version": 7, "hash": "sha256:…" }
    },
    { "id": "n2", "node_type": "flow", "contract_version": 2, "contract_hash": "…", "prompt_pin": {…} }
  ],
  "edges": [
    { "from": "n1", "out": "doc", "to": "n2", "in": "requirements" }
  ]
}
```

### 6.3 治理用关联行(可查,非 blob)

> Q1 修正:`graph_raw` 整块读执行没问题,但「哪些图用了 `requirements@v3` / 某 prompt 版本」若只存在 JSON 里,1000 客户时做废弃/迁移只能全表扫。因此 publish 时**额外**落一张可查行表:

```python
class WorkflowGraphNodeUsage(db.Model):
    __tablename__ = "workflow_graph_node_usage"
    id           = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    graph_id     = db.Column(db.String(36), db.ForeignKey("workflow_graphs.id"), index=True)
    node_type    = db.Column(db.String(60), index=True)
    contract_hash= db.Column(db.String(80), index=True)
    prompt_key   = db.Column(db.String(120), index=True)
    prompt_hash  = db.Column(db.String(80), index=True)
```

→ 「找出仍在用某废弃 prompt/契约的所有蓝图」变成一条索引查询,而非全表 JSON 扫描。

### 6.4 解释器接入既有 runtime

蓝图作为一种 workflow key 注册:`get_workflow("graph:<graph_id>")` 解析出图 → 交给**通用解释器** `run_graph_workflow(ctx, recorder)`。它替换 `code_workflow._run_from` 的 if-elif:

```
拓扑排序(无环已由 §8 校验保证)
for 每个节点(按拓扑序,可并行无依赖节点):
    if ctx.is_cancelled(): return cancel_result()
    bindings = 解析该节点每个 input 端口 ← 上游 output 或 run 输入
    with recorder.step(node.id, …, role=contract.role) as step:
        ledger 注入 = ContextLedger.render_for_prompt()(按 contract.context.reads 投影)
        result = executor_registry[contract.executor](node, bindings, ledger, ctx, recorder)
        step.set_port_bindings({...})        # §7
        把 result 的产物登记为 typed PortValue(供下游解析)
        ledger.merge(... 仅限 contract.context.writes 段 ...); persist_ledger()
    if contract.review_gate: return pause_at(node.id)   # 复用现有人机闸门
```

`WORKFLOW_COSTS` 白名单改为:固定内建 key 照旧;`graph:*` 的计费 = 图内各节点 `pricing_key` 之和,创建 run 时按图预扣(沿用现有「预扣 + 失败无产物退款」)。

---

## 7. 运行期绑定快照(扩展 AgentStep,保回放)

```python
# AgentStep 新增一列(需 migration,见 §11)
port_bindings_raw = db.Column(db.Text, nullable=True)

def get_port_bindings(self) -> dict: return self._load_json(self.port_bindings_raw, {})
def set_port_bindings(self, d):      self.port_bindings_raw = json.dumps(d or {}, ensure_ascii=False)
```

```jsonc
// port_bindings 内容
{
  "node_id": "n2",
  "node_type": "flow",
  "inputs":  { "requirements": { "type": "code:requirements_doc", "ref_kind": "code_document", "ref_id": "…" } },
  "outputs": { "doc": { "type": "code:development_flow", "ref_kind": "code_document", "ref_id": "…" } },
  "prompt_pin": { "key": "code/development_flow_prompt.txt", "version": 4, "hash": "sha256:…" }
}
```

回放三重锚定:`prompt_pin`(冻结内容)+ 现有 `prompt_snapshot`(实际全文,双保险)+ `port_bindings`(数据来龙去脉)→ 任何 run 完全可复现。

---

## 8. 校验规则(确定性,无向量库)

发布前对图做一次**纯确定性**校验(对齐 `context_verifier` 的确定性检查风格):

```python
def validate_graph(graph: dict, contracts: dict[str, NodeContract]) -> list[str]:
    errors = []
    # 1. 拓扑:无环、无悬空节点(所有非源节点都有入边)
    # 2. 端口类型匹配:每条 edge 的 from.out 类型 == to.in 类型,且两端类型都 is_registered
    # 3. 必填输入全部被连上(或来自 run 输入 core:user_text)
    # 4. 上下文一致性:
    #      - 每个 context.writes/reads 段名 ∈ ContextLedger 合法段
    #      - 【并行写防护】不存在两个「互无依赖」的节点写同一 ledger 段(§9)
    # 5. pricing_key ∈ pricing.py 常量;executor ∈ executor_registry
    # 6. 发布态:每个 prompt_pin.hash 非空,且 get_pinned 取得到;contract_hash 与当前契约一致或为已冻结历史
    return errors
```

类型匹配只是**字符串相等** —— 编排是控制流问题,不需要语义检索/向量库。

---

## 9. 上下文契约与并行写 scope

`ContextLedger` 是为**线性流**设计的共享可变全局(merge 按 id 去重、last-write-wins)。真做成任意 DAG 并行后,多个并发节点 merge 同一段 = 数据竞争。对策分两步:

- **P0/P1(校验拦截)**:`validate_graph` 规则 4 禁止「互无依赖的两个节点写同一 ledger 段」。即并行只允许写**不相交**的段。够覆盖近期所有真实蓝图。
- **P2(真并行)**:并行分支各产**自己的 ledger delta**,在汇合节点按确定性顺序 merge(`merge()` 已是幂等去重,只需固定 apply 次序)。这才支持「两条分支都改 tech_stack」这类场景。

> 这是「复用现有 ledger」与「通用图」之间的真实张力,显式记录于此,不在实现里假装不存在。

---

## 10. 与现有底座的复用对照(无任何重造)

| 设计件 | 复用的现有东西 | 新增薄层 |
|---|---|---|
| Port 上下文 | `ContextLedger`(段/merge/render 全沿用) | 节点声明 `context.reads/writes` + 并行 scope |
| 节点契约存储 | `prompt_store` 的 default-seed+override+TTL 模式 | Mongo `node_contracts` / `node_contract_versions` + `iter_default_node_contracts()` |
| 提示词钉定 | `prompts` + `get(key)` HEAD 路径不变 | `prompt_versions` 集合 + `head_pin/get_pinned`(内容寻址) |
| 蓝图实例 | `CodeProject` 的 UUID/owner/visibility/JSON-in-Text 约定 | SQL `workflow_graphs` + `workflow_graph_node_usage` |
| 解释器 | `runtime.register_workflow/get_workflow` + ThreadPoolExecutor + recorder/SSE | `graph:*` 解释器 + executor 注册表 |
| 运行/回放 | `AgentRun/Step` + `prompt_snapshot` + recorder/bus | `AgentStep.port_bindings_raw` 一列 |
| 计费 | `pricing.py` + `charge/refund_credits` 预扣模式 | 图计费 = 节点 `pricing_key` 之和 |
| 校验 | `context_verifier` 的确定性检查风格 | `validate_graph()` 纯类型匹配 |
| 执行器 | 现有各 `_do_*` / fe/be/mw/deploy workflow | 包成 executor 注册项 |

---

## 11. 落地顺序

> **进展(2026-06-26):** 契约层(端口注册表 / NodeContract / 默认契约 / `validate_graph`)**已实现 + 单测通过**。下面 P0 第 1/4 步对应这部分;其余待做。注意:因复用 `CodeCanvas`,**不再需要为图本身引入 Alembic**——只有「阶段执行器把产物落 `AgentStep` 加列」等 SQL 改动才需要迁移(届时再接 Alembic)。

### P0 —— 地基 + 数据化(不含编辑器)
0. **【前置·不可谈判】引入 Alembic**,补 `env.py` / `alembic.ini` / 首个 baseline migration(把现有 `create_all` 的表结构纳入版本管理)。见 §1.2。
1. Port 类型注册表(§3)+ NodeContract 纯 Python 类型 + Mongo 存储(§4)。
2. 提示词版本表:`prompts` 加 `version/hash` + `prompt_versions` + `head_pin/get_pinned`(§5)。**这一步独立可用**——即便不做图,也让现有 prompt 可版本钉定、可复现。
3. 把现有 7 阶段 + fe/be/mw/deploy 写成 `iter_default_node_contracts()` 默认契约。
4. `validate_graph()`(§8)。

→ 里程碑:**用数据描述现有流水线 + 提示词钉版可复现**,行为与今天等价。

### P1 —— 通用解释器
5. `WorkflowGraph` + `WorkflowGraphNodeUsage` SQL(§6)+ migration。
6. executor 注册表 + 通用解释器 `run_graph_workflow`,接 `get_workflow("graph:*")`(§6.4)。
7. `AgentStep.port_bindings_raw`(§7)+ migration。
8. publish 冻结流程 + 图计费预扣。

→ 里程碑:**能保存并运行一张自定义蓝图**(后端闭环,先用 API/JSON 提交图)。

### P2 —— 前端画布 + 真并行
9. 前端画布编辑器:拖节点/连线,前端拉 `node_contracts` 渲染端口,实时跑 `validate_graph`。
10. ledger 并行 delta-merge(§9 第二步)。

---

## 12. 已知限制与设计取舍(诚实记录)

1. **执行底座不横向扩**:`runtime` 是进程内 `ThreadPoolExecutor` + 单 gunicorn worker(`reconcile_orphaned_runs` 的孤儿续跑逻辑明文假设「单 worker、无 peer 进程」)。本设计**不解决**这一点。真要支撑大量并发蓝图运行,需把执行层换成分布式队列(Celery/RQ/Temporal),并把孤儿续跑改成基于 worker 租约/心跳——**列为「真要多租户规模」时的独立 P1,不属本功能**。
2. **容器生成隔离**:fe/be/slicer 走 Docker-out-of-Docker(共享宿主 `docker.sock` + 宿主 TMPDIR bind mount)。大规模下是资源/安全/隔离的硬墙,与本 schema 正交,独立治理。
3. **ledger 并行写**:P0/P1 仅靠校验禁止并行写同段(§9),真并行 delta-merge 推迟到 P2。
4. **节点契约 → 代码常量软耦合**:`pricing_key`/`executor` 是指向代码的字符串,靠 publish 校验保证存在,接受这种软耦合(换来契约可在 Mongo 在线编辑)。
5. **不做的(YAGNI)**:用户自定义端口类型、第三方节点插件 SDK、跨域市场、可视化 DSL 导出——在出现第二个消费域之前一律不做(§1.1)。

---

## 13. 向后兼容 / 迁移

- 现有 7 个内建 workflow key **保持不变**,继续以 Python 函数注册;蓝图是**新增**的 `graph:*` 路径,二者并存。历史 `AgentRun` 不受影响,照常回放。
- `prompt_store.get(key)` 行为**完全不变**;版本表是叠加层,旧无版本文档视为 legacy,首次编辑生成首个不可变版本。
- 所有持久化 blob(`graph_raw`/`port_bindings`/契约)带 `schema_version`,加载走容忍式 loader(未知版本安全降级),对齐 `ContextLedger.load()`。
- 所有 DDL 变更(新表、`AgentStep` 加列、`prompts` 加字段)**必须经 Alembic migration**,不依赖 `create_all`(§1.2)。
