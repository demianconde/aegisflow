"""Roteador de custo (aegis-auto) — local-first com escalonamento.

Política:
- **Local é o primeiro recurso**, para tarefas simples E complexas (é gratuito).
- Só quando o local **não dá conta** (falha/erro na chamada) é que escala para um
  modelo pago hospedado — o **premium** no caso de tarefa complexa, ou o hospedado
  mais barato no caso de tarefa simples.
- Sem provedor local, escolhe direto o hospedado adequado ao tier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from app.providers.registry import KNOWN_PROVIDERS, is_local_url
from app.routing.pricing import price_of

_HIGH_PATTERN = re.compile(
    r"arquitetura|projete|projeto completo|design de sistema|infraestrutura|"
    r"planejamento|escalabilidade|microservi|multiusu|sistema completo|"
    r"refatore o sistema|migra[çc][aã]o|prova matem|demonstra[çc][aã]o|"
    r"otimize a performance|threat model|modelagem de dados",
    re.IGNORECASE,
)
_HIGH_CHAR_THRESHOLD = 6000  # ~1500 tokens de entrada

PROVIDER_TIERS: dict[str, dict[str, str]] = {
    "openai": {"cheap": "gpt-4o-mini", "premium": "gpt-4o"},
    "anthropic": {"cheap": "claude-3-5-haiku", "premium": "claude-3-5-sonnet"},
    "google": {"cheap": "gemini-3.1-flash-lite", "premium": "gemini-2.5-pro"},
    "qwen": {"cheap": "qwen-turbo", "premium": "qwen-max"},
    "deepseek": {"cheap": "deepseek-chat", "premium": "deepseek-reasoner"},
    "mistral": {"cheap": "mistral-small", "premium": "mistral-large"},
    "groq": {"cheap": "llama-3.1-8b", "premium": "llama-3.3-70b"},
    "together": {"cheap": "llama-3.1-8b", "premium": "llama-3.1-70b"},
}


class _KeyLike(Protocol):
    provider: str
    base_url: str | None
    default_model: str | None


@dataclass
class Route:
    provider_key: _KeyLike
    model: str
    baseline_model: str
    complexity: str
    tier: str
    is_local: bool
    escalation: Route | None = None


def estimate_complexity(messages: list[dict]) -> str:
    text = " ".join(str(m.get("content", "")) for m in messages)
    if _HIGH_PATTERN.search(text) or len(text) > _HIGH_CHAR_THRESHOLD:
        return "high"
    return "low"


def _is_local(pk: _KeyLike) -> bool:
    spec = KNOWN_PROVIDERS.get(pk.provider)
    return bool(spec and spec.local) or is_local_url(pk.base_url)


def _tier_model(pk: _KeyLike, tier: str) -> str | None:
    if _is_local(pk):
        return pk.default_model
    tiers = PROVIDER_TIERS.get(pk.provider)
    if tiers:
        return tiers.get(tier) or pk.default_model
    return pk.default_model


def _best_hosted(keys: list[_KeyLike], tier: str) -> tuple[_KeyLike, str] | None:
    """Provedor hospedado mais barato para o tier."""
    cands: list[tuple[float, _KeyLike, str]] = []
    for pk in keys:
        if _is_local(pk):
            continue
        model = _tier_model(pk, tier)
        if model:
            inp, out = price_of(model)
            cands.append((inp + out, pk, model))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    return cands[0][1], cands[0][2]


def choose_route(complexity: str, provider_keys: list[_KeyLike]) -> Route | None:
    tier = "premium" if complexity == "high" else "cheap"

    local_keys = [pk for pk in provider_keys if _is_local(pk) and pk.default_model]
    hosted_tier = _best_hosted(provider_keys, tier)
    hosted_premium = _best_hosted(provider_keys, "premium")
    baseline_model = (hosted_premium or hosted_tier or (None, None))[1]

    if local_keys:
        lpk = local_keys[0]
        escalation = None
        if hosted_tier:
            e_pk, e_model = hosted_tier
            escalation = Route(
                provider_key=e_pk,
                model=e_model,
                baseline_model=e_model,
                complexity=complexity,
                tier=tier,
                is_local=False,
            )
        return Route(
            provider_key=lpk,
            model=lpk.default_model,
            baseline_model=baseline_model or lpk.default_model,
            complexity=complexity,
            tier=tier,
            is_local=True,
            escalation=escalation,
        )

    if hosted_tier:
        h_pk, h_model = hosted_tier
        return Route(
            provider_key=h_pk,
            model=h_model,
            baseline_model=baseline_model or h_model,
            complexity=complexity,
            tier=tier,
            is_local=False,
        )

    return None
