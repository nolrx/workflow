# Dev Sprint P3:并行调度设计备忘

> 状态:**设计草案(未实施)**。本文承接 `docs/code-dev-sprint-p1-p2.md`。
> P0/P1/P2 已交付:`CodeDevTask` 持久任务板、`CodeDevSprint` **串行**调度器、
> `code_dev_turn` 单任务验收、`code_dev_backlog_planner` 任务规划、asset lane。
> P1/P2 文档已明确「不启动并行 sprint,P3 再做」(`code-dev-sprint-p1-p2.md:45`)、
> 「`parent_feature_id` 后续 P3 会用它避免同一 feature 并行冲突」(同 156)。本文就是那份 P3。

---

## 0. TL;DR

- **难的部分已经做好了。** 「单个 dev 容器里多 agent 安全并发编辑」这一块——git worktree
  隔离 + 集成屏障 + 冲突串行兜底——已经在 `code_dev_parallel_turn` +
  `dev_service` 的 git 原语里实装并验证过。P3 **不新增任何 git/容器机制**。
- **P3 只补两块新逻辑,都在调度器一侧:**
  1. **无冲突批量 claim**——把「每回合�──掉 1 个任务」换成「掰掉一批相互独立的就绪任务」。
  2. **逐任务 AC 回灌**——一批并行编辑合并后,按**每个任务各自的验收标准**分别推进其状态机
     (done / 回退 pending / blocked),而不是像手动并行 turn 那样只做一次「整体应用级」评审。
- **全程 env-gated,默认关 = 逐字等价于现串行行为**(遵循仓库惯例:每个有风险的杠杆默认关、
  判定逐字不变)。开关打开才走并行。

---

## 1. 现状与已具备的原语(不要重复造)

### 1.1 串行调度器(现状)
`code_dev_sprint_workflow.py` 的调度循环:每回合
`dev_sprint_service.claim_next_task()` 原子掰掉**一个**就绪任务 →
`_create_turn_run()` 建一个 `code_dev_turn` 子 run → `agent_runtime.run_sync()`
**同线程**驱动它 → 子 turn 自己按该任务 AC 验收并推进状态机 → 回合收尾。无状态可重启是设计不变量。

### 1.2 并行原语(已实装,来自手动多分片 turn)
`code_dev_parallel_turn_workflow.py` + `dev_service.py:592-674`:

| 原语 | 作用 |
|------|------|
| `dev.git_ready(pid)` | 确保容器内是 git 仓库 + 把当前 `/work` 快照成基线 HEAD;git 缺失返回 False → 调用方降级串行 |
| `dev.create_worktree(pid, i)` | 建 `dev-lane-<i>` 分支的隔离 worktree `/tmp/work-lanes/lane-<i>` |
| `dev.exec_turn(pid, prompt, workdir=wt)` | 在指定 worktree 里跑 `docker exec claude`(纯子进程、DB-free、可并发) |
| `dev.commit_worktree(pid, i)` | 把该分片改动 `git add -A && commit` 到它的分支(物化成可合并对象) |
| `dev.merge_lane(pid, i)` | 把分支合并回 `/work`,返回 `(ok, 冲突文件列表)`;冲突则 `merge --abort` 保 `/work` 干净 |
| `dev.cleanup_worktrees(pid, ids)` | 删除所有 lane worktree + 分支 |

**并发安全模型**(必须照抄):fan-out 的 worker 线程只做**纯子进程**(`exec_turn`)并把
stream 事件推进线程安全队列;**主线程**排队 drain 并发 `AgentEvent`(唯一 DB 写者)。
镜像自 `_verify_support.run_reviewers`。

### 1.3 逐任务 AC 验收(已实装,来自单任务 turn)
`dev_sprint_service.apply_verify_outcome(task, run_id, features, verification_blocking, summary)`
—— 这是 P3 最关键的复用点。它**只看该任务自己的 AC**(`ac_ids_for(task)` 圈定的
`FRx.Ty.ACn`),据此判定:

- 全部 AC 通过 且 无回归 且 无阻断 → `DONE`;
- 否则:还有重试预算 → 回 `PENDING`(retry+1),否则 → `BLOCKED`。

返回 `{status, passed, failed_criteria, regressed, note}`。**在一批合并后的 `features`
上对每个任务分别调用它,天然按各自 AC 打分**——这是并行验收几乎零成本落地的原因。

