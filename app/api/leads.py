"""Captação de leads: endpoint público do formulário de interesse da landing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Lead
from app.db.session import get_db
from app.ratelimit import enforce_minute

from .schemas import LeadCreate

router = APIRouter(prefix="/v1", tags=["leads"])


@router.post("/leads", status_code=201)
async def create_lead(
    body: LeadCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Registra um lead do formulário de interesse (público, com rate limit por IP)."""
    ip = request.client.host if request.client else "anon"
    # anti-spam best-effort: se o Redis estiver indisponível, NÃO bloqueia o lead
    # (formulário público jamais deve dar 503 por causa do rate limiter).
    await enforce_minute(f"lead:{ip}", 10, label="envios", fail_open=True)

    email = (body.email or "").strip().lower()
    name = (body.name or "").strip()
    if not email or "@" not in email or not name:
        raise HTTPException(status_code=400, detail="Informe nome e e-mail válidos.")

    company = (body.company or "").strip()[:255] or None
    message = (body.message or "").strip()[:2000] or None
    monthly_spend = (body.monthly_spend or "").strip()[:50] or None

    # Um lead por e-mail: se já existe, atualiza os dados (sem duplicar).
    existing = (
        await db.execute(select(Lead).where(Lead.email == email))
    ).scalars().first()
    if existing is not None:
        existing.name = name[:255]
        existing.company = company
        existing.message = message
        existing.monthly_spend = monthly_spend
    else:
        db.add(
            Lead(
                name=name[:255],
                email=email[:255],
                company=company,
                message=message,
                monthly_spend=monthly_spend,
                source="landing",
            )
        )
    await db.commit()
    return {"status": "ok", "message": "Obrigado! Entraremos em contato."}
