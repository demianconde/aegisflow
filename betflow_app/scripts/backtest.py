"""Backtest walk-forward honesto do Betflow.

Uso:
    python scripts/backtest.py                 # Premier League (E0 24/25)
    python scripts/backtest.py --league BSA    # Brasileirao (varias temporadas)
    python scripts/backtest.py --league BSA --min-edge 0.05 --kelly 0.25
    python scripts/backtest.py --calibration platt      # calibra as probabilidades
    python scripts/backtest.py --league BSA --compare   # tabela comparativa

Treina o modelo SO com o passado de cada jogo (walk-forward), aposta apenas com
valor e reporta ROI, yield, drawdown, calibracao e o baseline do favorito.

Calibracao (Ciclo 4): `--calibration platt|isotonic` corrige o NIVEL das
probabilidades (previsto vs. observado) antes de calcular EV/Kelly. O calibrador
e reajustado a cada retreino usando SO o historico passado (sem lookahead).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from betflow.data import football_data as fd  # noqa: E402
from betflow.data import extra_leagues as el  # noqa: E402
from betflow.backtest import walk_forward as wf  # noqa: E402
from config import settings  # noqa: E402


def _load(league: str) -> pd.DataFrame:
    cfg = settings.LEAGUES.get(league)
    if cfg is None:
        raise SystemExit(f"liga desconhecida: {league}")
    if cfg["source"] == "extra":
        df = el.load(cfg["extra_code"], league=cfg.get("extra_league"))
        # usa temporadas com odds de fechamento disponiveis
        if "season" in df.columns:
            seasons = sorted(df["season"].astype(str).unique())
            keep = set(seasons[-6:])  # ~6 temporadas recentes
            df = df[df["season"].astype(str).isin(keep)]
        return df
    return fd.load_season(cfg.get("division", "E0"), "2425")


def _compare(df, args) -> int:
    """Roda varias configuracoes e imprime ROI/yield/DD lado a lado.

    Compara o modelo puro contra blends progressivos com o mercado + filtros,
    para ver se alguma variante vira o yield positivo de forma honesta.
    """
    base = dict(min_train=args.min_train, retrain_every=args.retrain_every,
                min_edge=args.min_edge, kelly=args.kelly,
                initial_bankroll=args.bankroll)
    grid = [
        ("modelo puro (w=1.0)", dict(blend_weight=1.0)),
        ("blend w=0.5", dict(blend_weight=0.5)),
        ("blend w=0.3", dict(blend_weight=0.3)),
        ("blend w=0.2", dict(blend_weight=0.2)),
        ("blend w=0.3 + odd<=5", dict(blend_weight=0.3, max_odd=5.0)),
        ("blend w=0.2 + odd<=5 + edge<=0.15",
         dict(blend_weight=0.2, max_odd=5.0, max_edge=0.15)),
        ("blend w=0.3 + flat 1u", dict(blend_weight=0.3, flat_stake=1.0)),
        ("modelo puro + Platt", dict(blend_weight=1.0, calibration="platt")),
        ("modelo puro + isotonica",
         dict(blend_weight=1.0, calibration="isotonic")),
        ("blend w=0.5 + Platt",
         dict(blend_weight=0.5, calibration="platt")),
    ]
    print("\nComparacao de configuracoes (walk-forward honesto):")
    print(f"{'config':<42}{'apostas':>8}{'ROI':>9}{'yield':>9}{'maxDD':>8}")
    print("-" * 76)
    for name, over in grid:
        cfg = wf.BacktestConfig(**{**base, **over})
        res = wf.run(df, cfg)
        s = res.summary
        print(f"{name:<42}{s['n_bets']:>8}"
              f"{s['roi'] * 100:>8.1f}%{s['yield'] * 100:>8.1f}%"
              f"{s['max_drawdown'] * 100:>7.0f}%")
    b = wf.run(df, wf.BacktestConfig(**base)).baseline_fav
    print("-" * 76)
    print(f"{'BASELINE favorito (1u/jogo)':<42}{b['n_bets']:>8}"
          f"{b['roi'] * 100:>8.1f}%{b['yield'] * 100:>8.1f}%{'-':>8}")
    print("\n(yield positivo e consistente entre variantes = sinal de edge real)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Backtest walk-forward do Betflow")
    ap.add_argument("--league", default="E0")
    ap.add_argument("--min-edge", type=float, default=settings.MIN_EDGE)
    ap.add_argument("--kelly", type=float, default=settings.KELLY_FRACTION)
    ap.add_argument("--min-train", type=int, default=200)
    ap.add_argument("--retrain-every", type=int, default=20)
    ap.add_argument("--bankroll", type=float, default=100.0)
    ap.add_argument("--flat", type=float, default=None,
                    help="aposta valor fixo em vez de Kelly (ex.: 1)")
    ap.add_argument("--blend", type=float, default=1.0,
                    help="peso do modelo no blend com o mercado (1=so modelo)")
    ap.add_argument("--fair", default="shin", choices=["shin", "proportional"])
    ap.add_argument("--max-odd", type=float, default=1e9,
                    help="ignora azarões acima desta odd")
    ap.add_argument("--max-edge", type=float, default=1.0,
                    help="ignora edges acima disto (ruido)")
    ap.add_argument("--calibration", default=None,
                    choices=["platt", "isotonic"],
                    help="calibra as probabilidades (Platt ou isotonica)")
    ap.add_argument("--compare", action="store_true",
                    help="compara varias configuracoes lado a lado")
    args = ap.parse_args()

    print(f"== Backtest walk-forward: liga {args.league} ==")
    df = _load(args.league)
    print(f"Jogos carregados: {len(df)} "
          f"({df['date'].min().date()} -> {df['date'].max().date()})")

    if args.compare:
        return _compare(df, args)

    cfg = wf.BacktestConfig(
        min_train=args.min_train, retrain_every=args.retrain_every,
        min_edge=args.min_edge, kelly=args.kelly,
        initial_bankroll=args.bankroll, flat_stake=args.flat,
        blend_weight=args.blend, fair_method=args.fair,
        max_odd=args.max_odd, max_edge=args.max_edge,
        calibration=args.calibration)
    print(f"Config: min_edge={cfg.min_edge} kelly={cfg.kelly} "
          f"min_train={cfg.min_train} retrain={cfg.retrain_every} "
          f"stake={'flat ' + str(cfg.flat_stake) if cfg.flat_stake else 'Kelly'}")
    print("Treinando walk-forward (sem lookahead)... pode levar ~1min.\n")

    res = wf.run(df, cfg)
    s = res.summary

    print(f"Jogos avaliados: {res.n_matches_evaluated}")
    print(f"Apostas de valor feitas: {s['n_bets']}")
    print("-" * 52)
    print(f"Banca:      {cfg.initial_bankroll:.2f} -> {s['final_bankroll']:.2f}")
    print(f"Lucro:      {s['profit']:+.2f} u")
    print(f"ROI:        {s['roi'] * 100:+.2f}%  (sobre a banca)")
    print(f"Yield:      {s['yield'] * 100:+.2f}%  (sobre o volume apostado)")
    print(f"Turnover:   {s['turnover']:.2f} u")
    print(f"Hit rate:   {s['hit_rate'] * 100:.1f}%")
    print(f"Max DD:     {s['max_drawdown'] * 100:.1f}%")
    print(f"Brier:      {res.brier}  (0=perfeito, 0.25=chute 50/50)")
    print("-" * 52)
    b = res.baseline_fav
    print(f"BASELINE favorito (1u/jogo): ROI={b['roi'] * 100:+.2f}% "
          f"yield={b['yield'] * 100:+.2f}% em {b['n_bets']} jogos")
    print("-" * 52)
    print("Calibracao (previsto vs. observado):")
    for c in res.calibration:
        flag = "" if abs(c["pred_mean"] - c["obs_freq"]) < 0.05 else "  <-- desvio"
        print(f"  faixa {c['bin']:<9} n={c['n']:<4} "
              f"previsto={c['pred_mean'] * 100:5.1f}% "
              f"observado={c['obs_freq'] * 100:5.1f}%{flag}")

    print("\nVEREDITO:")
    if s["n_bets"] == 0:
        print("  Nenhuma aposta de valor -> o modelo nao bateu o mercado.")
    elif s["roi"] > 0 and s["yield"] > 0:
        print(f"  ROI/yield POSITIVOS. Edge aparente, mas confira drawdown "
              f"({s['max_drawdown'] * 100:.0f}%) e o tamanho da amostra.")
    else:
        print("  ROI/yield NAO positivos -> sem edge real nesta configuracao. "
              "E o resultado mais comum e honesto: bater a casa e dificil.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
