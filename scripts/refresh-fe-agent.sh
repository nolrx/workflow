#!/usr/bin/env bash
#
# refresh-fe-agent.sh — 一键让「前端多文件项目 + Codex 资源生成」改动生效。
#
# 做两件事:
#   1) 重建 fe-agent 沙箱镜像(装入 Claude Code + Codex CLI),并校验 codex 已在镜像内;
#   2) 把本地最新的 frontend_project_prompt.txt 写入运行时的 Mongo prompt 库
#      (走 admin API 的 PUT —— 比 reset 更稳:reset 恢复的是后端进程"导入时"读到的
#       旧默认值;PUT 直接写入当前 .txt 内容,并由后端 invalidate 缓存,立即生效)。
#
# 用法:
#   bash scripts/refresh-fe-agent.sh
#   ADMIN_EMAIL=a@b.com ADMIN_PASSWORD=xxx bash scripts/refresh-fe-agent.sh
#   USE_COMPOSE=1 bash scripts/refresh-fe-agent.sh        # 用 docker compose 构建
#   SKIP_BUILD=1 bash scripts/refresh-fe-agent.sh          # 只更新 prompt
#   SKIP_PROMPT=1 bash scripts/refresh-fe-agent.sh         # 只重建镜像
#
# 可用环境变量(均有默认值):
#   API_BASE        默认 http://localhost:5001/api
#   FE_AGENT_IMAGE  默认 fe-agent:latest
#   ADMIN_EMAIL / ADMIN_PASSWORD   管理员账号(留空则交互式询问)
#   PROMPT_KEY      默认 code/frontend_project_prompt.txt
#   PROMPT_FILE     默认 backend/prompts/code/frontend_project_prompt.txt
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

API_BASE="${API_BASE:-http://localhost:5001/api}"
FE_AGENT_IMAGE="${FE_AGENT_IMAGE:-fe-agent:latest}"
PROMPT_KEY="${PROMPT_KEY:-code/frontend_project_prompt.txt}"
PROMPT_FILE="${PROMPT_FILE:-$REPO_ROOT/backend/prompts/code/frontend_project_prompt.txt}"
SKIP_BUILD="${SKIP_BUILD:-0}"
SKIP_PROMPT="${SKIP_PROMPT:-0}"
USE_COMPOSE="${USE_COMPOSE:-0}"

# crude flag parsing (env vars are the primary interface)
for arg in "$@"; do
  case "$arg" in
    --skip-build)  SKIP_BUILD=1 ;;
    --skip-prompt) SKIP_PROMPT=1 ;;
    --compose)     USE_COMPOSE=1 ;;
    -h|--help)     sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg"; exit 2 ;;
  esac
