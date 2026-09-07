"""Coleta automatica de odds (snapshots) - roda 1 vez e sai.

Este e o script chamado pela Tarefa Agendada do Windows. Cada execucao grava um
snapshot das odds atuais (1X2, escanteios, cartoes) dos proximos jogos, no banco
data/odds_history.db. Rodando de tempos em tempos, acumula o historico p/ CLV.

Uso manual:
    python scripts/collect_odds.py                 # todas as ligas configuradas
    python scripts/collect_odds.py --leagues E0    # so uma liga
    python scripts/collect_odds.py --max 6         # limita jogos por liga (cota)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from betflow.collect import collector  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Coleta de odds do Betflow")
    ap.add_argument("--leagues", nargs="*", default=None,
                    help="codigos de liga (ex.: E0 BSA). Padrao: todas.")
    ap.add_argument("--max", type=int, default=10,
                    help="max de jogos por liga (controla gasto de cota)")
    args = ap.parse_args()

    print(f"[{collector.store.now()}] iniciando coleta...")
    res = collector.collect_once(leagues=args.leagues, max_fixtures=args.max)
    print(f"ligas={res['leagues']} jogos={res['fixtures']} "
          f"snapshots gravados={res['snapshots']} cota={res['quota_left']}")
    if res["notes"]:
        for n in res["notes"][-5:]:
            print(f"  aviso: {n}")

    st = collector.store.stats()
    print(f"[historico acumulado] snapshots={st['snapshots']} "
          f"jogos={st['matches']} coletas={st['runs']} "
          f"desde={st['first_capture']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
