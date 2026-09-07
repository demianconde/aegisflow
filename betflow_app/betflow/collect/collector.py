"""Rotina de coleta automatica de odds (snapshots) da SofaScore.

Para cada liga configurada, busca os proximos jogos, puxa as odds (1X2,
escanteios, cartoes) e grava um snapshot no banco `odds_history.db`. Rodando
periodicamente (via Tarefa Agendada do Windows), acumula a evolucao das odds ao
longo do tempo -> permite calcular CLV real, inclusive em escanteios/cartoes.

Onde o modelo reconhece os dois times, tambem grava a probabilidade estimada e
o edge, para depois cruzar "onde tinhamos edge" x "o mercado se moveu a favor".
"""
from __future__ import annotations

from typing import Any

from betflow.data import sofascore as ss
from betflow.data import team_names
from betflow.collect import store
from config import settings


def _model_probs_for(engine, home_model: str | None, away_model: str | None
                     ) -> dict[str, float]:
    """Probabilidades do modelo para as selecoes coletadas (se times conhecidos)."""
    if not (home_model and away_model):
        return {}
    try:
        p1x2 = engine.dc.predict_1x2(home_model, away_model)
    except Exception:
        return {}
    probs = {"1X2:home": p1x2["H"], "1X2:draw": p1x2["D"], "1X2:away": p1x2["A"]}
    # escanteios/cartoes so quando a liga tem esses modelos treinados
    if engine.has_stats and engine.corners and engine.cards:
        for line in (8.5, 9.5, 10.5, 11.5):
            probs[f"corners:over{line}"] = engine.corners.prob_over(
                line, home_model, away_model)
        for line in (3.5, 4.5, 5.5):
            probs[f"cards:over{line}"] = engine.cards.prob_over(line)
    return probs


def _snapshots_for_match(engine, league: str, fx: dict[str, Any],
                         captured_at: str) -> list[dict[str, Any]]:
    """Monta as linhas de snapshot de UM jogo (consome 1 credito SofaScore)."""
    client = engine._sofa_client()
    markets = client.all_odds(fx["match_id"])
    odds_1x2 = ss.parse_odds(markets)          # {"1X2": {home,draw,away}, ...}
    odds_ou = ss.parse_ou_markets(markets)     # {"corners":..,"cards":..}

    model_probs = _model_probs_for(engine, fx.get("home_model"),
                                   fx.get("away_model"))

    rows: list[dict[str, Any]] = []

    def add(market: str, selection: str, odd: float, prob_key: str) -> None:
        mp = model_probs.get(prob_key)
        edge = (mp - 1.0 / odd) if (mp is not None and odd > 1.0) else None
        rows.append({
            "captured_at": captured_at, "league": league,
            "match_id": fx["match_id"], "home_team": fx["home_team"],
            "away_team": fx["away_team"], "kickoff_utc": fx.get("kickoff_utc"),
            "market": market, "selection": selection, "odd": float(odd),
            "model_prob": mp, "edge": edge,
        })

    # 1X2
    for sel, key in (("home", "H"), ("draw", "D"), ("away", "A")):
        val = odds_1x2.get("1X2", {}).get(sel)
        if val:
            add("1X2", sel, val, f"1X2:{sel}")

    # escanteios e cartoes (over/under com linha)
    for canon in ("corners", "cards"):
        for sel, odd in odds_ou.get(canon, {}).items():   # sel ex.: "over9.5"
            add(canon, sel, odd, f"{canon}:{sel}")

    return rows


def collect_once(leagues: list[str] | None = None, max_fixtures: int = 10,
                 db_path=store.DB_PATH) -> dict[str, Any]:
    """Executa UMA coleta para as ligas dadas. Retorna um resumo.

    Importado tardiamente o Engine para evitar dependencia circular no import.
    """
    from betflow.web.engine import Engine

    store.init_db(db_path)
    leagues = leagues or [c for c in settings.LEAGUES]
    captured_at = store.now()
    total_snaps = 0
    total_fx = 0
    quota_left = None
    notes: list[str] = []

    for lg in leagues:
        try:
            eng = Engine().reload(league=lg)
            if not eng.sofascore_enabled:
                notes.append(f"{lg}: SofaScore desativada")
                continue
            fixtures = eng.upcoming_fixtures(limit=max_fixtures)
            rows: list[dict[str, Any]] = []
            for fx in fixtures:
                if not fx.get("match_id"):
                    continue
                try:
                    rows.extend(_snapshots_for_match(eng, lg, fx, captured_at))
                except Exception as exc:      # 1 jogo falho nao derruba a coleta
                    notes.append(f"{lg}/{fx.get('match_id')}: {exc}")
            n = store.record_snapshots(rows, db_path)
            quota_left = eng.quota_remaining
            store.log_run(lg, len(fixtures), n, quota_left,
                          note="; ".join(notes[-3:]), db_path=db_path)
            total_snaps += n
            total_fx += len(fixtures)
        except Exception as exc:
            notes.append(f"{lg}: {exc}")
            store.log_run(lg, 0, 0, quota_left, note=str(exc), db_path=db_path)

    return {
        "captured_at": captured_at,
        "leagues": leagues,
        "fixtures": total_fx,
        "snapshots": total_snaps,
        "quota_left": quota_left,
        "notes": notes,
    }
