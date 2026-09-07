"""Backtest offline (sem custo de API) dos DOIS motores na Premier League
2024/25, comparando o comportamento ANTIGO x NOVO na MESMA janela de jogos.

Fonte: data/raw/E0_2425.csv (football-data.co.uk) — cotacoes de fechamento reais
e resultados finais. Walk-forward de janela expansiva com retreino periodico
(sem look-ahead). Banca fixa R$ 1.000 (mesma regua).

Cada motor opera na sua linha de referencia de producao:
  * MOTOR PRINCIPAL (Dixon-Coles)  -> media do mercado (colunas AvgC*), proxy do
    varejo/Betano.
  * BOOSTED RESEARCH (Elo+XGBoost) -> Pinnacle de fechamento (colunas PSC*).

Comportamentos comparados por motor:
  PRINCIPAL ANTIGO : edge absoluto (p - 1/odd) >= 3%, 1/4 Kelly, sem devig/teto/
                     ancoragem/sanidade (o que forcava zebras).
  PRINCIPAL NOVO   : staking.main_policy().
  BOOSTED  ANTIGO  : filtro triplo (edge>=4%, discordancia rel.>=15%, cotacao<=4)
                     com evaluate_bet + Shin.
  BOOSTED  NOVO    : staking.boosted_policy().

Uso: python -u scripts/backtest_pl_offline.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from betflow.betting import staking, value
from betflow.models.dixon_coles import DixonColesModel
from betflow.research.model import BoostedModel
from config import settings

BANKROLL = 1000.0
KELLY = settings.KELLY_FRACTION
OLD_MAIN_MIN_EDGE = 0.03
OLD_B_MIN_EDGE, OLD_B_REL, OLD_B_MAXODD = 0.04, 0.15, 4.0
BURN_IN = 150          # jogos de aquecimento antes de operar
RETRAIN_EVERY = 10     # retreina os modelos a cada N jogos de teste


def _log(m: str) -> None:
    print(m, flush=True)


def load() -> pd.DataFrame:
    df = pd.read_csv(settings.RAW_DIR / "E0_2425.csv")
    df = df.rename(columns={
        "HomeTeam": "home_team", "AwayTeam": "away_team",
        "FTHG": "home_goals", "FTAG": "away_goals",
        "AvgCH": "rH", "AvgCD": "rD", "AvgCA": "rA",   # media do mercado (varejo)
        "PSCH": "pH", "PSCD": "pD", "PSCA": "pA"})     # Pinnacle fechamento
    df["date"] = pd.to_datetime(df["Date"], format="%d/%m/%Y", utc=True)
    keep = ["date", "home_team", "away_team", "home_goals", "away_goals",
            "rH", "rD", "rA", "pH", "pD", "pA"]
    df = df.dropna(subset=keep)[keep].sort_values("date").reset_index(drop=True)
    df["home_goals"] = df["home_goals"].astype(int)
    df["away_goals"] = df["away_goals"].astype(int)
    return df


def _res(row) -> str:
    if row.home_goals > row.away_goals:
        return "H"
    if row.home_goals < row.away_goals:
        return "A"
    return "D"


def _curve(d: pd.DataFrame, compound: bool) -> tuple[float, float, float]:
    """Simula a banca entrada a entrada. compound=False -> aporte fixo sobre a
    banca inicial; compound=True -> reinveste (aporte = fracao x banca corrente).
    Devolve (banca_final, volume_total, drawdown_max)."""
    bank = BANKROLL
    peak = bank
    mdd = 0.0
    vol = 0.0
    for r in d.itertuples():
        base = bank if compound else BANKROLL
        stake = r.frac * base
        vol += stake
        bank += stake * (r.odd - 1) if r.won else -stake
        peak = max(peak, bank)
        if peak > 0:
            mdd = max(mdd, (peak - bank) / peak)
    return bank, vol, mdd


def report(name: str, entries: list[dict]) -> dict:
    if not entries:
        _log(f"\n{name}: nenhuma entrada")
        return {}
    d = pd.DataFrame(entries).sort_values("date")
    f_final, f_vol, f_mdd = _curve(d, compound=False)
    c_final, c_vol, c_mdd = _curve(d, compound=True)
    out = dict(n=len(d), hit=float(d["won"].mean()), odd=float(d["odd"].mean()),
               vol=f_vol, pnl=f_final - BANKROLL,
               roi=(f_final - BANKROLL) / BANKROLL,
               yld=(f_final - BANKROLL) / f_vol if f_vol else 0.0, mdd=f_mdd,
               final=f_final, c_final=c_final,
               c_roi=(c_final - BANKROLL) / BANKROLL, c_mdd=c_mdd)
    _log(f"\n{name}")
    _log(f"  entradas:            {out['n']} ({int(d['won'].sum())} certas, "
         f"acerto {out['hit']:.1%}, cotacao media {out['odd']:.2f})")
    _log(f"  -- APORTE FIXO (nao reinveste) --")
    _log(f"  volume apostado:     R$ {out['vol']:,.2f}")
    _log(f"  ROI (banca):         {out['roi']:+.1%}")
    _log(f"  yield (volume):      {out['yld']:+.1%}")
    _log(f"  drawdown maximo:     {out['mdd']:.1%}")
    _log(f"  banca final:         R$ {out['final']:,.2f}")
    _log(f"  -- COM REINVESTIMENTO (juros compostos) --")
    _log(f"  ROI (banca):         {out['c_roi']:+.1%}")
    _log(f"  drawdown maximo:     {out['c_mdd']:.1%}")
    _log(f"  banca final:         R$ {out['c_final']:,.2f}")
    return out


def _delta(old: dict, new: dict) -> None:
    if not (old and new):
        return
    _log("  DELTA (novo - antigo):")
    _log(f"    entradas:  {new['n'] - old['n']:+d}  ({old['n']} -> {new['n']})")
    _log(f"    acerto:    {(new['hit'] - old['hit']) * 100:+.1f} pp  "
         f"({old['hit']:.1%} -> {new['hit']:.1%})")
    _log(f"    cot.media: {new['odd'] - old['odd']:+.2f}  "
         f"({old['odd']:.2f} -> {new['odd']:.2f})")
    _log(f"    drawdown:  {(new['mdd'] - old['mdd']) * 100:+.1f} pp  "
         f"({old['mdd']:.1%} -> {new['mdd']:.1%})")
    _log(f"    ROI:       {(new['roi'] - old['roi']) * 100:+.1f} pp  "
         f"({old['roi']:+.1%} -> {new['roi']:+.1%})")
    _log(f"    yield:     {(new['yld'] - old['yld']) * 100:+.1f} pp  "
         f"({old['yld']:+.1%} -> {new['yld']:+.1%})")
    _log(f"    banca final (fixa):     R$ {old['final']:,.2f} -> "
         f"R$ {new['final']:,.2f}")
    _log(f"    banca final (composta): R$ {old['c_final']:,.2f} -> "
         f"R$ {new['c_final']:,.2f}")


def main() -> None:
    df = load()
    _log(f"Premier League 2024/25: {len(df)} jogos com cotacao de fechamento "
         f"e resultado.")
    _log(f"Walk-forward: burn-in {BURN_IN}, retreino a cada {RETRAIN_EVERY}. "
         f"Banca R$ {BANKROLL:,.0f}, {KELLY:g} Kelly.")

    main_pol, boost_pol = staking.main_policy(), staking.boosted_policy()
    mo_old: list[dict] = []
    mo_new: list[dict] = []
    bo_old: list[dict] = []
    bo_new: list[dict] = []

    dc: DixonColesModel | None = None
    bo: BoostedModel | None = None
    dc_teams: set[str] = set()
    games_map = None

    for i in range(BURN_IN, len(df)):
        if dc is None or (i - BURN_IN) % RETRAIN_EVERY == 0:
            train = df.iloc[:i]
            dc = DixonColesModel(xi=settings.DC_XI,
                                 ridge=settings.DC_RIDGE).fit(
                train, ref_date=df.iloc[i]["date"])
            bo = BoostedModel().fit(train)
            dc_teams = set(dc.teams)
            games_map = (train["home_team"].value_counts().add(
                train["away_team"].value_counts(), fill_value=0)).astype(int)

        row = df.iloc[i]
        h, a = row["home_team"], row["away_team"]
        res = _res(row)
        gh = int(games_map.get(h, 0))
        ga = int(games_map.get(a, 0))

        # ---------------- MOTOR PRINCIPAL (Dixon-Coles) — media do mercado -----
        if h in dc_teams and a in dc_teams:
            p = dc.predict_1x2(h, a)
            r_odds = [float(row["rH"]), float(row["rD"]), float(row["rA"])]
            r_fair = value.fair_probs(r_odds, method="shin")
            for side, odd, fp in zip("HDA", r_odds, r_fair):
                pm = p[side]
                # ANTIGO
                if pm - 1.0 / odd >= OLD_MAIN_MIN_EDGE and pm * odd - 1.0 > 0:
                    f = value.kelly_fraction(pm, odd, fraction=KELLY)
                    if f > 0:
                        mo_old.append(dict(date=row["date"], odd=odd, frac=f,
                                           won=res == side))
                # NOVO
                dec = staking.evaluate(main_pol, "1X2:" + side, side, p_model=pm,
                                       p_fair=float(fp), odd=odd, probs=p,
                                       games_home=gh, games_away=ga)
                if dec.passed:
                    mo_new.append(dict(date=row["date"], odd=odd,
                                       frac=dec.stake_fraction, won=res == side))

        # ---------------- BOOSTED RESEARCH (Elo+XGB) — Pinnacle fechamento -----
        pb = bo.predict_1x2(h, a, when=row["date"])
        if pb:
            bgh, bga = bo.fixture_games(h, a)
            p_odds = [float(row["pH"]), float(row["pD"]), float(row["pA"])]
            p_fair = value.fair_probs(p_odds, method="shin")
            for side, odd, fp in zip("HDA", p_odds, p_fair):
                pm = pb[side]
                # ANTIGO (filtro triplo)
                if not (odd > OLD_B_MAXODD or pm < fp * (1.0 + OLD_B_REL)):
                    vb = value.evaluate_bet("1X2:" + side, side, pm, odd,
                                            fair_prob=float(fp),
                                            min_edge=OLD_B_MIN_EDGE, kelly=KELLY)
                    if vb.stake_fraction > 0:
                        bo_old.append(dict(date=row["date"], odd=odd,
                                           frac=vb.stake_fraction,
                                           won=res == side))
                # NOVO
                dec = staking.evaluate(boost_pol, "1X2:" + side, side, p_model=pm,
                                       p_fair=float(fp), odd=odd, probs=pb,
                                       games_home=bgh, games_away=bga)
                if dec.passed:
                    bo_new.append(dict(date=row["date"], odd=odd,
                                       frac=dec.stake_fraction, won=res == side))

    _log("\n" + "=" * 64)
    _log("MOTOR PRINCIPAL (Dixon-Coles) — media do mercado (varejo)")
    _log("=" * 64)
    o = report("  ANTIGO (edge absoluto 3%, sem travas)", mo_old)
    n = report("  NOVO (main_policy)", mo_new)
    _delta(o, n)

    _log("\n" + "=" * 64)
    _log("BOOSTED RESEARCH (Elo + XGBoost) — Pinnacle de fechamento")
    _log("=" * 64)
    o = report("  ANTIGO (filtro triplo: edge 4% / rel 15% / cotacao<=4)", bo_old)
    n = report("  NOVO (boosted_policy)", bo_new)
    _delta(o, n)

    # ---- CAIXA UNICO: R$ 1.000 rodando os DOIS motores (versao NOVA) juntos --
    combined = sorted(mo_new + bo_new, key=lambda e: e["date"])
    if combined:
        d = pd.DataFrame(combined)
        c_final, _, c_mdd = _curve(d, compound=True)
        f_final, f_vol, f_mdd = _curve(d, compound=False)
        _log("\n" + "=" * 64)
        _log("CAIXA UNICO — R$ 1.000, sem aporte novo, os DOIS motores (NOVO)")
        _log("=" * 64)
        _log(f"  entradas totais:     {len(d)}")
        _log(f"  -- APORTE FIXO --")
        _log(f"  banca final:         R$ {f_final:,.2f}  ({(f_final - BANKROLL) / BANKROLL:+.1%})")
        _log(f"  drawdown maximo:     {f_mdd:.1%}")
        _log(f"  -- COM REINVESTIMENTO --")
        _log(f"  banca final:         R$ {c_final:,.2f}  ({(c_final - BANKROLL) / BANKROLL:+.1%})")
        _log(f"  drawdown maximo:     {c_mdd:.1%}")


if __name__ == "__main__":
    main()
