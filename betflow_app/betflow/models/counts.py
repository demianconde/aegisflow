"""Modelos de contagem para escanteios e cartoes.

Escanteios
----------
Escanteios sao **superdispersos**: a variancia observada e maior que a media, o
que viola a suposicao do Poisson (var == media). Usar Poisson subestima a cauda
(muitos/poucos escanteios) e mal-precifica linhas de Over/Under. A **Binomial
Negativa** (NB) adiciona um parametro de dispersao e modela essa cauda gorda.

Cartoes
-------
Cartoes sao contagens raras e fortemente influenciadas pelo **arbitro**. Um
Poisson sobre a media do confronto + ajuste pela severidade do arbitro captura
bem o comportamento sem sobre-ajustar (poucos eventos por jogo).

Ambos os modelos estimam a media do total do jogo (mandante + visitante) e
convertem em probabilidade de linhas Over/Under.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import nbinom, poisson


def _nb_params_from_mean_var(mean: float, var: float) -> tuple[float, float]:
    """Converte (media, variancia) na parametrizacao (n, p) do scipy.nbinom.

    Para NB: var = mean + mean^2 / n  =>  n = mean^2 / (var - mean),
    p = n / (n + mean). Se nao houver superdispersao (var<=mean), cai no Poisson
    (n grande => NB -> Poisson).
    """
    if var <= mean:
        n = 1e6  # aproxima Poisson
    else:
        n = mean * mean / (var - mean)
    p = n / (n + mean)
    return n, p


@dataclass
class NegativeBinomialTotals:
    """Modela o TOTAL de eventos por jogo (ex.: escanteios) via NB.

    Simplicidade proposital: aprende media e variancia globais do total e,
    opcionalmente, ajusta pela media dos dois times. Robusto com poucos dados.
    """
    mean_: float = 0.0
    var_: float = 0.0
    team_rate_: dict[str, float] | None = None
    league_mean_: float = 0.0
    _fitted: bool = False

    def fit(self, df: pd.DataFrame, home_col: str = "home_corners",
            away_col: str = "away_corners") -> "NegativeBinomialTotals":
        data = df.dropna(subset=[home_col, away_col]).copy()
        totals = (data[home_col] + data[away_col]).to_numpy(dtype=float)
        self.mean_ = float(totals.mean())
        self.var_ = float(totals.var(ddof=1))
        self.league_mean_ = self.mean_

        # taxa relativa de cada time (soma de eventos pro+contra / n jogos),
        # normalizada pela media da liga -> multiplicador ~1.0
        rate: dict[str, list[float]] = {}
        for _, r in data.iterrows():
            rate.setdefault(r["home_team"], []).append(r[home_col] + r[away_col])
            rate.setdefault(r["away_team"], []).append(r[home_col] + r[away_col])
        base = self.mean_ if self.mean_ > 0 else 1.0
        self.team_rate_ = {t: float(np.mean(v)) / base for t, v in rate.items()}
        self._fitted = True
        return self

    def expected_total(self, home: str | None = None,
                       away: str | None = None) -> float:
        """Total esperado do jogo, ajustado pelos times se conhecidos."""
        if not self._fitted:
            raise RuntimeError("modelo nao ajustado")
        if home is None or away is None or not self.team_rate_:
            return self.mean_
        mult_h = self.team_rate_.get(home, 1.0)
        mult_a = self.team_rate_.get(away, 1.0)
        return self.mean_ * (mult_h + mult_a) / 2.0

    def prob_over(self, line: float, home: str | None = None,
                  away: str | None = None) -> float:
        """P(total > line) sob NB com a media (ajustada) e a variancia global."""
        mean = self.expected_total(home, away)
        # escala a variancia proporcionalmente a media (mantem a dispersao)
        var = self.var_ * (mean / self.mean_) if self.mean_ > 0 else self.var_
        n, p = _nb_params_from_mean_var(mean, max(var, mean + 1e-6))
        # P(X > line) = 1 - P(X <= floor(line))
        k = int(np.floor(line))
        return float(1.0 - nbinom.cdf(k, n, p))

    def over_under(self, line: float, home: str | None = None,
                   away: str | None = None) -> dict[str, float]:
        over = self.prob_over(line, home, away)
        return {"over": over, "under": 1.0 - over}


@dataclass
class PoissonCards:
    """Total de cartoes por jogo via Poisson, com ajuste opcional por arbitro."""
    mean_: float = 0.0
    referee_mult_: dict[str, float] | None = None
    _fitted: bool = False

    def fit(self, df: pd.DataFrame, home_col: str = "home_yellow",
            away_col: str = "away_yellow",
            referee_col: str = "referee") -> "PoissonCards":
        data = df.dropna(subset=[home_col, away_col]).copy()
        totals = (data[home_col] + data[away_col]).astype(float)
        self.mean_ = float(totals.mean())

        if referee_col in data.columns:
            data = data.assign(_tot=totals)
            grp = data.groupby(referee_col)["_tot"].mean()
            base = self.mean_ if self.mean_ > 0 else 1.0
            # so confia em arbitros com amostra minima
            counts = data.groupby(referee_col)["_tot"].count()
            self.referee_mult_ = {
                ref: float(m / base)
                for ref, m in grp.items() if counts.get(ref, 0) >= 5
            }
        self._fitted = True
        return self

    def expected_total(self, referee: str | None = None) -> float:
        if not self._fitted:
            raise RuntimeError("modelo nao ajustado")
        if referee and self.referee_mult_ and referee in self.referee_mult_:
            return self.mean_ * self.referee_mult_[referee]
        return self.mean_

    def prob_over(self, line: float, referee: str | None = None) -> float:
        mean = self.expected_total(referee)
        k = int(np.floor(line))
        return float(1.0 - poisson.cdf(k, mean))

    def over_under(self, line: float, referee: str | None = None
                   ) -> dict[str, float]:
        over = self.prob_over(line, referee)
        return {"over": over, "under": 1.0 - over}
