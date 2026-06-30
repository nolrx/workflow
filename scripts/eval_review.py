#!/usr/bin/env python3
"""
Offline eval harness for the skeptical project evaluator (the eval framework, P0-B).

You can't improve a judge you don't measure. This runs the REAL evaluator
(``FrontendProjectService.review_project`` / ``BackendProjectService.review_project``
on the text lane) over LABELED fixtures — clean apps and deliberately-broken ones —
through the SAME verify gate the live workflows use (``_verify_support.Verification``:
deterministic house-rules + runtime smoke + the rubric evaluator's blocking_issues),
and reports two things:

  1. **Discrimination** — does the end-to-end gate block the broken builds and NOT
     the clean ones? (The product-level metric: ``gate_blocked == expected``.)
  2. **Score distribution** — the rubric ``weighted_score`` for the good vs bad
     clusters, so a human can pick the ``CODE_QUALITY_MIN_SCORE`` threshold for the
     A1 score gate with real data instead of a guess.

Calls a live model, so it needs ``AI_TEXT_*`` configured (and uses the Mongo /
bundled critic prompt). Also wired as ``tests/test_review_eval.py`` (marked
``integration``) so it doubles as a regression guard for the critic prompts.

    uv run python scripts/eval_review.py                 # both lanes, single reviewer
    uv run python scripts/eval_review.py --panel 3       # 3-reviewer consensus
    uv run python scripts/eval_review.py --lane backend  # one lane only
    uv run python scripts/eval_review.py --persist       # also store eval samples (trend)
    uv run python scripts/eval_review.py --baseline      # snapshot current behavior -> eval/baseline.json
    uv run python scripts/eval_review.py --check         # gate: fail (exit 1) on regression vs baseline

The ``--check`` mode is the regression flow's teeth: it compares this run to the
committed ``eval/baseline.json`` and exits non-zero only when a previously-correct
fixture now misjudges, or the functionality clean-gap closes. Lane errors (model
outage / timeout) are TOLERATED — an outage is not a regression. Run it locally via
``make eval-regression`` (no GitHub CI is wired yet — see docs/code-eval-and-gating.md §8).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Committed regression baseline (written by --baseline, gated against by --check).
BASELINE_DEFAULT = REPO_ROOT / "eval" / "baseline.json"

try:  # standalone runs need .env so the text-lane provider resolves its key
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from backend.services.code import house_rules  # noqa: E402

# ============================ FRONTEND FIXTURES =============================
_FE_GOOD_TODO = """// ===== src/App.tsx =====
import { useState } from 'react'
export default function App() {
  const [todos, setTodos] = useState<string[]>([])
  const [t, setT] = useState('')
  return (
    <div>
      <input value={t} onChange={(e) => setT(e.target.value)} placeholder="待办" />
      <button onClick={() => { if (t) { setTodos([...todos, t]); setT('') } }}>添加</button>
      <ul>{todos.map((x, i) => (
        <li key={i}>{x} <button onClick={() => setTodos(todos.filter((_, j) => j !== i))}>删除</button></li>
      ))}</ul>
    </div>
  )
}
"""

_FE_GOOD_COUNTER = """// ===== src/App.tsx =====
import { useState } from 'react'
export default function App() {
  const [n, setN] = useState(0)
  return (
    <div>
      <h1>计数器</h1>
      <span data-testid="count">{n}</span>
      <button onClick={() => setN(n + 1)}>+1</button>
      <button onClick={() => setN(0)}>重置</button>
    </div>
  )
}
"""

_FE_BAD_DEAD = """// ===== src/App.tsx =====
import { useState } from 'react'
export default function App() {
  const [todos] = useState<string[]>(['示例待办'])
  const [t, setT] = useState('')
  return (
    <div>
      <input value={t} onChange={(e) => setT(e.target.value)} placeholder="待办" />
      <button>添加</button>
      <ul>{todos.map((x, i) => (<li key={i}>{x}</li>))}</ul>
    </div>
  )
}
"""

_FE_BAD_FAKE_DATA = """// ===== src/App.tsx =====
import { useState } from 'react'
export default function App() {
  const [t, setT] = useState('')
  const todos = ['示例1', '示例2']  // 硬编码假数据,永不更新
  return (
    <div>
      <input value={t} onChange={(e) => setT(e.target.value)} placeholder="待办" />
      <button onClick={() => alert('TODO')}>添加</button>
      <ul>{todos.map((x, i) => (<li key={i}>{x}</li>))}</ul>
    </div>
  )
}
"""

_FE_BAD_ROUTER = """// ===== src/main.tsx =====
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { createRoot } from 'react-dom/client'
createRoot(document.getElementById('root')!).render(
  <BrowserRouter><Routes><Route path="/" element={<div>home</div>} /></Routes></BrowserRouter>
)
"""

_FE_BAD_TAILWIND_APP = """// ===== src/App.tsx =====
export default function App() {
  return <div className="flex p-4 bg-blue-500 text-white">Hello</div>
}
"""
_FE_BAD_TAILWIND_CFG = """module.exports = { content: ['./src/**/*.tsx'], theme: {} }
"""

_FE_GOOD_FORM = """// ===== src/App.tsx =====
import { useState } from 'react'
export default function App() {
  const [name, setName] = useState('')
  const [msg, setMsg] = useState('')
  const [sent, setSent] = useState<string | null>(null)
  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    setSent(`已收到 ${name} 的留言`)
    setName(''); setMsg('')
  }
  return (
    <form onSubmit={onSubmit}>
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="姓名" />
      <textarea value={msg} onChange={(e) => setMsg(e.target.value)} placeholder="留言" />
      <button type="submit">提交</button>
      {sent && <p role="status">{sent}</p>}
    </form>
  )
}
"""

_FE_GOOD_TABS = """// ===== src/App.tsx =====
import { useState } from 'react'
const TABS = ['概览', '明细', '设置']
export default function App() {
  const [active, setActive] = useState(0)
  return (
    <div>
      <nav>{TABS.map((t, i) => (
        <button key={t} aria-selected={active === i} onClick={() => setActive(i)}>{t}</button>
      ))}</nav>
      <section>{active === 0 ? '概览内容' : active === 1 ? '明细内容' : '设置内容'}</section>
    </div>
  )
}
"""

_FE_BAD_REMOTE_CSS = (
    "@import url('https://fonts.googleapis.com/css2?family=Roboto&display=swap');\n"
    "body { font-family: 'Roboto', sans-serif; }\n"
)
_FE_BAD_REMOTE_APP = "export default function App() { return <h1>Hello</h1> }\n"

_FE_BAD_LOCKFILE_APP = "export default function App() { return <div>app</div> }\n"
_FE_BAD_LOCKFILE_LOCK = (
    '{ "name": "x", "lockfileVersion": 3, "packages": { "node_modules/react": '
    '{ "version": "18.2.0", "resolved": "https://registry.npmmirror.com/react/-/react-18.2.0.tgz" } } }\n'
)

_FE_BAD_CRASH = """// ===== src/App.tsx =====
export default function App() {
  const user: any = null
  return <div>欢迎 {user.profile.name}</div>  // 运行时崩溃:读取 null 的属性
}
"""

_FE_BAD_NOSUBMIT = """// ===== src/App.tsx =====
import { useState } from 'react'
export default function App() {
  const [email, setEmail] = useState('')
  return (
    <form>
      <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="邮箱" />
      <button>订阅</button>{/* 既无 type=submit 也无 onSubmit/onClick,点击无任何效果 */}
    </form>
  )
}
"""

# label "good" => gate must NOT block; "bad" => gate must block.
FE_FIXTURES = [
    {
        "name": "good-todo", "label": "good", "source": _FE_GOOD_TODO,
        "reqs": [("FR1", "用户能新增待办"), ("FR2", "用户能删除待办")],
        "files": {"src/App.tsx": _FE_GOOD_TODO},
        "runtime": {"ran": True, "console_errors": [], "page_errors": [],
                    "interactions": {"total": 2, "clicked": 2, "filled": 1, "dead_controls": []}},
    },
    {
        "name": "good-counter", "label": "good", "source": _FE_GOOD_COUNTER,
        "reqs": [("FR1", "点击按钮使计数加一"), ("FR2", "能重置计数")],
        "files": {"src/App.tsx": _FE_GOOD_COUNTER},
        "runtime": {"ran": True, "console_errors": [], "page_errors": [],
                    "interactions": {"total": 2, "clicked": 2, "filled": 0, "dead_controls": []}},
    },
    {
        "name": "bad-dead-control", "label": "bad", "source": _FE_BAD_DEAD,
        "reqs": [("FR1", "用户能新增待办"), ("FR2", "用户能删除待办")],
        "files": {"src/App.tsx": _FE_BAD_DEAD},
        "runtime": {"ran": True, "console_errors": [], "page_errors": [],
                    "interactions": {"total": 1, "clicked": 1, "filled": 1, "dead_controls": ["添加"]}},
    },
    {
        "name": "bad-fake-data", "label": "bad", "source": _FE_BAD_FAKE_DATA,
        "reqs": [("FR1", "用户能新增待办并出现在列表")],
        "files": {"src/App.tsx": _FE_BAD_FAKE_DATA},
        "runtime": {"ran": True, "console_errors": [], "page_errors": [],
                    "interactions": {"total": 1, "clicked": 1, "filled": 1, "dead_controls": ["添加"]}},
    },
    {
        "name": "bad-browser-router", "label": "bad", "source": _FE_BAD_ROUTER,
        "reqs": [("FR1", "首页可访问且子路径预览不跳主域名")],
        "files": {"src/main.tsx": _FE_BAD_ROUTER},
        "runtime": {"ran": True, "console_errors": [], "page_errors": [], "interactions": {}},
    },
    {
        "name": "bad-tailwind", "label": "bad",
        "source": _FE_BAD_TAILWIND_APP + "\n// ===== tailwind.config.js =====\n" + _FE_BAD_TAILWIND_CFG,
        "reqs": [("FR1", "展示首页")],
        "files": {"src/App.tsx": _FE_BAD_TAILWIND_APP, "tailwind.config.js": _FE_BAD_TAILWIND_CFG},
        "runtime": {"ran": True, "console_errors": [], "page_errors": [], "interactions": {}},
    },
    {
        "name": "good-form-submit", "label": "good", "source": _FE_GOOD_FORM,
        "reqs": [("FR1", "用户填写并提交留言表单后看到确认")],
        "files": {"src/App.tsx": _FE_GOOD_FORM},
        "runtime": {"ran": True, "console_errors": [], "page_errors": [],
                    "interactions": {"total": 1, "clicked": 1, "filled": 2, "dead_controls": []}},
    },
    {
        "name": "good-tabs", "label": "good", "source": _FE_GOOD_TABS,
        "reqs": [("FR1", "点击标签切换显示对应内容")],
        "files": {"src/App.tsx": _FE_GOOD_TABS},
        "runtime": {"ran": True, "console_errors": [], "page_errors": [],
                    "interactions": {"total": 3, "clicked": 3, "filled": 0, "dead_controls": []}},
    },
    {
        "name": "bad-remote-font", "label": "bad",
        "source": "// ===== src/index.css =====\n" + _FE_BAD_REMOTE_CSS
                  + "\n// ===== src/App.tsx =====\n" + _FE_BAD_REMOTE_APP,
        "reqs": [("FR1", "展示首页")],
        "files": {"src/index.css": _FE_BAD_REMOTE_CSS, "src/App.tsx": _FE_BAD_REMOTE_APP},
        "runtime": {"ran": True, "console_errors": [], "page_errors": [], "interactions": {}},
    },
    {
        "name": "bad-lockfile-mirror", "label": "bad",
        "source": "// ===== src/App.tsx =====\n" + _FE_BAD_LOCKFILE_APP
                  + "\n// ===== package-lock.json =====\n" + _FE_BAD_LOCKFILE_LOCK,
        "reqs": [("FR1", "展示首页")],
        "files": {"src/App.tsx": _FE_BAD_LOCKFILE_APP, "package-lock.json": _FE_BAD_LOCKFILE_LOCK},
        "runtime": {"ran": True, "console_errors": [], "page_errors": [], "interactions": {}},
    },
    {
        "name": "bad-runtime-crash", "label": "bad", "source": _FE_BAD_CRASH,
        "reqs": [("FR1", "首页正常渲染")],
        "files": {"src/App.tsx": _FE_BAD_CRASH},
        "runtime": {"ran": True,
                    "console_errors": ["TypeError: Cannot read properties of null (reading 'profile')"],
                    "page_errors": ["TypeError: Cannot read properties of null (reading 'profile')"],
                    "interactions": {}},
    },
    {
        "name": "bad-no-submit-form", "label": "bad", "source": _FE_BAD_NOSUBMIT,
        "reqs": [("FR1", "用户提交邮箱后完成订阅")],
        "files": {"src/App.tsx": _FE_BAD_NOSUBMIT},
        "runtime": {"ran": True, "console_errors": [], "page_errors": [],
                    "interactions": {"total": 1, "clicked": 1, "filled": 1, "dead_controls": ["订阅"]}},
    },
]

# ============================ BACKEND FIXTURES =============================
# A deployable "good" backend: root-mounted routes, unified envelope, /health,
# input validation, error handler, empty-store self-seed, AND a Dockerfile +
# package.json — so it clears the backend acceptance bar (the BE critic's
# robustness/deployability lens explicitly checks Dockerfile / /health / seed).
_BE_GOOD_SERVER = """const express = require('express')
const app = express()
app.use(express.json())
const ok = (data) => ({ success: true, data, message: null })
const fail = (res, code, error, message) => res.status(code).json({ success: false, error, message })
let seq = 1
const todos = []
if (!todos.length) todos.push({ id: seq++, title: '示例待办' })  // 空库自播种
app.get('/health', (req, res) => res.json({ status: 'ok' }))
app.get('/todos', (req, res) => res.json(ok(todos)))
app.post('/todos', (req, res) => {
  const title = String((req.body && req.body.title) || '').trim()
  if (!title) return fail(res, 400, 'VALIDATION_ERROR', 'title 不能为空')
  const todo = { id: seq++, title }
  todos.push(todo)
  res.status(201).json(ok(todo))
})
app.use((e, req, res, next) => fail(res, 500, 'SERVER_ERROR', 'internal error'))
app.listen(process.env.PORT || 8080, () => console.log('listening'))
"""

_BE_GOOD_DOCKERFILE = """FROM node:20-alpine
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY . .
EXPOSE 8080
CMD ["node", "server.js"]
"""

_BE_GOOD_PKG = (
    '{ "name": "todo-api", "version": "1.0.0", "main": "server.js", '
    '"dependencies": { "express": "^4.19.2" } }\n'
)

_BE_GOOD_FILES = {
    "server.js": _BE_GOOD_SERVER,
    "Dockerfile": _BE_GOOD_DOCKERFILE,
    "package.json": _BE_GOOD_PKG,
}
_BE_GOOD = "".join(f"// ===== {p} =====\n{c}\n" for p, c in _BE_GOOD_FILES.items())


def _be_bundle(server_src: str):
    """Wrap a server source into a deployable bundle (+ Dockerfile + package.json) so a
    'good' fixture clears the deployability lens and a 'bad' fixture's ONLY defect is the
    one under test. Returns (files_dict, source_digest)."""
    files = {"server.js": server_src, "Dockerfile": _BE_GOOD_DOCKERFILE, "package.json": _BE_GOOD_PKG}
    return files, "".join(f"// ===== {p} =====\n{c}\n" for p, c in files.items())


# --- good: auth backend (login -> data.token, Bearer-protected route) ---
_BE_AUTH_SERVER = """const express = require('express')
const app = express()
app.use(express.json())
const ok = (data) => ({ success: true, data, message: null })
const fail = (res, code, error, message) => res.status(code).json({ success: false, error, message })
const users = [{ id: 1, email: 'demo@example.com', password: 'Demo1234!', name: 'Demo' }]
const tokens = {}
let seq = 1
app.get('/health', (req, res) => res.json({ status: 'ok' }))
app.post('/auth/login', (req, res) => {
  const { email, password } = req.body || {}
  const u = users.find((x) => x.email === email && x.password === password)
  if (!u) return fail(res, 401, 'UNAUTHORIZED', '邮箱或密码错误')
  const token = 'tok-' + (seq++)
  tokens[token] = u.id
  res.json(ok({ token, user: { id: u.id, name: u.name } }))
})
function auth(req, res, next) {
  const h = req.headers.authorization || ''
  const t = h.startsWith('Bearer ') ? h.slice(7) : ''
  if (!tokens[t]) return fail(res, 401, 'UNAUTHORIZED', '未登录')
  req.userId = tokens[t]
  next()
}
app.get('/me', auth, (req, res) => {
  const u = users.find((x) => x.id === req.userId)
  res.json(ok({ id: u.id, name: u.name, email: u.email }))  // 不回传 password
})
app.use((e, req, res, next) => fail(res, 500, 'SERVER_ERROR', 'internal'))
app.listen(process.env.PORT || 8080)
"""
_BE_AUTH_CONTRACT = """统一信封 {success, data, message}。端点:
- POST /auth/login {email,password} -> {success, data: {token, user: {id,name}}}
- GET  /me  (Authorization: Bearer <token>) -> {success, data: {id,name,email}}
- GET  /health -> {status: "ok"}
登录 token 放在 data.token;受保护端点用 Authorization: Bearer 校验;所有路由挂根路径。"""
_BE_AUTH_REQ = """# 需求
- FR1: 登录(POST /auth/login),成功返回 data.token。
- FR2: 凭 token 获取当前用户(GET /me)。
- NFR1: 暴露 /health。"""
_BE_AUTH_FILES, _BE_AUTH_SRC = _be_bundle(_BE_AUTH_SERVER)

# --- bad: GET /todos returns a raw array, not the unified envelope ---
_BE_BAD_NOENV_SERVER = """const express = require('express')
const app = express()
app.use(express.json())
let seq = 1
const todos = [{ id: seq++, title: '示例待办' }]
app.get('/health', (req, res) => res.json({ status: 'ok' }))
app.get('/todos', (req, res) => res.json(todos))   // 违约:未用统一信封 {success,data,message}
app.post('/todos', (req, res) => { const t = { id: seq++, title: req.body.title }; todos.push(t); res.json(t) })
app.listen(process.env.PORT || 8080)
"""
_BE_BAD_NOENV_FILES, _BE_BAD_NOENV_SRC = _be_bundle(_BE_BAD_NOENV_SERVER)

# --- bad: no /health endpoint (deploy health-check would fail) ---
_BE_BAD_NOHEALTH_SERVER = """const express = require('express')
const app = express()
app.use(express.json())
const ok = (d) => ({ success: true, data: d, message: null })
let seq = 1
const todos = [{ id: seq++, title: '示例待办' }]
// 缺少 /health 健康检查端点
app.get('/todos', (req, res) => res.json(ok(todos)))
app.post('/todos', (req, res) => { const t = { id: seq++, title: String(req.body.title || '') }; todos.push(t); res.status(201).json(ok(t)) })
app.listen(process.env.PORT || 8080)
"""
_BE_BAD_NOHEALTH_FILES, _BE_BAD_NOHEALTH_SRC = _be_bundle(_BE_BAD_NOHEALTH_SERVER)

# --- bad: GET /users leaks the password field (security lens reliably blocks this) ---
_BE_BAD_LEAK_SERVER = """const express = require('express')
const app = express()
app.use(express.json())
const ok = (d) => ({ success: true, data: d, message: null })
const users = [{ id: 1, email: 'demo@example.com', password: 'Demo1234!', name: 'Demo' }]
app.get('/health', (req, res) => res.json({ status: 'ok' }))
app.get('/users', (req, res) => res.json(ok(users)))  // 泄露:响应直接返回含 password 的用户对象
app.listen(process.env.PORT || 8080)
"""
_BE_LEAK_CONTRACT = """统一信封 {success, data, message}。端点:
- GET /users  -> {success, data: [{id, name, email}]}   (绝不能包含 password 等敏感字段)
- GET /health -> {status: "ok"}
路由挂根路径。"""
_BE_LEAK_REQ = "# 需求\n- FR1: GET /users 列出用户(仅 id/name/email,不得泄露 password)。\n- NFR1: 暴露 /health。"
_BE_BAD_LEAK_FILES, _BE_BAD_LEAK_SRC = _be_bundle(_BE_BAD_LEAK_SERVER)

# --- bad: login returns the token at the TOP LEVEL, not in data.token (contract violation) ---
_BE_BAD_LOGIN_SERVER = """const express = require('express')
const app = express()
app.use(express.json())
const users = [{ id: 1, email: 'demo@example.com', password: 'Demo1234!', name: 'Demo' }]
let seq = 1
app.get('/health', (req, res) => res.json({ status: 'ok' }))
app.post('/auth/login', (req, res) => {
  const { email, password } = req.body || {}
  const u = users.find((x) => x.email === email && x.password === password)
  if (!u) return res.status(401).json({ success: false, error: 'UNAUTHORIZED', message: '错误' })
  const token = 'tok-' + (seq++)
  res.json({ success: true, token, data: { user: { id: u.id, name: u.name } } })  // 违约:token 不在 data.token
})
app.listen(process.env.PORT || 8080)
"""
_BE_BAD_LOGIN_FILES, _BE_BAD_LOGIN_SRC = _be_bundle(_BE_BAD_LOGIN_SERVER)

_BE_BAD_API_PREFIX = """// ===== server.js =====
const express = require('express')
const app = express()
app.use(express.json())
const ok = (data) => ({ success: true, data, message: null })
const todos = []
app.get('/health', (req, res) => res.json({ status: 'ok' }))
app.get('/api/todos', (req, res) => res.json(ok(todos)))
app.post('/api/todos', (req, res) => { todos.push(req.body); res.json(ok(req.body)) })
app.listen(process.env.PORT || 8080)
"""

_BE_BAD_MISSING = """// ===== server.js =====
const express = require('express')
const app = express()
app.use(express.json())
const ok = (data) => ({ success: true, data, message: null })
const todos = []
app.get('/health', (req, res) => res.json({ status: 'ok' }))
app.post('/todos', (req, res) => { todos.push(req.body); res.json(ok(req.body)) })
// NOTE: no GET /todos — listing is unimplemented.
app.listen(process.env.PORT || 8080)
"""

_BE_CONTRACT = """统一信封 {success, data, message}。端点:
- GET  /todos        -> {success, data: [{id, title}]}
- POST /todos {title} -> {success, data: {id, title}}
- GET  /health       -> {status: "ok"}
所有路由挂在根路径(反代会剥掉 /app/<pid>/api 前缀)。"""

_BE_REQ_DOC = """# 需求
- FR1: 列出全部待办(GET /todos)。
- FR2: 新增待办(POST /todos)。
- NFR1: 暴露 /health 健康检查。
- M1: 数据用内存数组即可(演示)。"""

BE_FIXTURES = [
    {
        "name": "good-be-crud", "label": "good", "source": _BE_GOOD,
        "reqs": [("FR1", "GET /todos 列出待办"), ("FR2", "POST /todos 新增待办")],
        "files": _BE_GOOD_FILES,
        "contract": _BE_CONTRACT, "requirements_doc": _BE_REQ_DOC,
    },
    {
        "name": "bad-be-api-prefix", "label": "bad", "source": _BE_BAD_API_PREFIX,
        "reqs": [("FR1", "GET /todos 列出待办"), ("FR2", "POST /todos 新增待办")],
        "files": {"server.js": _BE_BAD_API_PREFIX},
        "contract": _BE_CONTRACT, "requirements_doc": _BE_REQ_DOC,
    },
    {
        "name": "bad-be-missing-endpoint", "label": "bad", "source": _BE_BAD_MISSING,
        "reqs": [("FR1", "GET /todos 列出待办"), ("FR2", "POST /todos 新增待办")],
        "files": {"server.js": _BE_BAD_MISSING},
        "contract": _BE_CONTRACT, "requirements_doc": _BE_REQ_DOC,
    },
    {
        "name": "good-be-auth", "label": "good", "source": _BE_AUTH_SRC,
        "reqs": [("FR1", "POST /auth/login 登录返回 data.token"), ("FR2", "GET /me 凭 token 返回当前用户")],
        "files": _BE_AUTH_FILES,
        "contract": _BE_AUTH_CONTRACT, "requirements_doc": _BE_AUTH_REQ,
    },
    {
        "name": "bad-be-no-envelope", "label": "bad", "source": _BE_BAD_NOENV_SRC,
        "reqs": [("FR1", "GET /todos 列出待办"), ("FR2", "POST /todos 新增待办")],
        "files": _BE_BAD_NOENV_FILES,
        "contract": _BE_CONTRACT, "requirements_doc": _BE_REQ_DOC,
    },
    {
        "name": "bad-be-no-health", "label": "bad", "source": _BE_BAD_NOHEALTH_SRC,
        "reqs": [("FR1", "GET /todos 列出待办"), ("FR2", "POST /todos 新增待办")],
        "files": _BE_BAD_NOHEALTH_FILES,
        "contract": _BE_CONTRACT, "requirements_doc": _BE_REQ_DOC,
    },
    {
        "name": "bad-be-password-leak", "label": "bad", "source": _BE_BAD_LEAK_SRC,
        "reqs": [("FR1", "GET /users 列出用户(不含敏感字段)")],
        "files": _BE_BAD_LEAK_FILES,
        "contract": _BE_LEAK_CONTRACT, "requirements_doc": _BE_LEAK_REQ,
    },
    {
        "name": "bad-be-login-token-shape", "label": "bad", "source": _BE_BAD_LOGIN_SRC,
        "reqs": [("FR1", "POST /auth/login 登录,token 必须在 data.token")],
        "files": _BE_BAD_LOGIN_FILES,
        "contract": _BE_AUTH_CONTRACT, "requirements_doc": _BE_AUTH_REQ,
    },
]


# ----------------------------- evaluation -----------------------------------
def _features(reqs):
    return [{"id": rid, "category": "functional", "description": stmt, "passes": False, "note": ""}
            for rid, stmt in reqs]


def _registry(reqs):
    return "\n".join(f"- [{rid}] {stmt}" for rid, stmt in reqs)


def _run_panel(review_one, lenses, n):
    """N independent reviews (rotating lenses) -> majority consensus, mirroring the
    workflows' ``_review_panel``. ``n == 1`` is a single un-lensed review."""
    from backend.services.agent.workflows import _verify_support

    outs = []
    for i in range(max(1, n)):
        lens = lenses[i % len(lenses)] if (n > 1 and lenses) else ""
        r = review_one(lens)
        if r:
            outs.append(r)
    return _verify_support.aggregate_reviews(outs)


