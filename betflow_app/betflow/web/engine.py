"""Motor de previsao para o servidor: carrega dados e treina modelos 1x.

Treinar Dixon-Coles a cada request seria lento. Aqui carregamos a temporada e
ajustamos os modelos uma unica vez (cache em memoria), reutilizando-os em todas
as consultas. `reload()` reconstroi tudo se quiser trocar de liga/temporada.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from betflow.data import football_data as fd
from betflow.data import extra_leagues as el
from betflow.data import sofascore as ss
from betflow.data import team_names
from betflow.data import odds_api
from betflow.models.dixon_coles import DixonColesModel
from betflow.models.counts import NegativeBinomialTotals, PoissonCards
from betflow.betting import value
from betflow.betting import calibration as calib
from betflow.web import store
from config import settings


@dataclass
class Engine:
    """Estado carregado do motor: dados + modelos treinados."""
    league: str = "E0"          # codigo em settings.LEAGUES
    division: str = "E0"
    season: str = "2425"
    has_stats: bool = True      # False p/ ligas extra (sem escanteios/cartoes)
    df: pd.DataFrame = field(default_factory=pd.DataFrame)
    dc: DixonColesModel | None = None
    corners: NegativeBinomialTotals | None = None
    cards: PoissonCards | None = None
    _ready: bool = False
    # SofaScore (agenda + odds ao vivo)
    _sofa: ss.SofaScoreClient | None = None
    _season_id: int | str | None = None
    # calibracao de probabilidades a partir de apostas liquidadas
    calibrators: dict[str, Any] | None = field(default=None, repr=False)
    calibration_method: str | None = "platt"
    calibration_min_samples: int = 20

    # ------------------------------------------------------------------
    def reload(self, league: str | None = None, division: str | None = None,
               season: str | None = None) -> "Engine":
        """(Re)carrega os dados e treina os modelos para a liga escolhida.

        Aceita `league` (codigo em settings.LEAGUES, ex.: 'E0', 'BSA'). Para
        compatibilidade, ainda aceita `division`/`season` das ligas europeias.
        """
        if league:
            self.league = league
        self.season = season or self.season
        cfg = settings.LEAGUES.get(self.league, settings.LEAGUES["E0"])
        self._season_id = None  # reseta cache de seasonId da SofaScore

        if cfg["source"] == "extra":
            # ligas extra: 1 CSV/pais, sem escanteios/cartoes
            self.division = self.league
            self.has_stats = False
            self.df = el.load(cfg["extra_code"], league=cfg.get("extra_league"))
            # treina nas ultimas temporadas (recencia > volume p/ elenco atual)
            if "season" in self.df.columns:
                seasons = sorted(self.df["season"].astype(str).unique())
                recent = set(seasons[-3:])
                self.df = self.df[self.df["season"].astype(str).isin(recent)]
        else:
            # ligas europeias: 1 CSV/temporada, com stats
            self.division = division or cfg.get("division", "E0")
            self.has_stats = True
            self.df = fd.load_season(self.division, self.season)

        if self.df.empty:
            raise RuntimeError(f"nenhum jogo para a liga {self.league}")

        self.dc = DixonColesModel(xi=0.0018).fit(self.df)
        if self.has_stats:
            self.corners = NegativeBinomialTotals().fit(self.df)
            self.cards = PoissonCards().fit(self.df)
        else:
            self.corners = None
            self.cards = None
        self._ready = True
        return self

    def ensure_ready(self) -> None:
        if not self._ready:
            self.reload()

    # ------------------------------------------------------------------
    @property
    def teams(self) -> list[str]:
        self.ensure_ready()
        return self.dc.teams if self.dc else []

    def referees(self) -> list[str]:
        self.ensure_ready()
        if "referee" not in self.df.columns:
            return []
        return sorted(self.df["referee"].dropna().unique().tolist())

    # ------------------------------------------------------------------
    def predict(self, home: str, away: str,
                referee: str | None = None) -> dict[str, Any]:
        """Todas as probabilidades do modelo para um confronto."""
        self.ensure_ready()
        assert self.dc is not None

        lh, la = self.dc.expected_goals(home, away)
        p1x2 = self.dc.predict_1x2(home, away)
        ou25 = self.dc.predict_over_under(home, away, 2.5)
        btts = self.dc.predict_btts(home, away)

        markets: dict[str, Any] = {
            "1X2": {k: round(v, 4) for k, v in p1x2.items()},
            "OU2.5": {k: round(v, 4) for k, v in ou25.items()},
            "BTTS": {k: round(v, 4) for k, v in btts.items()},
        }
        # escanteios/cartoes so quando a liga tem essas estatisticas
        if self.has_stats and self.corners and self.cards:
            markets["corners"] = {
                "expected_total": round(self.corners.expected_total(home, away), 2),
                "over9.5": round(self.corners.prob_over(9.5, home, away), 4),
                "over10.5": round(self.corners.prob_over(10.5, home, away), 4),
                "over11.5": round(self.corners.prob_over(11.5, home, away), 4),
            }
            markets["cards"] = {
                "expected_total": round(self.cards.expected_total(referee), 2),
                "over3.5": round(self.cards.prob_over(3.5, referee), 4),
                "over4.5": round(self.cards.prob_over(4.5, referee), 4),
                "over5.5": round(self.cards.prob_over(5.5, referee), 4),
            }

        return {
            "home_team": home,
            "away_team": away,
            "division": self.division,
            "has_stats": self.has_stats,
            "expected_goals": {"home": round(lh, 3), "away": round(la, 3),
                               "total": round(lh + la, 3)},
            "markets": markets,
        }

    def evaluate(self, market: str, prob: float, odd: float,
                 kelly: float | None = None,
                 min_edge: float | None = None) -> dict[str, Any]:
        """Avalia uma odd de mercado contra a probabilidade do modelo."""
        vb = value.evaluate_bet(
            market=market, selection=market, model_prob=prob, odd=odd,
            fair_prob=1.0 / odd,
            min_edge=settings.MIN_EDGE if min_edge is None else min_edge,
            kelly=settings.KELLY_FRACTION if kelly is None else kelly,
        )
        return {
            "model_prob": round(vb.model_prob, 4),
            "implied_prob": round(1.0 / odd, 4),
            "ev": round(vb.ev, 4),
            "edge": round(vb.edge, 4),
            "stake_fraction": round(vb.stake_fraction, 4),
            "is_value": vb.is_value,
        }

    # ------------------------------------------------------------------
    # SofaScore: agenda de jogos da liga + odds ao vivo
    # ------------------------------------------------------------------
    @property
    def sofascore_enabled(self) -> bool:
        return bool(settings.RAPIDAPI_KEY)

    def _sofa_client(self) -> ss.SofaScoreClient:
        if self._sofa is None:
            self._sofa = ss.SofaScoreClient()
        return self._sofa

    def _resolve_season(self) -> int | str:
        """Descobre (e cacheia) o seasonId atual da liga carregada."""
        if self._season_id is not None:
            return self._season_id
        tid = settings.SOFASCORE_TOURNAMENTS.get(self.division)
        if not tid:
            raise RuntimeError(f"divisao {self.division} sem tournamentId mapeado")
        seasons = self._sofa_client().tournament_seasons(tid)
        if not seasons:
            raise RuntimeError("SofaScore nao retornou temporadas")
        self._season_id = seasons[0]["id"]
        return self._season_id

    def upcoming_fixtures(self, limit: int = 20) -> list[dict[str, Any]]:
        """Proximos jogos da liga, com nomes mapeados p/ o modelo.

        Cada item traz: match_id, kickoff, times (SofaScore), times do modelo
        (home_model/away_model) e known=True/False se o modelo reconhece ambos.
        """
        self.ensure_ready()
        tid = settings.SOFASCORE_TOURNAMENTS.get(self.division)
        sid = self._resolve_season()
        raw = self._sofa_client().tournament_next_matches(tid, sid)
        games = ss.parse_events(raw)
        known = self.teams
        out: list[dict[str, Any]] = []
        for g in games[:limit]:
            hm = team_names.match_team(g["home_team"] or "", known)
            am = team_names.match_team(g["away_team"] or "", known)
            out.append({
                "match_id": g["match_id"],
                "kickoff_utc": g["kickoff_utc"],
                "tournament": g["tournament"],
                "home_team": g["home_team"],
                "away_team": g["away_team"],
                "home_model": hm,
                "away_model": am,
                "known": bool(hm and am),
            })
        return out

    def match_odds(self, match_id: int | str) -> dict[str, dict[str, float]]:
        """Odds decimais (fechamento) de um jogo, por mercado."""
        markets = self._sofa_client().all_odds(match_id)
        return ss.parse_odds(markets)

    # ------------------------------------------------------------------
    # VALUE BETS automaticas: The Odds API (odds por casa, ex.: Betano)
    # ------------------------------------------------------------------
    @property
    def odds_api_enabled(self) -> bool:
        return bool(settings.ODDS_API_KEY)

    def fit_calibration(self, method: str | None = None,
                        min_samples: int | None = None,
                        db_path: Path | str = store.DB_PATH) -> dict[str, Any]:
        """Treina calibradores one-vs-rest usando apostas liquidadas do tracker.

        A calibracao aprende o vies do modelo a partir do par
        (probabilidade prevista, resultado real) das apostas ja liquidadas.
        Mercados com poucas amostras ficam sem calibrador (identidade).
        """
        method = method or self.calibration_method or "platt"
        min_samples = (min_samples if min_samples is not None
                       else self.calibration_min_samples)
        samples = store.settled_1x2_calibration_samples(
            division=self.league, db_path=db_path)
        self.calibrators = calib.fit_1x2_calibrators(
            samples, method=method, min_samples=min_samples)
        return {
            "method": method,
            "min_samples": min_samples,
            "samples": {k: len(v) for k, v in samples.items()},
            "active": {k: k in self.calibrators for k in ("H", "D", "A")},
        }

    def scan_value_bets(self, bookmaker: str | None = None,
                        regions: str | None = None,
                        db_path: Path | str = store.DB_PATH) -> dict[str, Any]:
        """Varre os jogos futuros da liga, compara as odds de UMA casa
        (ex.: Betano) com o modelo e retorna SO as apostas de valor (EV>0,
        edge>=MIN_EDGE), explicando o porque de cada uma.

        Consome 1 credito da The Odds API por chamada.
        """
        self.ensure_ready()
        assert self.dc is not None
        bookmaker = bookmaker or settings.PREFERRED_BOOKMAKER
        cfg = settings.LEAGUES.get(self.league, {})
        sport_key = cfg.get("odds_api_key")
        if not sport_key:
            raise RuntimeError(f"liga {self.league} sem odds_api_key mapeada")

        # Tenta treinar calibradores com apostas ja liquidadas (somente na 1a
        # chamada, depois fica em cache ate o engine ser recarregado).
        if self.calibrators is None:
            self.fit_calibration(db_path=db_path)

        client = odds_api.OddsApiClient()
        events = client.get_odds(sport_key, markets="h2h",
                                 regions=regions or client.regions)

        known = self.teams
        bets: list[dict[str, Any]] = []
        skipped_unknown = 0
        scanned = 0
        for ev in events:
            hm = team_names.match_team(ev.get("home_team", ""), known)
            am = team_names.match_team(ev.get("away_team", ""), known)
            if not (hm and am):
                skipped_unknown += 1
                continue
            odds = odds_api.extract_h2h_by_bookmaker(ev, bookmaker)
            if not odds:
                continue
            scanned += 1
            p = self.dc.predict_1x2(hm, am)
            if self.calibrators:
                p = calib.calibrate_1x2(p, self.calibrators)
            legs = [
                ("1X2:H", f"{ev['home_team']} vencer", p["H"], odds["home"]),
                ("1X2:D", "Empate", p["D"], odds["draw"]),
                ("1X2:A", f"{ev['away_team']} vencer", p["A"], odds["away"]),
            ]
            for market, sel, prob, odd in legs:
                vb = value.evaluate_bet(
                    market=market, selection=sel, model_prob=prob, odd=odd,
                    fair_prob=1.0 / odd, min_edge=settings.MIN_EDGE,
                    kelly=settings.KELLY_FRACTION)
                if vb.stake_fraction > 0:   # so apostas de valor
                    bets.append({
                        "home_team": ev.get("home_team"),
                        "away_team": ev.get("away_team"),
                        "home_model": hm, "away_model": am,
                        "commence_time": ev.get("commence_time"),
                        "market": market, "selection": sel,
                        "bookmaker": bookmaker, "odd": round(odd, 2),
                        "model_prob": round(prob, 4),
                        "implied_prob": round(1.0 / odd, 4),
                        "ev": round(vb.ev, 4), "edge": round(vb.edge, 4),
                        "stake_fraction": round(vb.stake_fraction, 4),
                    })
        bets.sort(key=lambda b: b["ev"], reverse=True)
        cal_active = {k: k in (self.calibrators or {}) for k in ("H", "D", "A")}
        return {
            "bookmaker": bookmaker,
            "league": self.league,
            "events_total": len(events),
            "scanned": scanned,
            "skipped_unknown": skipped_unknown,
            "quota_remaining": client.last_quota.remaining,
            "value_bets": bets,
            "calibration": {
                "method": self.calibration_method,
                "active": cal_active,
            },
        }

    @property
    def quota_remaining(self) -> int | None:
        return self._sofa.quota_remaining if self._sofa else None


# instancia global reutilizada pelo app (carregamento preguicoso)
engine = Engine()
