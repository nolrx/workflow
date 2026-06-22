"""Service-level tests for figma_slice_service (mocked container, no docker).

Drives ``FigmaSliceService.slice_image`` with a fake ``subprocess.Popen`` that
stands in for the throwaway slicer container — it can drop an ir.json + slices
into the mounted /out workdir, letting us assert both the happy path and the
single-image fallback without ever launching docker or Codex.
"""
import base64
import io
import json
from pathlib import Path

import pytest

from backend.services.code import figma_slice_service as svc_mod
from backend.services.code.figma_slice_service import FigmaSliceService, build_slice_payload


def _png_data_url(w: int = 120, h: int = 80) -> str:
    from PIL import Image

    img = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _png_bytes(w: int = 30, h: int = 30) -> bytes:
    from PIL import Image

    img = Image.new("RGBA", (w, h), (10, 20, 30, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _workdir_from_cmd(cmd) -> Path:
    for i, arg in enumerate(cmd):
        if arg == "-v" and i + 1 < len(cmd) and str(cmd[i + 1]).endswith(":/out"):
            return Path(str(cmd[i + 1]).split(":/out")[0])
    raise AssertionError("no -v {workdir}:/out in docker cmd")


def _make_fake_popen(producer=None, stdout_lines=None, returncode=0):
    """Build a fake Popen class; ``producer(workdir)`` may write files into /out."""

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            workdir = _workdir_from_cmd(cmd)
            if producer:
                producer(workdir)
            self.stdout = iter(stdout_lines or [])
            self.stderr = io.StringIO("")
            self.returncode = returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    return _FakePopen


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return FigmaSliceService()


def test_not_configured_returns_empty(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    svc = FigmaSliceService()
    out = svc.slice_image(image_data_url=_png_data_url())
    assert out["success"] is False
    assert out["ir"] is None


def test_happy_path_collects_ir_and_slices(monkeypatch, configured):
    ir = {
        "ir_version": "1.0",
        "source": "sliced",
        "name": "T",
        "root": {
            "type": "FRAME",
            "box": {"x": 0, "y": 0, "width": 120, "height": 80},
            "children": [
                {
                    "type": "TEXT",
                    "characters": "Hi",
                    "box": {"x": 4, "y": 4, "width": 40, "height": 16},
                    "fills": [{"type": "SOLID", "color": "#111111"}],
                },
                {
                    "type": "IMAGE",
                    "box": {"x": 60, "y": 0, "width": 60, "height": 80},
                    "crop": {"x": 60, "y": 0, "width": 60, "height": 80},
                    "fills": [{"type": "IMAGE", "image_ref": "img1"}],
                },
            ],
        },
    }

    def producer(workdir: Path):
        (workdir / "ir.json").write_text(json.dumps(ir), encoding="utf-8")
        slices = workdir / "slices"
        slices.mkdir(exist_ok=True)
        (slices / "img1.png").write_bytes(_png_bytes())
        (workdir / "degraded").write_text("", encoding="utf-8")

    monkeypatch.setattr(svc_mod.subprocess, "Popen", _make_fake_popen(producer))
    out = configured.slice_image(image_data_url=_png_data_url(), name="T")

    assert out["success"] is True
    assert out["degraded"] is False
    assert isinstance(out["ir"].get("root"), dict)
    assert out["slices"] and "img1" in out["slices"]

    # The publish assembly wires the slice in and keeps the tree editable.
    payload = build_slice_payload(out["ir"], out["slices"], name="T")
    assert payload["source"] == "sliced"
    assert "img1" in payload["images"]
    types = {c["type"] for c in payload["root"]["children"]}
    assert {"TEXT", "IMAGE"} <= types


def test_missing_ir_degrades_to_single_image(monkeypatch, configured):
    # Container "ran" (exit 0) but produced no ir.json -> single-image fallback.
    monkeypatch.setattr(
        svc_mod.subprocess, "Popen", _make_fake_popen(producer=None)
    )
    out = configured.slice_image(image_data_url=_png_data_url(120, 80), name="T")

    assert out["success"] is True
    assert out["degraded"] is True
    assert out["degraded_reason"] == "fallback"
    assert out["slices"] == {}
    # Fallback IR == legacy preview_image export: one IMAGE child filling a frame.
    root = out["ir"]["root"]
    assert root["type"] == "FRAME"
    assert root["children"][0]["type"] == "IMAGE"


def test_invalid_ir_json_degrades(monkeypatch, configured):
    def producer(workdir: Path):
        (workdir / "ir.json").write_text("{not valid json", encoding="utf-8")

    monkeypatch.setattr(svc_mod.subprocess, "Popen", _make_fake_popen(producer))
    out = configured.slice_image(image_data_url=_png_data_url(), name="T")
    assert out["success"] is True
    assert out["degraded"] is True
    assert out["ir"]["root"]["children"][0]["type"] == "IMAGE"


def test_cancelled_midstream(monkeypatch, configured):
    monkeypatch.setattr(
        svc_mod.subprocess, "Popen",
        _make_fake_popen(stdout_lines=['{"type":"slice_phase","phase":"analyze"}']),
    )
    out = configured.slice_image(
        image_data_url=_png_data_url(), is_cancelled=lambda: True
    )
    assert out["success"] is False
    assert out["error"] == "cancelled"
