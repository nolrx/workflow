# Code 全栈生成(前端 + 后端 + 中间件)架构设计

> 状态:在研(2026-06)。本文件是权威设计 + 实施清单。当前版本只动 Code 域。

## 目标

在既有 `code_frontend_project_generation`(前端多文件工程)基础上,新增**后端多文件工程生成**与**中间件 provisioning**两个工作流,使三者:

1. **并发生成**:前端 / 后端 / 中间件三条沙箱流水线并行运行(用户已选「三个独立并发 run」)。
2. **统一进度**:三股进度展示在同一「执行详情」面板(新建多 run 面板,复用既有渲染原语)。
3. **互相打通,零功能错误**:由**共享 OpenAPI 契约**连接 —— 后端实现它、前端消费它。
4. **前端实打后端**:生成的前端所有请求走 `/app/<pid>/api` 命中实跑的后端容器。
5. **原子部署**:中间件 → 后端 → 前端 有序拉起 + 健康检查 + 任一步失败回滚。
6. **技术栈跟随 flow 文档**(polyglot):生成的后端工程自带 `Dockerfile`,部署时 `docker build` 它自己 —— 多语言复杂度落到「工程自带 Dockerfile」,后端构建 agent 只需会**写**代码。

## 枢纽:共享 OpenAPI 契约

- 三 run 启动**之前**,编排端点同步合成一份 **OpenAPI 3.x 契约 + 中间件清单**(从 `development_flow` 的 `## 接口设计`/`## 数据设计`/`## 后端服务`/`## AI/提示词链路` 推导)。
- 存进**共享账本表** `CodeProjectLedger`(resource_id 键控 + version 乐观锁),作为三 run 的唯一真值源。
- 后端 agent 按契约实现路由;前端 agent 按契约生成 API client;→ 「由 api 文档连接两服务」「无功能错误」。

## 运行拓扑(三独立 run + 一部署 run)

```
[已完成 code_full_generation] → requirements_doc / development_flow / documents / style / UI预览
        │
   POST /api/code/projects/<pid>/fullstack/runs   (前端一个按钮)
        │  ① 同步合成共享契约(写 CodeProjectLedger,计费 CODE_CONTRACT_SYNTHESIS)
        │  ② 创建 3 个并发 AgentRun:
        ├──> code_frontend_project_generation  (既有,改:注入契约 + 运行时 API base)
        ├──> code_backend_project_generation   (新:be_planner→be_project_build→be_publish)
        └──> code_middleware_provisioning       (新:mw_planner→mw_provision→mw_publish)
        │
   三者皆 COMPLETED 后,前端「部署并预览」→ POST /api/code/projects/<pid>/deploy
        └──> code_fullstack_deploy  (新部署 run,有序原子 + 回滚):
             中间件建库/跑迁移 → docker build+run 后端容器(注入 DB 连接) → 健康检查
             → 注册 /app/<pid>/api 反代 → 前端 dist 运行时 API base 指向它 → 端到端联通校验
```

- **并发上限**:`MAX_CONCURRENT_RUNS` 提到 6(env `AGENT_MAX_CONCURRENT_RUNS`);fullstack 编排端点创建 3 个 run,故须 ≥3 + 余量。
- **线程池**:`agent_runtime` max_workers 提到 8(env `AGENT_MAX_WORKERS`);三个容器构建 run 多在等 `docker run`(I/O 阻塞),需足够 worker。
- **每个 run 仍是单线程**(各自的 recorder/session)→ 不触发 recorder 并发问题。共识只走共享账本表,不靠 run 间内存通信。

## 部署与隔离(每项目长驻容器 + 共享设施命名空间)

