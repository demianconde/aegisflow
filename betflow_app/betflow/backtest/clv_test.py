"""Teste de CLV (Closing Line Value) com odds de ABERTURA.

A ideia central do edge de longo prazo: se apostamos na odd de ABERTURA e, na
media, ela e MELHOR que a odd de FECHAMENTO da mesma selecao, entao estamos
'batendo o mercado' - CLV positivo. CLV positivo e o preditor mais confiavel de
lucro sustentavel, mais ainda que o ROI de curto prazo (que e ruidoso).

    CLV de uma aposta = odd_abertura / odd_fechamento - 1
        > 0  => pegamos preco melhor que o mercado no fechamento (BOM)
        < 0  => o mercado se moveu contra nos (RUIM)

Aqui o modelo (walk-forward, sem lookahead) escolhe as selecoes de valor usando
as odds de ABERTURA; para cada aposta registramos o CLV vs. o FECHAMENTO e
tambem o P&L real (liquidado na abertura, como se tivesse apostado cedo).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from betflow.models.dixon_coles import DixonColesModel
from betflow.betting import value
from betflow.backtest import metrics


@dataclass
class CLVConfig:
    min_train: int = 200
    retrain_every: int = 20
    min_edge: float = 0.03
    kelly: float = 0.25
    xi: float = 0.0018
    initial_bankroll: float = 100.0
    blend_weight: float = 0.5       # mistura modelo x mercado (usa a abertura)
    fair_method: str = "shin"
    max_odd: float = 6.0
    flat_stake: float | None = 1.0  # CLV avalia melhor com stake fixo


@dataclass
class CLVResult:
    config: CLVConfig
    n_bets: int
    clv_mean: float                 # CLV medio (a metrica principal)
    clv_positive_rate: float        # % de apostas com CLV > 0
    summary: dict[str, float] = field(default_factory=dict)
    bets: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"config": self.config.__dict__, "n_bets": self.n_bets,
                "clv_mean": self.clv_mean,
                "clv_positive_rate": self.clv_positive_rate,
                "summary": self.summary}


# (abertura, fechamento) por selecao 1X2
_OPEN = ("odds_home_open_avg", "odds_draw_open_avg", "odds_away_open_avg")
_CLOSE = ("odds_home_close_avg", "odds_draw_close_avg", "odds_away_close_avg")


def run(df: pd.DataFrame, config: CLVConfig | None = None) -> CLVResult:
    """Walk-forward: aposta na ABERTURA e mede CLV vs. FECHAMENTO."""
    cfg = config or CLVConfig()
    need = ["date", "home_team", "away_team", "home_goals", "away_goals",
            "result", *_OPEN, *_CLOSE]
    data = df.copy()
    missing = [c for c in need if c not in data.columns]
    if missing:
        raise RuntimeError(f"faltam colunas para CLV: {missing}")
    data = data.dropna(subset=need).sort_values("date").reset_index(drop=True)

    n = len(data)
    if n <= cfg.min_train:
        raise RuntimeError(f"jogos insuficientes: {n} <= {cfg.min_train}")

    model: DixonColesModel | None = None
    trained_upto = -1
    bankroll = cfg.initial_bankroll
    stakes: list[float] = []
    pnls: list[float] = []
    clvs: list[float] = []
    bets: list[dict[str, Any]] = []
    keys = ("H", "D", "A")

    for i in range(cfg.min_train, n):
        if model is None or (i - trained_upto) >= cfg.retrain_every:
            model = DixonColesModel(xi=cfg.xi).fit(
                data.iloc[:i], ref_date=data.iloc[i]["date"])
            trained_upto = i

        row = data.iloc[i]
        home, away = row["home_team"], row["away_team"]
        if home not in model.attack or away not in model.attack:
            continue

        open_odds = {k: float(row[c]) for k, c in zip(keys, _OPEN)}
        close_odds = {k: float(row[c]) for k, c in zip(keys, _CLOSE)}
        if any(v <= 1.0 for v in open_odds.values()) or \
           any(v <= 1.0 for v in close_odds.values()):
            continue

        model_p = model.predict_1x2(home, away)
        # probabilidade justa do mercado a partir da ABERTURA (o preco que pagamos)
        fair = value.fair_probs([open_odds[k] for k in keys],
                                method=cfg.fair_method)
        market_p = dict(zip(keys, fair))
        w = cfg.blend_weight
        probs = {k: w * model_p[k] + (1 - w) * market_p[k] for k in keys}
        s = sum(probs.values())
        probs = {k: v / s for k, v in probs.items()}

        result = row["result"]
        for k in keys:
            odd_open = open_odds[k]
            if odd_open > cfg.max_odd:
                continue
            vb = value.evaluate_bet(
                market=f"1X2:{k}", selection=k, model_prob=probs[k],
                odd=odd_open, fair_prob=1.0 / odd_open,
                min_edge=cfg.min_edge, kelly=cfg.kelly)
            if vb.stake_fraction <= 0:
                continue

            odd_close = close_odds[k]
            clv = odd_open / odd_close - 1.0   # CLV desta aposta
            stake = (cfg.flat_stake if cfg.flat_stake is not None
                     else vb.stake_fraction * bankroll)
            won = (result == k)
            pnl = stake * (odd_open - 1.0) if won else -stake
            bankroll += pnl

            stakes.append(stake)
            pnls.append(pnl)
            clvs.append(clv)
            bets.append({
                "date": str(row["date"].date()),
                "match": f"{home} x {away}", "market": f"1X2:{k}",
                "odd_open": round(odd_open, 3), "odd_close": round(odd_close, 3),
                "clv": round(clv, 4), "model_prob": round(probs[k], 4),
                "edge": round(vb.edge, 4), "won": won, "pnl": round(pnl, 3),
            })

    clv_arr = np.asarray(clvs) if clvs else np.array([0.0])
    summary = metrics.summarize(stakes, pnls, cfg.initial_bankroll)
    return CLVResult(
        config=cfg,
        n_bets=len(bets),
        clv_mean=round(float(clv_arr.mean()), 4) if bets else 0.0,
        clv_positive_rate=round(float((clv_arr > 0).mean()), 4) if bets else 0.0,
        summary=summary,
        bets=bets,
    )
