"""Definição dos planos (fonte única de limites e preços)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    key: str
    label: str
    price_brl: float
    rpm: int  # requisições por minuto (rate limit) — teto de vazão do TENANT
    monthly_quota: int  # requisições por mês
    max_api_keys: int  # nº máximo de chaves agf_ ativas (anti-abuso)
    features: tuple[str, ...]


PLANS: dict[str, Plan] = {
    "free": Plan(
        key="free",
        label="Hobby",
        price_brl=0.0,
        rpm=60,
        monthly_quota=10_000,
        max_api_keys=2,
        features=("Roteamento por custo", "Cache semântico", "Cofre BYOK cifrado"),
    ),
    "pro": Plan(
        key="pro",
        label="Pro",
        price_brl=249.0,
        rpm=600,
        monthly_quota=500_000,
        max_api_keys=10,
        features=(
            "Roteamento avançado + fallback",
            "Multi-provider (qualquer LLM/local)",
            "Relatório de economia exportável (CSV)",
        ),
    ),
    "enterprise": Plan(
        key="enterprise",
        label="Teams",
        price_brl=999.0,
        rpm=6_000,
        monthly_quota=5_000_000,
        max_api_keys=50,
        features=(
            "Tudo do Pro",
            "Chaves virtuais (orçamento e limite por chave)",
            "Guardrails + redação de PII (LGPD)",
            "Logs de requisições",
        ),
    ),
}

DEFAULT_PLAN = "free"


def get_plan(key: str | None) -> Plan:
    return PLANS.get(key or DEFAULT_PLAN, PLANS[DEFAULT_PLAN])
