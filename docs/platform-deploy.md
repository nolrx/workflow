# 平台部署：优雅重发布(单机)

本文档描述**平台自身**(Worksflow 后端)在**单机 docker-compose** 下如何
优雅地发新版本，把"重启即中断"变成"短暂暂停 → 自动续跑"。

> 这里说的是**平台**的部署。用户生成的应用如何部署见
> [`code-fullstack-generation.md`](./code-fullstack-generation.md) 的应用部署。

## 为什么不是字面"蓝绿"

后端是**有状态的单进程**:Agent 运行时(`ThreadPoolExecutor`)、SSE 事件总线、
recorder 都是**进程内单例**,且 `reconcile_orphaned_runs` 假设单实例。两个后端实例
并存会立刻冲突——`green` 的 reconcile 会抢 `blue` 正在跑的 run(双重执行),一条 run
在 `blue` 跑而客户端被路由到 `green` 时 SSE 实时事件也收不到。所以单机这套架构下不做
字面蓝绿,而是 **优雅排空 + 续跑重启**:

1. **排空(drain)**:旧实例停止接受新任务(`POST /api/agent/runs`、resume/retry、
   全栈生成/部署),`/health/ready` 转 503。在飞任务与 SSE/快照/取消照常可用。
2. **重建**:`docker compose up -d --no-deps backend` 重建唯一的后端容器。
3. **续跑**:新进程启动时 `reconcile_orphaned_runs` 把在飞的 run 从**持久化阶段**续跑
   (见 [agent-resume-on-restart](#相关)),前端 SSE 自动重连、API client 对幂等请求
   做有界重试,抹平几秒切换窗口。

**权衡**:这条路换来"在飞任务不丢、API 只有几秒 blip",但**不是**字面零停机。真正零
停机需要先做 **worker 解耦**(把 executor 拆成独立容器 + 事件总线换 Redis pub/sub),
之后 web 层才能安全蓝绿/滚动。

## 一键优雅重发布

```bash
# 需要后端与脚本看到同一个 DEPLOY_CONTROL_TOKEN(见 .env)
make redeploy
# 等价于:scripts/deploy-backend.sh —— drain → build → 重建 backend → 等 /health/ready → reload nginx
```

手动运维:

```bash
make drain          # 让线上后端停止接收新任务(/health/ready → 503)
make drain-status   # 查看当前排空状态
make undrain        # 恢复接收新任务(例如取消了一次发布)
```

脚本可调环境变量:`DEPLOY_CONTROL_TOKEN`(必需,启用 ops 端点)、`DEPLOY_BASE_URL`
(默认 `http://localhost:5001`)、`DEPLOY_DRAIN_GRACE`(排空后等短任务落地的秒数,默认
20)、`DEPLOY_READY_TIMEOUT`(等新实例就绪上限,默认 120)、`COMPOSE`。

## 关键点 / 坑

- **健康探针拆分**:`/health`(=`/health/live`)是**存活**探针,排空时仍 200(进程还活着,
  Docker `HEALTHCHECK` / 重启策略用它);`/health/ready` 是**就绪**探针,排空时 503,
  部署脚本与未来的负载均衡据此判断"别再往这个实例发新流量"。
- **nginx upstream 陈旧 IP**:`frontend` 容器的 nginx 在配置加载时把 `backend` 解析成
  当时的 IP;`backend` 重建后可能换 IP,不 reload 会持续 502。脚本在新实例就绪后
  `nginx -s reload`(best-effort)。
- **SIGTERM 兜底**:即使有人直接 `docker stop backend`(没走脚本),`backend/gunicorn.conf.py`
  的 `post_worker_init` 也会在 SIGTERM 时**先排空再优雅退出**,所以关停窗口内不会再接新任务;
  在飞任务照样由新进程续跑。
- **崩溃循环护栏**:每个 run 的续跑次数记在其 config `_resume_count`,超过
  `AGENT_MAX_RESUME_ATTEMPTS`(默认 3)就判失败,避免反复重启把(可能很贵的)构建无限重跑。
- **ops 端点鉴权**:`/api/admin/lifecycle/*` 用 `DEPLOY_CONTROL_TOKEN`(`X-Deploy-Token` 头,
  常量时间比较),**不是**用户 JWT;未配置该 token 时端点禁用(403),所以默认是惰性的。

## 相关

- 进行中 run 跨重启续跑的实现:`backend/services/agent/runtime.py::reconcile_orphaned_runs`
  与 `tests/test_agent_resume.py`。
- 前端断流自动重连:`frontend/src/stores/{agentStore,fullstackStore}.ts`。
- 升级到真零停机的路线(worker 解耦 + Redis 总线)目前仅作设计方向,尚未实现。
