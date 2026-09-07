"""Configuracoes centrais do Betflow.

Carrega variaveis de ambiente do .env e define constantes de ligas, fontes de
dados e parametros de operacao. Mantenha segredos apenas no .env.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Caminhos base
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("BETFLOW_DATA_DIR", str(PROJECT_ROOT / "data")))
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"

for _d in (DATA_DIR, RAW_DIR, PROCESSED_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Carrega o .env da raiz do projeto (se existir)
load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# The Odds API
# ---------------------------------------------------------------------------
ODDS_API_KEY = os.getenv("BETFLOW_ODDS_API_KEY", "").strip()
ODDS_API_HOST = "https://api.the-odds-api.com"
ODDS_API_REGIONS = os.getenv("BETFLOW_ODDS_REGIONS", "eu,uk").strip()
ODDS_API_FORMAT = os.getenv("BETFLOW_ODDS_FORMAT", "decimal").strip()

# ---------------------------------------------------------------------------
# SofaScore via RapidAPI
# ---------------------------------------------------------------------------
RAPIDAPI_KEY = os.getenv("BETFLOW_RAPIDAPI_KEY", "").strip()
SOFASCORE_HOST = os.getenv("BETFLOW_SOFASCORE_HOST", "sofascore.p.rapidapi.com").strip()

# Mapa: codigo football-data -> SofaScore uniqueTournament id (estaveis).
# Usado para puxar agenda/tabela da liga inteira via /tournaments/*.
SOFASCORE_TOURNAMENTS = {
    "E0": 17,     # Premier League (Inglaterra)
    "E1": 18,     # Championship (Inglaterra)
    "SP1": 8,     # La Liga (Espanha)
    "I1": 23,     # Serie A (Italia)
    "D1": 35,     # Bundesliga (Alemanha)
    "F1": 34,     # Ligue 1 (Franca)
    "BSA": 325,   # Brasileirao Serie A
}

# Registro unificado de ligas. Cada liga aponta como carregar dados de treino
# (europeia = 1 CSV/temporada; extra = 1 CSV/pais) e as chaves das APIs.
#   source: "europe" | "extra"
#   odds_api_key: sport key da The Odds API (odds por casa, inclui Betano)
LEAGUES = {
    "E0": {
        "name": "Premier League (Inglaterra)",
        "source": "europe", "division": "E0",
        "odds_api_key": "soccer_epl", "sofascore": 17,
    },
    "BSA": {
        "name": "Brasileirao Serie A (Brasil)",
        "source": "extra", "extra_code": "BRA", "extra_league": "Serie A",
        "odds_api_key": "soccer_brazil_campeonato", "sofascore": 325,
    },
    "ARG": {
        "name": "Primera Division (Argentina)",
        "source": "extra", "extra_code": "ARG", "extra_league": None,
        "odds_api_key": "soccer_argentina_primera_division", "sofascore": None,
    },
    "F1": {
        "name": "Ligue 1 (Franca)",
        "source": "europe", "division": "F1",
        "odds_api_key": "soccer_france_ligue_one", "sofascore": 34,
    },
    "I1": {
        "name": "Serie A (Italia)",
        "source": "europe", "division": "I1",
        "odds_api_key": "soccer_italy_serie_a", "sofascore": 23,
    },
    "SP1": {
        "name": "La Liga (Espanha)",
        "source": "europe", "division": "SP1",
        "odds_api_key": "soccer_spain_la_liga", "sofascore": 8,
    },
    "UCL": {
        "name": "UEFA Champions League",
        "source": "fallback",
        "odds_api_key": "soccer_uefa_champs_league", "sofascore": 7,
    },
}

# Competicoes-alvo do relatorio "apostas prontas" (ordem de exibicao).
# Restringe as chamadas da The Odds API a ESTAS ligas (economia de cota).
# So ligas onde a Betano (unica casa que operamos, autorizada no Brasil) cota.
# ARG e UCL ficam de fora: a Betano nao oferece esses campeonatos no feed.
TARGET_LEAGUES = ["BSA", "E0", "F1", "I1", "SP1"]

# Deep-link de melhor esforco para a Betano (nao ha link publico por jogo):
# leva a busca da casa pelo confronto. {q} = "Time A Time B" url-encoded.
BETANO_SEARCH_URL = os.getenv(
    "BETFLOW_BETANO_SEARCH_URL",
    "https://www.betano.bet.br/search/?query={q}",
).strip()

# Casa de aposta preferida ao destacar value bets (titulo na The Odds API).
# "Betano (UK)" e o rotulo que a API retorna na regiao eu/uk.
PREFERRED_BOOKMAKER = os.getenv("BETFLOW_PREFERRED_BOOKMAKER", "Betano").strip()

# ---------------------------------------------------------------------------
# football-data.co.uk
# ---------------------------------------------------------------------------
# Padrao de URL: https://www.football-data.co.uk/mmz4281/{season}/{division}.csv
# season no formato "2425" (2024/2025). division: E0=Premier League, E1=Championship...
FOOTBALL_DATA_BASE = "https://www.football-data.co.uk/mmz4281"

# Divisoes que nos interessam (codigo football-data -> descricao)
DIVISIONS = {
    "E0": "Premier League (Inglaterra)",
    "E1": "Championship (Inglaterra)",
    "SP1": "La Liga (Espanha)",
    "I1": "Serie A (Italia)",
    "D1": "Bundesliga (Alemanha)",
    "F1": "Ligue 1 (Franca)",
}

# The Odds API sport keys equivalentes (para a fase de operacao)
ODDS_API_SOCCER_KEYS = {
    "E0": "soccer_epl",
    "SP1": "soccer_spain_la_liga",
    "I1": "soccer_italy_serie_a",
    "D1": "soccer_germany_bundesliga",
    "F1": "soccer_france_ligue_one",
}

# ---------------------------------------------------------------------------
# Parametros de operacao (value / Kelly / gestao de banca)
# ---------------------------------------------------------------------------
MIN_EDGE = 0.03          # aposta so quando EV > 3% (limiar ABSOLUTO base)
KELLY_FRACTION = 0.25    # 1/4 Kelly (gestao conservadora de banca)


def _envf(name: str, default: float) -> float:
    """Le um float de ambiente com fallback silencioso."""
    try:
        return float(os.getenv(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Politica de staking — MOTOR PRINCIPAL (Dixon-Coles)
# ---------------------------------------------------------------------------
# Estes parametros sao SEPARADOS dos da Boosted (metodologias independentes).
# Ver betflow/betting/staking.py e PLANO_MELHORIA_MODELOS.md (Fases 1 e 2).
MAIN_MIN_EDGE = _envf("BETFLOW_MAIN_MIN_EDGE", 0.03)      # edge absoluto minimo
MAIN_REL_EDGE = _envf("BETFLOW_MAIN_REL_EDGE", 0.10)      # discordancia relativa
MAIN_MAX_ODD = _envf("BETFLOW_MAIN_MAX_ODD", 6.0)         # teto de cotacao
MAIN_LAMBDA = _envf("BETFLOW_MAIN_LAMBDA", 0.35)          # ancoragem ao mercado
MAIN_MAX_REL_DISAGREE = _envf("BETFLOW_MAIN_MAX_REL_DISAGREE", 0.60)  # sanidade

# Dixon-Coles interno (Fase 3): decaimento temporal + regularizacao ridge (L2).
DC_XI = _envf("BETFLOW_DC_XI", 0.0018)
DC_RIDGE = _envf("BETFLOW_DC_RIDGE", 0.05)   # encolhe forcas p/ media da liga

# ---------------------------------------------------------------------------
# Politica de staking — BOOSTED RESEARCH (Elo + XGBoost)
# ---------------------------------------------------------------------------
BOOSTED_MIN_EDGE = _envf("BOOSTED_MIN_EDGE", 0.04)
BOOSTED_REL_EDGE = _envf("BOOSTED_REL_EDGE", 0.15)      # selecao tight mantida
BOOSTED_MAX_ODD = _envf("BOOSTED_MAX_ODD", 4.0)         # teto de cotacao mantido
BOOSTED_LAMBDA = _envf("BOOSTED_LAMBDA", 0.20)          # modelo mais confiavel
BOOSTED_MAX_REL_DISAGREE = _envf("BOOSTED_MAX_REL_DISAGREE", 0.80)  # sanidade mantida
# Dimensionamento proprio da Boosted (mais agressivo, ainda responsavel): como e o
# modelo mais afiado, opera com Kelly e teto por entrada um pouco maiores que o
# principal. A SELECAO segue igual (mesmas portas de qualidade/sanidade) — o que
# muda e so o TAMANHO das entradas boas, nao o criterio de aceita-las.
BOOSTED_KELLY = _envf("BOOSTED_KELLY", 0.30)            # vs 0.25 do principal
BOOSTED_STAKE_CAP = _envf("BOOSTED_STAKE_CAP", 0.035)   # vs 0.02 do principal

# ---------------------------------------------------------------------------
# Gestao de banca (comum aos dois motores)
# ---------------------------------------------------------------------------
STAKE_CAP = _envf("BETFLOW_STAKE_CAP", 0.02)             # teto por entrada (2%)
CONF_MIN_GAMES = _envf("BETFLOW_CONF_MIN_GAMES", 8)      # abaixo => confianca baixa
CONF_FULL_GAMES = _envf("BETFLOW_CONF_FULL_GAMES", 30)   # a partir daqui, cheia
CONF_FLOOR = _envf("BETFLOW_CONF_FLOOR", 0.25)           # confianca minima (>0)


def season_code(start_year: int) -> str:
    """Converte o ano inicial da temporada no codigo football-data.

    Ex.: 2024 -> "2425" (temporada 2024/2025).
    """
    end = (start_year + 1) % 100
    return f"{start_year % 100:02d}{end:02d}"
