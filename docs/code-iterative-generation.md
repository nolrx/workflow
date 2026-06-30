# 实施报告:分功能增量生成 + 验收驱动自迭代(面向「游戏 / 带后端中后台」量级)

> 目标:把当前「一轮 run 一次性吐出整个工程 + 固定 2 轮反应式修复」演进成
> **「分批构建 → 验收驱动地迭代到达标 → 预算闸早停」**,并把迭代/修复次数与超时
> 按真实产物量级(游戏、带后端的中后台)调大。**全部 env-gated,默认值下行为与现在逐字不变**。

---

## 0. 为什么针对这个量级

| 产物类 | 特征 | 对生成的压力 |
|---|---|---|
| **游戏** | 单页、canvas/状态机重、逻辑高度耦合(主循环/碰撞/计分/关卡/HUD/存档) | 一口气写完易超时;功能多但**共享一份游戏状态**,无法功能内并行;首轮常只跑通一半机制 |
| **带后端中后台** | 多页(鉴权 + N 实体 CRUD + 仪表盘 + 角色权限 + 导出)+ 后端(每实体接口 + DB + auth) | FR 常 10~20+;一个 build 装不下这么多实体 → 超时/漂移;前端共享路由/布局/auth,后端接口较独立 |

共同结论:**这两类都需要"迭代到验收通过"而不是"修 2 轮就收工";大项目还需要"分批构建"治超时。并行只在已分好的 lane 边界(FE/BE/MW)有意义,功能内并行=合并地狱。**

---

## 1. 现状基线(已核实,file:line)

- **生成 = monolithic 一次性**:`code_frontend_project_workflow.py:341` / `code_backend_project_workflow.py` 单次 `build_project(...)` → 容器内 `claude -p` 调一次产整个工程。提示词 `frontend_project_prompt.txt:111` *建议*逐功能 commit + `progress.json`,但**没有机制驱动分批**。
- **修复 = 反应式定轮数**:`CODE_VERIFY_MAX_ROUNDS=2`(`..._workflow.py:373` / `:327`);`blocking`(`_verify_support.py:243`:房规错∪运行时错∪评审阻断∪分数闸)才修;修复走 `build_project(base_files=...)` 编辑模式(`frontend_project_service.py:98-102` 把上轮源码挂进 `/out/_base`);**回归守护** `:508-519`(`repair_regressed`→回退);**pivot** `:474-480`(`should_pivot` 停滞→`REPAIR_PIVOT_PLAN`,env `CODE_REPAIR_PIVOT`)。
- **验收信号已齐、但没当循环条件**:`features_from_ledger`(`_verify_support.py:77`,FR/NFR→清单,上限 60)→ `render_features_block` → 评审回填 `apply_feature_results`(`:115`,逐条 `passes`)→ `repair_instruction`(`:249`,冻结已过、列未过)。**功能没全过 ≠ blocking**,所以今天功能没做完照样会停。
- **预算现状**:钱=0(`pricing.py` 全 `CODE_*` 默认 0);轮数=2;FE 生成超时 `FE_AGENT_TIMEOUT=720`、修复 `FE_AGENT_REPAIR_TIMEOUT=300`、总兜底 `FE_AGENT_TOTAL_TIMEOUT=2400`(`frontend_project_service.py:770-777`);BE 生成 `BE_AGENT_TIMEOUT=900`;文本 lane `AI_TEXT_MAX_TOKENS=32000`。**放宽迭代的真实代价不是积分,而是 wall-clock + 网关(flaky lane)负载 + worker 占用**。

---

## 2. 设计:两层循环

把"一次生成 + 2 轮修"换成**外层分批构建 + 内层验收驱动修复**,两层都复用现成的编辑模式 / 验收清单 / 回归守护 / pivot。

```
planner ──▶ 功能清单(features_from_ledger)+ 批次划分(可选)
            │
            ▼  外层:批次循环(CODE_BUILD_BATCHES,默认 1 = 现状 monolithic)
   ┌──────────────────────────────────────────────────────────┐
   │ batch b: build_project(base_files=上一批产物, 仅本批功能)    │
   │          ▼  内层:验收驱动修复循环                            │
   │   while 有未通过 functional feature 或 blocking:            │
   │       verify(房规 + 运行时冒烟 + 评审逐条 passes + 分数闸)    │
   │       if 全过且非 blocking: break(达标即停,不烧满预算)       │
   │       if 轮数 >= CODE_ITERATE_MAX_ROUNDS: break(预算闸)      │
   │       if 连续 CODE_ITERATE_STALL 轮功能覆盖无增长: pivot/停   │
   │       repaired = build_project(base_files=当前, repair_instruction)│
   │       if repair_regressed(prev, repaired): 回退 + break       │
   │       采纳 repaired                                          │
   │   git commit "batch b done"                                 │
   └──────────────────────────────────────────────────────────┘
publish(scaffold 注入)
```

