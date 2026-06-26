# 给运维与开发提供, AI 不加载

compose 端到端冒烟:怎么跑

0. 前置

- 宿主机有 Docker daemon(后端通过挂载 /var/run/docker.sock 以 Docker-out-of-Docker 方式拉起 fe-agent/be-agent 容器和被部署的应用容器——compose 已接好,不用你配)。
- 填 .env:make env 从 .env.example 生成,然后至少填:
  - ANTHROPIC_API_KEY(文本/代码生成)
  - OPENAI_API_KEY(图像 + Codex)
  - 没填也能跑通闭环,但生成会降级(影响分析走确定性兜底、fe/be-agent 出占位产物)——要验真实产物就得填 key。

1. 构建并起栈(首次必须带 agent 镜像)

make deploy        # = docker compose --profile setup build(含 fe/be/slicer-agent)+ up -d
make ps            # 等 postgres/redis/mongo/backend/frontend 都 healthy
- 前端 http://localhost(80),后端 http://localhost:5001。
- --profile setup 这步是关键:不构建 agent 镜像,全栈生成/部署起不来。

2. 先有一个"已部署应用"(二开的起点)

二开是对已部署应用的迭代,所以得先用既有流程产出一个已部署应用(这部分不是本次新增,但二开依赖它):
注册/登录 → /code 建项目 → 跑 code_full_generation → 确认 UI → 全栈生成(前端/后端/中间件)→ 部署。完成后 侧边栏「应用空间」/ /apps 里就能看到它。

3. 跑二开闭环(本次新增——推荐先用"仅后端"小变更,最快)

UI 路径(最简单): /apps → 点该应用「二次开发」→ 变更目标选「调整后端逻辑」、变更说明填如"给任务列表接口加一个按完成状态过滤的查询参数 done" → 提交 → 看影响分析 + 执行计划(scope=仅后端)→「确认并开始生成」→ 等后端 lane 重生成出现「生成完成,可部署新版本」→「部署新版本」→「已发布」→「打开应用」验证。

curl 路径(可脚本化):
BASE=http://localhost:5001
TOKEN=$(curl -s $BASE/api/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"yourpass"}' | jq -r .data.access_token)
AUTH="Authorization: Bearer $TOKEN"

# 拿到一个已部署应用的 PID
curl -s $BASE/api/code/apps -H "$AUTH" | jq '.data.apps[]|{project_id,title,deployment_status}'
PID=<上面的 project_id>

# ① 发起二开 → 自动起影响分析 run
IID=$(curl -s $BASE/api/code/apps/$PID/iterations -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"instruction":"给任务列表接口加 done 过滤参数","change_type":"backend_logic"}' | jq -r .data.iteration.id)

# ② 轮询到 awaiting_plan_approval,看分析+计划
curl -s $BASE/api/code/apps/$PID/iterations/$IID -H "$AUTH" \
  | jq '.data.iteration|{status,impact_scope,analysis:.analysis.recommended_lanes,steps:[.plan.steps[].lane]}'

# ③ 确认计划 → 起生成 lane(仅后端则只 1 条)
curl -s -X POST $BASE/api/code/apps/$PID/iterations/$IID/confirm -H "$AUTH" -d '{}' -H 'Content-Type: application/json' | jq .data.runs

# ④ 轮询到 generation_ready=true
curl -s $BASE/api/code/apps/$PID/iterations/$IID -H "$AUTH" | jq '.data.iteration|{status,generation_ready,runs}'

# ⑤ 部署新版本(把 deploy 关联到本次迭代)
curl -s -X POST $BASE/api/code/projects/$PID/deploy -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"iteration_id\":\"$IID\"}" | jq

# ⑥ 轮询到 released(读时懒重对账:deploy 完成且容器 RUNNING 自动置 released)
curl -s $BASE/api/code/apps/$PID/iterations/$IID -H "$AUTH" | jq '.data.iteration.status'

# ⑦ 验证应用接口确实变了(走反代)
curl -s "$BASE/app/$PID/api/health"
curl -s "$BASE/app/$PID/api/tasks?done=true"   # 你这次新增的过滤参数

4. 每阶段要核对的点

- 影响分析:impact_scope/recommended_lanes 与你的变更类型相符(仅后端→backend;新增表→含 middleware 且 database_change=true;登录/支付/权限→risk_level=high 且要求确认)。
- 确认:只起了应该起的 lane(仅后端就只有 1 个 code_backend_project_generation run);契约改动且你勾选允许时才会重合成。
- 部署:deploy run 过 provision→build→start→health→smoke→itest;迭代态走到 released;CodeDeployment.status=running。
- 回放:UI 里每个 run/lane 都能点开 AgentRunPanel 逐步回放。

5. 出问题怎么查

make logs S=backend          # 后端 + 编排日志
docker ps                    # 看被部署的应用容器(加入 worksflow-net)
ls .fe-agent-work/           # DooD 工作目录;降级时这里有 *_stderr.log / degraded 标记
- 影响分析"没走模型只出兜底" → .env 没填 ANTHROPIC_API_KEY,或新 prompt 没 sync 进 Mongo(但兜底仍会出合理计划,闭环不受影响)。
- 部署 health 失败 → 看时间线 narrate 的容器日志;常见根因见我之前的记忆(schema 漂移 / sslmode / 空库)。

6. 收尾

make down       # 停服务,保留数据库
# make destroy  # 危险:连库一起删