---

## 2. 目标 / 非目标

**目标**
- 让一个 sprint 在无依赖冲突的前提下,单回合并行推进**多个**就绪任务,缩短整体墙钟时间。
- 复用现成并行原语与逐任务验收,**不动** git/容器模型、不动任务状态机语义。
- 保持无状态可重启、协作式取消、SSE 回放、按任务 AC 验收这四个不变量。

**非目标(P3 不做)**
- 不做跨前后端并行(仍仅 `frontend` + 内联 `asset` lane,与 P1/P2 同)。
- 不做「同一 feature 内多子任务并行」——同 `parent_feature_id` 的任务**刻意不并行**(见 §3.1)。
- 不把 backlog 放进容器(DB 仍是任务状态唯一真相)。
- 不做批内的 in-turn 定向修复(失败任务走既有 retry 循环回下一回合,更简单也更安全;见 §5)。

---

## 3. 核心设计

### 3.1 无冲突批量选批 `claim_ready_batch`

新增 `dev_sprint_service.claim_ready_batch(session_id, lane, k) -> list[CodeDevTask]`,
与 `claim_next_task` 并列(**不替换**它;串行路径保持不变):

```
候选 = ready_tasks(session_id, lane)          # 已有:依赖全 done、lane 兼容、按优先级排序
batch, used_parents = [], set()
for cand in 候选:
    if len(batch) >= k: break
    # 冲突启发式:同一 FR/模块(parent_feature_id)不同批 → 大概率改同一批文件
    pf = cand.parent_feature_id or cand.feature_id
    if pf in used_parents: continue
    # asset 任务写共享输出目录 + 一次性 lane,不并行(见 §3.4)→ 只作为独批 k=1
    if cand.category == "asset":
        if batch: continue          # 已有非 asset 任务在批里,asset 留到下一批
        return [claim if mark_queued(cand.id)]   # asset 独占一批
    if mark_queued(cand.id):        # 原子掰:与 claim_next_task 同一条件 UPDATE 竞态守卫
        batch.append(refetch(cand.id)); used_parents.add(pf)
return batch
```

要点:
- **原子性**沿用 `mark_queued` 的条件 `UPDATE`(pending→queued);抢输某个候选就跳过,和串行同守卫。
- `parent_feature_id` 去重是**降低冲突的启发式**,不是正确性保证——真正的正确性由 §3.3 的
  **集成屏障**兜底(冲突 lane 自动串行重做)。所以选批不必追求完美,够松即可。
- 批大小 `k = min(空闲槽, CODE_DEV_SPRINT_BATCH)`,默认 `CODE_DEV_SPRINT_BATCH = 4`(对齐
  `DEV_MODE_MAX_PARALLEL`)。
- asset 任务**只跑独批**(k=1),回退到等价串行——它们本就 "asset 先行" 且写共享目录。

### 3.2 批量 turn:任务感知的并行 turn

**架构决策:批量以「一个批量子 run」为单位驱动,而非并发派发 K 个独立 `code_dev_turn`。**
原因:dev 容器是单例共享的——K 个独立 `code_dev_turn` 各自 `git_ready`/worktree/verify
打同一个容器且**彼此之间没有集成屏障**,正是设计评审点名的「合并地狱」。批量子 run 把
worktree 隔离 + **一次**集成屏障 + **一次**合并后评审集中掉,才是正确的执行单元。

**落地方式:把 `code_dev_parallel_turn` 抽成可共享的三段,加一条任务感知的 verify 尾。**
不再复制第 4 个近似 workflow。抽出:

- `_prepare(...)`:载项目/会话、起容器、reload 账本、seed checklist —— 手动版与批量版共用。
- `_parallel_edit_and_merge(lanes)`:建 worktree → fan-out `exec_turn` → 集成屏障 →
  冲突/失败 lane 串行重做 → 依赖/构建配置变更则重启 dev server —— **原样复用现实现**。
- **verify 尾分两种**:
  - 手动多分片(现状):一次整体应用级 `review_project` + `sync_checklist`(不变)。
  - **批量 sprint(P3 新增)**:见 §3.3。