### 2.1 验收驱动终止(核心改动)
今天的循环条件是「`blocking` 才继续修」。新增 env **`CODE_ITERATE_TO_ACCEPTANCE`(默认 0=现状)**:置 1 时,循环在**预算内**会**因「仍有未通过的 functional feature」而继续**,不只因 blocking。终止三选一:**① 全部 functional feature `passes=true` 且非 blocking(达标)/ ② 到 `CODE_ITERATE_MAX_ROUNDS`(预算闸)/ ③ 连续 `CODE_ITERATE_STALL` 轮功能覆盖无增长(早停,接 pivot)**。

### 2.2 分批构建(治超时 / 治漂移,大项目才开)
新增 env **`CODE_BUILD_BATCHES`(默认 1=现状一次性)**。>1 时:planner 把功能清单切成 b 批(中后台按实体/模块分组,游戏按「核心循环→机制→打磨」分层),**逐批在上一批源码上增量构建**(复用 `base_files` 编辑模式),每批跑一遍内层验收循环 + 一次 git commit。
- **上下文不爆**:编辑模式把上批源码**写到磁盘**(`/out/_base`),容器里的 Claude Code **读工作区文件**而非全塞进 prompt,所以批次累加不会线性撑爆 prompt token。
- **每批更小 → 单次 claude 调用不易超时**,且模型每批聚焦,减少"铺一堆半成品"。

### 2.3 为什么不并行 subagent(明确否决)
单个游戏/单个中后台前端**共享文件**(`App.tsx`/路由/store/游戏状态/`server.js` 路由表),并行 agent 各改各的 → 平台对 AI 发散产物**无自动合并** → 集成必崩。且容器内 Claude Code **本身就是 agentic 多步**,平台层再开 subagent 是双重编排。**并行只保留在现成的 lane 边界**(FE/BE/MW 已并发,`fullstack_routes.py`)。真要功能并行,只能在**彼此独立的页面/模块** + 一个**强制集成 agent** 缝合,复杂度与合并风险都高,**本期不做**。

---

## 3. 预算调参(按量级,推荐生产值)

所有 env 默认保持现状(=行为不变);下表是**面向两类产物的推荐生产值**,通过 `.env` 设置、不改代码默认。

| 旋钮 | env | 现默认 | 游戏 | 中后台(带后端) | 说明 |
|---|---|---|---|---|---|
| 验收驱动开关 | `CODE_ITERATE_TO_ACCEPTANCE` | `0` | `1` | `1` | 开:迭代到功能全过(预算内) |
| 迭代/修复轮上限 | `CODE_ITERATE_MAX_ROUNDS` | =`CODE_VERIFY_MAX_ROUNDS`(2) | **4** | **6** | 达标即停,不到上限不停 |
| 构建批次 | `CODE_BUILD_BATCHES` | `1` | `1`(或 2) | **3~4** | 大项目分批,edit-mode 累加 |
| 无进展早停 | `CODE_ITERATE_STALL` | `2` | `2` | `2` | 功能覆盖连 2 轮不增 → pivot/停 |
| pivot | `CODE_REPAIR_PIVOT` | `1`(已开) | `1` | `1` | 停滞轮允许重构模块 |
| 评审面板 | `CODE_REVIEW_PANEL` | `3`(已开) | `3` | `3` | 多数票;注意每轮评审 ×3 |
| FE 生成超时 | `FE_AGENT_TIMEOUT` | `720` | **1080** | `900` | 游戏逻辑重,给更久 |
| FE 修复超时 | `FE_AGENT_REPAIR_TIMEOUT` | `300` | **420** | **480** | 增量修复也需时间 |
| FE 总兜底 | `FE_AGENT_TOTAL_TIMEOUT` | `2400` | **3600** | **5400** | 随 批次×轮 放大 |
| BE 生成超时 | `BE_AGENT_TIMEOUT` | `900` | — | **1200** | 多实体 CRUD |

**轮数为什么这样定**:弱 agent lane 下首轮 monolithic build 通常只跑通约 50~70% 功能;每轮定向修复闭合一部分。游戏功能≈6~12 项 → 1 build + 约 3 修复(上限 4)多能达标;中后台 FR≈10~20+ 且需分批 → 给到 6 轮 + 3~4 批。**因为有"达标即停",实际轮数 ≤ 上限**——上限只是安全预算。

