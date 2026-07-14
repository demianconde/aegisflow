"""Smoke test do Stripe em homologação (test mode).

Valida a STRIPE_SECRET_KEY de teste criando uma sessão de Checkout de assinatura
(sem cobrar nada) e imprimindo a URL. Requer AEGIS_STRIPE_* no .env.

Uso:  python scripts/stripe_smoke.py [pro|enterprise]
"""

from __future__ import annotations

import sys

from app.billing.plans import get_plan
from app.config import get_settings


def main() -> None:
    plan_key = sys.argv[1] if len(sys.argv) > 1 else "pro"
    settings = get_settings()
    if not settings.stripe_secret_key:
        print("STRIPE_SECRET_KEY não configurada no .env (use sk_test_... em homologação).")
        raise SystemExit(1)
    if not settings.stripe_secret_key.startswith("sk_test_"):
        print("AVISO: a chave não parece ser de TESTE (sk_test_...). Homologação usa test mode.")

    import stripe

    stripe.api_key = settings.stripe_secret_key
    plan = get_plan(plan_key)
    methods = [m.strip() for m in settings.stripe_payment_methods.split(",") if m.strip()]
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
        metadata={"tenant_id": "smoke-test", "plan": plan_key},
        subscription_data={"metadata": {"tenant_id": "smoke-test", "plan": plan_key}},
        success_url="http://localhost:8000/dashboard",
        cancel_url="http://localhost:8000/dashboard",
    )
    print("OK — sessão de checkout de TESTE criada:")
    print("  plano:", plan.label, f"(R$ {plan.price_brl}/mês)")
    print("  métodos:", methods or ["card"])
    print("  URL:", session.url)


if __name__ == "__main__":
    main()
