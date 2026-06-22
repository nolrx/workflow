"""
Frontend PROJECT build service (agentic, container-sandboxed).

Unlike ``frontend_build_service`` (one text-model call returning a single inline
index.html), this drives an autonomous coding CLI (Claude Code, headless) inside
a throwaway Docker container to produce a COMPLETE multi-file React + Vite + TS
project that the agent itself builds and self-checks.

Sandbox properties (validated by PoC):
  * runs as the non-root ``node`` user (the CLI refuses bypassPermissions as root);
  * the only host-visible write is the mounted ``/out`` dir — node_modules and the
    npm cache live only in the container fs and die with ``--rm``;
  * the deliverable copied to /out is source + built ``dist`` ONLY.

This deliberately steps OUTSIDE the capability-routed AI provider abstraction:
the CLI owns its own Anthropic backend, so this is an agent-EXECUTION lane, not a
single ``generate_text`` call. Token usage is reported back via the CLI's terminal
``result`` event so the caller can meter it into the credit system.
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from werkzeug.utils import secure_filename

from backend.services.prompts import prompt_store

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).parent.parent.parent / "prompts" / "code"

# Per-file ceiling when collecting the deliverable, so a runaway file can't blow
# up the DB / artifact storage.
_MAX_FILE_BYTES = 512_000

# Runs INSIDE the container. The agent's stream-json goes to stdout (streamed live
# to the host); in-container ``timeout`` guards every phase against a hang. Only
# source + built/synthesized dist are copied to the mounted /out.
#
# A single claude pass routinely fails to produce a clean build (it times out
# mid-write leaving a dangling import, or emits a type/resolution error). To make
# a previewable deliverable GUARANTEED, the build is a self-healing ladder:
#   1. npm run build (as generated)
#   2. one AI repair round  -- claude -p fed the build log, told to only fix
#   3. deterministic stub    -- node script back-fills every unresolved relative
#                               import (the dominant failure: a referenced file the
#                               agent never wrote) so resolution can't sink the build
#   4. vite build (no tsc)   -- bypass type-only errors and let the bundler emit
#   5. synthesized dist       -- a minimal static page, so dist is NEVER empty
# The reached rung is written to /out/degraded for the host to report.
_CONTAINER_SCRIPT = r"""
WORK=/tmp/work
mkdir -p "$WORK" && cd "$WORK"

CLAUDE_FLAGS="--output-format stream-json --verbose --permission-mode bypassPermissions --allowedTools Read Write Edit Bash"

# Emit a phase sentinel onto the JSON stream so the host timeline can narrate
# the recovery ladder (build phases write their output to /out logs, not stdout).
emit() { printf '%s\n' "{\"type\":\"fe_phase\",\"phase\":\"$1\"}"; }

# --- 1. Generate ---------------------------------------------------------
timeout "${FE_AGENT_TIMEOUT:-720}" claude -p "$(cat /out/prompt.txt)" $CLAUDE_FLAGS
echo "$?" > /out/claude_exit
mkdir -p /out/project

# The prompt asks the agent to initialize directly in $WORK, but autonomous
# coding agents sometimes create one extra project directory. Publish the first
# buildable-looking project root so a harmless nesting mistake is recoverable.
PROJECT_ROOT="$WORK"
if [ ! -f "$PROJECT_ROOT/package.json" ]; then
  PACKAGE_JSON="$(find "$WORK" -maxdepth 3 -name package.json -not -path '*/node_modules/*' -print -quit 2>/dev/null)"
  if [ -n "$PACKAGE_JSON" ]; then
    PROJECT_ROOT="$(dirname "$PACKAGE_JSON")"
  fi
fi
echo "$PROJECT_ROOT" > /out/project_root

# Write the deterministic repair + fallback tools (run only on the failure path).
cat > /tmp/repair.mjs <<'REPAIR_EOF'
import fs from 'node:fs';
import path from 'node:path';

