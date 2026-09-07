"""Gera "apostas prontas" das 7 competicoes-alvo com odds da Betano.

Para cada liga em `settings.TARGET_LEAGUES`:
  1. treina Dixon-Coles nos dados historicos (football-data.co.uk) - excecao:
     ligas `source="fallback"` (ex.: Champions) nao tem CSV proprio e usam a
     linha JUSTA DO MERCADO (de-vig via Shin) como probabilidade de referencia;
  2. busca as odds da Betano na The Odds API (1 regiao, mercado h2h => ~1
     credito por liga) apenas dos jogos de HOJE e dos proximos N dias;
  3. calcula EV / edge / stake (Kelly fracionario) de cada selecao 1X2;
  4. mantem so as apostas de VALOR (edge >= MIN_EDGE) e gera, para cada uma, um
     link de busca na Betano (nao ha deep-link publico por jogo);
  5. escreve um relatorio HTML consolidado em data/apostas/apostas.html.

Uso:
    python scripts/apostas_prontas.py                # hoje + 7 dias, todas as 7 ligas
    python scripts/apostas_prontas.py --days 3       # janela menor
    python scripts/apostas_prontas.py --today        # so jogos de hoje
    python scripts/apostas_prontas.py --leagues E0,BSA
    python scripts/apostas_prontas.py --min-edge 0.05 --bankroll 1000
    python scripts/apostas_prontas.py --dry-run      # NAO chama a Odds API (nao gasta cota)

Cota: 1 credito por liga por execucao (regions=1, markets=h2h). 7 ligas => 7.
"""
from __future__ import annotations

import argparse
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from betflow.data import football_data as fd  # noqa: E402
from betflow.data import extra_leagues as el  # noqa: E402
from betflow.data import team_names  # noqa: E402
from betflow.data import odds_api  # noqa: E402
from betflow.models.dixon_coles import DixonColesModel  # noqa: E402
from betflow.models.counts import NegativeBinomialTotals, PoissonCards  # noqa: E402
from betflow.betting import value  # noqa: E402
from betflow.betting import calibration as calib  # noqa: E402
from betflow.web import store  # noqa: E402
from config import settings  # noqa: E402


# cache de calibradores por liga (treinados com apostas liquidadas do tracker)
_CALIB_CACHE: dict[str, dict[str, Any]] = {}


def _calibrators_for(league_code: str, method: str,
                       min_samples: int) -> dict[str, Any]:
    """Retorna (e cacheia) calibradores one-vs-rest para uma liga."""
    key = f"{league_code}:{method}:{min_samples}"
    if key not in _CALIB_CACHE:
        samples = store.settled_1x2_calibration_samples(division=league_code)
        _CALIB_CACHE[key] = calib.fit_1x2_calibrators(
            samples, method=method, min_samples=min_samples)
    return _CALIB_CACHE[key]


# Linhas Over/Under exibidas nas previsoes do modelo (custo ZERO de cota; a
# The Odds API nao fornece odds da Betano nesses mercados).
CORNER_LINES = (9.5, 10.5, 11.5)
CARD_LINES = (3.5, 4.5, 5.5)


@dataclass
class LeagueModels:
    """Modelos treinados de uma liga. corners/cards so existem em ligas com
    estatisticas (CSVs europeus: E0/F1/I1/SP1)."""
    dc: DixonColesModel | None = None
    corners: NegativeBinomialTotals | None = None
    cards: PoissonCards | None = None

    @property
    def has_stats(self) -> bool:
        return self.corners is not None and self.cards is not None



# ---------------------------------------------------------------------------
# Modelos por liga
# ---------------------------------------------------------------------------
def _current_seasons() -> tuple[str, str]:
    """Codigos das 2 temporadas europeias mais recentes (ex.: '2526','2425')."""
    now = datetime.now()
    start = now.year if now.month >= 7 else now.year - 1
    return settings.season_code(start), settings.season_code(start - 1)


