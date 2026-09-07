"""Calibracao de probabilidades (Ciclo 4).

Um modelo pode *ordenar* bem os eventos (bom poder discriminante) e ainda assim
estar **descalibrado**: quando ele diz "70%", o evento acontece 60% das vezes.
Apostar por valor (EV) com probabilidades descalibradas destroi o edge, porque o
EV depende diretamente de `p`. A calibracao aprende uma funcao monotona

    p_calibrado = g(p_bruto)

a partir de dados historicos (previsto vs. ocorrido) e a aplica antes de calcular
EV/Kelly. Nao mexe na *ordem* das previsoes - so no *nivel*.

Dois metodos classicos, ambos sem dependencias externas (numpy/scipy ja usados):

- **Platt / sigmoid** (`PlattCalibrator`): ajusta uma regressao logistica 1-D
  `sigma(a*logit(p)+b)`. Parametrico e suave; ideal quando o vies e sistematico
  (modelo consistentemente sobre/subconfiante) e ha poucos dados.
- **Isotonica** (`IsotonicCalibrator`): regressao monotona nao-parametrica via
  Pool Adjacent Violators (PAV). Mais flexivel; precisa de mais dados e nunca
  inverte a ordem.

Para o mercado 1X2 (H/D/A) usamos calibracao **one-vs-rest**: cada classe e
calibrada como um problema binario e as tres saidas sao renormalizadas para
somar 1 (`calibrate_1x2`).

IMPORTANTE (sem lookahead): calibradores devem ser treinados APENAS com jogos
anteriores ao que esta sendo apostado. No backtest walk-forward isso e garantido
usando as previsoes acumuladas das janelas passadas.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize

# Evita logit(0)/logit(1) infinitos e divisoes por zero em geral.
_EPS = 1e-6


def _clip01(p: np.ndarray) -> np.ndarray:
    return np.clip(np.asarray(p, dtype=float), _EPS, 1.0 - _EPS)


def _logit(p: np.ndarray) -> np.ndarray:
    p = _clip01(p)
    return np.log(p / (1.0 - p))


def _sigmoid(z: np.ndarray) -> np.ndarray:
    # estavel para z muito grande/pequeno
    return np.where(z >= 0, 1.0 / (1.0 + np.exp(-z)),
                    np.exp(z) / (1.0 + np.exp(z)))


# ---------------------------------------------------------------------------
# Platt scaling (regressao logistica 1-D sobre o logit da prob. bruta)
# ---------------------------------------------------------------------------
@dataclass
class PlattCalibrator:
    """Calibracao sigmoide: `p_cal = sigma(a * logit(p) + b)`.

    Sem calibracao equivale a `a=1, b=0` (funcao identidade no logit). Se houver
    dados insuficientes ou de uma unica classe, cai para a identidade (nao piora).
    """
    a: float = 1.0
    b: float = 0.0
    fitted: bool = False
    method: str = "platt"

    def fit(self, probs, outcomes) -> "PlattCalibrator":
        p = _clip01(np.asarray(probs, dtype=float))
        y = np.asarray(outcomes, dtype=float)
        if p.size == 0 or p.size != y.size:
            return self
        # precisa das duas classes para a logistica ser identificavel
        if y.min() == y.max():
            self.a, self.b, self.fitted = 1.0, 0.0, True
            return self

        z = _logit(p)

        def nll(params: np.ndarray) -> float:
            a, b = params
            q = _sigmoid(a * z + b)
            q = np.clip(q, _EPS, 1.0 - _EPS)
            return float(-np.sum(y * np.log(q) + (1.0 - y) * np.log(1.0 - q)))

        res = minimize(nll, np.array([1.0, 0.0]), method="L-BFGS-B",
                       options={"maxiter": 200})
        self.a, self.b = float(res.x[0]), float(res.x[1])
        self.fitted = True
        return self

    def predict(self, probs) -> np.ndarray:
        p = _clip01(np.asarray(probs, dtype=float))
        if not self.fitted:
            return p
        return _sigmoid(self.a * _logit(p) + self.b)

    # alias no estilo pipeline usado no resto do projeto
    transform = predict


# ---------------------------------------------------------------------------
# Regressao isotonica via Pool Adjacent Violators (PAV)
# ---------------------------------------------------------------------------
def _pav(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pool Adjacent Violators: ajuste monotono nao-decrescente por minimos
    quadrados ponderados. Retorna (x_representativo_do_bloco, y_ajustado).
    """
    order = np.argsort(x, kind="mergesort")
    x, y, w = x[order], y[order], w[order]

    vals: list[float] = []   # valor medio do bloco
    wts: list[float] = []    # peso acumulado do bloco
    xs: list[float] = []     # menor x do bloco
    for xi, yi, wi in zip(x, y, w):
        vals.append(float(yi))
        wts.append(float(wi))
        xs.append(float(xi))
        # funde blocos adjacentes que violam a monotonicidade
        while len(vals) > 1 and vals[-2] > vals[-1]:
            w2 = wts[-1] + wts[-2]
            v2 = (vals[-1] * wts[-1] + vals[-2] * wts[-2]) / w2
            vals[-2], wts[-2] = v2, w2
            xs[-2] = min(xs[-2], xs[-1])
            vals.pop(); wts.pop(); xs.pop()
    return np.asarray(xs, dtype=float), np.asarray(vals, dtype=float)


