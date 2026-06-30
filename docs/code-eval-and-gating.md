# Code 生成质量:验收闸门 + eval 框架(P0-B)

> 源自三篇 harness 文章的共同地基:**「度量你的评判者」**。Anthropic
> *harness-design-long-running-apps* 要求「读评估器日志找偏差迭代」与「假设压测:每个脚手架组件都编码了一条『模型还做不到』的假设,随模型变强要逐个移除」;OpenAI *harness engineering* 全篇 eval 驱动。本框架给 Code 生成补上**可随时间度量生成质量**的那一层,使「拧紧验收闸门」这件事建立在数据而非猜测上。

本文件覆盖 **eval 框架(P0-B)** 与 **评估器咬合(P0-A)**。P0-A 的判定改变项(评分阈值闸门、refine→pivot)**已实装但默认关闭**(env 未设时判定与 P0-A 之前逐字一致),须用 P0-B 标定后再逐个灰度开 —— 见 §6。

---

## 1. 它解决什么

生成链路早已有验收闸门(`_verify_support.Verification`:确定性房规 + 运行时浏览器冒烟 + 怀疑式 rubric 评审 → 合成一个 blocking 决策驱动有界修复)。缺的是**对这套闸门本身的度量**:

- 评估器判得准吗?(改 critic 提示词后判别力是回归了还是进步了?)
- 生成质量随时间在涨还是在跌?成功率、平均分、修复轮次、降级率?
- 哪个脚手架组件还「load-bearing」?哪些在当前模型上已是冗余成本?

三件事分别由下面三块回答,共享一个持久化层。

---

## 2. 架构

```
                         ┌─────────────────────────────┐
   在线(每次生成 run)  │  verify->repair loop 末尾    │
   FE/BE workflow ──────▶│  record_quality_sample(...)  │──┐
                         └─────────────────────────────┘  │
                                                           ▼
   离线(标定/回归)                              ┌──────────────────────┐
   scripts/eval_review.py ──record_eval_sample──▶│  CodeQualitySample   │  (新表)
                                                  │  code_quality_samples │
                                                  └──────────┬───────────┘
   假设压测                                                   │ summarize_quality
   scripts/harness_ablation.py                                ▼
   (不落库,纯诊断)            GET /api/code/quality/trends  (owner / admin ?scope=all)
```

- **在线半**:每个 FE/BE 生成 run 在验收循环结束时落**一条** `CodeQualitySample(kind="online")`——最终 verdict、rubric 分、weighted_score、功能覆盖、修复轮数、降级原因、共识 panel 规模、模型名。写入 **fail-soft**(指标写入绝不可拖垮生成 run)。
- **离线半**:`eval_review.py` 用**带标签的 fixture**(干净 app + 故意做坏的 app)跑**真实评估器**,经**与线上同一个闸门** `Verification.blocking` 判定,度量两件事:① 判别力(坏的被拦、好的不被拦)② 好/坏两簇的 weighted_score 分布(给 A1 阈值标定用真实数据)。`--persist` 时同样落 `kind="eval"` 样本做长期回归。
- **假设压测**:`harness_ablation.py` 每个 fixture 只调一次评估器,然后**确定性地**把房规 / 运行时 / 评审逐个移除重组判定,报告每个组件的**边际贡献**(flips / breaks)。0 breaks 的组件是可简化候选。

---

## 3. 文件索引

| 文件 | 角色 |
|---|---|
| `backend/models/code/quality.py` | `CodeQualitySample` 模型(`code_quality_samples` 表,JSON-in-Text,`create_all` 自动建,无 Alembic) |
| `backend/services/code/quality_metrics.py` | `record_quality_sample` / `record_eval_sample`(fail-soft 落库)+ `summarize_quality`(查询)+ **纯函数** `summarize_samples` / `bucketize` / `timeseries_by_day` / `block_reasons_of`(可无网络单测) |
| `backend/routes/code/quality_routes.py` | `GET /api/code/quality/trends`(owner 默认,admin `?scope=all` 全平台) |
| `backend/services/agent/workflows/_verify_support.py` | 新增 `RUBRIC_WEIGHTS` 常量 + `weighted_score_of()`;`aggregate_reviews()` 现在**也输出 `weighted_score`**(共识聚合此前只平均各维分、不出总分) |
| `backend/services/agent/workflows/code_{frontend,backend}_project_workflow.py` | 验收循环末尾各接一行 `record_quality_sample(...)` |
| `scripts/eval_review.py` | 离线 eval:6 个 FE + 3 个 BE fixture、真实闸门判别、评分分布、`--panel/--lane/--persist` |
| `scripts/harness_ablation.py` | 假设压测(组件边际贡献) |
| `tests/test_review_eval.py` | 集成回归守护(打真实文本 lane,`@pytest.mark.integration`) |
| `tests/test_quality_metrics.py` | 纯单元(无网络):聚合数学、`weighted_score_of`、共识补分、block-reason 推导 |

---

## 4. 怎么跑

