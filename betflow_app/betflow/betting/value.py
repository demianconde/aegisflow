"""Deteccao de value bets e gestao de banca.

A ideia central do Betflow vive aqui. A casa de aposta nao vende a probabilidade
"justa" de um evento: ela infla as odds implicitas de modo que a soma das
probabilidades implicitas seja > 1. Esse excesso e a *margem* (overround / vig).

    prob_implicita_bruta = 1 / odd
    overround = sum(prob_implicita_bruta) - 1

Para saber o que a casa "realmente acha", removemos a margem e obtemos a
probabilidade justa do mercado. Nosso modelo produz uma probabilidade propria
`p_modelo`. Ha *value* (aposta de valor) quando:

    EV = p_modelo * (odd - 1) - (1 - p_modelo) > 0
        <=>  p_modelo > 1 / odd            (edge positivo)

O tamanho da aposta segue o criterio de **Kelly**, que maximiza o crescimento
geometrico da banca no longo prazo:

    f* = (b * p - q) / b ,  onde b = odd - 1, q = 1 - p

Usamos **Kelly fracionario** (ex.: 1/4) porque o Kelly cheio e volatil demais e
sensivel a erro de estimativa de `p` - e o nosso `p` e estimado, nao conhecido.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


# ---------------------------------------------------------------------------
# Probabilidades implicitas / remocao de margem
# ---------------------------------------------------------------------------
def implied_prob(odds: np.ndarray | list[float]) -> np.ndarray:
    """Probabilidade implicita BRUTA (com margem) de odds decimais: 1/odd."""
    odds = np.asarray(odds, dtype=float)
    if np.any(odds <= 1.0):
        raise ValueError("odds decimais devem ser > 1.0")
    return 1.0 / odds


def overround(odds: np.ndarray | list[float]) -> float:
    """Margem da casa (vig). >0 significa livro a favor da casa.

    Ex.: 1X2 com overround 0.06 => a casa embutiu ~6% de vantagem.
    """
    return float(implied_prob(odds).sum() - 1.0)


def fair_probs(odds: np.ndarray | list[float], method: str = "proportional") -> np.ndarray:
    """Remove a margem e devolve probabilidades justas que somam 1.

    - "proportional" (normalizacao multiplicativa): divide cada 1/odd pela soma.
      Simples e padrao de mercado; tende a superestimar favoritos levemente.
    - "shin": modelo de Shin (1993) que atribui parte da margem a apostadores
      informados, corrigindo o vies favorito-azarao. Melhor para 1X2/2-vias.
    """
    p = implied_prob(odds)
    if method == "proportional":
        return p / p.sum()
    if method == "shin":
        return _shin_probs(p)
    raise ValueError(f"metodo desconhecido: {method!r}")


def _shin_probs(raw: np.ndarray) -> np.ndarray:
    """Estima probabilidades justas via modelo de Shin.

    Resolve numericamente o z (fracao de dinheiro informado) tal que as
    probabilidades reconstruidas somem 1. Formula padrao:
        p_i = ( sqrt(z^2 + 4(1-z) * raw_i^2 / booksum) - z ) / (2(1-z))
    """
    booksum = raw.sum()
    # z entre 0 e overround; busca por bisseccao (monotona e estavel).
    lo, hi = 0.0, 0.5
    for _ in range(100):
        z = 0.5 * (lo + hi)
        p = (np.sqrt(z * z + 4.0 * (1.0 - z) * raw * raw / booksum) - z) / (2.0 * (1.0 - z))
        s = p.sum()
        if s > 1.0:
            lo = z
        else:
            hi = z
    z = 0.5 * (lo + hi)
    p = (np.sqrt(z * z + 4.0 * (1.0 - z) * raw * raw / booksum) - z) / (2.0 * (1.0 - z))
    return p / p.sum()


# ---------------------------------------------------------------------------
# Valor esperado e Kelly
# ---------------------------------------------------------------------------
def expected_value(prob: float, odd: float) -> float:
    """EV por unidade apostada. EV>0 => aposta de valor.

    EV = p*(odd-1) - (1-p) = p*odd - 1
    """
    if odd <= 1.0:
        raise ValueError("odd decimal deve ser > 1.0")
    if not 0.0 <= prob <= 1.0:
        raise ValueError("prob deve estar em [0, 1]")
    return prob * odd - 1.0


def edge(prob: float, odd: float) -> float:
    """Vantagem sobre a probabilidade implicita: p_modelo - 1/odd."""
    return prob - 1.0 / odd


def kelly_fraction(prob: float, odd: float, fraction: float = 1.0) -> float:
    """Fracao da banca a apostar (Kelly), opcionalmente escalada por `fraction`.

    Retorna 0.0 quando nao ha valor (nunca aposta contra o proprio edge).
    `fraction=0.25` => Kelly 1/4 (conservador).
    """
    b = odd - 1.0
    q = 1.0 - prob
    f_star = (b * prob - q) / b
    if f_star <= 0.0:
        return 0.0
    return float(f_star * fraction)


@dataclass
class ValueBet:
    """Uma oportunidade de aposta avaliada pelo motor."""
    market: str          # ex.: "1X2:H", "OU2.5:over", "corners:over9.5"
    selection: str       # rotulo legivel
    odd: float           # melhor odd decimal disponivel
    model_prob: float    # probabilidade estimada pelo modelo
    fair_prob: float     # probabilidade justa implicita (sem margem), se houver
    ev: float
    edge: float
    stake_fraction: float  # fracao da banca (Kelly fracionario)

    @property
    def is_value(self) -> bool:
        return self.ev > 0.0


def evaluate_bet(market: str, selection: str, model_prob: float, odd: float,
                 fair_prob: float | None = None, min_edge: float = 0.0,
                 kelly: float = 0.25) -> ValueBet:
    """Avalia uma selecao: calcula EV, edge e stake sugerido.

    So sugere stake > 0 quando edge >= `min_edge` (filtro de ruido/erro do
    modelo). `kelly` e a fracao de Kelly (0.25 = 1/4 Kelly).
    """
    ev = expected_value(model_prob, odd)
    e = edge(model_prob, odd)
    stake = kelly_fraction(model_prob, odd, fraction=kelly) if e >= min_edge else 0.0
    return ValueBet(
        market=market, selection=selection, odd=float(odd),
        model_prob=float(model_prob),
        fair_prob=float(fair_prob) if fair_prob is not None else float("nan"),
        ev=float(ev), edge=float(e), stake_fraction=float(stake),
    )
