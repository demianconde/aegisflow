"""Ingestao das 'extra leagues' da football-data.co.uk (Brasil, Argentina...).

Diferente das ligas europeias (um CSV por temporada, com escanteios/cartoes),
as extra leagues vem em UM unico CSV por pais, com TODAS as temporadas e um
schema mais enxuto:

    Country,League,Season,Date,Time,Home,Away,HG,AG,Res,
    PSCH,PSCD,PSCA,MaxCH,MaxCD,MaxCA,AvgCH,AvgCD,AvgCA,B365CH,B365CD,B365CA

Ou seja: resultado (gols + 1X2) e odds de FECHAMENTO, mas SEM escanteios,
cartoes ou chutes. Portanto, para essas ligas o Betflow modela apenas
1X2 / Over-Under / BTTS (Dixon-Coles). Escanteios/cartoes ficam indisponiveis.

URL padrao: https://www.football-data.co.uk/new/{code}.csv  (ex.: BRA.csv)
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from config import settings
from betflow.data.football_data import _get_with_retry

EXTRA_BASE = "https://www.football-data.co.uk/new"

# schema do CSV extra -> schema canonico do Betflow
_EXTRA_COLUMNS = {
    "Country": "country",
    "League": "league",
    "Season": "season",
    "Date": "date",
    "Time": "time",
    "Home": "home_team",
    "Away": "away_team",
    "HG": "home_goals",
    "AG": "away_goals",
    "Res": "result",
    # odds de fechamento
    "PSCH": "odds_home_close_pinnacle",
    "PSCD": "odds_draw_close_pinnacle",
    "PSCA": "odds_away_close_pinnacle",
    "AvgCH": "odds_home_close_avg",
    "AvgCD": "odds_draw_close_avg",
    "AvgCA": "odds_away_close_avg",
    "MaxCH": "odds_home_close_max",
    "MaxCD": "odds_draw_close_max",
    "MaxCA": "odds_away_close_max",
    "B365CH": "odds_home_b365",
    "B365CD": "odds_draw_b365",
    "B365CA": "odds_away_b365",
}

_NUMERIC = [
    "home_goals", "away_goals",
    "odds_home_close_pinnacle", "odds_draw_close_pinnacle", "odds_away_close_pinnacle",
    "odds_home_close_avg", "odds_draw_close_avg", "odds_away_close_avg",
    "odds_home_close_max", "odds_draw_close_max", "odds_away_close_max",
    "odds_home_b365", "odds_draw_b365", "odds_away_b365",
]


def build_url(code: str) -> str:
    """URL do CSV da liga extra. `code` ex.: 'BRA' (Brasil), 'ARG' (Argentina)."""
    return f"{EXTRA_BASE}/{code}.csv"


def download_csv(code: str, cache: bool = True) -> Path:
    """Baixa (ou reusa cache) o CSV da liga extra para data/raw."""
    dest = settings.RAW_DIR / f"extra_{code}.csv"
    if cache and dest.exists() and dest.stat().st_size > 0:
        return dest
    resp = _get_with_retry(build_url(code), timeout=60)
    dest.write_bytes(resp.content)
    return dest


def parse(path: Path, league: str | None = None,
          season: str | int | None = None) -> pd.DataFrame:
    """Le e padroniza o CSV extra. Filtra por `league` e/ou `season` se dados.

    `league` ex.: 'Serie A'. `season` ex.: 2025 (o ano no arquivo).
    """
    try:
        raw = pd.read_csv(path, encoding="utf-8-sig", on_bad_lines="skip")
    except UnicodeDecodeError:
        raw = pd.read_csv(path, encoding="latin-1", on_bad_lines="skip")

    present = {s: d for s, d in _EXTRA_COLUMNS.items() if s in raw.columns}
    df = raw[list(present.keys())].rename(columns=present).copy()

    if "date" in df.columns:
        d4 = pd.to_datetime(df["date"], format="%d/%m/%Y", errors="coerce")
        d2 = pd.to_datetime(df["date"], format="%d/%m/%y", errors="coerce")
        df["date"] = d4.fillna(d2)

    for col in _NUMERIC:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if league and "league" in df.columns:
        df = df[df["league"].str.strip().str.lower() == league.strip().lower()]
    if season is not None and "season" in df.columns:
        df = df[df["season"].astype(str).str.strip() == str(season).strip()]

    df = df.dropna(subset=[c for c in ("home_team", "away_team") if c in df.columns])
    if "date" in df.columns:
        df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    return df


def load(code: str, league: str | None = None, season: str | int | None = None,
         cache: bool = True) -> pd.DataFrame:
    """Atalho: baixa (ou cache) e devolve o DataFrame padronizado e filtrado."""
    path = download_csv(code, cache=cache)
    return parse(path, league=league, season=season)


def available_seasons(code: str, league: str | None = None,
                      cache: bool = True) -> list[str]:
    """Lista as temporadas presentes no arquivo (para a UI escolher)."""
    df = load(code, league=league, cache=cache)
    if "season" not in df.columns or df.empty:
        return []
    return sorted(df["season"].astype(str).unique().tolist())