const projectRoot = process.argv[2];
const srcDir = path.join(projectRoot, 'src');
const CODE_EXTS = ['.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs'];
const RES_EXTS = [...CODE_EXTS, '.json', '.css', '.scss', '.sass', '.less'];
const STYLE_EXTS = ['.css', '.scss', '.sass', '.less'];

function walk(dir) {
  let out = [];
  let entries = [];
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return out; }
  for (const e of entries) {
    if (e.name === 'node_modules' || e.name === '.git' || e.name === 'dist') continue;
    const p = path.join(dir, e.name);
    if (e.isDirectory()) out = out.concat(walk(p));
    else out.push(p);
  }
  return out;
}

function resolveSpec(fromFile, spec) {
  const base = path.resolve(path.dirname(fromFile), spec);
  const ext = path.extname(base);
  if (ext && RES_EXTS.includes(ext)) return fs.existsSync(base) ? base : null;
  for (const e of RES_EXTS) if (fs.existsSync(base + e)) return base + e;
  for (const e of RES_EXTS) if (fs.existsSync(path.join(base, 'index' + e))) return path.join(base, 'index' + e);
  return null;
}

function targetPathFor(fromFile, spec) {
  const base = path.resolve(path.dirname(fromFile), spec);
  if (path.extname(base)) return base;
  const isComponent = /^[A-Z]/.test(path.basename(base));
  return base + (isComponent ? '.tsx' : '.ts');
}

function clauseInfo(clause) {
  const info = { hasDefault: false, named: new Set() };
  if (!clause) return info;
  clause = clause.trim();
  const brace = clause.match(/\{([\s\S]*)\}/);
  const before = clause.split('{')[0].replace(/\*\s+as\s+\w+/, '').replace(/,/g, ' ').trim();
  if (before) info.hasDefault = true;
  if (brace) {
    for (const part of brace[1].split(',')) {
      let name = part.trim().split(/\s+as\s+/)[0].trim().replace(/^type\s+/, '');
      if (name && name !== 'type') info.named.add(name);
    }
  }
  return info;
}

function ident(name) {
  const s = name.replace(/[^A-Za-z0-9_$]/g, '_');
  return /^[0-9]/.test(s) ? '_' + s : (s || 'Placeholder');
}

const stmtRe = /import\s+([\s\S]*?)\s+from\s+['"]([^'"]+)['"]|import\s+['"]([^'"]+)['"]|export\s+(?:\*|\{[\s\S]*?\})\s+from\s+['"]([^'"]+)['"]/g;
const sources = walk(srcDir).filter((p) => CODE_EXTS.includes(path.extname(p)));
const targets = new Map();

for (const file of sources) {
  let code;
  try { code = fs.readFileSync(file, 'utf8'); } catch { continue; }
  let m;
  while ((m = stmtRe.exec(code))) {
    const spec = m[2] || m[3] || m[4];
    if (!spec || !(spec.startsWith('./') || spec.startsWith('../'))) continue;
    if (resolveSpec(file, spec)) continue;
    const tp = targetPathFor(file, spec);
    let entry = targets.get(tp);
    if (!entry) { entry = { isStyle: STYLE_EXTS.includes(path.extname(tp)), named: new Set(), hasDefault: false }; targets.set(tp, entry); }
    const info = clauseInfo(m[1]);
    if (info.hasDefault) entry.hasDefault = true;
    for (const n of info.named) entry.named.add(n);
  }
}

