"""Entry point do coletor de odds (process group 'collector' no Fly).

Modos:
    python -m aegis_side.run_collector once    # 1 coleta e sai (teste/cron)
    python -m aegis_side.run_collector loop    # loop 24/7 (processo do Fly)

O modo `loop` dorme BETFLOW_COLLECT_INTERVAL segundos entre coletas (default 2h).
Nao expoe porta HTTP: e um worker puro, isolado da API do AegisFlow.
"""
from __future__ import annotations

import os
import sys
import time

from aegis_side import odds_collector as oc


def _log(msg: str) -> None:
    print(f"[collector {oc._utcnow()}] {msg}", flush=True)


def _run_once() -> None:
    res = oc.collect_once()
    if res.get("error"):
        _log(f"ERRO: {res['error']}")
        return
    _log(f"ligas={res['leagues']} jogos={res['fixtures']} "
         f"snapshots={res['snapshots']} cota={res['quota_left']}")
    st = oc.stats()
    _log(f"acumulado: snapshots={st['snapshots']} jogos={st['matches']} "
         f"coletas={st['runs']} desde={st['first']}")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if mode == "once":
        _run_once()
        return 0
    if mode == "loop":
        interval = int(os.getenv("BETFLOW_COLLECT_INTERVAL", "7200") or "7200")
        _log(f"iniciando loop (intervalo {interval}s)")
        while True:
            try:
                _run_once()
            except Exception as exc:  # nunca deixa o worker morrer
                _log(f"falha na rodada: {exc}")
            time.sleep(interval)
    _log(f"modo desconhecido: {mode!r} (use 'once' ou 'loop')")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
