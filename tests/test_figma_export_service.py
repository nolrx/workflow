"""Unit tests for the deterministic preview-image export path (no DB / no model)."""
import pytest

from backend.services.code.figma_export_service import (
    ExportError,
    build_preview_export_payload,
    select_preview_data_url,
)

# A valid 1x1 PNG, inline as a data URL.
_PNG_1x1 = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class _FakeProject:
    def __init__(self, previews, confirmed=None, title="Proj"):
        self._previews = previews
        self.confirmed_preview_url = confirmed
        self.title = title

    def get_preview_images(self):
        return self._previews


def test_select_prefers_requested_id():
    project = _FakeProject(
        previews=[{"id": "preview-1", "url": "a"}, {"id": "preview-2", "url": "b"}]
    )
    assert select_preview_data_url(project, "preview-2") == "b"


def test_select_falls_back_to_confirmed_then_first():
    project = _FakeProject(previews=[{"id": "preview-1", "url": "a"}], confirmed="confirmed-url")
    assert select_preview_data_url(project, None) == "confirmed-url"
    project_no_confirm = _FakeProject(previews=[{"id": "preview-1", "url": "a"}])
    assert select_preview_data_url(project_no_confirm, None) == "a"


def test_select_missing_id_raises():
    project = _FakeProject(previews=[{"id": "preview-1", "url": "a"}])
    with pytest.raises(ExportError):
        select_preview_data_url(project, "nope")


def test_build_preview_payload():
    project = _FakeProject(previews=[{"id": "preview-1", "url": _PNG_1x1}])
    payload = build_preview_export_payload(project, "preview-1")
    assert payload["source"] == "preview_image"
    assert payload["root"]["type"] == "FRAME"
    image_node = payload["root"]["children"][0]
    assert image_node["type"] == "IMAGE"
    ref = image_node["fills"][0]["imageRef"]
    assert payload["images"][ref] == _PNG_1x1


def test_build_preview_payload_rejects_non_data_url():
    project = _FakeProject(previews=[{"id": "preview-1", "url": "https://example.com/x.png"}])
    with pytest.raises(ExportError):
        build_preview_export_payload(project, "preview-1")
