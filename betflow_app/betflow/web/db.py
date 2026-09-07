"""Camada de conexao dual-backend do Betflow (SQLite <-> PostgreSQL).

Motivacao: em desenvolvimento e nos testes usamos SQLite (arquivo local, zero
dependencia externa). Em producao, montado no AegisFlow, o Betflow passa a usar
o PostgreSQL/Supabase que o gateway ja possui — assim o track record e
**compartilhado entre maquinas e sobrevive a deploys** (o SQLite ficava em disco
efemero e reiniciava a cada deploy).

Selecao do backend (em tempo de import):
- PostgreSQL se ``BETFLOW_DATABASE_URL`` estiver definido, ou se
  ``BETFLOW_USE_GATEWAY_DB`` for verdadeiro e ``DATABASE_URL`` existir.
- Caso contrario, SQLite (default seguro para dev/testes).

As tabelas do Betflow ficam num schema Postgres dedicado (``betflow``) para nao
colidir com as tabelas do gateway (geridas por Alembic no schema ``public``).

O objeto de conexao devolvido por :func:`get_conn` expoe uma interface minima e
uniforme usada por ``store.py``: ``.execute(sql, params)`` (com fetchone/
fetchall/rowcount), ``.commit()`` e uso como context manager (faz commit no
sucesso). No Postgres os placeholders ``?`` sao traduzidos para ``%s``.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

_PG_SCHEMA_NAME = "betflow"


def _resolve_pg_url() -> str | None:
    """URL libpq para o psycopg, ou None se o backend deve ser SQLite."""
    url = os.getenv("BETFLOW_DATABASE_URL")
    if not url:
        if os.getenv("BETFLOW_USE_GATEWAY_DB", "").strip() in ("1", "true", "True"):
            url = os.getenv("DATABASE_URL")
    if not url:
        return None
    # remove sufixo de driver (o gateway usa +asyncpg) e normaliza o esquema
    for pref in ("postgresql+asyncpg://", "postgresql+psycopg://",
                 "postgresql+psycopg2://"):
        if url.startswith(pref):
            url = "postgresql://" + url[len(pref):]
            break
    if url.startswith("postgres://"):  # esquema legado (Heroku/Supabase)
        url = "postgresql://" + url[len("postgres://"):]
    # so ativa Postgres se a URL for de fato Postgres (o gateway pode apontar
    # DATABASE_URL para sqlite+aiosqlite em dev -> mantem o Betflow em SQLite).
    if not url.startswith("postgresql://"):
        return None
    return url


PG_URL = _resolve_pg_url()
BACKEND = "postgres" if PG_URL else "sqlite"


# ---------------------------------------------------------------------------
# wrapper Postgres (psycopg 3)
# ---------------------------------------------------------------------------
class _PGConn:
    """Adapta uma conexao psycopg para a interface esperada por store.py."""

    def __init__(self, raw: Any) -> None:
        self._c = raw

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()):  # noqa: ANN401
        # store.py escreve placeholders no estilo SQLite (?); o psycopg usa %s.
        return self._c.execute(sql.replace("?", "%s"), tuple(params))

    def commit(self) -> None:
        self._c.commit()

    def rollback(self) -> None:
        self._c.rollback()

    def close(self) -> None:
        self._c.close()

    def __enter__(self) -> "_PGConn":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        try:
            if exc_type is None:
                self._c.commit()
            else:
                self._c.rollback()
        finally:
            self._c.close()


def _connect_pg() -> _PGConn:
    import psycopg
    from psycopg.rows import dict_row

    raw = psycopg.connect(PG_URL, row_factory=dict_row, autocommit=False)
    # isola as tabelas do Betflow no schema dedicado
    raw.execute(f"SET search_path TO {_PG_SCHEMA_NAME}, public")
    raw.commit()
    return _PGConn(raw)


# ---------------------------------------------------------------------------
# conexao (fachada usada por store.py)
# ---------------------------------------------------------------------------
def get_conn(db_path: Path | str | None = None) -> Any:  # noqa: ANN401
    """Abre uma conexao no backend ativo.

    No SQLite usa ``db_path``; no Postgres o ``db_path`` e ignorado.
    """
    if BACKEND == "postgres":
        return _connect_pg()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


# ---------------------------------------------------------------------------
# DDL Postgres (equivalente ao schema SQLite de store.py)
# ---------------------------------------------------------------------------
# REAL -> DOUBLE PRECISION, AUTOINCREMENT -> BIGSERIAL, timestamps ficam TEXT
# (ISO-8601 UTC, comparavel/ordenavel lexicograficamente como no SQLite).
_PG_STATEMENTS = [
    f"CREATE SCHEMA IF NOT EXISTS {_PG_SCHEMA_NAME}",
    """CREATE TABLE IF NOT EXISTS users (
        id           BIGSERIAL PRIMARY KEY,
        email        TEXT UNIQUE,
        name         TEXT,
        created_at   TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS bankroll (
        user_id      BIGINT PRIMARY KEY,
        initial      DOUBLE PRECISION NOT NULL,
        created_at   TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS bets (
        id            BIGSERIAL PRIMARY KEY,
        user_id       BIGINT NOT NULL DEFAULT 1,
        created_at    TEXT NOT NULL,
        division      TEXT,
        sport_key     TEXT,
        event_id      TEXT,
        commence_time TEXT,
        home_team     TEXT NOT NULL,
        away_team     TEXT NOT NULL,
        market        TEXT NOT NULL,
        selection     TEXT NOT NULL,
        odd           DOUBLE PRECISION NOT NULL,
        stake         DOUBLE PRECISION NOT NULL,
        model_prob    DOUBLE PRECISION,
        ev            DOUBLE PRECISION,
        edge          DOUBLE PRECISION,
        status        TEXT NOT NULL DEFAULT 'PENDING',
        pnl           DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        auto_settled  INTEGER NOT NULL DEFAULT 0,
        settled_at    TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS suggestions_bankroll (
        user_id      BIGINT PRIMARY KEY,
        initial      DOUBLE PRECISION NOT NULL DEFAULT 1000.0,
        created_at   TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS suggestions (
        id            BIGSERIAL PRIMARY KEY,
        user_id       BIGINT NOT NULL DEFAULT 1,
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
        odd           DOUBLE PRECISION NOT NULL,
        model_prob    DOUBLE PRECISION,
        implied_prob  DOUBLE PRECISION,
        fair_prob     DOUBLE PRECISION,
        confidence    DOUBLE PRECISION,
        ev            DOUBLE PRECISION,
        edge          DOUBLE PRECISION,
        source        TEXT,
        stake_frac    DOUBLE PRECISION,
        home_score    INTEGER,
        away_score    INTEGER,
        status        TEXT NOT NULL DEFAULT 'PENDING',
        pnl           DOUBLE PRECISION NOT NULL DEFAULT 0.0,
        auto_settled  INTEGER NOT NULL DEFAULT 0,
        settled_at    TEXT,
        CONSTRAINT uq_suggestions_event UNIQUE (user_id, strategy, event_id, market)
    )""",
    """CREATE TABLE IF NOT EXISTS suggestions_scans (
        id           BIGSERIAL PRIMARY KEY,
        user_id      BIGINT NOT NULL DEFAULT 1,
        scanned_at   TEXT NOT NULL,
        bookmaker    TEXT,
        days         INTEGER,
        today_only   INTEGER,
        leagues      TEXT,
        n_suggestions INTEGER,
        quota_remaining INTEGER,
        notes        TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS matches (
        id            BIGSERIAL PRIMARY KEY,
        league_code   TEXT NOT NULL,
        event_id      TEXT,
        match_date    TEXT NOT NULL,
        season        TEXT,
        home_team     TEXT NOT NULL,
        away_team     TEXT NOT NULL,
        home_goals    INTEGER,
        away_goals    INTEGER,
        home_corners  INTEGER,
        away_corners  INTEGER,
        home_yellow   INTEGER,
        away_yellow   INTEGER,
        source        TEXT,
        updated_at    TEXT NOT NULL,
        CONSTRAINT uq_matches UNIQUE (league_code, home_team, away_team, match_date)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_matches_league ON matches(league_code)",
    "CREATE INDEX IF NOT EXISTS idx_matches_date   ON matches(match_date)",
    "CREATE INDEX IF NOT EXISTS idx_bets_user   ON bets(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status)",
    "CREATE INDEX IF NOT EXISTS idx_bets_event  ON bets(event_id)",
    "CREATE INDEX IF NOT EXISTS idx_suggestions_user   ON suggestions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status)",
    "CREATE INDEX IF NOT EXISTS idx_suggestions_event  ON suggestions(event_id)",
    "CREATE INDEX IF NOT EXISTS idx_suggestions_generated ON suggestions(generated_at)",
]


_PG_MIGRATIONS = [
    "ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS strategy "
    "TEXT NOT NULL DEFAULT 'main'",
    "ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS fair_prob DOUBLE PRECISION",
    "ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION",
    "ALTER TABLE suggestions DROP CONSTRAINT IF EXISTS uq_suggestions_event",
    "ALTER TABLE suggestions ADD CONSTRAINT uq_suggestions_event "
    "UNIQUE (user_id, strategy, event_id, market)",
    "CREATE INDEX IF NOT EXISTS idx_suggestions_strategy "
    "ON suggestions(strategy)",
]


def init_pg_schema() -> None:
    """Cria schema, tabelas e indices no Postgres (idempotente)."""
    import psycopg
    from psycopg.rows import dict_row

    raw = psycopg.connect(PG_URL, row_factory=dict_row, autocommit=True)
    try:
        raw.execute(f"CREATE SCHEMA IF NOT EXISTS {_PG_SCHEMA_NAME}")
        raw.execute(f"SET search_path TO {_PG_SCHEMA_NAME}, public")
        for stmt in _PG_STATEMENTS:
            raw.execute(stmt)
        # Migracao idempotente p/ bancos ja existentes: coluna 'strategy' e
        # troca da unicidade p/ incluir a estrategia (main x boosted no mesmo jogo).
        for mig in _PG_MIGRATIONS:
            try:
                raw.execute(mig)
            except Exception:  # noqa: BLE001 - migracao ja aplicada
                pass
    finally:
        raw.close()