def evaluate_fe(fx: dict, panel: int = 1):
    """Run the real frontend evaluator + the real verify gate on one fixture."""
    from backend.services.agent.workflows import _verify_support
    from backend.services.code.frontend_project_service import get_frontend_project_service

    svc = get_frontend_project_service()
    feats = _features(fx["reqs"])
    violations = house_rules.check_frontend(fx["files"])
    hr_report = house_rules.render_report(violations)
    rt = fx.get("runtime")
    rt_report = _verify_support.render_runtime_report(rt)

    def review_one(lens):
        return svc.review_project(
            source_digest=fx["source"], requirements_registry=_registry(fx["reqs"]),
            features_block=_verify_support.render_features_block(feats),
            house_rules_report=hr_report, runtime_report=rt_report, extra_directive=lens,
        )

    review = _run_panel(review_one, _verify_support.REVIEW_LENSES_FRONTEND, panel)
    _feats, _stats = _verify_support.apply_feature_results(feats, (review or {}).get("feature_results"))
    verification = _verify_support.Verification(
        house_rule_errors=house_rules.errors(violations),
        house_rule_warnings=house_rules.warnings(violations),
        runtime_errors=_verify_support.runtime_errors(rt),
        runtime_check=rt, review=review, features=_feats, feature_stats=_stats,
    )
    return review, verification