- **后端容器**:`docker build -t app-<pid> -f <生成的Dockerfile>` → `docker run -d --name app-<pid> --network <APP_NETWORK> -e DATABASE_URL=... -e REDIS_URL=... -e PORT=8080`。在 compose 的 `app-net` 上,backend 用容器名直连,无需发布端口/host-gateway。
- **中间件**:复用共享 `postgres`/`redis`,按项目隔离 —— `CREATE DATABASE app_<pid_sanitized>`、redis key 前缀 `app:<pid>:`。连接串注入后端容器 env。
- **反代**:nginx `/app/` → backend;backend 路由 `/app/<pid>/api/<path>` → 代理到 `http://app-<pid>:<port>/<path>`(backend 持有部署登记表,动态路由放在 backend,nginx 保持静态)。
- **预览**:`/preview/<pid>/` 服务前端 dist,运行时 API base = `/app/<pid>/api`;CSP `connect-src` 放开到 `'self'`(同源经 nginx→backend→容器)。

## 数据模型(新增,`backend/models/code/fullstack.py`)

- `CodeProjectLedger`(resource_id=project_id 唯一键):`api_contract_raw`(OpenAPI JSON)、`middleware_manifest_raw`、`contract_status`(pending/building/ready)、`version`(乐观锁)、`shared_ledger_raw`(三 run 合并的共识账本)。
- `CodeDeployment`(project_id 键):`backend_run_id`/`frontend_run_id`/`middleware_run_id`/`deploy_run_id`、`container_name`、`internal_port`、`db_name`、`redis_prefix`、`status`(provisioning/building/running/failed/stopped)、`health`、`api_base_path`、`image_tag`、时间戳。

> `db.create_all()` 驱动 schema(无 Alembic),新模型须在 `backend/models/__init__` 链路被 import 才会建表;重启即生效。

## 计费(`pricing.py`,默认 0,可 env 覆盖)

`CODE_CONTRACT_SYNTHESIS`、`CODE_BACKEND_PROJECT_GENERATION`、`CODE_MIDDLEWARE_PROVISIONING`、`CODE_FULLSTACK_DEPLOY`。`WORKFLOW_COSTS` 既是计费表也是白名单 —— 三个新 workflow 必须登记。

## 服务层

- `services/code/fullstack/contract_service.py` —— 合成共享契约(一次 text-model 调用),乐观锁写 `CodeProjectLedger`;`ensure_contract(project_id)` 幂等。
- `services/code/backend_project_service.py` —— 镜像 `frontend_project_service`:DooD 容器跑 Claude Code+Codex 写 polyglot 后端工程(含 Dockerfile/健康检查/读 env),自检构建梯子(语法/契约校验,**不实跑**),二进制安全收集。镜像 `be-agent:latest`(`backend/docker/be-agent/`)。
- `services/code/middleware_service.py` —— 从清单生成 schema SQL / 迁移 / seed;`provision(project_id, manifest)` 在共享 pg/redis 建库/前缀并跑初始化。
- `services/code/deploy_service.py` —— 原子有序部署 + 回滚;长驻容器生命周期;健康检查;部署登记表读写;反代解析。

## 工作流(`services/agent/workflows/`)

- `code_backend_project_workflow.py`:`be_planner`(校验 requirements_doc+development_flow,载共享契约) → `be_project_build`(容器生成,事件翻译复用 fe 的 on_event,验收评审) → `be_publish`(源码 zip + meta,`domain_ref_type=code_backend_project_*`)。
- `code_middleware_provisioning` (`code_middleware_workflow.py`):`mw_planner` → `mw_provision`(生成 schema/迁移产物,**不实建库** —— 实建库在部署 run) → `mw_publish`(清单 + SQL artifact,`code_middleware_*`)。
- `code_fullstack_deploy` (`code_fullstack_deploy_workflow.py`):join 三 run 产物 → 调 deploy_service 原子部署 → 发 deploy meta(`code_deploy_meta`,含 preview_url + api_base)。

## 路由(`backend/routes/code/fullstack_routes.py`,蓝图挂 `/api/code` 或新前缀)

- `POST /projects/<pid>/fullstack/runs` —— 合成契约 + 创建 3 run,返回 `{contract, runs:{frontend,backend,middleware}}`。
- `POST /projects/<pid>/deploy` —— 创建 `code_fullstack_deploy` run。
- `GET  /projects/<pid>/fullstack/status` —— 三 run + 部署状态汇总。
- `GET  /projects/<pid>/contract` —— 取共享契约。
- `ANY  /app/<pid>/api/<path>` —— 反代到生成后端容器(部署后)。

