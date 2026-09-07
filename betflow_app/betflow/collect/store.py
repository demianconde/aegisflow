"""Persistencia dos snapshots de odds coletados automaticamente.

Cada linha e uma observacao (snapshot) de uma odd de UMA selecao de UM jogo em
UM instante. Coletando varios snapshots ao longo do tempo ate o inicio do jogo,
poderemos depois calcular o CLV real (odd da abertura vs. odd do fechamento) -
exatamente o historico que hoje nao existe para escanteios/cartoes.

Tabela `odds_snapshots`:
    captured_at   quando coletamos (UTC)
    match_id      id SofaScore do jogo
    kickoff_utc   inicio do jogo
    market        ex.: 1X2, corners, cards, BTTS
    selection     ex.: home/draw/away, over9.5, over3.5
    odd           odd decimal observada
    model_prob    prob. do modelo (se o jogo for de time conhecido), senao NULL
    edge          model_prob - 1/odd (se houver model_prob)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import settings

DB_PATH = settings.DATA_DIR / "odds_history.db"

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
    odd          REAL NOT NULL,
    model_prob   REAL,
    edge         REAL
);
CREATE INDEX IF NOT EXISTS ix_snap_match
    ON odds_snapshots(match_id, market, selection, captured_at);

CREATE TABLE IF NOT EXISTS collect_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at       TEXT NOT NULL,
    league       TEXT,
    fixtures     INTEGER,
    snapshots    INTEGER,
    quota_left   INTEGER,
    note         TEXT
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(_SCHEMA)


def now() -> str:
    return _utcnow()


def record_snapshots(rows: list[dict[str, Any]],
                     db_path: Path | str = DB_PATH) -> int:
    """Grava uma lista de snapshots. Retorna quantos foram inseridos."""
    if not rows:
        return 0
    with get_conn(db_path) as conn:
        conn.executemany(
            """INSERT INTO odds_snapshots
               (captured_at, league, match_id, home_team, away_team,
                kickoff_utc, market, selection, odd, model_prob, edge)
               VALUES (:captured_at,:league,:match_id,:home_team,:away_team,
                       :kickoff_utc,:market,:selection,:odd,:model_prob,:edge);""",
            rows,
        )
    return len(rows)


def log_run(league: str, fixtures: int, snapshots: int,
            quota_left: int | None, note: str = "",
            db_path: Path | str = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO collect_runs
               (ran_at, league, fixtures, snapshots, quota_left, note)
               VALUES (?,?,?,?,?,?);""",
            (_utcnow(), league, fixtures, snapshots, quota_left, note),
        )



# ---------------------------------------------------------------------------
# consultas / metricas de CLV a partir do historico acumulado
# ---------------------------------------------------------------------------
def stats(db_path: Path | str = DB_PATH) -> dict[str, Any]:
    """Resumo do que ja foi coletado (para o dashboard)."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS snapshots,
                      COUNT(DISTINCT match_id) AS matches,
                      MIN(captured_at) AS first, MAX(captured_at) AS last
               FROM odds_snapshots;"""
        ).fetchone()
        runs = conn.execute("SELECT COUNT(*) AS n FROM collect_runs;").fetchone()
    return {
        "snapshots": row["snapshots"] or 0,
        "matches": row["matches"] or 0,
        "first_capture": row["first"],
        "last_capture": row["last"],
        "runs": runs["n"] or 0,
    }


def clv_report(db_path: Path | str = DB_PATH) -> list[dict[str, Any]]:
    """Para cada (match, market, selection), CLV = primeira odd / ultima odd - 1.

    So retorna selecoes com >=2 snapshots (precisamos de abertura e fechamento).
    Positivo => a primeira odd observada foi melhor que a ultima (pegamos valor).
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """WITH ordered AS (
                 SELECT match_id, market, selection, home_team, away_team,
                        odd, model_prob, captured_at,
                        ROW_NUMBER() OVER (PARTITION BY match_id, market, selection
                                           ORDER BY captured_at ASC)  AS rn_first,
                        ROW_NUMBER() OVER (PARTITION BY match_id, market, selection
                                           ORDER BY captured_at DESC) AS rn_last,
                        COUNT(*)   OVER (PARTITION BY match_id, market, selection)
                                           AS n_snaps
                 FROM odds_snapshots)
               SELECT o1.match_id, o1.home_team, o1.away_team, o1.market,
                      o1.selection, o1.model_prob,
                      o1.odd AS open_odd, o2.odd AS close_odd, o1.n_snaps
               FROM ordered o1
               JOIN ordered o2
                 ON o1.match_id=o2.match_id AND o1.market=o2.market
                AND o1.selection=o2.selection
               WHERE o1.rn_first=1 AND o2.rn_last=1 AND o1.n_snaps>=2;"""
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        clv = r["open_odd"] / r["close_odd"] - 1.0 if r["close_odd"] else 0.0
        out.append({
            "match": f"{r['home_team']} x {r['away_team']}",
            "market": r["market"], "selection": r["selection"],
            "open_odd": round(r["open_odd"], 3),
            "close_odd": round(r["close_odd"], 3),
            "clv": round(clv, 4), "model_prob": r["model_prob"],
            "snapshots": r["n_snaps"],
        })
    out.sort(key=lambda x: x["clv"], reverse=True)
    return out
