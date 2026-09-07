"""Backtest walk-forward do ultimo ano: motor principal x Boosted Research.

Reproduz as regras de producao de cada linha sobre cotacoes HISTORICAS reais
(football-data.co.uk):

  * Motor principal  — Dixon-Coles (xi=0.0018), mercado 1X2, cotacao de varejo
    (Bet365, proxy da Betano), edge absoluto >= 3%, 1/4 Kelly.
  * Boosted Research — Elo bivariado + XGBoost, referencia Pinnacle (fechamento),
    filtro triplo: edge >= 4pp, discordancia relativa >= 15% sobre a linha justa
    (Shin) e cotacao <= 4.0, 1/4 Kelly.

Walk-forward com retreino MENSAL: para prever os jogos do mes M, cada modelo so
ve jogos anteriores a M (sem look-ahead). Stake em R$ sobre banca de referencia
fixa de R$ 1.000 (mesma regua do track record em producao).

Uso:  python -m scripts.backtest_year
"""
from __future__ import annotations

import io
import sys
import urllib.request

import numpy as np
import pandas as pd

from betflow.betting import value
from betflow.models.dixon_coles import DixonColesModel
from betflow.research.model import BoostedModel

BANKROLL = 1000.0
KELLY = 0.25

# regras de producao (main)
MAIN_MIN_EDGE = 0.03
# regras de producao (boosted)
B_MIN_EDGE, B_REL_EDGE, B_MAX_ODD = 0.04, 0.15, 4.0

# ligas principais do football-data (mmz4281) + Brasil (arquivo "new")
MAIN_SEASONS = ["2122", "2223", "2324", "2425", "2526", "2627"]
MAIN_LEAGUES = ["E0", "F1", "I1", "SP1"]
BRA_URL = "https://www.football-data.co.uk/new/BRA.csv"

TEST_START = pd.Timestamp("2025-09-07")
TEST_END = pd.Timestamp("2026-09-06")


def _fetch(url: str) -> pd.DataFrame | None:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
        return pd.read_csv(io.BytesIO(raw), encoding="latin-1",
                           on_bad_lines="skip")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! falha ao baixar {url}: {exc}", file=sys.stderr)
        return None


def load_league(code: str) -> pd.DataFrame:
    """Historico com cotacoes: date, home_team, away_team, gols, b365*, pin*."""
    frames = []
    if code == "BSA":
        df = _fetch(BRA_URL)
        if df is None:
            return pd.DataFrame()
        df = df.rename(columns={
            "Home": "home_team", "Away": "away_team",
            "HG": "home_goals", "AG": "away_goals"})
        df["date"] = pd.to_datetime(df["Date"], dayfirst=True, errors="coerce")
        # Pinnacle (PH/PD/PA); varejo: media do mercado (AvgH...) como proxy
        for src, dst in [("PH", "pin_h"), ("PD", "pin_d"), ("PA", "pin_a"),
                         ("AvgH", "ret_h"), ("AvgD", "ret_d"), ("AvgA", "ret_a")]:
            df[dst] = pd.to_numeric(df.get(src), errors="coerce")
        frames.append(df)
    else:
        for season in MAIN_SEASONS:
            url = f"https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
            df = _fetch(url)
            if df is None or "HomeTeam" not in df.columns:
                continue
            df = df.rename(columns={
                "HomeTeam": "home_team", "AwayTeam": "away_team",
                "FTHG": "home_goals", "FTAG": "away_goals"})
            df["date"] = pd.to_datetime(df["Date"], dayfirst=True,
                                        errors="coerce")
            # Pinnacle fechamento (PSCH...) com fallback abertura (PSH...)
            for close, open_, dst in [("PSCH", "PSH", "pin_h"),
                                      ("PSCD", "PSD", "pin_d"),
                                      ("PSCA", "PSA", "pin_a")]:
                c = pd.to_numeric(df.get(close), errors="coerce")
                o = pd.to_numeric(df.get(open_), errors="coerce")
                df[dst] = c.fillna(o) if c is not None else o
            for src, dst in [("B365H", "ret_h"), ("B365D", "ret_d"),
                             ("B365A", "ret_a")]:
                df[dst] = pd.to_numeric(df.get(src), errors="coerce")
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    cols = ["date", "home_team", "away_team", "home_goals", "away_goals",
            "pin_h", "pin_d", "pin_a", "ret_h", "ret_d", "ret_a"]
    out = pd.concat(frames, ignore_index=True)
    out = out.dropna(subset=["date", "home_team", "away_team",
                             "home_goals", "away_goals"])
    out = out[[c for c in cols if c in out.columns]].sort_values("date")
    return out.reset_index(drop=True)


def _result(row) -> str:
    if row.home_goals > row.away_goals:
        return "H"
    if row.home_goals < row.away_goals:
        return "A"
    return "D"