**wall-clock 量感(钱=0,代价是时间)**:
- 游戏:1 build(~12~18min)+ 2~3 修复(~5~7min/轮)≈ **30~40min**。
- 中后台:3~4 批 ×(build + 1~2 修复)≈ **50~90min**(达标早停可显著短于此)。
- **吞吐影响**:一个中后台 run 占一个 worker 近 1 小时;`AGENT_MAX_WORKERS=16`/`MAX_CONCURRENT_RUNS=12`,低并发无碍,高并发需排队或调大(注意 SSE 线程,见 `[[sse-thread-starvation]]`)。

---

## 4. 落点(改哪些 + 如何保证默认不变)

| 文件 | 改动 |
|---|---|
| `_verify_support.py` | 新增 `iterate_to_acceptance()`/`env_iterate_rounds(verify_default)`/`env_stall()` 读 env;新增 `unmet_functional(features)`、`coverage(features)`;`should_stop(verification, round, rounds, history, to_acceptance, stall)` 统一终止判定(默认参数下退化为「`not blocking or round>=2`」=现状) |
| `code_frontend_project_workflow.py:373-519` | 把 `max_verify_rounds` + 循环条件换成上面的两层结构;批次外层(`CODE_BUILD_BATCHES==1` 时退化为单批=现状);内层用 `should_stop`;回归守护/pivot 原样复用 |
| `code_backend_project_workflow.py:327…` | 对称改造(BE 无运行时冒烟,验收靠评审 + 编译/测试 + 部署期 itest) |
| `code_workflow.py`(planner)| `CODE_BUILD_BATCHES>1` 时产出「批次划分」(给每批一个功能子集);=1 时不产、行为不变 |
| `frontend_project_service.py` / `backend_project_service.py` | 仅当分批时给 build 传 `feature_subset`(prompt 增「本批只做这些功能、其余保持」段);超时 env 已存在,只调 `.env` 值 |
| 提示词 `frontend_project_prompt.txt` / `backend_project_prompt.txt` | 加可选「本批功能子集」占位符(分批时填,否则空=现状);**改后须 sync Mongo**(见 `[[prompt-store-mongo-overrides]]`) |
| `.env` | 设第 3 节推荐值(分游戏/中后台两套,可按 run 的 app 类型选;或先统一取中后台值) |

**默认不变的保证**:`CODE_ITERATE_TO_ACCEPTANCE=0` + `CODE_BUILD_BATCHES=1` + `CODE_ITERATE_MAX_ROUNDS` 缺省回退 `CODE_VERIFY_MAX_ROUNDS` 时,`should_stop` 退化成现有 `not _blocking or _round>=max_verify_rounds`,批次外层只跑一批 → **判定与产物逐字不变**;用 `tests/test_verify_gate.py` 同款单测钉死。

---

## 5. 验收信号增强(按量级)

迭代质量取决于"验收信号"准不准:
- **游戏**:现有 runtimecheck 已会「像用户一样点控件」(质量杠杆①);建议补**多步交互**(连点几下、检查计分/状态变化),把"机制真生效"喂进 `feature_results`。
- **中后台**:前端 CRUD 走交互冒烟 + 评审;**后端走现成的真实构建验证**(install→compile→test 自愈)+ **部署期 itest/smoke 闸**(`integration_test_service.py`)。功能清单与 FR 天然对齐。
- 两类都建议:planner 产功能清单时**显式标注每条的"可观测验收方式"**(点哪个控件看什么变化 / 打哪个接口看什么字段),让评审逐条判 `passes` 更可靠。

---

## 6. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 轮数变多 → 更多 flaky lane 暴露 | 已有 `AGENT_GATEWAY_RETRY_403` + 降级;回归守护保证每轮不更差;早停防空转 |
| wall-clock/吞吐下降 | 达标早停 + `CODE_ITERATE_STALL` 早停;总兜底超时封顶;必要时调 worker/排队 |
| 分批后批间集成裂缝 | 每批 build 都是**在全量源码上编辑**(非新建),且每批跑验收循环 + 运行时冒烟兜底集成 |
| 上下文增长 | 编辑模式走磁盘文件、非 prompt 堆叠 |
| 误改默认行为 | 全 env-gated,默认退化为现状 + 单测钉死;每个 env 可独立回滚 |

---

## 7. 分期实施

