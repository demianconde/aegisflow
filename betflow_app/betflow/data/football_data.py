"""Ingestao de dados historicos da football-data.co.uk.

Esta e a Fonte A (TREINO/BACKTEST). Fornece resultados reais de partidas com
estatisticas ricas: gols, escanteios, cartoes, chutes e odds de fechamento de
varias casas - exatamente o que os modelos estatisticos precisam.

Padrao de URL:
    https://www.football-data.co.uk/mmz4281/{season}/{division}.csv
    ex.: .../mmz4281/2425/E0.csv  (Premier League 2024/2025)

Chave das colunas (ver notes.txt da fonte):
    FTHG/FTAG = gols mandante/visitante | FTR = resultado (H/D/A)
    HC/AC     = escanteios mandante/visitante
    HY/AY/HR/AR = cartoes amarelos/vermelhos
    HS/AS/HST/AST = chutes / chutes no alvo
    B365C*/PSC*/AvgC* = odds de FECHAMENTO (closing) - as mais importantes p/ CLV
"""
from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests

from config import settings

# football-data.co.uk as vezes responde 503/403 a clientes sem User-Agent de
# navegador ou sob rajada de requests. Enviamos um UA e tentamos algumas vezes.
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept": "text/csv,application/octet-stream,*/*",
}


def _get_with_retry(url: str, timeout: int = 30, attempts: int = 4) -> requests.Response:
    """GET com User-Agent de browser e retry exponencial em 5xx/erros de rede."""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=timeout)
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp
            last_exc = requests.HTTPError(f"{resp.status_code} em {url}")
        except requests.RequestException as exc:
            last_exc = exc
        if i < attempts - 1:
            time.sleep(1.5 * (2 ** i))  # 1.5s, 3s, 6s
    assert last_exc is not None
    raise last_exc

# Schema canonico: nome_football_data -> nome_betflow
# Mantemos nomes claros e estaveis para o resto do pipeline nao depender da fonte.
_CORE_COLUMNS = {
    "Div": "division",
    "Date": "date",
    "Time": "time",
    "HomeTeam": "home_team",
    "AwayTeam": "away_team",
    "Referee": "referee",
    # resultado
    "FTHG": "home_goals",
    "FTAG": "away_goals",
    "FTR": "result",           # H / D / A
    "HTHG": "ht_home_goals",
    "HTAG": "ht_away_goals",
    "HTR": "ht_result",
    # estatisticas de jogo
    "HS": "home_shots",
    "AS": "away_shots",
    "HST": "home_shots_target",
    "AST": "away_shots_target",
    "HC": "home_corners",
    "AC": "away_corners",
    "HF": "home_fouls",
    "AF": "away_fouls",
    "HY": "home_yellow",
    "AY": "away_yellow",
    "HR": "home_red",
    "AR": "away_red",
    # odds de fechamento (closing) - preferenciais para EV/CLV
    "B365CH": "odds_home_close_b365",
    "B365CD": "odds_draw_close_b365",
    "B365CA": "odds_away_close_b365",
    "PSCH": "odds_home_close_pinnacle",
    "PSCD": "odds_draw_close_pinnacle",
    "PSCA": "odds_away_close_pinnacle",
    "AvgCH": "odds_home_close_avg",
    "AvgCD": "odds_draw_close_avg",
    "AvgCA": "odds_away_close_avg",
    "MaxCH": "odds_home_close_max",
    "MaxCD": "odds_draw_close_max",
    "MaxCA": "odds_away_close_max",
    # over/under 2.5 gols (fechamento)
    "AvgC>2.5": "odds_over25_close_avg",
    "AvgC<2.5": "odds_under25_close_avg",
    "MaxC>2.5": "odds_over25_close_max",
    "MaxC<2.5": "odds_under25_close_max",
    # ---- odds de ABERTURA (opening) - para medir CLV vs. fechamento ----
    # 1X2 abertura (media de mercado)
    "AvgH": "odds_home_open_avg",
    "AvgD": "odds_draw_open_avg",
    "AvgA": "odds_away_open_avg",
    # over/under 2.5 gols abertura
    "Avg>2.5": "odds_over25_open_avg",
    "Avg<2.5": "odds_under25_open_avg",
    # odds pre-jogo Bet365 (abertura, fallback)
    "B365H": "odds_home_b365",
    "B365D": "odds_draw_b365",
    "B365A": "odds_away_b365",
}

_NUMERIC_COLUMNS = [v for v in _CORE_COLUMNS.values() if v not in (
    "division", "date", "time", "home_team", "away_team",
    "referee", "result", "ht_result",
)]


def build_url(division: str, season: str) -> str:
    """Monta a URL do CSV. `season` no formato '2425'."""
    return f"{settings.FOOTBALL_DATA_BASE}/{season}/{division}.csv"


def download_csv(division: str, season: str, cache: bool = True) -> Path:
    """Baixa o CSV bruto para data/raw e retorna o caminho local.

    Se `cache=True` e o arquivo ja existir, reutiliza sem rebaixar.
    """
    dest = settings.RAW_DIR / f"{division}_{season}.csv"
    if cache and dest.exists() and dest.stat().st_size > 0:
        return dest

    url = build_url(division, season)
    resp = _get_with_retry(url, timeout=30)
    # football-data usa latin-1 em alguns arquivos antigos
    dest.write_bytes(resp.content)
    return dest


def _read_raw(path: Path) -> pd.DataFrame:
    """Le o CSV bruto tolerando encoding e linhas em branco no fim."""
    try:
        return pd.read_csv(path, encoding="utf-8", on_bad_lines="skip")
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="latin-1", on_bad_lines="skip")


def parse(path: Path) -> pd.DataFrame:
    """Le e padroniza um CSV bruto para o schema canonico do Betflow."""
    raw = _read_raw(path)

    # Mantem apenas colunas conhecidas que existem neste arquivo
    present = {src: dst for src, dst in _CORE_COLUMNS.items() if src in raw.columns}
    df = raw[list(present.keys())].rename(columns=present).copy()

    # Datas: football-data usa dd/mm/yy ou dd/mm/yyyy (varia por temporada).
    # Tenta os dois formatos explicitos e combina, evitando parsing ambiguo.
    if "date" in df.columns:
        d4 = pd.to_datetime(df["date"], format="%d/%m/%Y", errors="coerce")
        d2 = pd.to_datetime(df["date"], format="%d/%m/%y", errors="coerce")
        df["date"] = d4.fillna(d2)

    # Converte colunas numericas de forma segura
    for col in _NUMERIC_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Remove linhas sem partida valida (sem times ou sem resultado)
    df = df.dropna(subset=[c for c in ("home_team", "away_team") if c in df.columns])
    if "date" in df.columns:
        df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    return df


def load_season(division: str, season: str, cache: bool = True) -> pd.DataFrame:
    """Atalho: baixa (ou usa cache) e devolve o DataFrame padronizado."""
    path = download_csv(division, season, cache=cache)
    df = parse(path)
    df["season"] = season
    return df


def load_many(divisions: Iterable[str], seasons: Iterable[str],
              cache: bool = True) -> pd.DataFrame:
    """Carrega e concatena varias divisoes/temporadas num unico DataFrame."""
    frames: list[pd.DataFrame] = []
    for div in divisions:
        for season in seasons:
            try:
                frames.append(load_season(div, season, cache=cache))
            except requests.HTTPError as exc:
                # temporada/divisao inexistente -> apenas avisa e segue
                print(f"[aviso] falha ao baixar {div} {season}: {exc}")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