def evaluate_be(fx: dict, panel: int = 1):
    """Run the real backend evaluator + the real verify gate on one fixture."""
    from backend.services.agent.workflows import _verify_support
    from backend.services.code.backend_project_service import get_backend_project_service

    svc = get_backend_project_service()
    feats = _features(fx["reqs"])
    violations = house_rules.check_backend(fx["files"])
    hr_report = house_rules.render_report(violations)

    def review_one(lens):
        return svc.review_project(
            source_digest=fx["source"], contract_summary=fx["contract"],
            requirements_doc=fx.get("requirements_doc", ""),
            development_flow=fx.get("development_flow", ""),
            features_block=_verify_support.render_features_block(feats),
            house_rules_report=hr_report, extra_directive=lens,
        )

    review = _run_panel(review_one, _verify_support.REVIEW_LENSES_BACKEND, panel)
    _feats, _stats = _verify_support.apply_feature_results(feats, (review or {}).get("feature_results"))
    verification = _verify_support.Verification(
        house_rule_errors=house_rules.errors(violations),
        house_rule_warnings=house_rules.warnings(violations),
        runtime_errors=[], review=review, features=_feats, feature_stats=_stats,
    )
    return review, verification


def run_eval(panel: int = 1, persist: bool = False, lanes=("frontend", "backend"), user_id=None) -> list[dict]:
    """Evaluate every fixture; return one result dict per fixture.

    ``blocked`` is the REAL end-to-end gate (``Verification.blocking``) — house-rule
    errors + runtime errors + the evaluator's blocking_issues — so the eval measures
    exactly what ships, not a re-implementation. ``correct`` = blocked == expected.
    """
    from backend.services.agent.workflows import _verify_support

    plan = []
    if "frontend" in lanes:
        plan += [("frontend", fx) for fx in FE_FIXTURES]
    if "backend" in lanes:
        plan += [("backend", fx) for fx in BE_FIXTURES]

    out = []
    for lane, fx in plan:
        review, verification = (evaluate_fe if lane == "frontend" else evaluate_be)(fx, panel)
        blocked = bool(verification.blocking)
        expect_block = fx["label"] == "bad"
        correct = (review is not None) and (blocked == expect_block)
        rec = {
            "name": fx["name"], "lane": lane, "label": fx["label"],
            "blocked": blocked, "expect_block": expect_block, "correct": correct,
            "verdict": (review or {}).get("verdict"),
            "weighted_score": _verify_support.weighted_score_of(review),
            "scores": (review or {}).get("scores"),
            "review": review,
        }
        out.append(rec)
        if persist:
            from backend.services.code.quality_metrics import record_eval_sample

            record_eval_sample(
                lane=lane, fixture_name=fx["name"], review=review, blocked=blocked,
                expected_block=expect_block, correct=correct, user_id=user_id,
            )
    return out


