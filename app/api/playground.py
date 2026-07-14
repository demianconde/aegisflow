"""Playground do painel: testa prompts/rotas sem precisar de x-api-key (auth Supabase)."""

from __future__ import annotations

import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat import _plan, _service
from app.auth.supabase import get_current_user
from app.config import get_settings
from app.db.models import User
from app.db.session import get_db
from app.providers.service import ProviderError, now_ms
from app.routing.pricing import cost_usd
from app.security.net import validate_endpoint_async
from app.usage import record_usage

from .schemas import ChatCompletionRequest

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.post("/playground")
async def playground(
    body: ChatCompletionRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Executa uma completions (não-stream) para o tenant e retorna a rota escolhida."""
    settings = get_settings()
    body.stream = False
    plan = await _plan(db, user.tenant_id, body)

    base = {"messages": [m.model_dump() for m in body.messages]}
    if body.max_tokens is not None:
        base["max_tokens"] = body.max_tokens
    if body.temperature is not None:
        base["temperature"] = body.temperature

    started = now_ms()
    last_exc: Exception | None = None
    for att in plan.attempts:
        try:
            await validate_endpoint_async(att.record.base_url, settings.allow_private_endpoints)
            result = await _service(att).complete({**base, "model": att.model})
        except (ProviderError, httpx.HTTPError, ValueError) as exc:
            last_exc = exc
            continue
        pt, ct = result.usage.prompt_tokens, result.usage.completion_tokens
        model_used = result.usage.model or att.model
        await record_usage(
            tenant_id=user.tenant_id,
            api_key_id=None,
            request_id=uuid.uuid4().hex,
            provider=att.provider,
            model_requested=body.model,
            model_used=model_used,
            prompt_tokens=pt,
            completion_tokens=ct,
            cost_usd=cost_usd(model_used, pt, ct),
            latency_ms=now_ms() - started,
        )
        return {
            "content": result.content,
            "provider": att.provider,
            "model": model_used,
            "complexity": plan.complexity,
            "routed": plan.routed,
            "usage": {"prompt_tokens": pt, "completion_tokens": ct},
            "cost_usd": cost_usd(model_used, pt, ct),
        }

    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Falha ao executar: {str(last_exc)[:200]}",
    )
