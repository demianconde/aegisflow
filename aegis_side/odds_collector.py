"""Coletor de odds da SofaScore (RapidAPI) - leve, so httpx + sqlite (stdlib).

Isolado do gateway AegisFlow. Captura snapshots das odds dos proximos jogos das
ligas configuradas e grava em SQLite. Rodando periodicamente (loop com sleep),
acumula a evolucao das odds -> permite calcular CLV depois, offline.

Env vars (lidas so por este coletor; nao colidem com as do AegisFlow):
    BETFLOW_RAPIDAPI_KEY        chave RapidAPI (obrigatoria p/ coletar)
    BETFLOW_SOFASCORE_HOST      host (default sofascore.p.rapidapi.com)
    BETFLOW_ODDS_DB             caminho do SQLite (default /data/odds_history.db)
    BETFLOW_LEAGUES            ligas separadas por virgula (default E0,BSA)
    BETFLOW_MAX_FIXTURES       max de jogos por liga por rodada (default 10)
    BETFLOW_COLLECT_INTERVAL   segundos entre coletas no modo loop (default 7200)
"""
from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from typing import Any

import httpx

# uniqueTournament ids estaveis da SofaScore por codigo de liga
TOURNAMENTS: dict[str, int] = {
    "E0": 17,     # Premier League
    "SP1": 8,     # La Liga
    "I1": 23,     # Serie A (Italia)
    "D1": 35,     # Bundesliga
    "F1": 34,     # Ligue 1
    "BSA": 325,   # Brasileirao Serie A
}


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key, default) or "").strip()


def db_path() -> str:
    return _env("BETFLOW_ODDS_DB", "/data/odds_history.db")


def leagues() -> list[str]:
    raw = _env("BETFLOW_LEAGUES", "E0,BSA")
    return [x.strip() for x in raw.split(",") if x.strip()]


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# SQLite (proprio, isolado do Postgres do gateway)
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at  TEXT NOT NULL,
    league       TEXT NOT NULL,
    match_id     INTEGER NOT NULL,
    home_team    TEXT NOT NULL,
    away_team    TEXT NOT NULL,
    kickoff_utc  TEXT,
    market       TEXT NOT NULL,
    selection    TEXT NOT NULL,
    odd          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snap_match
    ON odds_snapshots(match_id, market, selection, captured_at);
