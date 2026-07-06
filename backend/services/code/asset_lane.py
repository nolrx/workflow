"""
Shared asset-generation lane (image-assets skill -> Codex -> image model).

ONE authoritative copy of the in-container asset tooling used by BOTH the
one-shot frontend project generation (fe-agent throwaway container, state under
``/out``) and Dev Mode (long-running dev container, state under
``/tmp/dev-assets``) — so the two never drift. ``render_bootstrap`` emits the
shell fragment that installs ``genimage.mjs`` + ``gen-assets`` + the
``image-assets`` skill and writes the diagnostics files; the pure helpers
normalize/validate ``resource_spec.outputs`` for the P1 planner and the P2
per-task output verification.

Comments in English to match the Code/core convention.
"""
import os
import posixpath

# Image-lane env the container needs (passed through docker -e when set).
IMAGE_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_IMAGE_MODEL",
    "OPENAI_IMAGE_QUALITY",
    "OPENAI_IMAGE_SIZE",
    "FE_CODEX_TIMEOUT",
    "FE_GENIMAGE_TIMEOUT",
)

_ALLOWED_SIZES = {"1024x1024", "1536x1024", "1024x1536"}
_ASSET_PREFIX = "src/assets/"
_ALLOWED_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def docker_env_flags() -> list[str]:
    """``-e KEY=value`` flags for every set image-lane env var (secrets excluded —
    ``OPENAI_API_KEY`` must be passed by NAME by the caller, never inline)."""
    flags: list[str] = []
    for key in IMAGE_ENV_KEYS:
        if key == "OPENAI_API_KEY":
            continue  # by-name only (docker -e OPENAI_API_KEY), value stays in env
        value = os.getenv(key)
        if value:
            flags += ["-e", f"{key}={value}"]
    return flags


# --- in-container bootstrap (shared by one-shot fe-agent and Dev Mode) ----------
# genimage.mjs — deterministic one-prompt-one-file OpenAI image call (pure Node,
# global fetch, no deps). Codex invokes it once per asset. Written via a QUOTED
# heredoc, so nothing here expands at write time.
_GENIMAGE_MJS = r"""#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith('--')) { out[argv[i].slice(2)] = argv[i + 1]; i++; }
  }
  return out;
}

const args = parseArgs(process.argv.slice(2));
const prompt = args.prompt;
const outPath = args.out;
const size = args.size || process.env.OPENAI_IMAGE_SIZE || '1024x1024';
const quality = args.quality || process.env.OPENAI_IMAGE_QUALITY || 'medium';
if (!prompt || !outPath) {
  console.error('usage: node genimage.mjs --out <path> --prompt "<text>" [--size WxH] [--quality low|medium|high]');
  process.exit(2);
}
const apiKey = process.env.OPENAI_API_KEY;
if (!apiKey) { console.error('OPENAI_API_KEY not set; cannot generate image'); process.exit(3); }
const base = (process.env.OPENAI_BASE_URL || 'https://api.openai.com/v1').replace(/\/+$/, '');
const model = process.env.OPENAI_IMAGE_MODEL || 'gpt-image-2';

const body = { model, prompt, size, n: 1 };
if (quality && quality !== 'auto') body.quality = quality;

const timeoutMs = Number(process.env.FE_GENIMAGE_TIMEOUT || 180) * 1000;
let resp;
try {
  resp = await fetch(`${base}/images/generations`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  });
} catch (err) {
  console.error('image request failed:', err && err.message ? err.message : err);
  process.exit(4);
}
if (!resp.ok) {
  let detail = '';
  try { detail = await resp.text(); } catch {}
  console.error(`image API ${resp.status}: ${detail.slice(0, 400)}`);
  process.exit(5);
}
const data = await resp.json();
const b64 = data && data.data && data.data[0] && data.data[0].b64_json;
if (!b64) { console.error('no image data in response'); process.exit(6); }
const abs = path.resolve(outPath);
fs.mkdirSync(path.dirname(abs), { recursive: true });
fs.writeFileSync(abs, Buffer.from(b64, 'base64'));
console.log(`wrote ${outPath} (${size}, ${model})`);"""

