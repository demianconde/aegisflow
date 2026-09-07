"""ETL da Boosted Research (executavel por GitHub Actions ou cron).

Rotina diaria:
  1) ingere placares novos no Data Warehouse (matches) — SofaScore + Odds API;
  2) recalcula os ratings e retreina os modelos (limpando os caches);
  3) gera os sinais +EV da vertente e grava no track record (strategy='boosted');
  4) tambem roda o motor principal (strategy='main') para manter a comparacao.

Depende das mesmas variaveis de ambiente do app (BETFLOW_ODDS_API_KEY,
BETFLOW_RAPIDAPI_KEY, e a URL do Postgres via BETFLOW_DATABASE_URL ou
BETFLOW_USE_GATEWAY_DB=1 + DATABASE_URL). Nao imprime segredos.
"""
from __future__ import annotations

import logging
import sys

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("boosted.etl")


def main() -> int:
    from config import settings
    from betflow.web import store
    from betflow.betting import suggest
    from betflow.data import results_ingest
    from betflow.research import engine as research

    store.init_db()
    leagues = list(settings.TARGET_LEAGUES)

    before = store.count_matches()
    summary = results_ingest.ingest_all(leagues)
    after = store.count_matches()
    log.info("ingest: %s (cache=%d, novos=%d)", summary, after, after - before)
    if after > before:
        suggest.clear_cache()
        research.clear_cache()

    # motor principal
    main_res = suggest.suggest(days=7, leagues=leagues, calibration="platt",
                               calibration_min_samples=20)
    n_main = store.save_suggestions(
        main_res.suggestions, bookmaker=settings.PREFERRED_BOOKMAKER,
        days=7, leagues=leagues, quota_remaining=main_res.quota_remaining,
        strategy="main")
    log.info("main: %d sinais (%d novos)", len(main_res.suggestions), n_main)

    # Boosted Research
    boosted_res = research.run(days=7, leagues=leagues)
    n_boosted = store.save_suggestions(
        boosted_res.suggestions, bookmaker=research.BOOKMAKER,
        days=7, leagues=leagues, quota_remaining=boosted_res.quota_remaining,
        strategy="boosted")
    log.info("boosted: %d sinais (%d novos)", len(boosted_res.suggestions), n_boosted)

    return 0


if __name__ == "__main__":
    sys.exit(main())
