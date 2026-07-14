"""Testes do envelope encryption (AES-256-GCM)."""

from __future__ import annotations

import base64
import os

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("AEGIS_MASTER_KEY", key)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_roundtrip():
    from app.crypto import decrypt_secret, encrypt_secret

    secret = "sk-super-secreta-123"
    enc = encrypt_secret(secret)
    # o texto cifrado não contém o segredo em claro
    assert secret.encode() not in enc.ciphertext
    assert decrypt_secret(enc.ciphertext, enc.nonce, enc.dek_wrapped) == secret


def test_empty_secret_is_valid():
    from app.crypto import decrypt_secret, encrypt_secret

    enc = encrypt_secret("")
    assert decrypt_secret(enc.ciphertext, enc.nonce, enc.dek_wrapped) == ""


def test_unique_ciphertext_per_call():
    from app.crypto import encrypt_secret

    a = encrypt_secret("mesma-coisa")
    b = encrypt_secret("mesma-coisa")
    assert a.ciphertext != b.ciphertext  # nonce/DEK aleatórios


def test_is_configured_false_with_invalid_key(monkeypatch):
    # Chave com tamanho errado (16 bytes) → inválida (env var tem precedência sobre .env).
    monkeypatch.setenv("AEGIS_MASTER_KEY", base64.b64encode(os.urandom(16)).decode())
    get_settings.cache_clear()
    from app.crypto import is_configured

    assert is_configured() is False
