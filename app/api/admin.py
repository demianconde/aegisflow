"""Endpoints do painel (auth Supabase): perfil e gestão de chaves de API."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_key import generate_api_key
from app.auth.supabase import get_current_user
from app.db.models import AegisApiKey, Tenant, User
from app.db.session import get_db

from .schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyInfo,
    ApiKeyLimits,
    GuardrailsConfig,
    MeResponse,
)

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.get("/me", response_model=MeResponse)
async def me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MeResponse:
    tenant = await db.get(Tenant, user.tenant_id)
    return MeResponse(
        user_id=user.id, tenant_id=user.tenant_id, role=user.role, plan=tenant.plan
    )


@router.get("/guardrails")
async def get_guardrails(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tenant = await db.get(Tenant, user.tenant_id)
    return {"pii": tenant.guardrail_pii, "blocked_terms": tenant.guardrail_blocked_terms or ""}


@router.put("/guardrails")
async def set_guardrails(
    body: GuardrailsConfig,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    tenant = await db.get(Tenant, user.tenant_id)
    tenant.guardrail_pii = bool(body.pii)
    tenant.guardrail_blocked_terms = (body.blocked_terms or "").strip() or None
    await db.commit()
    return {"pii": tenant.guardrail_pii, "blocked_terms": tenant.guardrail_blocked_terms or ""}


@router.get("/keys", response_model=list[ApiKeyInfo])
async def list_keys(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ApiKeyInfo]:
    result = await db.execute(
        select(AegisApiKey)
        .where(AegisApiKey.tenant_id == user.tenant_id)
        .order_by(AegisApiKey.created_at.desc())
    )
    return [ApiKeyInfo.model_validate(k) for k in result.scalars().all()]


@router.post("/keys", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: ApiKeyCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyCreated:
    full_key, prefix, key_hash = generate_api_key()
    record = AegisApiKey(
        tenant_id=user.tenant_id, key_prefix=prefix, key_hash=key_hash, name=body.name
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    # api_key em claro é retornada só aqui, na criação.
    return ApiKeyCreated(
        id=record.id,
        key_prefix=record.key_prefix,
        name=record.name,
        created_at=record.created_at,
        revoked_at=record.revoked_at,
        api_key=full_key,
    )


@router.patch("/keys/{key_id}", response_model=ApiKeyInfo)
async def update_key_limits(
    key_id: uuid.UUID,
    body: ApiKeyLimits,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyInfo:
    """Define limites da chave virtual: orçamento mensal, rpm e allowlist de modelos."""
    record = await db.get(AegisApiKey, key_id)
    if record is None or record.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chave não encontrada")
    record.monthly_budget_usd = body.monthly_budget_usd
    record.rpm_limit = body.rpm_limit
    record.allowed_models = (body.allowed_models or "").strip() or None
    await db.commit()
    await db.refresh(record)
    return ApiKeyInfo.model_validate(record)


@router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def revoke_key(
    key_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    record = await db.get(AegisApiKey, key_id)
    if record is None or record.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chave não encontrada")
    if record.revoked_at is None:
        record.revoked_at = func.now()
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