批量子 run 的 `config`:
```json
{
  "session_id": "...",
  "sprint_id": "...",
  "tasks": [
    {"task_id":"...", "instruction": "<build_task_brief>", "feature_ids":[...], "ac_ids":[...]}
  ],
  "title": "[Sprint 并行] 3 个任务"
}
```
每个 task 的 `instruction` 仍走现有 `dev_sprint_service.build_task_brief(task, done_titles)`
(与串行完全一致),分片说明 `_LANE_NOTE` 照附。lane_i ↔ task_i 一一对应。

### 3.3 逐任务 AC 回灌(P3 唯一成体量的新代码)

合并后在 `/work` 采源、跑**一次** `review_project`,拿到覆盖全部 feature 的
`feature_results`;`apply_feature_results` 折出 `feats`。然后**对批里每个任务分别**:

```python
for t in batch_tasks:
    # 排除「同批其它任务的 AC」被误判为回归:回归集 = 全 feats 里
    # 既不属于本任务 AC、也不属于同批兄弟 AC 的失败项。
    sibling_ac = union(ac_ids_for(other) for other in batch_tasks if other != t)
    feats_for_t = [f for f in feats if f["id"] not in sibling_ac]  # 只是屏蔽兄弟 AC 作回归源
    outcome = dev_sprint_service.apply_verify_outcome(
        t, run_id, feats_for_t, verification.objective_blocking, summary,
    )
    emit CHECKLIST_UPDATED(task_id=t.id, task_outcome=outcome)
```

- `apply_verify_outcome` 已按 `ac_ids_for(t)` 圈本任务 AC,天然「各判各的」——**几乎白嫖**。
- **唯一坑点(必须处理):回归误判。** `apply_verify_outcome` 把「非本任务 AC 且未通过」的
  feature 记为回归。若不屏蔽,任务 B 的 AC 挂了会被算成任务 A 的「回归」。故对任务 A 回灌时,
  从其 `features` 里**剔除同批兄弟的 AC id**(上面 `sibling_ac`)。对「已完成任务」的真回归仍
  保留检测——那是应有的。
- `objective_blocking`(房规/运行时/评分)是**批级**的:合并后的整棵树若有硬阻断,批里所有任务
  都判不过(合理——合并结果坏了,谁都不算交付)。

### 3.4 asset 任务
asset 任务(`category=="asset"`)**不进并行批**(§3.1 令其独批 k=1),因为它们共享一次性
codex/输出目录、且验收是容器内 `validate_resource_outputs` 的确定性覆写。独批 k=1 = 退化成
现串行 asset 路径,零改动复用 `code_dev_turn` 的 asset 分支。

---

## 4. 调度循环改动(伪代码 diff)

`run_code_dev_sprint_workflow` 的「one scheduling round」段,env-gated 分叉:

```python
# --- claim ---
if PARALLEL_ON:
    k = min(free_slots, CODE_DEV_SPRINT_BATCH)
    batch = dev_sprint_service.claim_ready_batch(session_id, session.lane, k)
else:
    t = dev_sprint_service.claim_next_task(session_id, session.lane)
    batch = [t] if t else []

if not batch:
    ... # 与现状完全相同的「无可调度」判定(等待/完成/阻塞)

# --- drive ---
batch_run = _create_batch_turn_run(ctx, session_id, sprint_id, batch)   # 见下
if batch_run is None:  # 积分不足
    for t in batch: release_to_pending(t.id, count_retry=False)
    return _finish(BLOCKED, "积分不足")
sprint.turn_count += 1                       # 一批算一个回合(更省预算,合理)
sprint.set_current_task_ids([t.id for t in batch])   # 已是 list,天然支持多任务
db.session.commit()

run_sync(app, batch_run.id)                  # 同线程驱动;子 run 内部再 fan-out

# --- settle ---
child = get(batch_run.id)
if child.status == CANCELLED: return _finalize_cancelled()
if child.status == FAILED:                   # 基础设施级(非验收失败)
    consecutive_failures += 1
    for t in batch:
        release_to_pending(t.id, note="批量回合基础设施失败,已重新排队", count_retry=False)
    if consecutive_failures >= _MAX_RUN_FAILURES: 终止 sprint(FAILED)
else:
    consecutive_failures = 0
    # 每个任务的状态已由子 run 内 apply_verify_outcome 推进;这里只兜底
    # 「子 run 崩在半路留下 ACTIVE」的任务(reconcile 会 heal,与现状同)。
    for t in batch:
        row = get(t.id)
        if row.status in ACTIVE: reconcile_or_release(row)

newly_done = any(get(t.id).status == DONE for t in batch)
sprint.stall_count = 0 if newly_done else sprint.stall_count + 1
sprint.set_current_task_ids([]); persist snapshot; _emit_pulse(...)
```

