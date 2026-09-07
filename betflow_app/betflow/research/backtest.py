"""Backtesting da Boosted Research.

Duas medidas complementares:

1) QUALIDADE PREDITIVA (walk-forward): treina no passado e mede, no futuro
   nao visto, log-loss multiclasse, Brier score e acuracia. Sao *proper scoring
   rules*: recompensam probabilidade calibrada, nao "chute do favorito".

2) CLOSING LINE VALUE (CLV): o teste de ouro de um modelo de valor. Se, na
   media, pegamos odds MELHORES do que a linha de fechamento da Pinnacle, o
   modelo esta a frente do mercado — o preditor mais confiavel de lucro no longo
   prazo. Requer as odds de entrada e de fechamento (coletadas na operacao);
   aqui fica a funcao que as agrega assim que existirem no track record.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from betflow.research.model import BoostedModel


def walk_forward(df: pd.DataFrame, train_frac: float = 0.6) -> dict[str, Any]:
    """Treina nos primeiros `train_frac` jogos e avalia no restante.

    Retorna log-loss, Brier e acuracia no conjunto de teste (fora da amostra).
    """
    data = df.dropna(subset=["home_team", "away_team",
                             "home_goals", "away_goals"]).copy()
    if "date" in data.columns:
        data = data.sort_values("date")
    if len(data) < 100:
        return {"n_test": 0, "note": "amostra insuficiente para backtest"}

    cut = int(len(data) * train_frac)
    train_df, test_df = data.iloc[:cut], data.iloc[cut:]
    model = BoostedModel().fit(train_df)

    eps = 1e-12
    ll = brier = 0.0
    hit = n = 0
    for r in test_df.itertuples(index=False):
        probs = model.predict_1x2(r.home_team, r.away_team)
        if not probs:
            continue
        gh, ga = int(r.home_goals), int(r.away_goals)
        y = 0 if gh > ga else (1 if gh == ga else 2)
        p = np.array([probs["H"], probs["D"], probs["A"]], dtype=float)
        p = np.clip(p, eps, 1.0)
        onehot = np.zeros(3)
        onehot[y] = 1.0
        ll += -np.log(p[y])
        brier += float(np.sum((p - onehot) ** 2))
        hit += int(np.argmax(p) == y)
        n += 1

    if n == 0:
        return {"n_test": 0, "note": "nenhuma predicao valida no teste"}
    return {
        "n_train": len(train_df), "n_test": n,
        "log_loss": ll / n, "brier": brier / n, "accuracy": hit / n,
        "baseline_log_loss": float(-np.log(1.0 / 3.0)),  # chute uniforme
    }


def clv_report(entries: list[dict[str, float]]) -> dict[str, Any]:
    """Agrega o Closing Line Value de sinais com odd de entrada e de fechamento.

    `entries`: lista de {"entry_odd": x, "closing_odd": y} da MESMA selecao.
    CLV por sinal = entry_odd / closing_odd - 1 (positivo = pegamos preco melhor
    que o fechamento). "beat_close_rate" = fracao de sinais que bateram a linha.
    """
    vals = []
    for e in entries:
        eo, co = e.get("entry_odd"), e.get("closing_odd")
        if eo and co and co > 1.0:
            vals.append(eo / co - 1.0)
    if not vals:
        return {"n": 0, "note": "sem odds de fechamento coletadas ainda"}
    arr = np.array(vals, dtype=float)
    return {
        "n": len(arr),
        "avg_clv": float(arr.mean()),
        "median_clv": float(np.median(arr)),
        "beat_close_rate": float((arr > 0).mean()),
    }
