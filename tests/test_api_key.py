"""Testes unitários da geração/verificação de chaves e parsing de bearer."""

from __future__ import annotations

import hmac

from app.auth.api_key import generate_api_key, hash_key
from app.auth.tokens import extract_bearer


def test_generate_api_key_shape() -> None:
    full, prefix, key_hash = generate_api_key()
    assert full.startswith("agf_")
    assert prefix.startswith("agf_")
    assert len(prefix) <= 16  # cabe na coluna key_prefix
    assert "." in full
    assert full.split(".", 1)[0] == prefix
    # o hash guardado corresponde à chave completa
    assert hmac.compare_digest(key_hash, hash_key(full))


def test_generate_api_key_is_unique() -> None:
    a, _, _ = generate_api_key()
    b, _, _ = generate_api_key()
    assert a != b


def test_wrong_key_does_not_match() -> None:
    full, _, key_hash = generate_api_key()
    assert not hmac.compare_digest(key_hash, hash_key(full + "x"))


def test_extract_bearer() -> None:
    assert extract_bearer("Bearer abc123") == "abc123"
    assert extract_bearer("bearer abc123") == "abc123"
    assert extract_bearer("  Bearer   tok  ") == "tok"
    assert extract_bearer(None) is None
    assert extract_bearer("") is None
    assert extract_bearer("Basic abc") is None
    assert extract_bearer("Bearer") is None
    assert extract_bearer("Bearer a b") is None
