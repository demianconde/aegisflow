"""Chaves de API do AegisFlow (x-api-key) usadas pelas apps clientes.

Formato: agf_<8 hex>.<segredo>. Guardamos apenas o prefixo (para lookup) e o
hash SHA-256 da chave completa. O valor em claro so eh mostrado uma vez, na criacao.

Suporta chaves virtuais: limites de rpm, orcamento mensal (USD) e allowlist de
modelos por chave.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.plans import get_plan
from app.db.models import AegisApiKey, Tenant
from app.db.session import get_db
from app.ratelimit import enforce_minute, enforce_monthly_quota

_PREFIX_NAMESPACE = "agf_"


@dataclass
class ApiContext:
    tenant: Tenant
    key: AegisApiKey


def generate_api_key() -> tuple[str, str, str]:
    """Gera (chave_completa, prefixo, hash). A chave completa nao eh persistida."""
    prefix = _PREFIX_NAMESPACE + secrets.token_hex(4)  # ex.: agf_a1b2c3d4 (12 chars)
    secret = secrets.token_urlsafe(32)
    full_key = f"{prefix}.{secret}"
    return full_key, prefix, hash_key(full_key)


def hash_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode("utf-8")).hexdigest()


def _parse_prefix(full_key: str) -> str | None:
    if not full_key.startswith(_PREFIX_NAMESPACE) or "." not in full_key:
        return None
    return full_key.split(".", 1)[0]


async def get_api_context(
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    db: AsyncSession = Depends(get_db),
) -> ApiContext:
    """Resolve tenant + chave da x-api-key e aplica limites (plano e chave virtual)."""
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Header x-api-key eh obrigatorio"
        )
    prefix = _parse_prefix(x_api_key)
    if not prefix:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chave de API invalida")

    result = await db.execute(
        select(AegisApiKey).where(
            AegisApiKey.key_prefix == prefix,
            AegisApiKey.revoked_at.is_(None),
        )
    )
    key = result.scalar_one_or_none()
    expected_hash = key.key_hash if key else "0" * 64
    if key is None or not hmac.compare_digest(expected_hash, hash_key(x_api_key)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chave de API invalida")

    tenant = await db.get(Tenant, key.tenant_id)
    if tenant is None or tenant.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant inativo")

    plan = get_plan(tenant.plan)
    # Teto de vazao do TENANT: o plano define a capacidade total; chaves virtuais
    # subdividem essa capacidade, nao a multiplicam. Sem isto, criar N chaves daria
    # N x rpm efetivo, driblando o limite do plano (anti-abuso).
    await enforce_minute(f"tenant:{tenant.id}", plan.rpm, "requisicoes do plano")
    # Rate limit por minuto da chave: chave virtual pode ter limite proprio (<= plano).
    effective_rpm = key.rpm_limit if key.rpm_limit is not None else plan.rpm
    await enforce_minute(f"key:{key.id}", effective_rpm)
    # Quota mensal de requisicoes (nivel tenant/plano).
    await enforce_monthly_quota(str(tenant.id), plan.monthly_quota, plan.label)

    return ApiContext(tenant=tenant, key=key)