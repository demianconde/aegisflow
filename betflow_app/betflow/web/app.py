"""Aplicacao Flask do Betflow (factory + rotas).

Paginas (HTML):
    /                 dashboard: banca, ROI/yield, apostas abertas/liquidadas
    /predict          formulario de confronto -> probabilidades do modelo
    /bets             historico completo de apostas

Acoes (POST, redirecionam de volta):
    /bankroll         define banca inicial
    /bet/add          registra uma aposta
    /bet/<id>/settle  liquida (WON/LOST/VOID)
    /bet/<id>/delete  remove

API (JSON):
    GET /api/teams
    GET /api/predict?home=..&away=..&referee=..
    GET /api/value?prob=..&odd=..&market=..
    GET /api/stats
"""
from __future__ import annotations

import time
from typing import Any

from flask import (Flask, abort, jsonify, redirect, render_template,
                   request, url_for)

from betflow.web import store
from betflow.web import scheduler
from betflow.web import scores
from betflow.web.engine import engine
from betflow.betting import suggest
from betflow.data import odds_api
from config import settings

# cache simples em memória para a lista leve de fixtures (jogos futuros).
# evita gastar 7 créditos da Odds API a cada refresh da página de sugestões.
_FIXTURES_CACHE: dict[str, Any] = {}
_FIXTURES_TTL_SECONDS = 300  # 5 minutos


def _settle_suggestions_auto(user_id: int = store.DEFAULT_USER_ID,
                             days_from: int = 3) -> dict[str, Any]:
    """Liquida automaticamente as sugestoes pendentes cujos jogos ja terminaram."""
    from betflow.data import odds_api
    pending = store.list_pending_suggestions(user_id=user_id)
    if not pending:
        return {"checked": 0, "settled": 0, "won": 0, "lost": 0,
                "skipped": 0, "quota_remaining": None}

    by_sport: dict[str, list[dict]] = {}
    for s in pending:
        by_sport.setdefault(s.get("sport_key") or "", []).append(s)

    client = odds_api.OddsApiClient()
    settled = won = lost = skipped = 0
    quota = None
    for sport_key, group in by_sport.items():
        if not sport_key:
            skipped += len(group)
            continue
        try:
            events = client.get_scores(sport_key, days_from=days_from)
        except odds_api.OddsApiError:
            skipped += len(group)
            continue
        quota = client.last_quota.remaining
        finished = scores._scores_index(events)
        for s in group:
            info = finished.get(s.get("event_id"))
            if not info:
                skipped += 1
                continue
            if not s["market"].startswith("1X2:"):
                skipped += 1
                continue
            result = scores._resolve_1x2(s["market"], info["hs"], info["as"])
            store.settle_suggestion(s["id"], result,
                                    home_score=info.get("hs"),
                                    away_score=info.get("as"), auto=True)
            settled += 1
            won += result == "WON"
            lost += result == "LOST"

    return {"checked": len(pending), "settled": settled, "won": won,
            "lost": lost, "skipped": skipped, "quota_remaining": quota}


def _fmt_kickoff(iso: str) -> str:
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%d/%m %H:%M")
    except (ValueError, AttributeError):
        return iso


def _upcoming_fixtures_for_page(leagues: list[str], days: int = 7,
                                today_only: bool = False,
                                limit_per_league: int = 20) -> dict[str, Any]:
    """Carrega os proximos jogos das ligas-alvo de forma leve (sem treinar modelos).

    Consome ~1 credito da The Odds API por liga. Nao treina modelos nem
    calcula EV, entao e rapido para popular a pagina inicial de sugestoes.

    Resultado e cacheado em memoria por 5 minutos para evitar gastar cota
    a cada refresh da pagina.
    """
    from datetime import datetime, timezone, timedelta as _td
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + _td(days=days if not today_only else 1)
    if today_only:
        end = end.replace(hour=23, minute=59, second=59)

    cache_key = f"{','.join(sorted(leagues))}:{days}:{int(today_only)}"
    cached = _FIXTURES_CACHE.get(cache_key)
    if cached and (time.time() - cached["ts"]) < _FIXTURES_TTL_SECONDS:
        return cached["data"]

    client = odds_api.OddsApiClient()
    events_all: list[dict[str, Any]] = []
    errors: list[str] = []
    league_map: dict[str, dict] = {}
    for code in leagues:
        cfg = settings.LEAGUES.get(code)
        if not cfg or not cfg.get("odds_api_key"):
            continue
        try:
            events = client.get_odds(cfg["odds_api_key"], markets="h2h",
                                     regions=settings.ODDS_API_REGIONS)
            league_map[code] = cfg
        except odds_api.OddsApiError as exc:
            errors.append(f"{code}: {exc}")
            continue
        for ev in events:
            ct = ev.get("commence_time", "")
            try:
                dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if today_only:
                if dt.date() != now.date():
                    continue
            elif dt < start or dt > end:
                continue
            events_all.append({
                "league": cfg["name"],
                "league_code": code,
                "sport_key": cfg["odds_api_key"],
                "event_id": ev.get("id"),
                "commence_time": ct,
                "commence_time_fmt": _fmt_kickoff(ct),
                "home": ev.get("home_team"),
                "away": ev.get("away_team"),
                "odds": odds_api.extract_h2h_by_bookmaker(
                    ev, settings.PREFERRED_BOOKMAKER),
            })

    events_all.sort(key=lambda x: x["commence_time"])
    result = {
        "suggestions": events_all,
        "leagues": {code: {"events": 0, "value": 0} for code in league_map},
        "errors": errors,
        "quota_remaining": client.last_quota.remaining,
    }
    _FIXTURES_CACHE[cache_key] = {"ts": time.time(), "data": result}
    return result


