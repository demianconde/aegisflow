"""Rate limiting e quotas via Redis (janela fixa)."""

from __future__ import annotations

import time

from fastapi import HTTPException, status
from redis.exceptions import RedisError

from app.config import get_settings
from app.logging_config import get_logger
from app.redis_client import get_redis

_log = get_logger("ratelimit")

_WINDOW_SECONDS = 60


def _on_redis_error(exc: RedisError) -> None:
    """Fail-open (segue) ou fail-closed (503), conforme configuração."""
    _log.warning("rate_limit_unavailable", error=str(exc))
    if get_settings().ratelimit_fail_closed_effective:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Serviço de limites indisponível.",
        ) from exc


async def enforce_minute(subject: str, rpm: int, label: str = "requisições") -> None:
    """Limite por minuto para um 'subject' (tenant ou chave)."""
    now = int(time.time())
    key = f"rl:{subject}:{now // _WINDOW_SECONDS}"
    try:
        redis = get_redis()
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, _WINDOW_SECONDS)
    except RedisError as exc:
        _on_redis_error(exc)
        return
    if count > rpm:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Limite de {label} por minuto excedido",
            headers={"Retry-After": str(_WINDOW_SECONDS)},
        )


async def enforce_monthly_quota(tenant_id: str, quota: int, plan_label: str) -> None:
    """Quota mensal de requisições do tenant."""
    now = int(time.time())
    key = f"quota:{tenant_id}:{time.strftime('%Y%m', time.gmtime(now))}"
    try:
        redis = get_redis()
        used = await redis.incr(key)
        if used == 1:
            await redis.expire(key, 31 * 24 * 3600)
    except RedisError as exc:
        _on_redis_error(exc)
        return
    if used > quota:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Quota mensal do plano '{plan_label}' excedida. Faça upgrade.",
        )
