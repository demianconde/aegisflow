"""Metricas de avaliacao do backtest.

Separadas do motor para poderem ser testadas isoladamente com valores conhecidos.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Ledger:
    """Registro acumulado de apostas para calcular metricas."""
    stakes: list[float]
    pnls: list[float]        # lucro/prejuizo por aposta (unidades)
    bankroll_curve: list[float]

    @property
    def n(self) -> int:
        return len(self.pnls)


def summarize(stakes: list[float], pnls: list[float],
              initial_bankroll: float) -> dict[str, float]:
    """ROI, yield, curva de banca, drawdown e afins a partir das apostas.

    - ROI  = lucro_total / banca_inicial
    - yield= lucro_total / volume_apostado (turnover)  <- metrica de eficiencia
    - max_drawdown = maior queda pico-a-vale da curva de banca (fracao)
    """
    n = len(pnls)
    if n == 0:
        return {"n_bets": 0, "profit": 0.0, "roi": 0.0, "yield": 0.0,
                "turnover": 0.0, "final_bankroll": initial_bankroll,
                "max_drawdown": 0.0, "hit_rate": 0.0}

    profit = float(np.sum(pnls))
    turnover = float(np.sum(stakes))
    wins = int(np.sum([1 for p in pnls if p > 0]))

    # curva de banca e drawdown
    curve = initial_bankroll + np.cumsum(pnls)
    running_max = np.maximum.accumulate(
        np.concatenate([[initial_bankroll], curve]))
    full = np.concatenate([[initial_bankroll], curve])
    drawdowns = (running_max - full) / running_max
    max_dd = float(np.max(drawdowns))

    return {
        "n_bets": n,
        "profit": round(profit, 3),
        "roi": round(profit / initial_bankroll, 4),
        "yield": round(profit / turnover, 4) if turnover else 0.0,
        "turnover": round(turnover, 3),
        "final_bankroll": round(float(curve[-1]), 3),
        "max_drawdown": round(max_dd, 4),
        "hit_rate": round(wins / n, 4),
    }


def brier_score(probs: list[float], outcomes: list[int]) -> float:
    """Brier score medio (0=perfeito, 0.25=chute 50/50). Mede calibracao.

    `probs` = prob. estimada do evento; `outcomes` = 1 se ocorreu, 0 se nao.
    """
    if not probs:
        return float("nan")
    p = np.asarray(probs, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    return round(float(np.mean((p - y) ** 2)), 4)


def calibration_table(probs: list[float], outcomes: list[int],
                      bins: int = 10) -> list[dict[str, float]]:
    """Agrupa por faixa de probabilidade e compara previsto vs. observado.

    Ideal: em cada faixa, prob_media ~ freq_observada (modelo bem calibrado).
    """
    if not probs:
        return []
    p = np.asarray(probs, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    edges = np.linspace(0.0, 1.0, bins + 1)
    table: list[dict[str, float]] = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (p >= lo) & (p < hi) if i < bins - 1 else (p >= lo) & (p <= hi)
        if not mask.any():
            continue
        table.append({
            "bin": f"{lo:.1f}-{hi:.1f}",
            "n": int(mask.sum()),
            "pred_mean": round(float(p[mask].mean()), 4),
            "obs_freq": round(float(y[mask].mean()), 4),
        })
    return table


def clv(bet_odds: list[float], closing_odds: list[float]) -> float:
    """Closing Line Value medio: quanto a odd apostada bate a de fechamento.

    CLV>0 significa que voce pegou odds MELHORES que o fechamento - o preditor
    mais confiavel de lucro no longo prazo. Aqui, como usamos odds de fechamento
    para apostar, o CLV mede a diferenca vs. a odd justa do mercado (proxy).
    """
    if not bet_odds:
        return float("nan")
    b = np.asarray(bet_odds, dtype=float)
    c = np.asarray(closing_odds, dtype=float)
    return round(float(np.mean(b / c - 1.0)), 4)