```bash
# 离线 eval(需 .env 配好 AI_TEXT_*;打真实文本 lane,无用户计费)
uv run python scripts/eval_review.py                 # FE+BE,单评审
uv run python scripts/eval_review.py --panel 3       # 3 评审共识
uv run python scripts/eval_review.py --lane backend  # 只跑后端
uv run python scripts/eval_review.py --persist       # 同时落 kind=eval 样本(趋势)

# 作为 CI 回归守护(默认 not integration 不会跑;手动触发)
uv run pytest -m integration tests/test_review_eval.py -s

# 纯单元(无网络,日常 CI 默认就跑)
uv run pytest tests/test_quality_metrics.py -q

# 假设压测:哪个闸门组件还 load-bearing
uv run python scripts/harness_ablation.py

# 在线趋势(owner 看自己;admin 加 ?scope=all 看全平台)
GET /api/code/quality/trends?lane=frontend&window_days=30
```

`eval_review.py` 输出含一段 **weighted_score 分布**(good 簇 vs bad 簇 + functionality 维),并给出建议阈值——这是下一节标定 A1 的输入。

---

## 5. 如何用它标定 A1 评分闸门(通往 P0-A)

A1(评分阈值闸门)的落点已预留但**默认关闭**:`CODE_QUALITY_MIN_SCORE` 未设即不改判定。开启路径:

1. 把 `eval_review.py` 的 fixture 扩到足够代表真实项目分布(现 **20 个:12 FE + 8 BE**,覆盖死控件/假数据/房规/运行时崩溃/契约不一致/缺 health/登录 token 形态/越权泄露等)。
2. 跑 `eval_review.py`,看脚本末尾「suggested gate」——它会自动判断**哪个维度有干净 gap**(good-min > bad-max)才可做阈值。
   - **实测(n=20,deepseek-v4-pro 评审,判别 20/20)**:**总分两簇重叠**(good 1.8–5.0 / bad 0.0–3.4 —— 因「能用但朴素」的页面总分仅 1.8–2.0,与 bad 的 CONCERNS-but-blocked 用例重叠)→ **总分不可用作闸门**;**functionality 维干净**(good 3.0–5.0 / bad 0.0–2.0)→ 用 **`CODE_QUALITY_MIN_FUNCTIONALITY ≈ 2.5`**(gap 两侧各 ~0.5 余量)。
   - 即**主闸 = functionality 维 + 客观阻断项**(房规/运行时/评审 blocking_issues);`CODE_QUALITY_MIN_SCORE`(总分)意义不大,留空即可。
3. 设 `CODE_QUALITY_MIN_FUNCTIONALITY=2.5`,灰度开;用 `GET /quality/trends` 观察成功率/均分无异常;每步可单 env 回滚。重标定只需重跑 `eval_review.py` 看「suggested gate」。

> 已实装:`Verification.score_blocking`(env `CODE_QUALITY_MIN_SCORE` / 逐维 `CODE_QUALITY_MIN_*`)纳入 `blocking`,`aggregate_reviews` 输出 `weighted_score`、`weighted_score_of` 缺值时按 `RUBRIC_WEIGHTS` 兜底重算,所以单评审 / N 评审共识都拿得到可比总分;阈值命中会自动进样本的 `block_reasons`(threshold)。**默认 `CODE_QUALITY_MIN_SCORE` 未设 = 闸门关 = 判定不变。**

---

## 6. P0-A 已实装(默认关闭)— 如何启用

判定改变项全部 env-gated,**默认 env 下 `Verification.blocking` 与 P0-A 之前逐字一致**(单测 `tests/test_verify_gate.py` 钉死)。标定后按下表逐个灰度开、每步可单 env 回滚:

| 杠杆 | env | 默认 | 启用建议 |
|---|---|---|---|
| **A1 评分阈值闸门** | `CODE_QUALITY_MIN_SCORE`(+ 逐维 `CODE_QUALITY_MIN_FUNCTIONALITY` 等) | 未设=关 | 先跑 `eval_review.py` 看 good 簇下沿,设到略低于它;评分低于阈值时**也**触发修复,并把评审 advisory 打磨项回灌进修复简报 |
| **A3 refine→pivot** | `CODE_REPAIR_PIVOT` | `0`=关 | 设 `1`:某轮定向修复后评分无改善(`should_pivot`,gain<0.2)则把该轮简报从「仅修复」升级为「允许重构本模块」;判过分才生效,lane 挂时自然休眠 |
| **A2 多评审共识** | `CODE_REVIEW_PANEL` | `1` | 标定后设 `3`(轮换视角+多数票);**注意 review 是 per-call 计费,N=3→×3**(默认价 0) |
| 迭代轮次 | `CODE_VERIFY_MAX_ROUNDS` | `2` | 复杂项目可提到 `3` |

