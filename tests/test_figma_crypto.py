"""Unit tests for Figma PAT at-rest encryption."""
import importlib

import pytest


def _crypto(monkeypatch, key="unit-test-stable-key"):
    """Reload the crypto module with a fixed key so encrypt/decrypt are stable."""
    monkeypatch.setenv("FIGMA_TOKEN_KEY", key)
    import backend.services.code.figma.crypto as crypto

    return importlib.reload(crypto)


def test_encrypt_decrypt_roundtrip(monkeypatch):
    crypto = _crypto(monkeypatch)
    token = "figd_abc123_secret_value"
    cipher = crypto.encrypt_token(token)
    assert cipher != token  # actually encrypted
    assert crypto.decrypt_token(cipher) == token


def test_last4(monkeypatch):
    crypto = _crypto(monkeypatch)
    assert crypto.last4("figd_abcd1234") == "1234"
    assert crypto.last4("ab") == "ab"


def test_decrypt_with_rotated_key_raises(monkeypatch):
    crypto = _crypto(monkeypatch, key="key-one")
    cipher = crypto.encrypt_token("figd_token")

    # Rotate the key -> the old ciphertext must not silently decrypt.
    crypto2 = _crypto(monkeypatch, key="key-two-different")
    with pytest.raises(crypto2.FigmaTokenDecryptError):
        crypto2.decrypt_token(cipher)


def test_decrypt_garbage_raises(monkeypatch):
    crypto = _crypto(monkeypatch)
    with pytest.raises(crypto.FigmaTokenDecryptError):
        crypto.decrypt_token("not-a-valid-fernet-token")
