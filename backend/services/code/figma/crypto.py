"""
At-rest encryption for the user's Figma personal access token (PAT).

The PAT is a long-lived credential, so it is never stored in plaintext. We use
Fernet (AES-128-CBC + HMAC) with a key resolved as:

1. ``FIGMA_TOKEN_KEY`` — a stable, explicitly-provisioned 32-byte urlsafe-base64
   Fernet key (recommended for any environment that must persist tokens across
   restarts).
2. else derived from ``SECRET_KEY`` via SHA-256.

CAVEAT: in development ``SECRET_KEY`` defaults to ``os.urandom(...)`` on every
restart (see ``config.DevelopmentConfig``), so a derived key changes each boot
and previously-stored ciphertext can no longer be decrypted. That is handled
gracefully — ``decrypt_token`` raises ``FigmaTokenDecryptError`` and the caller
asks the user to re-paste the PAT — but to make tokens durable, set
``FIGMA_TOKEN_KEY``.

Read keys from ``os.getenv`` directly (not Flask app config) so this works inside
background workflow threads.
"""
import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


class FigmaTokenDecryptError(Exception):
    """Raised when a stored token cannot be decrypted (key rotated / corrupted)."""


def _resolve_key() -> bytes:
    """Return a valid 32-byte urlsafe-base64 Fernet key from the environment."""
    explicit = os.getenv("FIGMA_TOKEN_KEY")
    if explicit:
        # Accept a ready-made Fernet key as-is; fall back to deriving from it if
        # it is not already a valid 32-byte urlsafe-base64 key.
        try:
            Fernet(explicit.encode("utf-8"))
            return explicit.encode("utf-8")
        except (ValueError, TypeError):
            return _derive_key(explicit)
    secret = os.getenv("SECRET_KEY") or "dev-insecure-figma-fallback-secret"
    return _derive_key(secret)


def _derive_key(material: str) -> bytes:
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_token(plaintext: str) -> str:
    """Encrypt a PAT for storage. Returns urlsafe-base64 ciphertext text."""
    if not plaintext:
        raise ValueError("Cannot encrypt an empty token")
    token = Fernet(_resolve_key()).encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a stored PAT. Raises ``FigmaTokenDecryptError`` if undecryptable."""
    if not ciphertext:
        raise FigmaTokenDecryptError("No stored token")
    try:
        return Fernet(_resolve_key()).decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise FigmaTokenDecryptError(
            "Stored Figma token could not be decrypted (encryption key changed)"
        ) from exc


def last4(token: str) -> str:
    """Return the last 4 chars for non-sensitive display (e.g. ``****ab12``)."""
    cleaned = (token or "").strip()
    return cleaned[-4:] if len(cleaned) >= 4 else cleaned
