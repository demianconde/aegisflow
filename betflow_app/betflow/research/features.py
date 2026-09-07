"""Engenharia de features da Boosted Research.

Gera a matriz de treino fazendo um *replay* cronologico: para cada jogo,
fotografa o estado (ratings de ataque/defesa, fadiga, forma) ANTES da partida
e so entao aplica o resultado. Isso evita vazamento de dado (look-ahead): o
modelo so ve o que era conhecido no momento da aposta.

Fatores capturados (a partir de placares + datas, unico dado disponivel hoje):
  * forcas de ataque/defesa (Elo bivariado) de mandante e visitante;
  * probabilidades "cruas" do modelo-base (Elo -> Poisson -> 1X2);
  * gols esperados de cada lado;
  * fadiga: dias de descanso desde o ultimo jogo de cada time;
  * forma recente: pontos e saldo de gols nos ultimos 5 jogos.

Campos avancados (xG, xT, PPDA) entram aqui quando uma fonte os fornecer —
basta adicionar colunas em FEATURE_COLS e preenche-las no replay.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from betflow.research.elo import BivariateElo

FEATURE_COLS = [
    "att_home", "dfc_home", "att_away", "dfc_away",
    "att_diff", "dfc_diff",
    "exp_gh", "exp_ga",
    "base_H", "base_D", "base_A",
    "rest_home", "rest_away",
    "form_home", "form_away",
    "gd_home", "gd_away",
]

_REST_CAP = 21.0     # dias de descanso saturam em 3 semanas
_FORM_N = 5          # janela de forma recente


@dataclass
class TeamState:
    """Estado corrente de um time durante/apos o replay (para features futuras)."""
    last_date: pd.Timestamp | None = None
    results: deque = field(default_factory=lambda: deque(maxlen=_FORM_N))
    # cada item: (pontos, saldo_de_gols) do jogo


def _rest_days(state: TeamState, when: pd.Timestamp | None) -> float:
    if state.last_date is None or when is None:
        return _REST_CAP
    d = (when - state.last_date).days
    return float(np.clip(d, 0.0, _REST_CAP))


def _form(state: TeamState) -> tuple[float, float]:
    """(pontos normalizados, saldo de gols medio) nos ultimos jogos."""
    if not state.results:
        return 0.5, 0.0
    pts = sum(r[0] for r in state.results)
    gd = sum(r[1] for r in state.results)
    return pts / (3.0 * len(state.results)), gd / len(state.results)


def _push_result(state: TeamState, when: pd.Timestamp | None,
                 goals_for: int, goals_against: int) -> None:
    pts = 3 if goals_for > goals_against else (1 if goals_for == goals_against else 0)
    state.results.append((pts, goals_for - goals_against))
    state.last_date = when


def _feature_row(elo: BivariateElo, home: str, away: str,
                 sh: TeamState, sa: TeamState,
                 when: pd.Timestamp | None) -> dict[str, float]:
    lh, la = elo.expected_goals(home, away)
    base = elo.predict_1x2(home, away)
    fh_pts, fh_gd = _form(sh)
    fa_pts, fa_gd = _form(sa)
    return {
        "att_home": elo.attack.get(home, 0.0),
        "dfc_home": elo.defence.get(home, 0.0),
        "att_away": elo.attack.get(away, 0.0),
        "dfc_away": elo.defence.get(away, 0.0),
        "att_diff": elo.attack.get(home, 0.0) - elo.attack.get(away, 0.0),
        "dfc_diff": elo.defence.get(home, 0.0) - elo.defence.get(away, 0.0),
        "exp_gh": lh, "exp_ga": la,
        "base_H": base["H"], "base_D": base["D"], "base_A": base["A"],
        "rest_home": _rest_days(sh, when), "rest_away": _rest_days(sa, when),
        "form_home": fh_pts, "form_away": fa_pts,
        "gd_home": fh_gd, "gd_away": fa_gd,
    }


def build_training_frame(df: pd.DataFrame
                         ) -> tuple[pd.DataFrame, np.ndarray, BivariateElo,
                                    dict[str, TeamState]]:
    """Replay cronologico -> (X, y, elo_ajustado, estado_por_time).

    y: 0=vitoria mandante, 1=empate, 2=vitoria visitante.
    O `elo` e o `estado_por_time` retornados ja refletem TODO o historico e
    servem para montar features de jogos futuros (predicao).
    """
    data = df.dropna(subset=["home_team", "away_team",
                             "home_goals", "away_goals"]).copy()
    if "date" in data.columns:
        data = data.sort_values("date")
    data["home_goals"] = data["home_goals"].astype(int)
    data["away_goals"] = data["away_goals"].astype(int)

    elo = BivariateElo()
    # baselines da liga antes do replay
    elo.mu_home = float(np.log(max(data["home_goals"].mean(), 0.2)))
    elo.mu_away = float(np.log(max(data["away_goals"].mean(), 0.2)))
    for t in set(data["home_team"]) | set(data["away_team"]):
        elo._ensure(t)

    state: dict[str, TeamState] = {}
    rows: list[dict[str, float]] = []
    labels: list[int] = []
    for r in data.itertuples(index=False):
        home, away = r.home_team, r.away_team
        when = getattr(r, "date", None)
        sh = state.setdefault(home, TeamState())
        sa = state.setdefault(away, TeamState())
        rows.append(_feature_row(elo, home, away, sh, sa, when))
        gh, ga = int(r.home_goals), int(r.away_goals)
        labels.append(0 if gh > ga else (1 if gh == ga else 2))
        # aplica o resultado ao estado (pos-jogo)
        elo._update(home, away, gh, ga)
        _push_result(sh, when, gh, ga)
        _push_result(sa, when, ga, gh)

    elo._fitted = True
    X = pd.DataFrame(rows, columns=FEATURE_COLS)
    return X, np.asarray(labels, dtype=int), elo, state


def fixture_features(elo: BivariateElo, state: dict[str, TeamState],
                     home: str, away: str,
                     when: pd.Timestamp | None = None) -> dict[str, float]:
    """Monta o vetor de features de um jogo futuro a partir do estado atual."""
    sh = state.get(home, TeamState())
    sa = state.get(away, TeamState())
    return _feature_row(elo, home, away, sh, sa, when)
