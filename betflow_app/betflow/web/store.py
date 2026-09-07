"""Persistencia do bet tracker em SQLite (stdlib, sem ORM externo).

Duas nocoes de banca:
- **inicial** (bankroll): capital de partida, definido uma vez.
- **atual**: inicial + soma dos lucros/prejuizos das apostas liquidadas.

Cada aposta tem ciclo: PENDING -> WON/LOST/VOID (liquidacao). O P&L (profit &
loss) so muda quando a aposta e liquidada:
    WON  -> +stake * (odd - 1)
    LOST -> -stake
    VOID -> 0 (aposta anulada / devolvida)
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from config import settings
from betflow.web import db
from betflow.web.db import BACKEND

DB_PATH = settings.DATA_DIR / "betflow.db"

# Usuario local padrao. Toda a persistencia ja carrega user_id para que a
# evolucao para multi-tenant (SaaS) seja apenas ativar login e trocar este
# valor pelo id da sessao - sem migrar dados nem reescrever queries.
DEFAULT_USER_ID = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT UNIQUE,
    name         TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bankroll (
    user_id      INTEGER PRIMARY KEY,
    initial      REAL NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    division      TEXT,             -- codigo da liga (E0, BSA, ...)
    sport_key     TEXT,             -- sport key da The Odds API
    event_id      TEXT,             -- id do evento na Odds API (liquidacao auto)
    commence_time TEXT,             -- inicio do jogo (ISO UTC)
    home_team     TEXT NOT NULL,
    away_team     TEXT NOT NULL,
    market        TEXT NOT NULL,   -- ex.: 1X2:H, OU2.5:over, corners:over9.5
    selection     TEXT NOT NULL,   -- rotulo legivel
    odd           REAL NOT NULL,
    stake         REAL NOT NULL,
    model_prob    REAL,
    ev            REAL,
    edge          REAL,
    status        TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING/WON/LOST/VOID
    pnl           REAL NOT NULL DEFAULT 0.0,
    auto_settled  INTEGER NOT NULL DEFAULT 0,       -- 1 = liquidada pela API
    settled_at    TEXT
);

CREATE TABLE IF NOT EXISTS suggestions_bankroll (
    user_id      INTEGER PRIMARY KEY,
    initial      REAL NOT NULL DEFAULT 1000.0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suggestions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL DEFAULT 1,
    generated_at  TEXT NOT NULL,      -- quando a sugestao foi gerada/escaneada
    division      TEXT NOT NULL,      -- codigo da liga
    sport_key     TEXT,               -- sport key da The Odds API
    event_id      TEXT,               -- id do evento na Odds API
    commence_time TEXT,               -- inicio do jogo (ISO UTC)
    home_team     TEXT NOT NULL,
    away_team     TEXT NOT NULL,
    market        TEXT NOT NULL,      -- ex.: 1X2:H
    selection     TEXT NOT NULL,
    side          TEXT,               -- home/draw/away
    bookmaker     TEXT,
    strategy      TEXT NOT NULL DEFAULT 'main',  -- 'main' | 'boosted'
    odd           REAL NOT NULL,
    model_prob    REAL,               -- prob. usada (ancorada ao mercado)
    implied_prob  REAL,               -- 1/cotacao (crua)
    fair_prob     REAL,               -- prob. justa do mercado (Shin, sem margem)
    confidence    REAL,               -- fator de confianca aplicado ao Kelly [0..1]
    ev            REAL,
    edge          REAL,
    source        TEXT,               -- modelo | mercado
    stake_frac    REAL,               -- fracao de kelly (ex.: 0.02)
    home_score    INTEGER,
    away_score    INTEGER,
    status        TEXT NOT NULL DEFAULT 'PENDING',
    pnl           REAL NOT NULL DEFAULT 0.0,
    auto_settled  INTEGER NOT NULL DEFAULT 0,
    settled_at    TEXT,
    UNIQUE(user_id, strategy, event_id, market) ON CONFLICT IGNORE
);

CREATE TABLE IF NOT EXISTS suggestions_scans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL DEFAULT 1,
    scanned_at   TEXT NOT NULL,
    bookmaker    TEXT,
    days         INTEGER,
    today_only   INTEGER,
    leagues      TEXT,
    n_suggestions INTEGER,
    quota_remaining INTEGER,
    notes        TEXT
);

CREATE TABLE IF NOT EXISTS matches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    league_code   TEXT NOT NULL,       -- E0, BSA, ...
    event_id      TEXT,                -- id externo (SofaScore/Odds API)
    match_date    TEXT NOT NULL,       -- ISO UTC do inicio do jogo
    season        TEXT,                -- codigo da temporada (ex.: 2526)
    home_team     TEXT NOT NULL,
    away_team     TEXT NOT NULL,
    home_goals    INTEGER,
    away_goals    INTEGER,
    home_corners  INTEGER,
    away_corners  INTEGER,
    home_yellow   INTEGER,
    away_yellow   INTEGER,
    source        TEXT,                -- sofascore | odds_api | csv
    updated_at    TEXT NOT NULL,
    UNIQUE(league_code, home_team, away_team, match_date) ON CONFLICT REPLACE
);

"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_matches_league ON matches(league_code);
CREATE INDEX IF NOT EXISTS idx_matches_date   ON matches(match_date);
CREATE INDEX IF NOT EXISTS idx_bets_user   ON bets(user_id);
CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status);
CREATE INDEX IF NOT EXISTS idx_bets_event  ON bets(event_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_user   ON suggestions(user_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status);
CREATE INDEX IF NOT EXISTS idx_suggestions_event  ON suggestions(event_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_generated ON suggestions(generated_at);
"""

