"""
GitHub App authentication.

Authenticates as an org-level GitHub App: sign a short-lived RS256 JWT with the
App private key, exchange it for an *installation access token* (valid ~1h), and
hand that token to ``GitHubClient``. Installation tokens are cached per
installation (refreshed shortly before expiry) behind a lock, mirroring the
thread-safe singleton pattern in ``services/ai/factory.py``.

Everything reads from ``os.getenv`` directly (not Flask app config) so it works
inside background workflow threads.
"""
import logging
import os
import threading
import time
from datetime import datetime
from typing import Optional, Tuple

import httpx
import jwt  # PyJWT (transitive via flask-jwt-extended)

from backend.services.code.github.client import GitHubError

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_API_VERSION = "2022-11-28"

# installation_id -> (token, expires_at_epoch)
_token_cache: dict[str, Tuple[str, float]] = {}
_cache_lock = threading.Lock()
# Cached (installation_id, account_login); resolved once, rarely changes.
_installation: Optional[Tuple[str, str]] = None


def api_base() -> str:
    return os.getenv("GITHUB_API_BASE", "https://api.github.com").rstrip("/")


def is_configured() -> bool:
    """True when an App id and a private key (inline or path) are present."""
    has_key = bool(os.getenv("GITHUB_APP_PRIVATE_KEY") or os.getenv("GITHUB_APP_PRIVATE_KEY_PATH"))
    return bool(os.getenv("GITHUB_APP_ID")) and has_key


def repo_owner(account_login: str) -> str:
    """The org/user repos are created under — explicit override or the install account."""
    return (os.getenv("GITHUB_REPO_OWNER") or account_login or "").strip()


def _resolve_private_key() -> str:
    path = os.getenv("GITHUB_APP_PRIVATE_KEY_PATH")
    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:
            raise GitHubError(500, "GITHUB_NOT_CONFIGURED", "无法读取 GitHub App 私钥文件") from exc
    key = os.getenv("GITHUB_APP_PRIVATE_KEY") or ""
    # Accept PEM with literal "\n" escapes (common when stored in a single env var).
    if "\\n" in key and "\n" not in key:
        key = key.replace("\\n", "\n")
    if not key.strip():
        raise GitHubError(500, "GITHUB_NOT_CONFIGURED", "未配置 GitHub App 私钥")
    return key


def _app_jwt() -> str:
    app_id = os.getenv("GITHUB_APP_ID")
    if not app_id:
        raise GitHubError(500, "GITHUB_NOT_CONFIGURED", "未配置 GITHUB_APP_ID")
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 540, "iss": app_id}  # exp < 10 min per GitHub
    try:
        return jwt.encode(payload, _resolve_private_key(), algorithm="RS256")
    except Exception as exc:  # noqa: BLE001 - bad key material
        raise GitHubError(500, "GITHUB_NOT_CONFIGURED", "GitHub App 私钥无效,无法签发 JWT") from exc


def _app_headers() -> dict:
    return {
        "Authorization": f"Bearer {_app_jwt()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _API_VERSION,
    }


def _app_request(method: str, path: str) -> dict:
    url = f"{api_base()}{path}"
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.request(method, url, headers=_app_headers())
    except httpx.HTTPError as exc:
        raise GitHubError(502, "GITHUB_API_ERROR", "连接 GitHub 失败") from exc
    if resp.status_code in (200, 201):
        return resp.json() if resp.content else {}
    if resp.status_code in (401, 403):
        raise GitHubError(403, "FORBIDDEN", "GitHub App 鉴权失败(检查 App ID / 私钥)")
    if resp.status_code == 404:
        raise GitHubError(404, "NOT_FOUND", "GitHub App 安装不存在")
    raise GitHubError(502, "GITHUB_API_ERROR", f"GitHub App 接口错误({resp.status_code})")


def resolve_installation(force: bool = False) -> Tuple[str, str]:
    """Return ``(installation_id, account_login)`` for the configured installation.

    Uses ``GITHUB_APP_INSTALLATION_ID`` when pinned, else the first installation.
    """
    global _installation
    if _installation and not force:
        return _installation

    pinned = os.getenv("GITHUB_APP_INSTALLATION_ID")
    if pinned:
        data = _app_request("GET", f"/app/installations/{pinned}")
        account = (data.get("account") or {}).get("login") or ""
        _installation = (str(pinned), account)
        return _installation

    installs = _app_request("GET", "/app/installations")
    if not isinstance(installs, list) or not installs:
        raise GitHubError(404, "GITHUB_NOT_CONFIGURED", "GitHub App 尚未安装到任何账户/组织")
    first = installs[0]
    _installation = (str(first.get("id")), (first.get("account") or {}).get("login") or "")
    return _installation


def get_installation_token(installation_id: str) -> str:
    """Return a cached installation access token, refreshing before it expires."""
    now = time.time()
    with _cache_lock:
        cached = _token_cache.get(installation_id)
        if cached and cached[1] - now > 120:
            return cached[0]

    data = _app_request("POST", f"/app/installations/{installation_id}/access_tokens")
    token = data.get("token")
    if not token:
        raise GitHubError(502, "GITHUB_API_ERROR", "GitHub 未返回 installation token")
    expires_at = _parse_expiry(data.get("expires_at"), fallback=now + 3000)
    with _cache_lock:
        _token_cache[installation_id] = (token, expires_at)
    return token


def _parse_expiry(value, *, fallback: float) -> float:
    if not value:
        return fallback
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").timestamp()
    except (ValueError, TypeError):
        return fallback


def reset_cache() -> None:
    """Clear cached tokens / installation (used by tests)."""
    global _installation
    with _cache_lock:
        _token_cache.clear()
    _installation = None
