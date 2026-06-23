"""
Thumbnail-slicing service (agentic, container-sandboxed).

Turns a Code project's preview thumbnail (one PNG) into an EDITABLE Figma design
instead of a single flat image fill. An OpenAI Codex CLI runs headless inside a
throwaway Docker container, looks at the rendered thumbnail, and emits a Design
IR layer tree (text -> TEXT nodes, flat/gradient blocks -> vector RECTANGLEs,
photos/icons/logos -> IMAGE nodes carrying a crop bbox). A deterministic Pillow
script then crops those bboxes out of the source PNG into per-region slices.

Sandbox properties (mirror frontend_project_service):
  * runs as the non-root ``node`` user inside a ``--rm`` container;
  * the only host-visible write is the mounted ``/out`` dir — Codex config, the
    git scratch tree and any noise live in the container fs and die with --rm;
  * only ``ir.json`` + the cropped slices are read back.

This steps OUTSIDE the capability-routed AI provider abstraction on purpose: the
Codex CLI owns its own OpenAI backend, so this is an agent-EXECUTION lane, not a
single ``generate_text`` call. It NEVER raises on agent/model failure — it
degrades to a single-image IR (== the legacy preview_image export) so the run
always yields a previewable, importable payload. It raises only on infrastructure
errors (docker missing, container never exits).
"""
import base64
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional, Tuple

from backend.services.code.figma.ir import (
    Box,
    IRNode,
    designir_from_dict,
    image_design_ir,
    ir_to_plugin_payload,
)
from backend.services.prompts import prompt_store

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).parent.parent.parent / "prompts" / "code"

# Per-slice ceiling when reading the deliverable back (a runaway crop can't blow
# up the export payload row). The container also downsamples large crops.
_MAX_SLICE_BYTES = 4_000_000

# Total budget for inline slice images in ONE export payload. The payload is
# stored as a single DB row (FigmaExportPackage.payload_json) and base64 inflates
# ~33%, so cap the combined slice bytes; extras beyond the budget are dropped.
_MAX_PAYLOAD_IMAGE_BYTES = 24_000_000

# Default Codex CLI flags for headless, non-interactive execution inside our own
# container. The container IS the sandbox, so Codex's own approval prompts and
# inner sandbox are disabled. Overridable via SLICER_CODEX_FLAGS without a
# rebuild (the exact flag spelling must be validated against the baked-in Codex
# version — see backend/docker/slicer-agent/Dockerfile / the PoC notes).
_DEFAULT_CODEX_FLAGS = (
    "exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --json"
)