let created = 0;
for (const [tp, entry] of targets) {
  if (fs.existsSync(tp)) continue;
  fs.mkdirSync(path.dirname(tp), { recursive: true });
  if (entry.isStyle) {
    fs.writeFileSync(tp, '/* auto-generated placeholder stylesheet */\n');
    created++;
    continue;
  }
  const ext = path.extname(tp);
  const isTsx = ext === '.tsx' || ext === '.jsx';
  const id = ident(path.basename(tp, ext));
  const lines = ['// Auto-generated placeholder: original module missing before build.'];
  if (isTsx) {
    lines.push("import { createElement } from 'react';");
    lines.push(`function ${id}(_props: any) {`);
    lines.push(`  return createElement('div', { 'data-placeholder': '${id}', style: { padding: 16, opacity: 0.6 } }, '${id} (placeholder)');`);
    lines.push('}');
    lines.push(`export default ${id};`);
    for (const n of entry.named) { if (ident(n) !== id) lines.push(`export const ${ident(n)}: any = ${id};`); }
  } else {
    lines.push('const placeholder: any = {};');
    if (entry.hasDefault) lines.push('export default placeholder;');
    for (const n of entry.named) lines.push(`export const ${ident(n)}: any = placeholder;`);
    if (!entry.hasDefault && entry.named.size === 0) lines.push('export {};');
  }
  fs.writeFileSync(tp, lines.join('\n') + '\n');
  created++;
}
console.log(`stub-repair: created ${created} placeholder file(s)`);
REPAIR_EOF

cat > /tmp/fallback.mjs <<'FALLBACK_EOF'
import fs from 'node:fs';
import path from 'node:path';

const projectRoot = process.argv[2];
const buildLogPath = process.argv[3];
const distDir = path.join(projectRoot, 'dist');
fs.mkdirSync(distDir, { recursive: true });

let log = '';
try { log = fs.readFileSync(buildLogPath, 'utf8').slice(-3000); } catch {}
const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

const html = `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>前端工程预览(降级)</title>
<style>
  body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0f172a;color:#e2e8f0;display:flex;min-height:100vh;align-items:center;justify-content:center;padding:24px}
  .card{max-width:760px;background:#1e293b;border:1px solid #334155;border-radius:16px;padding:32px}
  h1{margin:0 0 12px;font-size:20px}
  p{line-height:1.7;color:#cbd5e1}
  code{background:#0f172a;padding:2px 6px;border-radius:6px}
  pre{background:#0f172a;border:1px solid #334155;border-radius:10px;padding:16px;overflow:auto;max-height:280px;font-size:12px;color:#fca5a5}
</style></head>
<body><div class="card">
  <h1>项目已生成,但自动构建未通过</h1>
  <p>已为你保留完整源码(可在右侧下载源码 zip)。自动构建在多轮修复(AI 修复 + 确定性补桩 + Vite 兜底)后仍未通过,故合成此降级预览页,以保证始终有可预览产物。</p>
  <p>请下载源码后本地运行 <code>npm install &amp;&amp; npm run build</code> 查看完整报错。下面是构建日志节选:</p>
  <pre>${esc(log) || '(无构建日志)'}</pre>
</div></body></html>`;

fs.writeFileSync(path.join(distDir, 'index.html'), html);
console.log('fallback dist written');
FALLBACK_EOF

DEGRADED=""
run_build() {
  ( cd "$PROJECT_ROOT" && timeout "${FE_AGENT_BUILD_TIMEOUT:-180}" npm run build ) > /out/npm_build.log 2>&1
  echo "$?" > /out/npm_build_exit
}
build_ok() { [ "$(cat /out/npm_build_exit 2>/dev/null)" = "0" ]; }

if [ -f "$PROJECT_ROOT/package.json" ]; then
  emit install
  ( cd "$PROJECT_ROOT" && timeout "${FE_AGENT_NPM_TIMEOUT:-240}" npm install --no-audit --no-fund ) > /out/npm_install.log 2>&1
  echo "$?" > /out/npm_install_exit

  # rung 1: build as generated
  emit build
  run_build

  # rung 2: one AI repair round, fed the build log
  if ! build_ok; then
    emit ai-repair
    REPAIR_PROMPT="$(cat /out/repair_prompt.txt)

