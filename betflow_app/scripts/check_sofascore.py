"""Valida a Fonte C: conecta na SofaScore (RapidAPI) e testa os endpoints.

Uso:
    python scripts/check_sofascore.py 10974920         # detalhe + estatisticas
    python scripts/check_sofascore.py 10974920 --stats # so estatisticas achatadas

CADA chamada CONSOME cota do seu plano RapidAPI. Use com parcimonia.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from betflow.data.sofascore import (SofaScoreClient, SofaScoreError,  # noqa: E402
                                    extract_match_stats, parse_events, parse_odds,
                                    parse_standings)
from config import settings  # noqa: E402


def show_league(client: SofaScoreClient, division: str = "E0") -> int:
    """Mostra a agenda e a tabela da liga inteira (via tournaments/*)."""
    tid = settings.SOFASCORE_TOURNAMENTS.get(division)
    if not tid:
        print(f"[erro] divisao {division} sem tournamentId mapeado.")
        return 1
    seasons = client.tournament_seasons(tid)
    if not seasons:
        print("[aviso] sem temporadas.")
        return 0
    sid = seasons[0]["id"]
    print(f"== {division} (tournamentId={tid}) temporada {seasons[0].get('year')} ==")

    games = parse_events(client.tournament_next_matches(tid, sid))
    print(f"\nProximos jogos da liga: {len(games)}")
    for g in games[:10]:
        print(f"  {g['kickoff_utc'][:16]}  {g['home_team']} x {g['away_team']}"
              f"  (matchId={g['match_id']})")

    table = parse_standings(client.tournament_standings(tid, sid))
    print("\nTabela:")
    for t in table[:6]:
        print(f"  {t['position']:>2}. {t['team']:<22} {t['points']:>2}pts "
              f"J{t['played']} ({t['wins']}-{t['draws']}-{t['losses']})")
    print(f"\nCota restante: {client.quota_remaining}")
    print("[ok] Fonte C (liga: agenda + tabela) validada.")
    return 0


def show_team(client: SofaScoreClient, team_id: str) -> int:
    """Mostra a agenda de um time e as odds do proximo jogo."""
    print(f"== Agenda do time {team_id} ==")
    games = parse_events(client.next_matches(team_id))
    if not games:
        print("[aviso] sem proximos jogos.")
        return 0
    for g in games[:8]:
        print(f"  {g['kickoff_utc']}  {g['home_team']} x {g['away_team']}"
              f"  | {g['tournament']}  (matchId={g['match_id']})")
    nxt = games[0]
    print(f"\nOdds do proximo jogo ({nxt['home_team']} x {nxt['away_team']}):")
    odds = parse_odds(client.all_odds(nxt["match_id"]))
    for group, sels in odds.items():
        pretty = ", ".join(f"{k}={v}" for k, v in sels.items())
        print(f"  {group}: {pretty}")
    print(f"\nCota restante: {client.quota_remaining}")
    print("[ok] Fonte C (agenda + odds) validada.")
    return 0


def main(match_id: str, only_stats: bool = False) -> int:
    print("== Fonte C: SofaScore (RapidAPI) ==")
    try:
        client = SofaScoreClient()
    except SofaScoreError as exc:
        print(f"[erro] {exc}")
        return 1

    try:
        if not only_stats:
            ev = client.match_detail(match_id)
            if ev:
                home = ev.get("homeTeam", {}).get("name")
                away = ev.get("awayTeam", {}).get("name")
                tour = ev.get("tournament", {}).get("name")
                print(f"Jogo {match_id}: {home} x {away} | {tour}")
                print(f"customId: {ev.get('customId')} | "
                      f"status: {ev.get('status', {}).get('description')}")
            else:
                print(f"[aviso] sem detalhe para o matchId {match_id}.")

        stats = client.match_statistics(match_id)
        flat = extract_match_stats(stats, period="ALL")
        if flat:
            print("\nEstatisticas (mandante x visitante):")
            for k, v in flat.items():
                print(f"  {k:<14} {v['home']:>6.1f}  x  {v['away']:<6.1f}")
        else:
            print("\n[aviso] nenhuma estatistica mapeada (jogo sem stats "
                  "ou esporte diferente de futebol).")
    except SofaScoreError as exc:
        print(f"[erro] {exc}")
        return 1

    print(f"\nCota restante: {client.quota_remaining}")
    print("[ok] Fonte C validada.")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    # modo liga: python scripts/check_sofascore.py --league E0
    if "--league" in sys.argv:
        i = sys.argv.index("--league")
        div = sys.argv[i + 1] if len(sys.argv) > i + 1 else "E0"
        try:
            raise SystemExit(show_league(SofaScoreClient(), div))
        except SofaScoreError as exc:
            print(f"[erro] {exc}")
            raise SystemExit(1)
    # modo agenda por time: python scripts/check_sofascore.py --team 42
    if "--team" in sys.argv:
        i = sys.argv.index("--team")
        tid = sys.argv[i + 1] if len(sys.argv) > i + 1 else "42"
        try:
            raise SystemExit(show_team(SofaScoreClient(), tid))
        except SofaScoreError as exc:
            print(f"[erro] {exc}")
            raise SystemExit(1)
    mid = args[0] if args else "10974920"
    raise SystemExit(main(mid, only_stats="--stats" in sys.argv))
