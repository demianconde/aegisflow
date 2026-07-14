"""Painel: gestão das credenciais BYOK de provedores de LLM (qualquer LLM/local)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.supabase import get_current_user
from app.config import get_settings
from app.crypto import encrypt_secret, is_configured
from app.db.models import ProviderKey, User
from app.db.session import get_db
from app.providers.registry import KNOWN_PROVIDERS, is_local_url, resolve_endpoint
from app.security.net import validate_endpoint_async

from .schemas import ProviderKeyCreate, ProviderKeyInfo

router = APIRouter(prefix="/v1/admin", tags=["admin"])


def _to_info(record: ProviderKey) -> ProviderKeyInfo:
    spec = KNOWN_PROVIDERS.get(record.provider)
    local = (spec.local if spec else False) or is_local_url(record.base_url)
    provider_label = spec.label if spec else record.provider
    return ProviderKeyInfo(
        id=record.id,
        provider=record.provider,
        provider_label=provider_label,
        format=record.format,
        base_url=record.base_url,
        label=record.label,
        default_model=record.default_model,
        is_local=local,
        created_at=record.created_at,
    )


@router.get("/providers")
async def list_known_providers(_user: User = Depends(get_current_user)) -> dict:
    """Provedores conhecidos (atalhos de base_url/format) para o painel."""
    return {
        "providers": [
            {
                "key": s.key,
                "label": s.label,
                "format": s.format,
                "default_base_url": s.default_base_url,
                "requires_key": s.requires_key,
                "local": s.local,
            }
            for s in KNOWN_PROVIDERS.values()
        ]
    }


@router.get("/provider-keys", response_model=list[ProviderKeyInfo])
async def list_provider_keys(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProviderKeyInfo]:
    result = await db.execute(
        select(ProviderKey)
        .where(ProviderKey.tenant_id == user.tenant_id)
        .order_by(ProviderKey.created_at.desc())
    )
    return [_to_info(k) for k in result.scalars().all()]


@router.post("/provider-keys", response_model=ProviderKeyInfo, status_code=status.HTTP_201_CREATED)
async def create_provider_key(
    body: ProviderKeyCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProviderKeyInfo:
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Criptografia não configurada (defina AEGIS_MASTER_KEY).",
        )
    # Valida provedor/base_url/format.
    try:
        fmt, base_url = resolve_endpoint(body.provider, body.format, body.base_url)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Anti-SSRF: bloqueia endpoints em rede privada/local (salvo self-host).
    try:
        await validate_endpoint_async(base_url, get_settings().allow_private_endpoints)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    enc = encrypt_secret(body.api_key or "")  # chave vazia é válida (endpoints locais)
    record = ProviderKey(
        tenant_id=user.tenant_id,
        provider=body.provider,
        format=fmt,
        base_url=base_url,
        label=body.label,
        default_model=body.default_model,
        ciphertext=enc.ciphertext,
        nonce=enc.nonce,
        dek_wrapped=enc.dek_wrapped,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _to_info(record)


@router.delete(
    "/provider-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_provider_key(
    key_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    record = await db.get(ProviderKey, key_id)
    if record is None or record.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chave não encontrada")
    await db.delete(record)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