- **P-A(先做,低风险、复用 80% 零件)**:验收驱动内层循环(§2.1)+ `CODE_ITERATE_MAX_ROUNDS`/`CODE_ITERATE_TO_ACCEPTANCE`/`CODE_ITERATE_STALL` + `should_stop` + 单测。`.env` 取游戏/中后台轮数与超时。**这步就能把"功能真做完"的比例显著抬上去**,不动生成结构。
- **P-B(治超时,大项目才需要)**:分批构建(§2.2)+ planner 批次划分 + prompt「本批功能子集」+ `CODE_BUILD_BATCHES`。须 sync Mongo + 视情况重建 agent 镜像(若改容器脚本)。
- **P-C(信号增强)**:游戏多步交互冒烟 + planner 标注每条验收方式 + critic `fr_coverage` 维度。

---

## 8. 度量(确认迭代真有效,而非空转烧时间)

用已上线的 eval/quality-trends 闭环验证 P-A/P-B 的收益:
- `record_quality_sample` 已落每 run 的 `feature_stats`(passed/total)、修复轮数、降级原因 → `GET /api/code/quality/trends` 看**功能覆盖率↑、降级率↓、平均轮数**是否随开关改善;
- `scripts/eval_review.py` 的判别基线(`eval/baseline.json`,本地 `make eval-regression`)守住"评判者没退化",保证抬上去的是真质量;
- 若某类项目"轮数顶满但覆盖率不涨"=信号/lane 问题而非轮数不够 → 回到 §5 而不是继续加轮数。

> 相关:`[[harness-verify-repair-loop]]`、`[[code-eval-framework-p0b]]`、`docs/code-eval-and-gating.md`、`docs/code-fullstack-generation.md`。

---

## 9. 实施状态(2026-06-30 落地,对报告有取舍)

- **✅ P-A 全量**:验收驱动循环。`_verify_support` 加 `iterate_to_acceptance()` / `iterate_max_rounds()` / `iterate_stall()` / `functional_coverage()` / `should_stop()`;FE/BE 两 workflow 内层循环改用 `should_stop`(`_max_rounds` 在验收模式下取 `CODE_ITERATE_MAX_ROUNDS`)。**默认 env 下 `should_stop` 退化为旧 `not blocking or round>=max`,判定逐字不变**,`tests/test_verify_gate.py`(51 测)钉死。
- **✅ P-B 安全版**:`CODE_BUILD_BATCHES` + `split_batches()` / `render_feature_subset()`。批次 0 = 整体脚手架构建,批次 1..n-1 **复用现成编辑模式**(`base_files` + `change_instruction`)增量加本批功能 —— **未改 prompt 模板、未改 `build_project` 签名、免 sync Mongo**。用**确定性连续分块**代替报告里的 LLM planner 语义切分(更稳、零额外调用;语义分组列为未来增强)。默认 `CODE_BUILD_BATCHES=1` 走单次构建分支,逐字不变。
- **✅ P-C(键盘冒烟)**:`runtimecheck.mjs` 在内层 try 内加键盘输入驱动(`ArrowUp/Down/Left/Right/Space/Enter`),捕获游戏/canvas 的**输入触发型运行时错误**;fail-soft(失败只记 `interactions.error`,不升级成硬 page_error)。host 侧脚本,`make redeploy` 生效、免重建镜像。
- **⏸ P-C 延后两件(理由充分)**:
  - **critic `fr_coverage` 评分维度** —— 会改 `RUBRIC_WEIGHTS`(与 critic 提示词紧耦合),从而**作废刚标定的 `eval/baseline.json`**;属校准敏感改动,应走「改 critic → `make eval-baseline` 重标定」流程,不宜盲加。
  - **planner 逐功能「可观测验收方式」标注** —— 需改 planner 提示词 + ledger schema + `features_from_ledger` 携带新字段 + **sync Mongo**,且此环境无法验证激活;而 critic 现已逐功能判 `passes` 并记证据(`note`),边际价值有限。
- **.env**:已设 `CODE_ITERATE_TO_ACCEPTANCE=1` / `CODE_ITERATE_MAX_ROUNDS=6` / `CODE_ITERATE_STALL=2` + `FE_AGENT_TIMEOUT=900` / `FE_AGENT_REPAIR_TIMEOUT=420` / `BE_AGENT_TIMEOUT=1200`;`CODE_BUILD_BATCHES` 留注释默认关(每多一批 = 一次额外编辑构建,按需开)。
- **激活**:全部改动是 host 侧 `.py` + 容器脚本字符串 → **`make redeploy` 即生效,免重建 agent 镜像、免 sync Mongo**(本次未改 prompt)。验证:`ruff` 全过、440 非集成测试通过(含 `test_verify_gate` 51 条)。
