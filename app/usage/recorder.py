"""Grava linhas em usage_logs (sessão própria, seguro em fluxo de streaming)."""

from __future__ import annotations

import uuid

from app.db.models import UsageLog
from app.db.session import SessionLocal


async def record_usage(
    *,
    tenant_id: uuid.UUID,
    request_id: str,
    provider: str,
    model_requested: str,
    model_used: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    cost_saved_usd: float = 0.0,
    cache_hit: bool = False,
    latency_ms: int = 0,
    api_key_id: uuid.UUID | None = None,
    status: str = "ok",
    prompt_preview: str | None = None,
    response_preview: str | None = None,
) -> None:
    async with SessionLocal() as session:
        session.add(
            UsageLog(
                tenant_id=tenant_id,
                api_key_id=api_key_id,
                request_id=request_id,
                provider=provider,
                model_requested=model_requested,
                model_used=model_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost_usd,
                cost_saved_usd=cost_saved_usd,
                cache_hit=cache_hit,
                status=status,
                latency_ms=latency_ms,
                prompt_preview=prompt_preview,
                response_preview=response_preview,
            )
        )
        await session.commit()
