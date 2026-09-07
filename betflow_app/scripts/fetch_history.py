"""Valida a Fonte A: baixa dados reais da football-data.co.uk e resume.

Uso:
    python scripts/fetch_history.py                # Premier League 24/25
    python scripts/fetch_history.py E0 2324        # divisao e temporada custom
"""
from __future__ import annotations

import sys
from pathlib import Path

# permite rodar como script direto (adiciona a raiz do projeto ao path)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from betflow.data import football_data as fd  # noqa: E402


def main(division: str = "E0", season: str = "2425") -> int:
    print(f"== Fonte A: football-data.co.uk ==")
    print(f"Baixando divisao={division} temporada={season} ...")
    url = fd.build_url(division, season)
    print(f"URL: {url}")

    df = fd.load_season(division, season)

    if df.empty:
        print("[erro] nenhum jogo carregado.")
        return 1

    print(f"\nJogos carregados: {len(df)}")
    print(f"Periodo: {df['date'].min().date()} -> {df['date'].max().date()}")
    print(f"Times: {df['home_team'].nunique()}")

    cols_interesse = [c for c in (
        "date", "home_team", "away_team", "home_goals", "away_goals", "result",
        "home_corners", "away_corners", "home_yellow", "away_yellow",
        "odds_home_close_avg", "odds_draw_close_avg", "odds_away_close_avg",
    ) if c in df.columns]

    print("\nAmostra (5 primeiros jogos):")
    print(df[cols_interesse].head().to_string(index=False))

    # sanity check estatistico rapido
    if {"home_goals", "away_goals"}.issubset(df.columns):
        print(f"\nMedia de gols mandante: {df['home_goals'].mean():.2f}")
        print(f"Media de gols visitante: {df['away_goals'].mean():.2f}")
    if {"home_corners", "away_corners"}.issubset(df.columns):
        tot = (df["home_corners"] + df["away_corners"]).mean()
        print(f"Media de escanteios/jogo: {tot:.2f}")
    if {"home_yellow", "away_yellow"}.issubset(df.columns):
        tot = (df["home_yellow"] + df["away_yellow"]).mean()
        print(f"Media de cartoes amarelos/jogo: {tot:.2f}")

    print("\n[ok] Fonte A validada com dados reais.")
    return 0


if __name__ == "__main__":
    div = sys.argv[1] if len(sys.argv) > 1 else "E0"
    sea = sys.argv[2] if len(sys.argv) > 2 else "2425"
    raise SystemExit(main(div, sea))
