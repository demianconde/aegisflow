"""Valida a Fonte B: conecta na The Odds API real e mostra cota + esportes.

Comeca pelo endpoint /sports (GRATIS, nao gasta cota). So consulta odds/scores
se voce passar --odds explicitamente, para nao gastar seus creditos por engano.

Uso:
    python scripts/check_odds_api.py            # so lista esportes (gratis)
    python scripts/check_odds_api.py --odds     # tambem busca odds (gasta cota!)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from betflow.data.odds_api import OddsApiClient, OddsApiError  # noqa: E402


def main(fetch_odds: bool = False) -> int:
    print("== Fonte B: The Odds API ==")
    try:
        client = OddsApiClient()
    except OddsApiError as exc:
        print(f"[erro] {exc}")
        return 1

    # 1) endpoint gratuito: lista de esportes de futebol
    try:
        soccer = client.list_soccer_sports(all_sports=True)
    except OddsApiError as exc:
        print(f"[erro] {exc}")
        return 1

    print(f"Conexao OK. Esportes de futebol disponiveis: {len(soccer)}")
    for s in soccer[:15]:
        estado = "em temporada" if s.get("active") else "fora de temporada"
        print(f"  - {s['key']:<32} {s['title']} ({estado})")
    if len(soccer) > 15:
        print(f"  ... e mais {len(soccer) - 15}")

    print(f"\n{client.last_quota}")
    print("(o endpoint /sports NAO consome cota)")

    # 2) opcional: buscar odds reais (consome cota)
    if fetch_odds:
        active = [s for s in soccer if s.get("active")]
        if not active:
            print("\n[aviso] nenhuma liga ativa agora para buscar odds.")
            return 0
        target = active[0]["key"]
        print(f"\nBuscando odds h2h de: {target} (isso CONSOME cota)")
        try:
            events = client.get_odds(target, markets="h2h")
        except OddsApiError as exc:
            print(f"[erro] {exc}")
            return 1
        print(f"Eventos com odds: {len(events)}")
        for ev in events[:3]:
            books = len(ev.get("bookmakers", []))
            print(f"  {ev.get('home_team')} x {ev.get('away_team')} "
                  f"| inicio: {ev.get('commence_time')} | casas: {books}")
        print(f"\n{client.last_quota}")

    print("\n[ok] Fonte B validada.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(fetch_odds="--odds" in sys.argv))