# 构建报错(节选)
$(tail -c 6000 /out/npm_build.log 2>/dev/null)"
    ( cd "$PROJECT_ROOT" && timeout "${FE_AGENT_REPAIR_TIMEOUT:-300}" claude -p "$REPAIR_PROMPT" $CLAUDE_FLAGS )
    echo "$?" > /out/claude_repair_exit
    run_build
    build_ok && DEGRADED="ai-repair"
  fi

  # rung 3: deterministic stub of every unresolved relative import
  if ! build_ok; then
    emit stub
    node /tmp/repair.mjs "$PROJECT_ROOT" > /out/stub_repair.log 2>&1
    echo "$?" > /out/stub_repair_exit
    run_build
    build_ok && DEGRADED="stub"
  fi

  # rung 4: skip tsc, let Vite bundle whatever resolves
  if ! build_ok; then
    emit vite-only
    ( cd "$PROJECT_ROOT" && timeout "${FE_AGENT_BUILD_TIMEOUT:-180}" npx --no-install vite build ) > /out/npm_build.log 2>&1
    echo "$?" > /out/npm_build_exit
    build_ok && DEGRADED="vite-only"
  fi
fi

# rung 5 / final guarantee: never ship an empty dist.
if [ ! -f "$PROJECT_ROOT/dist/index.html" ]; then
  emit fallback
  node /tmp/fallback.mjs "$PROJECT_ROOT" /out/npm_build.log > /out/fallback.log 2>&1
  DEGRADED="fallback"
fi
echo "$DEGRADED" > /out/degraded

# --- Collect deliverable (source + built/synthesized dist) ---------------
if [ -d "$PROJECT_ROOT" ]; then
  (
    cd "$PROJECT_ROOT" && \
    tar \
      --exclude='./node_modules' \
      --exclude='./.git' \
      --exclude='./.cache' \
      --exclude='./.npm' \
      --exclude='./*.log' \
      -cf - .
  ) | (cd /out/project && tar -xf -)
