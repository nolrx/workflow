# Agent 会话上下文账本（Session Context Ledger）规范

> 范围:**仅 Code 域 + Agent Swarm**(`code_full_generation`、`code_frontend_generation`)。PPT / RedBook 不涉及。
> 性质:**内部 / 调试可见,从不进入用户产出**。它是保证多步工作流口径一致、避免上下文漂移层层放大的底座。

## 1. 目的

Code 工作流的每一步都各自从数据库读取前序产出、再把整篇文档塞进 prompt,步骤之间没有共享的"共识"与一致性闸门,导致后续步骤的术语 / 技术栈 / 范围口径漂移,错误层层累积。**上下文账本**在一次 `AgentRun` 内累积并固化"已确立的共识",注入每一步 prompt,并在关键边界做校验,使后续 Agent 始终对齐同一口径。

## 2. 账本数据结构(`schema_version: 1`)

权威定义见 `backend/services/agent/context_ledger.py` 的 `ContextLedger`。

```jsonc
{
  "schema_version": 1,
  "project": {
    "title": "",            // 项目标题
    "one_liner": "",        // 一句话产品定位(口径锚点)
    "target_users": [],     // 目标用户
    "scope_in": [],         // 范围内
    "scope_out": []         // 明确不做(非目标)
  },
  "glossary":   [ { "term": "", "definition": "", "source_step": "" } ],  // 术语口径
  "tech_stack": { "frontend": "", "backend": "", "data": "", "constraints": [] },
  "decisions":  [ { "id": "", "statement": "", "rationale": "", "source_step": "" } ],
  "constraints": [ "..." ],       // 全局硬约束
  "open_questions": [ "..." ],    // 待确认问题,逐步携带
  "provenance": [ { "step": "", "agent_key": "", "fields_touched": [], "at": "ISO8601" } ]
}
```

- **去重规则**:`glossary` 按小写 `term`(最新定义覆盖),`decisions` 按 `id`,`constraints` / `scope` / `open_questions` 按归一化字符串。空值 merge 被忽略,绝不擦除已有口径。
- **`schema_version` 策略**:`load()` 容错——缺键补默认;遇到未知 `schema_version` 记日志并按**空账本**处理,保证未来升级不会让进行中或回放的 run 崩溃。

## 3. 生命周期

| 阶段 | 工作流 / 步骤 | 账本动作 |
|---|---|---|
| 播种 | `code_full_generation` · planner | `seed_from_inputs()`:项目定位 + 默认技术栈(前端 React18+TS+plain CSS、后端 Flask)+ 风格约束 |
| 充实 | requirements / flow / documents / style | 每步后从产物**容错抽取**关键事实 merge 回账本 |
| 校验闸门 #1 | documents(文档拆分) | 确定性校验 + **AI 一致性闸门**(高风险边界) |
| 终态 | publisher | 写最终快照 + 落盘 `上下文账本（最终）` JSON artifact |
| 校验闸门 #2 | `code_frontend_generation` · fe_build | 前端 run 在 fe_planner **重载**上一轮 full-generation run 的账本;build 后做确定性校验 + **AI 一致性闸门** |

> **闸门为何落在 documents 与 frontend_build**:这两处是口径最易漂移、且漂移会向下游放大的边界(文档拆分决定后续所有文档的口径;前端代码生成是所有文档的汇流处)。当前 `code_full_generation` 为 7 步(无独立"润化"步),故原计划的"拆分→润化"边界对应到**文档拆分步**本身。

- 账本的**当前态**持久化在 `AgentRun.context_ledger_raw`(`get/set_context_ledger`)。
- 每一步的**注入快照 + 校验结果**持久化在 `AgentStep.context_snapshot_raw` / `context_check_raw`(`get/set_context_snapshot` / `get/set_context_check`),以支持逐步回放。

## 4. 注入契约

- **`.format` 系模板**(`backend/prompts/code/{requirements,development_flow,document_split,style}_prompt.txt`):在 `{system_prefix}` 与 `---` 之间插入新占位符 **`{context_ledger}`**。`generation_service.py` 的对应 `_xxx_context(..., context_ledger="")` 把渲染文本作为实参传入。
  - **空账本渲染为 `""`** → 模板仅多一个空行,完全向后兼容(旧调用方不传该参数也正常)。
  - **`.format` 纪律**:模板加了 `{context_ledger}` 就必须在该模板所有 `.format()` 调用处传 `context_ledger=`,否则 `KeyError`。渲染文本本身不含单个 `{`/`}`(作为实参不被二次扫描),模板内既有 JSON 示例的 `{{ }}` 转义保持不变。
