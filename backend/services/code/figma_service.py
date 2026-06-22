"""
Figma REST client.

Thin wrapper over the Figma REST API (https://www.figma.com/developers/api)
authenticated with a personal access token (PAT). Used by the import / restore
path to pull a file's node tree and a rendered image of the selected frame.

Figma REST is READ-ONLY — there is no endpoint to write nodes back, which is why
the export direction is handled by a companion Figma plugin instead.

All failures are normalised to ``FigmaError(status, code, message)`` so the route
layer can map them onto the unified ``error_response`` vocabulary.
"""
import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger(__name__)

FIGMA_API_BASE = "https://api.figma.com"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class FigmaError(Exception):
    """A Figma API / transport failure, pre-classified for the route layer."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status  # suggested HTTP status for the API response
        self.code = code  # error_response code string
        self.message = message


def parse_figma_url(url: str) -> Tuple[str, Optional[str]]:
    """Extract ``(file_key, node_id?)`` from a pasted Figma URL.

    Supports both ``/file/<KEY>/...`` and ``/design/<KEY>/...`` shapes, and reads
    the optional ``node-id`` query param, normalising ``1-23`` / ``1%3A23`` to the
    REST form ``1:23``.
    """
    if not url or not isinstance(url, str):
        raise FigmaError(400, "VALIDATION_ERROR", "请粘贴有效的 Figma 链接")
    cleaned = url.strip()
    match = re.search(r"figma\.com/(?:file|design|proto)/([A-Za-z0-9]+)", cleaned)
    if not match:
        raise FigmaError(400, "VALIDATION_ERROR", "无法从链接中解析 Figma 文件 key")
    file_key = match.group(1)

    node_id: Optional[str] = None
    query = parse_qs(urlparse(cleaned).query)
    raw_node = (query.get("node-id") or query.get("node_id") or [None])[0]
    if raw_node:
        node_id = raw_node.replace("-", ":") if ":" not in raw_node else raw_node
    return file_key, node_id


class FigmaService:
    """Authenticated Figma REST client bound to a single PAT."""

    def __init__(self, token: str):
        if not token:
            raise FigmaError(401, "FORBIDDEN", "缺少 Figma 访问令牌")
        self._token = token

    def _headers(self) -> Dict[str, str]:
        return {"X-Figma-Token": self._token}

    def _get(self, path: str, *, params: Optional[dict] = None) -> dict:
        url = f"{FIGMA_API_BASE}{path}"
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                resp = client.get(url, headers=self._headers(), params=params)
        except httpx.TimeoutException as exc:
            raise FigmaError(504, "SERVER_ERROR", "请求 Figma 超时,请稍后重试") from exc
        except httpx.HTTPError as exc:
            logger.warning("Figma request failed: %s", exc)
            raise FigmaError(502, "SERVER_ERROR", "连接 Figma 失败") from exc
        return self._handle(resp)

    @staticmethod
    def _handle(resp: httpx.Response) -> dict:
        if resp.status_code == 200:
            try:
                return resp.json()
            except ValueError as exc:
                raise FigmaError(502, "SERVER_ERROR", "Figma 返回了无法解析的响应") from exc
        if resp.status_code in (401, 403):
            raise FigmaError(403, "FORBIDDEN", "Figma 令牌无效或无权访问该文件")
        if resp.status_code == 404:
            raise FigmaError(404, "NOT_FOUND", "Figma 文件不存在或无访问权限")
        if resp.status_code == 429:
            raise FigmaError(429, "RATE_LIMITED", "Figma 接口请求过于频繁,请稍后重试")
        logger.warning("Figma API error %s: %s", resp.status_code, resp.text[:200])
        raise FigmaError(502, "SERVER_ERROR", f"Figma 接口返回错误({resp.status_code})")

    # --- endpoints -----------------------------------------------------------
    def validate_token(self) -> dict:
        """Probe ``GET /v1/me`` to verify the PAT (used when saving credentials)."""
        return self._get("/v1/me")

    def get_file(self, file_key: str, *, depth: Optional[int] = None) -> dict:
        params = {"depth": depth} if depth else None
        return self._get(f"/v1/files/{file_key}", params=params)

    def get_nodes(self, file_key: str, node_ids: List[str]) -> dict:
        """``GET /v1/files/:key/nodes`` — full subtree(s) for the given node ids."""
        if not node_ids:
            raise FigmaError(400, "VALIDATION_ERROR", "缺少要拉取的 Figma 节点")
        return self._get(f"/v1/files/{file_key}/nodes", params={"ids": ",".join(node_ids)})

    def get_image_urls(
        self, file_key: str, node_ids: List[str], *, scale: float = 2.0, fmt: str = "png"
    ) -> Dict[str, Optional[str]]:
        """``GET /v1/images/:key`` — returns {node_id: signed_url|None}.

        The URLs are short-lived; download them immediately (see ``download_image``).
        """
        data = self._get(
            f"/v1/images/{file_key}",
            params={"ids": ",".join(node_ids), "scale": scale, "format": fmt},
        )
        images = data.get("images")
        return images if isinstance(images, dict) else {}

    @staticmethod
    def download_image(url: str) -> bytes:
        """Fetch the bytes behind a (short-lived) Figma-rendered image URL."""
        if not url:
            raise FigmaError(404, "NOT_FOUND", "Figma 渲染图地址为空")
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                resp = client.get(url)
        except httpx.HTTPError as exc:
            raise FigmaError(502, "SERVER_ERROR", "下载 Figma 渲染图失败") from exc
        if resp.status_code != 200 or not resp.content:
            raise FigmaError(502, "SERVER_ERROR", "下载 Figma 渲染图失败")
        return resp.content


def extract_first_frame_node(nodes_response: dict, requested_id: Optional[str]) -> Tuple[str, dict]:
    """From a ``/v1/files/:key/nodes`` response pick the node to convert.

    Returns ``(node_id, node_document_dict)``. Prefers the explicitly requested id;
    otherwise the first returned node.
    """
    nodes = nodes_response.get("nodes") if isinstance(nodes_response, dict) else None
    if not isinstance(nodes, dict) or not nodes:
        raise FigmaError(404, "NOT_FOUND", "Figma 文件中没有可用的节点")
    if requested_id and requested_id in nodes:
        chosen_id = requested_id
    else:
        chosen_id = next(iter(nodes.keys()))
    document = (nodes.get(chosen_id) or {}).get("document")
    if not isinstance(document, dict):
        raise FigmaError(404, "NOT_FOUND", "无法读取 Figma 节点内容")
    return chosen_id, document
