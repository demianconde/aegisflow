"""Cliente da The Odds API - Fonte B (OPERACAO).

Fornece odds ao vivo/futuras de multiplas casas de aposta e placares recentes.
NAO fornece historico rico de resultados (por isso a Fonte A existe).

Endpoints usados no Ciclo 1:
    GET /v4/sports                      -> lista de esportes (GRATIS, nao gasta cota)
    GET /v4/sports/{sport}/odds         -> odds de eventos futuros (custa cota)
    GET /v4/sports/{sport}/scores       -> placares recentes/ao vivo

Controle de cota: cada resposta traz os headers
    x-requests-remaining, x-requests-used, x-requests-last
que este cliente le e expoe para evitar gasto acidental de creditos.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests

from config import settings


class OddsApiError(RuntimeError):
    """Erro de comunicacao com a The Odds API."""


@dataclass
class QuotaInfo:
    """Snapshot do uso de cota retornado nos headers da resposta."""
    remaining: int | None = None
    used: int | None = None
    last_cost: int | None = None

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> "QuotaInfo":
        def _int(key: str) -> int | None:
            val = headers.get(key)
            try:
                return int(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        return cls(
            remaining=_int("x-requests-remaining"),
            used=_int("x-requests-used"),
            last_cost=_int("x-requests-last"),
        )

    def __str__(self) -> str:
        return (f"cota: restantes={self.remaining} "
                f"usadas={self.used} custo_ultima={self.last_cost}")


@dataclass
class OddsApiClient:
    """Cliente fino sobre a The Odds API v4."""

    api_key: str = field(default_factory=lambda: settings.ODDS_API_KEY)
    host: str = settings.ODDS_API_HOST
    regions: str = settings.ODDS_API_REGIONS
    odds_format: str = settings.ODDS_API_FORMAT
    timeout: int = 30

    # ultimo snapshot de cota observado
    last_quota: QuotaInfo = field(default_factory=QuotaInfo)

    def __post_init__(self) -> None:
        if not self.api_key:
            raise OddsApiError(
                "BETFLOW_ODDS_API_KEY nao configurada. Preencha o arquivo .env."
            )

    # ------------------------------------------------------------------
    # infra
    # ------------------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        params["apiKey"] = self.api_key
        url = f"{self.host}{path}"
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise OddsApiError(f"falha de rede ao chamar {path}: {exc}") from exc

        # atualiza cota sempre que os headers estiverem presentes
        self.last_quota = QuotaInfo.from_headers(resp.headers)

        if resp.status_code == 401:
            raise OddsApiError("401 Nao autorizado - verifique a API key.")
        if resp.status_code == 429:
            raise OddsApiError("429 Cota excedida / rate limit atingido.")
        if not resp.ok:
            raise OddsApiError(f"{resp.status_code} ao chamar {path}: {resp.text[:200]}")

        return resp.json()

    # ------------------------------------------------------------------
    # endpoints
    # ------------------------------------------------------------------
    def list_sports(self, all_sports: bool = False) -> list[dict[str, Any]]:
        """Lista esportes em temporada. GRATIS - nao consome cota."""
        params = {"all": "true"} if all_sports else None
        return self._get("/v4/sports", params)

    def list_soccer_sports(self, all_sports: bool = True) -> list[dict[str, Any]]:
        """Filtra apenas os esportes do grupo Soccer."""
        return [s for s in self.list_sports(all_sports=all_sports)
                if s.get("group") == "Soccer"]

    def get_odds(self, sport_key: str, markets: str = "h2h",
                 regions: str | None = None) -> list[dict[str, Any]]:
        """Odds de eventos futuros. CONSOME COTA (10 x mercados x regioes)."""
        params = {
            "regions": regions or self.regions,
            "markets": markets,
            "oddsFormat": self.odds_format,
        }
        return self._get(f"/v4/sports/{sport_key}/odds", params)

    def get_scores(self, sport_key: str, days_from: int = 3) -> list[dict[str, Any]]:
        """Placares recentes (ate 3 dias) e jogos ao vivo.

        `daysFrom` custa 2 de cota; ao vivo/proximo sem daysFrom custa 1.
        """
        params: dict[str, Any] = {}
        if days_from:
            params["daysFrom"] = days_from
        return self._get(f"/v4/sports/{sport_key}/scores", params)


def extract_h2h_by_bookmaker(event: dict[str, Any], bookmaker: str
                             ) -> dict[str, float] | None:
    """Extrai as odds 1X2 (h2h) de UMA casa especifica de um evento.

    Casa o `bookmaker` por prefixo case-insensitive (ex.: "Betano" casa
    "Betano (UK)"). Retorna {"home":..,"draw":..,"away":..} com odds decimais,
    ou None se a casa nao cotou o jogo.
    """
    home_team = event.get("home_team")
    away_team = event.get("away_team")
    bkl = bookmaker.strip().lower()
    for bk in event.get("bookmakers", []):
        title = (bk.get("title") or "").lower()
        if not (title == bkl or title.startswith(bkl)):
            continue
        for market in bk.get("markets", []):
            if market.get("key") != "h2h":
                continue
            out: dict[str, float] = {}
            for oc in market.get("outcomes", []):
                name, price = oc.get("name"), oc.get("price")
                if name == home_team:
                    out["home"] = float(price)
                elif name == away_team:
                    out["away"] = float(price)
                elif name == "Draw":
                    out["draw"] = float(price)
            if {"home", "draw", "away"} <= out.keys():
                return out
    return None


def list_bookmakers(events: list[dict[str, Any]]) -> list[str]:
    """Todas as casas presentes em uma lista de eventos (para diagnostico/UI)."""
    names: set[str] = set()
    for ev in events:
        for bk in ev.get("bookmakers", []):
            if bk.get("title"):
                names.add(bk["title"])
    return sorted(names)