def _train_model(league_code: str) -> LeagueModels:
    """Treina os modelos da liga.

    - Dixon-Coles (1X2) sempre que houver dados; None para ligas `fallback`.
    - Escanteios (NB) e cartoes (Poisson) SO quando o CSV tem as colunas
      home_corners/home_yellow (ligas europeias). Custo ZERO de cota.
    """
    cfg = settings.LEAGUES[league_code]
    src = cfg["source"]
    if src == "fallback":
        return LeagueModels()
    if src == "extra":
        df = el.load(cfg["extra_code"], league=cfg.get("extra_league"))
        if "season" in df.columns and not df.empty:
            seasons = sorted(df["season"].astype(str).unique())
            df = df[df["season"].astype(str).isin(set(seasons[-3:]))]
    else:  # europe: temporada corrente + anterior (mais amostra)
        div = cfg["division"]
        frames = []
        for sea in _current_seasons():
            try:
                frames.append(fd.load_season(div, sea))
            except Exception as exc:  # noqa: BLE001
                print(f"    [aviso] {div} {sea}: {exc}")
        # fallback: se o download falhou (site fora do ar/temporada inexistente),
        # usa qualquer CSV ja baixado em data/raw para esta divisao.
        if not frames:
            for cached in sorted(settings.RAW_DIR.glob(f"{div}_*.csv"), reverse=True):
                try:
                    d = fd.parse(cached)
                    d["season"] = cached.stem.split("_")[-1]
                    frames.append(d)
                    print(f"    [cache] usando {cached.name} ({len(d)} jogos)")
                    break
                except Exception as exc:  # noqa: BLE001
                    print(f"    [aviso] cache {cached.name}: {exc}")
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        raise RuntimeError(f"sem dados de treino para {league_code}")

    models = LeagueModels(dc=DixonColesModel(xi=0.0018).fit(df))
    # escanteios/cartoes so quando o CSV tem as colunas (ligas europeias)
    if {"home_corners", "away_corners"} <= set(df.columns) \
            and df[["home_corners", "away_corners"]].notna().any().all():
        models.corners = NegativeBinomialTotals().fit(df)
    if {"home_yellow", "away_yellow"} <= set(df.columns) \
            and df[["home_yellow", "away_yellow"]].notna().any().all():
        models.cards = PoissonCards().fit(df)
    return models


# ---------------------------------------------------------------------------
# Janela de datas (hoje / proximos N dias) sobre commence_time (ISO UTC)
# ---------------------------------------------------------------------------
def _in_window(commence_iso: str, days: int, today_only: bool) -> bool:
    try:
        dt = datetime.fromisoformat(commence_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=3)  # tolera jogo iniciado ha pouco
    end = (now.replace(hour=23, minute=59, second=59) if today_only
           else now + timedelta(days=days))
    return start <= dt <= end


