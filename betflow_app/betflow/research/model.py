"""Ensemble da Boosted Research: modelo-base (Elo/Poisson) + refino XGBoost.

Arquitetura em dois estagios (stacking):

  1) MODELO-BASE (Elo bivariado -> Poisson): produz a probabilidade "crua" de
     1X2 a partir das forcas de ataque/defesa. E robusto e nunca falta (funciona
     com poucos jogos).

  2) REFINADOR (Gradient Boosting / XGBoost): recebe COMO FEATURES a saida do
     modelo-base cruzada com os fatores contextuais (fadiga, forma, gols
     esperados, diffs de rating) e reaprende as probabilidades de H/D/A. O
     boosting corrige vieses sistematicos do Poisson (ex.: subestimar empates,
     efeito de calendario apertado) que o modelo-base sozinho nao captura.

Se o XGBoost nao estiver instalado, ou nao houver dados suficientes, o ensemble
degrada com elegancia e devolve as probabilidades do modelo-base.

--------------------------------------------------------------------------------
Matematica da parametrizacao do XGBoost (multi:softprob)
--------------------------------------------------------------------------------
O alvo e a distribuicao categorica de 3 classes (H/D/A). Usamos:

  objective   = "multi:softprob"  -> saida = vetor softmax de 3 probabilidades.
  num_class   = 3
  eval_metric = "mlogloss"        -> log-loss multiclasse; e uma *proper scoring
                                     rule*: minimiza-la exige probabilidades
                                     CALIBRADAS, nao apenas o vencedor certo — e
                                     probabilidade calibrada e o que da +EV.

Cada arvore ajusta o gradiente/hessiana da log-loss. Regularizacao (crucial:
odds carregam pouco sinal e muito ruido, entao overfit destroi o edge):

  max_depth=3        arvores rasas => baixa ordem de interacao; captura efeitos
                     principais (att_diff, mando) sem decorar ruido.
  eta=0.03           shrinkage: cada arvore contribui pouco => generaliza melhor
                     (compensado por mais arvores + early stopping).
  subsample=0.8      amostra 80% das linhas por arvore (bagging estocastico).
  colsample_bytree=0.8  amostra 80% das features por arvore (descorrelaciona).
  min_child_weight=5 exige massa hessiana minima por folha => nao cria folhas
                     baseadas em pouquissimos jogos.
  gamma=0.5          ganho minimo para abrir um no => poda cortes espurios.
  reg_lambda=1.5     L2 nos pesos das folhas => encolhe predicoes extremas.
  n_estimators<=400 + early stopping (50) num holdout temporal => escolhe a
                     complexidade pelos dados, sem overfit ao passado.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from betflow.data import team_names
from betflow.betting import calibration as calib
from betflow.research import features as feat
from betflow.research.elo import BivariateElo

MIN_TRAIN_XGB = 300     # abaixo disso, so o modelo-base (boosting overfit)
_PARAMS = dict(
    objective="multi:softprob", num_class=3, eval_metric="mlogloss",
    max_depth=3, learning_rate=0.03, subsample=0.8, colsample_bytree=0.8,
    min_child_weight=5, gamma=0.5, reg_lambda=1.5, n_estimators=400,
    tree_method="hist",
)


def _try_import_xgb():
    try:
        import xgboost as xgb  # noqa: F401
        return xgb
    except Exception:  # noqa: BLE001 - ambiente sem xgboost => usa modelo-base
        return None


def _games_count(df: pd.DataFrame) -> dict[str, int]:
    """Numero de jogos de cada time no treino (para a confianca por dados)."""
    if df is None or df.empty:
        return {}
    h = df["home_team"].value_counts()
    a = df["away_team"].value_counts()
    return (h.add(a, fill_value=0)).astype(int).to_dict()


@dataclass
class BoostedModel:
    """Ensemble treinavel por liga. `.fit(df)` e `.predict_fixture(...)`."""

    elo: BivariateElo | None = None
    state: dict[str, feat.TeamState] = field(default_factory=dict)
    booster: Any = None
    calibrators: list | None = None       # isotonica por classe (0,1,2)
    n_train: int = 0                       # tamanho do treino (peso do blend)
    games: dict[str, int] = field(default_factory=dict)
    teams: set[str] = field(default_factory=set)
    _fitted: bool = False

    # ------------------------------------------------------------------
    def fit(self, df: pd.DataFrame) -> "BoostedModel":
        X, y, elo, state = feat.build_training_frame(df)
        self.elo, self.state = elo, state
        self.teams = set(elo.attack.keys())
        self.n_train = len(X)
        self.games = _games_count(df)
        self._fitted = True

        xgb = _try_import_xgb()
        if xgb is None or len(X) < MIN_TRAIN_XGB or len(np.unique(y)) < 3:
            self.booster = None
            return self

        # holdout temporal (ultimos 15%) para early stopping honesto E para
        # calibrar a saida softprob (multi:softprob NAO e calibrado de fabrica).
        cut = int(len(X) * 0.85)
        clf = xgb.XGBClassifier(
            **_PARAMS, early_stopping_rounds=50, n_jobs=2,
            random_state=42,
        )
        clf.fit(X.iloc[:cut], y[:cut],
                eval_set=[(X.iloc[cut:], y[cut:])], verbose=False)
        self.booster = clf
        self._fit_calibrators(clf, X.iloc[cut:], y[cut:])
        return self

    def _fit_calibrators(self, clf: Any, X_val: pd.DataFrame,
                         y_val: np.ndarray) -> None:
        """Calibra a softprob por classe (isotonica) num holdout temporal."""
        if len(X_val) < 40:      # poucos dados => sem calibracao (identidade)
            self.calibrators = None
            return
        proba = clf.predict_proba(X_val)
        cals = []
        for k in range(3):
            cal = calib.IsotonicCalibrator()
            cal.fit(proba[:, k], (y_val == k).astype(float))
            cals.append(cal)
        self.calibrators = cals

    def _blend_weight(self) -> float:
        """Peso do Elo no blend Elo+XGB (decai conforme a base cresce).

        Evita a descontinuidade da troca dura em 300 amostras: o Elo (estavel,
        sempre presente) mantem um piso de sinal, e o XGBoost domina so quando
        ha dados para justifica-lo.
        """
        if self.booster is None or self.n_train <= 0:
            return 1.0
        w = MIN_TRAIN_XGB / float(self.n_train)
        return float(min(0.6, max(0.2, w)))

    # ------------------------------------------------------------------
    def _match(self, name: str) -> str | None:
        return team_names.match_team(name, sorted(self.teams)) if self.teams else None

    def fixture_games(self, home_name: str, away_name: str) -> tuple[int, int]:
        """Jogos historicos de cada time do confronto (0 se desconhecido)."""
        hm, am = self._match(home_name), self._match(away_name)
        return self.games.get(hm or "", 0), self.games.get(am or "", 0)

    def predict_1x2(self, home_name: str, away_name: str,
                    when: pd.Timestamp | None = None) -> dict[str, float] | None:
        """Probabilidades {H,D,A} para um confronto; None se time desconhecido."""
        if not self._fitted or self.elo is None:
            return None
        hm, am = self._match(home_name), self._match(away_name)
        if not (hm and am):
            return None

        base = self.elo.predict_1x2(hm, am)
        if self.booster is None:
            return base
        row = feat.fixture_features(self.elo, self.state, hm, am, when)
        X = pd.DataFrame([row], columns=feat.FEATURE_COLS)
        proba = self.booster.predict_proba(X)[0]   # ordem de classes: 0,1,2
        # calibra a softprob por classe (se houve holdout suficiente)
        if self.calibrators is not None:
            proba = np.array([float(self.calibrators[k].predict([proba[k]])[0])
                              for k in range(3)])
        s_xgb = float(proba.sum())
        p_xgb = (proba / s_xgb) if s_xgb else np.array(
            [base["H"], base["D"], base["A"]])
        # blend Elo + XGBoost (em vez de troca dura no limiar de 300 amostras)
        w = self._blend_weight()
        blend = {
            "H": w * base["H"] + (1 - w) * float(p_xgb[0]),
            "D": w * base["D"] + (1 - w) * float(p_xgb[1]),
            "A": w * base["A"] + (1 - w) * float(p_xgb[2]),
        }
        s = blend["H"] + blend["D"] + blend["A"]
        return {k: v / s for k, v in blend.items()} if s else base
