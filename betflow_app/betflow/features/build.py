"""Engenharia de features a partir do schema canonico (Fonte A).

Regra de ouro: **sem lookahead**. Toda feature calculada para o jogo t usa
apenas jogos ESTRITAMENTE anteriores a t. Isso e o que separa um backtest
honesto de uma ilusao de lucro.

As features aqui sao "leves" (medias moveis de forma, taxas de escanteios e
cartoes). As forcas de ataque/defesa propriamente ditas sao estimadas dentro do
modelo Dixon-Coles (`betflow.models.dixon_coles`), que ja aprende esses
parametros via maxima verossimilhanca.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _team_long_format(df: pd.DataFrame) -> pd.DataFrame:
    """Explode cada jogo em duas linhas (mandante/visitante) para rolling por time.

    Cada linha vira a "visao" de um time num jogo, preservando o indice original
    do jogo em `match_id` para reagrupar depois.
    """
    df = df.reset_index(drop=True).copy()
    df["match_id"] = df.index

    home = pd.DataFrame({
        "match_id": df["match_id"],
        "date": df["date"],
        "team": df["home_team"],
        "opponent": df["away_team"],
        "is_home": 1,
        "goals_for": df.get("home_goals"),
        "goals_against": df.get("away_goals"),
        "corners_for": df.get("home_corners"),
        "corners_against": df.get("away_corners"),
        "cards_for": df.get("home_yellow", 0) + df.get("home_red", 0),
        "shots_for": df.get("home_shots"),
        "shots_target_for": df.get("home_shots_target"),
    })
    away = pd.DataFrame({
        "match_id": df["match_id"],
        "date": df["date"],
        "team": df["away_team"],
        "opponent": df["home_team"],
        "is_home": 0,
        "goals_for": df.get("away_goals"),
        "goals_against": df.get("home_goals"),
        "corners_for": df.get("away_corners"),
        "corners_against": df.get("home_corners"),
        "cards_for": df.get("away_yellow", 0) + df.get("away_red", 0),
        "shots_for": df.get("away_shots"),
        "shots_target_for": df.get("away_shots_target"),
    })
    long = pd.concat([home, away], ignore_index=True)
    return long.sort_values(["team", "date", "match_id"]).reset_index(drop=True)


def _points(gf: pd.Series, ga: pd.Series) -> pd.Series:
    return np.where(gf > ga, 3, np.where(gf == ga, 1, 0))


def rolling_team_form(df: pd.DataFrame, window: int = 5) -> pd.DataFrame:
    """Medias moveis por time SEM lookahead (shift(1) antes do rolling).

    Retorna um DataFrame indexado por (match_id, is_home) com as features
    prontas para juntar de volta ao jogo. Colunas: pontos/jogo, gols pro/contra,
    escanteios pro/contra e cartoes, todos medios na janela recente.
    """
    long = _team_long_format(df)
    long["points"] = _points(long["goals_for"], long["goals_against"])

    metrics = {
        "points": "form_points",
        "goals_for": "form_goals_for",
        "goals_against": "form_goals_against",
        "corners_for": "form_corners_for",
        "corners_against": "form_corners_against",
        "cards_for": "form_cards_for",
        "shots_target_for": "form_shots_target_for",
    }

    grp = long.groupby("team", group_keys=False)
    for src, dst in metrics.items():
        # shift(1): so passado. min_periods=1: usa o que tiver ate encher a janela.
        long[dst] = grp[src].apply(
            lambda s: s.shift(1).rolling(window, min_periods=1).mean()
        )

    keep = ["match_id", "is_home", "team", "opponent", *metrics.values()]
    return long[keep]


def build_match_features(df: pd.DataFrame, window: int = 5) -> pd.DataFrame:
    """Junta as features de mandante e visitante em uma linha por jogo.

    Sufixos `_home` / `_away`. Tambem cria diffs (home - away) uteis como sinais.
    """
    form = rolling_team_form(df, window=window)

    home = (form[form["is_home"] == 1]
            .drop(columns=["is_home", "team", "opponent"])
            .set_index("match_id")
            .add_suffix("_home"))
    away = (form[form["is_home"] == 0]
            .drop(columns=["is_home", "team", "opponent"])
            .set_index("match_id")
            .add_suffix("_away"))

    feats = home.join(away, how="outer")

    # diffs (sinal direto de superioridade recente)
    for base in ("form_points", "form_goals_for", "form_goals_against",
                 "form_corners_for", "form_shots_target_for"):
        feats[f"{base}_diff"] = feats[f"{base}_home"] - feats[f"{base}_away"]

    base = df.reset_index(drop=True).copy()
    base["match_id"] = base.index
    out = base.merge(feats, left_on="match_id", right_index=True, how="left")
    return out.sort_values("date").reset_index(drop=True)
