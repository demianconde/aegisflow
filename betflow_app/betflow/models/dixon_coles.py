"""Modelo Dixon-Coles para gols de futebol.

Referencia: Dixon & Coles (1997), "Modelling Association Football Scores and
Inefficiencies in the Football Betting Market". E o padrao-ouro para estimar
probabilidades de 1X2, gols totais (Over/Under) e ambas marcam (BTTS).

Ideia
-----
Cada time tem forca de **ataque** (alpha) e **defesa** (beta). Ha uma vantagem
de mandante global (gamma). Os gols esperados de uma partida sao:

    lambda_home = exp(alpha_home + beta_away + gamma)   # mandante
    lambda_away = exp(alpha_away + beta_home)           # visitante

Os gols seguem Poisson, MAS Dixon-Coles adiciona:

1. **Correcao tau** para placares baixos (0-0, 1-0, 0-1, 1-1), onde o Poisson
   independente erra sistematicamente (subestima empates 0-0/1-1). Parametro
   `rho` controla essa dependencia.
2. **Decaimento temporal** `xi`: jogos antigos pesam menos na verossimilhanca,
   peso = exp(-xi * dias_atras). Captura mudanca de forma/elenco.

Identificabilidade: fixamos sum(alpha)=0 (restricao padrao) para o modelo nao
ter infinitas solucoes equivalentes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson


# ---------------------------------------------------------------------------
# Correcao de dependencia para placares baixos (funcao tau de Dixon-Coles)
# ---------------------------------------------------------------------------
def _tau(h: np.ndarray, a: np.ndarray, lh: np.ndarray, la: np.ndarray,
         rho: float) -> np.ndarray:
    """Fator multiplicativo tau(x, y) aplicado aos 4 placares baixos."""
    out = np.ones_like(lh, dtype=float)
    m00 = (h == 0) & (a == 0)
    m01 = (h == 0) & (a == 1)
    m10 = (h == 1) & (a == 0)
    m11 = (h == 1) & (a == 1)
    out[m00] = 1.0 - lh[m00] * la[m00] * rho
    out[m01] = 1.0 + lh[m01] * rho
    out[m10] = 1.0 + la[m10] * rho
    out[m11] = 1.0 - rho
    return out


@dataclass
class DixonColesModel:
    """Dixon-Coles ajustavel por maxima verossimilhanca.

    Apos `fit`, expoe `attack`, `defence`, `home_adv` (gamma) e `rho`, e permite
    prever a matriz de placares e as probabilidades de mercado de qualquer
    confronto entre times vistos no treino.
    """
    xi: float = 0.0                       # decaimento temporal (0 = sem decaimento)
    ridge: float = 0.0                    # regularizacao L2 das forcas (encolhe p/ media)
    max_goals: int = 10                   # truncamento da matriz de placares
    teams: list[str] = field(default_factory=list)
    attack: dict[str, float] = field(default_factory=dict)
    defence: dict[str, float] = field(default_factory=dict)
    home_adv: float = 0.0
    rho: float = 0.0
    _fitted: bool = False

    # ------------------------------------------------------------------
    # verossimilhanca
    # ------------------------------------------------------------------
    def _neg_log_likelihood(self, params: np.ndarray, idx_h: np.ndarray,
                            idx_a: np.ndarray, gh: np.ndarray, ga: np.ndarray,
                            weights: np.ndarray, n_teams: int) -> float:
        att = params[:n_teams]
        def_ = params[n_teams:2 * n_teams]
        gamma = params[2 * n_teams]
        rho = params[2 * n_teams + 1]

        # restricao de identificabilidade: media dos ataques = 0
        att = att - att.mean()

        lh = np.exp(att[idx_h] + def_[idx_a] + gamma)
        la = np.exp(att[idx_a] + def_[idx_h])

        # log P(gols) sob Poisson independente + correcao tau
        log_p = (poisson.logpmf(gh, lh) + poisson.logpmf(ga, la))
        tau = _tau(gh, ga, lh, la, rho)
        # tau pode ficar <=0 para rho extremo; penaliza (evita log(<=0))
        tau = np.clip(tau, 1e-10, None)
        ll = weights * (log_p + np.log(tau))

        # regularizacao ridge (L2): encolhe ataque/defesa para a media da liga.
        # Escala pela massa de pesos para o termo nao depender do tamanho do
        # treino (mesma "forca" de prior com poucos ou muitos jogos). Estabiliza
        # forcas de times com amostra pequena (a origem das zebras extremas).
        nll = -float(ll.sum())
        if self.ridge > 0.0:
            nll += self.ridge * float(weights.sum()) * float(
                np.mean(att ** 2) + np.mean(def_ ** 2))
        return nll

    def fit(self, df: pd.DataFrame, ref_date: pd.Timestamp | None = None
            ) -> "DixonColesModel":
        """Ajusta o modelo. `df` precisa de home_team, away_team, home_goals,
        away_goals e (se xi>0) date."""
        data = df.dropna(subset=["home_team", "away_team",
                                 "home_goals", "away_goals"]).copy()
        data["home_goals"] = data["home_goals"].astype(int)
        data["away_goals"] = data["away_goals"].astype(int)

        self.teams = sorted(set(data["home_team"]) | set(data["away_team"]))
        index = {t: i for i, t in enumerate(self.teams)}
        n = len(self.teams)

        idx_h = data["home_team"].map(index).to_numpy()
        idx_a = data["away_team"].map(index).to_numpy()
        gh = data["home_goals"].to_numpy()
        ga = data["away_goals"].to_numpy()

        # pesos por decaimento temporal
        if self.xi > 0 and "date" in data.columns:
            ref = ref_date or data["date"].max()
            days = (ref - data["date"]).dt.days.to_numpy().astype(float)
            weights = np.exp(-self.xi * np.clip(days, 0, None))
        else:
            weights = np.ones(len(data), dtype=float)

        # chute inicial: ataque/defesa ~0, gamma pequeno positivo, rho ~0
        x0 = np.concatenate([
            np.zeros(n),               # attack
            np.zeros(n),               # defence
            np.array([0.25]),          # home advantage (gamma)
            np.array([0.0]),           # rho
        ])
        # rho limitado para tau nao explodir
        bounds = [(-3, 3)] * (2 * n) + [(-2, 2), (-0.2, 0.2)]

        res = minimize(
            self._neg_log_likelihood, x0,
            args=(idx_h, idx_a, gh, ga, weights, n),
            method="L-BFGS-B", bounds=bounds,
            options={"maxiter": 500, "ftol": 1e-9},
        )

        att = res.x[:n]
        att = att - att.mean()
        def_ = res.x[n:2 * n]
        self.attack = dict(zip(self.teams, att))
        self.defence = dict(zip(self.teams, def_))
        self.home_adv = float(res.x[2 * n])
        self.rho = float(res.x[2 * n + 1])
        self._fitted = True
        return self

    # ------------------------------------------------------------------
    # previsao
    # ------------------------------------------------------------------
    def _check_fitted(self) -> None:
        if not self._fitted:
            raise RuntimeError("modelo nao ajustado: chame .fit() primeiro")

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        """Gols esperados (lambda) de mandante e visitante."""
        self._check_fitted()
        for t in (home, away):
            if t not in self.attack:
                raise KeyError(f"time desconhecido no treino: {t!r}")
        lh = np.exp(self.attack[home] + self.defence[away] + self.home_adv)
        la = np.exp(self.attack[away] + self.defence[home])
        return float(lh), float(la)

    def score_matrix(self, home: str, away: str) -> np.ndarray:
        """Matriz P[i, j] = prob. de mandante fazer i e visitante fazer j gols."""
        lh, la = self.expected_goals(home, away)
        g = np.arange(self.max_goals + 1)
        ph = poisson.pmf(g, lh)
        pa = poisson.pmf(g, la)
        mat = np.outer(ph, pa)

        # aplica correcao tau nos 4 cantos baixos
        mat[0, 0] *= 1.0 - lh * la * self.rho
        mat[0, 1] *= 1.0 + lh * self.rho
        mat[1, 0] *= 1.0 + la * self.rho
        mat[1, 1] *= 1.0 - self.rho

        mat = np.clip(mat, 0.0, None)
        mat /= mat.sum()   # renormaliza (tau + truncamento quebram a soma 1)
        return mat

    def predict_1x2(self, home: str, away: str) -> dict[str, float]:
        """Probabilidades de vitoria mandante (H), empate (D) e visitante (A)."""
        mat = self.score_matrix(home, away)
        home_win = float(np.tril(mat, -1).sum())   # i > j
        draw = float(np.trace(mat))                # i == j
        away_win = float(np.triu(mat, 1).sum())    # i < j
        return {"H": home_win, "D": draw, "A": away_win}

    def predict_over_under(self, home: str, away: str,
                           line: float = 2.5) -> dict[str, float]:
        """Prob. de Over/Under para uma linha de gols totais (ex.: 2.5)."""
        mat = self.score_matrix(home, away)
        i = np.arange(mat.shape[0])[:, None]
        j = np.arange(mat.shape[1])[None, :]
        totals = i + j
        over = float(mat[totals > line].sum())
        return {"over": over, "under": 1.0 - over}

    def predict_btts(self, home: str, away: str) -> dict[str, float]:
        """Prob. de ambas as equipes marcarem (BTTS)."""
        mat = self.score_matrix(home, away)
        yes = float(mat[1:, 1:].sum())   # ambos >= 1 gol
        return {"yes": yes, "no": 1.0 - yes}