`_create_batch_turn_run`:与现 `_create_turn_run` 同构,差异:
- `workflow = "code_dev_parallel_turn"`(任务感知模式,config 带 `tasks`)。
- `credit_reserved = CODE_DEV_TURN * len(batch)`(见 §7)。
- **给批里每个任务**都盖 `last_attempt_run_id = batch_run.id`(现只盖一个)——供崩溃后按 run 对账。

**取消转发**:`_forward_cancel` watcher 不变,目标是单个 `batch_run.id`(子 run 内部会把取消
广播给各 lane 的 `exec_turn`,现实现已有 `is_cancelled` 透传)。

---

## 5. 失败 / 取消 / 续跑语义

| 场景 | 处理 |
|------|------|
| 某 lane 编辑未干净完成 / 合并冲突 | 集成屏障已处理:该 lane 串行重做到 `/work`(现实现),再统一 verify |
| 某任务 AC 未过(有预算) | `apply_verify_outcome` → 回 `PENDING`(retry+1),下回合再被 claim(可能进更小的批或独批) |
| 某任务 AC 未过(无预算) | → `BLOCKED`,人工处理;不拖累同批其它任务 |
| 批量子 run 基础设施失败(FAILED) | 全批 `release_to_pending(count_retry=False)`——系统性故障不烧任务重试预算(镜像现 `_MAX_RUN_FAILURES` 注释精神);连续 `_MAX_RUN_FAILURES` 次则整 sprint FAILED |
| 取消 | 子 run 收敛 → CANCELLED → `_finalize_cancelled()`,批内 ACTIVE 任务 `mark_cancelled` |
| 服务重启 / pause 续跑 | 无状态循环重入:`reconcile_stale_tasks` 把「dead 批量 run 留下的 ACTIVE 任务」按 `last_attempt_run_id` 判活/重排——因为每个任务都盖了 batch_run.id,**批量下续跑无需任何新逻辑** |

**不做批内 in-turn 修复**:单任务 turn 会对失败 AC 做一次 edit-mode 定向修复;批量版**跳过**它。
理由:批量修复要么串行逐任务修(退化)、要么再来一轮 fan-out+屏障(复杂度陡增)。让失败任务走既有
retry 循环回下一回合,更简单、语义也和串行一致。(可作为 P3.1 增强。)

---

## 6. 数据 / 状态机改动:**几乎为零**

- `CodeDevSprint.current_task_ids` **本就是 list**(`get/set_current_task_ids`),多任务 in-flight 免改。
- 任务状态机(pending/queued/in_progress/verifying/done/blocked/failed/cancelled)不变。
- `parent_feature_id` 字段 P1 已存在,P3 才第一次真正用它(选批去重)。
- 不新增表、不新增列 → **零 schema 迁移**(符合 `create_all` + `schema_guard` 的演进模型)。

---

## 7. 计费

- 现状:每个任务子 turn 预扣 `CODE_DEV_TURN`。
- P3:批量子 run 预扣 `CODE_DEV_TURN * len(batch)`——每个任务仍对应一次 claude 编辑(一个 lane 一次
  `exec_turn`),成本与串行等量,只是墙钟并行。语义与「K 个串行 turn」计费一致,用户不多付也不少付。
- 退款:与现状同——仅批量子 run **基础设施级失败**且任务被重排时退整批;AC 验收失败不退(和串行一致)。
- **开放项**:是否要为「批量共享一次合并后评审」给折扣?建议先不折扣(评审成本相对模型编辑很小),
  P3 上线后按实际 `CodeQualitySample` 成本再定。改动只在 `_create_batch_turn_run` 一处,好调。

---

## 8. 配置开关与灰度

| env | 默认 | 含义 |
|-----|------|------|
| `CODE_DEV_SPRINT_PARALLEL` | `0`(关) | 关时 `claim_next_task` 单任务路径**逐字不变**;开时走批量 |
| `CODE_DEV_SPRINT_BATCH` | `4` | 单批最大任务数;实际 = `min(该值, 空闲槽, ready 数)` |
| `DEV_MODE_MAX_PARALLEL` | `4`(已存在) | 并行 turn 的 lane 上限,批量 turn 复用它做 fan-out 线程上限 |

