"""Plano de dados (auth por x-api-key).

Na Fase 1 expõe apenas /v1/whoami para validar autenticação, resolução de tenant e
rate limiting. O endpoint real /v1/chat/completions chega na Fase 2.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.api_key import ApiContext, get_api_context

router = APIRouter(prefix="/v1", tags=["proxy"])


@router.get("/whoami")
async def whoami(ctx: ApiContext = Depends(get_api_context)) -> dict:
    """Retorna o tenant resolvido a partir da x-api-key (após passar pelo rate limit)."""
    return {
        "tenant_id": str(ctx.tenant.id),
        "tenant_name": ctx.tenant.name,
        "plan": ctx.tenant.plan,
        "api_key": ctx.key.name,
    }