# gen-assets — the image-assets skill's executor: forward Claude's request to the
# Codex agent. Fail-soft (always exits 0) so a missing key / image error never
# sinks a build/turn; Claude then falls back to CSS/SVG (never emoji). The
# ``__DIAG_DIR__`` / ``__STYLE_READ__`` markers are substituted in Python before
# the QUOTED heredoc is emitted (nothing expands at write time).
_GEN_ASSETS_SH = r"""#!/usr/bin/env bash
set -uo pipefail
GENIMG="$HOME/.fe-assets/genimage.mjs"

# Evidence that Claude actually invoked the asset lane (host reads this back).
echo "invoked: $*" >> __DIAG_DIR__/asset_gen.log 2>/dev/null || true

if ! command -v codex >/dev/null 2>&1; then
  echo "[gen-assets] 容器内未安装 codex(fe-agent 镜像可能未重建);跳过图片资源生成。"
  echo "no-codex" >> __DIAG_DIR__/asset_gen.log 2>/dev/null || true
  exit 0
fi
if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "[gen-assets] 未配置 OPENAI_API_KEY,跳过图片资源生成;请用 CSS/SVG 兜底,不要用 emoji。"
  echo "no-key" >> __DIAG_DIR__/asset_gen.log 2>/dev/null || true
  exit 0
fi
REQUEST="$*"
if [ -z "${REQUEST// /}" ]; then
  echo "[gen-assets] 用法: gen-assets \"<需要的图片:相对路径(放 src/assets/ 下)/主题/风格/尺寸>\""
  exit 0
fi

# Pull the project's visual-style context so EVERY image is anchored to one
# consistent style family even when Claude's per-image spec is terse. The source
# file + extraction mode are环境相关 (one-shot: sliced from the assembled prompt;
# Dev Mode: a pre-distilled context file). Fail-soft: missing file / no match ->
# empty -> no baseline block prepended. Overridable via ASSET_STYLE_CONTEXT_FILE.
STYLE_CONTEXT_FILE="${ASSET_STYLE_CONTEXT_FILE:-__STYLE_CONTEXT_PATH__}"
STYLE_CTX=""
if [ -f "$STYLE_CONTEXT_FILE" ]; then
__STYLE_READ__
fi
STYLE_BLOCK=""
if [ -n "${STYLE_CTX// /}" ]; then
  STYLE_BLOCK="# 全局视觉风格基线(本工程统一视觉口径 — 所有图片必须严格遵循、属于同一风格家族)
$STYLE_CTX

请据上述风格基线,让所有图在以下维度保持一致(看起来像同一个产品出品):配色(尽量呼应基线里的主色/强调色)、媒介(写实照片 / 扁平插画 / 3D 等,全工程统一一种)、光影、质感、构图与背景处理。

"
fi

INSTR="你是一个「资源图片生成」子 Agent。请为当前前端工程生成所需的位图资源。
${STYLE_BLOCK}唯一的生成手段是调用本机脚本(它会请求 OpenAI 图像模型并把 PNG 写入文件):
  node \"$GENIMG\" --out <相对路径,统一放在 src/assets/ 下> --size <1024x1024|1536x1024|1024x1536> --prompt \"<英文图像提示词>\"
要求:
- 为下面每一项资源各调用一次该脚本,保存到 src/assets/ 指定路径(目录会自动创建)。
- **把上面的全局视觉风格基线融进每一条英文 prompt**(配色 / 媒介 / 光影 / 质感 / 构图保持一致),让所有图属于同一视觉风格家族、并呼应 UI 的主色与强调色;提示词用英文、具体写实、贴合产品语境;图内不要渲染界面文字 / 按钮 / 水印 / UI 截图。
- 只能写入 src/assets/ 下的图片文件,不要改动任何其他源码。
- 全部完成后,用一句话列出你实际写入的文件路径。

# 需要的资源
$REQUEST"

printf '%s' "$INSTR" | timeout "${FE_CODEX_TIMEOUT:-420}" \
  codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check 2>&1
status=$?
if [ "$status" -ne 0 ]; then
  echo "[gen-assets] Codex 资源生成未完成(code $status,已忽略);请用 CSS/SVG 兜底,不要用 emoji。"
fi
exit 0"""