fi
"""


class FrontendProjectService:
    """Generate a runnable multi-file frontend project via a sandboxed agent."""

    def __init__(self):
        self.image = os.getenv("FE_AGENT_IMAGE", "fe-agent:latest")
        self.docker = os.getenv("DOCKER_BIN", "docker")
        # Per-phase budgets handed to the in-container `timeout` guards. The
        # self-healing ladder (generate + repair + several builds) can run long,
        # so the host's hard ceiling is the SUM of phases plus margin, NOT a single
        # claude budget. The in-container timeouts are the real per-phase guard;
        # total_timeout only backstops a container that never exits.
        self.gen_timeout = int(os.getenv("FE_AGENT_TIMEOUT", "720"))
        self.repair_timeout = int(os.getenv("FE_AGENT_REPAIR_TIMEOUT", "300"))
        self.npm_timeout = int(os.getenv("FE_AGENT_NPM_TIMEOUT", "240"))
        self.build_timeout = int(os.getenv("FE_AGENT_BUILD_TIMEOUT", "180"))
        self.total_timeout = int(os.getenv("FE_AGENT_TOTAL_TIMEOUT", "2400"))

    # --- prompt assembly -----------------------------------------------------
    def _load_prompt(self, name: str) -> str:
        # Mongo-backed (admin-editable); falls back to the bundled default under
        # PROMPT_DIR when Mongo is unavailable.
        return prompt_store.get(f"code/{name}")

    @staticmethod
    def _fill(template: str, **values: str) -> str:
        out = template
        for key, value in values.items():
            out = out.replace(f"[[{key}]]", value if value is not None else "")
        return out

    def is_configured(self) -> bool:
        return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY"))

    # --- public API ----------------------------------------------------------
    # Total budget for the Figma design block injected into the prompt. Visual
    # detail comes from the rendered images (the agent Reads them on demand), so
    # the prompt only carries a compact per-frame IR summary — keeping it bounded
    # even when a whole file has many frames.
    _FIGMA_BLOCK_BUDGET = 18_000

    def _figma_design_block(
        self, frames: Optional[list]
    ) -> tuple[str, list[tuple[str, str]]]:
        """Build the [[FIGMA_DESIGN]] prompt text + the (dest, src) render copies.

        ``frames`` items: ``{name, ir_text, render_path}``. Each frame becomes one
        page/route; its render is referenced as ``design/<dest>.png`` (mounted at
        ``/out/design/<dest>.png`` in the container) for the agent to ``Read``.
        """
        if not frames:
            return "", []
        head = [
            "# Figma 设计稿(视觉真值 — 优先级高于上面的文字风格 / UI 基调)",
            f"本产品有 {len(frames)} 个画板,每个画板对应一个页面/路由。",
            "实现每个页面前,先用 Read 工具读取它的设计图(在 design/ 目录下)作为视觉真值,"
            "精确还原布局、配色、字号、间距与层级;下方 IR 给出精确数值(hex/字号/间距)。",
            "设计稿与文字风格/UI 基调冲突时,以设计稿为准。",
        ]
        sections: list[str] = []
        copies: list[tuple[str, str]] = []
        used = 0
        for index, frame in enumerate(frames):
            name = frame.get("name") or f"frame{index + 1}"
            safe = secure_filename(name) or f"frame{index + 1}"
            dest = f"{index + 1:02d}-{safe}.png"
            src = frame.get("render_path")
            has_img = bool(src and Path(src).exists())
            if has_img:
                copies.append((dest, str(src)))
            ir = (frame.get("ir_text") or "").strip()
            ir = ir[: max(0, self._FIGMA_BLOCK_BUDGET - used)]
            used += len(ir)
            part = [f"\n## 画板 {index + 1}:{name}"]
            part.append(
                f"设计图: design/{dest}(用 Read 工具查看)" if has_img else "(无渲染图,仅依据下方 IR)"
            )
            if ir:
                part.append(f"设计 IR:\n{ir}")
            sections.append("\n".join(part))
        return "\n".join(head) + "\n" + "\n".join(sections), copies

    def build_project(
        self,
        *,
        requirement: str,
        requirements_doc: str = "",
        development_flow: str = "",
        documents_digest: str = "",
        style_prompt: str = "",
        ui_baseline_prompt: str = "",
        context_ledger: str = "",
        figma_frames: Optional[list] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Run the containerized agent through its self-healing build ladder.

        Returns ``{success, degraded, degraded_reason, error, files, dist_files,
        summary, usage, cost_usd, workdir}``. The container always produces a
        previewable ``dist`` (a synthesized notice page in the worst case), so
        ``success`` is True whenever the agent wrote any source; ``degraded`` /
        ``degraded_reason`` report which recovery rung produced the dist. Never
        raises on agent/model failure; raises only on infrastructure errors
        (docker missing, container never exits).
        """
        if not self.is_configured():
            return self._empty("ANTHROPIC_API_KEY not configured")

        figma_block, figma_copies = self._figma_design_block(figma_frames)
        prompt = self._fill(
            self._load_prompt("frontend_project_prompt.txt"),
            CONTEXT_LEDGER=context_ledger or "",
            REQUIREMENT=requirement or "",
            REQUIREMENTS_DOC=requirements_doc or "",
            DEVELOPMENT_FLOW=development_flow or "",
            DOCUMENTS=documents_digest or "",
            STYLE_PROMPT=style_prompt or "",
            UI_BASELINE=ui_baseline_prompt or "",
            FIGMA_DESIGN=figma_block,
        )

        workdir = Path(tempfile.mkdtemp(prefix="fe-agent-"))
        # The container runs as the non-root `node` user (uid 1000) and writes
        # the deliverable to this mounted dir. mkdtemp is 0700 (host user only);
        # on real Linux the container uid couldn't write it (Docker Desktop for
        # Mac is permissive and hides this), so open it up explicitly.
        os.chmod(workdir, 0o777)
        (workdir / "prompt.txt").write_text(prompt, encoding="utf-8")
        # The AI repair rung reuses this prompt; the container appends the live
        # build log before invoking claude a second time.
        (workdir / "repair_prompt.txt").write_text(
            self._load_prompt("frontend_project_repair_prompt.txt"), encoding="utf-8"
        )
        # Drop the Figma render images where the container agent can Read them
        # (/out/design/*.png). World-readable so the non-root `node` uid can open
        # them on real Linux (Docker Desktop for Mac is permissive).
        if figma_copies:
            design_dir = workdir / "design"
            design_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(design_dir, 0o777)
            for dest, src in figma_copies:
                try:
                    shutil.copyfile(src, design_dir / dest)
                    os.chmod(design_dir / dest, 0o644)
                except OSError:
                    logger.warning("failed to stage figma render %s", src)
        api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")

        cmd = [
            self.docker, "run", "--rm", "--user", "node",
            "-e", "ANTHROPIC_API_KEY",
            "-e", f"FE_AGENT_TIMEOUT={self.gen_timeout}",
            "-e", f"FE_AGENT_REPAIR_TIMEOUT={self.repair_timeout}",
            "-e", f"FE_AGENT_NPM_TIMEOUT={self.npm_timeout}",
            "-e", f"FE_AGENT_BUILD_TIMEOUT={self.build_timeout}",
            "-v", f"{workdir}:/out",
            self.image, "bash", "-c", _CONTAINER_SCRIPT,
        ]
        env = dict(os.environ, ANTHROPIC_API_KEY=api_key or "")

        # The ladder runs claude up to twice (generate + repair); accumulate the
        # terminal `result` events so cost/usage metering reflects BOTH passes.
        result_events: list[dict] = []
        non_json_stdout: list[str] = []
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    non_json_stdout.append(line[:500])
                    non_json_stdout = non_json_stdout[-20:]
                    continue  # non-JSON noise on stdout — ignore
                if event.get("type") == "result":
                    result_events.append(event)
                if on_event:
                    try:
                        on_event(event)
                    except Exception:  # noqa: BLE001 - a bad callback must not kill the run
                        logger.exception("on_event callback raised")
                if is_cancelled and is_cancelled():
                    proc.kill()
                    return self._empty("cancelled")
            proc.wait(timeout=self.total_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("frontend project agent timed out")
        finally:
            stderr = proc.stderr.read() if proc.stderr else ""
            if stderr:
                logger.info("fe-agent stderr: %s", stderr[:2000])

        docker_exit = proc.returncode
        claude_exit = self._read_exit_code(workdir / "claude_exit")
        npm_install_exit = self._read_exit_code(workdir / "npm_install_exit")
        npm_build_exit = self._read_exit_code(workdir / "npm_build_exit")
        npm_install_log = self._read_text(workdir / "npm_install.log")
        npm_build_log = self._read_text(workdir / "npm_build.log")
        # Which recovery rung produced the dist: "" (clean first build), or one of
        # ai-repair / stub / vite-only / fallback. Anything non-empty is degraded;
        # "fallback" means the dist is a synthesized notice page, not a real build.
        degraded_reason = self._read_text(workdir / "degraded").strip() or None
        files = self._collect(workdir / "project", exclude_top="dist")
        dist_files = self._collect(workdir / "project" / "dist")

        # Sum cost/usage across every claude pass (generate + optional repair).
        summary, usage, cost, is_error = "", {}, 0.0, True
        if result_events:
            summary = next(
                (r.get("result") for r in result_events if isinstance(r.get("result"), str)),
                "",
            )
            cost = sum(float(r.get("total_cost_usd") or 0.0) for r in result_events)
            usage = self._merge_usage(result_events)
            is_error = bool(result_events[-1].get("is_error"))

        # The container always synthesizes a previewable dist, so a run is
        # publishable whenever the agent produced source. Hard-fail only when the
        # agent produced nothing at all (config/auth error) -> runtime refunds it.
        success = (docker_exit == 0) and bool(files) and bool(dist_files)
        degraded = success and bool(degraded_reason)
        return {
            "success": success,
            "degraded": degraded,
            "degraded_reason": degraded_reason if degraded else None,
            "error": None if success else self._format_failure(
                docker_exit=docker_exit,
                claude_exit=claude_exit,
                npm_install_exit=npm_install_exit,
                npm_build_exit=npm_build_exit,
                is_error=is_error,
                files=files,
                dist_files=dist_files,
                summary=summary,
                stderr=stderr,
                non_json_stdout=non_json_stdout,
                npm_install_log=npm_install_log,
                npm_build_log=npm_build_log,
            ),
            "files": files,
            "dist_files": dist_files,
            "summary": summary,
            "usage": usage,
            "cost_usd": cost,
            "workdir": str(workdir),
        }

    # --- helpers -------------------------------------------------------------
    @staticmethod
    def _empty(error: str) -> dict:
        return {
            "success": False, "degraded": False, "degraded_reason": None,
            "error": error, "files": {}, "dist_files": {},
            "summary": "", "usage": {}, "cost_usd": 0.0, "workdir": None,
        }

    @staticmethod
    def _merge_usage(events: list[dict]) -> dict:
        """Sum integer token counters across every claude `result` event."""
        merged: dict = {}
        for event in events:
            usage = event.get("usage") or {}
            for key, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    merged[key] = merged.get(key, 0) + value
                elif key not in merged:
                    merged[key] = value
        return merged

    @staticmethod
    def _read_exit_code(path: Path) -> int | None:
        try:
            raw = path.read_text(encoding="utf-8").strip()
            return int(raw) if raw else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    @staticmethod
    def _clip(text: str, limit: int = 1200) -> str:
        text = (text or "").strip()
        return text if len(text) <= limit else f"{text[:limit]}..."

    def _format_failure(
        self,
        *,
        docker_exit: int | None,
        claude_exit: int | None,
        npm_install_exit: int | None,
        npm_build_exit: int | None,
        is_error: bool,
        files: dict,
        dist_files: dict,
        summary: str,
        stderr: str,
        non_json_stdout: list[str],
        npm_install_log: str,
        npm_build_log: str,
    ) -> str:
        reasons: list[str] = []
        if docker_exit not in (0, None):
            reasons.append(f"container exited with code {docker_exit}")
        if claude_exit not in (0, None):
            reasons.append(f"claude exited with code {claude_exit}")
        if npm_install_exit not in (0, None):
            reasons.append(f"npm install exited with code {npm_install_exit}")
        if npm_build_exit not in (0, None):
            reasons.append(f"npm run build exited with code {npm_build_exit}")
        if npm_build_exit is None:
            reasons.append("npm run build did not run")
        if is_error:
            reasons.append("claude reported an error")
        if not files:
            reasons.append("no source files were published")
        if not dist_files:
            reasons.append("no built dist files were published")

        details: list[str] = []
        if summary:
            details.append(f"summary: {self._clip(summary)}")
        if stderr:
            details.append(f"stderr: {self._clip(stderr)}")
        if non_json_stdout:
            details.append(f"stdout: {self._clip(chr(10).join(non_json_stdout))}")
        if npm_build_log:
            details.append(f"build log: {self._clip(npm_build_log)}")
        elif npm_install_log:
            details.append(f"install log: {self._clip(npm_install_log)}")

        headline = "; ".join(reasons) or "agent did not produce a buildable project"
        return " | ".join([headline, *details])

    def _collect(self, root: Path, exclude_top: Optional[str] = None) -> dict:
        """Return ``{relative_path: text}`` for text files under ``root``."""
        out: dict[str, str] = {}
        if not root.exists():
            return out
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if exclude_top and rel.parts and rel.parts[0] == exclude_top:
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                out[str(rel)] = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                out[str(rel)] = ""  # binary/unreadable — keep the path as a marker
        return out


_service_instance: Optional[FrontendProjectService] = None


def get_frontend_project_service() -> FrontendProjectService:
    global _service_instance
    if _service_instance is None:
        _service_instance = FrontendProjectService()
    return _service_instance