# Runs INSIDE the container. Codex analyses /out/preview.png into /out/ir.json;
# crop.py slices the source PNG by each IMAGE node's bbox; a tiny validator marks
# the run degraded when Codex produced no usable ir.json (host then falls back to
# a single-image IR). Codex stream-json goes to stdout (mapped to the timeline);
# stderr/noise is redirected to /out logs so only structured events reach stdout.
_CONTAINER_SCRIPT = r"""
set -u
export HOME=/tmp/work
export CODEX_HOME=/tmp/work/.codex
mkdir -p "$HOME" "$CODEX_HOME" /out/slices
cd /tmp/work
git init -q 2>/dev/null || true

emit() { printf '%s\n' "{\"type\":\"slice_phase\",\"phase\":\"$1\"}"; }

# --- crop.py: deterministic slicing of IMAGE regions from the source PNG ----
cat > /tmp/crop.py <<'CROP_EOF'
import json, os, sys
from PIL import Image

ir_path, src_path, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(out_dir, exist_ok=True)
try:
    with open(ir_path) as fh:
        ir = json.load(fh)
except Exception as exc:  # noqa: BLE001
    print("no valid ir.json:", exc)
    sys.exit(0)

img = Image.open(src_path).convert("RGBA")
W, H = img.size
MAX_DIM = 1600

def as_int(value, default):
    try:
        return int(round(float(value)))
    except Exception:  # noqa: BLE001
        return default

def clamp(value, lo, hi):
    return max(lo, min(hi, value))

count = 0

def image_ref(node):
    for paint in node.get("fills") or []:
        if isinstance(paint, dict) and str(paint.get("type", "")).upper() == "IMAGE":
            return paint.get("image_ref") or paint.get("imageRef")
    return None

def walk(node):
    global count
    if not isinstance(node, dict):
        return
    if str(node.get("type", "")).upper() == "IMAGE":
        ref = image_ref(node)
        box = node.get("crop") or node.get("box") or {}
        x = clamp(as_int(box.get("x", 0), 0), 0, max(0, W - 1))
        y = clamp(as_int(box.get("y", 0), 0), 0, max(0, H - 1))
        w = clamp(as_int(box.get("width", box.get("w", 0)), 0), 1, W - x)
        h = clamp(as_int(box.get("height", box.get("h", 0)), 0), 1, H - y)
        if ref and w > 0 and h > 0:
            crop = img.crop((x, y, x + w, y + h))
            if max(crop.size) > MAX_DIM:
                ratio = MAX_DIM / max(crop.size)
                crop = crop.resize((max(1, int(crop.size[0] * ratio)),
                                    max(1, int(crop.size[1] * ratio))))
            safe = "".join(c for c in str(ref) if c.isalnum() or c in "-_") or "img%d" % count
            crop.save(os.path.join(out_dir, safe + ".png"), optimize=True)
            count += 1
    for child in node.get("children") or []:
        walk(child)

walk(ir.get("root") or {})
print("cropped %d slice(s)" % count)
CROP_EOF

# --- 0. Authenticate Codex with the API key --------------------------------
# codex (>=0.x) does NOT pick up OPENAI_API_KEY from the environment on its own:
# the key must be written to auth.json via `login --with-api-key`, otherwise
# every request goes out with no bearer header and the API rejects it (401).
# `login` reads the key from stdin; pipe it in here.
printf '%s' "${OPENAI_API_KEY:-}" | codex login --with-api-key > /out/login.log 2>&1
echo "$?" > /out/login_exit

# --- 1. Analyse: Codex vision -> /out/ir.json -------------------------------
# The prompt MUST be fed on stdin, NOT as a positional argument: `-i/--image` is
# variadic (<FILE>...) and greedily swallows any following positional, so a
# trailing prompt arg gets parsed as a second image path and codex then reads an
# (empty) stdin -> "No prompt provided". With the prompt on stdin, `-i` binds
# only preview.png and codex takes its instructions from stdin.
emit analyze
timeout "${CODEX_TIMEOUT:-300}" codex ${SLICER_CODEX_FLAGS} \
  -i /out/preview.png < /out/prompt.txt 2> /out/codex_stderr.log
echo "$?" > /out/codex_exit

# Codex is told to write /out/ir.json; if it wrote one elsewhere in the scratch
# tree, publish the first one found (a harmless path mistake stays recoverable).
if [ ! -f /out/ir.json ]; then
  FOUND="$(find /tmp/work -maxdepth 3 -name ir.json -print -quit 2>/dev/null)"
  [ -n "$FOUND" ] && cp "$FOUND" /out/ir.json
fi

# --- 2. Crop image slices (deterministic) -----------------------------------
emit crop
python3 /tmp/crop.py /out/ir.json /out/preview.png /out/slices > /out/crop.log 2>&1
echo "$?" > /out/crop_exit

# --- 3. Validate / mark degraded --------------------------------------------
emit validate
python3 - <<'VAL_EOF'
import json
try:
    with open('/out/ir.json') as fh:
        data = json.load(fh)
    ok = isinstance(data, dict) and isinstance(data.get('root'), dict)
except Exception:  # noqa: BLE001
    ok = False
open('/out/degraded', 'w').write('' if ok else 'fallback')
VAL_EOF
"""