- **score gate 只在判过分时咬合**:`review is None`(judge 挂/未配)永不被阈值误杀,客观阻断项照常生效。
- **前端质量趋势看板(P1-3,已实现)**:admin-only 页 `/admin/quality`(`frontend/src/pages/admin/QualityTrends.tsx` + `api/quality.ts`),按 lane / 时间窗 / scope(全平台 vs 仅我的)看成功率/均分/修复轮次/降级率 + 按天柱状(柱高=通过率)+ 按通道 + verdict 分布;Sidebar admin 区入口,i18n 四语言。
- **prompt A/B**:`prompt_version` 分桶已就绪,手动 pin 不同 critic 版本跑两段即可对比;自动分流未做。

---

## 7. 运维注意

- **建表**:`code_quality_samples` 由启动时 `db.create_all()` 自动创建;本项目无 Alembic,`schema_guard` 只对**已存在**的表补列、跳过整张新表(见 [docs 无,见 CLAUDE.md「schema 演进」])。改平台后端 `make redeploy` 即生效。
- **计费**:在线评审复用 `pricing.CODE_PROJECT_REVIEW`(per-call、folded-in、provider 未配则不扣);**共识 panel=N → 自然 ×N**。默认价 0(免费),metering 时务必把 N 倍写进定价说明。离线 eval/ablation 走 `.env` 的 lane key、不走用户计费。
- **critic 提示词**:本框架不改 `*_critic_prompt.txt`;若后续扩校准范例,改 `.txt` 后须 **sync 进 Mongo** 才在运行时生效(见 CLAUDE.md「运行时 prompt 由 Mongo 读」),否则用的是旧版本。
- **写入 fail-soft**:`record_*` 任何异常都 rollback + 仅 WARNING 日志,绝不影响生成 run 的成败与计费。

---

## 8. 回归流程(本地/手动)——「度量评判者」的退化闸

前面 §1–§5 给的是**工具**(eval_review / ablation / trends),§8 把 eval 接成一个**退化闸**:用提交进仓库的 `eval/baseline.json` 当基线,`--check` 比对本次运行,**判别力一旦退化就非零退出**。

> **当前定位:本地/手动,未接 GitHub Actions。** 经评估**刻意不挂自动 CI**(2026-06-30 决定)——闸门能力做好放在这里随手能跑;将来要自动化,把 `make eval-regression` 包成一个定时/PR 的 Actions job 即可(需把 `AI_TEXT_AUTH_TOKEN` 设为 repo secret;`schedule` 仅从默认分支触发)。

```
本地 / 改完 critic 后 ─▶ make eval-regression        打真实文本 lane
                         └─ eval_review.py --check eval/baseline.json
                            ├─ exit 0  无退化            → 通过
                            ├─ exit 1  判别力退化/gap闭合  → 失败(查 critic prompt 或 fixture)
                            └─ exit 2  lane 挂/无 token    → 无法评估(不算退化)
```

### 8.1 baseline 与 `--check` 语义

`eval/baseline.json` 是**判别力快照**(由 `make eval-baseline` 写,提交进仓库),只记 `judged`(评判者真出过分)的 fixture:每条的 `correct`、整体 `discrimination`、以及 functionality 维的 **clean-gap**(`good_min > bad_max`)。当前基线:**20/20 判别正确**,functionality gap clean(bad ≤ 2.0 / good ≥ 4.0)。

`eval_review.py --check` 只在**真退化**时非零退出:
- 某条 baseline 里 `correct` 的 fixture 现在判错 → **regression(exit 1)**;
- baseline 里 functionality 是 clean-gap、现在闭合 → **regression(exit 1)**;
- 某条这次 **errored**(lane 超时/挂)→ **容忍**,打印 `~ skipped`,不算退化(停电不是退步);
- 全部 errored → **exit 2**(无法评估);
- baseline 里没有的新 fixture → 仅信息提示,不参与判定。

### 8.2 何时重写 baseline

baseline 钉的是**当前评判者的行为**。下列改动后须 `make eval-baseline` 重写并提交,否则 `--check` 会把「有意的提升」误报成失败:
- 改了 `*_critic_prompt.txt`(评审 rubric / few-shot);
- 改了 `_verify_support` 的裁决逻辑、`house_rules` 规则、或闸门 env 默认;
- 增删 `eval_review.py` 的 fixture。

> ⚠️ **prompt 源一致性**:`eval_review.py` 经 `prompt_store` 取 critic 提示词——本机若**连得到 Mongo** 用的是 Mongo 版(可能被 admin 改过),连不到则回退仓库 `.txt`(见 CLAUDE.md「运行时 prompt 由 Mongo 读」)。重写 baseline 与 `--check` 应在**同一 prompt 源**下跑才可比;若将来接 Actions(无 Mongo,走 `.txt`),baseline 也应在无 Mongo 条件下重写,保证同源。

### 8.3 本地命令

- `make eval` —— 跑一遍看判别 + 分数分布(标定阈值用);
- `make eval-baseline` —— 重写 `eval/baseline.json`(改完 critic / fixture 后做);
- `make eval-regression` —— 退化闸:有退化则非零退出(`--check` 等价命令)。
- 都打真实文本 lane,需 `.env` 配好 `AI_TEXT_*`;不走用户计费。
