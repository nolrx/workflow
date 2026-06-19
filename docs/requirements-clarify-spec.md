# 需求澄清问卷规范（Requirements Clarification Questionnaire）

> 适用范围：Code 域 `code_full_generation` 工作流的**需求文档提问阶段**。
> 目标：在需求文档生成后、进入下一步之前，自动生成一份结构化「澄清问卷」，
> 前端据此渲染**选择弹框**，帮助用户用几次点击快速确认关键决策并**重新迭代需求文档**。
> 未确认的问题自动采用模型给出的**默认建议**。

## 1. 端到端流程

```
需求 Agent 生成需求文档
        │
        ├─ 生成澄清问卷（generate_clarifications）
        │     └─ 作为 JSON 产物「需求澄清问卷」(requirements_questions.json) 挂到 requirements 步
        ▼
run 暂停在 requirements 审阅门（STEP_AWAITING_REVIEW）
        │
        ▼
前端从 run 快照的 artifacts 读取最新问卷（selectRequirementsQuestions）
        │
        ▼
RequirementsClarifyDialog 自动弹出（每个审阅轮次一次）
        │  用户逐题：单选 / 多选 + 可选自定义输入；未改动 = 采用建议
        ▼
「确认并迭代需求」→ 编译为中文 revise 指令 → resumeRun("revise", instruction)
        │
        ▼
需求修订 Agent（revise_requirements）按指令增量修订文档
        │  并**重新生成**新一轮澄清问卷（仅保留仍待澄清的问题）
        ▼
再次暂停在 requirements 门 …（循环，直到用户「确认，进入下一步」approve）
```

要点：
- **复用既有的审阅门 / revise 机制**——问卷的答复被编译成一条普通的调整意见走 `resume(action="revise")`，
  后端 `revise_requirements` 与上下文账本逻辑无需改动。
- 问卷在 **fresh 与 revise 两条路径都会重新生成**，因此每一轮都反映「当前还剩哪些待澄清的问题」。
  当模型判断需求已足够清晰时返回**空数组**，前端便不再弹框，用户直接 approve 即可。
- 问卷**只面向用户确认**，不进入需求文档正文；编译出的指令会出现在对话记录里（透明可见）。

## 2. 数据结构（规范本体）

后端生成、规范化后作为 **JSON AgentArtifact** 输出（`filename = requirements_questions.json`，
`content_json = { "questions": ClarificationQuestion[] }`），挂在该轮 `requirements` 步上——
**不新增 CodeProject 列，因而无需任何数据库迁移**（仓库以 `db.create_all()` 建表，不会为既有表加列）。
前端从 run 快照的 `artifacts` 里取**最新一份**该产物（`selectRequirementsQuestions`，见
`frontend/src/components/code/clarify.ts`）。前端类型见 `frontend/src/api/code.ts` 的
`ClarificationQuestion` / `ClarificationOption`。

```jsonc
// content_json.questions: ClarificationQuestion[]
{
  "id": "platform",            // 稳定标识，[A-Za-z0-9_-]，缺失时回退 q{n}
  "category": "平台与范围",     // 可选：分组/维度标签
  "question": "产品的目标运行平台是？",
  "type": "single",            // "single"(单选) | "multi"(多选)
  "options": [                 // 2~5 个；每项 {value,label,description?}
    { "value": "web",   "label": "Web 端（浏览器）", "description": "免安装，浏览器访问" },
    { "value": "mobile","label": "移动端 App" },
    { "value": "both",  "label": "Web + 移动端" }
  ],
  "default": ["web"],          // 默认建议（option.value 列表）。
                               //   single 题恒为 1 个；multi 题为 0..n 个。
  "allow_custom": true,        // 是否展示「其他（自定义）」输入框
  "rationale": "影响技术选型与界面适配方式"   // 可选：为什么问 / 影响哪些章节
}
```

### 字段约束与规范化（后端 `_normalize_clarification`）

