"""
Deterministic ``docs/`` + ``AGENTS.md`` scaffold for generated projects (P2-E).

Progressive disclosure (OpenAI harness-engineering): every generated repo gets a
short ``AGENTS.md`` table-of-contents + a ``docs/`` knowledge base, so iteration /
repair runs (which re-seed from the published source) can NAVIGATE the repo
instead of re-stuffing the whole prompt, and the project's hard conventions live
in-repo as "golden principles" (mirroring ``house_rules``).

Injected as a GUARANTEE: only files the agent did not already write are added, so
an agent-authored ``AGENTS.md`` / docs file is never clobbered. Returns a
``{path: bytes}`` map to merge into the published source (binary-safe like the
rest of the collected files).
"""
from __future__ import annotations


def _present(files) -> set[str]:
    return {str(p).replace("\\", "/") for p in (files or {})}


_FRONTEND_AGENTS_MD = """# AGENTS.md — 工程导览(续改/修复前先读本文件)

本工程由 Worksflow 自动生成,技术栈为 React + TypeScript + Vite(纯 CSS)。后续 AI 或人类在
续改、修复前,请**先读本文件与 `docs/` 下的文档**,再动手,避免推倒重来。

## 文档地图(docs/ 为权威说明)
- `docs/ARCHITECTURE.md` — 架构、目录约定与关键模块
- `docs/contract.md` — 共享 API 契约(全栈模式下存在;前端按它调真实后端)
- `progress.json` — 构建 / 验证状态(由生成流水线自动维护)

## 黄金准则(Golden Principles — 必须遵守;违反会被 House Rules 检查拦截并触发修复)
- 路由用 `HashRouter`(hash 模式),**禁止** `BrowserRouter` / history(产物挂在 `/preview/<id>/`、
  `/app/<id>/` 子路径下,history 路由会跳回主域名导致白屏)。
- `vite.config.ts` 使用 `base: './'`,保证子路径下资源可加载。
- **禁用** Tailwind、第三方 UI 组件库、远程 Web 字体;使用手写纯 CSS + 系统字体栈。
- 调后端走同源 `window.__API_BASE__`;响应统一拆 `resp.data`(`success===false` 视为错误);
  实时走 `window.__WS_BASE__` + `?token=` 查询参数鉴权。
- 需要真实图片用 `image-assets` 技能(`gen-assets`),不要用 emoji 当插画/图标。

## 关键目录
- `src/` — 源码(`components/`、`pages/`、`types.ts` 等)
- `src/assets/` — 被打包的图片资源(通过 import 引用,不要放 `public/` 用运行时绝对路径)
"""

_FRONTEND_ARCHITECTURE_MD = """# 架构说明(ARCHITECTURE)

React + TypeScript + Vite 单页应用,纯 CSS,无 UI 框架。

## 目录约定
- `src/main.tsx` — 入口,挂载 `HashRouter`。
- `src/pages/` 或 `src/components/` — 页面与组件。
- `src/types.ts` — 共享类型定义。
- `src/api/` 或等价封装 — 对后端的调用,统一基于 `window.__API_BASE__`。
- `src/assets/` — 打包图片资源。

## 路由与部署约定
- 产物被服务在子路径(`/preview/<项目id>/`、`/app/<项目id>/`)下,故必须用 hash 路由 +
  `base: './'`。详见 AGENTS.md 的黄金准则。

## 续改指引
- 续改前先读本目录文档与 `progress.json`;只改与变更相关的文件,保持其余不变。
"""

_BACKEND_AGENTS_MD = """# AGENTS.md — 工程导览(续改/修复前先读本文件)

本工程由 Worksflow 自动生成的后端服务。后续 AI 或人类在续改、修复前,请**先读本文件与
`docs/` 下的文档**,再动手。

## 文档地图(docs/ 为权威说明)
- `ARCHITECTURE.md` — 架构、分层与关键模块(工程根)
- `docs/contract.md` — 共享 API 契约(权威:接口、信封、错误码、鉴权)
- `docs/db-schema.md` — 数据库 schema(权威:表 / 列 / 类型)
- `progress.json` — 构建 / 验证状态(由生成流水线自动维护)

## 黄金准则(Golden Principles — 必须遵守;违反会被 House Rules 检查拦截并触发修复)
- 路由一律挂在**根路径**(如 `/auth/login`、`/items`、`/health`),**禁止** `/api` 前缀
  (平台反代会剥掉 `/app/<id>/api` 前缀,带 `/api` 会让每个接口 404)。
- 统一响应信封 `{success, data, message}` / `ApiError`;登录 `POST /auth/login` 返回 `data.token`(JWT),
  客户端用 `Authorization: Bearer` 携带。
- 提供 `/health` 健康检查;监听 `PORT`(默认 8080);`DATABASE_URL` / `REDIS_URL` 由平台注入。
- 异步引擎连接串用 `postgresql+asyncpg://`;**不要**硬编码 `sslmode`(由平台按需注入)。
- 空库要能**自播种** demo 数据(`SEED_DEMO_DATA`),保证首屏可见、demo 账号可登录(无外部依赖)。

## 关键文件
- `Dockerfile` — 构建与启动(`EXPOSE 8080`、`/health`)。
- `Makefile` — `make test` 等本地校验入口。
"""

def ensure_scaffold(
    files,
    *,
    kind: str,
    contract_block: str = "",
    db_schema_block: str = "",
) -> dict[str, bytes]:
    """Return ``{path: bytes}`` of scaffold docs to ADD (only those not present).

    ``kind`` is ``"frontend"`` or ``"backend"``. ``contract_block`` /
    ``db_schema_block`` are the rendered contract / schema text injected as docs
    when available (full-stack mode). Never clobbers an agent-authored file.
    """
    present = _present(files)
    add: dict[str, bytes] = {}

    agents_md = _BACKEND_AGENTS_MD if kind == "backend" else _FRONTEND_AGENTS_MD
    if "AGENTS.md" not in present:
        add["AGENTS.md"] = agents_md.encode("utf-8")
    # Frontend gets a docs/ARCHITECTURE.md (its prompt mandates none); the backend
    # prompt already mandates a root ARCHITECTURE.md, so don't duplicate it there.
    if kind == "frontend" and "docs/ARCHITECTURE.md" not in present:
        add["docs/ARCHITECTURE.md"] = _FRONTEND_ARCHITECTURE_MD.encode("utf-8")
    if contract_block and "docs/contract.md" not in present:
        add["docs/contract.md"] = (
            "# 共享 API 契约(权威 — 接口 / 信封 / 错误码 / 鉴权以此为准)\n\n" + contract_block
        ).encode("utf-8")
    if kind == "backend" and db_schema_block and "docs/db-schema.md" not in present:
        add["docs/db-schema.md"] = (
            "# 数据库 Schema(权威 — 表 / 列 / 类型以此为准)\n\n" + db_schema_block
        ).encode("utf-8")
    return add