def _fmt_kickoff(commence_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(commence_iso.replace("Z", "+00:00"))
        return (dt - timedelta(hours=3)).strftime("%d/%m %H:%M")  # Brasilia
    except (ValueError, AttributeError):
        return commence_iso or "?"


# ---------------------------------------------------------------------------
# Probabilidade de referencia (modelo OU de-vig de mercado via Shin)
# ---------------------------------------------------------------------------
def _reference_probs(models: LeagueModels, event: dict, bo: dict[str, float],
                     calibrators: dict[str, Any] | None = None
                     ) -> tuple[dict[str, float], str, tuple[str | None, str | None]]:
    """Retorna (probs 1X2, fonte, (home_model, away_model)).

    A tupla de nomes casados e reaproveitada para as previsoes de
    escanteios/cartoes (evita casar os times duas vezes). Se `calibrators`
    for fornecido, ajusta as probabilidades do modelo antes de calcular EV.
    """
    if models.dc is not None:
        hm = team_names.match_team(event.get("home_team", ""), models.dc.teams)
        am = team_names.match_team(event.get("away_team", ""), models.dc.teams)
        if hm and am:
            p = dict(models.dc.predict_1x2(hm, am))
            if calibrators:
                p = calib.calibrate_1x2(p, calibrators)
            return {"H": p["H"], "D": p["D"], "A": p["A"]}, "modelo", (hm, am)
    fair = value.fair_probs([bo["home"], bo["draw"], bo["away"]], method="shin")
    return ({"H": float(fair[0]), "D": float(fair[1]), "A": float(fair[2])},
            "mercado", (None, None))


def _stats_forecast(models: LeagueModels, hm: str | None, am: str | None
                    ) -> dict | None:
    """Previsao do modelo p/ escanteios e cartoes de um confronto.

    So retorna dados quando a liga tem os modelos de contagem (E0/F1/I1/SP1) e
    os dois times sao conhecidos. NAO consulta a API (custo zero de cota) e NAO
    ha odd da Betano nesses mercados (a The Odds API nao as fornece).
    """
    if not models.has_stats or not (hm and am):
        return None
    corners = {
        "expected": round(models.corners.expected_total(hm, am), 1),
        "over": {str(l): round(models.corners.prob_over(l, hm, am), 3)
                 for l in CORNER_LINES},
    }
    # cartoes: sem arbitro definido a priori (a Odds API nao informa) -> media
    cards = {
        "expected": round(models.cards.expected_total(None), 1),
        "over": {str(l): round(models.cards.prob_over(l, None), 3)
                 for l in CARD_LINES},
    }
    return {"corners": corners, "cards": cards}


# ---------------------------------------------------------------------------
# Link de busca na Betano (texto; sem QR)
# ---------------------------------------------------------------------------
def _betano_link(home: str, away: str) -> str:
    q = urllib.parse.quote_plus(f"{home} {away}")
    return settings.BETANO_SEARCH_URL.format(q=q)



# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------
def collect(leagues: list[str], days: int, today_only: bool, min_edge: float,
            kelly: float, bankroll: float, dry_run: bool,
            calibration: str | None = None,
            calibration_min_samples: int = 20) -> tuple[list[dict], dict]:
    bets: list[dict] = []
    meta: dict = {"quota_remaining": None, "leagues": {}}
    client = None if dry_run else odds_api.OddsApiClient()

    for code in leagues:
        cfg = settings.LEAGUES.get(code)
        if not cfg:
            print(f"[aviso] liga desconhecida ignorada: {code}")
            continue
        print(f"\n== {cfg['name']} [{code}] ==")

        try:
            models = _train_model(code)
            if models.dc is not None:
                extra = " + escanteios/cartoes" if models.has_stats else ""
                print(f"   modelo: Dixon-Coles treinado{extra}")
            else:
                print("   modelo: fallback de mercado (Shin)")
        except Exception as exc:  # noqa: BLE001
            print(f"   [erro modelo] {exc} -> usando fallback de mercado")
            models = LeagueModels()

        calibrators = (_calibrators_for(code, calibration, calibration_min_samples)
                       if calibration and models.dc is not None else None)
        if calibrators:
            print(f"   calibracao ativa: {calibration} "
                  f"({sum(1 for v in calibrators.values() if v)} mercados)")

        if dry_run:
            print("   [dry-run] pulando chamada da Odds API")
            meta["leagues"][code] = {"events": 0, "value": 0, "dry_run": True}
            continue
        try:
            events = client.get_odds(cfg["odds_api_key"], markets="h2h",
                                     regions="uk")  # 1 regiao => 1 credito
        except odds_api.OddsApiError as exc:
            print(f"   [erro odds] {exc}")
            meta["leagues"][code] = {"error": str(exc)}
            continue
        meta["quota_remaining"] = client.last_quota.remaining

        n_window = n_value = 0
        for ev in events:
            if not _in_window(ev.get("commence_time", ""), days, today_only):
                continue
            n_window += 1
            odds = odds_api.extract_h2h_by_bookmaker(ev, settings.PREFERRED_BOOKMAKER)
            if not odds:
                continue
            probs, source, (hm, am) = _reference_probs(models, ev, odds, calibrators)
            stats = _stats_forecast(models, hm, am)
            legs = [
                ("1X2:H", f"{ev['home_team']} vencer", probs["H"], odds["home"]),
                ("1X2:D", "Empate", probs["D"], odds["draw"]),
                ("1X2:A", f"{ev['away_team']} vencer", probs["A"], odds["away"]),
            ]
            for market, sel, prob, odd in legs:
                vb = value.evaluate_bet(market=market, selection=sel,
                                        model_prob=prob, odd=odd,
                                        fair_prob=1.0 / odd,
                                        min_edge=min_edge, kelly=kelly)
                if vb.stake_fraction > 0:
                    n_value += 1
                    bets.append({
                        "league": cfg["name"], "league_code": code,
                        "home": ev.get("home_team"), "away": ev.get("away_team"),
                        "kickoff": _fmt_kickoff(ev.get("commence_time", "")),
                        "market": market, "selection": sel,
                        "odd": round(odd, 2), "prob": round(prob, 4),
                        "implied": round(1.0 / odd, 4),
                        "ev": round(vb.ev, 4), "edge": round(vb.edge, 4),
                        "stake_frac": round(vb.stake_fraction, 4),
                        "stake_value": round(vb.stake_fraction * bankroll, 2),
                        "source": source,
                        "stats": stats,
                        "link": _betano_link(ev.get("home_team", ""),
                                             ev.get("away_team", "")),
                    })
        print(f"   jogos na janela: {n_window} | apostas de valor: {n_value}")
        meta["leagues"][code] = {"events": n_window, "value": n_value}

    bets.sort(key=lambda b: b["ev"], reverse=True)
    return bets, meta



# ---------------------------------------------------------------------------
# Relatorio HTML
# ---------------------------------------------------------------------------
def _stats_cell(stats: dict | None) -> str:
    """Celula HTML com a previsao do modelo p/ escanteios e cartoes.

    Sao PREVISOES (nao ha odd da Betano nesses mercados na Odds API). Ligas sem
    dados historicos (Brasil/Argentina/Champions) mostram '-'.
    """
    if not stats:
        return '<td class="stats muted">sem dados<br>(so ligas europeias)</td>'
    c, k = stats["corners"], stats["cards"]
    corners = " ".join(
        f'<span class="ln">O{l}: <b>{c["over"][str(l)]*100:.0f}%</b></span>'
        for l in CORNER_LINES)
    cards = " ".join(
        f'<span class="ln">O{l}: <b>{k["over"][str(l)]*100:.0f}%</b></span>'
        for l in CARD_LINES)
    return (f'<td class="stats">'
            f'<div class="grp"><span class="tag">Escanteios</span> '
            f'esp. {c["expected"]:.1f}<br>{corners}</div>'
            f'<div class="grp"><span class="tag">Cartoes</span> '
            f'esp. {k["expected"]:.1f}<br>{cards}</div></td>')


def render_html(bets: list[dict], meta: dict, params: dict) -> str:
    rows = []
    for b in bets:
        rows.append(f"""
        <tr>
          <td>{b['kickoff']}</td>
          <td>{b['league']}</td>
          <td><b>{b['home']}</b> x <b>{b['away']}</b><br>
              <span class="sel">{b['selection']}</span>
              <span class="src">({b['source']})</span></td>
          <td class="num">{b['odd']:.2f}</td>
          <td class="num">{b['prob']*100:.1f}%</td>
          <td class="num">{b['implied']*100:.1f}%</td>
          <td class="num edge">{b['edge']*100:+.1f}%</td>
          <td class="num ev">{b['ev']*100:+.1f}%</td>
          <td class="num">{b['stake_frac']*100:.2f}%<br>R$ {b['stake_value']:.2f}</td>
          {_stats_cell(b.get('stats'))}
          <td><a href="{b['link']}" target="_blank">Abrir na Betano →</a></td>
        </tr>""")

    quota = meta.get("quota_remaining")
    quota_txt = (f"cota restante na Odds API: {quota}" if quota is not None
                 else "cota: n/d")
    gerado = datetime.now().strftime("%d/%m/%Y %H:%M")
    janela = ("somente hoje" if params["today_only"]
              else f"hoje + {params['days']} dias")
    if bets:
        table = (
            "<table><thead><tr>"
            "<th>Inicio</th><th>Liga</th><th>Jogo / Selecao</th>"
            "<th>Odd</th><th>P(modelo)</th><th>P(impl.)</th>"
            "<th>Edge</th><th>EV</th><th>Stake</th>"
            "<th>Escanteios / Cartoes<br>(previsao do modelo)</th><th>Betano</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        )
    else:
        table = ("<div class='empty'>Nenhuma aposta de valor encontrada "
                 "nesta janela.</div>")

    return f"""<!doctype html>
<html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Betflow - Apostas prontas (Betano)</title>
<style>
  body{{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:24px;color:#111;background:#f6f7f9}}
  h1{{margin:0 0 4px}} .sub{{color:#555;margin-bottom:16px}}
  table{{border-collapse:collapse;width:100%;background:#fff;box-shadow:0 1px 4px #0001}}
  th,td{{border-bottom:1px solid #eee;padding:8px 10px;text-align:left;vertical-align:top;font-size:14px}}
  th{{background:#0b6;color:#fff}}
  td.num{{text-align:right;font-variant-numeric:tabular-nums}}
  .edge,.ev{{font-weight:700;color:#0a7}}
  .sel{{color:#0a7;font-weight:600}} .src{{color:#999;font-size:12px}}
  .stats{{font-size:12px;color:#333;min-width:150px}}
  .stats .grp{{margin-bottom:4px}}
  .stats .tag{{display:inline-block;background:#eef6f1;color:#0a7;border-radius:4px;padding:0 5px;font-weight:700;font-size:11px}}
  .stats .ln{{display:inline-block;margin-right:6px;white-space:nowrap}}
  .stats.muted{{color:#aaa;font-style:italic}}
  .warn{{background:#fff8e1;border:1px solid #ffe082;padding:10px 14px;border-radius:8px;margin:14px 0;font-size:13px}}
  .empty{{padding:24px;background:#fff;border-radius:8px}}
</style></head><body>
<h1>Betflow - Apostas prontas</h1>
<div class="sub">Casa: <b>{settings.PREFERRED_BOOKMAKER}</b> &middot; Mercado 1X2 &middot;
 Janela: {janela} &middot; edge minimo: {params['min_edge']*100:.1f}% &middot;
 Kelly: {params['kelly']*100:.0f}% &middot; banca: R$ {params['bankroll']:.2f}<br>
 Gerado em {gerado} &middot; {quota_txt}</div>
<div class="warn">O link "Abrir na Betano" leva a <b>busca do confronto</b> no site da
 {settings.PREFERRED_BOOKMAKER} (a casa nao expoe link publico que ja abra a aposta
 preenchida). Confira a odd no app antes de apostar - odds mudam. Isto e analise
 estatistica, nao garantia de lucro.<br>
 <b>Escanteios/Cartoes</b> sao <b>previsoes do modelo</b> (P(Over) por linha) - a
 The Odds API nao fornece odds desses mercados na {settings.PREFERRED_BOOKMAKER},
 entao nao ha EV/stake para eles. Disponivel so nas ligas europeias com dados
 historicos (Premier, Ligue 1, Serie A, La Liga).</div>
{table}
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Apostas prontas Betano (7 ligas)")
    ap.add_argument("--days", type=int, default=7, help="janela em dias (default 7)")
    ap.add_argument("--today", action="store_true", help="somente jogos de hoje")
    ap.add_argument("--leagues", type=str, default=None,
                    help="lista separada por virgula (default: as 7 alvo)")
    ap.add_argument("--min-edge", type=float, default=settings.MIN_EDGE)
    ap.add_argument("--kelly", type=float, default=settings.KELLY_FRACTION)
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="nao chama a Odds API (nao gasta cota)")
    ap.add_argument("--calibration", type=str, default=None,
                    help="metodo de calibracao: platt | isotonic (default: desligado)")
    ap.add_argument("--calibration-min-samples", type=int, default=20,
                    help="minimo de apostas liquidadas por mercado para calibrar")
    args = ap.parse_args()

    leagues = ([c.strip() for c in args.leagues.split(",")]
               if args.leagues else list(settings.TARGET_LEAGUES))

    print("== Betflow :: apostas prontas (Betano) ==")
    print(f"Ligas: {', '.join(leagues)} | janela: "
          f"{'hoje' if args.today else str(args.days)+' dias'} | "
          f"edge>={args.min_edge:.0%} | Kelly {args.kelly:.0%}")

    bets, meta = collect(leagues, args.days, args.today, args.min_edge,
                         args.kelly, args.bankroll, args.dry_run,
                         calibration=args.calibration,
                         calibration_min_samples=args.calibration_min_samples)

    print(f"\n== Total de apostas de valor: {len(bets)} ==")
    for b in bets[:20]:
        print(f"  [{b['league_code']:<3}] {b['kickoff']} {b['home']} x {b['away']} "
              f"| {b['selection']} @ {b['odd']:.2f} "
              f"| edge {b['edge']:+.1%} EV {b['ev']:+.1%} "
              f"stake R$ {b['stake_value']:.2f} ({b['source']})")

    out_dir = settings.DATA_DIR / "apostas"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "apostas.html"
    params = {"days": args.days, "today_only": args.today,
              "min_edge": args.min_edge, "kelly": args.kelly,
              "bankroll": args.bankroll}
    out_file.write_text(render_html(bets, meta, params), encoding="utf-8")
    print(f"\n[ok] Relatorio HTML: {out_file}")
    if meta.get("quota_remaining") is not None:
        print(f"[cota] restantes na The Odds API: {meta['quota_remaining']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
