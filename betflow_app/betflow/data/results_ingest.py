"""Ingestao de resultados historicos para o cache em banco (tabela matches).

Estrategia (o que faltar numa API, pega na outra):
  1) SofaScore (via RapidAPI): fonte primaria de historico em volume
     (varias temporadas/rodadas), usada para TREINAR o Dixon-Coles.
  2) The Odds API (/scores): complemento com os resultados mais recentes
     (ultimos dias), util para manter o cache em dia entre rodadas.

Grava tudo em `matches` via store.upsert_matches (dedupe por liga+times+data),
sem depender do football-data.co.uk (fora do ar).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from config import settings
from betflow.web import store

log = logging.getLogger("betflow.ingest")


def _season_code_from_year(year: Any) -> str | None:
    """Normaliza o 'year' do SofaScore (ex.: '24/25', '2025') em codigo curto."""
    if not year:
        return None
    s = str(year).strip()
    if "/" in s:  # '24/25' -> '2425'
        a, b = s.split("/", 1)
        return f"{a[-2:]}{b[-2:]}"
    if len(s) == 4 and s.isdigit():  # '2025' -> temporada unica
        return s
    return s


def _from_sofascore(code: str, cfg: dict[str, Any], *,
                    seasons: int = 2, pages: int = 6) -> list[dict[str, Any]]:
    """Puxa resultados finalizados das ultimas N temporadas via SofaScore."""
    tid = cfg.get("sofascore")
    if not tid:
        return []
    from betflow.data.sofascore import SofaScoreClient, parse_events
    client = SofaScoreClient()
    rows: list[dict[str, Any]] = []
    try:
        seasons_list = client.tournament_seasons(tid)[:seasons]
    except Exception as exc:  # noqa: BLE001
        log.warning("sofascore seasons %s: %s", code, exc)
        return []
    for sea in seasons_list:
        sid = sea.get("id")
        scode = _season_code_from_year(sea.get("year"))
        if sid is None:
            continue
        for page in range(pages):
            try:
                events = client.tournament_last_matches(tid, sid, page)
            except Exception as exc:  # noqa: BLE001
                log.warning("sofascore last %s s=%s p=%s: %s", code, sid, page, exc)
                break
            if not events:
                break
            for ev in parse_events(events):
                hs, as_ = ev.get("home_score"), ev.get("away_score")
                if hs is None or as_ is None:
                    continue
                rows.append({
                    "league_code": code,
                    "event_id": str(ev.get("match_id") or ""),
                    "match_date": ev.get("kickoff_utc"),
                    "season": scode,
                    "home_team": ev.get("home_team"),
                    "away_team": ev.get("away_team"),
                    "home_goals": int(hs), "away_goals": int(as_),
                    "home_corners": None, "away_corners": None,
                    "home_yellow": None, "away_yellow": None,
                    "source": "sofascore",
                })
    return rows


def _from_odds_api(code: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Complemento: resultados recentes (ultimos dias) via The Odds API."""
    sport = cfg.get("odds_api_key")
    if not sport or not settings.ODDS_API_KEY:
        return []
    from betflow.data.odds_api import OddsApiClient
    client = OddsApiClient()
    try:
        events = client.get_scores(sport, days_from=3)
    except Exception as exc:  # noqa: BLE001
        log.warning("odds_api scores %s: %s", code, exc)
        return []
    scode = (settings.season_code(datetime.now(timezone.utc).year
             if datetime.now(timezone.utc).month >= 7
             else datetime.now(timezone.utc).year - 1))
    rows: list[dict[str, Any]] = []
    for ev in events:
        if not ev.get("completed"):
            continue
        sc = {s.get("name"): s.get("score") for s in (ev.get("scores") or [])}
        hs, as_ = sc.get(ev.get("home_team")), sc.get(ev.get("away_team"))
        if hs is None or as_ is None:
            continue
        try:
            hs, as_ = int(hs), int(as_)
        except (TypeError, ValueError):
            continue
        rows.append({
            "league_code": code,
            "event_id": str(ev.get("id") or ""),
            "match_date": ev.get("commence_time"),
            "season": scode,
            "home_team": ev.get("home_team"),
            "away_team": ev.get("away_team"),
            "home_goals": hs, "away_goals": as_,
            "home_corners": None, "away_corners": None,
            "home_yellow": None, "away_yellow": None,
            "source": "odds_api",
        })
    return rows


def ingest_league(code: str, *, deep: bool = False) -> int:
    """Ingesta resultados de uma liga no cache. Retorna nº de jogos gravados.

    `deep=True` puxa mais temporadas/paginas do SofaScore (backfill inicial).
    Sempre complementa com os resultados recentes da The Odds API.
    """
    cfg = settings.LEAGUES.get(code)
    if not cfg:
        return 0
    # Backfill profundo automatico enquanto a liga nao tem historico suficiente.
    try:
        if store.count_matches(code) < 60:
            deep = True
    except Exception:  # noqa: BLE001
        pass
    rows = _from_sofascore(code, cfg,
                           seasons=3 if deep else 2,
                           pages=16 if deep else 4)
    # Complemento com os placares recentes (o que a SofaScore nao trouxe).
    rows += _from_odds_api(code, cfg)
    if not rows:
        return 0
    return store.upsert_matches(rows)


def ingest_all(leagues: list[str] | None = None, *,
               deep: bool = False) -> dict[str, int]:
    """Ingesta todas as ligas-alvo. Retorna {liga: jogos_gravados}."""
    leagues = leagues or list(settings.TARGET_LEAGUES)
    out: dict[str, int] = {}
    for code in leagues:
        try:
            out[code] = ingest_league(code, deep=deep)
        except Exception as exc:  # noqa: BLE001
            log.warning("ingest %s falhou: %s", code, exc)
            out[code] = 0
    return out
