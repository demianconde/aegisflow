-- ============================================================================
-- Boosted Research — Schema Supabase (PostgreSQL)
-- ============================================================================
-- Reaproveita o schema dedicado `betflow` do gateway (isolado do `public`).
-- Este arquivo documenta as tabelas usadas pela vertente e e idempotente:
-- pode ser reaplicado sem perder dados. Em producao, o proprio app cria/migra
-- via betflow/web/db.py::init_pg_schema(); este SQL e a referencia canonica.

CREATE SCHEMA IF NOT EXISTS betflow;
SET search_path TO betflow, public;

-- ---------------------------------------------------------------------------
-- Cache de partidas (Data Warehouse do modelo): placares historicos e, quando
-- houver fonte, metricas avancadas (xG/xT/PPDA) — colunas prontas para plugar.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS matches (
    id            BIGSERIAL PRIMARY KEY,
    league_code   TEXT NOT NULL,             -- E0, BSA, F1, I1, SP1
    event_id      TEXT,
    match_date    TEXT NOT NULL,             -- ISO-8601 UTC
    season        TEXT,
    home_team     TEXT NOT NULL,
    away_team     TEXT NOT NULL,
    home_goals    INTEGER,
    away_goals    INTEGER,
    home_corners  INTEGER,
    away_corners  INTEGER,
    home_yellow   INTEGER,
    away_yellow   INTEGER,
    -- metricas avancadas (NULL ate uma fonte de xG/PPDA ser plugada):
    home_xg       DOUBLE PRECISION,
    away_xg       DOUBLE PRECISION,
    home_ppda     DOUBLE PRECISION,
    away_ppda     DOUBLE PRECISION,
    source        TEXT,
    updated_at    TEXT NOT NULL,
    CONSTRAINT uq_matches UNIQUE (league_code, home_team, away_team, match_date)
);
CREATE INDEX IF NOT EXISTS idx_matches_league ON matches(league_code);
CREATE INDEX IF NOT EXISTS idx_matches_date   ON matches(match_date);

-- ---------------------------------------------------------------------------
-- Track record unificado. A coluna `strategy` separa as linhas de predicao:
--   'main'   -> motor principal (Dixon-Coles calibrado)
--   'boosted' -> Boosted Research (Elo bivariado + XGBoost)
-- Ambas avaliam os MESMOS jogos e liquidam pela MESMA rotina => comparaveis.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS suggestions (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL DEFAULT 1,
    generated_at  TEXT NOT NULL,
    division      TEXT NOT NULL,
    sport_key     TEXT,
    event_id      TEXT,
    commence_time TEXT,
    home_team     TEXT NOT NULL,
    away_team     TEXT NOT NULL,
    market        TEXT NOT NULL,             -- 1X2:H | 1X2:D | 1X2:A | AH:...
    selection     TEXT NOT NULL,
    side          TEXT,
    bookmaker     TEXT,                       -- Pinnacle (referencia da vertente)
    strategy      TEXT NOT NULL DEFAULT 'main',
    odd           DOUBLE PRECISION NOT NULL,  -- odd de entrada
    closing_odd   DOUBLE PRECISION,           -- linha de fechamento (p/ CLV)
    model_prob    DOUBLE PRECISION,
    implied_prob  DOUBLE PRECISION,
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
);
CREATE INDEX IF NOT EXISTS idx_suggestions_strategy ON suggestions(strategy);
CREATE INDEX IF NOT EXISTS idx_suggestions_status   ON suggestions(status);
CREATE INDEX IF NOT EXISTS idx_suggestions_event    ON suggestions(event_id);

-- Migracao para bancos ja existentes (idempotente):
ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS strategy TEXT NOT NULL DEFAULT 'main';
ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS closing_odd DOUBLE PRECISION;