- **`.replace` 系模板**(`frontend_build_prompt.txt`,因含 JSX 花括号):使用 **`[[CONTEXT_LEDGER]]`** token,由 `frontend_build_service.build_app(..., context_ledger="")` 经 `_fill` 替换。

渲染格式(`render_for_prompt`)为紧凑中文 markdown:`## 项目共识 / ### 术语口径 / ### 技术栈口径 / ### 关键决策 / ### 全局约束 / ### 待确认问题`,超长时自底向上裁剪、保留标题。

## 5. 校验规则(程序化为主 + 关键节点 AI 闸门)

两层都在 `backend/services/agent/context_verifier.py`,**任何一层都不会向工作流抛异常**。

### 5.1 确定性校验 `run_deterministic_checks`
按 `expectations` 选用:`nonempty_output`(产物长度)、`doc_types_covered`(文档步覆盖 6 个基线类型)、`ledger_fields_complete`(必需账本字段)、`stack_conformance`(前端步:技术栈关键词 + 入口文件)。失败为 `warning` 级,主流程继续。

### 5.2 AI 一致性闸门 `run_ai_consistency_gate`(**fail-open**)
- 输入:`ledger.fingerprint()`(技术栈 + 术语 + 范围 + 决策,**仅指纹,不含整篇文档**)+ 截断的新产物摘要(≤2000 字符)。
- 输出契约(严格 JSON):`{"conflict": bool, "conflicts": [{"field","established","new","severity"}], "summary": str}`。
- **永不阻断**:provider 未配置 → 返回 `None`(闸门跳过);调用 / 解析失败 → 返回 `{"conflict": false, ..., "degraded": true}`。
- 闸门的模型调用**不**经 `step.model_tracer()`,以免覆盖该步主产物的 prompt/response 调试轨迹;其判定写入 `context_check.ai_gate`。

### 5.3 事件 `emit_context_events`
写入 `step.set_context(snapshot, check)` 后:有冲突(AI 冲突或确定性告警)→ 发 `CONTEXT_CONFLICT`(`warning` 级);否则 → 发 `CONTEXT_UPDATED`(`info` 级)。

## 6. 积分策略

- 单价:`pricing.CODE_CONTEXT_VERIFY`(env `PRICE_CODE_CONTEXT_VERIFY`,默认 1),`OPERATION["code_context_verify"] = ("agent_run", …)`。
- **按每次 AI 闸门调用实时 `charge()`**,不折进预扣预留。理由:闸门可被跳过(provider 未配 / 余额不足 / 前端 fallback),折进预扣会对从不触发闸门的 run 多扣,并使现有 `_run_produced_nothing` 自动退款逻辑复杂化。
- **优雅降级**:仅在 `gate_available()` 为真时才 `charge`;`charge` 返回 `False`(余额不足)→ 跳过 AI 闸门、仅做程序化校验、发 `warning` 事件;均不阻断主流程。
- **不退款**:闸门扣费是独立审计事务,校验工作已执行,失败时不退;只退未产出任何 artifact 时的预扣预留。
- 工作流把本次 run 的闸门花费累计为 `extra_credits` 经返回值带出;`runtime._execute` 以 `run.credit_used = run.credit_reserved + extra_credits` 让显示与审计一致。

## 7. 可见性

仅在 `AgentRunPanel` 的 **调试模式(debugMode)** 下出现"上下文"页签,展示:注入快照(`context_snapshot.injected_text`)、账本状态(`context_snapshot.ledger`)、校验结果(`context_check`,冲突时显示红色徽标)。普通用户界面不变、不输出给用户。

## 8. 向后兼容 / 迁移

- 新增列经 `db.create_all()` 在新库重启即生效;**已存在的 SQLite 库需删库重建**(无 Alembic)。保数据时可在 `app.py` 的 `create_all()` 后加幂等 `ALTER TABLE ... ADD COLUMN`(吞 "duplicate column")。
- 旧 run:`context_ledger`/`context_snapshot`/`context_check` 取空,渲染为空、页签不显示、回放正常。
