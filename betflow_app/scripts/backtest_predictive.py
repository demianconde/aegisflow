"""Validacao preditiva walk-forward dos ultimos 12 meses: motor principal x Boosted.

Nao usa cotacoes historicas (o warehouse guarda so resultados), entao mede a
QUALIDADE PREDITIVA de cada metodologia, sem look-ahead:

  * para cada trimestre da janela de teste, cada modelo so ve jogos ANTERIORES
    ao trimestre (retreino trimestral);
  * metricas: log-loss (quanto menor melhor) e taxa de acerto do top-1 (H/D/A);
  * baseline de referencia = frequencia historica de H/D/A da propria liga.

Le apenas o que ja esta no warehouse (store.load_matches_df); nao re-ingere
historico. Uso local:  python -m scripts.backtest_predictive
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from betflow.web import store
from betflow.models.dixon_coles import DixonColesModel
from betflow.research.model import BoostedModel

warnings.filterwarnings("ignore")

LEAGUES = ["E0", "F1", "I1", "SP1", "BSA"]
MIN_TRAIN = 120


def _log(msg: str) -> None:
    print(msg, flush=True)


def _res_idx(row) -> int:
    if row.home_goals > row.away_goals:
        return 0
    if row.home_goals < row.away_goals:
        return 2
    return 1


def main() -> None:
    end = pd.Timestamp.utcnow().normalize()
    start = end - pd.Timedelta(days=365)

    tot = {"dc": [], "bo": [], "base": []}
    acc = {"dc": [], "bo": [], "base": []}
    per = []

    for code in LEAGUES:
        df = store.load_matches_df(code, seasons=10)
        if df is None or df.empty:
            _log(f"{code}: sem dados")
            continue
        df = df.sort_values("date").reset_index(drop=True)
        test = df[(df["date"] >= start) & (df["date"] <= end)].copy()
        _log(f"{code}: {len(df)} jogos, {len(test)} na janela de teste")
        test["q"] = test["date"].dt.to_period("Q")

        ll = {"dc": [], "bo": [], "base": []}
        hit = {"dc": [], "bo": [], "base": []}

        for q, chunk in test.groupby("q"):
            train = df[df["date"] < chunk["date"].min()]
            if len(train) < MIN_TRAIN:
                _log(f"  {code} {q}: treino insuficiente ({len(train)})")
                continue
            freq = np.array([(train.apply(_res_idx, axis=1) == k).mean()
                             for k in range(3)])
            try:
                dc = DixonColesModel(xi=0.0018).fit(
                    train, ref_date=chunk["date"].min())
            except Exception as exc:  # noqa: BLE001
                dc = None
                _log(f"  {code} {q}: DC falhou {exc}")
            try:
                bo = BoostedModel().fit(train)
            except Exception as exc:  # noqa: BLE001
                bo = None
                _log(f"  {code} {q}: Boosted falhou {exc}")

            nb = 0
            for row in chunk.itertuples():
                y = _res_idx(row)
                ll["base"].append(-np.log(max(freq[y], 1e-9)))
                hit["base"].append(int(np.argmax(freq) == y))
                if dc is not None:
                    try:
                        p = dc.predict_1x2(row.home_team, row.away_team)
                        v = np.array([p["H"], p["D"], p["A"]])
                        ll["dc"].append(-np.log(max(v[y], 1e-9)))
                        hit["dc"].append(int(np.argmax(v) == y))
                    except Exception:  # noqa: BLE001
                        pass
                if bo is not None:
                    pb = bo.predict_1x2(row.home_team, row.away_team,
                                        when=row.date)
                    if pb:
                        v = np.array([pb["H"], pb["D"], pb["A"]])
                        ll["bo"].append(-np.log(max(v[y], 1e-9)))
                        hit["bo"].append(int(np.argmax(v) == y))
                        nb += 1
            _log(f"  {code} {q}: treino={len(train)} teste={len(chunk)} "
                 f"boosted_prev={nb} [ok]")

        n = len(ll["dc"])
        mean = lambda a: (np.mean(a) if a else None)  # noqa: E731
        per.append((code, len(test), n,
                    mean(ll["dc"]), mean(hit["dc"]),
                    mean(ll["bo"]), mean(hit["bo"]),
                    mean(ll["base"]), mean(hit["base"])))
        for k in tot:
            tot[k] += ll[k]
            acc[k] += hit[k]

    _log("")
    _log("===== RESULTADO =====")
    _log("liga | teste | aval | DC ll/acerto | Boosted ll/acerto | baseline ll/acerto")
    for code, nt, n, a, b, c, d, e, g in per:
        fmt = lambda x: ("%.4f" % x) if x is not None else "-"  # noqa: E731
        fp = lambda x: ("%.1f%%" % (100 * x)) if x is not None else "-"  # noqa: E731
        _log(f"{code:4s} | {nt:4d} | {n:4d} | {fmt(a)} / {fp(b)} | "
             f"{fmt(c)} / {fp(d)} | {fmt(e)} / {fp(g)}")

    n_eval = len(tot["dc"])
    _log("")
    _log(f"CONSOLIDADO ultimos 12 meses ({n_eval} jogos avaliados)")
    labels = [("dc", "Dixon-Coles (principal)"),
              ("bo", "Boosted Research"),
              ("base", "Baseline freq. liga")]
    for k, label in labels:
        if tot[k]:
            _log(f"  {label:24s} log-loss {np.mean(tot[k]):.4f} | "
                 f"acerto {100 * np.mean(acc[k]):.1f}%")
    _log(f"  (log-loss do palpite uniforme = ln 3 = {float(np.log(3)):.4f})")


if __name__ == "__main__":
    main()
