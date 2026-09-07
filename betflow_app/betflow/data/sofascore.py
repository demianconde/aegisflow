"""Cliente da SofaScore via RapidAPI (apidojo).

Fonte C do Betflow: dados ricos e ao vivo da SofaScore - agenda, detalhes de
partida, estatisticas (escanteios, cartoes, chutes), head-to-head e incidentes.
Complementa a Fonte A (football-data, treino) e a Fonte B (odds).

Autenticacao por headers RapidAPI:
    x-rapidapi-host: sofascore.p.rapidapi.com
    x-rapidapi-key:  <BETFLOW_RAPIDAPI_KEY>   (guardada apenas no .env)

Endpoints CONFIRMADOS (retornaram 200 em testes reais):
    GET /matches/detail?matchId=..          -> objeto {"event": {...}}
    GET /matches/get-statistics?matchId=..  -> {"statistics": [ {period, groups} ]}
    GET /matches/get-head2head?matchId=..   -> confrontos diretos (204 se ausente)
    GET /matches/get-h2h-events?custom-id=..-> eventos H2H por customId
    GET /matches/get-incidents?matchId=..   -> gols/cartoes/substituicoes

Agenda por data: o nome exato do endpoint varia; use `list_scheduled(path=...)`
passando o path do seu painel RapidAPI (ou configure SOFASCORE_SCHEDULE_PATH).
O metodo generico `get(path, params)` permite chamar QUALQUER endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from config import settings


class SofaScoreError(RuntimeError):
    """Erro de comunicacao com a SofaScore API."""


@dataclass
class SofaScoreClient:
    """Cliente fino sobre a SofaScore (apidojo) no RapidAPI."""

    api_key: str = field(default_factory=lambda: settings.RAPIDAPI_KEY)
    host: str = field(default_factory=lambda: settings.SOFASCORE_HOST)
    timeout: int = 30
    quota_remaining: int | None = None

    def __post_init__(self) -> None:
        if not self.api_key:
            raise SofaScoreError(
                "BETFLOW_RAPIDAPI_KEY nao configurada. Preencha o .env.")

    # ------------------------------------------------------------------
    # infra
    # ------------------------------------------------------------------
    @property
    def _headers(self) -> dict[str, str]:
        return {"x-rapidapi-host": self.host, "x-rapidapi-key": self.api_key}

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET generico. `path` iniciando com '/'. Retorna JSON ou None (204).

        Levanta SofaScoreError para 401/404/429/5xx. Atualiza `quota_remaining`.
        """
        if not path.startswith("/"):
            path = "/" + path
        url = f"https://{self.host}{path}"
        try:
            resp = requests.get(url, headers=self._headers, params=params,
                                timeout=self.timeout)
        except requests.RequestException as exc:
            raise SofaScoreError(f"falha de rede em {path}: {exc}") from exc

        rem = resp.headers.get("x-ratelimit-requests-remaining")
        if rem is not None:
            try:
                self.quota_remaining = int(rem)
            except ValueError:
                pass

        if resp.status_code == 204:
            return None
        if resp.status_code == 401:
            raise SofaScoreError("401 nao autorizado - verifique a RapidAPI key.")
        if resp.status_code == 429:
            raise SofaScoreError("429 cota excedida / rate limit.")
        if resp.status_code == 404:
            raise SofaScoreError(f"404 endpoint inexistente: {path}")
        if not resp.ok:
            raise SofaScoreError(f"{resp.status_code} em {path}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SofaScoreError(f"resposta nao-JSON em {path}") from exc


    # ------------------------------------------------------------------
    # endpoints confirmados
    # ------------------------------------------------------------------
    def match_detail(self, match_id: int | str) -> dict[str, Any]:
        """Detalhe de uma partida (times, placar, torneio, status, customId)."""
        data = self.get("/matches/detail", {"matchId": str(match_id)})
        return (data or {}).get("event", {})

    def match_statistics(self, match_id: int | str) -> list[dict[str, Any]]:
        """Estatisticas por periodo (ALL/1ST/2ND). Cada item tem `groups`."""
        data = self.get("/matches/get-statistics", {"matchId": str(match_id)})
        return (data or {}).get("statistics", [])

    def head2head(self, match_id: int | str) -> Any:
        """Confrontos diretos por matchId (pode ser None se indisponivel)."""
        return self.get("/matches/get-head2head", {"matchId": str(match_id)})

    def h2h_events(self, custom_id: str) -> Any:
        """Eventos H2H pelo customId da partida (do match_detail)."""
        return self.get("/matches/get-h2h-events", {"custom-id": custom_id})

    def match_incidents(self, match_id: int | str) -> Any:
        """Incidentes (gols, cartoes, substituicoes) de uma partida."""
        return self.get("/matches/get-incidents", {"matchId": str(match_id)})

    def h2h(self, custom_id: str) -> Any:
        """Resumo head-to-head (endpoint novo `matches/get-h2h`)."""
        return self.get("/matches/get-h2h", {"customId": custom_id})

    def all_odds(self, match_id: int | str) -> list[dict[str, Any]]:
        """Todos os mercados de odds de um jogo (fracionarias). Ver parse_odds."""
        data = self.get("/matches/get-all-odds", {"matchId": str(match_id)})
        return (data or {}).get("markets", [])

    # ------------------------------------------------------------------
    # agenda (por time) - a SofaScore nao lista por data pura
    # ------------------------------------------------------------------
    def next_matches(self, team_id: int | str, page: int = 0
                     ) -> list[dict[str, Any]]:
        """Proximos jogos de um time (agenda futura)."""
        data = self.get("/teams/get-next-matches",
                        {"teamId": str(team_id), "pageIndex": str(page)})
        return (data or {}).get("events", [])

    def last_matches(self, team_id: int | str, page: int = 0
                     ) -> list[dict[str, Any]]:
        """Ultimos jogos de um time (historico recente com placar)."""
        data = self.get("/teams/get-last-matches",
                        {"teamId": str(team_id), "pageIndex": str(page)})
        return (data or {}).get("events", [])

    def team_statistics(self, team_id: int | str, tournament_id: int | str,
                        season_id: int | str) -> Any:
        """Estatisticas agregadas do time numa temporada/torneio."""
        return self.get("/teams/get-statistics", {
            "teamId": str(team_id), "tournamentId": str(tournament_id),
            "seasonId": str(season_id)})

    # ------------------------------------------------------------------
    # torneios / ligas - agenda da liga inteira, tabela, temporadas
    # ------------------------------------------------------------------
    def tournament_seasons(self, tournament_id: int | str) -> list[dict[str, Any]]:
        """Temporadas de um torneio (id + year). A [0] e a mais recente."""
        data = self.get("/tournaments/get-seasons",
                        {"tournamentId": str(tournament_id)})
        return (data or {}).get("seasons", [])

    def tournament_next_matches(self, tournament_id: int | str,
                                season_id: int | str, page: int = 0
                                ) -> list[dict[str, Any]]:
        """Proximos jogos da LIGA inteira (agenda por rodada/data)."""
        data = self.get("/tournaments/get-next-matches", {
            "tournamentId": str(tournament_id), "seasonId": str(season_id),
            "pageIndex": str(page)})
        return (data or {}).get("events", [])

    def tournament_last_matches(self, tournament_id: int | str,
                                season_id: int | str, page: int = 0
                                ) -> list[dict[str, Any]]:
        """Ultimos jogos da liga (resultados recentes)."""
        data = self.get("/tournaments/get-last-matches", {
            "tournamentId": str(tournament_id), "seasonId": str(season_id),
            "pageIndex": str(page)})
        return (data or {}).get("events", [])

    def tournament_live_events(self, tournament_id: int | str,
                               season_id: int | str) -> list[dict[str, Any]]:
        """Jogos ao vivo da liga (pode ser vazio fora de horario de jogo)."""
        data = self.get("/tournaments/get-live-events", {
            "tournamentId": str(tournament_id), "seasonId": str(season_id)})
        return (data or {}).get("events", [])

    def tournament_standings(self, tournament_id: int | str,
                             season_id: int | str) -> list[dict[str, Any]]:
        """Tabela de classificacao (lista de linhas por time)."""
        data = self.get("/tournaments/get-standings", {
            "tournamentId": str(tournament_id), "seasonId": str(season_id)})
        standings = (data or {}).get("standings", [])
        return standings[0].get("rows", []) if standings else []


# ---------------------------------------------------------------------------
# parsing de estatisticas -> schema canonico do Betflow
# ---------------------------------------------------------------------------
# nomes de estatistica na SofaScore -> chave canonica (mandante/visitante)
_STAT_MAP = {
    "Corner kicks": "corners",
    "Corners": "corners",
    "Yellow cards": "yellow",
    "Red cards": "red",
    "Total shots": "shots",
    "Shots on target": "shots_target",
    "Ball possession": "possession",
    "Fouls": "fouls",
}


def extract_match_stats(statistics: list[dict[str, Any]],
                        period: str = "ALL") -> dict[str, dict[str, float]]:
    """Achata as estatisticas de um periodo no schema {stat: {home, away}}.

    Ex.: {"corners": {"home": 7.0, "away": 4.0},
          "yellow":  {"home": 2.0, "away": 3.0}, ...}
    Robusto a formatos: le home/away de cada statisticsItem (home/away ou
    homeValue/awayValue).
    """
    out: dict[str, dict[str, float]] = {}
    for per in statistics:
        if per.get("period") != period:
            continue
        for group in per.get("groups", []):
            for item in group.get("statisticsItems", []):
                name = item.get("name")
                key = _STAT_MAP.get(name)
                if not key:
                    continue
                home = _num(item.get("home", item.get("homeValue")))
                away = _num(item.get("away", item.get("awayValue")))
                if home is None and away is None:
                    continue
                out[key] = {"home": home or 0.0, "away": away or 0.0}
    return out


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    # SofaScore as vezes traz "45%" ou "7" como string
    s = str(v).replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# parsing de agenda (eventos) -> lista limpa
# ---------------------------------------------------------------------------
def parse_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Achata os eventos de next/last_matches num formato simples e estavel."""
    out: list[dict[str, Any]] = []
    for ev in events or []:
        ts = ev.get("startTimestamp")
        kickoff = (datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                   if isinstance(ts, (int, float)) else None)
        home = (ev.get("homeTeam") or {})
        away = (ev.get("awayTeam") or {})
        hs = (ev.get("homeScore") or {}).get("current")
        as_ = (ev.get("awayScore") or {}).get("current")
        out.append({
            "match_id": ev.get("id") or ev.get("detailId"),
            "custom_id": ev.get("customId"),
            "kickoff_utc": kickoff,
            "timestamp": ts,
            "tournament": (ev.get("tournament") or {}).get("name"),
            "home_team": home.get("name"),
            "away_team": away.get("name"),
            "home_id": home.get("id"),
            "away_id": away.get("id"),
            "home_score": hs,
            "away_score": as_,
            "status": (ev.get("status") or {}).get("description"),
        })
    return out


# ---------------------------------------------------------------------------
# parsing de odds (fracionarias -> decimais, mapeadas p/ mercados do Betflow)
# ---------------------------------------------------------------------------
def fractional_to_decimal(frac: str) -> float | None:
    """Converte odd fracionaria 'a/b' em decimal (a/b + 1). Ex.: '11/4' -> 3.75."""
    if not frac:
        return None
    try:
        if "/" in str(frac):
            num, den = str(frac).split("/")
            return round(float(num) / float(den) + 1.0, 4)
        return round(float(frac) + 1.0, 4)
    except (ValueError, ZeroDivisionError):
        return None


# nome do choice na SofaScore -> selecao canonica, por grupo de mercado
_CHOICE_MAP = {
    "1X2": {"1": "home", "X": "draw", "2": "away"},
    "Both teams to score": {"Yes": "yes", "No": "no"},
    "Double chance": {"1X": "1X", "X2": "X2", "12": "12"},
    "Draw no bet": {"1": "home", "2": "away"},
}


def parse_ou_markets(markets: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Extrai mercados Over/Under com LINHA (escanteios, cartoes, gols).

    A SofaScore guarda a linha em `choiceGroup` (ex.: '9.5') e os lados em
    `choices` (Over/Under). Retorna, por grupo canonico, um dict:
        {"corners": {"over9.5": 1.83, "under9.5": 1.95},
         "cards":   {"over4.5": 1.83, "under4.5": 1.95},
         "goals":   {"over2.5": 1.72, ...}}
    So mercados de tempo integral (Full-time).
    """
    group_map = {
        "Corners 2-Way": "corners",
        "Total Cards": "cards",
        "Match goals": "goals",
    }
    out: dict[str, dict[str, float]] = {}
    for m in markets or []:
        if m.get("marketPeriod") not in (None, "Full-time"):
            continue
        canon = group_map.get(m.get("marketGroup"))
        line = m.get("choiceGroup")
        if not canon or not line:
            continue
        bucket = out.setdefault(canon, {})
        for ch in m.get("choices", []):
            side = (ch.get("name") or "").lower()  # "over"/"under"
            if side not in ("over", "under"):
                continue
            dec = fractional_to_decimal(ch.get("fractionalValue"))
            if dec is not None:
                bucket[f"{side}{line}"] = dec
    return out


def parse_odds(markets: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Extrai odds DECIMAIS dos mercados de fechamento (Full-time).

    Retorna, por grupo mapeado, um dict selecao->odd. Ex.:
        {"1X2": {"home": 1.67, "draw": 3.75, "away": 5.25},
         "Both teams to score": {"yes": 1.75, "no": 2.0}}
    Ignora mercados de periodos parciais (1st half etc.).
    """
    out: dict[str, dict[str, float]] = {}
    for m in markets or []:
        group = m.get("marketGroup")
        period = m.get("marketPeriod")
        if period and period != "Full-time":
            continue
        mapping = _CHOICE_MAP.get(group)
        if not mapping:
            continue
        sel_odds: dict[str, float] = {}
        for ch in m.get("choices", []):
            sel = mapping.get(ch.get("name"))
            if not sel:
                continue
            dec = fractional_to_decimal(ch.get("fractionalValue"))
            if dec is not None:
                sel_odds[sel] = dec
        if sel_odds:
            out[group] = sel_odds
    return out


# ---------------------------------------------------------------------------
# parsing de tabela de classificacao (standings)
# ---------------------------------------------------------------------------
def parse_standings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Achata as linhas da tabela num formato simples e ordenado por posicao."""
    out: list[dict[str, Any]] = []
    for r in rows or []:
        team = r.get("team") or {}
        out.append({
            "position": r.get("position"),
            "team": team.get("name"),
            "team_id": team.get("id"),
            "played": r.get("matches"),
            "wins": r.get("wins"),
            "draws": r.get("draws"),
            "losses": r.get("losses"),
            "goals_for": r.get("scoresFor"),
            "goals_against": r.get("scoresAgainst"),
            "points": r.get("points"),
        })
    out.sort(key=lambda x: (x["position"] is None, x["position"]))
    return out