# ------------------------------- reporting ----------------------------------
def _cluster_scores(results: list[dict], label: str) -> list[float]:
    return sorted(r["weighted_score"] for r in results
                  if r["label"] == label and isinstance(r.get("weighted_score"), (int, float)))


def _functionality(results: list[dict], label: str) -> list[float]:
    out = []
    for r in results:
        if r["label"] != label:
            continue
        f = (r.get("scores") or {}).get("functionality")
        if isinstance(f, (int, float)):
            out.append(float(f))
    return sorted(out)


def _fmt(vals: list[float]) -> str:
    if not vals:
        return "n/a"
    return f"min={vals[0]:.1f} mean={sum(vals) / len(vals):.2f} max={vals[-1]:.1f}"


def print_report(results: list[dict], panel: int) -> int:
    # A None review = the judge model was UNAVAILABLE (402 / timeout / unconfigured),
    # NOT a judgment. Segregate those so a model outage can't masquerade as a quality
    # collapse, and score discrimination only over fixtures the judge actually judged.
    judged = [r for r in results if r["review"] is not None]
    errored = [r for r in results if r["review"] is None]
    correct = sum(1 for r in judged if r["blocked"] == r["expect_block"])

    print(f"\n=== Evaluator eval (panel={panel}) ===")
    for r in results:
        status = "ERR" if r["review"] is None else ("✓" if r["blocked"] == r["expect_block"] else "✗")
        ws = r["weighted_score"]
        ws_s = f"{ws:.2f}" if isinstance(ws, (int, float)) else "  - "
        print(f"  {status:3s} [{r['lane'][:2]}] {r['name']:22s} label={r['label']:4s} "
              f"verdict={str(r['verdict']):9s} score={ws_s} blocked={r['blocked']} (expect {r['expect_block']})")

    if errored:
        held = sum(1 for r in errored if r["blocked"] == r["expect_block"])
        print(f"\n⚠ {len(errored)}/{len(results)} fixtures ERRORED — the judge model returned nothing "
              "(insufficient balance / timeout / unconfigured text lane). Those rows measure NOTHING "
              "about judgment quality; fix the text lane and re-run.")
        print(f"  (deterministic gate still matched expectation on {held}/{len(errored)} of them via "
              "house-rules/runtime — defense-in-depth held while the judge was down.)")

    print(f"\nDiscrimination (judged only): {correct}/{len(judged)} correct"
          + (f"  [+{len(errored)} errored, not counted]" if errored else ""))

    # Diagnosability: why did any GOOD fixture get blocked? (mislabeled fixture vs over-strict judge)
    for r in judged:
        if r["label"] == "good" and r["blocked"]:
            issues = (r["review"] or {}).get("blocking_issues") or []
            print(f"  ! good '{r['name']}' was BLOCKED → blocking_issues: {issues[:3]}")

    good_ws, bad_ws = _cluster_scores(judged, "good"), _cluster_scores(judged, "bad")
    good_fn, bad_fn = _functionality(judged, "good"), _functionality(judged, "bad")
    print("\n--- score distribution (judged) ---")
    print(f"  overall weighted_score — good: {_fmt(good_ws)} | bad: {_fmt(bad_ws)}")
    print(f"  functionality dim      — good: {_fmt(good_fn)} | bad: {_fmt(bad_fn)}")

    def _suggest(good, bad, env):
        # A clean gap (good-min > bad-max) => a floor in the gap is a usable gate.
        if not good or not bad:
            return f"  {env}: insufficient data"
        if good[0] > bad[-1]:
            mid = round((good[0] + bad[-1]) / 2, 1)
            return f"  [OK] {env} ~= {mid}  (clean gap: bad <= {bad[-1]:.1f} || good >= {good[0]:.1f})"
        return (f"  [!!] {env}: clusters OVERLAP (good-min {good[0]:.1f} <= bad-max {bad[-1]:.1f}) "
                "-- NOT a usable threshold on this set")

    print("\n--- suggested gate (pick the dimension with a clean gap) ---")
    print(_suggest(good_fn, bad_fn, "CODE_QUALITY_MIN_FUNCTIONALITY"))
    print(_suggest(good_ws, bad_ws, "CODE_QUALITY_MIN_SCORE"))
    print("  note: functionality separates working/broken cleanly; the overall score blends in "
          "design/craft and routinely overlaps -- prefer the functionality floor.")
    # Non-zero on any error OR any judged mismatch — an outage is NOT a pass.
    return 0 if (not errored and correct == len(judged)) else 1


