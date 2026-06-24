#!/usr/bin/env python3
"""
End-to-end联调 smoke for the full-stack pipeline
(frontend + backend + middleware → atomic deploy → live preview).

Drives the REAL HTTP API the same way the UI does, stage by stage, and reports a
clear PASS/FAIL per stage:

  1. auth            login (or --token / --register)
  2. project         use --project-id, or create + seed a canned-flow project
  3. start           POST /fullstack/runs  → shared contract + 3 concurrent runs
  4. generate        poll until the 3 generation runs reach a terminal status
  5. deploy          POST /deploy → poll until the deployment is running
  6. verify          GET /preview/<pid>/  +  GET /app/<pid>/api/health  (live backend)

What it actually exercises end to end: contract synthesis, the three concurrent
runs, the per-project middleware namespace, the generated backend's own Dockerfile
build (with the deploy-time AI self-heal rung), the long-lived container + health
check, the reverse proxy, and the frontend↔backend wiring. So it needs the live
stack reachable, ANTHROPIC_API_KEY configured (for backend generation), and Docker
available on the backend host (for deploy). Use --no-deploy to stop after
generation when Docker/AI for deploy isn't available.

Examples
--------
    # against the live stack, seeding a fresh canned project
    uv run python scripts/e2e_fullstack.py --base-url http://localhost:5001 \
        --email you@example.com --password 'secret'

    # against an existing project that already finished code_full_generation
    uv run python scripts/e2e_fullstack.py --token "$JWT" --project-id <pid>

    # wiring-only (stop after generation; no deploy)
    uv run python scripts/e2e_fullstack.py --token "$JWT" --requirement "待办应用" --no-deploy

Exit code: 0 if every attempted stage passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
import time

try:
    import requests
except ImportError:  # pragma: no cover
    print("requests is required: pip install requests", file=sys.stderr)
    raise SystemExit(2)

GEN_LANES = ("frontend", "backend", "middleware")
TERMINAL_OK = {"completed", "partial"}
TERMINAL = {"completed", "partial", "failed", "cancelled"}
DEPLOY_DONE = {"running", "failed", "rolled_back", "stopped"}

# A small, self-contained flow: a TODO API with one table — keeps the generated
# backend tiny so the docker build + deploy stays fast for a smoke run. Has the
# verbatim section headers the contract synthesis / middleware steps parse.
CANNED_FLOW = """# 开发流程

## 技术假设
后端采用 Node.js + Express,数据库用 PostgreSQL。沿用此方向,不另选栈。

## 模块拆分
M1 任务管理(覆盖 FR1, FR2)。

## 数据设计
Task 实体:id(uuid 主键)、title(text 非空)、done(boolean 默认 false)、created_at(timestamptz)。使用 PostgreSQL 存储。

## 接口设计
GET /health 健康检查,返回 200。
GET /tasks 返回任务列表。
POST /tasks 创建任务(body: title)。
DELETE /tasks/{id} 删除任务。

## 前端页面/状态
单页:任务列表 + 新建输入框 + 删除按钮(覆盖 FR1, FR2,属于 M1)。

## 后端服务
单一 REST 服务,负责任务的增删查。

## AI/提示词链路
本项目不涉及 AI 调用。

## 开发里程碑
MS1 跑通增删查(覆盖 FR1, FR2)。

## 验收标准
能创建、列出、删除任务;GET /health 返回 200。

