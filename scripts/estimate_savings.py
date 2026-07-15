"""Estimativa de economia — Nível 1 (offline, custo zero).

Passa um corpus de prompts pelo roteador do AegisFlow e compara o custo do
cenário ROTEADO (cada prompt no modelo mais barato do tier que o roteador
escolheu) contra um BASELINE (o cliente rodaria tudo num único modelo).

IMPORTANTE — o que este script NÃO faz (seja honesto ao usar o número):
- NÃO valida se a resposta do modelo barato foi boa o suficiente (isso é o
  Nível 3, shadow-mode + juiz de qualidade). O número aqui assume que a
  qualidade se mantém — é uma estimativa DIRECIONAL, não uma prova comercial.
- NÃO usa tokens reais: estima prompt_tokens pelo tamanho do texto e
  completion_tokens por premissa fixa por tier (ver OUT_TOKENS). Ajuste conforme
  o seu caso de uso real.

Uso:
  python scripts/estimate_savings.py                         # dataset padrão (98 prompts), baseline gpt-4o
  python scripts/estimate_savings.py --baseline gpt-4o       # baseline configurável
  python scripts/estimate_savings.py --provider google       # roteia dentro de 1 provedor
  python scripts/estimate_savings.py --corpus prompts.txt    # 1 prompt por linha (ou .json com lista)
  python scripts/estimate_savings.py --cache-rate 0.2        # cenário extra: 20% de cache hit
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)  # para importar app.*

from app.routing.pricing import cost_usd, price_of  # noqa: E402
from app.routing.router import PROVIDER_TIERS, estimate_complexity  # noqa: E402

# Premissa de tokens de SAÍDA por tier (ajuste ao seu caso real).
OUT_TOKENS = {"low": 200, "medium": 600, "high": 1500}
# Estimativa grosseira de tokens de ENTRADA: ~4 caracteres por token.
CHARS_PER_TOKEN = 4
TIER_OF = {"low": "cheap", "medium": "mid", "high": "premium"}


def load_corpus(path: str | None) -> list[str]:
    """Carrega prompts de um arquivo (.txt = 1 por linha; .json = lista) ou usa o dataset padrão."""
    if path:
        with open(path, encoding="utf-8") as f:
            if path.endswith(".json"):
                data = json.load(f)
                return [x if isinstance(x, str) else x.get("content", "") for x in data]
            return [ln.strip() for ln in f if ln.strip()]
    # padrão: reaproveita o dataset rotulado do eval_router (só os textos)
    from eval_router import DATASET  # scripts/ está no sys.path ao rodar como script

    return [prompt for _label, prompt in DATASET]


def est_prompt_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def cheapest_for_tier(tier: str, only_provider: str | None) -> tuple[str, str] | None:
    """(modelo, provedor) mais barato para o tier, entre os provedores conhecidos.

    Ignora modelos sem preço no catálogo (evita economia fantasma de modelo "grátis").
    """
    cands: list[tuple[float, str, str]] = []
    for prov, tiers in PROVIDER_TIERS.items():
        if only_provider and prov != only_provider:
            continue
        model = tiers.get(tier)
        if not model:
            continue
        inp, out = price_of(model)
        if inp == 0 and out == 0:
            continue  # preço desconhecido → não conta
        cands.append((inp + out, model, prov))
    if not cands:
        return None
    cands.sort()
    return cands[0][1], cands[0][2]


def main() -> None:
    ap = argparse.ArgumentParser(description="Estimativa de economia Nível 1 (offline).")
    ap.add_argument("--baseline", default="gpt-4o", help="Modelo baseline (cliente rodaria tudo nele).")
    ap.add_argument("--provider", default=None, help="Restringe o roteamento a um provedor (ex.: google).")
    ap.add_argument("--corpus", default=None, help="Arquivo de prompts (.txt/.json). Padrão: dataset de 98.")
    ap.add_argument("--cache-rate", type=float, default=0.0, help="Cenário extra: fração de cache hit (0-1).")
    args = ap.parse_args()

    prompts = load_corpus(args.corpus)
    if not prompts:
        print("Corpus vazio.")
        return

    base_inp, base_out = price_of(args.baseline)
    if base_inp == 0 and base_out == 0:
        print(f"AVISO: baseline '{args.baseline}' sem preço no catálogo — resultado inválido.")
        return

    total_routed = 0.0
    total_baseline = 0.0
    by_tier: dict[str, int] = {"low": 0, "medium": 0, "high": 0}
    by_model: dict[str, dict] = {}
    unpriced = 0

    for p in prompts:
        messages = [{"role": "user", "content": p}]
        complexity = estimate_complexity(messages)  # low/medium/high
        by_tier[complexity] += 1
        tier = TIER_OF[complexity]

        pick = cheapest_for_tier(tier, args.provider)
        if not pick:
            unpriced += 1
            continue
        routed_model, _prov = pick

        pt = est_prompt_tokens(p)
        ct = OUT_TOKENS[complexity]

        c_routed = cost_usd(routed_model, pt, ct)
        c_base = cost_usd(args.baseline, pt, ct)
        total_routed += c_routed
        total_baseline += c_base

        m = by_model.setdefault(routed_model, {"n": 0, "cost": 0.0})
        m["n"] += 1
        m["cost"] += c_routed

    n = len(prompts) - unpriced
    saved = total_baseline - total_routed
    pct = (saved / total_baseline * 100) if total_baseline else 0.0

    # Cenário com cache (opcional): cache hit → custo roteado 0 naquela fração.
    routed_cache = total_routed * (1 - args.cache_rate)
    saved_cache = total_baseline - routed_cache
    pct_cache = (saved_cache / total_baseline * 100) if total_baseline else 0.0

    print("=" * 64)
    print(" AegisFlow — Estimativa de economia (Nível 1, offline)")
    print("=" * 64)
    print(f" Corpus:           {len(prompts)} prompts ({n} precificados, {unpriced} ignorados)")
    print(f" Baseline:         {args.baseline}  (US$ {base_inp}/{base_out} por 1M tok in/out)")
    print(f" Roteamento:       {'provedor ' + args.provider if args.provider else 'mais barato entre provedores'}")
    print(f" Premissa saída:   low={OUT_TOKENS['low']} / medium={OUT_TOKENS['medium']} / high={OUT_TOKENS['high']} tok")
    print(f" Premissa entrada: ~{CHARS_PER_TOKEN} chars/token (estimado do texto)")
    print("-" * 64)
    print(" Distribuição por complexidade:")
    for lvl in ("low", "medium", "high"):
        c = by_tier[lvl]
        print(f"   {lvl:7} {c:4}  ({c / len(prompts) * 100:4.1f}%)")
    print("-" * 64)
    print(" Modelos escolhidos pelo roteador:")
    for model, d in sorted(by_model.items(), key=lambda x: -x[1]["cost"]):
        print(f"   {model:24} {d['n']:4} req   US$ {d['cost']:.4f}")
    print("-" * 64)
    print(f" Custo BASELINE (tudo em {args.baseline}):  US$ {total_baseline:.4f}")
    print(f" Custo ROTEADO (Aegis):                   US$ {total_routed:.4f}")
    print(f" ECONOMIA só com roteamento:              US$ {saved:.4f}  ({pct:.1f}%)")
    if args.cache_rate > 0:
        print(f" ECONOMIA com +{args.cache_rate*100:.0f}% cache hit:            "
              f"US$ {saved_cache:.4f}  ({pct_cache:.1f}%)")
    print("=" * 64)
    print(" AVISO: estimativa DIRECIONAL. Não valida qualidade das respostas")
    print(" nem usa tokens reais. Para número que fecha venda, use o Nível 3")
    print(" (shadow-mode + juiz de qualidade sobre tráfego real).")
    print("=" * 64)


if __name__ == "__main__":
    main()
