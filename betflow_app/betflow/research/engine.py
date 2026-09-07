"""Motor +EV da Boosted Research.

Para cada liga: treina (ou reusa do cache) o ensemble, busca as odds de
referencia da Pinnacle (a casa mais eficiente do mercado — bater a linha dela
e o teste de ouro do modelo), remove a margem da casa via metodo de Shin e
emite um sinal apenas quando TODOS os filtros passam:
  1) edge absoluto >= MIN_EDGE (default 4pp);
  2) prob. do modelo >= prob. justa * (1 + REL_EDGE) — discordancia RELATIVA
     minima (default 15%), que neutraliza o favourite-longshot bias: com edge
     so absoluto, 4pp de ruido em cotacao alta ja disparava sinal em azarao;
  3) cotacao <= MAX_ODD (default 4.0) — acima disso o erro de calibracao do
     modelo domina qualquer vantagem aparente.

Devolve dicionarios no mesmo formato consumido por store.save_suggestions,
marcados com strategy='boosted' para comparacao direta com o motor principal.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from betflow.data import odds_api
from betflow.betting import value
from betflow.betting import staking
from betflow.research.model import BoostedModel
from betflow.web import store
from config import settings

log = logging.getLogger("betflow.research")

# Casa de referencia e regiao da vertente. A politica de selecao/staking (edge,
# teto de cotacao, ancoragem, confianca) vem de staking.boosted_policy() — os
# MESMOS mecanismos do motor principal, mas com parametros PROPRIos (as duas
# metodologias permanecem separadas). Ver PLANO_MELHORIA_MODELOS.md.
BOOKMAKER = os.getenv("BOOSTED_BOOKMAKER", "Pinnacle")
REGIONS = os.getenv("BOOSTED_REGIONS", "eu")
MIN_TRAIN = 60

# cache de modelos por liga (treino e caro)
_MODEL_CACHE: dict[str, BoostedModel] = {}


def clear_cache() -> None:
    _MODEL_CACHE.clear()


def train(league_code: str) -> BoostedModel | None:
    """Treina (ou reusa) o ensemble de uma liga a partir do cache de resultados."""
    if league_code in _MODEL_CACHE:
        return _MODEL_CACHE[league_code]
    try:
        df = store.load_matches_df(league_code, seasons=6)
    except Exception:  # noqa: BLE001
        df = pd.DataFrame()
    if df.empty or len(df) < MIN_TRAIN:
        return None
    model = BoostedModel().fit(df)
    _MODEL_CACHE[league_code] = model
    return model


def _in_window(iso: str, days: int) -> bool:
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    now = datetime.now(timezone.utc)
    return now - timedelta(hours=3) <= dt <= now + timedelta(days=days)


def _fmt_kickoff(iso: str) -> str:
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        return (dt - timedelta(hours=3)).strftime("%d/%m %H:%M")  # Brasilia
    except (ValueError, AttributeError):
        return iso or "?"


@dataclass
class ResearchResult:
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    leagues: dict[str, Any] = field(default_factory=dict)
    quota_remaining: int | None = None
    errors: list[str] = field(default_factory=list)


def run(leagues: list[str] | None = None, days: int = 7,
        min_edge: float | None = None, kelly: float | None = None,
        regions: str | None = None) -> ResearchResult:
    """Gera sinais +EV da Boosted Research. 1 credito por liga (regiao unica)."""
    leagues = leagues or list(settings.TARGET_LEAGUES)
    regions = regions or REGIONS
    # Piso operacional (R$ minimo por entrada) em fracao da banca de referencia:
    # sinais menores que isso sao descartados dentro do evaluate.
    _bank = store.get_suggestions_initial_bankroll()
    _floor = (settings.STAKE_FLOOR_ABS / _bank) if _bank and _bank > 0 else 0.0
    pol = staking.boosted_policy(stake_floor_frac=_floor)

    res = ResearchResult()
    client = odds_api.OddsApiClient()

    for code in leagues:
        cfg = settings.LEAGUES.get(code)
        if not cfg:
            continue
        model = train(code)
        if model is None:
            res.errors.append(f"{code}: modelo indisponivel (dados insuficientes)")
            res.leagues[code] = {"events": 0, "value": 0}
            continue
        try:
            events = client.get_odds(cfg["odds_api_key"], markets="h2h",
                                     regions=regions)
        except odds_api.OddsApiError as exc:
            res.errors.append(f"{code}: odds indisponiveis ({exc})")
            res.leagues[code] = {"events": 0, "value": 0, "error": str(exc)}
            continue
        res.quota_remaining = client.last_quota.remaining

        n_win = n_val = 0
        for ev in events:
            if not _in_window(ev.get("commence_time", ""), days):
                continue
            n_win += 1
            odds = odds_api.extract_h2h_by_bookmaker(ev, BOOKMAKER)
            if not odds:
                continue
            probs = model.predict_1x2(ev.get("home_team", ""),
                                      ev.get("away_team", ""))
            if not probs:
                continue
            # probabilidades justas via Shin: remove a margem da casa e corrige
            # o vies favorito-azarao antes de medir a vantagem do modelo
            trio = [odds["home"], odds["draw"], odds["away"]]
            fair = value.fair_probs(trio, method="shin")
            gh_n, ga_n = model.fixture_games(ev.get("home_team", ""),
                                             ev.get("away_team", ""))
            legs = [
                ("1X2:H", f"{ev['home_team']} vencer", "home", probs["H"], odds["home"], float(fair[0])),
                ("1X2:D", "Empate", "draw", probs["D"], odds["draw"], float(fair[1])),
                ("1X2:A", f"{ev['away_team']} vencer", "away", probs["A"], odds["away"], float(fair[2])),
            ]
            for market, sel, side, prob, odd, fp in legs:
                dec = staking.evaluate(
                    pol, market, sel, p_model=prob, p_fair=fp, odd=odd,
                    probs=probs, games_home=gh_n, games_away=ga_n)
                if dec.passed:
                    n_val += 1
                    res.suggestions.append({
                        "league": cfg["name"], "league_code": code,
                        "sport_key": cfg["odds_api_key"],
                        "event_id": ev.get("id"),
                        "commence_time": ev.get("commence_time"),
                        "commence_time_fmt": _fmt_kickoff(ev.get("commence_time", "")),
                        "home": ev.get("home_team"), "away": ev.get("away_team"),
                        "market": market, "selection": sel, "side": side,
                        "bookmaker": BOOKMAKER,
                        "odd": round(odd, 2), "prob": round(dec.p_used, 4),
                        "implied": round(1.0 / odd, 4),
                        "fair_prob": round(dec.p_fair, 4),
                        "confidence": round(dec.confidence, 4),
                        "ev": round(dec.ev, 4), "edge": round(dec.edge, 4),
                        "stake_frac": round(dec.stake_fraction, 4),
                        "source": "boosted", "stats": None,
                    })
        res.leagues[code] = {"events": n_win, "value": n_val}

    res.suggestions.sort(key=lambda b: b["ev"], reverse=True)
    return res