灰度:默认关 → 先 env 打开对内测项目 → 观测 `CodeQualitySample` 通过率 / 冲突率(集成屏障
「改为串行重做」的 WARNING 频率)/ 墙钟缩短比 → 再决定是否默认开。

---

## 9. 测试计划(`tests/test_dev_sprint_parallel.py`,新增)

单元(无网络,fake dev service):
1. `claim_ready_batch` 只掰无同 `parent_feature_id` 的候选;抢占竞态下不超发(两次调用不重复掰同一任务)。
2. asset 任务永远独批(k=1),不与前端任务同批。
3. `k` 上限被 `CODE_DEV_SPRINT_BATCH` / ready 数 / 空闲槽三者取最小。
4. **逐任务回灌**:构造一批 3 任务,mock review 使 A 全过、B 一条 AC 挂、C 全过;断言 A/C→DONE、
   B→PENDING(retry+1),且 **A/C 不因 B 的 AC 失败被记回归**(sibling_ac 屏蔽生效)。
5. 批量子 run FAILED → 全批 `release_to_pending(count_retry=False)`,retry 预算不减。
6. 续跑:批量 run 标记为 dead,`reconcile_stale_tasks` 按 `last_attempt_run_id` 重排全批。
7. **开关关闭时判定逐字不变**——沿用 `test_verify_gate.py` 的「默认路径钉死」手法,断言
   `CODE_DEV_SPRINT_PARALLEL=0` 时走的仍是 `claim_next_task` 单任务分支(可用 spy 断言未调用
   `claim_ready_batch`)。

集成(可选,打真实容器):批 2 个改不同模块的任务 → 两 worktree 并发 → 干净合并 → 各自 AC 判定;
再构造两个都改 `App.tsx` 的任务 → 触发合并冲突 → 串行重做兜底 → 仍全部 settle。

---

## 10. 风险与开放问题

- **共享文件冲突频率**:`parent_feature_id` 去重挡不住「不同 FR 都改 App/路由/全局 store」。缓解:
  `_LANE_NOTE` 已叮嘱少碰共享入口;集成屏障兜底串行。若冲突率高(WARNING 频繁),说明并行收益被
  串行重做吃掉,可调小 `CODE_DEV_SPRINT_BATCH` 或对「触碰共享入口的任务」强制串行(P3.1)。
- **合并后评审的截断**:`_source_digest` 有体量上限,批越大源越大,评审 digest 越可能截断 →
  误判。缓解:`apply_verify_outcome` 只认 objective 硬阻断做闸门,主观 whole-app blocking 不参与
  任务关闭(现单任务 turn 已是此策略),批量沿用。仍建议批不宜过大(默认 4 保守)。
- **turn_count 语义**:一批算 1 回合,`max_turns` 预算下并行能推进更多任务——是增益不是问题,但
  停滞检测 `stall_count` 需按「本批**是否有**任务转 DONE」判,已在 §4 处理。
- **批内 in-turn 修复缺席**:失败任务多绕一个回合。可接受;P3.1 再补。

---

## 11. 实施清单(小步,每步可独立上线且默认关)

1. `dev_sprint_service.claim_ready_batch(session_id, lane, k)` + `_create_batch_turn_run` 骨架 + 单测 1-3。
2. 抽 `code_dev_parallel_turn` 的 prepare / parallel-edit-and-merge 为共享 helper(纯重构,手动版行为不变)。
3. 批量 verify 尾:逐任务 `apply_verify_outcome` + sibling_ac 回归屏蔽 + 单测 4。
4. 调度循环 env-gated 分叉(§4)+ 失败/续跑语义 + 单测 5-7。
5. 计费 `CODE_DEV_TURN * len(batch)` + 退款对齐(§7)。
6. `pricing` / `.env.example` / 本文档状态更新;跑全量 `uv run pytest -m "not integration"` + ruff。

> 生效方式:纯 backend `.py` 改动 → `make redeploy` 即可(无需重建 agent 镜像、无需 sync Mongo);
> 无 prompt 改动、无 schema 迁移。
