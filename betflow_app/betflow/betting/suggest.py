"""Motor de sugestoes de apostas (reutilizavel: web + scripts).

Para cada liga-alvo treina Dixon-Coles (+ escanteios/cartoes quando ha dados),
busca as odds da Betano na The Odds API (1 credito por liga) e devolve as
apostas de VALOR de 1X2 (EV>0, edge>=MIN_EDGE) com o stake sugerido por Kelly
fracionario, alem das previsoes de escanteios/cartoes.

NAO depende do SofaScore. A unica fonte online e a The Odds API.
Os modelos ficam em cache em memoria (treino e caro) via `_MODEL_CACHE`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from betflow.data import football_data as fd
from betflow.data import extra_leagues as el
from betflow.data import team_names
from betflow.data import odds_api
from betflow.models.dixon_coles import DixonColesModel
from betflow.models.counts import NegativeBinomialTotals, PoissonCards
from betflow.betting import value
from betflow.betting import staking
from betflow.betting import calibration as calib
from betflow.web import store
from config import settings

CORNER_LINES = (9.5, 10.5, 11.5)
CARD_LINES = (3.5, 4.5, 5.5)


def _games_count(df) -> dict[str, int]:
    """Numero de jogos de cada time no treino (para a confianca por dados)."""
    if df is None or df.empty:
        return {}
    h = df["home_team"].value_counts()
    a = df["away_team"].value_counts()
    return (h.add(a, fill_value=0)).astype(int).to_dict()


@dataclass
class LeagueModels:
    dc: DixonColesModel | None = None
    corners: NegativeBinomialTotals | None = None
    cards: PoissonCards | None = None
    games: dict[str, int] = field(default_factory=dict)

    @property
    def has_stats(self) -> bool:
        return self.corners is not None and self.cards is not None


# cache de modelos por liga (evita re-treinar a cada request)
_MODEL_CACHE: dict[str, LeagueModels] = {}

# cache de calibradores por liga (treinados 1x com apostas liquidadas)
_CALIB_CACHE: dict[str, dict[str, Any]] = {}


def _calibrators_for(league_code: str, method: str,
                       min_samples: int) -> dict[str, Any]:
    """Retorna (e cacheia) calibradores one-vs-rest para uma liga."""
    key = f"{league_code}:{method}:{min_samples}"
    if key not in _CALIB_CACHE:
        samples = store.settled_1x2_calibration_samples(division=league_code)
        _CALIB_CACHE[key] = calib.fit_1x2_calibrators(
            samples, method=method, min_samples=min_samples)
    return _CALIB_CACHE[key]


# ---------------------------------------------------------------------------
def _current_seasons() -> tuple[str, str]:
    now = datetime.now()
    start = now.year if now.month >= 7 else now.year - 1
    return settings.season_code(start), settings.season_code(start - 1)


def train_models(league_code: str) -> LeagueModels:
    """Treina (ou reusa do cache) os modelos de uma liga."""
    if league_code in _MODEL_CACHE:
        return _MODEL_CACHE[league_code]

    cfg = settings.LEAGUES[league_code]
    src = cfg["source"]

    # 1) Fonte primaria: cache de resultados no banco (SofaScore + Odds API),
    #    alimentado pela ingestao automatica. Independe do football-data.co.uk.
    MIN_TRAIN = 60
    try:
        df_db = store.load_matches_df(league_code)
    except Exception:  # noqa: BLE001
        df_db = pd.DataFrame()
    if len(df_db) >= MIN_TRAIN:
        models = LeagueModels(
            dc=DixonColesModel(xi=settings.DC_XI, ridge=settings.DC_RIDGE).fit(df_db),
            games=_games_count(df_db))
        if {"home_corners", "away_corners"} <= set(df_db.columns) \
                and df_db[["home_corners", "away_corners"]].notna().any().all():
            models.corners = NegativeBinomialTotals().fit(df_db)
        if {"home_yellow", "away_yellow"} <= set(df_db.columns) \
                and df_db[["home_yellow", "away_yellow"]].notna().any().all():
            models.cards = PoissonCards().fit(df_db)
        _MODEL_CACHE[league_code] = models
        return models

    # 2) Fallbacks legados (CSV local / football-data / extra_leagues).
    if src == "fallback":
        models = LeagueModels()
        _MODEL_CACHE[league_code] = models
        return models

    if src == "extra":
        df = el.load(cfg["extra_code"], league=cfg.get("extra_league"))
        if "season" in df.columns and not df.empty:
            seasons = sorted(df["season"].astype(str).unique())
            df = df[df["season"].astype(str).isin(set(seasons[-3:]))]
    else:
        div = cfg["division"]
        frames = []
        for sea in _current_seasons():
            try:
                frames.append(fd.load_season(div, sea))
            except Exception:  # noqa: BLE001
                pass
        if not frames:  # fallback a qualquer CSV em cache local
            for cached in sorted(settings.RAW_DIR.glob(f"{div}_*.csv"), reverse=True):
                try:
                    d = fd.parse(cached)
                    d["season"] = cached.stem.split("_")[-1]
                    frames.append(d)
                    break
                except Exception:  # noqa: BLE001
                    pass
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    if df.empty:
        raise RuntimeError(f"sem dados de treino para {league_code}")

    models = LeagueModels(
        dc=DixonColesModel(xi=settings.DC_XI, ridge=settings.DC_RIDGE).fit(df),
        games=_games_count(df))
    if {"home_corners", "away_corners"} <= set(df.columns) \
            and df[["home_corners", "away_corners"]].notna().any().all():
        models.corners = NegativeBinomialTotals().fit(df)
    if {"home_yellow", "away_yellow"} <= set(df.columns) \
            and df[["home_yellow", "away_yellow"]].notna().any().all():
        models.cards = PoissonCards().fit(df)
    _MODEL_CACHE[league_code] = models
    return models


def clear_cache() -> None:
    _MODEL_CACHE.clear()
    _CALIB_CACHE.clear()



def _in_window(iso: str, days: int, today_only: bool) -> bool:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=3)
    end = (now.replace(hour=23, minute=59, second=59) if today_only
           else now + timedelta(days=days))
    return start <= dt <= end


def _fmt_kickoff(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (dt - timedelta(hours=3)).strftime("%d/%m %H:%M")  # Brasilia
    except (ValueError, AttributeError):
        return iso or "?"


def _reference_probs(models: LeagueModels, ev: dict, bo: dict[str, float]
                     ) -> tuple[dict[str, float], str, tuple[str | None, str | None]]:
    if models.dc is not None:
        hm = team_names.match_team(ev.get("home_team", ""), models.dc.teams)
        am = team_names.match_team(ev.get("away_team", ""), models.dc.teams)
        if hm and am:
            p = models.dc.predict_1x2(hm, am)
            return {"H": p["H"], "D": p["D"], "A": p["A"]}, "modelo", (hm, am)
    fair = value.fair_probs([bo["home"], bo["draw"], bo["away"]], method="shin")
    return ({"H": float(fair[0]), "D": float(fair[1]), "A": float(fair[2])},
            "mercado", (None, None))


def _stats_forecast(models: LeagueModels, hm: str | None, am: str | None
                    ) -> dict | None:
    if not models.has_stats or not (hm and am):
        return None
    return {
        "corners": {
            "expected": round(models.corners.expected_total(hm, am), 1),
            "over": {str(l): round(models.corners.prob_over(l, hm, am), 3)
                     for l in CORNER_LINES},
        },
        "cards": {
            "expected": round(models.cards.expected_total(None), 1),
            "over": {str(l): round(models.cards.prob_over(l, None), 3)
                     for l in CARD_LINES},
        },
    }


@dataclass
class SuggestResult:
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    leagues: dict[str, Any] = field(default_factory=dict)
    quota_remaining: int | None = None
    errors: list[str] = field(default_factory=list)


def suggest(leagues: list[str] | None = None, days: int = 7,
            today_only: bool = False, min_edge: float | None = None,
            kelly: float | None = None, bookmaker: str | None = None,
            regions: str = "uk", calibration: str | None = None,
            calibration_min_samples: int = 20) -> SuggestResult:
    """Gera sugestoes de valor para as ligas. 1 credito por liga."""
    leagues = leagues or list(settings.TARGET_LEAGUES)
    min_edge = settings.MIN_EDGE if min_edge is None else min_edge
    kelly = settings.KELLY_FRACTION if kelly is None else kelly
    bookmaker = bookmaker or settings.PREFERRED_BOOKMAKER

    res = SuggestResult()
    client = odds_api.OddsApiClient()
    # Piso operacional (R$ minimo por entrada) convertido em fracao da banca de
    # referencia: entradas menores que isso sao descartadas dentro do evaluate.
    _bank = store.get_suggestions_initial_bankroll()
    _floor = (settings.STAKE_FLOOR_ABS / _bank) if _bank and _bank > 0 else 0.0
    pol = staking.main_policy(stake_floor_frac=_floor)

    for code in leagues:
        cfg = settings.LEAGUES.get(code)
        if not cfg:
            continue
        try:
            models = train_models(code)
        except Exception as exc:  # noqa: BLE001
            models = LeagueModels()
            res.errors.append(f"{code}: modelo indisponivel ({exc})")

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
            if not _in_window(ev.get("commence_time", ""), days, today_only):
                continue
            n_win += 1
            # So recomendamos jogos que a Betano (casa que opera no Brasil)
            # realmente cotou; sem Betano, nao ha onde aportar aqui.
            odds = odds_api.extract_h2h_by_bookmaker(ev, bookmaker)
            if not odds:
                continue
            probs, source, (hm, am) = _reference_probs(models, ev, odds)
            # Sem modelo (times desconhecidos) nao ha discordancia com o mercado:
            # p_modelo == p_justa => nada passa. Poupamos o processamento.
            if source != "modelo":
                continue
            if calibration:
                calibrators = _calibrators_for(code, calibration,
                                                 calibration_min_samples)
                if calibrators:
                    probs = calib.calibrate_1x2(probs, calibrators)
            stats = _stats_forecast(models, hm, am)
            # Probabilidade JUSTA do mercado (Shin) — devig antes de comparar.
            fair = value.fair_probs([odds["home"], odds["draw"], odds["away"]],
                                    method="shin")
            gh_n = models.games.get(hm or "", 0)
            ga_n = models.games.get(am or "", 0)
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
                        "bookmaker": bookmaker,
                        "odd": round(odd, 2), "prob": round(dec.p_used, 4),
                        "implied": round(1.0 / odd, 4),
                        "fair_prob": round(dec.p_fair, 4),
                        "confidence": round(dec.confidence, 4),
                        "ev": round(dec.ev, 4), "edge": round(dec.edge, 4),
                        "stake_frac": round(dec.stake_fraction, 4),
                        "source": source, "stats": stats,
                    })
        res.leagues[code] = {"events": n_win, "value": n_val}

    res.suggestions.sort(key=lambda b: b["ev"], reverse=True)
    return res
