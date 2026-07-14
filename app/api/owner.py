"""Console do dono (/gestaonexus): assinaturas, receita e leads. Acesso restrito."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.supabase import require_owner
from app.billing.plans import get_plan
from app.db.models import Lead, Tenant, UsageLog
from app.db.session import get_db

from .schemas import LeadStatusUpdate

router = APIRouter(prefix="/v1/owner", tags=["owner"])


@router.get("/overview")
async def overview(
    _owner: dict = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    # Assinaturas por plano (a partir de tenants ativos).
    rows = (
        await db.execute(
            select(Tenant.plan, func.count())
            .where(Tenant.status == "active")
            .group_by(Tenant.plan)
        )
    ).all()
    by_plan = {plan: int(n) for plan, n in rows}
    mrr = sum(get_plan(plan).price_brl * n for plan, n in by_plan.items())
    paid = sum(n for plan, n in by_plan.items() if get_plan(plan).price_brl > 0)

    total_tenants = int((await db.execute(select(func.count()).select_from(Tenant))).scalar() or 0)
    total_leads = int((await db.execute(select(func.count()).select_from(Lead))).scalar() or 0)
    new_leads = int(
        (await db.execute(select(func.count()).where(Lead.status == "new"))).scalar() or 0
    )
    total_requests = int(
        (await db.execute(select(func.count()).select_from(UsageLog))).scalar() or 0
    )

    return {
        "mrr_brl": round(mrr, 2),
        "active_paid_subscriptions": paid,
        "total_tenants": total_tenants,
        "by_plan": by_plan,
        "total_leads": total_leads,
        "new_leads": new_leads,
        "total_requests": total_requests,
    }


@router.get("/subscriptions")
async def subscriptions(
    _owner: dict = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = (
        await db.execute(select(Tenant).order_by(Tenant.created_at.desc()).limit(200))
    ).scalars().all()
    return {
        "tenants": [
            {
                "id": str(t.id),
                "name": t.name,
                "plan": t.plan,
                "plan_label": get_plan(t.plan).label,
                "price_brl": get_plan(t.plan).price_brl,
                "status": t.status,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in rows
        ]
    }


@router.get("/leads")
async def list_leads(
    _owner: dict = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = (
        await db.execute(select(Lead).order_by(Lead.created_at.desc()).limit(500))
    ).scalars().all()
    return {
        "leads": [
            {
                "id": str(x.id),
                "name": x.name,
                "email": x.email,
                "company": x.company,
                "message": x.message,
                "monthly_spend": x.monthly_spend,
                "status": x.status,
                "created_at": x.created_at.isoformat() if x.created_at else None,
            }
            for x in rows
        ]
    }


@router.patch("/leads/{lead_id}")
async def update_lead(
    lead_id: uuid.UUID,
    body: LeadStatusUpdate,
    _owner: dict = Depends(require_owner),
    db: AsyncSession = Depends(get_db),
) -> dict:
    lead = await db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead não encontrado")
    lead.status = body.status
    await db.commit()
    return {"status": "ok"}