# image-assets skill — the documented trigger Claude uses for real imagery.
_SKILL_MD = r"""---
name: image-assets
description: Generate real raster image assets (hero images, illustrations, photos, avatars, product shots, backgrounds, textures, logos) for the project. Use this whenever the UI needs genuine imagery instead of emoji, icon-font hacks, or empty placeholders. Delegates to the Codex agent and the OpenAI image model.
---

# 生成真实图片资源(经 Codex)

当界面需要**真实图片**(主视觉/插画/照片/头像/产品图/背景/纹理/Logo 等)时使用本技能,
用真实位图取代 emoji、字符画或空占位,以达到高保真。

## 用法

在**工程根目录**运行(一次可描述多张):

    gen-assets "把需要的图片逐条列清楚:每张写明 src/assets/ 下的目标路径、主题内容、风格、建议尺寸"

例如:

    gen-assets "1) src/assets/hero.png — 现代简约 SaaS 仪表盘工作场景照,冷色调,1536x1024; 2) src/assets/avatar-1.png — 职业人物头像,中性背景,1024x1024"

该命令会触发 Codex 子 Agent,用 OpenAI 图像模型逐张生成位图并写入对应路径。

## 在代码中引用

资源统一放在 `src/assets/` 并通过 import 引用(Vite 会打包并按 base './' 重写路径,
保证子路径 iframe 预览也能加载):

    import heroUrl from './assets/hero.png'
    // ...
    <img src={heroUrl} alt="产品概览" />

## 约束

- 优先真实图片;**禁止**用 emoji 当图标/插画/装饰。
- **风格一致**:Codex 子 Agent 看不到工程的视觉风格规格,只能读你写进 `gen-assets` 的描述——所以**每条描述都要带上统一的视觉风格简报(主色/强调色、媒介、光影、调性),全工程所有图共用同一套**,让它们属于同一风格家族并呼应 UI 配色。
- 资源数量适度(一般 3–8 张),聚焦关键视觉位。
- 图片放 `src/assets/`(被打包);不要放 `public/` 再用运行时绝对路径(子路径预览会失效)。
- 若资源生成不可用,改用克制的 CSS/SVG 兜底,仍然不要用 emoji。"""

# How gen-assets extracts the style context from the configured file.
_STYLE_READ_PROMPT_SECTIONS = (
    "  STYLE_CTX=\"$(awk '/^# 视觉风格规格/{f=1} /^# 共享 API 契约/{f=0} f' "
    "\"$STYLE_CONTEXT_FILE\" 2>/dev/null | head -c 6000)\""
)
_STYLE_READ_RAW = '  STYLE_CTX="$(head -c 6000 "$STYLE_CONTEXT_FILE" 2>/dev/null)"'


def render_bootstrap(
    *,
    style_context_path: str,
    diagnostics_dir: str,
    style_extract: str = "raw",
) -> str:
    """The shell fragment that installs the whole asset lane at container start.

    Emits: env exports + dirs, ``genimage.mjs``, ``gen-assets`` (diagnostics under
    ``diagnostics_dir``, style context from ``style_context_path`` — extracted as
    the assembled prompt's style section when ``style_extract='prompt_sections'``,
    or read raw when ``'raw'``), the ``image-assets`` skill, the diagnostics
    files (``codex_path`` / ``gen_assets_path`` / ``asset_gen.log`` /
    ``codex_login.log``) and the optional Codex login. Idempotent — safe to run
    on every container (re)start.
    """
    style_read = (
        _STYLE_READ_PROMPT_SECTIONS if style_extract == "prompt_sections" else _STYLE_READ_RAW
    )
    gen_assets = (
        _GEN_ASSETS_SH
        .replace("__DIAG_DIR__", diagnostics_dir)
        .replace("__STYLE_CONTEXT_PATH__", style_context_path)
        .replace("__STYLE_READ__", style_read)
    )
    return f"""
# ===========================================================================
# Asset-generation lane (shared module: backend/services/code/asset_lane.py).
# Claude (code) -> image-assets skill -> gen-assets -> Codex -> image model.
# Written at startup so no app code is baked into the image and it stays
# tweakable without a rebuild.
# ===========================================================================
export CODEX_HOME="$HOME/.codex"
export PATH="$HOME/bin:$PATH"
mkdir -p "$HOME/bin" "$HOME/.fe-assets" "$HOME/.claude/skills/image-assets" "$CODEX_HOME" "{diagnostics_dir}"

cat > "$HOME/.fe-assets/genimage.mjs" <<'GENIMG_EOF'
{_GENIMAGE_MJS}
GENIMG_EOF

cat > "$HOME/bin/gen-assets" <<'GENASSETS_EOF'
{gen_assets}
GENASSETS_EOF
chmod +x "$HOME/bin/gen-assets"

cat > "$HOME/.claude/skills/image-assets/SKILL.md" <<'SKILL_EOF'
{_SKILL_MD}
SKILL_EOF

# Diagnostics so the host can explain a "no assets" run: is the Codex CLI even in
# this image and is gen-assets on PATH for Claude?
command -v codex     > {diagnostics_dir}/codex_path     2>/dev/null || true
command -v gen-assets > {diagnostics_dir}/gen_assets_path 2>/dev/null || true
: > {diagnostics_dir}/asset_gen.log  # gen-assets appends one line per invocation

# Authenticate Codex for the image-assets skill (optional). Codex does NOT read
# OPENAI_API_KEY from the env on its own — the key must be written to auth.json
# via `login --with-api-key`. Degrades to no assets when unset; gen-assets gates
# on the key too, so nothing ever blocks on this.
if [ -n "${{OPENAI_API_KEY:-}}" ] && command -v codex >/dev/null 2>&1; then
  printf '%s' "$OPENAI_API_KEY" | codex login --with-api-key > {diagnostics_dir}/codex_login.log 2>&1 || true
fi
"""


