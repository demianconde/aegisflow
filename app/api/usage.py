"""Painel: gestão de uso e economia (auth Supabase)."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
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


@router.get("/usage/export")
async def export_usage(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Relatório de economia auditável (CSV) — uso por LLM com custo real e economia."""
    summary = await usage_summary(db, user.tenant_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["provedor", "modelo", "requisicoes", "prompt_tokens", "completion_tokens",
         "custo_usd", "economia_usd"]
    )
    for m in summary["per_model"]:
        w.writerow([
            m["provider"], m["model"], m["requests"], m["prompt_tokens"],
            m["completion_tokens"], f"{m['cost_usd']:.6f}", f"{m['cost_saved_usd']:.6f}",
        ])
    t = summary["totals"]
    w.writerow([])
    w.writerow([
        "TOTAL", "", t["requests"], t["prompt_tokens"], t["completion_tokens"],
        f"{t['cost_usd']:.6f}", f"{t['cost_saved_usd']:.6f}",
    ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=aegisflow-economia.csv"},
    )


@router.get("/logs")
async def get_logs(
    limit: int = 50,
    offset: int = 0,
    model: str | None = None,
    status: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Logs de requisição (observabilidade). Prévias só aparecem se AEGIS_LOG_CONTENT=true."""
    logs = await recent_logs(
        db, user.tenant_id, limit=limit, offset=offset, model=model, status=status
    )
    return {"logs": logs}
