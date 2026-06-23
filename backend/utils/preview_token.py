"""
Minted, resource-scoped tokens for browser preview sessions.

A browser tab / iframe cannot send an ``Authorization`` header, so the preview
routes (``/preview/<pid>/``, ``/api/agent/runs/<id>/site/...``) and the deployed
backend reverse proxy (``/app/<pid>/api/...``) authenticate via a JWT pinned into
an httponly, path-scoped cookie. They used to reuse the user's *access* token for
that cookie — but the access token lives only 30 minutes, so a preview tab left
open longer than that broke: the deployed app's polling started returning 403 and
the static preview's relative assets stopped loading.

Instead, the one-shot ``?token=`` access token on entry is exchanged for a
dedicated, longer-lived preview token (``PREVIEW_TOKEN_TTL``, default 12h) scoped
to a single resource via a ``preview_scope`` claim — long enough to outlast a
working session, narrow enough that it grants nothing but that one preview. These
are still ordinary JWTs signed with the same key, so the existing ``decode_token``
reads ``sub`` unchanged; the scope claim is verified defense-in-depth on top of the
cookie's path-scope (a token minted for one project/run can't be replayed against
another). The main API reads JWTs from the Authorization header only
(``JWT_TOKEN_LOCATION`` defaults to ``["headers"]``), so these httponly cookies are
never accepted as credentials there even though they outlive the access token.
"""
import os
from datetime import timedelta

from flask_jwt_extended import create_access_token, decode_token

# Long enough to outlast a working preview session; overridable. The cookie's
# Max-Age is pinned to the same value so cookie and token expire together.
PREVIEW_TOKEN_TTL = int(os.getenv("CODE_PREVIEW_TOKEN_TTL", str(12 * 3600)))

# Custom JWT claim that pins a minted preview token to one resource.
_SCOPE_CLAIM = "preview_scope"


def mint_preview_token(user_id: str, scope: str) -> str:
    """A longer-lived JWT for one preview resource (scope e.g. ``project:<pid>``)."""
    return create_access_token(
        identity=user_id,
        expires_delta=timedelta(seconds=PREVIEW_TOKEN_TTL),
        additional_claims={_SCOPE_CLAIM: scope},
    )


def preview_identity(token: str, scope: str) -> str | None:
    """Owner id from a preview token (or a plain access token), else ``None``.

    Accepts either the one-shot ``?token=`` *access* token used on entry (which
    carries no ``preview_scope`` claim) or a minted preview token. A minted token
    must match ``scope`` — one minted for a different project/run is rejected.
    """
    if not token:
        return None
    try:
        claims = decode_token(token)
    except Exception:  # noqa: BLE001 - any decode failure is simply not authenticated
        return None
    token_scope = claims.get(_SCOPE_CLAIM)
    if token_scope is not None and token_scope != scope:
        return None
    return claims.get("sub")
