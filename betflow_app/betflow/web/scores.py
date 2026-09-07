"""Busca automatica de resultados e liquidacao das apostas.

Usa o endpoint /v4/sports/{sport}/scores da The Odds API (unica fonte online)
para descobrir os placares dos jogos ja apostados e liquidar as apostas 1X2
automaticamente (WON/LOST), atualizando o track record e a banca.

Custo de cota: 1-2 creditos por sport_key consultado (daysFrom<=3 custa 2).
Agrupamos por sport_key para minimizar chamadas.
"""
from __future__ import annotations

from typing import Any

from betflow.data import odds_api
from betflow.web import store


def _resolve_1x2(market: str, home_score: int, away_score: int) -> str:
    """Retorna 'WON'/'LOST' para um mercado 1X2:H|D|A dado o placar final."""
    if home_score > away_score:
        winner = "H"
    elif home_score < away_score:
        winner = "A"
    else:
        winner = "D"
    picked = market.split(":", 1)[1] if ":" in market else ""
    return "WON" if picked == winner else "LOST"


def _scores_index(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Mapa event_id -> {completed, home, away, hs, as} dos jogos finalizados."""
    out: dict[str, dict[str, Any]] = {}
    for ev in events:
        if not ev.get("completed"):
            continue
        scores = {s.get("name"): s.get("score") for s in (ev.get("scores") or [])}
        hs = scores.get(ev.get("home_team"))
        as_ = scores.get(ev.get("away_team"))
        if hs is None or as_ is None:
            continue
        try:
            out[ev["id"]] = {"hs": int(hs), "as": int(as_),
                             "home": ev.get("home_team"),
                             "away": ev.get("away_team")}
        except (TypeError, ValueError):
            continue
    return out


def settle_finished(user_id: int = store.DEFAULT_USER_ID,
                    days_from: int = 3) -> dict[str, Any]:
    """Liquida automaticamente as apostas abertas cujos jogos ja terminaram.

    So resolve mercados 1X2 (o unico com placar suficiente). Retorna um resumo
    com quantas foram liquidadas, ganhas/perdidas e a cota restante.
    """
    pending = store.list_pending_with_event(user_id=user_id)
    if not pending:
        return {"checked": 0, "settled": 0, "won": 0, "lost": 0,
                "skipped": 0, "quota_remaining": None}

    client = odds_api.OddsApiClient()
    # agrupa por sport_key para 1 chamada por liga
    by_sport: dict[str, list[dict]] = {}
    for b in pending:
        by_sport.setdefault(b.get("sport_key") or "", []).append(b)

    settled = won = lost = skipped = 0
    quota = None
    for sport_key, group in by_sport.items():
        if not sport_key:
            skipped += len(group)
            continue
        try:
            events = client.get_scores(sport_key, days_from=days_from)
        except odds_api.OddsApiError:
            skipped += len(group)
            continue
        quota = client.last_quota.remaining
        finished = _scores_index(events)

        for b in group:
            info = finished.get(b.get("event_id"))
            if not info:
                skipped += 1
                continue
            if not b["market"].startswith("1X2:"):
                skipped += 1  # sem placar suficiente p/ outros mercados
                continue
            result = _resolve_1x2(b["market"], info["hs"], info["as"])
            store.settle_bet(b["id"], result, auto=True)
            settled += 1
            won += result == "WON"
            lost += result == "LOST"

    return {"checked": len(pending), "settled": settled, "won": won,
            "lost": lost, "skipped": skipped, "quota_remaining": quota}