| 字段 | 规则 |
| --- | --- |
| `id` | 非 `[A-Za-z0-9_-]` 字符替换为 `_`，截断 60；为空回退 `q{index+1}` |
| `question` | 必填、非空，截断 300；为空则**丢弃整题** |
| `type` | 归一为 `single` / `multi`（`multiple`/`checkbox` 等→`multi`，其余→`single`） |
| `options` | 每项归一为 `{value,label,description?}`；`value` 缺失回退 `label` 或 `opt_{n}`；**少于 2 个选项则丢弃整题**；最多保留 5 个 |
| `default` | 按 `value`（其次按 `label`）匹配到合法 option；`single` 题保证**恰好 1 个**（空则取首个选项）；`multi` 题去重、可为空 |
| `allow_custom` | 布尔，缺省 `true` |
| 问题总数 | 最多保留 6 题 |

规范化是**容错**的：解析失败 / 空数组 → 返回 `[]`（视为「无需澄清」），整个生成过程绝不抛错、绝不阻断需求文档。

## 3. 默认建议（未确认即采用）的语义

- 弹框打开时，每道题**预先选中其 `default`**（最快路径就是直接点「确认并迭代需求」）。
- 用户改动过的题标记为 `touched`，编译指令时注明「（用户确认）」；未改动的注明「（采用建议）」。
- 「全部采用建议」按钮：忽略当前改动，强制按 `default` 编译并提交。
- 自定义输入：作为附加答复并入该题（与所选项一起）；填写即视为 `touched`。

## 4. 编译出的修订指令（前端 → 后端）

答复编译成一条**中文**调整意见（与始终为中文的需求文档保持口径一致，类似后端修订 prompt 都是中文硬编码）：

```
我已通过「需求澄清问卷」确认了以下关键决策，请据此修订需求文档：

1. [平台与范围] 产品的目标运行平台是？
   答复：Web 端（浏览器）（用户确认）
2. [功能范围] 首个可用版本（MVP）优先包含哪些能力？
   答复：核心业务主流程、数据看板与统计；偏向技术型独立开发者（用户确认）
3. [权限与账户] 需要怎样的账户与登录体系？
   答复：邮箱密码登录（采用建议）

修订要求：
- 将以上每条确认逐一落实到需求文档的相应章节（如功能范围、用户流程、权限与账户、数据对象、非功能要求等）。
- 把已确认的问题从「边界与待确认问题」中移除，或标注为「已确认」。
- 仅做与这些决策相关的增量修改，保持文档其余部分稳定、自洽。
```

该指令通过现有的 `POST /api/agent/runs/<id>/resume`（`action="revise"`）提交。

## 5. 涉及的代码位置

| 层 | 文件 | 作用 |
| --- | --- | --- |
| Prompt | `backend/prompts/code/requirements_clarify_prompt.txt` | 出题提示词（仅输出 JSON 数组，花括号已转义） |
| Service | `backend/services/code/generation_service.py` | `generate_clarifications` + `_normalize_clarification(s)` + `_fallback_clarifications` |
| Workflow | `backend/services/agent/workflows/code_workflow.py` | `_do_requirements` 末尾生成问卷并作为 JSON 产物输出（best-effort，无 DB 写入） |
| 前端类型 | `frontend/src/api/code.ts` | `ClarificationQuestion` / `ClarificationOption` |
| 前端取数 | `frontend/src/components/code/clarify.ts` | `selectRequirementsQuestions(run)`：从 run 产物取最新问卷 |
| 前端弹框 | `frontend/src/components/code/RequirementsClarifyDialog.tsx` | 渲染问卷、收集答复、编译指令 |
| 前端接线 | `frontend/src/components/code/ConversationRail.tsx` | requirements 审阅门自动弹框 + 按钮 + 提交走 `onRevise` |
| i18n | `frontend/src/locales/{zh-CN,en,ja,ko}/code.json` → `clarify.*` | 弹框 UI 文案（模型可见的指令文本不走 i18n，固定中文） |

## 6. 计费

澄清问卷生成是需求步内部的一次文本模型调用，属于 `code_full_generation` 工作流的一部分，
由创建 run 时**预扣**的 `CODE_FULL_GENERATION_TOTAL` 覆盖，**不单独计费**
（与 requirements / flow / style 等核心生成调用一致；仅 `documents` 边界的 AI 一致性闸才逐次加扣）。
当前版本 Code 域计费默认关闭（价格为 0）。