# --------------------------- regression baseline ----------------------------
def _baseline_snapshot(results: list[dict], panel: int) -> dict:
    """Capture the discrimination + functionality gap a baseline must not regress below.
    Only ``judged`` fixtures (review is not None) go in — an outage is not behavior."""
    judged = [r for r in results if r["review"] is not None]
    good_fn = _functionality(judged, "good")
    bad_fn = _functionality(judged, "bad")
    return {
        "version": 1,
        "panel": panel,
        "discrimination": {
            "correct": sum(1 for r in judged if r["correct"]),
            "judged": len(judged),
            "total": len(results),
        },
        "functionality_gap": {
            "good_min": good_fn[0] if good_fn else None,
            "bad_max": bad_fn[-1] if bad_fn else None,
            "clean": bool(good_fn and bad_fn and good_fn[0] > bad_fn[-1]),
        },
        "fixtures": {
            r["name"]: {
                "lane": r["lane"], "label": r["label"],
                "correct": bool(r["correct"]), "blocked": bool(r["blocked"]),
                "verdict": r["verdict"],
            }
            for r in judged
        },
    }


def write_baseline(path: str, results: list[dict], panel: int) -> int:
    snap = _baseline_snapshot(results, panel)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    d = snap["discrimination"]
    g = snap["functionality_gap"]
    print(f"\n✓ baseline written → {path}  "
          f"({d['correct']}/{d['judged']} judged correct, functionality gap clean={g['clean']})")
    return 0