## 前端

- 新 store `stores/fullstackStore.ts` —— 管理 FE/BE/MW + deploy 四个 run,各自 SSE 流;不动既有单 run `agentStore`(对话流仍用它)。
- 新面板 `components/code/CodeFullstackPanel.tsx` —— 三 run 分组进度(统一执行详情)+ 部署按钮 + 预览;复用 AgentRunPanel 的 step/event 渲染原语。
- `previewTabs.ts` —— 加 `backend`/`middleware`/`deploy` 的 STEP_TAB/PROGRESS_TAB;新 `domain_ref_type` 识别。
- `api/fullstack.ts` —— 新端点封装。
- i18n —— `agent`/`codeapp`/`code` 命名空间新 key,四语言 `{en,ja,ko,zh-CN}`。

## 部署基础设施

- `backend/docker/be-agent/Dockerfile` —— node 基础 + claude-code + codex(写代码足矣;实跑用工程自带 Dockerfile)。
- `docker-compose.yml` —— 加 `be-agent`(profile setup 构建);backend 加 env `APP_NETWORK`、`APP_BACKEND_PORT`;backend 已有 docker.sock + `.fe-agent-work` 挂载,后端容器构建复用同一 DooD 工作目录(prefix `be-agent-`)。
- `nginx default.conf` —— 加 `location /app/ { proxy_pass backend; }`(SSE/长连接友好)。

## Prompts(`backend/prompts/code/`,改后须 sync 进 Mongo,见 [[prompt-store-mongo-overrides]])

- `contract_synthesis_prompt.txt`(`.format` 模板,JSON 花括号转义)—— 产 OpenAPI + 中间件清单 JSON。
- `backend_project_prompt.txt` + `backend_project_repair_prompt.txt` + `backend_project_critic_prompt.txt`(`[[KEY]]` fill 模板,见 [[fill-prompt-token-dedup]] 占位符唯一)。
- `middleware_prompt.txt`。
- 过 `scripts/validate_code_prompts.py` + `tests/test_code_prompts.py` CI 守护;`scripts/sync_code_prompts.py` 同步。

## 实施清单(顺序) — 已完成 ✅

- [x] L0 模型:`models/code/fullstack.py` + 注册建表(create_all)
- [x] L1 计费/注册/并发:pricing(4 常量)、runtime(注册3+max_workers→8)、agent_routes(WORKFLOW_COSTS+guards+cap→6)
- [x] L2 服务:contract_service、backend_project_service、middleware_service、deploy_service
- [x] L3 工作流:backend / middleware / deploy 三个 workflow
- [x] L4 路由:fullstack_routes + app_proxy_bp(注册蓝图 app.py)+ preview_routes 部署感知改造
- [x] L5 基础设施:be-agent 镜像、compose(be-agent/网络命名/env)、nginx(/app/)
- [x] L6 前端:fullstackStore、CodeFullstackPanel、previewTabs、api/fullstack、i18n×4、StageArtifactCard 接入
- [x] L7 prompts(4 新 + FE prompt 改)+ validator MANIFEST
- [x] L8 测试:tests/test_fullstack_pipeline.py(14 例)+ lint clean + 既有回归通过

## 上线必读

1. **改过的 `frontend_project_prompt.txt` 需同步进 Mongo 才生效**(运行时读 Mongo,seed 不覆盖已存):
   `uv run python scripts/sync_code_prompts.py --key code/frontend_project_prompt.txt`
   新增的 4 个 prompt 是缺失 key,会被 `seed_defaults()` 自动插入,无需手动同步。
2. **deploy 需在 docker-compose 栈内运行**:生成的后端容器接入 `ai-creative-studio-net`,平台 backend 也须在该网络(compose 已配)。先 `docker compose --profile setup build` 构建 fe-agent / be-agent / slicer-agent 镜像。
3. 计费默认 0(免费);用 `PRICE_CODE_BACKEND_PROJECT` / `PRICE_CODE_MIDDLEWARE` / `PRICE_CODE_FULLSTACK_DEPLOY` / `PRICE_CODE_CONTRACT_SYNTHESIS` 开启计量。
```
