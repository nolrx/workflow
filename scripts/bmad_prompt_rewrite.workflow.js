export const meta = {
  name: 'bmad-code-prompt-rewrite',
  description: 'Rewrite Code-domain prompts in BMAD style with per-stage constraints/boundaries, then adversarially verify + self-repair each',
  whenToUse: 'Optimizing the Code-domain prompt pipeline of AI Creative Studio with BMAD prompt-engineering patterns',
  phases: [
    { title: 'Draft', detail: 'Rewrite each prompt in BMAD skeleton, write to disk' },
    { title: 'Verify', detail: 'Adversarial check: placeholders, braces, JSON schema, ledger headers, operational contracts, boundary' },
    { title: 'Repair', detail: 'Fix any blocking violations in place' },
  ],
}

const DIR = '/data/workflow/backend/prompts/code'

// ── The shared BMAD skeleton + house rules every prompt must follow ──────────
const SHARED_SPEC = `
你正在把一个 AI 代码生成流水线（"Code 域"）的提示词，按 BMAD-METHOD 的提示词工程范式做"约束化、边界化"重写。
目标：让每个阶段(stage)的提示词都成为一个**角色契约 + 输入契约 + 职责边界 + 产出契约 + 交付前自检**的自包含单元，
从而把"质量一般、阶段间口径漂移、互相越界"的问题压下去。

【本流水线背景（理解全链路，才能写好单个阶段的"分界"）】
code_full_generation 主链：
  planner → requirements(需求文档) → flow(开发流程) → documents(文档拆分) → style(风格) → preview(缩略图) → publisher
之后 code_frontend_project_generation：fe_planner → fe_build(前端工程) → fe_publish。
每个文本阶段都注入一个"项目共识账本"({context_ledger} / [[CONTEXT_LEDGER]])，它是跨阶段不漂移的锚。
这次我们已在代码侧让 Code 配方**不再附加通用 OUTPUT_CONTRACT**，所以"产出契约"由每个提示词自己**唯一**定义。

【统一骨架——每个提示词都按这五段组织（英文提示词用对应英文小节名）】
对 .format() 类（中文、占位符 {xxx}）：保留文件最前面的
    {system_prefix}

    {context_ledger}

    ---
然后用这五段（Markdown 一级标题）：
  # 角色与原则        —— 资深角色 + 身份 + 风格 + 3~5 条编号"核心原则/护栏"（针对本阶段，不是套话）
  # 输入（视为既定事实）—— 列出本阶段会收到的上游产物；规则：把它们当作已确认事实，**派生而非重造**，不得与「项目共识」冲突
  # 本阶段职责与边界    —— "只负责：…" + "明确不做（交给下游阶段 X）：…"。这是本次最重要的"分界"，必须具体、可执行
  # 产出契约（唯一权威输出结构）—— 精确的输出结构 / 章节 / 稳定 ID / 格式；显式声明"本契约优先于上方任何通用结构化建议，冲突以此为准"
  # 交付前自检（全部满足才能输出）—— 一份简短 checklist（5~8 条），模型在产出前据此自查；含"无占位/无 TODO、覆盖完整、未越界、与共识一致、ID 可追溯"等

对 [[KEY]] 填充类（占位符形如 [[REQUIREMENT]]）：骨架同上，但小节标题沿用该文件现有语言（英文文件用 # ROLE & PRINCIPLES / # INPUTS (treat as established truth) / # SCOPE & BOUNDARIES / # OUTPUT CONTRACT (authoritative) / # PRE-DELIVERY SELF-CHECK），并保留该文件现有的角色开场（如 frontend_project 以"你是一名资深前端工程师…"开头）。

【稳定 ID（贯穿全链路的抗漂移核心）】
- requirements 阶段：功能需求用 FR1, FR2…；非功能需求用 NFR1, NFR2…（写在"功能范围""非功能要求"小节里，每条一个编号）。
- flow 阶段：模块用 M1, M2…；里程碑用 MS1, MS2…；并让每个模块/里程碑**标注覆盖了哪些 FR/NFR**（如"覆盖 FR1, FR3"）。
- documents 阶段：每份文档在正文里**标注它对应/追溯的 FR/NFR/模块 ID**，做到可追溯、不新增需求。
- 前端构建阶段：要求**逐 FR 实现并可在界面中演示/验证**。
ID 只是文本约定，不改变任何输出格式契约（JSON 数组仍是 JSON 数组）。

【House Rules（违反即判失败，验证阶段会逐项查）】
R1. **占位符逐字保留**：记录里的 placeholders 列出的每个占位符都必须在文件中原样出现（数量不少于原文件）。**不得新增、改名、删除任何占位符。** {system_prefix} 与 {context_ledger} 必须仍是 .format 文件最前面的两个块。
R2. **花括号规则**：
    - fill='format' 的文件，会被 Python str.format(**dummy) 调用。**除占位符 {xxx} 外，正文/JSON 示例里的每一个 { 和 } 都必须写成 {{ 和 }}**（双花括号转义），否则 .format 会崩。
    - fill='fill' 的文件用简单字符串替换，**花括号是字面量**：JSON 示例里的 { } 保持**单**花括号，**不要**转义；占位符是 [[KEY]]。
    - fill='plain' 的文件没有占位符。
R3. **输出形态契约不变**：
    - output='json-array' / 'json-object'：必须仍明确要求"只输出 JSON、不要 Markdown 代码围栏、不要数组/对象以外的任何文字"，且**字段名、枚举值、整体结构与原文件完全一致**（见记录的 schema 字段）。
    - output='fragment'：必须仍要求"只输出用于替换选中片段的新文本本身，不要整篇、不要解释、不要代码围栏"。
    - output='markdown'：中文 Markdown 完整文档。
    - output='agent-files'：这是给 Claude Code/Codex 容器内 Agent 的指令（它会创建文件并自检构建），**不是**要它输出 JSON。
    - output='plain'：纯文本指令。
R4. **账本兼容章节名逐字保留**：记录的 ledgerHeaders 列出的章节标题必须在"产出契约"里要求模型输出（用 ## 标题），因为后端用正则按这些关键词抽取账本字段，改名会破坏抽取。
R5. **运营契约逐字保留**：记录的 operationalContracts 列出的每个技术指令（命令名、路径、构建命令、约束）在语义与关键字面量上都必须保留——容器/收集器依赖它们。可以重组措辞、加强表达，但**绝不能删除或改变其含义/关键 token**。
R6. **语言**：保持记录里 lang 指定的语言（zh=中文，en=英文）。
R7. **只动这一个文件**，写回它原来的绝对路径。不要碰别的文件。
R8. 内容要"可执行、可检查"，不写空泛口号；针对"本项目/本阶段"具体化。删除原文件里已有的、好的硬约束属于退步——只做"保留+结构化+加强"，不做"删减"。

【与旧版相比要明显改进的点】
- 把模糊的"必须包含 X、Y、Z"升级为带稳定 ID、带边界、带自检的产出契约。
- 把阶段间重叠（如 requirements 谈"技术架构建议"、flow 谈"技术假设"）明确为：requirements **决定**架构方向与选型（并锁入账本），flow **沿用且细化**、不得改选型。类似地把每个阶段"不做什么、交给谁"写清楚。
- 增加"派生自上游、不臆造、不与共识冲突"的硬规则。
`

