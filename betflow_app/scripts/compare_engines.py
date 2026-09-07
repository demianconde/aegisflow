"""Fase 4 — comparacao final dos dois motores na MESMA REGUA.

Diferenca para os backtests anteriores: aqui os DOIS motores passam pelo
*mesmo nucleo* de selecao e dimensionamento (`betflow.betting.staking`), cada um
com a sua propria politica (`main_policy()` / `boosted_policy()`). Assim, a unica
coisa que varia entre eles e a METODOLOGIA de estimar probabilidade — nao as
regras de aposta. E o teste honesto de qual abordagem generaliza melhor.

Walk-forward com retreino trimestral (sem look-ahead), banca fixa R$ 1.000.
Cotacoes historicas reais do The Odds API (~5min antes da largada = proxy da
linha de fechamento). Motor principal usa a media do varejo; a Boosted usa a
Pinnacle. Ambos aplicam devig (Shin), ancoragem, porta de sanidade, Kelly x
confianca e teto de stake — via staking.evaluate.

Uso:  python -u scripts/compare_engines.py [LIGA]   (default: BSA)
Requer BETFLOW_ODDS_API_KEY (plano com endpoint historico).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

import numpy as np
import pandas as pd

from betflow.betting import staking, value
from betflow.data import team_names
from betflow.models.dixon_coles import DixonColesModel
from betflow.research.model import BoostedModel
from betflow.web import store
from config import settings

KEY = os.environ["BETFLOW_ODDS_API_KEY"]
REGIONS = "eu,uk"           # eu = Pinnacle; uk = casas de varejo
BANKROLL = 1000.0
MIN_CREDITS = 500
MIN_TRAIN = 120


def _log(m: str) -> None:
    print(m, flush=True)


def hist_snapshot(sport: str, iso: str) -> dict | None:
    url = (f"https://api.the-odds-api.com/v4/historical/sports/{sport}/odds"
           f"?apiKey={KEY}&regions={REGIONS}&markets=h2h&date={iso}"
           f"&oddsFormat=decimal")
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=45) as r:
                data = json.loads(r.read())
                data["_remaining"] = int(r.headers.get(
                    "x-requests-remaining", "0"))
            return data
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503):
                time.sleep(2)
                continue
            _log(f"  ! HTTP {e.code} em {iso}: {e.read()[:100]}")
            return None
        except Exception as exc:  # noqa: BLE001
            _log(f"  ! erro {iso}: {exc}")
            time.sleep(2)
    return None


def _hda(book: dict, home: str, away: str) -> list[float] | None:
    mk = next((m for m in book.get("markets", []) if m.get("key") == "h2h"),
              None)
    if not mk:
        return None
    px: dict[str, float] = {}
    for o in mk.get("outcomes", []):
        nm = o.get("name")
        if nm == home:
            px["H"] = o.get("price")
        elif nm == away:
            px["A"] = o.get("price")
        elif nm == "Draw":
            px["D"] = o.get("price")
    if all(k in px and px[k] for k in ("H", "D", "A")):
        return [float(px["H"]), float(px["D"]), float(px["A"])]
    return None


def event_prices(ev: dict) -> tuple[list[float] | None, list[float] | None]:
    """Devolve (pinnacle[H,D,A], varejo_media[H,D,A])."""
    home, away = ev.get("home_team"), ev.get("away_team")
    pin, rets = None, []
    for b in ev.get("bookmakers", []):
        hda = _hda(b, home, away)
        if not hda:
            continue
        if b.get("key") == "pinnacle":
            pin = hda
        else:
            rets.append(hda)
    ret = list(np.mean(rets, axis=0)) if rets else None
    return pin, ret


def _res(row) -> str:
    if row.home_goals > row.away_goals:
        return "H"
    if row.home_goals < row.away_goals:
        return "A"
    return "D"


def _settle(entries: list[dict], pol, probs: dict[str, float],
            odds: list[float], res: str, date, games: tuple[int, int]) -> None:
    """Roda os 3 legs (H/D/A) por staking.evaluate e registra os aprovados."""
    fair = value.fair_probs(odds, method="shin")
    gh, ga = games
    for side, odd, fp in zip("HDA", odds, fair):
        dec = staking.evaluate(pol, "1X2:" + side, side,
                               p_model=probs[side], p_fair=float(fp), odd=odd,
                               probs=probs, games_home=gh, games_away=ga)
        if not dec.passed:
            continue
        stake = dec.stake_fraction * BANKROLL
        pnl = stake * (odd - 1) if res == side else -stake
        entries.append(dict(date=date, side=side, odd=odd, stake=stake,
                            pnl=pnl, won=res == side,
                            conf=dec.confidence))


def report(name: str, entries: list[dict]) -> None:
    if not entries:
        _log(f"\n{name}: nenhuma entrada")
        return
    d = pd.DataFrame(entries).sort_values("date")
    equity = BANKROLL + d["pnl"].cumsum()
    peak = equity.cummax()
    mdd = float(((peak - equity) / peak).max())
    _log(f"\n{name}")
    _log(f"  entradas:        {len(d)} ({int(d['won'].sum())} certas, "
         f"acerto {d['won'].mean():.1%}, cotacao media {d['odd'].mean():.2f})")
    _log(f"  confianca media: {d['conf'].mean():.2f}")
    _log(f"  volume apostado: R$ {d['stake'].sum():,.2f}")
    _log(f"  P&L:             R$ {d['pnl'].sum():+,.2f}")
    _log(f"  ROI (banca):     {d['pnl'].sum() / BANKROLL:+.1%}")
    _log(f"  yield (volume):  {d['pnl'].sum() / d['stake'].sum():+.1%}")
    _log(f"  drawdown maximo: {mdd:.1%}")
    _log(f"  banca final:     R$ {equity.iloc[-1]:,.2f}")


def main() -> None:
    code = (sys.argv[1] if len(sys.argv) > 1 else "BSA").upper()
    cfg = settings.LEAGUES.get(code)
    if not cfg:
        _log(f"liga desconhecida: {code}")
        return
    sport = cfg["odds_api_key"]
    main_pol, boost_pol = staking.main_policy(), staking.boosted_policy()

    end = pd.Timestamp.utcnow().normalize()
    start = end - pd.Timedelta(days=365)

    df = store.load_matches_df(code, seasons=10).sort_values("date")
    df = df.reset_index(drop=True)
    known = list({*(df["home_team"]), *(df["away_team"])})
    test = df[(df["date"] >= start) & (df["date"] <= end)].copy()
    _log(f"{code}: {len(df)} jogos no warehouse, {len(test)} na janela de teste")

    test["q"] = test["date"].dt.to_period("Q")
    models: dict = {}
    for q, chunk in test.groupby("q"):
        train = df[df["date"] < chunk["date"].min()]
        if len(train) < MIN_TRAIN:
            _log(f"  {q}: treino insuficiente ({len(train)}) — pulado")
            models[q] = (None, None)
            continue
        dc = DixonColesModel(xi=settings.DC_XI, ridge=settings.DC_RIDGE).fit(
            train, ref_date=chunk["date"].min())
        bo = BoostedModel().fit(train)
        models[q] = (dc, bo)
        _log(f"  {q}: modelos treinados (treino={len(train)}, teste={len(chunk)})")

    slots: dict[str, list] = {}
    for row in test.itertuples():
        iso = (row.date - pd.Timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        slots.setdefault(iso, []).append(row)
    _log(f"{len(slots)} horarios distintos -> ~{len(slots)} chamadas")

    main_e: list[dict] = []
    boost_e: list[dict] = []
    no_odds = 0
    remaining = None

    for n, (iso, rows) in enumerate(sorted(slots.items()), 1):
        snap = hist_snapshot(sport, iso)
        if not snap:
            no_odds += len(rows)
            continue
        remaining = snap.get("_remaining", remaining)
        idx = {}
        for ev in snap.get("data", []):
            mh = team_names.match_team(ev.get("home_team", ""), known)
            ma = team_names.match_team(ev.get("away_team", ""), known)
            if mh and ma:
                idx[(mh, ma)] = ev
        for row in rows:
            ev = idx.get((row.home_team, row.away_team))
            if not ev:
                no_odds += 1
                continue
            pin, ret = event_prices(ev)
            q = pd.Period(row.date, freq="Q")
            dc, bo = models.get(q, (None, None))
            res = _res(row)
            # ---- motor principal: varejo, staking.main_policy() ----
            if dc is not None and ret and all(o > 1.0 for o in ret):
                try:
                    p = dc.predict_1x2(row.home_team, row.away_team)
                except (KeyError, ValueError):
                    p = None
                if p:
                    # DC nao expoe contagem por time aqui: confianca por dados
                    # fica neutra (999) e o ridge ja cuida da amostra pequena.
                    _settle(main_e, main_pol, p, ret, res, row.date, (999, 999))
            # ---- Boosted: Pinnacle, staking.boosted_policy() ----
            if bo is not None and pin and all(o > 1.0 for o in pin):
                pb = bo.predict_1x2(row.home_team, row.away_team, when=row.date)
                if pb:
                    gh, ga = bo.fixture_games(row.home_team, row.away_team)
                    _settle(boost_e, boost_pol, pb, pin, res, row.date, (gh, ga))
        if n % 20 == 0:
            _log(f"  {n}/{len(slots)} horarios | creditos={remaining}"
                 f" | main={len(main_e)} boosted={len(boost_e)}")
        if remaining is not None and remaining < MIN_CREDITS:
            _log(f"  ! parando: creditos abaixo de {MIN_CREDITS}")
            break

    _log("")
    _log("=" * 64)
    _log(f"{code} — ultimos 12 meses | jogos sem cotacao casada: {no_odds}")
    _log(f"creditos restantes ao final: {remaining}")
    _log("MESMA REGUA: os dois motores passam por staking.evaluate")
    _log("=" * 64)
    report("Motor principal (Dixon-Coles + ridge, varejo, main_policy)", main_e)
    report("Boosted Research (Elo+XGB blend, Pinnacle, boosted_policy)", boost_e)


if __name__ == "__main__":
    main()
