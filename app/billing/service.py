"""Serviço de billing: períodos, upgrade proporcional, downgrade no vencimento.

Regras de mudança de plano:
- **Upgrade** (mais barato → mais caro): entra em vigor **imediatamente**, com cobrança
  **proporcional ao tempo restante** do período; o vencimento é mantido.
- **Downgrade** (mais caro já pago → mais barato): o novo preço **conta a partir do
  vencimento** do período atual (fica agendado).

Stripe é opcional (lazy). Sem Stripe, aplica em modo dev para desenvolvimento/testes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.plans import PLANS, get_plan
from app.config import get_settings
from app.db.models import Subscription, Tenant, UsageLog

PERIOD_DAYS = 30


def _now() -> datetime:
    # Naive UTC (as colunas no SQLite voltam sem tz; mantém comparação consistente).
    return datetime.utcnow()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


async def monthly_request_count(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    start = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    stmt = select(func.count()).where(UsageLog.tenant_id == tenant_id, UsageLog.ts >= start)
    return int((await db.execute(stmt)).scalar() or 0)


async def get_or_create_subscription(db: AsyncSession, tenant: Tenant) -> Subscription:
    result = await db.execute(select(Subscription).where(Subscription.tenant_id == tenant.id))
    sub = result.scalars().first()
    if sub is None:
        sub = Subscription(tenant_id=tenant.id, plan=tenant.plan or "free", status="active")
        db.add(sub)
        await db.flush()
    return sub


async def _apply_due(db: AsyncSession, tenant: Tenant, sub: Subscription) -> None:
    """Se o período venceu e há downgrade agendado, aplica o novo plano."""
    if sub.current_period_end and _now() >= sub.current_period_end:
        if sub.pending_plan:
            sub.plan = sub.pending_plan
            sub.pending_plan = None
            sub.current_period_start = sub.current_period_end
            sub.current_period_end = sub.current_period_end + timedelta(days=PERIOD_DAYS)
            tenant.plan = sub.plan
            await db.commit()


async def change_plan(db: AsyncSession, tenant: Tenant, new_key: str) -> dict:
    """Aplica a mudança de plano seguindo as regras de proração. Retorna o efeito."""
    if new_key not in PLANS:
        raise ValueError(f"Plano inválido: {new_key}")
    sub = await get_or_create_subscription(db, tenant)
    await _apply_due(db, tenant, sub)

    cur, nw = get_plan(sub.plan), get_plan(new_key)
    now = _now()
    active_paid = bool(
        sub.current_period_end and now < sub.current_period_end and cur.price_brl > 0
    )

    # Sem período pago ativo (free ou expirado): começa agora.
    if not active_paid:
        sub.plan = new_key
        sub.pending_plan = None
        sub.current_period_start = now
        sub.current_period_end = now + timedelta(days=PERIOD_DAYS)
        tenant.plan = new_key
        await db.commit()
        return {
            "effect": "immediate",
            "charge_brl": round(nw.price_brl, 2),
            "period_end": _iso(sub.current_period_end),
        }

    if nw.price_brl > cur.price_brl:
        # UPGRADE: imediato, cobrança proporcional ao tempo restante; mantém vencimento.
        total = (sub.current_period_end - (sub.current_period_start or now)).total_seconds()
        remaining = max(0.0, (sub.current_period_end - now).total_seconds())
        frac = (remaining / total) if total > 0 else 0.0
        charge = (nw.price_brl - cur.price_brl) * frac
        sub.plan = new_key
        sub.pending_plan = None
        tenant.plan = new_key
        await db.commit()
        return {
            "effect": "upgrade_immediate",
            "charge_brl": round(charge, 2),
            "period_end": _iso(sub.current_period_end),
        }

    if nw.price_brl < cur.price_brl:
        # DOWNGRADE: agenda para o vencimento (o pago atual vale até lá).
        sub.pending_plan = new_key
        await db.commit()
        return {
            "effect": "scheduled",
            "starts_at": _iso(sub.current_period_end),
            "charge_brl": round(nw.price_brl, 2),
        }

    # Mesmo preço/plano: cancela downgrade agendado, se houver.
    sub.pending_plan = None
    await db.commit()
    return {"effect": "none"}


async def set_plan(db: AsyncSession, tenant: Tenant, plan_key: str) -> None:
    """Atalho usado pelo webhook do Stripe: aplica o plano imediatamente."""
    await change_plan(db, tenant, plan_key)


async def billing_state(db: AsyncSession, tenant: Tenant) -> dict:
    """Estado de cobrança para o painel (plano, vigência, plano agendado)."""
    sub = await get_or_create_subscription(db, tenant)
    await _apply_due(db, tenant, sub)
    pending = get_plan(sub.pending_plan) if sub.pending_plan else None
    return {
        "period_start": _iso(sub.current_period_start),
        "period_end": _iso(sub.current_period_end),
        "pending_plan": sub.pending_plan,
        "pending_plan_label": pending.label if pending else None,
    }


def stripe_enabled() -> bool:
    return bool(get_settings().stripe_secret_key)


async def create_checkout(tenant: Tenant, plan_key: str, success_url: str, cancel_url: str) -> dict:
    """Cria uma sessão de checkout Stripe (Pix/boleto/cartão) ou responde em modo dev."""
    plan = get_plan(plan_key)
    if not stripe_enabled():
        return {
            "mode": "dev",
            "checkout_url": None,
            "message": "Stripe não configurado (modo dev).",
        }

    import stripe  # lazy: só quando configurado

    settings = get_settings()
    stripe.api_key = settings.stripe_secret_key
    methods = [m.strip() for m in settings.stripe_payment_methods.split(",") if m.strip()]
    meta = {"tenant_id": str(tenant.id), "plan": plan_key}
    session = stripe.checkout.Session.create(
        mode="subscription",
        payment_method_types=methods or ["card"],
        line_items=[
            {
                "price_data": {
                    "currency": "brl",
                    "product_data": {"name": f"AegisFlow {plan.label}"},
                    "unit_amount": int(plan.price_brl * 100),
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }
        ],
        client_reference_id=str(tenant.id),
        metadata=meta,
        subscription_data={"metadata": meta},
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return {"mode": "stripe", "checkout_url": session.url}