done

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m   ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m   ! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m   ✗ %s\033[0m\n' "$*" >&2; exit 1; }

command -v docker  >/dev/null 2>&1 || die "未找到 docker"
command -v python3 >/dev/null 2>&1 || die "未找到 python3"

# ---------------------------------------------------------------------------
# 1) 重建 fe-agent 镜像并校验 codex
# ---------------------------------------------------------------------------
if [ "$SKIP_BUILD" = "1" ]; then
  step "跳过镜像重建 (SKIP_BUILD=1)"
else
  step "重建 fe-agent 镜像 ($FE_AGENT_IMAGE) — 装入 Claude Code + Codex CLI"
  if [ "$USE_COMPOSE" = "1" ]; then
    docker compose --profile setup build fe-agent
  else
    docker build -t "$FE_AGENT_IMAGE" "$REPO_ROOT/backend/docker/fe-agent"
  fi
  ok "镜像构建完成"

  step "校验镜像内的 codex / claude"
  if docker run --rm "$FE_AGENT_IMAGE" bash -lc 'codex --version && claude --version'; then
    ok "codex 与 claude 均已在镜像内"
  else
    die "镜像内未检测到 codex/claude —— 构建可能失败,请检查上面的构建日志"
  fi
fi

# ---------------------------------------------------------------------------
# 2) 把本地 .txt 写入运行时的 Mongo prompt 库(admin API)
# ---------------------------------------------------------------------------
if [ "$SKIP_PROMPT" = "1" ]; then
  step "跳过 prompt 更新 (SKIP_PROMPT=1)"
else
  [ -f "$PROMPT_FILE" ] || die "找不到 prompt 文件: $PROMPT_FILE"

  if [ -z "${ADMIN_EMAIL:-}" ]; then
    read -r -p "管理员邮箱 (ADMIN_EMAIL): " ADMIN_EMAIL
  fi
  if [ -z "${ADMIN_PASSWORD:-}" ]; then
    read -r -s -p "管理员密码 (ADMIN_PASSWORD): " ADMIN_PASSWORD; echo
  fi
  [ -n "$ADMIN_EMAIL" ] && [ -n "$ADMIN_PASSWORD" ] || die "需要管理员邮箱与密码"

  step "登录并把 $PROMPT_KEY 写入 Mongo (PUT $API_BASE/admin/prompts/$PROMPT_KEY)"
  API_BASE="$API_BASE" PROMPT_KEY="$PROMPT_KEY" PROMPT_FILE="$PROMPT_FILE" \
  ADMIN_EMAIL="$ADMIN_EMAIL" ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  python3 - <<'PY'
import json, os, sys, urllib.request, urllib.error

base = os.environ["API_BASE"].rstrip("/")
key  = os.environ["PROMPT_KEY"]
content = open(os.environ["PROMPT_FILE"], encoding="utf-8").read()

def call(method, path, payload=None, token=None):
    url = base + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            body = json.loads(body)
        except Exception:
            pass
        return e.code, body
    except urllib.error.URLError as e:
        print(f"   ✗ 连不上后端 {url}: {e.reason}", file=sys.stderr)
        print("     请确认后端在运行,或用 API_BASE 指定正确地址(默认 http://localhost:5001/api)。", file=sys.stderr)
        sys.exit(1)

# --- login ---
st, body = call("POST", "/auth/login",
                {"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]})
if st != 200 or not isinstance(body, dict) or "access_token" not in body:
    print(f"   ✗ 登录失败 (HTTP {st}): {body}", file=sys.stderr); sys.exit(1)
token = body["access_token"]
print("   ✓ 登录成功")

# --- PUT prompt ---
st, body = call("PUT", f"/admin/prompts/{key}", {"content": content}, token=token)
if st == 403:
    print("   ✗ 该账号不是管理员(role != admin),无法编辑 prompt。", file=sys.stderr); sys.exit(1)
if st == 404:
    print(f"   ✗ 未知 prompt key: {key}", file=sys.stderr); sys.exit(1)
if st == 503:
    print("   ! Mongo 不可用:无法写库。运行时会直接用 .txt 默认值——", file=sys.stderr)
    print("     若后端在你修改 .txt 之前已启动,请重启后端进程使其重新加载默认值。", file=sys.stderr)
    sys.exit(3)
if st != 200:
    print(f"   ✗ 写入失败 (HTTP {st}): {body}", file=sys.stderr); sys.exit(1)
print(f"   ✓ 已写入 Mongo({len(content)} 字符),后端缓存已失效,立即生效")

# --- verify ---
st, body = call("GET", f"/admin/prompts/{key}", token=token)
text = json.dumps(body, ensure_ascii=False)
if st == 200 and "gen-assets" in text:
    print("   ✓ 校验通过:线上 prompt 已包含 gen-assets 资源生成指令")
else:
    print(f"   ! 校验未通过(HTTP {st}):线上内容里没找到 'gen-assets',请手动确认", file=sys.stderr)
PY
  ok "prompt 更新完成"
fi

step "完成"
cat <<EOF
下一步:
  • 重新触发一次「前端多文件项目」生成。
  • 在运行时间线里看那条 asset_lane 播报:
      - "图片资源生成:gen-assets 调用 N 次,产出 M 张图片资源。"  → 成功
      - "未找到 Codex CLI / 未配置 OPENAI_API_KEY / gen-assets 调用 0 次"  → 按提示排查
  • 确认 .env 里配了 OPENAI_API_KEY(Codex 复用图像那把 key)。
EOF
