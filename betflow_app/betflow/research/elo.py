"""Elo bivariado dinamico (forca de ataque e defesa) para gols de futebol.

Diferente do Elo classico (que rastreia uma unica forca por time), aqui cada
time tem DUAS forcas latentes em espaco log:

    att[time]  -> quao acima/abaixo da media o time PRODUZ gols
    dfc[time]  -> quao acima/abaixo da media o time SOFRE gols (defesa)

Os gols esperados de uma partida sao modelados como Poisson:

    lambda_casa  = exp(mu_casa  + att[casa]  - dfc[visitante])
    lambda_visit = exp(mu_visit + att[visit] - dfc[casa])

onde ``mu_casa``/``mu_visit`` sao as medias da liga (o mando de campo aparece
naturalmente porque ``mu_casa > mu_visit``).

Atualizacao online (rodada a rodada): a derivada do log-verossimilhanca de
Poisson em relacao a ``att`` e ``(gols_observados - lambda)``. Logo, apos cada
jogo movemos as forcas na direcao do "erro de gols", escalado por uma taxa de
aprendizado ``k``. Como o erro ja e ponderado por ``lambda`` (que depende da
forca do oponente), o ajuste e naturalmente calibrado pela forca do adversario.

Processando os jogos em ordem cronologica, as forcas mais recentes pesam mais
(memoria exponencial implicita), capturando mudanca de forma/elenco — o analogo
online do decaimento temporal ``xi`` do Dixon-Coles.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import poisson


@dataclass
class BivariateElo:
    """Elo bivariado ataque/defesa ajustado por replay cronologico."""

    k: float = 0.045              # taxa de aprendizado do update online
    max_goals: int = 10           # truncamento da matriz de placares
    mu_home: float = 0.0          # log-media de gols do mandante (liga)
    mu_away: float = 0.0          # log-media de gols do visitante (liga)
    attack: dict[str, float] = field(default_factory=dict)
    defence: dict[str, float] = field(default_factory=dict)
    _fitted: bool = False

    # ------------------------------------------------------------------
    def _ensure(self, team: str) -> None:
        self.attack.setdefault(team, 0.0)
        self.defence.setdefault(team, 0.0)

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        """Gols esperados (lambda) de mandante e visitante."""
        ah, dh = self.attack.get(home, 0.0), self.defence.get(home, 0.0)
        aa, da = self.attack.get(away, 0.0), self.defence.get(away, 0.0)
        lh = float(np.exp(self.mu_home + ah - da))
        la = float(np.exp(self.mu_away + aa - dh))
        # limita para evitar explosao numerica em series ruidosas
        return min(lh, 8.0), min(la, 8.0)

    def _update(self, home: str, away: str, gh: int, ga: int) -> None:
        """Passo online: move att/dfc na direcao do erro de gols de cada lado."""
        lh, la = self.expected_goals(home, away)
        eh, ea = gh - lh, ga - la          # residuo de Poisson (gradiente)
        # mandante marca gh: sobe att[casa], desce dfc[visitante]
        self.attack[home] += self.k * eh
        self.defence[away] -= self.k * eh
        # visitante marca ga: sobe att[visit], desce dfc[casa]
        self.attack[away] += self.k * ea
        self.defence[home] -= self.k * ea

    # ------------------------------------------------------------------
    def fit(self, df: pd.DataFrame) -> "BivariateElo":
        """Ajusta por replay cronologico. `df`: home_team, away_team,
        home_goals, away_goals e (idealmente) date."""
        data = df.dropna(subset=["home_team", "away_team",
                                 "home_goals", "away_goals"]).copy()
        if "date" in data.columns:
            data = data.sort_values("date")
        data["home_goals"] = data["home_goals"].astype(int)
        data["away_goals"] = data["away_goals"].astype(int)

        # baselines da liga (log-media de gols por mando)
        self.mu_home = float(np.log(max(data["home_goals"].mean(), 0.2)))
        self.mu_away = float(np.log(max(data["away_goals"].mean(), 0.2)))
        self.attack.clear()
        self.defence.clear()
        for t in set(data["home_team"]) | set(data["away_team"]):
            self._ensure(t)

        for row in data.itertuples(index=False):
            self._update(row.home_team, row.away_team,
                         int(row.home_goals), int(row.away_goals))
        self._fitted = True
        return self

    # ------------------------------------------------------------------
    def score_matrix(self, home: str, away: str) -> np.ndarray:
        lh, la = self.expected_goals(home, away)
        g = np.arange(self.max_goals + 1)
        mat = np.outer(poisson.pmf(g, lh), poisson.pmf(g, la))
        s = mat.sum()
        return mat / s if s else mat

    def predict_1x2(self, home: str, away: str) -> dict[str, float]:
        """Probabilidades de vitoria mandante (H), empate (D), visitante (A)."""
        mat = self.score_matrix(home, away)
        return {
            "H": float(np.tril(mat, -1).sum()),
            "D": float(np.trace(mat)),
            "A": float(np.triu(mat, 1).sum()),
        }
