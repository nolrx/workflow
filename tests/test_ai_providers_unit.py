"""
Pure-function / mocked unit tests for the Claude and Panlaxy providers (no network)."""
import base64
from types import SimpleNamespace

from backend.services.ai.claude import _guess_media_type
from backend.services.ai.panlaxy import PanlaxyProvider

# --- Fakes for the OpenAI-compatible client --------------------------------

class _FakeImages:
    def __init__(self, result=None, exc=None, capture=None):
        self._result = result
        self._exc = exc
        self.capture = capture if capture is not None else {}

    def generate(self, **kwargs):
        self.capture["endpoint"] = "generate"
        self.capture.update(kwargs)
        if self._exc:
            raise self._exc
        return self._result

    def edit(self, **kwargs):
        self.capture["endpoint"] = "edit"
        self.capture.update(kwargs)
        if self._exc:
            raise self._exc
        return self._result


class _FakeClient:
    def __init__(self, images):
        self.images = images


class _FakeAPIError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _result_with_b64(raw: bytes):
    return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(raw).decode(),
                                                 url=None)])


def _make_provider(images: _FakeImages, **kwargs) -> PanlaxyProvider:
    provider = PanlaxyProvider(api_key="k", model="gpt-image-2", **kwargs)
    provider._client = _FakeClient(images)  # bypass real OpenAI client
    return provider


# --- Claude helper ---------------------------------------------------------

def test_guess_media_type():
    assert _guess_media_type(b"\x89PNG\r\n\x1a\n....") == "image/png"
    assert _guess_media_type(b"\xff\xd8\xff\xe0....") == "image/jpeg"
    assert _guess_media_type(b"RIFF1234WEBP....") == "image/webp"
    assert _guess_media_type(b"GIF89a....") == "image/gif"
    assert _guess_media_type(b"\x00\x01\x02\x03") == "image/png"


# --- Panlaxy extraction / config ------------------------------------------

def test_panlaxy_extract_b64_image():
    raw = b"\x89PNG\r\n\x1a\nfake-image-bytes"
    item = SimpleNamespace(b64_json=base64.b64encode(raw).decode(), url=None)
    assert PanlaxyProvider._extract_image_bytes(item) == raw


def test_panlaxy_extract_no_data():
    item = SimpleNamespace(b64_json=None, url=None)
    assert PanlaxyProvider._extract_image_bytes(item) is None


def test_panlaxy_base_url_normalized():
    provider = PanlaxyProvider(api_key="k", model="gpt-image-2",
                               base_url="https://api.panlaxy.io/v1/")
    assert provider.base_url == "https://api.panlaxy.io/v1"


def test_panlaxy_unconfigured_returns_error():
    provider = PanlaxyProvider(api_key="", model="gpt-image-2")
    assert provider.is_configured() is False
    result = provider.generate_image("a cat")
    assert result.success is False
    assert result.image_data is None


# --- Panlaxy generate / edit flows -----------------------------------------

def test_panlaxy_generate_image_success_path():
    raw = b"\x89PNG\r\n\x1a\n" + b"x" * 2000
    capture = {}
    images = _FakeImages(result=_result_with_b64(raw), capture=capture)
    provider = _make_provider(images, quality="high", size="1536x1024")

    result = provider.generate_image("a red circle")

    assert result.success is True
    assert result.image_data == raw
    assert capture["endpoint"] == "generate"
    assert capture["model"] == "gpt-image-2"
    assert capture["prompt"] == "a red circle"
    assert capture["size"] == "1536x1024"
    assert capture["quality"] == "high"


def test_panlaxy_generate_image_uses_edits_with_references():
    raw = b"\x89PNG\r\n\x1a\n" + b"y" * 2000
    capture = {}
    images = _FakeImages(result=_result_with_b64(raw), capture=capture)
    provider = _make_provider(images)

    result = provider.generate_image("edit it", reference_images=[b"ref1", b"ref2"])

    assert result.success is True
    assert capture["endpoint"] == "edit"
    assert len(capture["image"]) == 2
    assert capture["prompt"] == "edit it"


def test_panlaxy_upstream_error_is_graceful():
    images = _FakeImages(exc=_FakeAPIError(503, "No available compatible accounts"))
    provider = _make_provider(images)

    result = provider.generate_image("a red circle")

    assert result.success is False
    assert result.image_data is None
    assert "503" in result.error
    assert "No available compatible accounts" in result.error