def check_against_baseline(path: str, results: list[dict], panel: int) -> int:
    """Gate this run against a committed baseline. Non-zero ONLY on a real regression:
    a previously-correct fixture now misjudges, or the functionality clean-gap closes.
    Lane errors are tolerated (an outage is not a regression); a total outage -> exit 2."""
    p = Path(path)
    if not p.exists():
        print(f"✗ baseline not found: {path} — run `make eval-baseline` first.", file=sys.stderr)
        return 2
    base = json.loads(p.read_text(encoding="utf-8"))
    base_fx = base.get("fixtures", {})

    judged = {r["name"]: r for r in results if r["review"] is not None}
    errored = [r["name"] for r in results if r["review"] is None]

    print(f"\n=== regression check vs {path} (panel={panel}) ===")
    if not judged:
        print("✗ every fixture errored — text lane appears down; cannot assess regression.",
              file=sys.stderr)
        return 2

    regressions: list[str] = []
    # 1) every fixture that was correct in the baseline must still be correct.
    for name, b in base_fx.items():
        if not b.get("correct"):
            continue
        cur = judged.get(name)
        if cur is None:
            tag = "errored this run" if name in errored else "missing this run"
            print(f"  ~ '{name}' {tag} (baseline-correct) — skipped, not counted as regression")
            continue
        if not cur["correct"]:
            regressions.append(
                f"'{name}' regressed: baseline correct → now MISJUDGED "
                f"(blocked={cur['blocked']}, expected_block={cur['expect_block']})")

    # 2) a functionality clean-gap that existed must not close.
    base_gap = base.get("functionality_gap", {})
    if base_gap.get("clean"):
        jl = list(judged.values())
        good_fn, bad_fn = _functionality(jl, "good"), _functionality(jl, "bad")
        if good_fn and bad_fn and not (good_fn[0] > bad_fn[-1]):
            regressions.append(
                f"functionality clean-gap CLOSED: good-min {good_fn[0]:.1f} <= bad-max {bad_fn[-1]:.1f} "
                f"(baseline: good-min {base_gap.get('good_min')} > bad-max {base_gap.get('bad_max')})")

    new = [n for n in judged if n not in base_fx]
    if new:
        print(f"  + {len(new)} new fixture(s) not in baseline (informational): {', '.join(new)}")
    if errored:
        print(f"  ⚠ {len(errored)} errored (tolerated): {', '.join(errored)}")

    if regressions:
        print(f"  ✗ {len(regressions)} REGRESSION(S):")
        for r in regressions:
            print(f"      - {r}")
        return 1
    print(f"  ✓ no regression — {len(judged)} judged, all baseline-correct fixtures still correct.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline eval for the project evaluator.")
    parser.add_argument("--panel", type=int, default=int(os.getenv("CODE_REVIEW_PANEL", "1") or 1),
                        help="reviewers in the consensus panel (default: CODE_REVIEW_PANEL or 1)")
    parser.add_argument("--lane", choices=["frontend", "backend", "both"], default="both")
    parser.add_argument("--persist", action="store_true",
                        help="also store each result as a CodeQualitySample (kind=eval)")
    parser.add_argument("--baseline", nargs="?", const=str(BASELINE_DEFAULT), default=None,
                        metavar="PATH",
                        help="write current results as the regression baseline (default: eval/baseline.json)")
    parser.add_argument("--check", nargs="?", const=str(BASELINE_DEFAULT), default=None,
                        metavar="PATH",
                        help="gate this run vs a baseline; non-zero on regression (default: eval/baseline.json)")
    args = parser.parse_args(argv)

    from backend.services.ai import get_text_provider

    if not (get_text_provider() and get_text_provider().is_configured()):
        print("✗ text provider not configured (AI_TEXT_* / etc.) — cannot run eval.", file=sys.stderr)
        return 2

    lanes = ("frontend", "backend") if args.lane == "both" else (args.lane,)
    app_ctx = None
    if args.persist:  # only the DB write needs an app context
        from backend.app import create_app

        app = create_app()
        app_ctx = app.app_context()
        app_ctx.push()
    try:
        results = run_eval(panel=args.panel, persist=args.persist, lanes=lanes)
    finally:
        if app_ctx is not None:
            app_ctx.pop()

    rc = print_report(results, args.panel)
    if args.baseline:  # snapshotting current behavior is never itself a gate
        return write_baseline(args.baseline, results, args.panel)
    if args.check:  # the regression gate (lane errors tolerated)
        return check_against_baseline(args.check, results, args.panel)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