# --- resource_spec helpers (pure, unit-testable) --------------------------------
def safe_asset_path(path) -> tuple[str | None, str]:
    """Normalize one output path; returns ``(normalized, error)``.

    Only relative paths strictly inside ``src/assets/`` with a raster-image
    extension are allowed — no absolute paths, no ``..`` escapes, no URLs, no
    ``public/`` (breaks the subpath preview).
    """
    raw = str(path or "").strip()
    if not raw:
        return None, "空路径"
    if "://" in raw:
        return None, f"禁止远程 URL:{raw}"
    if raw.startswith("/") or raw.startswith("\\"):
        return None, f"禁止绝对路径:{raw}"
    norm = posixpath.normpath(raw.replace("\\", "/").lstrip("./"))
    if norm.startswith("..") or "/../" in norm:
        return None, f"禁止路径逃逸:{raw}"
    if not norm.startswith(_ASSET_PREFIX) or norm == _ASSET_PREFIX.rstrip("/"):
        return None, f"资源必须放在 {_ASSET_PREFIX} 下:{raw}"
    if not norm.lower().endswith(_ALLOWED_EXTS):
        return None, f"仅支持位图扩展名 {'/'.join(_ALLOWED_EXTS)}:{raw}"
    return norm, ""


def normalize_outputs(resource_spec: dict | None) -> tuple[list[dict], list[str]]:
    """Normalize ``resource_spec.outputs``; returns ``(outputs, warnings)``.

    Invalid entries are DROPPED with a warning (the validator rejects them again
    at execution time, so a hand-edited spec can't smuggle a bad path through).
    """
    spec = resource_spec if isinstance(resource_spec, dict) else {}
    raw_outputs = spec.get("outputs")
    outputs: list[dict] = []
    warnings: list[str] = []
    if not isinstance(raw_outputs, list):
        return outputs, warnings
    seen: set[str] = set()
    default_size = os.getenv("OPENAI_IMAGE_SIZE", "1024x1024")
    for item in raw_outputs[:20]:
        if not isinstance(item, dict):
            warnings.append("忽略非法 output 项(非对象)")
            continue
        norm, err = safe_asset_path(item.get("path"))
        if not norm:
            warnings.append(f"忽略非法 output:{err}")
            continue
        if norm in seen:
            warnings.append(f"忽略重复 output:{norm}")
            continue
        seen.add(norm)
        size = str(item.get("size") or "").strip()
        if size and size not in _ALLOWED_SIZES:
            warnings.append(f"output {norm} 尺寸 {size} 不受支持,已改用默认")
            size = ""
        outputs.append({
            "path": norm,
            "size": size or default_size,
            "prompt": str(item.get("prompt") or "")[:600],
            "required": item.get("required") is not False,
            "used_by": [str(u)[:200] for u in item.get("used_by") or [] if str(u).strip()][:10],
        })
    return outputs, warnings


def validate_output_paths(outputs: list[dict] | None) -> tuple[bool, list[str]]:
    """Re-check every output path (defense-in-depth before touching the container)."""
    errors: list[str] = []
    for item in outputs or []:
        norm, err = safe_asset_path((item or {}).get("path"))
        if not norm:
            errors.append(err)
    return (not errors), errors
