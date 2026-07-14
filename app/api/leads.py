"""Captação de leads: endpoint público do formulário de interesse da landing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
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
    await enforce_minute(f"lead:{ip}", 10, label="envios")  # anti-spam simples

    email = (body.email or "").strip()
    name = (body.name or "").strip()
    if not email or "@" not in email or not name:
        raise HTTPException(status_code=400, detail="Informe nome e e-mail válidos.")

    lead = Lead(
        name=name[:255],
        email=email[:255],
        company=(body.company or "").strip()[:255] or None,
        message=(body.message or "").strip()[:2000] or None,
        monthly_spend=(body.monthly_spend or "").strip()[:50] or None,
        source="landing",
    )
    db.add(lead)
    await db.commit()
    return {"status": "ok", "message": "Obrigado! Entraremos em contato."}
