"""Backtest walk-forward: o teste honesto de edge do Betflow.

Regra de ouro: para apostar no jogo do dia t, o modelo e treinado APENAS com
jogos anteriores a t (sem lookahead). Avancamos no tempo reajustando o modelo
periodicamente (retreino incremental) e apostamos so quando ha valor
(EV>0, edge>=min_edge), com Kelly fracionario. Ao final, medimos ROI, yield,
drawdown, calibracao e comparamos com baselines honestos.

Isto responde a unica pergunta que importa: "o edge do modelo sobrevive quando
ele NAO conhece o futuro?" Se nao sobreviver aqui, nao ha edge real.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from betflow.models.dixon_coles import DixonColesModel
from betflow.betting import value
from betflow.betting import calibration as calib
from betflow.backtest import metrics


# mapeia o resultado real (H/D/A) para o indice do mercado 1X2
_RESULT_KEY = {"H": "H", "D": "D", "A": "A"}


@dataclass
class BacktestConfig:
    """Parametros do backtest."""
    min_train: int = 200        # jogos minimos antes de comecar a apostar
    retrain_every: int = 20     # reajusta o modelo a cada N jogos
    min_edge: float = 0.03      # so aposta com edge >= isto
    kelly: float = 0.25         # fracao de Kelly
    xi: float = 0.0018          # decaimento temporal do Dixon-Coles
    initial_bankroll: float = 100.0
    flat_stake: float | None = None  # se definido, aposta valor fixo (nao Kelly)
    max_stake_frac: float = 0.10     # trava de seguranca por aposta
    # --- melhorias (Ciclo 6): blending com o mercado + filtros ---
    blend_weight: float = 1.0   # p_final = w*p_modelo + (1-w)*p_mercado_justo
                                # w=1.0 usa so o modelo; w<1 mistura com o mercado
    fair_method: str = "shin"   # remocao de margem p/ p_mercado ("shin"|"proportional")
    max_edge: float = 1.0       # ignora edges irrealisticamente altos (ruido)
    max_odd: float = 1e9        # ignora azarões acima desta odd (caudas ruidosas)
    min_odd: float = 1.0        # ignora favoritos extremos abaixo desta odd
    # --- calibracao (Ciclo 4): corrige o NIVEL das probabilidades ---
    calibration: str | None = None   # None | "platt" | "isotonic"
    calib_min_samples: int = 150     # so calibra apos N previsoes acumuladas


@dataclass
class BacktestResult:
    """Saida completa do backtest."""
    config: BacktestConfig
    n_matches_evaluated: int
    bets: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, float] = field(default_factory=dict)
    calibration: list[dict[str, float]] = field(default_factory=list)
    brier: float = float("nan")
    baseline_fav: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config.__dict__,
            "n_matches_evaluated": self.n_matches_evaluated,
            "n_bets": len(self.bets),
            "summary": self.summary,
            "brier": self.brier,
            "calibration": self.calibration,
            "baseline_favorito": self.baseline_fav,
        }


def _odds_columns(df: pd.DataFrame) -> tuple[str, str, str] | None:
    """Escolhe as melhores colunas de odds 1X2 disponiveis (fechamento > pre)."""
    candidates = [
        ("odds_home_close_avg", "odds_draw_close_avg", "odds_away_close_avg"),
        ("odds_home_close_pinnacle", "odds_draw_close_pinnacle", "odds_away_close_pinnacle"),
        ("odds_home_close_max", "odds_draw_close_max", "odds_away_close_max"),
        ("odds_home_b365", "odds_draw_b365", "odds_away_b365"),
    ]
    for cols in candidates:
        if all(c in df.columns for c in cols):
            return cols


def run(df: pd.DataFrame, config: BacktestConfig | None = None) -> BacktestResult:
    """Executa o backtest walk-forward sobre um DataFrame ordenado por data.

    `df` precisa de: date, home_team, away_team, home_goals, away_goals, result
    e colunas de odds 1X2 (fechamento de preferencia).
    """
    cfg = config or BacktestConfig()
    data = df.dropna(subset=["date", "home_team", "away_team",
                             "home_goals", "away_goals", "result"]).copy()
    data = data.sort_values("date").reset_index(drop=True)

    odds_cols = _odds_columns(data)
    if odds_cols is None:
        raise RuntimeError("nenhuma coluna de odds 1X2 encontrada no DataFrame")
    ch, cd, ca = odds_cols

    n = len(data)
    if n <= cfg.min_train:
        raise RuntimeError(
            f"jogos insuficientes: {n} <= min_train {cfg.min_train}")

    stakes: list[float] = []
    pnls: list[float] = []
    bets: list[dict[str, Any]] = []
    # calibracao: guardamos (prob do resultado que ocorreu, 1) e das demais (p, 0)
    cal_probs: list[float] = []
    cal_out: list[int] = []
    # baseline: apostar 1u no favorito (menor odd) de cada jogo avaliado
    base_stakes: list[float] = []
    base_pnls: list[float] = []

    model: DixonColesModel | None = None
    trained_upto = -1
    bankroll = cfg.initial_bankroll

    # --- calibracao sem lookahead ---------------------------------------
    # Acumulamos (prob prevista, ocorreu?) SO dos jogos ja avaliados e, a cada
    # retreino, reajustamos os calibradores com esse historico estritamente
    # passado. Assim o calibrador nunca "ve" o jogo que estamos apostando.
    use_calib = calib.make_calibrator(cfg.calibration) is not None
    hist_probs: dict[str, list[float]] = {"H": [], "D": [], "A": []}
    hist_out: dict[str, list[int]] = {"H": [], "D": [], "A": []}
    calibrators: dict[str, object] = {}

    def _refit_calibrators() -> None:
        calibrators.clear()
        if not use_calib or len(hist_out["H"]) < cfg.calib_min_samples:
            return
        for k in ("H", "D", "A"):
            c = calib.make_calibrator(cfg.calibration)
            c.fit(hist_probs[k], hist_out[k])
            calibrators[k] = c

    for i in range(cfg.min_train, n):
        # (re)treina usando SOMENTE o passado [0, i) - sem lookahead
        if model is None or (i - trained_upto) >= cfg.retrain_every:
            train = data.iloc[:i]
            model = DixonColesModel(xi=cfg.xi).fit(train, ref_date=data.iloc[i]["date"])
            trained_upto = i
            _refit_calibrators()   # recalibra com o historico acumulado ate aqui

        row = data.iloc[i]
        home, away = row["home_team"], row["away_team"]
        # times precisam ter aparecido no treino
        if home not in model.attack or away not in model.attack:
            continue

        o = {"H": row[ch], "D": row[cd], "A": row[ca]}
        if any(pd.isna(v) or v <= 1.0 for v in o.values()):
            continue

        model_probs = model.predict_1x2(home, away)
        result = row["result"]

        # ---- probabilidade JUSTA do mercado (odd sem margem) ----
        odd_arr = [o["H"], o["D"], o["A"]]
        fair = value.fair_probs(odd_arr, method=cfg.fair_method)
        market_probs = {"H": fair[0], "D": fair[1], "A": fair[2]}

        # ---- BLENDING: mistura modelo com o mercado (reduz variancia) ----
        w = cfg.blend_weight
        probs = {k: w * model_probs[k] + (1.0 - w) * market_probs[k]
                 for k in ("H", "D", "A")}
        # renormaliza (garante soma 1 apos a mistura)
        _s = sum(probs.values())
        probs = {k: v / _s for k, v in probs.items()}

        # ---- alimenta o historico de calibracao (prob BRUTA blendada) ----
        # Guardamos ANTES de calibrar; e o par (previsto, ocorrido) que os
        # calibradores usarao no proximo retreino - sempre com jogos passados.
        if use_calib:
            for k in ("H", "D", "A"):
                hist_probs[k].append(probs[k])
                hist_out[k].append(1 if result == k else 0)

        # ---- aplica a calibracao ja aprendida (se houver) ----
        if calibrators:
            probs = calib.calibrate_1x2(probs, calibrators)

        # ---- calibracao: registra o previsto (ja calibrado) vs. ocorrido ----
        for k in ("H", "D", "A"):
            cal_probs.append(probs[k])
            cal_out.append(1 if result == k else 0)

        # ---- baseline favorito: 1u no menor odd ----
        fav = min(o, key=o.get)
        base_stakes.append(1.0)
        base_pnls.append((o[fav] - 1.0) if result == fav else -1.0)

        # ---- politica de valor: avalia as 3 selecoes, aposta nas de valor ----
        for k in ("H", "D", "A"):
            odd = float(o[k])
            # filtros de sanidade: ignora caudas ruidosas
            if odd > cfg.max_odd or odd < cfg.min_odd:
                continue
            vb = value.evaluate_bet(
                market=f"1X2:{k}", selection=k, model_prob=probs[k], odd=odd,
                fair_prob=1.0 / odd, min_edge=cfg.min_edge, kelly=cfg.kelly)
            if vb.stake_fraction <= 0:
                continue
            # ignora edges irrealisticamente altos (quase sempre ruido/erro)
            if vb.edge > cfg.max_edge:
                continue
            # define o stake (fracao de Kelly da banca atual, ou valor fixo)
            if cfg.flat_stake is not None:
                stake = cfg.flat_stake
            else:
                frac = min(vb.stake_fraction, cfg.max_stake_frac)
                stake = frac * bankroll
            if stake <= 0:
                continue
            won = (result == k)
            pnl = stake * (odd - 1.0) if won else -stake
            bankroll += pnl
            stakes.append(stake)
            pnls.append(pnl)
            bets.append({
                "date": str(row["date"].date()),
                "home_team": home, "away_team": away,
                "market": f"1X2:{k}", "odd": round(odd, 2),
                "model_prob": round(probs[k], 4),
                "edge": round(vb.edge, 4), "ev": round(vb.ev, 4),
                "stake": round(stake, 3), "won": won, "pnl": round(pnl, 3),
                "bankroll": round(bankroll, 3),
            })

    summary = metrics.summarize(stakes, pnls, cfg.initial_bankroll)
    base_summary = metrics.summarize(base_stakes, base_pnls,
                                     cfg.initial_bankroll)
    return BacktestResult(
        config=cfg,
        n_matches_evaluated=n - cfg.min_train,
        bets=bets,
        summary=summary,
        calibration=metrics.calibration_table(cal_probs, cal_out),
        brier=metrics.brier_score(cal_probs, cal_out),
        baseline_fav={"roi": base_summary["roi"], "yield": base_summary["yield"],
                      "n_bets": base_summary["n_bets"]},
    )
