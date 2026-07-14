"""Agregação de uso por LLM + comparação de custo (economia auditável).

O custo é recomputado a partir dos tokens usando o catálogo de preços atual — assim,
ajustar o catálogo corrige os valores retroativamente.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UsageLog
from app.routing.pricing import cost_usd, price_of


async def key_month_spend(db: AsyncSession, api_key_id: uuid.UUID) -> float:
    """Gasto (USD) atribuído a uma chave no mês corrente."""
    now = datetime.now(UTC)
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    stmt = select(func.coalesce(func.sum(UsageLog.cost_usd), 0)).where(
        UsageLog.api_key_id == api_key_id, UsageLog.ts >= start
    )
    return float((await db.execute(stmt)).scalar() or 0)


async def recent_logs(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    limit: int = 50,
    offset: int = 0,
    model: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """Logs de requisição recentes do tenant (para a aba de observabilidade)."""
    stmt = select(UsageLog).where(UsageLog.tenant_id == tenant_id)
    if model:
        stmt = stmt.where(UsageLog.model_used == model)
    if status:
        stmt = stmt.where(UsageLog.status == status)
    stmt = stmt.order_by(UsageLog.ts.desc()).limit(min(limit, 200)).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "request_id": r.request_id,
            "ts": r.ts.isoformat() if r.ts else None,
            "provider": r.provider,
            "model_used": r.model_used,
            "status": r.status,
            "cache_hit": r.cache_hit,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "cost_usd": float(r.cost_usd),
            "latency_ms": r.latency_ms,
            "prompt_preview": r.prompt_preview,
            "response_preview": r.response_preview,
        }
        for r in rows
    ]


async def usage_summary(db: AsyncSession, tenant_id: uuid.UUID) -> dict:
    stmt = (
        select(
            UsageLog.provider,
            UsageLog.model_used,
            func.count().label("requests"),
            func.coalesce(func.sum(UsageLog.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(UsageLog.completion_tokens), 0).label("completion_tokens"),
            func.coalesce(func.sum(UsageLog.cost_saved_usd), 0).label("cost_saved"),
        )
        .where(UsageLog.tenant_id == tenant_id)
        .group_by(UsageLog.provider, UsageLog.model_used)
        .order_by(func.count().desc())
    )
    rows = (await db.execute(stmt)).all()

    per_model = []
    total_prompt = 0
    total_completion = 0
    total_requests = 0
    total_cost = 0.0
    total_saved = 0.0
    for provider, model, requests, prompt_tokens, completion_tokens, cost_saved in rows:
        prompt_tokens = int(prompt_tokens)
        completion_tokens = int(completion_tokens)
        cost = cost_usd(model, prompt_tokens, completion_tokens)
        inp, out = price_of(model)
        saved = float(cost_saved)
        per_model.append(
            {
                "provider": provider,
                "model": model,
                "requests": int(requests),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "cost_usd": round(cost, 6),
                "cost_saved_usd": round(saved, 6),
                "priced": (inp > 0 or out > 0),
            }
        )
        total_prompt += prompt_tokens
        total_completion += completion_tokens
        total_requests += int(requests)
        total_cost += cost
        total_saved += saved

    return {
        "per_model": per_model,
        "totals": {
            "requests": total_requests,
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "cost_usd": round(total_cost, 6),
            "cost_saved_usd": round(total_saved, 6),
        },
    }
