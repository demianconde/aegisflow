"""Painel: gestão de uso e economia (auth Supabase)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.supabase import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.routing.pricing import catalog
from app.usage import recent_logs, usage_summary

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.get("/pricing")
async def get_pricing(_user: User = Depends(get_current_user)) -> dict:
    """Catálogo de preços (USD por 1M tokens) para o comparativo de economia."""
    return {"catalog": catalog()}


@router.get("/usage/summary")
async def get_usage_summary(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Tokens e custo por LLM (custo recomputado pelo catálogo atual)."""
    return await usage_summary(db, user.tenant_id)


@router.get("/logs")
async def get_logs(
    limit: int = 50,
    offset: int = 0,
    model: str | None = None,
    status: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Logs de requisição (observabilidade). Prévias só aparecem se NEXUS_LOG_CONTENT=true."""
    logs = await recent_logs(
        db, user.tenant_id, limit=limit, offset=offset, model=model, status=status
    )
    return {"logs": logs}
