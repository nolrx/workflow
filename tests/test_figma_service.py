"""Unit tests for the Figma REST client (URL parsing + error mapping)."""
import httpx
import pytest

import backend.services.code.figma_service as fs
from backend.services.code.figma_service import (
    FigmaError,
    FigmaService,
    extract_first_frame_node,
    parse_figma_url,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.figma.com/design/ABC123/My-File?node-id=12-34", ("ABC123", "12:34")),
        ("https://www.figma.com/file/KEY999/Name", ("KEY999", None)),
        ("https://figma.com/design/Zz9/Proj?node-id=1%3A2&t=x", ("Zz9", "1:2")),
    ],
)
def test_parse_figma_url(url, expected):
    assert parse_figma_url(url) == expected


def test_parse_figma_url_invalid():
    with pytest.raises(FigmaError):
        parse_figma_url("https://example.com/not-figma")


def _mock_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def factory(*_args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=transport)

    monkeypatch.setattr(fs.httpx, "Client", factory)


def test_get_file_success(monkeypatch):
    def handler(request):
        assert request.headers["X-Figma-Token"] == "tok"
        return httpx.Response(200, json={"name": "Doc", "document": {"type": "DOCUMENT"}})

    _mock_client(monkeypatch, handler)
    data = FigmaService("tok").get_file("KEY")
    assert data["name"] == "Doc"


@pytest.mark.parametrize(
    "status,expected_status",
    [(401, 403), (403, 403), (404, 404), (429, 429), (500, 502)],
)
def test_error_mapping(monkeypatch, status, expected_status):
    _mock_client(monkeypatch, lambda request: httpx.Response(status, json={}))
    with pytest.raises(FigmaError) as exc:
        FigmaService("tok").get_file("KEY")
    assert exc.value.status == expected_status


def test_get_image_urls(monkeypatch):
    _mock_client(
        monkeypatch,
        lambda request: httpx.Response(200, json={"images": {"1:2": "https://img/x.png"}}),
    )
    urls = FigmaService("tok").get_image_urls("KEY", ["1:2"])
    assert urls["1:2"] == "https://img/x.png"


def test_extract_first_frame_node():
    response = {"nodes": {"1:2": {"document": {"id": "1:2", "type": "FRAME"}}}}
    node_id, doc = extract_first_frame_node(response, "1:2")
    assert node_id == "1:2"
    assert doc["type"] == "FRAME"


def test_extract_first_frame_node_missing():
    with pytest.raises(FigmaError):
        extract_first_frame_node({"nodes": {}}, None)
