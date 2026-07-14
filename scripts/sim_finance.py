"""Simulação: construir um sisteminha de finanças.

Compara dois cenários de custo usando TOKENS REAIS (medidos em chamadas de verdade
ao Gemini via AegisFlow) e o catálogo de preços:

  A) 100% num Gemini premium (gemini-2.5-pro) para todas as tarefas.
  B) Roteado pelo Aegis: cada tarefa vai para o modelo mais barato que dá conta,
     conforme a complexidade.

Uso:  python scripts/sim_finance.py <AEGIS_API_KEY>
"""

from __future__ import annotations

import sys

import httpx

from app.routing.pricing import cost_usd, price_of

BASE = "http://127.0.0.1:8000"
REAL_MODEL = "gemini-3.1-flash-lite"  # modelo que roda de fato p/ medir tokens reais
PREMIUM = "gemini-2.5-pro"            # baseline "100% premium"

# Tarefas de construção do sisteminha, com complexidade e o modelo que o Aegis escolheria.
TASKS = [
    ("baixa", "gemini-3.1-flash-lite",
     "Liste em bullets os campos de uma transação financeira pessoal."),
    ("baixa", "gemini-3.1-flash-lite",
     "Sugira 6 categorias de despesa e 3 de receita para finanças pessoais."),
    ("média", "gemini-2.5-flash",
     "Escreva funções Python para adicionar transação, calcular saldo e "
     "salvar/carregar em JSON. Conciso."),
    ("alta", "gemini-2.5-pro",
     "Projete a arquitetura de um sistema de finanças multiusuário: modelo de dados, "
     "módulos, relatórios mensais, metas e alertas. Detalhado."),
]


def call(key: str, prompt: str) -> tuple[int, int]:
    payload = {
        "provider": "google",
        "model": REAL_MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }
    r = httpx.post(
        f"{BASE}/v1/chat/completions",
        headers={"x-api-key": key, "content-type": "application/json"},
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    u = r.json().get("usage", {})
    return u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


def main() -> None:
    key = sys.argv[1]
    print(f"Medindo tokens reais em {REAL_MODEL} via AegisFlow...\n")
    rows = []
    for complexity, routed_model, prompt in TASKS:
        pt, ct = call(key, prompt)
        routed = cost_usd(routed_model, pt, ct)
        premium = cost_usd(PREMIUM, pt, ct)
        rows.append((complexity, routed_model, pt, ct, routed, premium))
        print(f"[{complexity:6}] {routed_model:22} tokens {pt:>4}+{ct:>4}  "
              f"roteado ${routed:.6f}  premium ${premium:.6f}")

    tot_routed = sum(r[4] for r in rows)
    tot_premium = sum(r[5] for r in rows)
    saved = tot_premium - tot_routed
    pct = (saved / tot_premium * 100) if tot_premium else 0

    print("\n" + "=" * 60)
    pin, pout = price_of(PREMIUM)
    print(f"Baseline  100% {PREMIUM} (${pin}/${pout} por Mtok): ${tot_premium:.6f}")
    print(f"Roteado   pelo Aegis (por complexidade)          : ${tot_routed:.6f}")
    print(f"ECONOMIA  : ${saved:.6f}  ({pct:.1f}% mais barato)")
    print("=" * 60)
    print("\n(tokens reais; custo dos modelos premium estimado pelo catálogo, "
          "pois a chave de teste não tem acesso a eles)")


if __name__ == "__main__":
    main()