def create_app() -> Flask:
    app = Flask(__name__)
    store.init_db()
    scheduler.start()  # operacao continua 24x7 (no-op sem chave/desativada)

    # ------------------------------------------------------------------
    # paginas
    # ------------------------------------------------------------------
    @app.route("/")
    def dashboard():
        ref_bankroll = store.get_suggestions_initial_bankroll()
        recs = store.list_open_recommendations(limit=10)
        for r in recs:
            r["stake"] = (r.get("stake_frac") or 0.0) * ref_bankroll
            r["potential"] = r["stake"] * (r["odd"] - 1.0)
            r["kickoff_fmt"] = _fmt_kickoff(r.get("commence_time") or "")
        return render_template(
            "dashboard.html",
            stats=store.stats(),
            pending=store.list_bets(status="PENDING"),
            division=engine.division,
            season=engine.season,
            recommendations=recs,
            sugg_stats=store.suggestions_stats(),
            ref_bankroll=ref_bankroll,
            ops=scheduler.status(),
        )

    @app.route("/predict")
    def predict_page():
        engine.ensure_ready()
        home = request.args.get("home", "")
        away = request.args.get("away", "")
        referee = request.args.get("referee") or None
        match_id = request.args.get("match_id") or None
        prediction = None
        error = None
        market_odds: dict = {}
        if home and away:
            if home == away:
                error = "Escolha times diferentes."
            else:
                try:
                    prediction = engine.predict(home, away, referee)
                except KeyError as exc:
                    error = f"Time desconhecido: {exc}"
        # se veio de um jogo da agenda, tenta puxar as odds reais da SofaScore
        if prediction and match_id and engine.sofascore_enabled:
            try:
                market_odds = engine.match_odds(match_id)
            except Exception as exc:
                error = (error or "") + f" (odds indisponiveis: {exc})"
        return render_template(
            "predict.html",
            teams=engine.teams,
            referees=engine.referees(),
            home=home, away=away, referee=referee or "",
            prediction=prediction, error=error,
            market_odds=market_odds, match_id=match_id or "",
            kelly=settings.KELLY_FRACTION, min_edge=settings.MIN_EDGE,
        )

    @app.route("/bets")
    def bets_page():
        return render_template("bets.html", bets=store.list_bets(),
                               stats=store.stats())

    @app.route("/theory")
    def theory_page():
        """Página explicativa sobre a teoria de value betting do Betflow."""
        return render_template("theory.html")

    @app.route("/value")
    def value_page():
        """Value bets automaticas: compara odds de uma casa (Betano) c/ o modelo."""
        engine.ensure_ready()
        result, error = None, None
        bookmaker = request.args.get("bookmaker") or settings.PREFERRED_BOOKMAKER
        if not engine.odds_api_enabled:
            error = ("The Odds API desativada: defina BETFLOW_ODDS_API_KEY no "
                     ".env para varrer odds por casa (Betano).")
        elif request.args.get("scan"):  # so consome cota quando pedido
            try:
                result = engine.scan_value_bets(bookmaker=bookmaker)
            except Exception as exc:
                error = f"Falha ao varrer odds: {exc}"
        return render_template(
            "value.html", result=result, error=error, bookmaker=bookmaker,
            league=engine.league, league_name=_league_name(engine.league),
            leagues=settings.LEAGUES, kelly=settings.KELLY_FRACTION,
            min_edge=settings.MIN_EDGE, has_stats=engine.has_stats)

    @app.route("/league", methods=["POST"])
    def switch_league():
        code = request.form.get("league", "E0")
        if code in settings.LEAGUES:
            try:
                engine.reload(league=code)
            except Exception as exc:
                abort(502, f"falha ao carregar liga {code}: {exc}")
        return redirect(request.referrer or url_for("value_page"))

    @app.route("/matches")
    def matches_page():
        """Agenda ao vivo (SofaScore): proximos jogos da liga carregada."""
        engine.ensure_ready()
        fixtures, error = [], None
        if not engine.sofascore_enabled:
            error = ("SofaScore desativada: defina BETFLOW_RAPIDAPI_KEY no .env "
                     "para ver os jogos e odds ao vivo.")
        else:
            try:
                fixtures = engine.upcoming_fixtures(limit=20)
            except Exception as exc:  # rede/cota/mapa -> mostra msg amigavel
                error = f"Nao foi possivel buscar a agenda: {exc}"
        return render_template("matches.html", fixtures=fixtures, error=error,
                               division=engine.division,
                               quota=engine.quota_remaining)

    @app.route("/suggestions")
    def suggestions_page():
        """Sugestoes de apostas futuras (odds da Betano via Odds API)."""
        result, error, saved_count = None, None, 0
        show_suggestions = request.args.get("scan") == "1"
        days = int(request.args.get("days", 7) or 7)
        today_only = request.args.get("today") == "1"
        selected_leagues = [
            code for code in request.args.getlist("league")
            if code in settings.TARGET_LEAGUES
        ]
        if not selected_leagues:
            selected_leagues = list(settings.TARGET_LEAGUES)

        if not engine.odds_api_enabled:
            error = ("The Odds API desativada: defina BETFLOW_ODDS_API_KEY no "
                     ".env para gerar sugestoes.")
        elif show_suggestions:
            try:
                result = suggest.suggest(days=days, today_only=today_only,
                                         leagues=selected_leagues,
                                         calibration="platt",
                                         calibration_min_samples=20)
                # Persiste as sugestoes no track record (desempenho historico)
                saved_count = store.save_suggestions(
                    result.suggestions, bookmaker=settings.PREFERRED_BOOKMAKER,
                    days=days, today_only=today_only,
                    leagues=selected_leagues,
                    quota_remaining=result.quota_remaining)
            except Exception as exc:  # noqa: BLE001
                error = f"Falha ao gerar sugestoes: {exc}"
        else:
            # Modo leve: mostra os proximos jogos sem treinar modelos.
            try:
                result = _upcoming_fixtures_for_page(
                    leagues=selected_leagues,
                    days=days, today_only=today_only)
            except Exception as exc:  # noqa: BLE001
                error = f"Falha ao carregar jogos: {exc}"
        return render_template(
            "suggestions.html", result=result, error=error,
            did_scan=show_suggestions, saved_count=saved_count,
            days=days, today_only=today_only, stats=store.stats(),
            bookmaker=settings.PREFERRED_BOOKMAKER,
            min_edge=settings.MIN_EDGE, kelly=settings.KELLY_FRACTION,
            selected_leagues=selected_leagues,
            leagues=settings.TARGET_LEAGUES, league_names=settings.LEAGUES)

    # ------------------------------------------------------------------
    # acoes
    # ------------------------------------------------------------------
    @app.route("/history")
    def history_page():
        """Desempenho historico das sugestoes do modelo (track record)."""
        league_filter = request.args.get("league", "").strip().upper()
        status_filter = request.args.get("status", "").strip().upper()
        stats = store.suggestions_stats(
            division=league_filter if league_filter else None)
        suggestions = store.list_suggestions(
            division=league_filter if league_filter else None,
            status=status_filter if status_filter else None)
        by_league = store.suggestions_stats_by_league()
        return render_template("history.html", stats=stats,
                               suggestions=suggestions, by_league=by_league,
                               league_filter=league_filter,
                               status_filter=status_filter,
                               league_names=settings.LEAGUES,
                               bookmaker=settings.PREFERRED_BOOKMAKER)

    @app.route("/settle-suggestions", methods=["POST"])
    def settle_suggestions():
        """Liquida automaticamente as sugestoes pendentes com placar final."""
        try:
            summary = _settle_suggestions_auto()
            msg = (f"Track record atualizado: {summary['settled']} sugestoes "
                   f"({summary['won']} green / {summary['lost']} red)"
                   f". Cota restante: {summary['quota_remaining']}")
        except Exception as exc:  # noqa: BLE001
            msg = f"Falha ao liquidar sugestoes: {exc}"
        return redirect((request.referrer or url_for("history_page"))
                        + f"?flash={msg}")

    @app.route("/settle-auto", methods=["POST"])
    def settle_auto():
        """Busca resultados na Odds API e liquida as apostas terminadas."""
        try:
            summary = scores.settle_finished()
            msg = (f"Liquidacao automatica: {summary['settled']} apostas "
                   f"({summary['won']} green / {summary['lost']} red). "
                   f"Cota restante: {summary['quota_remaining']}")
        except Exception as exc:  # noqa: BLE001
            msg = f"Falha na liquidacao automatica: {exc}"
        return redirect((request.referrer or url_for("dashboard"))
                        + f"?flash={msg}")

    @app.route("/bankroll", methods=["POST"])
    def set_bankroll():
        try:
            store.set_bankroll(float(request.form["initial"]))
        except (KeyError, ValueError):
            abort(400, "banca inicial invalida")
        return redirect(url_for("dashboard"))

    @app.route("/bet/add", methods=["POST"])
    def add_bet():
        f = request.form
        try:
            bet = store.BetInput(
                home_team=f["home_team"], away_team=f["away_team"],
                market=f["market"], selection=f.get("selection", f["market"]),
                odd=float(f["odd"]), stake=float(f["stake"]),
                division=f.get("division") or engine.division,
                model_prob=_optfloat(f.get("model_prob")),
                ev=_optfloat(f.get("ev")), edge=_optfloat(f.get("edge")),
                sport_key=f.get("sport_key") or None,
                event_id=f.get("event_id") or None,
                commence_time=f.get("commence_time") or None,
            )
            store.add_bet(bet)
        except (KeyError, ValueError) as exc:
            abort(400, f"aposta invalida: {exc}")
        return redirect(request.form.get("next") or url_for("bets_page"))

    @app.route("/bet/<int:bet_id>/settle", methods=["POST"])
    def settle_bet(bet_id: int):
        try:
            store.settle_bet(bet_id, request.form["status"])
        except (KeyError, ValueError) as exc:
            abort(400, str(exc))
        return redirect(request.referrer or url_for("bets_page"))

    @app.route("/bet/<int:bet_id>/delete", methods=["POST"])
    def delete_bet(bet_id: int):
        store.delete_bet(bet_id)
        return redirect(request.referrer or url_for("bets_page"))

    # ------------------------------------------------------------------
    # API JSON
    # ------------------------------------------------------------------
    @app.route("/api/teams")
    def api_teams():
        return jsonify({"division": engine.division, "season": engine.season,
                        "teams": engine.teams})

    @app.route("/api/predict")
    def api_predict():
        home = request.args.get("home")
        away = request.args.get("away")
        referee = request.args.get("referee") or None
        if not home or not away:
            abort(400, "informe home e away")
        try:
            return jsonify(engine.predict(home, away, referee))
        except KeyError as exc:
            abort(404, f"time desconhecido: {exc}")

    @app.route("/api/value")
    def api_value():
        try:
            prob = float(request.args["prob"])
            odd = float(request.args["odd"])
        except (KeyError, ValueError):
            abort(400, "informe prob e odd numericos")
        market = request.args.get("market", "custom")
        kelly = _optfloat(request.args.get("kelly"))
        return jsonify(engine.evaluate(market, prob, odd, kelly=kelly))

    @app.route("/api/stats")
    def api_stats():
        return jsonify(store.stats())

    @app.route("/api/ops-status")
    def api_ops_status():
        """Estado da operacao continua 24x7 (monitoramento/health)."""
        return jsonify(scheduler.status())

    @app.route("/api/value-scan")
    def api_value_scan():
        if not engine.odds_api_enabled:
            abort(400, "The Odds API desativada (defina BETFLOW_ODDS_API_KEY)")
        bookmaker = request.args.get("bookmaker") or settings.PREFERRED_BOOKMAKER
        try:
            return jsonify(engine.scan_value_bets(bookmaker=bookmaker))
        except Exception as exc:
            abort(502, f"falha ao varrer odds: {exc}")

    @app.route("/api/fixtures")
    def api_fixtures():
        if not engine.sofascore_enabled:
            abort(400, "SofaScore desativada (defina BETFLOW_RAPIDAPI_KEY)")
        try:
            return jsonify({"division": engine.division,
                            "fixtures": engine.upcoming_fixtures(limit=30)})
        except Exception as exc:
            abort(502, f"falha ao buscar agenda: {exc}")

    @app.route("/api/odds")
    def api_odds():
        match_id = request.args.get("match_id")
        if not match_id:
            abort(400, "informe match_id")
        try:
            return jsonify({"match_id": match_id,
                            "odds": engine.match_odds(match_id)})
        except Exception as exc:
            abort(502, f"falha ao buscar odds: {exc}")

    # filtros de template
    app.jinja_env.filters["pct"] = _fmt_pct
    app.jinja_env.filters["money"] = _fmt_money
    return app


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _optfloat(v: str | None) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _fmt_pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _fmt_money(v: float | None) -> str:
    return "-" if v is None else f"{v:,.2f}"


def _league_name(code: str) -> str:
    return settings.LEAGUES.get(code, {}).get("name", code)

