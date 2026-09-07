"""Politica de selecao e dimensionamento (staking) — nucleo transversal.

Este modulo concentra as correcoes das Fases 1 e 2 do plano de melhoria e e
usado pelos DOIS motores (principal e Boosted) — cada um com seus PROPRIOS
parametros (uma :class:`StakingPolicy`). As metodologias continuam separadas: o
que se compartilha aqui e o *mecanismo*, nunca os numeros.

Mecanismos (ver PLANO_MELHORIA_MODELOS.md):

1. **Devig antes de comparar.** A vantagem e sempre medida contra a
   probabilidade JUSTA do mercado (Shin), nunca contra ``1/cotacao`` cru — que
   embute o *favourite-longshot bias* (margem concentrada nas cotacoes altas).

2. **Limiar de discordancia relativo.** Alem do edge absoluto, exige-se
   ``p >= p_justa * (1 + rel_edge)``. Um corte absoluto de 3pp e facil demais de
   disparar em cotacao alta (onde 3pp e uma discordancia relativa enorme) —
   e e exatamente isso que "forcava as zebras".

3. **Teto de cotacao.** Acima do teto, o erro de calibracao do modelo domina
   qualquer vantagem aparente.

4. **Ancoragem bayesiana ao mercado (encolhimento).**
   ``p_usada = (1 - lam) * p_modelo + lam * p_justa``. A linha de fechamento e
   um estimador afiado; trata-la como *prior* forte puxa as estimativas
   exageradas da cauda de volta ao consenso. E regularizacao pura.

5. **Porta de sanidade.** Se o modelo discorda do mercado ALEM de uma banda
   (``max_rel_disagree``), isso e quase sempre erro de modelo (ex.: rating
   extremo de time com poucos jogos), nao valor real — a entrada e REJEITADA.
   E o que elimina cartoes absurdos (ex.: "azarao a 46%" contra cotacao 11.5).

6. **Kelly escalado por confianca.** A fracao de Kelly e multiplicada por um
   fator ``c in (0, 1]`` que encolhe quando ha poucos jogos historicos, quando a
   distribuicao 1X2 e muito indefinida (entropia alta) ou quando a discordancia
   com o mercado e grande. Ataca o "pesar a mao em jogos duvidosos".

7. **Teto de stake por entrada.** A fracao final nunca passa de ``stake_cap``
   (ex.: 2% da banca), independentemente do que Kelly sugerir.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from betflow.betting import value
from config import settings


@dataclass(frozen=True)
class StakingPolicy:
    """Parametros de selecao/dimensionamento de UM motor (nunca compartilhados)."""
    name: str                     # "main" | "boosted" (rotulo)
    min_edge: float               # edge absoluto minimo (p_usada - p_justa)
    rel_edge: float               # discordancia relativa minima frente a p_justa
    max_odd: float                # teto de cotacao
    lam: float                    # peso da ancoragem ao mercado (0..1)
    max_rel_disagree: float       # porta de sanidade (discordancia relativa maxima)
    kelly: float = settings.KELLY_FRACTION
    stake_cap: float = settings.STAKE_CAP
    conf_min_games: float = settings.CONF_MIN_GAMES
    conf_full_games: float = settings.CONF_FULL_GAMES
    conf_floor: float = settings.CONF_FLOOR
    # Piso operacional: fracao MINIMA da banca abaixo da qual a entrada nao vale
    # o trabalho e e DESCARTADA (nao arredondada — arredondar poria mais dinheiro
    # nas entradas de menor conviccao, ferindo a disciplina de risco). Vem da
    # banca de referencia: floor_frac = STAKE_FLOOR_ABS / banca. 0.0 = desligado.
    stake_floor_frac: float = 0.0


def main_policy(stake_floor_frac: float = 0.0) -> StakingPolicy:
    """Politica do motor principal (Dixon-Coles).

    `stake_floor_frac` e o piso operacional (fracao da banca) abaixo do qual a
    entrada e descartada; calcule-o a partir da banca de referencia.
    """
    return StakingPolicy(
        name="main",
        min_edge=settings.MAIN_MIN_EDGE,
        rel_edge=settings.MAIN_REL_EDGE,
        max_odd=settings.MAIN_MAX_ODD,
        lam=settings.MAIN_LAMBDA,
        max_rel_disagree=settings.MAIN_MAX_REL_DISAGREE,
        stake_floor_frac=stake_floor_frac,
    )


def boosted_policy(stake_floor_frac: float = 0.0) -> StakingPolicy:
    """Politica da Boosted Research (Elo + XGBoost).

    `stake_floor_frac`: idem `main_policy` (piso operacional em fracao da banca).
    """
    return StakingPolicy(
        name="boosted",
        min_edge=settings.BOOSTED_MIN_EDGE,
        rel_edge=settings.BOOSTED_REL_EDGE,
        max_odd=settings.BOOSTED_MAX_ODD,
        lam=settings.BOOSTED_LAMBDA,
        max_rel_disagree=settings.BOOSTED_MAX_REL_DISAGREE,
        kelly=settings.BOOSTED_KELLY,
        stake_cap=settings.BOOSTED_STAKE_CAP,
        stake_floor_frac=stake_floor_frac,
    )


# ---------------------------------------------------------------------------
# mecanismos
# ---------------------------------------------------------------------------
def shrink_to_market(p_model: float, p_fair: float, lam: float) -> float:
    """Ancoragem bayesiana: mistura convexa modelo x mercado justo."""
    lam = min(max(lam, 0.0), 1.0)
    return (1.0 - lam) * p_model + lam * p_fair


def _entropy_norm(probs: dict[str, float]) -> float:
    """Entropia de {H,D,A} normalizada por log(3) -> [0, 1] (1 = indefinido)."""
    vals = [max(1e-9, float(v)) for v in probs.values()]
    s = sum(vals)
    vals = [v / s for v in vals]
    h = -sum(v * math.log(v) for v in vals)
    return h / math.log(len(vals)) if len(vals) > 1 else 0.0


def confidence_factor(policy: StakingPolicy, probs: dict[str, float],
                      games_home: int, games_away: int,
                      rel_disagree: float) -> float:
    """Fator de confianca ``c in [conf_floor, 1]`` que escala o Kelly.

    Encolhe com: poucos jogos historicos, entropia alta (jogo indefinido) e
    discordancia grande frente ao mercado (dentro da banda de sanidade).
    """
    # (a) dados: min de jogos dos dois times, saturando em conf_full_games
    n_min = float(min(games_home, games_away))
    lo, hi = policy.conf_min_games, max(policy.conf_full_games, policy.conf_min_games + 1)
    c_data = (n_min - lo) / (hi - lo)
    c_data = min(max(c_data, 0.0), 1.0)

    # (b) entropia: penaliza so o excesso de indefinicao (Hn acima de 0.85)
    hn = _entropy_norm(probs)
    c_entropy = 1.0 - 0.4 * max(0.0, (hn - 0.85) / 0.15)

    # (c) discordancia: cai linearmente da banda "confortavel" ate a de sanidade
    soft = max(policy.rel_edge, 0.05)
    span = max(policy.max_rel_disagree - soft, 1e-6)
    c_dis = 1.0 - 0.6 * min(max((rel_disagree - soft) / span, 0.0), 1.0)

    c = c_data * c_entropy * c_dis
    return float(min(max(c, policy.conf_floor), 1.0))


@dataclass
class StakeDecision:
    """Resultado de avaliar uma selecao sob uma politica de staking."""
    passed: bool
    reason: str
    market: str
    selection: str
    odd: float
    p_model: float          # probabilidade do modelo (pos-calibracao)
    p_fair: float           # probabilidade justa do mercado (Shin)
    p_used: float           # probabilidade ancorada, usada no dimensionamento
    confidence: float       # fator c aplicado ao Kelly
    ev: float               # valor esperado com p_used
    edge: float             # p_used - p_fair
    stake_fraction: float   # fracao final da banca (Kelly x confianca, com teto)


def evaluate(policy: StakingPolicy, market: str, selection: str,
             p_model: float, p_fair: float, odd: float,
             probs: dict[str, float] | None = None,
             games_home: int = 999, games_away: int = 999) -> StakeDecision:
    """Avalia uma selecao aplicando todos os mecanismos da politica.

    `probs` e a distribuicao 1X2 do modelo (para a entropia); se ausente, a
    porta de entropia fica neutra. `games_*` alimentam a confianca por dados.
    """
    p_used = shrink_to_market(p_model, p_fair, policy.lam)
    rel_disagree = (p_model / p_fair - 1.0) if p_fair > 0 else 0.0
    ev = value.expected_value(p_used, odd)
    edge = p_used - p_fair

    def _reject(reason: str) -> StakeDecision:
        return StakeDecision(False, reason, market, selection, float(odd),
                             float(p_model), float(p_fair), float(p_used),
                             0.0, float(ev), float(edge), 0.0)

    # --- portas de selecao (todas medidas contra a prob. JUSTA) ---
    if odd > policy.max_odd:
        return _reject(f"cotacao acima do teto ({policy.max_odd})")
    if rel_disagree > policy.max_rel_disagree:
        return _reject("discordancia alem da banda de sanidade")
    if p_used < p_fair * (1.0 + policy.rel_edge):
        return _reject("discordancia relativa insuficiente")
    if edge < policy.min_edge:
        return _reject("edge absoluto insuficiente")

    # --- confianca e dimensionamento ---
    conf = confidence_factor(policy, probs or {market: p_model},
                             games_home, games_away, rel_disagree)
    kelly = value.kelly_fraction(p_used, odd, fraction=policy.kelly)
    stake = min(kelly * conf, policy.stake_cap)
    if stake <= 0.0:
        return _reject("Kelly nao-positivo")
    # Piso operacional: entradas pequenas demais nao valem o trabalho -> descarta
    # (nunca arredonda para cima; ver StakingPolicy.stake_floor_frac).
    if policy.stake_floor_frac > 0.0 and stake < policy.stake_floor_frac:
        return _reject("abaixo do piso operacional")

    return StakeDecision(True, "ok", market, selection, float(odd),
                         float(p_model), float(p_fair), float(p_used),
                         float(conf), float(ev), float(edge), float(stake))