def simulate(code: str, df: pd.DataFrame) -> dict[str, list[dict]]:
    """Walk-forward mensal; devolve as entradas simuladas de cada estrategia."""
    test = df[(df["date"] >= TEST_START) & (df["date"] <= TEST_END)]
    out: dict[str, list[dict]] = {"main": [], "boosted": []}
    if test.empty:
        return out

    for month, chunk in test.groupby(test["date"].dt.to_period("M")):
        train = df[df["date"] < chunk["date"].min()]
        if len(train) < 300:
            continue
        dc = DixonColesModel(xi=0.0018).fit(train, ref_date=chunk["date"].min())
        bm = BoostedModel().fit(train)

        for row in chunk.itertuples():
            res = _result(row)
            # ---- motor principal (varejo, edge absoluto 3%) --------------
            trio_r = [row.ret_h, row.ret_d, row.ret_a]
            if all(pd.notna(o) and o > 1.0 for o in trio_r):
                try:
                    p = dc.predict_1x2(row.home_team, row.away_team)
                except (KeyError, ValueError):
                    p = None
                if p:
                    for side, odd in zip("HDA", trio_r):
                        vb = value.evaluate_bet("1X2:" + side, side, p[side],
                                                odd, min_edge=MAIN_MIN_EDGE,
                                                kelly=KELLY)
                        if vb.stake_fraction > 0:
                            stake = vb.stake_fraction * BANKROLL
                            pnl = stake * (odd - 1) if res == side else -stake
                            out["main"].append(dict(
                                league=code, date=row.date, side=side,
                                odd=odd, prob=p[side], stake=stake, pnl=pnl,
                                won=res == side))
            # ---- Boosted Research (Pinnacle, filtro triplo) --------------
            trio_p = [row.pin_h, row.pin_d, row.pin_a]
            if all(pd.notna(o) and o > 1.0 for o in trio_p):
                pb = bm.predict_1x2(row.home_team, row.away_team,
                                    when=row.date)
                if pb:
                    fair = value.fair_probs(trio_p, method="shin")
                    for side, odd, fp in zip("HDA", trio_p, fair):
                        if odd > B_MAX_ODD or pb[side] < fp * (1 + B_REL_EDGE):
                            continue
                        vb = value.evaluate_bet("1X2:" + side, side, pb[side],
                                                odd, fair_prob=fp,
                                                min_edge=B_MIN_EDGE,
                                                kelly=KELLY)
                        if vb.stake_fraction > 0:
                            stake = vb.stake_fraction * BANKROLL
                            pnl = stake * (odd - 1) if res == side else -stake
                            out["boosted"].append(dict(
                                league=code, date=row.date, side=side,
                                odd=odd, prob=pb[side], stake=stake, pnl=pnl,
                                won=res == side))
    return out


def report(name: str, entries: list[dict]) -> dict:
    if not entries:
        return dict(name=name, n=0)
    d = pd.DataFrame(entries).sort_values("date")
    equity = BANKROLL + d["pnl"].cumsum()
    peak = equity.cummax()
    mdd = float(((peak - equity) / peak).max())
    return dict(
        name=name, n=len(d), won=int(d["won"].sum()),
        hit=float(d["won"].mean()),
        avg_odd=float(d["odd"].mean()),
        staked=float(d["stake"].sum()),
        pnl=float(d["pnl"].sum()),
        roi=float(d["pnl"].sum() / BANKROLL),
        yld=float(d["pnl"].sum() / d["stake"].sum()),
        max_dd=mdd,
        final=float(equity.iloc[-1]),
    )


def main() -> None:
    leagues = ["E0", "F1", "I1", "SP1", "BSA"]
    all_entries: dict[str, list[dict]] = {"main": [], "boosted": []}
    per_league: list[dict] = []

    for code in leagues:
        print(f"== {code}: baixando historico...", flush=True)
        df = load_league(code)
        if df.empty:
            print(f"  sem dados para {code}")
            continue
        n_test = len(df[(df['date'] >= TEST_START) & (df['date'] <= TEST_END)])
        print(f"  {len(df)} jogos ({df['date'].min():%Y-%m} a "
              f"{df['date'].max():%Y-%m}), {n_test} na janela de teste")
        sim = simulate(code, df)
        for strat in ("main", "boosted"):
            all_entries[strat].extend(sim[strat])
            r = report(f"{code}/{strat}", sim[strat])
            per_league.append(r)
            if r["n"]:
                print(f"  {strat:8s}: {r['n']:4d} entradas | acerto "
                      f"{r['hit']:.1%} | P&L R$ {r['pnl']:+8.2f} | yield "
                      f"{r['yld']:+.1%}")

    print("\n" + "=" * 72)
    print(f"RESULTADO CONSOLIDADO — {TEST_START:%d/%m/%Y} a {TEST_END:%d/%m/%Y}"
          f" (banca de referencia R$ {BANKROLL:.0f}, 1/4 Kelly)")
    print("=" * 72)
    for strat, label in [("main", "Motor principal (Dixon-Coles)"),
                         ("boosted", "Boosted Research (Elo+XGB)")]:
        r = report(label, all_entries[strat])
        if not r.get("n"):
            print(f"{label}: nenhuma entrada")
            continue
        print(f"\n{label}")
        print(f"  entradas:        {r['n']} ({r['won']} certas, "
              f"acerto {r['hit']:.1%}, cotacao media {r['avg_odd']:.2f})")
        print(f"  volume:          R$ {r['staked']:,.2f}")
        print(f"  P&L:             R$ {r['pnl']:+,.2f}")
        print(f"  ROI (banca):     {r['roi']:+.1%}")
        print(f"  yield (volume):  {r['yld']:+.1%}")
        print(f"  drawdown maximo: {r['max_dd']:.1%}")
        print(f"  banca final:     R$ {r['final']:,.2f}")


if __name__ == "__main__":
    main()