# Migracao idempotente: adiciona colunas novas em bancos antigos (uso pessoal
# em andamento) sem perder dados. Cada ALTER e tentado e ignorado se ja existe.
_MIGRATIONS = [
    "ALTER TABLE bets ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE bets ADD COLUMN sport_key TEXT",
    "ALTER TABLE bets ADD COLUMN event_id TEXT",
    "ALTER TABLE bets ADD COLUMN commence_time TEXT",
    "ALTER TABLE bets ADD COLUMN auto_settled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE suggestions ADD COLUMN home_score INTEGER",
    "ALTER TABLE suggestions ADD COLUMN away_score INTEGER",
    "ALTER TABLE suggestions ADD COLUMN strategy TEXT NOT NULL DEFAULT 'main'",
    "ALTER TABLE suggestions ADD COLUMN fair_prob REAL",
    "ALTER TABLE suggestions ADD COLUMN confidence REAL",
]


# SQL para criar tabelas de historico de sugestoes em bancos ja existentes
# (executado via init_db depois do schema base).
_HISTORY_TABLES = """
CREATE TABLE IF NOT EXISTS suggestions_bankroll (
    user_id      INTEGER PRIMARY KEY,
    initial      REAL NOT NULL DEFAULT 1000.0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suggestions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL DEFAULT 1,
    generated_at  TEXT NOT NULL,
    division      TEXT NOT NULL,
    sport_key     TEXT,
    event_id      TEXT,
    commence_time TEXT,
    home_team     TEXT NOT NULL,
    away_team     TEXT NOT NULL,
    market        TEXT NOT NULL,
    selection     TEXT NOT NULL,
    side          TEXT,
    bookmaker     TEXT,
    strategy      TEXT NOT NULL DEFAULT 'main',
    odd           REAL NOT NULL,
    model_prob    REAL,
    implied_prob  REAL,
    fair_prob     REAL,
    confidence    REAL,
    ev            REAL,
    edge          REAL,
    source        TEXT,
    stake_frac    REAL,
    home_score    INTEGER,
    away_score    INTEGER,
    status        TEXT NOT NULL DEFAULT 'PENDING',
    pnl           REAL NOT NULL DEFAULT 0.0,
    auto_settled  INTEGER NOT NULL DEFAULT 0,
    settled_at    TEXT,
    UNIQUE(user_id, strategy, event_id, market) ON CONFLICT IGNORE
);

CREATE TABLE IF NOT EXISTS suggestions_scans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL DEFAULT 1,
    scanned_at   TEXT NOT NULL,
    bookmaker    TEXT,
    days         INTEGER,
    today_only   INTEGER,
    leagues      TEXT,
    n_suggestions INTEGER,
    quota_remaining INTEGER,
    notes        TEXT
);

CREATE INDEX IF NOT EXISTS idx_suggestions_user   ON suggestions(user_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status);
CREATE INDEX IF NOT EXISTS idx_suggestions_event  ON suggestions(event_id);
CREATE INDEX IF NOT EXISTS idx_suggestions_generated ON suggestions(generated_at);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    for stmt in _MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # coluna ja existe -> ok
    # migra a tabela bankroll antiga (chave fixa id=1) para user_id, se preciso
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bankroll);").fetchall()}
    if "id" in cols and "user_id" not in cols:
        row = conn.execute("SELECT initial, created_at FROM bankroll "
                           "WHERE id = 1;").fetchone()
        conn.execute("DROP TABLE bankroll;")
        conn.execute("CREATE TABLE bankroll (user_id INTEGER PRIMARY KEY, "
                     "initial REAL NOT NULL, created_at TEXT NOT NULL);")
        if row:
            conn.execute("INSERT INTO bankroll (user_id, initial, created_at) "
                         "VALUES (?,?,?);",
                         (DEFAULT_USER_ID, row["initial"], row["created_at"]))


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn(db_path: Path | str = DB_PATH) -> Any:
    """Abre uma conexao no backend ativo (SQLite local ou Postgres em prod)."""
    return db.get_conn(db_path)


def _init_db_postgres() -> None:
    """Cria o schema no Postgres e semeia usuario/banca padrao."""
    db.init_pg_schema()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (id, email, name, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT (id) DO NOTHING;",
            (DEFAULT_USER_ID, "local@betflow", "Usuario local", _utcnow()),
        )
        conn.execute(
            "INSERT INTO suggestions_bankroll (user_id, initial, created_at) "
            "VALUES (?,?,?) ON CONFLICT (user_id) DO NOTHING;",
            (DEFAULT_USER_ID, 1000.0, _utcnow()),
        )


def init_db(db_path: Path | str = DB_PATH) -> None:
    if BACKEND == "postgres":
        _init_db_postgres()
        return
    with get_conn(db_path) as conn:
        conn.executescript(_SCHEMA)   # cria tabelas que faltam (IF NOT EXISTS)
        # cria as tabelas do historico caso o schema base nao as tenha (antigos)
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if {"suggestions_bankroll", "suggestions", "suggestions_scans"} - existing:
            conn.executescript(_HISTORY_TABLES)
        _migrate(conn)                # adiciona colunas/rebuild em bancos antigos
        conn.executescript(_INDEXES)  # indices depois das colunas existirem
        # garante a existencia do usuario local padrao
        conn.execute(
            "INSERT OR IGNORE INTO users (id, email, name, created_at) "
            "VALUES (?,?,?,?);",
            (DEFAULT_USER_ID, "local@betflow", "Usuario local", _utcnow()),
        )
        # garante a banca inicial padrao do historico de sugestoes (1000.0)
        conn.execute(
            "INSERT OR IGNORE INTO suggestions_bankroll (user_id, initial, created_at) "
            "VALUES (?,?,?);",
            (DEFAULT_USER_ID, 1000.0, _utcnow()),
        )


# ---------------------------------------------------------------------------
# banca
# ---------------------------------------------------------------------------
def set_bankroll(initial: float, db_path: Path | str = DB_PATH, *,
                 user_id: int = DEFAULT_USER_ID) -> None:
    """Define (ou redefine) a banca inicial do usuario."""
    if initial <= 0:
        raise ValueError("banca inicial deve ser > 0")
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO bankroll (user_id, initial, created_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET initial=excluded.initial, "
            "created_at=excluded.created_at;",
            (user_id, float(initial), _utcnow()),
        )


def get_initial_bankroll(db_path: Path | str = DB_PATH, *,
                         user_id: int = DEFAULT_USER_ID) -> float | None:
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT initial FROM bankroll WHERE user_id = ?;",
                           (user_id,)).fetchone()
    return float(row["initial"]) if row else None



# ---------------------------------------------------------------------------
# apostas
# ---------------------------------------------------------------------------
@dataclass
class BetInput:
    home_team: str
    away_team: str
    market: str
    selection: str
    odd: float
    stake: float
    division: str | None = None
    model_prob: float | None = None
    ev: float | None = None
    edge: float | None = None
    sport_key: str | None = None
    event_id: str | None = None
    commence_time: str | None = None
    user_id: int = DEFAULT_USER_ID


def add_bet(bet: BetInput, db_path: Path | str = DB_PATH) -> int:
    if bet.odd <= 1.0:
        raise ValueError("odd decimal deve ser > 1.0")
    if bet.stake <= 0:
        raise ValueError("stake deve ser > 0")
    sql = (
        """INSERT INTO bets
           (user_id, created_at, division, sport_key, event_id,
            commence_time, home_team, away_team, market, selection,
            odd, stake, model_prob, ev, edge, status, pnl)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'PENDING', 0.0)"""
    )
    params = (bet.user_id, _utcnow(), bet.division, bet.sport_key, bet.event_id,
              bet.commence_time, bet.home_team, bet.away_team, bet.market,
              bet.selection, float(bet.odd), float(bet.stake),
              bet.model_prob, bet.ev, bet.edge)
    with get_conn(db_path) as conn:
        if BACKEND == "postgres":
            cur = conn.execute(sql + " RETURNING id;", params)
            return int(cur.fetchone()["id"])
        cur = conn.execute(sql + ";", params)
        return int(cur.lastrowid)


def settle_bet(bet_id: int, status: str, db_path: Path | str = DB_PATH, *,
               auto: bool = False) -> None:
    """Liquida uma aposta e calcula o P&L de acordo com o resultado.

    `auto=True` marca que a liquidacao veio da busca automatica de resultados.
    """
    status = status.upper()
    if status not in ("WON", "LOST", "VOID"):
        raise ValueError("status deve ser WON, LOST ou VOID")
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT odd, stake, status FROM bets WHERE id = ?;", (bet_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"aposta {bet_id} nao encontrada")
        if row["status"] != "PENDING":
            raise ValueError(f"aposta {bet_id} ja liquidada ({row['status']})")

        if status == "WON":
            pnl = row["stake"] * (row["odd"] - 1.0)
        elif status == "LOST":
            pnl = -row["stake"]
        else:  # VOID
            pnl = 0.0

        conn.execute(
            "UPDATE bets SET status=?, pnl=?, auto_settled=?, settled_at=? "
            "WHERE id = ?;",
            (status, float(pnl), 1 if auto else 0, _utcnow(), bet_id),
        )


def delete_bet(bet_id: int, db_path: Path | str = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM bets WHERE id = ?;", (bet_id,))


def list_bets(status: str | None = None, db_path: Path | str = DB_PATH, *,
              user_id: int = DEFAULT_USER_ID) -> list[dict[str, Any]]:
    q = "SELECT * FROM bets WHERE user_id = ?"
    params: list[Any] = [user_id]
    if status:
        q += " AND status = ?"
        params.append(status.upper())
    q += " ORDER BY id DESC;"
    with get_conn(db_path) as conn:
        rows = conn.execute(q, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def list_pending_with_event(db_path: Path | str = DB_PATH, *,
                            user_id: int = DEFAULT_USER_ID) -> list[dict[str, Any]]:
    """Apostas abertas que tem event_id (podem ser liquidadas pela Odds API)."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM bets WHERE user_id = ? AND status = 'PENDING' "
            "AND event_id IS NOT NULL;", (user_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def settled_1x2_calibration_samples(division: str | None = None,
                                    db_path: Path | str = DB_PATH, *,
                                    user_id: int = DEFAULT_USER_ID
                                    ) -> dict[str, list[tuple[float, int]]]:
    """Amostras (model_prob, outcome) por mercado 1X2:H/D/A das apostas liquidadas.

    Usado para treinar calibradores de probabilidade a partir do histórico real
    de apostas registradas no tracker. Se `division` for informado, filtra por
    código de liga (E0, BSA, ...).
    """
    samples: dict[str, list[tuple[float, int]]] = {"H": [], "D": [], "A": []}
    q = ("SELECT market, model_prob, status FROM bets "
         "WHERE user_id = ? AND status IN ('WON','LOST') "
         "AND market IN ('1X2:H','1X2:D','1X2:A') AND model_prob IS NOT NULL")
    params: list[Any] = [user_id]
    if division:
        q += " AND division = ?"
        params.append(division)
    q += ";"
    with get_conn(db_path) as conn:
        rows = conn.execute(q, tuple(params)).fetchall()
    for r in rows:
        key = r["market"].split(":")[-1]
        if key not in samples:
            continue
        outcome = 1 if r["status"] == "WON" else 0
        samples[key].append((float(r["model_prob"]), outcome))
    return samples


def list_pending_suggestions(db_path: Path | str = DB_PATH, *,
                             user_id: int = DEFAULT_USER_ID
                             ) -> list[dict[str, Any]]:
    """Sugestoes pendentes com event_id (podem ser liquidadas automaticamente)."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM suggestions WHERE user_id = ? AND status = 'PENDING' "
            "AND event_id IS NOT NULL;", (user_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def settle_suggestion(suggestion_id: int, status: str,
                      home_score: int | None = None,
                      away_score: int | None = None,
                      db_path: Path | str = DB_PATH, *,
                      auto: bool = False) -> None:
    """Liquida uma sugestao e calcula o P&L usando stake fracao da banca."""
    status = status.upper()
    if status not in ("WON", "LOST", "VOID"):
        raise ValueError("status deve ser WON, LOST ou VOID")
    with get_conn(db_path) as conn:
        initial = get_suggestions_initial_bankroll(db_path, user_id=1)
        row = conn.execute(
            "SELECT odd, stake_frac, status FROM suggestions WHERE id = ?;",
            (suggestion_id,)).fetchone()
        if row is None:
            raise KeyError(f"sugestao {suggestion_id} nao encontrada")
        if row["status"] != "PENDING":
            raise ValueError(f"sugestao {suggestion_id} ja liquidada")

        stake = (row["stake_frac"] or 0.0) * initial
        if status == "WON":
            pnl = stake * (row["odd"] - 1.0)
        elif status == "LOST":
            pnl = -stake
        else:
            pnl = 0.0

        conn.execute(
            "UPDATE suggestions SET status=?, pnl=?, auto_settled=?, settled_at=?, "
            "home_score=?, away_score=? WHERE id = ?;",
            (status, float(pnl), 1 if auto else 0, _utcnow(),
             home_score, away_score, suggestion_id),
        )


# ---------------------------------------------------------------------------
# historico de sugestoes (track record do modelo)
# ---------------------------------------------------------------------------
@dataclass
class SuggestionInput:
    """Dados de uma sugestao de aposta gerada pelo motor."""
    division: str
    home_team: str
    away_team: str
    market: str
    selection: str
    odd: float
    sport_key: str | None = None
    event_id: str | None = None
    commence_time: str | None = None
    side: str | None = None
    bookmaker: str | None = None
    model_prob: float | None = None
    implied_prob: float | None = None
    fair_prob: float | None = None
    confidence: float | None = None
    ev: float | None = None
    edge: float | None = None
    source: str | None = None
    stake_frac: float | None = None
    user_id: int = DEFAULT_USER_ID


def get_suggestions_initial_bankroll(db_path: Path | str = DB_PATH, *,
                                     user_id: int = DEFAULT_USER_ID
                                     ) -> float:
    """Banca inicial padrao (fixa em 1000.0) do track record de sugestoes."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT initial FROM suggestions_bankroll WHERE user_id = ?;",
            (user_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO suggestions_bankroll (user_id, initial, created_at) "
                "VALUES (?,?,?);",
                (user_id, 1000.0, _utcnow()))
            conn.commit()
            return 1000.0
    return float(row["initial"])


def save_suggestions(suggestions: list[dict[str, Any]],
                       bookmaker: str | None = None,
                       days: int = 7, today_only: bool = False,
                       leagues: list[str] | None = None,
                       quota_remaining: int | None = None,
                       db_path: Path | str = DB_PATH,
                       *,
                       strategy: str = "main",
                       user_id: int = DEFAULT_USER_ID) -> int:
    """Persiste as sugestoes geradas em um scan.

    Ignora duplicatas por (user_id, event_id, market). Cria o registro do scan.
    Retorna o numero de sugestoes inseridas.
    """
    scanned_at = _utcnow()
    inserted = 0
    with get_conn(db_path) as conn:
        for s in suggestions:
            division = s.get("league_code") or s.get("division") or ""
            if not division:
                continue
            event_id = s.get("event_id")
            # Fallback para chave unica caso a API nao retorne event_id.
            if not event_id:
                event_id = (
                    f"{s.get('home','')}|{s.get('away','')}|"
                    f"{s.get('commence_time','')}|{s.get('market','')}"
                )
            cur = conn.execute(
                """INSERT INTO suggestions
                   (user_id, generated_at, division, sport_key, event_id,
                    commence_time, home_team, away_team, market, selection,
                    side, bookmaker, strategy, odd, model_prob, implied_prob,
                    fair_prob, confidence, ev, edge, source, stake_frac,
                    home_score, away_score, status, pnl)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING',0.0)
                   ON CONFLICT(user_id, strategy, event_id, market) DO NOTHING;""",
                (user_id, scanned_at, division,
                 s.get("sport_key"), event_id, s.get("commence_time"),
                 s.get("home"), s.get("away"), s.get("market"),
                 s.get("selection"), s.get("side"), bookmaker, strategy,
                 float(s["odd"]), s.get("prob"), s.get("implied"),
                 s.get("fair_prob"), s.get("confidence"),
                 s.get("ev"), s.get("edge"), s.get("source"),
                 s.get("stake_frac"), None, None),
            )
            inserted += cur.rowcount
        conn.execute(
            """INSERT INTO suggestions_scans
               (user_id, scanned_at, bookmaker, days, today_only, leagues,
                n_suggestions, quota_remaining, notes)
               VALUES (?,?,?,?,?,?,?,?,?);""",
            (user_id, scanned_at, bookmaker, days,
             1 if today_only else 0, ",".join(leagues or []), inserted,
             quota_remaining, None),
        )
        conn.commit()
    return inserted


def list_suggestions(status: str | None = None,
                     division: str | None = None,
                     db_path: Path | str = DB_PATH, *,
                     strategy: str | None = None,
                     user_id: int = DEFAULT_USER_ID,
                     order: str = "generated_at DESC, id DESC"
                     ) -> list[dict[str, Any]]:
    """Lista sugestoes do track record, com filtros opcionais."""
    q = "SELECT * FROM suggestions WHERE user_id = ?"
    params: list[Any] = [user_id]
    if status:
        q += " AND status = ?"
        params.append(status.upper())
    if division:
        q += " AND division = ?"
        params.append(division.upper())
    if strategy:
        q += " AND strategy = ?"
        params.append(strategy)
    q += f" ORDER BY {order};"
    with get_conn(db_path) as conn:
        rows = conn.execute(q, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def list_open_recommendations(limit: int = 10,
                              db_path: Path | str = DB_PATH, *,
                              strategy: str | None = None,
                              user_id: int = DEFAULT_USER_ID
                              ) -> list[dict[str, Any]]:
    """Sugestoes pendentes de jogos ainda nao terminados, melhores EV primeiro.

    Alimenta o painel "Recomendacoes em aberto" do dashboard: o que seguir
    agora, com retorno esperado e fracao de alocacao sugerida. `strategy` isola
    a linha de predicao ('main'|'boosted').
    """
    cutoff = datetime.now(timezone.utc).isoformat(timespec="seconds")
    q = ("SELECT * FROM suggestions WHERE user_id = ? AND status = 'PENDING' "
         "AND (commence_time IS NULL OR commence_time >= ?)")
    params: list[Any] = [user_id, cutoff]
    if strategy:
        q += " AND strategy = ?"
        params.append(strategy)
    q += " ORDER BY ev DESC, commence_time ASC LIMIT ?;"
    params.append(limit)
    with get_conn(db_path) as conn:
        rows = conn.execute(q, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def list_open_recommendations_filtered(
        leagues: list[str] | None = None, days: int | None = None,
        today_only: bool = False, limit: int = 300,
        db_path: Path | str = DB_PATH, *,
        strategy: str | None = None,
        user_id: int = DEFAULT_USER_ID) -> list[dict[str, Any]]:
    """Recomendacoes pendentes de jogos futuros, filtradas por liga/janela.

    Le APENAS do banco (populado pelo scan automatico de 3 em 3 horas); nao
    consome cota de API. Ordena pelo maior EV. `strategy` isola a linha.
    """
    now = datetime.now(timezone.utc)
    q = ("SELECT * FROM suggestions WHERE user_id = ? AND status = 'PENDING' "
         "AND (commence_time IS NULL OR commence_time >= ?)")
    params: list[Any] = [user_id, now.isoformat(timespec="seconds")]
    if strategy:
        q += " AND strategy = ?"
        params.append(strategy)
    if today_only:
        end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        q += " AND commence_time <= ?"
        params.append(end.isoformat(timespec="seconds"))
    elif days:
        end = now + timedelta(days=days)
        q += " AND commence_time <= ?"
        params.append(end.isoformat(timespec="seconds"))
    if leagues:
        marks = ",".join("?" for _ in leagues)
        q += f" AND division IN ({marks})"
        params.extend([c.upper() for c in leagues])
    q += " ORDER BY ev DESC, commence_time ASC LIMIT ?;"
    params.append(limit)
    with get_conn(db_path) as conn:
        rows = conn.execute(q, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def suggestions_stats_by_league(db_path: Path | str = DB_PATH, *,
                                user_id: int = DEFAULT_USER_ID,
                                strategy: str | None = None,
                                min_samples: int = 1
                                ) -> list[dict[str, Any]]:
    """Taxa de acerto e P&L por campeonato no track record."""
    q = ("""SELECT
                 division,
                 COUNT(*)                                          AS total,
                 SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) AS pending,
                 SUM(CASE WHEN status='WON'  THEN 1 ELSE 0 END)    AS won,
                 SUM(CASE WHEN status='LOST' THEN 1 ELSE 0 END)    AS lost,
                 SUM(CASE WHEN status='VOID' THEN 1 ELSE 0 END)    AS void,
                 COALESCE(SUM(pnl), 0.0)                           AS pnl,
                 COALESCE(SUM(CASE WHEN status IN ('WON','LOST')
                                   THEN ABS(pnl) ELSE 0 END), 0.0) AS turnover
               FROM suggestions WHERE user_id = ?""")
    params: list[Any] = [user_id]
    if strategy:
        q += " AND strategy = ?"
        params.append(strategy)
    q += " GROUP BY division ORDER BY total DESC;"
    with get_conn(db_path) as conn:
        rows = conn.execute(q, tuple(params)).fetchall()

    result: list[dict[str, Any]] = []
    for r in rows:
        total = r["total"] or 0
        won = r["won"] or 0
        lost = r["lost"] or 0
        settled = won + lost
        pnl = float(r["pnl"] or 0.0)
        turnover = float(r["turnover"] or 0.0)
        if total >= min_samples:
            result.append({
                "division": r["division"],
                "name": settings.LEAGUES.get(r["division"], {}).get("name", r["division"]),
                "total": total,
                "pending": r["pending"] or 0,
                "won": won,
                "lost": lost,
                "void": r["void"] or 0,
                "pnl": pnl,
                "turnover": turnover,
                "yield": (pnl / turnover) if turnover else None,
                "hit_rate": (won / settled) if settled else None,
            })
    return result


# ---------------------------------------------------------------------------
# metricas agregadas
# ---------------------------------------------------------------------------
def suggestions_stats(db_path: Path | str = DB_PATH, *,
                       user_id: int = DEFAULT_USER_ID,
                       division: str | None = None,
                       strategy: str | None = None,
                       start_bankroll: float = 1000.0
                       ) -> dict[str, Any]:
    """Metricas de desempenho do track record de sugestoes.

    Calcula P&L, ROI, yield, taxa de acerto, drawdown maximo, serie temporal
    da banca, e medias de edge/EV das sugestoes ganhadoras/perdedoras.
    Se `strategy` for informado, isola a linha de predicao ('main'|'boosted').
    """
    initial = start_bankroll
    params: list[Any] = [user_id]
    where = "WHERE user_id = ?"
    if division:
        where += " AND division = ?"
        params.append(division.upper())
    if strategy:
        where += " AND strategy = ?"
        params.append(strategy)

    with get_conn(db_path) as conn:
        agg = conn.execute(
            f"""SELECT
                 COUNT(*)                                          AS total,
                 SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) AS pending,
                 SUM(CASE WHEN status='WON'  THEN 1 ELSE 0 END)    AS won,
                 SUM(CASE WHEN status='LOST' THEN 1 ELSE 0 END)    AS lost,
                 SUM(CASE WHEN status='VOID' THEN 1 ELSE 0 END)    AS void,
                 COALESCE(SUM(pnl), 0.0)                           AS pnl,
                 COALESCE(SUM(CASE WHEN status IN ('WON','LOST')
                                   THEN ABS(pnl) ELSE 0 END), 0.0) AS turnover,
                 COALESCE(SUM(CASE WHEN status='WON' THEN 1 ELSE 0 END), 0) AS n_won,
                 COALESCE(AVG(CASE WHEN status='WON' THEN ev END), 0) AS avg_ev_won,
                 COALESCE(AVG(CASE WHEN status='LOST' THEN ev END), 0) AS avg_ev_lost,
                 COALESCE(AVG(CASE WHEN status='WON' THEN edge END), 0) AS avg_edge_won,
                 COALESCE(AVG(CASE WHEN status='LOST' THEN edge END), 0) AS avg_edge_lost
               FROM suggestions {where};""", tuple(params)
        ).fetchone()

        equity_rows = conn.execute(
            f"""SELECT generated_at, pnl FROM suggestions
               {where} AND status IN ('WON','LOST')
               ORDER BY generated_at, id;""", tuple(params)
        ).fetchall()

    total = agg["total"] or 0
    won = agg["won"] or 0
    lost = agg["lost"] or 0
    void_ = agg["void"] or 0
    pnl = float(agg["pnl"] or 0.0)
    turnover = float(agg["turnover"] or 0.0)
    settled = won + lost
    current = initial + pnl

    peak = initial
    max_drawdown = 0.0
    equity = {"labels": ["inicio"], "values": [initial]}
    running = initial
    for r in equity_rows:
        running += float(r["pnl"])
        peak = max(peak, running)
        dd = (peak - running) / peak if peak else 0.0
        max_drawdown = max(max_drawdown, dd)
        equity["labels"].append(r["generated_at"][:10])
        equity["values"].append(round(running, 2))

    return {
        "initial_bankroll": initial,
        "current_bankroll": current,
        "pnl": pnl,
        "turnover": turnover,
        "exposure": 0.0,
        "roi": (pnl / initial) if initial else None,
        "yield": (pnl / turnover) if turnover else None,
        "n_total": total,
        "n_pending": agg["pending"] or 0,
        "n_won": won,
        "n_lost": lost,
        "n_void": void_,
        "hit_rate": (won / settled) if settled else None,
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown * 100,
        "equity": equity,
        "equity_labels": equity["labels"],
        "equity_values": equity["values"],
        "avg_ev_won": agg["avg_ev_won"],
        "avg_ev_lost": agg["avg_ev_lost"],
        "avg_edge_won": agg["avg_edge_won"],
        "avg_edge_lost": agg["avg_edge_lost"],
    }


# ---------------------------------------------------------------------------
# metricas agregadas
# ---------------------------------------------------------------------------
def stats(db_path: Path | str = DB_PATH, *,
          user_id: int = DEFAULT_USER_ID) -> dict[str, Any]:
    """Resumo de desempenho do usuario: banca, P&L, ROI/yield, contagens."""
    initial = get_initial_bankroll(db_path, user_id=user_id)
    with get_conn(db_path) as conn:
        agg = conn.execute(
            """SELECT
                 COUNT(*)                                          AS total,
                 SUM(CASE WHEN status='PENDING' THEN 1 ELSE 0 END) AS pending,
                 SUM(CASE WHEN status='WON'  THEN 1 ELSE 0 END)    AS won,
                 SUM(CASE WHEN status='LOST' THEN 1 ELSE 0 END)    AS lost,
                 SUM(CASE WHEN status='VOID' THEN 1 ELSE 0 END)    AS void,
                 COALESCE(SUM(pnl), 0.0)                           AS pnl,
                 COALESCE(SUM(CASE WHEN status IN ('WON','LOST')
                                   THEN stake ELSE 0 END), 0.0)    AS turnover,
                 COALESCE(SUM(CASE WHEN status='PENDING'
                                   THEN stake ELSE 0 END), 0.0)    AS exposure
               FROM bets WHERE user_id = ?;""", (user_id,)
        ).fetchone()

    total = agg["total"] or 0
    won, lost = agg["won"] or 0, agg["lost"] or 0
    pnl = float(agg["pnl"] or 0.0)
    turnover = float(agg["turnover"] or 0.0)
    exposure = float(agg["exposure"] or 0.0)
    settled = won + lost
    current = (initial + pnl) if initial is not None else None

    return {
        "initial_bankroll": initial,
        "current_bankroll": current,
        "pnl": pnl,
        "exposure": exposure,                          # dinheiro em apostas abertas
        "turnover": turnover,                          # volume liquidado
        "roi": (pnl / initial) if initial else None,   # sobre a banca
        "yield": (pnl / turnover) if turnover else None,  # sobre o volume apostado
        "n_total": total,
        "n_pending": agg["pending"] or 0,
        "n_won": won,
        "n_lost": lost,
        "n_void": agg["void"] or 0,
        "hit_rate": (won / settled) if settled else None,
    }


# ---------------------------------------------------------------------------
# cache de resultados historicos (treino a partir do banco)
# ---------------------------------------------------------------------------
_MATCH_COLS = ("league_code", "event_id", "match_date", "season",
               "home_team", "away_team", "home_goals", "away_goals",
               "home_corners", "away_corners", "home_yellow", "away_yellow",
               "source")


def upsert_matches(rows: list[dict[str, Any]],
                   db_path: Path | str = DB_PATH) -> int:
    """Insere/atualiza resultados de jogos no cache. Retorna quantos gravou.

    Deduplica por (league_code, home_team, away_team, match_date). Ignora
    linhas sem placar (jogo ainda nao terminado).
    """
    now = _utcnow()
    n = 0
    with get_conn(db_path) as conn:
        for r in rows:
            if r.get("home_goals") is None or r.get("away_goals") is None:
                continue
            if not (r.get("home_team") and r.get("away_team")
                    and r.get("match_date") and r.get("league_code")):
                continue
            vals = tuple(r.get(c) for c in _MATCH_COLS) + (now,)
            cols = ",".join(_MATCH_COLS) + ",updated_at"
            marks = ",".join("?" for _ in range(len(_MATCH_COLS) + 1))
            if BACKEND == "postgres":
                upd = ",".join(f"{c}=excluded.{c}" for c in _MATCH_COLS
                               if c not in ("league_code", "home_team",
                                            "away_team", "match_date"))
                sql = (f"INSERT INTO matches ({cols}) VALUES ({marks}) "
                       "ON CONFLICT (league_code, home_team, away_team, "
                       f"match_date) DO UPDATE SET {upd}, updated_at=excluded.updated_at;")
            else:  # sqlite: UNIQUE(...) ON CONFLICT REPLACE cuida do upsert
                sql = f"INSERT INTO matches ({cols}) VALUES ({marks});"
            conn.execute(sql, vals)
            n += 1
        conn.commit()
    return n


def load_matches_df(league_code: str, seasons: int = 3,
                    db_path: Path | str = DB_PATH):
    """DataFrame de resultados do cache p/ treino (colunas do DixonColes).

    Retorna colunas home_team, away_team, home_goals, away_goals, date e, se
    houver, home_corners/away_corners/home_yellow/away_yellow. `seasons` limita
    aos N codigos de temporada mais recentes presentes no cache.
    """
    import pandas as pd
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM matches WHERE league_code = ? "
            "AND home_goals IS NOT NULL AND away_goals IS NOT NULL "
            "ORDER BY match_date;", (league_code,)).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df = df.rename(columns={"match_date": "date"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce", utc=True)
    df = df.dropna(subset=["date"])
    if seasons and "season" in df.columns and df["season"].notna().any():
        keep = sorted(df["season"].dropna().astype(str).unique())[-seasons:]
        df = df[df["season"].astype(str).isin(set(keep))]
    return df


def count_matches(league_code: str | None = None,
                  db_path: Path | str = DB_PATH) -> int:
    """Numero de jogos com placar no cache (por liga, ou total)."""
    with get_conn(db_path) as conn:
        if league_code:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM matches WHERE league_code = ? "
                "AND home_goals IS NOT NULL;", (league_code,)).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM matches "
                "WHERE home_goals IS NOT NULL;").fetchone()
    return int(row["n"])


def store_health(db_path: Path | str = DB_PATH) -> dict[str, Any]:
    """Diagnostico do armazenamento: backend ativo e contagens (para health)."""
    with get_conn(db_path) as conn:
        s = conn.execute("SELECT COUNT(*) AS n FROM suggestions;").fetchone()
        b = conn.execute("SELECT COUNT(*) AS n FROM bets;").fetchone()
    return {
        "backend": BACKEND,
        "suggestions": int(s["n"]),
        "bets": int(b["n"]),
    }
