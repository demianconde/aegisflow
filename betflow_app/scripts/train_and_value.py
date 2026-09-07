"""Pipeline end-to-end do Betflow sobre dados REAIS (Ciclo 3).

Treina Dixon-Coles (1X2/OU/BTTS), Binomial Negativa (escanteios) e Poisson
(cartoes) na temporada carregada e, para cada jogo, compara a probabilidade do
modelo com a odd de fechamento do mercado - listando as apostas de VALOR
(EV>0, edge >= MIN_EDGE) com o stake sugerido por Kelly fracionario.

Uso:
    python scripts/train_and_value.py             # E0 24/25 (cache local)
    python scripts/train_and_value.py E0 2425

IMPORTANTE: avaliar as odds de fechamento do PROPRIO conjunto de treino serve
para demonstrar o motor. Um backtest honesto (Ciclo 5) usa walk-forward: treina
so no passado de cada jogo. Este script e uma vitrine do pipeline, nao um
backtest de lucro.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from betflow.data import football_data as fd  # noqa: E402
from betflow.models.dixon_coles import DixonColesModel  # noqa: E402
from betflow.models.counts import NegativeBinomialTotals, PoissonCards  # noqa: E402
from betflow.betting import value  # noqa: E402
from config import settings  # noqa: E402


def main(division: str = "E0", season: str = "2425") -> int:
    print("== Betflow - pipeline de modelagem + value (dados reais) ==")
    df = fd.load_season(division, season)
    if df.empty:
        print("[erro] nenhum jogo carregado.")
        return 1
    print(f"Liga {division} {season}: {len(df)} jogos "
          f"({df['date'].min().date()} -> {df['date'].max().date()})")

    # ------------------------------------------------------------------
    # treino dos modelos
    # ------------------------------------------------------------------
    dc = DixonColesModel(xi=0.0018).fit(df)  # xi>0: decaimento temporal leve
    print(f"\nDixon-Coles ajustado | vantagem mandante (gamma)={dc.home_adv:.3f} "
          f"| rho={dc.rho:.3f}")
    ranking = sorted(dc.attack, key=dc.attack.get, reverse=True)
    print("Top 5 ataques:", ", ".join(f"{t}({dc.attack[t]:+.2f})" for t in ranking[:5]))

    nb = NegativeBinomialTotals().fit(df)
    print(f"Escanteios (NB): media/jogo={nb.mean_:.2f} var={nb.var_:.2f} "
          f"(superdisperso={nb.var_ > nb.mean_})")

    cards = PoissonCards().fit(df)
    print(f"Cartoes amarelos (Poisson): media/jogo={cards.mean_:.2f}")

    # ------------------------------------------------------------------
    # busca de value nas odds de fechamento (1X2 e Over/Under 2.5)
    # ------------------------------------------------------------------
    bets: list[value.ValueBet] = []
    for _, r in df.iterrows():
        home, away = r["home_team"], r["away_team"]
        try:
            p1x2 = dc.predict_1x2(home, away)
            pou = dc.predict_over_under(home, away, 2.5)
        except KeyError:
            continue

        # 1X2 (usa odds de fechamento medias, ja limpas de margem para o fair)
        legs = [
            ("1X2:H", f"{home} vencer", p1x2["H"], r.get("odds_home_close_avg")),
            ("1X2:D", "Empate", p1x2["D"], r.get("odds_draw_close_avg")),
            ("1X2:A", f"{away} vencer", p1x2["A"], r.get("odds_away_close_avg")),
        ]
        # Over/Under 2.5
        legs += [
            ("OU2.5:over", "Over 2.5 gols", pou["over"], r.get("odds_over25_close_avg")),
            ("OU2.5:under", "Under 2.5 gols", pou["under"], r.get("odds_under25_close_avg")),
        ]

        for market, sel, prob, odd in legs:
            if odd is None or pd.isna(odd) or odd <= 1.0:
                continue
            vb = value.evaluate_bet(
                market, f"{home} x {away}: {sel}", prob, float(odd),
                min_edge=settings.MIN_EDGE, kelly=settings.KELLY_FRACTION,
            )
            if vb.stake_fraction > 0:
                bets.append(vb)

    bets.sort(key=lambda b: b.ev, reverse=True)
    print(f"\n== Value bets encontradas (edge >= {settings.MIN_EDGE:.0%}, "
          f"Kelly {settings.KELLY_FRACTION:.0%}): {len(bets)} ==")
    for vb in bets[:15]:
        print(f"  [{vb.market:<12}] EV={vb.ev:+.3f} edge={vb.edge:+.3f} "
              f"odd={vb.odd:.2f} stake={vb.stake_fraction:.3f} | {vb.selection}")
    if len(bets) > 15:
        print(f"  ... e mais {len(bets) - 15}")

    print("\n[ok] Pipeline executado. Ciclo 5 fara o backtest walk-forward honesto.")
    return 0


if __name__ == "__main__":
    div = sys.argv[1] if len(sys.argv) > 1 else "E0"
    sea = sys.argv[2] if len(sys.argv) > 2 else "2425"
    raise SystemExit(main(div, sea))
