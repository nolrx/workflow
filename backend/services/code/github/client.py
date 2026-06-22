"""
GitHub REST client (Git Data API).

A thin ``httpx`` wrapper authenticated with a GitHub App *installation* token
(see ``app_auth``). It exposes just enough of the REST + Git Data API to
find-or-create a repository and push a full file snapshot as one commit.

All failures are normalised to ``GitHubError(status, code, message)`` so the
route / sync layers can map them onto the unified ``error_response`` vocabulary,
mirroring ``FigmaService``.
"""
import base64
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_API_VERSION = "2022-11-28"


class GitHubError(Exception):
    """A GitHub API / transport failure, pre-classified for the route layer."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status  # suggested HTTP status for the API response
        self.code = code  # error_response code string
        self.message = message


class GitHubClient:
    """Authenticated GitHub REST client bound to a single token."""

    def __init__(self, token: str, api_base: str = "https://api.github.com"):
        if not token:
            raise GitHubError(401, "GITHUB_NOT_CONFIGURED", "缺少 GitHub 访问令牌")
        self._token = token
        self._base = api_base.rstrip("/")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _API_VERSION,
        }

    def _request(self, method: str, path: str, *, json: Optional[dict] = None) -> httpx.Response:
        url = f"{self._base}{path}"
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                return client.request(method, url, headers=self._headers(), json=json)
        except httpx.TimeoutException as exc:
            raise GitHubError(504, "GITHUB_API_ERROR", "请求 GitHub 超时,请稍后重试") from exc
        except httpx.HTTPError as exc:
            logger.warning("GitHub request failed: %s", exc)
            raise GitHubError(502, "GITHUB_API_ERROR", "连接 GitHub 失败") from exc

    def _ok(self, resp: httpx.Response, *, expected=(200, 201)) -> dict:
        if resp.status_code in expected:
            try:
                return resp.json() if resp.content else {}
            except ValueError as exc:
                raise GitHubError(502, "GITHUB_API_ERROR", "GitHub 返回了无法解析的响应") from exc
        self._raise(resp)

    @staticmethod
    def _raise(resp: httpx.Response) -> None:
        detail = ""
        try:
            detail = (resp.json() or {}).get("message", "")
        except ValueError:
            detail = resp.text[:200]
        if resp.status_code in (401, 403):
            raise GitHubError(403, "FORBIDDEN", f"GitHub 令牌无效或权限不足:{detail}")
        if resp.status_code == 404:
            raise GitHubError(404, "NOT_FOUND", "GitHub 资源不存在或无访问权限")
        if resp.status_code == 422:
            raise GitHubError(422, "GITHUB_API_ERROR", f"GitHub 拒绝了请求:{detail}")
        if resp.status_code == 429:
            raise GitHubError(429, "RATE_LIMITED", "GitHub 接口请求过于频繁,请稍后重试")
        logger.warning("GitHub API error %s: %s", resp.status_code, detail)
        raise GitHubError(502, "GITHUB_API_ERROR", f"GitHub 接口返回错误({resp.status_code})")

    # --- repositories --------------------------------------------------------
    def get_repo(self, owner: str, repo: str) -> Optional[dict]:
        """Return repo metadata, or ``None`` when it does not exist."""
        resp = self._request("GET", f"/repos/{owner}/{repo}")
        if resp.status_code == 404:
            return None
        return self._ok(resp, expected=(200,))

    def create_org_repo(
        self, org: str, name: str, *, private: bool = True, description: str = ""
    ) -> dict:
        """Create a repo under an org (``POST /orgs/{org}/repos``). ``auto_init=False``
        leaves it empty so the first push can author the initial commit."""
        return self._ok(
            self._request(
                "POST",
                f"/orgs/{org}/repos",
                json={
                    "name": name,
                    "private": private,
                    "description": description[:350],
                    "auto_init": False,
                    "has_issues": True,
                },
            ),
            expected=(201,),
        )

    # --- git data ------------------------------------------------------------
    def get_ref(self, owner: str, repo: str, ref: str) -> Optional[dict]:
        """``GET /git/ref/{ref}`` (ref like ``heads/main``). ``None`` if the branch
        does not exist yet (empty repo)."""
        resp = self._request("GET", f"/repos/{owner}/{repo}/git/ref/{ref}")
        if resp.status_code in (404, 409):
            return None
        return self._ok(resp, expected=(200,))

    def get_commit(self, owner: str, repo: str, sha: str) -> dict:
        return self._ok(
            self._request("GET", f"/repos/{owner}/{repo}/git/commits/{sha}"), expected=(200,)
        )

    def create_blob(self, owner: str, repo: str, content: bytes) -> str:
        """Create a base64 blob and return its sha."""
        encoded = base64.b64encode(content or b"").decode("ascii")
        data = self._ok(
            self._request(
                "POST",
                f"/repos/{owner}/{repo}/git/blobs",
                json={"content": encoded, "encoding": "base64"},
            ),
            expected=(201,),
        )
        return data["sha"]

    def create_tree(self, owner: str, repo: str, tree: list[dict], base_tree: str = None) -> dict:
        payload: dict = {"tree": tree}
        if base_tree:
            payload["base_tree"] = base_tree
        return self._ok(
            self._request("POST", f"/repos/{owner}/{repo}/git/trees", json=payload),
            expected=(201,),
        )

    def create_commit(
        self, owner: str, repo: str, message: str, tree_sha: str, parents: list[str]
    ) -> dict:
        return self._ok(
            self._request(
                "POST",
                f"/repos/{owner}/{repo}/git/commits",
                json={"message": message, "tree": tree_sha, "parents": parents or []},
            ),
            expected=(201,),
        )

    def update_ref(self, owner: str, repo: str, ref: str, sha: str, *, force: bool = False) -> dict:
        """``PATCH /git/refs/{ref}`` (ref like ``heads/main``)."""
        return self._ok(
            self._request(
                "PATCH",
                f"/repos/{owner}/{repo}/git/refs/{ref}",
                json={"sha": sha, "force": force},
            ),
            expected=(200,),
        )

    def create_ref(self, owner: str, repo: str, ref: str, sha: str) -> dict:
        """``POST /git/refs`` (ref like ``refs/heads/main``)."""
        return self._ok(
            self._request(
                "POST",
                f"/repos/{owner}/{repo}/git/refs",
                json={"ref": ref, "sha": sha},
            ),
            expected=(201,),
        )
