"""Envelope encryption para chaves BYOK.

Cada segredo é cifrado com uma DEK (Data Encryption Key) aleatória via AES-256-GCM.
A DEK é, por sua vez, cifrada ("embrulhada") com a KEK (Key Encryption Key) mestra,
lida de `AEGIS_MASTER_KEY` (base64 de 32 bytes). Só a KEK vive em segredo/secret
manager; o banco guarda apenas material cifrado.

Layout persistido (todos bytes):
- `ciphertext`  = AESGCM(dek).encrypt(nonce, plaintext)
- `nonce`       = nonce de 12 bytes usado nos dados
- `dek_wrapped` = nonce_dek(12) || AESGCM(kek).encrypt(nonce_dek, dek)
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

_NONCE_LEN = 12
_KEY_LEN = 32


@dataclass(frozen=True)
class EncryptedSecret:
    ciphertext: bytes
    nonce: bytes
    dek_wrapped: bytes


def is_configured() -> bool:
    """True se há uma KEK válida configurada."""
    try:
        _load_kek()
        return True
    except ValueError:
        return False


def _load_kek() -> bytes:
    raw = get_settings().master_key
    if not raw:
        raise ValueError("AEGIS_MASTER_KEY não configurada")
    try:
        kek = base64.b64decode(raw)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("AEGIS_MASTER_KEY não é base64 válido") from exc
    if len(kek) != _KEY_LEN:
        raise ValueError("AEGIS_MASTER_KEY deve ter 32 bytes (base64) após decodificar")
    return kek


def encrypt_secret(plaintext: str) -> EncryptedSecret:
    kek = _load_kek()
    dek = os.urandom(_KEY_LEN)
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), None)

    nonce_dek = os.urandom(_NONCE_LEN)
    wrapped = AESGCM(kek).encrypt(nonce_dek, dek, None)
    dek_wrapped = nonce_dek + wrapped
    return EncryptedSecret(ciphertext=ciphertext, nonce=nonce, dek_wrapped=dek_wrapped)


def decrypt_secret(ciphertext: bytes, nonce: bytes, dek_wrapped: bytes) -> str:
    kek = _load_kek()
    nonce_dek, wrapped = dek_wrapped[:_NONCE_LEN], dek_wrapped[_NONCE_LEN:]
    dek = AESGCM(kek).decrypt(nonce_dek, wrapped, None)
    plaintext = AESGCM(dek).decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")
