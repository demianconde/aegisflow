"""Sobe o servidor local do Betflow (gestao de banca + sugestoes + tracking).

Uso:
    python scripts/run_server.py                 # http://127.0.0.1:5000
    python scripts/run_server.py --port 8000
    python scripts/run_server.py --division SP1 --season 2425

Paginas:
    /              Dashboard: banca, ROI/yield, apostas abertas, liquidacao auto
    /suggestions   Sugestoes de valor das 7 ligas (odds da Betano via Odds API)
    /predict       Consulta manual de um confronto
    /bets          Historico de apostas

A pagina de sugestoes e a liquidacao automatica de resultados usam APENAS a
The Odds API. O historico fica em data/betflow.db (SQLite, ja com user_id para
evoluir a multi-usuario/SaaS).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from betflow.web import create_app  # noqa: E402
from betflow.web.engine import engine  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Servidor local do Betflow")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--division", default="E0", help="codigo football-data (E0, SP1...)")
    ap.add_argument("--season", default="2425", help="temporada (ex.: 2425)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    print("== Betflow - servidor local ==")
    print(f"Carregando {args.division} {args.season} e treinando modelos...")
    engine.reload(division=args.division, season=args.season)
    print(f"OK: {len(engine.teams)} times | {len(engine.df)} jogos")
    print(f"\nAbra no navegador: http://{args.host}:{args.port}\n")

    app = create_app()
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