## 风险清单
无重大风险。
"""
CANNED_REQUIREMENTS = (
    "# 需求文档\n\n"
    "## 功能需求\nFR1 用户可创建并查看任务列表。\nFR2 用户可删除任务。\n\n"
    "## 非功能需求\nNFR1 接口响应及时。\n\n"
    "## 技术架构建议\n前端 React,后端 Node.js + Express,数据库 PostgreSQL。\n"
)
CANNED_STYLE = "简洁、克制的浅色风格,卡片式列表。"


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class Reporter:
    def __init__(self) -> None:
        self.ok = True

    def stage(self, name: str) -> None:
        print(f"\n\033[1m▶ {name}\033[0m")

    def passed(self, msg: str) -> None:
        print(f"  \033[32m✓\033[0m {msg}")

    def failed(self, msg: str) -> None:
        self.ok = False
        print(f"  \033[31m✗\033[0m {msg}")

    def info(self, msg: str) -> None:
        print(f"    {msg}")


def main() -> int:
    p = argparse.ArgumentParser(description="End-to-end full-stack pipeline smoke.")
    p.add_argument(
        "--base-url",
        default="http://localhost:5001",
        help="backend or nginx base URL (default http://localhost:5001)",
    )
    p.add_argument("--token", default=None, help="JWT access token (skip login)")
    p.add_argument("--email", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--register", action="store_true", help="register the email/password first")
    p.add_argument(
        "--project-id",
        default=None,
        help="use an existing project (must have development_flow) instead of seeding",
    )
    p.add_argument(
        "--requirement",
        default="一个简单的待办事项应用",
        help="requirement for the seeded project (seed mode)",
    )
    p.add_argument("--title", default="E2E 全栈冒烟", help="seeded project title")
    p.add_argument(
        "--flow-file",
        default=None,
        help="path to a development_flow markdown to seed (default: canned TODO flow)",
    )
    p.add_argument(
        "--requirements-file",
        default=None,
        help="path to a requirements_doc markdown to seed (default: canned)",
    )
    p.add_argument(
        "--no-deploy", action="store_true", help="stop after generation; skip deploy + verify"
    )
    p.add_argument("--gen-timeout", type=int, default=1800, help="seconds to wait for generation")
    p.add_argument("--deploy-timeout", type=int, default=900, help="seconds to wait for deploy")
    p.add_argument("--poll", type=int, default=6, help="poll interval seconds")
    args = p.parse_args()

    base = args.base_url.rstrip("/")
    s = requests.Session()
    r = Reporter()

    def url(path: str) -> str:
        return base + (path if path.startswith("/") else "/" + path)

    def data_of(resp: requests.Response) -> dict:
        body = resp.json()
        if isinstance(body, dict) and "data" in body:
            return body.get("data") or {}
        return body

    # --- 1. Auth -------------------------------------------------------------
    r.stage("1. 认证")
    token = args.token
    if not token:
        if not (args.email and args.password):
            r.failed("缺少凭证:提供 --token,或 --email + --password")
            return 1
        if args.register:
            rr = s.post(
                url("/api/auth/register"), json={"email": args.email, "password": args.password}
            )
            if rr.status_code in (200, 201):
                r.passed(f"注册成功 {args.email}")
            else:
                r.info(f"注册返回 {rr.status_code}(可能已存在),尝试登录")
        rr = s.post(url("/api/auth/login"), json={"email": args.email, "password": args.password})
        if rr.status_code != 200:
            r.failed(f"登录失败 {rr.status_code}: {rr.text[:200]}")
            return 1
        token = rr.json().get("access_token")
    if not token:
        r.failed("未取得 access_token")
        return 1
    s.headers["Authorization"] = f"Bearer {token}"
    r.passed("已认证")

    # --- 2. Project ----------------------------------------------------------
    r.stage("2. 项目(就绪需含 development_flow)")
    if args.project_id:
        pid = args.project_id
        rr = s.get(url(f"/api/code/projects/{pid}"))
        if rr.status_code != 200:
            r.failed(f"项目不存在或无权访问 {rr.status_code}")
            return 1
        proj = data_of(rr).get("project", {})
        if not proj.get("development_flow"):
            r.failed(
                "该项目尚无 development_flow,无法跑全栈;请换一个已完成开发流程的项目或用 seed 模式"
            )
            return 1
        r.passed(f"复用现有项目 {pid}「{proj.get('title')}」")
    else:
        flow = _read_file(args.flow_file) if args.flow_file else CANNED_FLOW
        reqs = _read_file(args.requirements_file) if args.requirements_file else CANNED_REQUIREMENTS
        rr = s.post(
            url("/api/code/projects"), json={"requirement": args.requirement, "title": args.title}
        )
        if rr.status_code not in (200, 201):
            r.failed(
                f"创建项目失败 {rr.status_code}: {rr.text[:200]}(create 需要文本模型生成需求文档)"
            )
            return 1
        pid = data_of(rr).get("project", {}).get("id")
        r.passed(f"已创建项目 {pid}")
        # Seed deterministic flow / requirements / style so the run is repeatable.
        rr = s.patch(
            url(f"/api/code/projects/{pid}"),
            json={
                "requirements_doc": reqs,
                "development_flow": flow,
                "style_prompt": CANNED_STYLE,
            },
        )
        if rr.status_code != 200:
            r.failed(f"写入开发流程失败 {rr.status_code}: {rr.text[:200]}")
            return 1
        endpoints = (
            flow.count("\nGET ")
            + flow.count("\nPOST ")
            + flow.count("\nPUT ")
            + flow.count("\nPATCH ")
            + flow.count("\nDELETE ")
        )
        r.passed(f"已写入开发流程({len(flow)} 字符,约 {endpoints} 个端点)")

    # --- 3. Start the pipeline ----------------------------------------------
    r.stage("3. 启动全栈生成(合成共享契约 + 3 并发 run)")
    rr = s.post(url(f"/api/code/projects/{pid}/fullstack/runs"))
    if rr.status_code not in (200, 201):
        r.failed(f"启动失败 {rr.status_code}: {rr.text[:300]}")
        return 1
    started = data_of(rr)
    runs = started.get("runs", {})
    contract = started.get("contract", {})
    api = contract.get("api_contract", {})
    ts = api.get("tech_stack", {})
    paths = (api.get("openapi", {}) or {}).get("paths", {}) or {}
    mw = contract.get("middleware_manifest", {})
    r.passed(
        f"已启动:fe={runs.get('frontend')} be={runs.get('backend')} mw={runs.get('middleware')}"
    )
    r.info(
        f"契约状态={contract.get('contract_status')} 栈={ts.get('language')}/{ts.get('framework')} "
        f"端点={len(paths)} 数据存储={len(mw.get('datastores') or [])}"
    )

    # --- 4. Poll generation --------------------------------------------------
    r.stage("4. 生成中(轮询三条流水线直到结束)")
    deadline = time.monotonic() + args.gen_timeout
    last = {}
    final_runs: dict = {}
    while time.monotonic() < deadline:
        rr = s.get(url(f"/api/code/projects/{pid}/fullstack/status"))
        if rr.status_code != 200:
            time.sleep(args.poll)
            continue
        st = data_of(rr)
        final_runs = st.get("runs", {})
        line = []
        for lane in GEN_LANES:
            run = final_runs.get(lane) or {}
            status = run.get("status", "—")
            prog = run.get("progress") or {}
            line.append(
                f"{lane}={status}({prog.get('completed_steps', 0)}/{prog.get('total_steps', 0)})"
            )
        snapshot = " ".join(line)
        if snapshot != last.get("snap"):
            r.info(snapshot)
            last["snap"] = snapshot
        if all((final_runs.get(lane) or {}).get("status") in TERMINAL for lane in GEN_LANES):
            break
        time.sleep(args.poll)

    gen_ok = True
    for lane in GEN_LANES:
        status = (final_runs.get(lane) or {}).get("status")
        if status in TERMINAL_OK:
            r.passed(f"{lane}: {status}")
        else:
            gen_ok = False
            err = (final_runs.get(lane) or {}).get("error_message") or ""
            r.failed(f"{lane}: {status or '未结束(超时)'} {err[:160]}")

    if not gen_ok:
        r.info("生成阶段未全部成功(后端生成需 ANTHROPIC_API_KEY;查看 run 详情排查)。跳过部署。")
        return 0 if r.ok else 1

    if args.no_deploy:
        r.info("--no-deploy:在生成完成后停止。")
        return 0 if r.ok else 1

    # --- 5. Deploy -----------------------------------------------------------
    r.stage("5. 应用部署(中间件 → 构建+自愈 → 起容器 → 健康检查 → 反代)")
    rr = s.post(url(f"/api/code/projects/{pid}/deploy"))
    if rr.status_code not in (200, 201):
        r.failed(f"部署启动失败 {rr.status_code}: {rr.text[:300]}")
        return 1
    deploy_run_id = data_of(rr).get("run_id")
    r.passed(f"部署 run 已启动 {deploy_run_id}")
    deadline = time.monotonic() + args.deploy_timeout
    dep_status = None
    last["snap"] = None
    while time.monotonic() < deadline:
        rr = s.get(url(f"/api/code/projects/{pid}/fullstack/status"))
        if rr.status_code == 200:
            st = data_of(rr)
            dep = st.get("deployment") or {}
            drun = st.get("runs", {}).get("deploy") or {}
            dep_status = dep.get("status")
            prog = drun.get("progress") or {}
            snap = f"deploy={drun.get('status', '—')}({prog.get('completed_steps', 0)}/{prog.get('total_steps', 0)}) deployment={dep_status}"
            if snap != last.get("snap"):
                r.info(snap)
                last["snap"] = snap
            # Gate on the FRESH deploy run's status, not the deployment row — the
            # latter can briefly show a PRIOR deploy's terminal status before this
            # run resets it (a stale-read race).
            if drun.get("id") == deploy_run_id and drun.get("status") in TERMINAL:
                break
        time.sleep(args.poll)

    # Re-read the settled deployment once the deploy run finished.
    rr = s.get(url(f"/api/code/projects/{pid}/fullstack/status"))
    dep = (data_of(rr).get("deployment") or {}) if rr.status_code == 200 else {}
    dep_status = dep.get("status")
    if dep_status != "running":
        r.failed(f"部署未就绪:deployment={dep_status} {(dep.get('error_message') or '')[:200]}")
        return 1
    r.passed("部署运行中,后端容器健康检查通过")

    # --- 6. Verify live wiring ----------------------------------------------
    r.stage("6. 联通校验(预览页 + 经反代打真后端 /health)")
    rr = s.get(url(f"/preview/{pid}/?token={token}"), allow_redirects=True)
    if rr.status_code == 200:
        r.passed(f"预览页可访问({rr.status_code})")
    else:
        r.failed(f"预览页异常 {rr.status_code}")
    rr = s.get(url(f"/app/{pid}/api/health?token={token}"))
    if rr.status_code < 500 and rr.status_code != 404:
        r.passed(f"前端经 /app/{pid}/api/health 命中真后端:HTTP {rr.status_code} {rr.text[:120]}")
    else:
        r.failed(f"后端 /health 经反代异常 {rr.status_code}: {rr.text[:160]}")

    print()
    if r.ok:
        print("\033[32m全栈端到端联调:PASS\033[0m  预览:", url(f"/preview/{pid}/?token=<jwt>"))
    else:
        print("\033[31m全栈端到端联调:FAIL\033[0m(见上方 ✗）")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