@dataclass
class IsotonicCalibrator:
    """Calibracao isotonica (monotona nao-decrescente), interpolada linearmente.

    Aprende um mapa por partes a partir dos pontos (prob_bruta -> freq_observada),
    garantindo que probabilidades maiores nunca virem probabilidades menores.
    """
    x_thresholds: np.ndarray = field(default_factory=lambda: np.array([0.0, 1.0]))
    y_thresholds: np.ndarray = field(default_factory=lambda: np.array([0.0, 1.0]))
    fitted: bool = False
    method: str = "isotonic"

    def fit(self, probs, outcomes) -> "IsotonicCalibrator":
        p = _clip01(np.asarray(probs, dtype=float))
        y = np.asarray(outcomes, dtype=float)
        if p.size == 0 or p.size != y.size or y.min() == y.max():
            # nada a aprender: mantem identidade
            self.x_thresholds = np.array([0.0, 1.0])
            self.y_thresholds = np.array([0.0, 1.0])
            self.fitted = True
            return self

        w = np.ones_like(p)
        xs, ys = _pav(p, y, w)
        # colapsa x repetidos (mantendo o 1o do bloco) e garante bordas 0/1
        ux, idx = np.unique(xs, return_index=True)
        uy = np.clip(ys[idx], 0.0, 1.0)
        if ux[0] > 0.0:
            ux = np.concatenate([[0.0], ux]); uy = np.concatenate([[uy[0]], uy])
        if ux[-1] < 1.0:
            ux = np.concatenate([ux, [1.0]]); uy = np.concatenate([uy, [uy[-1]]])
        self.x_thresholds, self.y_thresholds = ux, uy
        self.fitted = True
        return self

    def predict(self, probs) -> np.ndarray:
        p = _clip01(np.asarray(probs, dtype=float))
        if not self.fitted:
            return p
        return np.interp(p, self.x_thresholds, self.y_thresholds)

    transform = predict


# ---------------------------------------------------------------------------
# Fabrica e calibracao multiclasse 1X2
# ---------------------------------------------------------------------------
def fit_1x2_calibrators(samples: dict[str, list[tuple[float, int]]],
                        method: str = "platt",
                        min_samples: int = 20) -> dict[str, Any]:
    """Treina calibradores one-vs-rest {H,D,A} a partir de apostas liquidadas.

    `samples` mapeia cada mercado para uma lista de (prob_modelo, outcome),
    onde outcome=1 para WON e 0 para LOST. Mercados com menos de
    `min_samples` amostras ficam sem calibrador (identidade).
    """
    calibrators: dict[str, Any] = {}
    for key in ("H", "D", "A"):
        pairs = samples.get(key, [])
        if len(pairs) < max(min_samples, 2):
            continue
        probs, outcomes = zip(*pairs)
        cal = make_calibrator(method)
        if cal is None:
            continue
        cal.fit(probs, outcomes)
        calibrators[key] = cal
    return calibrators


def make_calibrator(method: str | None):
    """Cria um calibrador pelo nome. `None`/"none" -> nenhum (identidade)."""
    if method is None or str(method).lower() in ("none", "", "off"):
        return None
    m = str(method).lower()
    if m in ("platt", "sigmoid", "logistic"):
        return PlattCalibrator()
    if m in ("isotonic", "iso"):
        return IsotonicCalibrator()
    raise ValueError(f"metodo de calibracao desconhecido: {method!r}")


def calibrate_1x2(probs: dict[str, float],
                  calibrators: dict[str, object]) -> dict[str, float]:
    """Aplica calibradores one-vs-rest a um dict {H,D,A} e renormaliza p/ somar 1.

    `calibrators` mapeia cada chave (H/D/A) para um calibrador ja treinado. Chaves
    ausentes ficam sem calibracao. A renormalizacao mantem a saida como uma
    distribuicao de probabilidade valida.
    """
    out: dict[str, float] = {}
    for k, p in probs.items():
        cal = calibrators.get(k)
        out[k] = float(cal.predict([p])[0]) if cal is not None else float(p)
    s = sum(out.values())
    if s <= 0:
        # degenerou: devolve o original normalizado
        s0 = sum(probs.values()) or 1.0
        return {k: v / s0 for k, v in probs.items()}
    return {k: v / s for k, v in out.items()}
