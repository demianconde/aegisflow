"""Operacao continua 24x7 do Betflow.

Thread de background que mantem o sistema funcionando sem intervencao:
- **Varredura** (default a cada 6h): gera sugestoes para as ligas-alvo e
  persiste no track record — o historico registra 100% das sugestoes do
  modelo, como se todas tivessem sido seguidas.
- **Liquidacao** (default a cada 1h): busca placares finais na The Odds API
  e resolve as sugestoes pendentes (WON/LOST), atualizando o desempenho.

Controles por variavel de ambiente:
    BETFLOW_AUTO_OPS=0                desliga a operacao continua
    BETFLOW_SCAN_INTERVAL_MIN=360     intervalo da varredura (minutos)
    BETFLOW_SETTLE_INTERVAL_MIN=60    intervalo da liquidacao (minutos)

Custo de cota: ~1 credito/liga por varredura (7 ligas => ~28 creditos/dia
no default) + ~1 credito/liga com pendencias por liquidacao.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("betflow.scheduler")

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "enabled": False,
    "started_at": None,
    "last_scan_at": None,
    "last_scan_summary": None,
    "last_settle_at": None,
    "last_settle_summary": None,
    "last_error": None,
    "scan_interval_min": None,
    "settle_interval_min": None,
}
_THREAD: threading.Thread | None = None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status() -> dict[str, Any]:
    """Snapshot do estado da operacao continua (para dashboard/API)."""
    with _LOCK:
        return dict(_STATE)


def _run_scan() -> None:
    from betflow.betting import suggest
    from betflow.web import store
    from config import settings

    result = suggest.suggest(days=7, leagues=list(settings.TARGET_LEAGUES),
                             calibration="platt", calibration_min_samples=20)
    saved = store.save_suggestions(
        result.suggestions, bookmaker=settings.PREFERRED_BOOKMAKER,
        days=7, leagues=list(settings.TARGET_LEAGUES),
        quota_remaining=result.quota_remaining)
    with _LOCK:
        _STATE["last_scan_at"] = _utcnow_iso()
        _STATE["last_scan_summary"] = {
            "suggestions": len(result.suggestions), "saved": saved,
            "errors": result.errors,
            "quota_remaining": result.quota_remaining,
        }
    log.info("scan ok: %d sugestoes (%d novas), cota=%s",
             len(result.suggestions), saved, result.quota_remaining)


def _run_settle() -> None:
    # import tardio para evitar import circular com betflow.web.app
    from betflow.web.app import _settle_suggestions_auto
    summary = _settle_suggestions_auto()
    with _LOCK:
        _STATE["last_settle_at"] = _utcnow_iso()
        _STATE["last_settle_summary"] = summary
    log.info("liquidacao ok: %s", summary)


def _loop(scan_interval: float, settle_interval: float) -> None:
    # primeira rodada logo apos subir (da tempo do servidor estabilizar)
    next_scan = time.time() + 120
    next_settle = time.time() + 60
    while True:
        now = time.time()
        try:
            if now >= next_settle:
                next_settle = now + settle_interval
                _run_settle()
            if now >= next_scan:
                next_scan = now + scan_interval
                _run_scan()
        except Exception as exc:  # noqa: BLE001 - o loop nunca pode morrer
            with _LOCK:
                _STATE["last_error"] = f"{_utcnow_iso()} {exc}"
            log.warning("ciclo falhou: %s", exc)
        time.sleep(30)


def start() -> bool:
    """Inicia a thread 24x7 (idempotente). Retorna True se ficou ativa."""
    global _THREAD
    if os.getenv("BETFLOW_AUTO_OPS", "1").strip() == "0":
        return False
    if os.getenv("PYTEST_CURRENT_TEST"):  # nunca liga durante os testes
        return False
    from config import settings
    if not settings.ODDS_API_KEY:
        log.info("operacao continua inativa: sem BETFLOW_ODDS_API_KEY")
        return False
    # evita thread duplicada no reloader do Werkzeug (processo pai)
    if os.getenv("FLASK_DEBUG") == "1" and \
            os.getenv("WERKZEUG_RUN_MAIN") != "true":
        return False

    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return True
        scan_min = int(os.getenv("BETFLOW_SCAN_INTERVAL_MIN", "360"))
        settle_min = int(os.getenv("BETFLOW_SETTLE_INTERVAL_MIN", "60"))
        _STATE.update(enabled=True, started_at=_utcnow_iso(),
                      scan_interval_min=scan_min,
                      settle_interval_min=settle_min)
        _THREAD = threading.Thread(
            target=_loop, args=(scan_min * 60.0, settle_min * 60.0),
            name="betflow-ops", daemon=True)
        _THREAD.start()
    log.info("operacao continua ativa (scan=%dmin, settle=%dmin)",
             scan_min, settle_min)
    return True