class FigmaSliceService:
    """Slice a preview thumbnail into an editable Design IR via a sandboxed agent."""

    def __init__(self):
        self.image = os.getenv("SLICER_AGENT_IMAGE", "slicer-agent:latest")
        self.docker = os.getenv("DOCKER_BIN", "docker")
        self.codex_flags = os.getenv("SLICER_CODEX_FLAGS", _DEFAULT_CODEX_FLAGS)
        self.codex_timeout = int(os.getenv("CODEX_TIMEOUT", "300"))
        # Host backstop for a container that never exits (analyse + crop is fast
        # vs the multi-round fe-agent, so this is much smaller than fe's ceiling).
        self.total_timeout = int(os.getenv("SLICER_TOTAL_TIMEOUT", "420"))

    def is_configured(self) -> bool:
        return bool(os.getenv("OPENAI_API_KEY"))

    # --- prompt assembly -----------------------------------------------------
    def _load_prompt(self, name: str) -> str:
        return prompt_store.get(f"code/{name}")

    @staticmethod
    def _fill(template: str, **values: str) -> str:
        out = template
        for key, value in values.items():
            out = out.replace(f"[[{key}]]", value if value is not None else "")
        return out

    # --- public API ----------------------------------------------------------
    def slice_image(
        self,
        *,
        image_data_url: str,
        name: str = "",
        image_size: Optional[Tuple[int, int]] = None,
        context_ledger: str = "",
        style_prompt: str = "",
        on_event: Optional[Callable[[dict], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Run the containerized slicer agent. Returns a result dict (never raises
        on model/agent failure):

        ``{success, degraded, degraded_reason, error, ir(dict), slices({ref:bytes}),
        usage, cost_usd, workdir}``. On any failure to produce a usable layer tree
        the result degrades to a single-image IR (== legacy preview_image export),
        so ``ir`` is always importable.
        """
        if not self.is_configured():
            return self._empty("OPENAI_API_KEY not configured")

        try:
            raw, _mime = _decode_data_url(image_data_url)
        except ValueError as exc:
            return self._empty(str(exc))
        width, height = image_size or _image_size(raw)

        prompt = self._fill(
            self._load_prompt("figma_slice_prompt.txt"),
            IMAGE_WIDTH=str(int(width)),
            IMAGE_HEIGHT=str(int(height)),
            NAME=name or "Design",
            CONTEXT_LEDGER=context_ledger or "",
            STYLE_PROMPT=style_prompt or "",
        )

        workdir = Path(tempfile.mkdtemp(prefix="slicer-agent-"))
        # The container runs as the non-root `node` uid and writes to this mounted
        # dir. mkdtemp is 0700 (host user only); open it up so the container uid
        # can write on real Linux (Docker Desktop for Mac is permissive).
        os.chmod(workdir, 0o777)
        (workdir / "preview.png").write_bytes(raw)
        (workdir / "prompt.txt").write_text(prompt, encoding="utf-8")
        os.chmod(workdir / "preview.png", 0o644)

        api_key = os.getenv("OPENAI_API_KEY")
        cmd = [
            self.docker, "run", "--rm", "--user", "node",
            "-e", "OPENAI_API_KEY",
            "-e", f"SLICER_CODEX_FLAGS={self.codex_flags}",
            "-e", f"CODEX_TIMEOUT={self.codex_timeout}",
            "-v", f"{workdir}:/out",
            self.image, "bash", "-c", _CONTAINER_SCRIPT,
        ]
        env = dict(os.environ, OPENAI_API_KEY=api_key or "")

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
                    continue
                if _looks_like_usage(event):
                    result_events.append(event)
                if on_event:
                    try:
                        on_event(event)
                    except Exception:  # noqa: BLE001 - a bad callback must not kill the run
                        logger.exception("slice on_event callback raised")
                if is_cancelled and is_cancelled():
                    proc.kill()
                    return self._empty("cancelled")
            proc.wait(timeout=self.total_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("figma slice agent timed out")
        finally:
            stderr = proc.stderr.read() if proc.stderr else ""
            if stderr:
                logger.info("slicer-agent stderr: %s", stderr[:2000])

        docker_exit = proc.returncode
        degraded_reason = self._read_text(workdir / "degraded").strip() or None
        ir_dict = self._read_ir(workdir / "ir.json")
        slices = self._collect_slices(workdir / "slices")
        usage = self._merge_usage(result_events)

        # Fall back to a single-image IR whenever Codex produced no usable tree.
        # This is exactly the legacy preview_image export, so the worst case still
        # yields an importable (if not editable) design.
        if not ir_dict or not isinstance(ir_dict.get("root"), dict):
            ir_dict = image_design_ir(
                name=name or "Design",
                image_data_url=image_data_url,
                width=float(width),
                height=float(height),
            ).to_dict()
            slices = {}
            degraded_reason = degraded_reason or "fallback"

        success = docker_exit == 0 and bool(ir_dict)
        degraded = bool(degraded_reason)
        return {
            "success": success,
            "degraded": degraded,
            "degraded_reason": degraded_reason if degraded else None,
            "error": None if success else self._format_failure(
                docker_exit=docker_exit,
                codex_exit=self._read_exit_code(workdir / "codex_exit"),
                stderr=stderr,
                non_json_stdout=non_json_stdout,
                codex_log=self._read_text(workdir / "codex_stderr.log"),
            ),
            "ir": ir_dict,
            "slices": slices,
            "usage": usage,
            "cost_usd": float(sum(_usage_cost(e) for e in result_events)),
            "workdir": str(workdir),
        }

    # --- helpers -------------------------------------------------------------
    @staticmethod
    def _empty(error: str) -> dict:
        return {
            "success": False, "degraded": False, "degraded_reason": None,
            "error": error, "ir": None, "slices": {},
            "usage": {}, "cost_usd": 0.0, "workdir": None,
        }

    @staticmethod
    def _read_ir(path: Path) -> Optional[dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _collect_slices(self, root: Path) -> dict:
        """Return ``{ref: png_bytes}`` for every cropped slice under ``root``."""
        out: dict[str, bytes] = {}
        if not root.exists():
            return out
        for path in sorted(root.glob("*.png")):
            try:
                if path.stat().st_size > _MAX_SLICE_BYTES:
                    logger.warning("slice %s exceeds size cap, skipping", path.name)
                    continue
                out[path.stem] = path.read_bytes()
            except OSError:
                continue
        return out

    @staticmethod
    def _merge_usage(events: list[dict]) -> dict:
        merged: dict = {}
        for event in events:
            usage = _usage_block(event)
            for key, value in (usage or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    merged[key] = merged.get(key, 0) + value
        return merged

    @staticmethod
    def _read_exit_code(path: Path) -> Optional[int]:
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
        self, *, docker_exit, codex_exit, stderr, non_json_stdout, codex_log
    ) -> str:
        reasons = []
        if docker_exit not in (0, None):
            reasons.append(f"container exited with code {docker_exit}")
        if codex_exit not in (0, None):
            reasons.append(f"codex exited with code {codex_exit}")
        reasons.append("no usable layer tree produced")
        details = []
        if codex_log:
            details.append(f"codex log: {self._clip(codex_log)}")
        if stderr:
            details.append(f"stderr: {self._clip(stderr)}")
        if non_json_stdout:
            details.append(f"stdout: {self._clip(chr(10).join(non_json_stdout))}")
        return " | ".join(["; ".join(reasons), *details])


# --- module-level helpers (decode / size / usage parsing) -------------------
def _decode_data_url(data_url: str) -> Tuple[bytes, str]:
    """Return (bytes, mime) from a ``data:<mime>;base64,<payload>`` URL."""
    if not data_url or not data_url.startswith("data:"):
        raise ValueError("preview image is not an inline data URL")
    try:
        header, payload = data_url.split(",", 1)
        mime = header[5:].split(";", 1)[0] or "image/png"
        raw = base64.b64decode(payload)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid preview image data") from exc
    return raw, mime


def _image_size(raw: bytes) -> Tuple[int, int]:
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(raw)) as img:
            return img.width, img.height
    except Exception:  # noqa: BLE001
        return 1024, 1024


def _usage_block(event: dict) -> Optional[dict]:
    """Best-effort token-usage block out of a Codex stream event (schema varies)."""
    if not isinstance(event, dict):
        return None
    for key in ("usage", "token_usage", "tokens"):
        block = event.get(key)
        if isinstance(block, dict):
            return block
    info = event.get("info")
    if isinstance(info, dict) and isinstance(info.get("usage"), dict):
        return info["usage"]
    return None


def _looks_like_usage(event: dict) -> bool:
    if not isinstance(event, dict):
        return False
    etype = str(event.get("type", "")).lower()
    return "token" in etype or _usage_block(event) is not None


def _usage_cost(event: dict) -> float:
    if not isinstance(event, dict):
        return 0.0
    for key in ("cost_usd", "total_cost_usd", "cost"):
        value = event.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


def build_slice_payload(ir_dict: dict, slices: dict, *, name: str = "") -> dict:
    """Turn the agent's IR dict + cropped slice bytes into a plugin payload.

    Wires each cropped slice in as an inline data URL (within a total size
    budget), drops dangling IMAGE refs / zero-area noise nodes, synthesizes a
    root box from the children union when missing, then flattens to the
    plugin-consumable structure via the canonical ``ir_to_plugin_payload``.
    Deterministic and DB-free, so it is unit-testable without a container.
    """
    ir = designir_from_dict(ir_dict)
    ir.source = "sliced"
    if name:
        ir.name = name

    images: dict[str, str] = {}
    budget = _MAX_PAYLOAD_IMAGE_BYTES
    for ref, data in (slices or {}).items():
        if not data or len(data) > budget:
            continue
        images[str(ref)] = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        budget -= len(data)

    _normalize_node(ir.root, images)
    _ensure_root_box(ir.root)
    ir.images = images
    return ir_to_plugin_payload(ir)


def _normalize_node(node: IRNode, images: dict) -> None:
    # Drop IMAGE paints whose slice never materialized (dangling ref), so the
    # plugin never receives an unresolvable imageRef.
    node.fills = [
        paint
        for paint in node.fills
        if not (paint.type == "IMAGE" and (paint.image_ref or "") not in images)
    ]
    kept: list[IRNode] = []
    for child in node.children:
        _normalize_node(child, images)
        box = child.box
        zero_area = box is None or box.width <= 0 or box.height <= 0
        if zero_area and not child.children:
            continue  # 1px / empty noise leaf — skip
        kept.append(child)
    node.children = kept


def _ensure_root_box(root: IRNode) -> None:
    """Guarantee a sane root box so absolute->relative conversion has an origin."""
    box = root.box
    if box and box.width > 0 and box.height > 0:
        return
    boxes = [child.box for child in root.children if child.box]
    if not boxes:
        root.box = Box(0, 0, 1, 1)
        return
    min_x = min(b.x for b in boxes)
    min_y = min(b.y for b in boxes)
    max_x = max(b.x + b.width for b in boxes)
    max_y = max(b.y + b.height for b in boxes)
    root.box = Box(min_x, min_y, max(1.0, max_x - min_x), max(1.0, max_y - min_y))


_service_instance: Optional[FigmaSliceService] = None


def get_figma_slice_service() -> FigmaSliceService:
    global _service_instance
    if _service_instance is None:
        _service_instance = FigmaSliceService()
    return _service_instance