// ── Per-prompt contract records ─────────────────────────────────────────────
const PROMPTS = [
  {
    file: 'requirements_prompt.txt', lang: 'zh', fill: 'format', output: 'markdown',
    placeholders: ['system_prefix', 'context_ledger', 'requirement'],
    role: '资深产品经理 + 软件架构顾问',
    ledgerHeaders: ['产品定位', '目标用户', '功能范围', '技术架构建议', '边界与待确认问题'],
    requiredSections: '产品定位、目标用户、核心场景、功能范围（带 FR 编号）、用户流程、权限与账户、数据对象、非功能要求（带 NFR 编号）、技术架构建议、边界与待确认问题',
    scope: '定义"做什么"与"用什么应用形态/技术方向"。功能范围逐条给 FR 编号，非功能逐条给 NFR 编号。技术架构建议必须按真实需求量身设计（先判应用形态，再分层选型并逐条说明取舍理由；能轻则不过度设计；不需要后端就明确写"无独立后端"）——这是要锁入账本的架构决策。',
    boundary: '不做：模块/接口/数据表/里程碑的工程级细化（交给 flow）；不做：把项目拆成可编辑文档（交给 documents）；不做：视觉风格（交给 style）；不做：写代码。',
    upstream: '用户原始需求 {requirement}',
    downstream: 'flow 将沿用此处技术架构方向并细化；documents 据此拆分；style 据产品定位做视觉',
    keepGuidance: '务必保留原文件里关于"技术架构建议"那一整段量身设计的详细指引（应用形态枚举、分层选型逐条理由、不要默认单HTML+Flask、能轻则轻），把它结构化进产出契约，不要删。',
    schema: '',
  },
  {
    file: 'development_flow_prompt.txt', lang: 'zh', fill: 'format', output: 'markdown',
    placeholders: ['system_prefix', 'context_ledger', 'requirements_doc'],
    role: '技术负责人 / 资深软件架构师',
    ledgerHeaders: ['技术假设'],
    requiredSections: '技术假设、模块拆分（带 M 编号、标注覆盖的 FR/NFR）、数据设计、接口设计、前端页面/状态、后端服务（若不需要后端则说明原因与替代方案）、AI/提示词链路、开发里程碑（带 MS 编号、标注覆盖的 FR）、验收标准、风险清单',
    scope: '把已确认需求细化为可执行的开发流程；模块用 M 编号、里程碑用 MS 编号并标注各自覆盖哪些 FR/NFR；每个里程碑要能指导后续文档拆分与开发任务。',
    boundary: '不做：重新选择技术栈（必须沿用需求文档已确立的技术架构方向，不得另选、不得退回单HTML+Flask）；不做：输出代码；不做：视觉风格；不做：把项目拆成可编辑文档（交给 documents）。',
    upstream: '需求文档 {requirements_doc}（含 FR/NFR 与技术架构方向）+ 项目共识账本',
    downstream: 'documents 据此与需求拆分文档；前端构建据此实现',
    keepGuidance: '保留"技术假设必须沿用需求文档的技术架构方向、不得另选栈、不要退回统一单HTML+Flask 模板"这一硬约束，并加强为可追溯。',
    schema: '',
  },
  {
    file: 'style_prompt.txt', lang: 'zh', fill: 'format', output: 'markdown',
    placeholders: ['system_prefix', 'context_ledger', 'requirement', 'styles'],
    role: '资深 UI / 产品设计师 + UI 提示词专家',
    ledgerHeaders: ['基调'],
    requiredSections: '视觉定位、布局规则、组件风格、色彩与字体（给具体色值）、交互反馈、禁用事项、缩略图生成提示词、后续代码开发 UI 基调提示词',
    scope: '据产品定位与所选风格 {styles} 产出可直接用于"生成缩略图"和"指导前端 UI 开发"的视觉规格。"缩略图生成提示词"必须是一段可直接复用的具体界面描述（页面类型+主要区域布局、主色与强调色给色值、组件密度与圆角/阴影、一处真实示例文案）；"后续代码开发 UI 基调提示词"要给出布局栅格、间距与圆角规范、主色与语义色、组件库基调。多选风格要说明混合比例与冲突处理。',
    boundary: '不做：改动功能范围/需求/技术栈/特性行为（只负责"长什么样"，不碰"做什么/怎么实现"）；不做：写代码。',
    upstream: '用户需求 {requirement} + 所选风格 {styles} + 项目共识账本',
    downstream: 'preview 用"缩略图生成提示词"生成缩略图；前端构建用"UI 基调提示词"',
    keepGuidance: '保留"缩略图生成提示词必须具体到色值/布局/示例文案，不堆抽象风格词"这一要求。',
    schema: '',
  },
  {
    file: 'document_split_prompt.txt', lang: 'zh', fill: 'format', output: 'json-array',
    placeholders: ['system_prefix', 'context_ledger', 'requirements_doc', 'development_flow'],
    role: '软件研发流程设计师',
    ledgerHeaders: [],
    requiredSections: '',
    scope: '把需求文档+开发流程**重组**为 ≥6 份用户可独立编辑的开发文档；每份文档正文里标注它追溯的 FR/NFR/模块(M)/里程碑(MS) ID；每份的 prompt_expert 必须针对该文档定制，按"角色/输入/输出格式/约束/质量标准/反例"分点写。',
    boundary: '不做：新增上游没有的需求或范围；不做：更改技术选型；不做：生成代码或视觉。只做"按上游既定事实重组+补充提示词专家建议"。',
    upstream: '需求文档 {requirements_doc} + 开发流程 {development_flow} + 项目共识账本',
    downstream: '前端构建会读取这些文档摘要；用户会逐份编辑',
    keepGuidance: '完整保留原 JSON 契约。',
    schema: `只输出一个 JSON 数组（无 Markdown 围栏、无数组外文字）。每个对象**必须且只能**含字段：
- document_type: 英文 snake_case，取值集（可按需补充其它 snake_case）：product_spec、frontend_spec、backend_spec、data_model、prompt_spec、acceptance_plan
- title: 中文标题
- content: 中文 Markdown 正文（用二级标题分节，具体贴合本项目，标注追溯的 FR/NFR/M/MS ID）
- prompt_expert: 中文"提示词专家建议"，按"角色 / 输入 / 输出格式 / 约束 / 质量标准 / 反例"分点
切分基线：至少覆盖 product_spec / frontend_spec / backend_spec / data_model / prompt_spec / acceptance_plan，至少 6 份。
⚠️ 因为是 .format 文件，JSON 示例里的所有 { } 必须写成 {{ }}。务必保留一个"仅示意结构"的 JSON 示例。`,
  },
  {
    file: 'requirements_clarify_prompt.txt', lang: 'zh', fill: 'format', output: 'json-array',
    placeholders: ['system_prefix', 'context_ledger', 'requirement', 'requirements_doc'],
    role: '资深产品经理（需求澄清官）',
    ledgerHeaders: [],
    requiredSections: '',
    scope: '从需求文档中抽取 3~6 个**真正会改变产品方向/功能范围/技术选型/目标用户/验收口径**的高杠杆决策，做成结构化"需求澄清问卷"。重点参考"边界与待确认问题"以及任何被假设补全/模糊之处。每题给 2~5 个互斥(single)或可叠加(multi)的具体选项，并在 default 给最合理默认建议、在 rationale 用一句话说明为何关键。',
    boundary: '不做：问细枝末节或不影响下游产物的问题；不做：编造需求里完全没有的方向。若需求已足够清晰、没有值得追问的关键决策，**直接返回空数组 []**。',
    upstream: '用户原始需求 {requirement} + 当前需求文档 {requirements_doc}',
    downstream: '前端渲染成选择弹框；未确认题采用 default；用户确认后并入账本驱动后续阶段',
    keepGuidance: '完整保留原 JSON 契约与字段。',
    schema: `只输出一个 JSON 数组（3~6 个对象，或空数组 []；无解释、无围栏）。每个对象字段：
- id: 英文短 id；category: 中文分类；question: 中文问题；type: "single" 或 "multi"
- options: 数组，每项 {{"value","label","description"(可选)}}
- default: 数组（single 恰好 1 个 value；multi 0~多个 value；值必须来自本题 options）
- allow_custom: 一律 true；rationale: 中文一句话说明为何关键
⚠️ .format 文件：JSON 示例里所有 { } 必须写成 {{ }}。保留一个含 single 与 multi 各一例的示例。`,
  },
  {
    file: 'requirements_revision_prompt.txt', lang: 'zh', fill: 'format', output: 'markdown',
    role: '资深产品经理', revision: true,
    placeholders: ['system_prefix', 'context_ledger', 'instruction', 'current_doc'],
    ledgerHeaders: ['产品定位', '目标用户', '功能范围', '技术架构建议', '边界与待确认问题'],
    requiredSections: '产品定位、目标用户、核心场景、功能范围（FR 编号）、用户流程、权限与账户、数据对象、非功能要求（NFR 编号）、技术架构建议、边界与待确认问题',
    scope: '据用户调整意见 {instruction}（最高优先级）对当前需求文档 {current_doc} 做**精准增量修订**，输出修订后的**完整**文档；只改相关部分，其余原样；保持 FR/NFR 编号体系延续。',
    boundary: '不做：解释改了什么；不做：无谓改写未受影响内容；不做：越界进入 flow/documents/style 的职责。技术架构建议仍要贴合真实需求、不回退单HTML+Flask；若意见改变应用形态或选型，同步更新该节及受影响章节，保持整篇自洽。',
    upstream: '当前需求文档 {current_doc} + 用户意见 {instruction} + 账本',
    downstream: '修订后重新驱动 flow/documents',
    keepGuidance: '保留章节集合与"不要回退单HTML+Flask"约束。', schema: '',
  },
  {
    file: 'development_flow_revision_prompt.txt', lang: 'zh', fill: 'format', output: 'markdown',
    role: '资深软件架构师', revision: true,
    placeholders: ['system_prefix', 'context_ledger', 'instruction', 'current_doc'],
    ledgerHeaders: ['技术假设'],
    requiredSections: '技术假设、模块拆分（M 编号）、数据设计、接口设计、前端页面/状态、后端服务、AI/提示词链路、开发里程碑（MS 编号）、验收标准、风险清单',
    scope: '据用户意见 {instruction} 对当前开发流程 {current_doc} 做精准增量修订，输出完整文档；保持 M/MS 编号与 FR 追溯延续。',
    boundary: '不做：重选技术栈（沿用既定方向）；不做：输出代码；不做：解释改动；不做：越界 documents/style。',
    upstream: '当前开发流程 {current_doc} + 用户意见 {instruction} + 账本',
    downstream: '修订后重新驱动 documents', keepGuidance: '保留"沿用既定技术栈口径"。', schema: '',
  },
  {
    file: 'style_revision_prompt.txt', lang: 'zh', fill: 'format', output: 'markdown',
    role: '资深产品设计师', revision: true,
    placeholders: ['system_prefix', 'context_ledger', 'instruction', 'current_doc'],
    ledgerHeaders: ['基调'],
    requiredSections: '视觉定位、所选风格、缩略图生成提示词、后续代码开发 UI 基调提示词（以及原文件已覆盖的其它视觉小节）',
    scope: '据用户意见 {instruction} 对当前风格文档 {current_doc} 做精准增量修订，输出完整文档。',
    boundary: '不做：改功能/需求/技术栈；不做：解释改动。与产品定位口径保持一致，整篇自洽。',
    upstream: '当前风格文档 {current_doc} + 用户意见 {instruction} + 账本',
    downstream: '修订后驱动 preview/前端 UI 基调', keepGuidance: '保留缩略图提示词/UI 基调提示词覆盖。', schema: '',
  },
  {
    file: 'document_split_revision_prompt.txt', lang: 'zh', fill: 'format', output: 'json-array',
    role: '软件研发流程设计师', revision: true,
    placeholders: ['system_prefix', 'context_ledger', 'instruction', 'requirements_doc', 'development_flow', 'current_documents'],
    ledgerHeaders: [], requiredSections: '',
    scope: '据用户意见 {instruction} 对当前文档数组 {current_documents} 做精准修订，输出修订后的**完整**文档数组；未受影响的文档/章节原样保留。',
    boundary: '不做：无谓改写；不做：破坏基线覆盖；不做：新增上游没有的需求。若用户要求增删某类文档，按意见调整但保持基线覆盖完整。',
    upstream: '需求 {requirements_doc} + 流程 {development_flow} + 当前数组 {current_documents} + 用户意见 {instruction}',
    downstream: '前端构建读取文档摘要',
    keepGuidance: '保留 JSON 契约。',
    schema: `只输出一个 JSON 数组（无围栏、无数组外文字）。每对象**必须且只能**含：document_type(snake_case：product_spec/frontend_spec/backend_spec/data_model/prompt_spec/acceptance_plan，可补充其它)、title(中文)、content(中文 Markdown，二级标题分节)、prompt_expert(中文，按"角色/输入/输出格式/约束/质量标准/反例"分点)。至少覆盖 product_spec/frontend_spec/backend_spec/data_model/prompt_spec/acceptance_plan。
⚠️ .format 文件：若放 JSON 示例，{ } 必须写成 {{ }}。`,
  },
  {
    file: 'requirements_section_revision_prompt.txt', lang: 'zh', fill: 'format', output: 'fragment',
    role: '资深产品经理', revision: true, sectionRevision: true,
    placeholders: ['system_prefix', 'context_ledger', 'current_doc', 'selected_text', 'instruction'],
    ledgerHeaders: [], requiredSections: '',
    scope: '用户在需求文档里选中了一小段 {selected_text}，只想局部调整。通读整篇 {current_doc}、保持主干与口径一致，按意见 {instruction} **只重写选中片段本身**。',
    boundary: '只输出用于替换选中片段的新文本本身；不要整篇、不要复述上下文、不要解释/前后缀/引号/代码围栏；只保证片段自洽，不改文档其它部分。紧扣项目共识的术语/选型/范围边界。若无需改变则原样返回片段。',
    upstream: '整篇 {current_doc} + 片段 {selected_text} + 意见 {instruction} + 账本',
    downstream: '输出原样拼回原片段位置', keepGuidance: '保留 fragment-only 契约。', schema: '',
  },
  {
    file: 'development_flow_section_revision_prompt.txt', lang: 'zh', fill: 'format', output: 'fragment',
    role: '软件研发流程设计师', revision: true, sectionRevision: true,
    placeholders: ['system_prefix', 'context_ledger', 'current_doc', 'selected_text', 'instruction'],
    ledgerHeaders: [], requiredSections: '',
    scope: '用户在开发流程文档里选中一小段 {selected_text}，按意见 {instruction} 只重写该片段，通读 {current_doc} 保持口径一致。',
    boundary: '只输出替换片段本身；不整篇/不复述/不解释/不围栏；片段自洽即可，涉及里程碑/模块/验收的相邻内容不要顺手改。',
    upstream: '整篇 {current_doc} + 片段 {selected_text} + 意见 {instruction} + 账本',
    downstream: '拼回原位置', keepGuidance: '保留 fragment-only 契约。', schema: '',
  },
  {
    file: 'document_section_revision_prompt.txt', lang: 'zh', fill: 'format', output: 'fragment',
    role: '资深研发工程师', revision: true, sectionRevision: true,
    placeholders: ['system_prefix', 'context_ledger', 'current_doc', 'selected_text', 'instruction'],
    ledgerHeaders: [], requiredSections: '',
    scope: '用户在某篇开发文档正文里选中一小段 {selected_text}，按意见 {instruction} 只重写该片段，通读 {current_doc} 保持一致。',
    boundary: '只输出替换片段本身；不整篇/不复述/不解释/不围栏；与其它开发文档口径一致；片段自洽即可。',
    upstream: '整篇文档正文 {current_doc} + 片段 {selected_text} + 意见 {instruction} + 账本',
    downstream: '拼回原位置', keepGuidance: '保留 fragment-only 契约（注意此文允许片段内含代码块对齐）。', schema: '',
  },
  {
    file: 'style_section_revision_prompt.txt', lang: 'zh', fill: 'format', output: 'fragment',
    role: '资深 UI / 产品设计师', revision: true, sectionRevision: true,
    placeholders: ['system_prefix', 'context_ledger', 'current_doc', 'selected_text', 'instruction'],
    ledgerHeaders: [], requiredSections: '',
    scope: '用户在风格文档里选中一小段 {selected_text}，按意见 {instruction} 只重写该片段，通读 {current_doc} 保持口径一致。',
    boundary: '只输出替换片段本身；不整篇/不复述/不解释/不围栏；涉及视觉定位/缩略图提示词/UI 基调提示词时只保证片段与上下文一致，不改其它部分。',
    upstream: '整篇 {current_doc} + 片段 {selected_text} + 意见 {instruction} + 账本',
    downstream: '拼回原位置', keepGuidance: '保留 fragment-only 契约。', schema: '',
  },
  {
    file: 'frontend_project_prompt.txt', lang: 'zh', fill: 'fill', output: 'agent-files',
    role: '资深前端工程师（在容器内创建并自检可构建的前端工程）',
    placeholders: ['CONTEXT_LEDGER', 'REQUIREMENT', 'REQUIREMENTS_DOC', 'DEVELOPMENT_FLOW', 'DOCUMENTS', 'STYLE_PROMPT', 'UI_BASELINE', 'FIGMA_DESIGN'],
    ledgerHeaders: [], requiredSections: '',
    scope: '在【当前目录】直接初始化一个 COMPLETE、可运行、可构建、接近真实上线视觉保真度的前端工程，实现已确认产品规格，并**逐 FR 实现且可在界面中演示/验证**。',
    boundary: '不做：与上游产物冲突的设计；不做：更换约定技术栈；不做：装饰性填充优先于完整流程。视上游（需求/流程/文档/风格/账本）为既定真理，派生而非重造。',
    upstream: '[[CONTEXT_LEDGER]] [[REQUIREMENT]] [[REQUIREMENTS_DOC]] [[DEVELOPMENT_FLOW]] [[DOCUMENTS]] [[STYLE_PROMPT]] [[UI_BASELINE]] [[FIGMA_DESIGN]]',
    downstream: 'fe_publish 收集源码与 dist 预览',
    keepGuidance: '这是最敏感的文件：必须逐字保留所有运营契约的语义与关键 token（见 operationalContracts）。可以把它们结构化进五段骨架并加强表达，但绝不能删除或弱化任何一条。把 emoji 限制、视觉保真、真实图片硬性验收都保留。',
    operationalContracts: [
      'React 19 + TypeScript + Vite + 纯 CSS（每组件配套 .css）；禁止 Tailwind / UI 组件库 / 任何 CDN / 远程资源 / 远程字体 / 运行时网络请求',
      'image-assets 技能在构建期生成、保存在 src/assets/ 下的本地图片不算远程资源（被 Vite 打包成本地文件，运行时不发网络请求）',
      '组件拆到 src/components/；状态用 React hooks；类型集中在 src/types.ts；数据用 localStorage 持久化并首次加载播种合理示例数据；图片资源放 src/assets/',
      "vite.config.ts 必须设置 base: './'（相对路径），以便子路径 iframe 预览",
      '所有交互必须真实可用，禁止死控件、禁止 href="#" 空链接，禁止 TODO/FIXME/占位/“未实现”/“coming soon”/空事件处理器/仅 mock 界面',
      '必须自己依次运行 `npm install` 和 `npm run build` 自检，修到两条命令都成功；不要运行 `npm run dev`、`vite preview` 或任何长驻/不退出进程',
      '必须用 `gen-assets` 命令（image-assets 技能执行器，触发 Codex 调图像模型）生成 3~6 张真实位图到 src/assets/ 并在界面 import 引用——这是硬性验收项；若 `gen-assets: command not found` 改用绝对路径 `/home/node/bin/gen-assets "..."`',
      'gen-assets 用法示例：一次可描述多张，每张写明 src/assets/ 下目标路径；命令返回后用 `ls src/assets` 确认 PNG 已生成；在组件中 `import heroUrl from \'./assets/hero.png\'` 引用',
      '严格控制 emoji：不要把 emoji 当作图标/插画/Logo/头像/界面装饰；功能性图标用内联 SVG；图片/插画/照片一律用 gen-assets 生成真实位图；仅当 gen-assets 明确报错时才退而用克制的 CSS 渐变/纯色块/内联 SVG 兜底，并在总结里写明 gen-assets 失败原因，且绝不用 emoji 充当图片',
      '内容真实可信，用贴合语境的真实文案/人名/数据，不要 Lorem ipsum/“示例1/2”占位',
      '完成后用一句话总结：生成了哪些主要文件、实际执行 gen-assets 生成了哪些图片资源（失败写明原因）、有哪些可交互功能、`npm run build` 是否通过',
    ],
    schema: '',
  },
  {
    file: 'frontend_build_prompt.txt', lang: 'en', fill: 'fill', output: 'json-object',
    role: 'senior frontend engineer (single-file HTML app)',
    placeholders: ['CONTEXT_LEDGER', 'REQUIREMENT', 'REQUIREMENTS_DOC', 'DEVELOPMENT_FLOW', 'DOCUMENTS', 'STYLE_PROMPT', 'UI_BASELINE'],
    ledgerHeaders: [], requiredSections: '',
    scope: 'Generate a COMPLETE, production-quality, fully-interactive single-page web app as exactly ONE index.html. Implement the real behavior of every FR from the spec and make each demonstrable in the UI.',
    boundary: 'Do NOT: contradict the upstream consensus/spec; introduce React/TypeScript/Tailwind/build tools/CDNs/remote assets/network calls; leave dead controls or stubs. Treat upstream artifacts as established truth.',
    upstream: '[[CONTEXT_LEDGER]] [[REQUIREMENT]] [[REQUIREMENTS_DOC]] [[DEVELOPMENT_FLOW]] [[DOCUMENTS]] [[STYLE_PROMPT]] [[UI_BASELINE]]',
    downstream: 'frontend_critic reviews it; frontend_repair fixes flagged issues',
    keepGuidance: 'Keep ALL existing HARD CONSTRAINTS (1-6) and the exact OUTPUT FORMAT. This is a fill-mode English file: JSON example braces stay LITERAL (single { }), do NOT escape; only [[KEY]] are placeholders.',
    operationalContracts: [
      'Output exactly ONE complete HTML document saveable as index.html with <!doctype html>,<html>,<head>,<style>,<body>,<script> in the same file',
      'Browser-native HTML/CSS/JS only; NO React, TypeScript, Tailwind, CSS frameworks, npm packages, CDNs, remote fonts, image URLs, network calls, build tools, import, process.env, import.meta',
      'Every interactive element must work; no dead controls; no href="#" without a handler',
      'No TODO/FIXME/"not implemented"/"coming soon"/alert-as-stub/empty handlers/mock-only screens; seed realistic in-memory data and make CRUD/interactions fully work',
    ],
    schema: `OUTPUT FORMAT — return ONLY a single JSON object, no prose, no markdown fences, braces LITERAL:
{
  "html": "<complete index.html source>",
  "summary": "one paragraph describing the app and its interactive features"
}
The "html" value is the complete source as a JSON string with escaped newlines.`,
  },
  {
    file: 'frontend_critic_prompt.txt', lang: 'en', fill: 'fill', output: 'json-object',
    role: 'strict frontend code reviewer (quality gate)',
    placeholders: ['HTML'],
    ledgerHeaders: [], requiredSections: '',
    scope: 'Decide PASS/FAIL on whether the generated single-file HTML satisfies: every visible control is interactive AND functionality is complete AND the delivery shape is valid. Be a checklist-driven, evidence-based reviewer; cite the specific control/line. Report each genuine violation with a severity (high/medium/low).',
    boundary: 'Do NOT invent issues; only report genuine violations. Do NOT rewrite the code (that is the repair stage). If fully interactive and complete, pass it.',
    upstream: '[[HTML]]',
    downstream: 'On fail, frontend_repair fixes the reported issues',
    keepGuidance: 'Keep the exact OUTPUT FORMAT and the four reject categories (dead interactivity / incomplete functionality / broken runtime / invalid delivery shape). Fill-mode English: JSON braces LITERAL.',
    operationalContracts: [
      'Reject categories: (1) dead interactivity (control with no working handler / input not affecting state / form with no submit); (2) incomplete functionality (TODO/FIXME/"// ..."/"not implemented"/"coming soon"/alert-as-stub/empty handler/feature that does nothing); (3) broken runtime (JS syntax errors, undefined refs, missing DOM nodes, state never affecting UI); (4) invalid delivery shape (not single HTML doc / external pkg/CDN/font/image / network calls / React/TS/build-tool code / import / process.env / import.meta)',
    ],
    schema: `OUTPUT FORMAT — return ONLY a single JSON object, no prose, no markdown fences, braces LITERAL:
{
  "passed": true,
  "issues": [ { "file": "index.html", "problem": "Save button has no click handler", "severity": "high" } ],
  "summary": "one sentence verdict"
}`,
  },
  {
    file: 'frontend_repair_prompt.txt', lang: 'en', fill: 'fill', output: 'json-object',
    role: 'senior frontend engineer (repair-only)',
    placeholders: ['HTML', 'ISSUES'],
    ledgerHeaders: [], requiredSections: '',
    scope: 'Fix EVERY reported issue in the single-file HTML so it fully satisfies "every visible control is interactive and functionality is complete". Wire real handlers/state; remove every TODO/placeholder/stub by implementing the real behavior. Return the COMPLETE corrected HTML, not a diff.',
    boundary: 'Repair only: do NOT add new features, do NOT rewrite working parts gratuitously, keep the SAME delivery shape (one complete index.html, inline CSS+JS). Do NOT introduce React/TS/Tailwind/CDNs/remote assets/network calls/Node APIs/build tools/import/process.env/import.meta.',
    upstream: '[[HTML]] + [[ISSUES]]',
    downstream: 'Re-reviewed by frontend_critic',
    keepGuidance: 'Keep exact OUTPUT FORMAT. Fill-mode English: JSON braces LITERAL.',
    operationalContracts: [
      'Same delivery shape: exactly ONE complete index.html with inline CSS and inline browser-native JavaScript',
      'No React/TS/Tailwind/CSS frameworks/CDNs/remote assets/network calls/Node APIs/build tools/extra deps/import/process.env/import.meta',
      'Return the COMPLETE corrected HTML source, not a diff',
    ],
    schema: `OUTPUT FORMAT — return ONLY a single JSON object, no prose, no markdown fences, braces LITERAL:
{
  "html": "<complete corrected index.html source>",
  "summary": "what you fixed"
}`,
  },
  {
    file: 'frontend_project_repair_prompt.txt', lang: 'zh', fill: 'plain', output: 'plain',
    role: '资深前端工程师（构建修复，仅修不增）',
    placeholders: [],
    ledgerHeaders: [], requiredSections: '',
    scope: '上一轮在【当前目录】生成的 React+TS+Vite 工程构建未通过。只做"修复"，让 `npm run build`（即 `tsc && vite build`）一次通过。',
    boundary: '不做：新增功能、重写已有实现、更换技术栈、引入新的远程资源/CDN/网络请求/UI 组件库/Tailwind。',
    upstream: '（无占位符）容器会在本提示词文本末尾追加真实构建报错节选',
    downstream: '修复后重新构建/收集',
    keepGuidance: '这是 plain 文件，无占位符。容器把构建报错追加在文本最后，所以**文本必须以"下面是实际的构建报错（节选），请据此定位并修复："这类引出句自然结尾**（保留这个结尾语义，让追加的日志接得上）。保留所有运营契约。',
    operationalContracts: [
      '优先补全缺失的文件/模块/导出：import 指向的文件不存在就按调用处用途创建并实现合理可用内容（禁止 TODO/FIXME/占位/空实现）',
      '修正错误导入路径、缺失默认/命名导出、类型错误、未使用变量等编译错误',
      '不得引入新的远程资源/CDN/网络请求/UI 组件库/Tailwind',
      "保持 vite.config.ts 的 base: './'",
      '改完自己运行 `npm run build` 确认通过；不要运行 `npm run dev`、`vite preview` 等长驻/不退出进程',
      '完成后用一句话说明修复了哪些文件、`npm run build` 是否通过',
      '文本必须以引出"构建报错（节选）"的句子结尾，供容器把日志追加在后面',
    ],
    schema: '',
  },
]

