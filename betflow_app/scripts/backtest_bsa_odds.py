"""Backtest financeiro do Brasileirao (BSA) nos ultimos 12 meses com cotacoes
HISTORICAS reais do The Odds API (plano pago), motor principal x Boosted.

Fonte de resultados: warehouse (store.load_matches_df). Fonte de cotacoes:
endpoint historico /v4/historical/.../odds, uma foto ~5min antes de cada
largada (dedup por horario -> economiza creditos). Casamento de times pelo
normalizador (data.team_names).

Regras replicadas de producao:
  * Motor principal — Dixon-Coles, mercado 1X2, PROXY DE VAREJO = media das
    casas disponiveis (Betano/Bet365 nao existem no feed historico), edge
    absoluto >= 3%, 1/4 Kelly.
  * Boosted Research — Elo+XGBoost, referencia PINNACLE, devig por Shin, filtro
    triplo (edge >= 4pp, discordancia relativa >= 15%, cotacao <= 4.0), 1/4 Kelly.

Walk-forward com retreino TRIMESTRAL (sem look-ahead). Banca fixa R$ 1.000.
Uso (na producao):  python -u scripts/backtest_bsa_odds.py
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import numpy as np
import pandas as pd

from betflow.betting import value
from betflow.data import team_names
from betflow.models.dixon_coles import DixonColesModel
from betflow.research.model import BoostedModel
from betflow.web import store

KEY = os.environ["BETFLOW_ODDS_API_KEY"]
SPORT = "soccer_brazil_campeonato"
REGIONS = "eu,uk"           # eu = Pinnacle; uk = casas de varejo
BANKROLL = 1000.0
KELLY = 0.25
MAIN_MIN_EDGE = 0.03
B_MIN_EDGE, B_REL_EDGE, B_MAX_ODD = 0.04, 0.15, 4.0
MIN_CREDITS = 500           # teto de seguranca
MIN_TRAIN = 120


def _log(m: str) -> None:
    print(m, flush=True)


def hist_snapshot(iso: str) -> dict | None:
    url = (f"https://api.the-odds-api.com/v4/historical/sports/{SPORT}/odds"
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
    px = {}
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
    pin = None
    rets = []
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
    _log(f"  volume apostado: R$ {d['stake'].sum():,.2f}")
    _log(f"  P&L:             R$ {d['pnl'].sum():+,.2f}")
    _log(f"  ROI (banca):     {d['pnl'].sum() / BANKROLL:+.1%}")
    _log(f"  yield (volume):  {d['pnl'].sum() / d['stake'].sum():+.1%}")
    _log(f"  drawdown maximo: {mdd:.1%}")
    _log(f"  banca final:     R$ {equity.iloc[-1]:,.2f}")


def main() -> None:
    end = pd.Timestamp.utcnow().normalize()
    start = end - pd.Timedelta(days=365)

    df = store.load_matches_df("BSA", seasons=10).sort_values("date")
    df = df.reset_index(drop=True)
    known = list({*(df["home_team"]), *(df["away_team"])})
    test = df[(df["date"] >= start) & (df["date"] <= end)].copy()
    _log(f"BSA: {len(df)} jogos no warehouse, {len(test)} na janela de teste")

    # modelos por trimestre (retreino sem look-ahead)
    test["q"] = test["date"].dt.to_period("Q")
    models: dict = {}
    for q, chunk in test.groupby("q"):
        train = df[df["date"] < chunk["date"].min()]
        if len(train) < MIN_TRAIN:
            _log(f"  {q}: treino insuficiente ({len(train)}) — pulado")
            models[q] = (None, None)
            continue
        dc = DixonColesModel(xi=0.0018).fit(train, ref_date=chunk["date"].min())
        bo = BoostedModel().fit(train)
        models[q] = (dc, bo)
        _log(f"  {q}: modelos treinados (treino={len(train)}, teste={len(chunk)})")

    # dedup de chamadas por horario de largada (-5min ~ linha de fechamento)
    slots: dict[str, list] = {}
    for row in test.itertuples():
        iso = (row.date - pd.Timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        slots.setdefault(iso, []).append(row)
    _log(f"{len(slots)} horarios distintos -> ~{len(slots)} chamadas "
         f"(regioes={REGIONS})")

    main_e: list[dict] = []
    boost_e: list[dict] = []
    no_odds = 0
    remaining = None

    for n, (iso, rows) in enumerate(sorted(slots.items()), 1):
        snap = hist_snapshot(iso)
        if not snap:
            no_odds += len(rows)
            continue
        remaining = snap.get("_remaining", remaining)
        # indexa eventos do snapshot por (home_norm, away_norm)
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
            # ---- motor principal: proxy de varejo, edge absoluto 3% ----
            if dc is not None and ret and all(o > 1.0 for o in ret):
                try:
                    p = dc.predict_1x2(row.home_team, row.away_team)
                except (KeyError, ValueError):
                    p = None
                if p:
                    for side, odd in zip("HDA", ret):
                        vb = value.evaluate_bet("1X2:" + side, side, p[side],
                                                odd, min_edge=MAIN_MIN_EDGE,
                                                kelly=KELLY)
                        if vb.stake_fraction > 0:
                            stake = vb.stake_fraction * BANKROLL
                            pnl = stake * (odd - 1) if res == side else -stake
                            main_e.append(dict(date=row.date, side=side, odd=odd,
                                               stake=stake, pnl=pnl,
                                               won=res == side))
            # ---- Boosted: Pinnacle, Shin, filtro triplo ----
            if bo is not None and pin and all(o > 1.0 for o in pin):
                pb = bo.predict_1x2(row.home_team, row.away_team, when=row.date)
                if pb:
                    fair = value.fair_probs(pin, method="shin")
                    for side, odd, fp in zip("HDA", pin, fair):
                        if odd > B_MAX_ODD or pb[side] < fp * (1 + B_REL_EDGE):
                            continue
                        vb = value.evaluate_bet("1X2:" + side, side, pb[side],
                                                odd, fair_prob=fp,
                                                min_edge=B_MIN_EDGE, kelly=KELLY)
                        if vb.stake_fraction > 0:
                            stake = vb.stake_fraction * BANKROLL
                            pnl = stake * (odd - 1) if res == side else -stake
                            boost_e.append(dict(date=row.date, side=side,
                                                odd=odd, stake=stake, pnl=pnl,
                                                won=res == side))
        if n % 20 == 0:
            _log(f"  {n}/{len(slots)} horarios | creditos restantes={remaining}"
                 f" | main={len(main_e)} boosted={len(boost_e)}")
        if remaining is not None and remaining < MIN_CREDITS:
            _log(f"  ! parando: creditos abaixo de {MIN_CREDITS}")
            break

    _log("")
    _log("=" * 64)
    _log(f"BSA — ultimos 12 meses | jogos sem cotacao casada: {no_odds}")
    _log(f"creditos restantes ao final: {remaining}")
    _log("=" * 64)
    report("Motor principal (Dixon-Coles, varejo medio)", main_e)
    report("Boosted Research (Elo+XGB, Pinnacle+Shin)", boost_e)


if __name__ == "__main__":
    main()