CREATE TABLE IF NOT EXISTS collect_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at TEXT NOT NULL, league TEXT, fixtures INTEGER,
    snapshots INTEGER, quota_left INTEGER, note TEXT
);
"""


def _conn(path: str | None = None) -> sqlite3.Connection:
    c = sqlite3.connect(path or db_path())
    c.row_factory = sqlite3.Row
    return c


def init_db(path: str | None = None) -> None:
    with _conn(path) as c:
        c.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# SofaScore (httpx puro)
# ---------------------------------------------------------------------------
def _headers() -> dict[str, str]:
    return {
        "x-rapidapi-host": _env("BETFLOW_SOFASCORE_HOST", "sofascore.p.rapidapi.com"),
        "x-rapidapi-key": _env("BETFLOW_RAPIDAPI_KEY"),
    }


def _get(client: httpx.Client, path: str, params: dict | None = None):
    host = _env("BETFLOW_SOFASCORE_HOST", "sofascore.p.rapidapi.com")
    r = client.get(f"https://{host}{path}", headers=_headers(),
                   params=params, timeout=30)
    quota = r.headers.get("x-ratelimit-requests-remaining")
    if r.status_code == 204:
        return None, quota
    r.raise_for_status()
    return r.json(), quota


def _frac_to_dec(frac: str | None) -> float | None:
    if not frac:
        return None
    try:
        if "/" in str(frac):
            n, d = str(frac).split("/")
            return round(float(n) / float(d) + 1.0, 4)
        return round(float(frac) + 1.0, 4)
    except (ValueError, ZeroDivisionError):
        return None


# ---------------------------------------------------------------------------
# parsing dos mercados (1X2, escanteios, cartoes)
# ---------------------------------------------------------------------------
def _parse_markets(markets: list[dict[str, Any]]) -> list[tuple[str, str, float]]:
    """Retorna [(market, selection, odd_decimal), ...] dos mercados de interesse."""
    out: list[tuple[str, str, float]] = []
    ou_groups = {"Corners 2-Way": "corners", "Total Cards": "cards"}
    for m in markets or []:
        if m.get("marketPeriod") not in (None, "Full-time"):
            continue
        group = m.get("marketGroup")
        if group == "1X2":
            sel_map = {"1": "home", "X": "draw", "2": "away"}
            for ch in m.get("choices", []):
                sel = sel_map.get(ch.get("name"))
                dec = _frac_to_dec(ch.get("fractionalValue"))
                if sel and dec:
                    out.append(("1X2", sel, dec))
        elif group in ou_groups:
            canon = ou_groups[group]
            line = m.get("choiceGroup")
            if not line:
                continue
            for ch in m.get("choices", []):
                side = (ch.get("name") or "").lower()
                dec = _frac_to_dec(ch.get("fractionalValue"))
                if side in ("over", "under") and dec:
                    out.append((canon, f"{side}{line}", dec))
    return out


def _fixtures(client: httpx.Client, league: str, limit: int
              ) -> tuple[list[dict], str | None]:
    """Proximos jogos da liga via tournaments/get-next-matches."""
    tid = TOURNAMENTS.get(league)
    if not tid:
        return [], None
    seasons, q1 = _get(client, "/tournaments/get-seasons", {"tournamentId": str(tid)})
    if not seasons or not seasons.get("seasons"):
        return [], q1
    sid = seasons["seasons"][0]["id"]
    data, q2 = _get(client, "/tournaments/get-next-matches",
                    {"tournamentId": str(tid), "seasonId": str(sid), "pageIndex": "0"})
    events = (data or {}).get("events", [])[:limit]
    fixtures = []
    for ev in events:
        ts = ev.get("startTimestamp")
        fixtures.append({
            "match_id": ev.get("id"),
            "home_team": (ev.get("homeTeam") or {}).get("name"),
            "away_team": (ev.get("awayTeam") or {}).get("name"),
            "kickoff_utc": (datetime.fromtimestamp(ts, tz=UTC).isoformat()
                            if isinstance(ts, int | float) else None),
        })
    return fixtures, q2 or q1


def collect_once(path: str | None = None) -> dict[str, Any]:
    """Uma rodada de coleta para todas as ligas configuradas."""
    if not _env("BETFLOW_RAPIDAPI_KEY"):
        return {"error": "BETFLOW_RAPIDAPI_KEY nao configurada", "snapshots": 0}
    init_db(path)
    captured_at = _utcnow()
    max_fx = int(_env("BETFLOW_MAX_FIXTURES", "10") or "10")
    total_snaps, total_fx, quota = 0, 0, None

    with httpx.Client() as client:
        for lg in leagues():
            try:
                fixtures, quota = _fixtures(client, lg, max_fx)
                rows = []
                for fx in fixtures:
                    if not fx.get("match_id"):
                        continue
                    try:
                        data, quota = _get(client, "/matches/get-all-odds",
                                           {"matchId": str(fx["match_id"])})
                    except httpx.HTTPError:
                        continue
                    markets = (data or {}).get("markets", [])
                    for market, sel, odd in _parse_markets(markets):
                        rows.append((captured_at, lg, fx["match_id"],
                                     fx["home_team"], fx["away_team"],
                                     fx["kickoff_utc"], market, sel, odd))
                n = _save(rows, path)
                _log_run(lg, len(fixtures), n, quota, path=path)
                total_snaps += n
                total_fx += len(fixtures)
            except Exception as exc:  # uma liga falha nao derruba as outras
                _log_run(lg, 0, 0, quota, note=str(exc)[:180], path=path)

    return {"captured_at": captured_at, "leagues": leagues(),
            "fixtures": total_fx, "snapshots": total_snaps, "quota_left": quota}


def _save(rows: list[tuple], path: str | None = None) -> int:
    if not rows:
        return 0
    with _conn(path) as c:
        c.executemany(
            """INSERT INTO odds_snapshots
               (captured_at, league, match_id, home_team, away_team,
                kickoff_utc, market, selection, odd)
               VALUES (?,?,?,?,?,?,?,?,?);""", rows)
    return len(rows)


def _log_run(league: str, fixtures: int, snapshots: int, quota_left,
             note: str = "", path: str | None = None) -> None:
    with _conn(path) as c:
        c.execute(
            """INSERT INTO collect_runs
               (ran_at, league, fixtures, snapshots, quota_left, note)
               VALUES (?,?,?,?,?,?);""",
            (_utcnow(), league, fixtures, snapshots,
             int(quota_left) if str(quota_left).isdigit() else None, note))


def stats(path: str | None = None) -> dict[str, Any]:
    with _conn(path) as c:
        r = c.execute(
            """SELECT COUNT(*) s, COUNT(DISTINCT match_id) m,
                      MIN(captured_at) f, MAX(captured_at) l
               FROM odds_snapshots;""").fetchone()
        runs = c.execute("SELECT COUNT(*) n FROM collect_runs;").fetchone()
    return {"snapshots": r["s"] or 0, "matches": r["m"] or 0,
            "first": r["f"], "last": r["l"], "runs": runs["n"] or 0}

