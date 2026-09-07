"""Teste de CLV: aposta na ABERTURA, mede se batemos o FECHAMENTO.

CLV positivo = pegamos odds melhores que o mercado no fechamento = edge real
de longo prazo (o preditor mais confiavel de lucro).

Uso:
    python scripts/clv_backtest.py                 # Premier League (E0)
    python scripts/clv_backtest.py --league BSA    # Brasileirao
    python scripts/clv_backtest.py --blend 0.5 --min-edge 0.02
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from betflow.data import football_data as fd  # noqa: E402
from betflow.data import extra_leagues as el  # noqa: E402
from betflow.backtest import clv_test  # noqa: E402
from config import settings  # noqa: E402


def _load(league: str) -> pd.DataFrame:
    cfg = settings.LEAGUES.get(league)
    if cfg is None:
        raise SystemExit(f"liga desconhecida: {league}")
    if cfg["source"] == "extra":
        df = el.load(cfg["extra_code"], league=cfg.get("extra_league"))
        if "season" in df.columns:
            seasons = sorted(df["season"].astype(str).unique())
            df = df[df["season"].astype(str).isin(set(seasons[-6:]))]
        return df
    return fd.load_season(cfg.get("division", "E0"), "2425")


def main() -> int:
    ap = argparse.ArgumentParser(description="Teste de CLV (odds de abertura)")
    ap.add_argument("--league", default="E0")
    ap.add_argument("--blend", type=float, default=0.5)
    ap.add_argument("--min-edge", type=float, default=0.02)
    ap.add_argument("--max-odd", type=float, default=6.0)
    ap.add_argument("--min-train", type=int, default=200)
    args = ap.parse_args()

    print(f"== Teste de CLV: liga {args.league} ==")
    df = _load(args.league)
    have_open = "odds_home_open_avg" in df.columns
    print(f"Jogos: {len(df)} | odds de abertura disponiveis: {have_open}")
    if not have_open:
        print("[erro] esta liga nao tem colunas de odds de abertura no CSV.")
        return 1

    cfg = clv_test.CLVConfig(min_train=args.min_train, min_edge=args.min_edge,
                             blend_weight=args.blend, max_odd=args.max_odd)
    print(f"Config: blend={cfg.blend_weight} min_edge={cfg.min_edge} "
          f"max_odd={cfg.max_odd} stake=flat {cfg.flat_stake}")
    print("Rodando walk-forward (aposta na abertura)...\n")

    res = clv_test.run(df, cfg)
    s = res.summary
    print(f"Apostas feitas na ABERTURA: {res.n_bets}")
    print("-" * 52)
    print(f"CLV medio:        {res.clv_mean * 100:+.2f}%   <== metrica principal")
    print(f"% apostas CLV>0:  {res.clv_positive_rate * 100:.1f}%")
    print("-" * 52)
    print(f"ROI (na abertura): {s['roi'] * 100:+.2f}%")
    print(f"Yield:             {s['yield'] * 100:+.2f}%")
    print(f"Hit rate:          {s['hit_rate'] * 100:.1f}%")
    print(f"Max drawdown:      {s['max_drawdown'] * 100:.1f}%")
    print("-" * 52)
    print("VEREDITO:")
    if res.n_bets == 0:
        print("  Nenhuma aposta -> modelo nao discorda do mercado nesta config.")
    elif res.clv_mean > 0:
        print(f"  CLV MEDIO POSITIVO ({res.clv_mean * 100:+.2f}%). Sinal de edge "
              f"real: apostando cedo, pegamos precos melhores que o fechamento.")
        print("  Este e o resultado mais promissor de todo o projeto.")
    else:
        print(f"  CLV medio negativo ({res.clv_mean * 100:+.2f}%). O mercado se "
              f"move CONTRA nossas selecoes -> ainda sem edge nesta config.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