function recordBrief(r) {
  return JSON.stringify({
    file: r.file, lang: r.lang, fill: r.fill, output: r.output, role: r.role,
    placeholders: r.placeholders, ledgerHeaders: r.ledgerHeaders,
    requiredSections: r.requiredSections, scope: r.scope, boundary: r.boundary,
    upstream: r.upstream, downstream: r.downstream, keepGuidance: r.keepGuidance,
    operationalContracts: r.operationalContracts || [], schema: r.schema,
    revision: !!r.revision, sectionRevision: !!r.sectionRevision,
  }, null, 2)
}

const DRAFT_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['written', 'summary', 'placeholders_present'],
  properties: {
    written: { type: 'boolean' },
    summary: { type: 'string', description: '改了什么、如何体现 BMAD 五段骨架与边界' },
    placeholders_present: { type: 'array', items: { type: 'string' }, description: '文件中实际保留的占位符 token' },
  },
}
const VERIFY_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['ok', 'violations', 'verdict_summary'],
  properties: {
    ok: { type: 'boolean', description: 'true 仅当无 high/blocking 违规' },
    violations: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        required: ['check', 'severity', 'detail'],
        properties: {
          check: { type: 'string', description: 'R1占位符/R2花括号/R3输出契约/R4账本章节/R5运营契约/R6语言/骨架/边界/质量' },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          detail: { type: 'string' },
        },
      },
    },
    verdict_summary: { type: 'string' },
  },
}
const REPAIR_SCHEMA = {
  type: 'object', additionalProperties: false,
  required: ['repaired', 'residual_violations', 'summary'],
  properties: {
    repaired: { type: 'boolean' },
    residual_violations: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
}

function draftPrompt(r) {
  return `${SHARED_SPEC}

# 你的任务
把以下这一个提示词文件按上面的 BMAD 骨架与 House Rules 重写，并**写回它的原路径**。

文件路径：${DIR}/${r.file}

本文件的契约记录（务必逐条满足）：
${recordBrief(r)}

${r.schema ? `# 输出形态契约（必须在重写后的提示词里精确体现，结构/字段/枚举不得变）\n${r.schema}\n` : ''}
${(r.operationalContracts && r.operationalContracts.length) ? `# 必须逐字保留语义的运营契约（每一条都要在新提示词中出现，可加强不可删/不可弱化）\n- ${r.operationalContracts.join('\n- ')}\n` : ''}
# 步骤
1. 用 Read 读取 ${DIR}/${r.file} 现有内容，理解其真实意图与所有已有硬约束。
2. 按 BMAD 五段骨架重写：角色与原则 / 输入（视为既定事实）/ 本阶段职责与边界 / 产出契约（唯一权威）/ 交付前自检。融入稳定 ID（按记录的 scope/boundary）。
3. **严格遵守 House Rules R1–R8**：占位符逐字保留且不新增；花括号按 fill 模式正确处理（format 双花括号转义、fill 单花括号字面量、plain 无占位符）；输出形态契约不变；账本兼容章节名保留；运营契约逐字保留；语言保持 ${r.lang}；只动这一个文件。
4. 对 fill='format' 文件：在心里用 str.format 验证——除列出的占位符外不得有任何单花括号。对 fill='fill' 文件：JSON 示例花括号保持单个、不要转义。
5. 用 Write 把重写后的完整内容写回 ${DIR}/${r.file}（覆盖）。
6. 返回结构化结果。

注意：这是给 LLM 用的提示词模板，不是要你现在就执行它描述的任务；你只是在"重写这份模板文本"。务必让重写后的文本可读、专业、具体、可检查。`
}

function verifyPrompt(r) {
  return `你是严格的提示词审查员（对抗性）。审查下面这个**已被重写**的提示词文件是否满足全部契约与 House Rules。只读，不要修改文件。

文件路径：${DIR}/${r.file}

它必须满足的契约记录：
${recordBrief(r)}

${r.schema ? `# 它必须精确保留的输出形态契约\n${r.schema}\n` : ''}
${(r.operationalContracts && r.operationalContracts.length) ? `# 它必须逐字保留语义的运营契约（逐条核对是否都在）\n- ${r.operationalContracts.join('\n- ')}\n` : ''}
# 审查清单（逐项判定，发现问题就记成 violation）
- R1 占位符：用 Read 打开文件，确认 placeholders 里**每个**占位符都原样出现；**没有新增/改名/缺失**的占位符；对 .format 文件 {system_prefix} 与 {context_ledger} 仍是最前两个块。
- R2 花括号：
  · fill='format'：除占位符外，正文/JSON 示例里的**每个** { 和 } 是否都已写成 {{ }}？任何裸的单花括号（非占位符）都是 **high** 违规（会让 str.format 崩）。
  · fill='fill'：占位符是 [[KEY]]；JSON 示例花括号应是**单个**字面量；若被误转义成 {{ }} 记 violation。
  · fill='plain'：不应有任何占位符。
- R3 输出形态：output=json-array/json-object 是否仍要求"只输出 JSON、无围栏、无多余文字"，且字段名/枚举(如 document_type 取值集)/结构与契约一致？output=fragment 是否仍要求"只输出替换片段本身、不整篇、不解释、不围栏"？
- R4 账本章节：ledgerHeaders 里每个标题是否都在"产出契约"里被要求输出（## 标题）？
- R5 运营契约：上面列的每条是否语义与关键 token 都在？缺失/弱化任一条记 **high**。
- R6 语言：是否为 ${r.lang}？
- 骨架：是否具备 BMAD 五段（角色与原则/输入/职责与边界/产出契约/交付前自检 或其英文对应）？
- 边界：本阶段"明确不做（交给下游）"是否写清、是否与记录一致、是否避免了越界进入其它阶段职责？
- 质量：是否删除了原文件里本应保留的好约束（退步）？是否仍空泛？

把每个问题记为一条 violation（check 用 R1/R2/R3/R4/R5/R6/骨架/边界/质量；severity 用 high/medium/low）。ok 仅当**没有任何 high 违规**时为 true。`
}

function repairPrompt(r, violations) {
  return `下面这个提示词文件在对抗性审查中发现了违规，请**只修复这些违规**，不要引入新问题，然后用 Write 写回原路径。

文件路径：${DIR}/${r.file}

它的契约记录：
${recordBrief(r)}

审查发现的违规（逐条修掉）：
${violations.map((v, i) => `${i + 1}. [${v.severity}] (${v.check}) ${v.detail}`).join('\n')}

修复要求：
- 严格遵守 House Rules：占位符逐字保留不新增；fill='${r.fill}' 的花括号规则（format 双花括号转义/fill 单花括号字面量/plain 无占位符）；输出形态契约不变；账本章节保留；运营契约逐字保留；语言 ${r.lang}；只动这一个文件。
- 先 Read 当前文件，针对性修复列出的每条违规，保持其余高质量内容不动。
- 用 Write 写回 ${DIR}/${r.file}。
- 返回结构化结果（residual_violations 为仍无法消除的项，应尽量为空）。`
}

// ── Run: draft → verify → conditional repair, per prompt, pipelined ──────────
log(`BMAD 重写 ${PROMPTS.length} 个 Code 域提示词：draft → verify → repair`)

const results = await pipeline(
  PROMPTS,
  // stage 1: draft (writes file in place)
  (r) => agent(draftPrompt(r), {
    label: `draft:${r.file}`, phase: 'Draft', schema: DRAFT_SCHEMA,
    agentType: 'general-purpose', effort: 'high',
  }).then((d) => ({ r, draft: d })),
  // stage 2: adversarial verify (read-only)
  (prev) => {
    if (!prev) return null
    return agent(verifyPrompt(prev.r), {
      label: `verify:${prev.r.file}`, phase: 'Verify', schema: VERIFY_SCHEMA,
      agentType: 'general-purpose', effort: 'high',
    }).then((v) => ({ ...prev, verify: v }))
  },
  // stage 3: conditional repair
  (prev) => {
    if (!prev) return null
    const v = prev.verify
    if (!v || v.ok) {
      return { file: prev.r.file, ok: true, draft: prev.draft, verify: v, repaired: false }
    }
    const blocking = (v.violations || []).filter((x) => x.severity === 'high')
    const toFix = blocking.length ? blocking : (v.violations || [])
    return agent(repairPrompt(prev.r, toFix), {
      label: `repair:${prev.r.file}`, phase: 'Repair', schema: REPAIR_SCHEMA,
      agentType: 'general-purpose', effort: 'high',
    }).then((rep) => ({
      file: prev.r.file, ok: false, draft: prev.draft, verify: v,
      repaired: true, repair: rep,
    }))
  },
)

const clean = results.filter(Boolean)
const passedFirst = clean.filter((x) => x.ok).length
const repaired = clean.filter((x) => x.repaired)
const residual = repaired
  .map((x) => ({ file: x.file, residual: (x.repair && x.repair.residual_violations) || [] }))
  .filter((x) => x.residual.length)

log(`完成：${clean.length}/${PROMPTS.length} 处理；首轮通过 ${passedFirst}；触发修复 ${repaired.length}`)

return {
  total: PROMPTS.length,
  processed: clean.length,
  passed_first_pass: passedFirst,
  repaired: repaired.map((x) => x.file),
  residual_violations: residual,
  per_file: clean.map((x) => ({
    file: x.file,
    ok_first_pass: x.ok,
    repaired: x.repaired,
    violations: (x.verify && x.verify.violations) || [],
    residual: (x.repair && x.repair.residual_violations) || [],
  })),
}
