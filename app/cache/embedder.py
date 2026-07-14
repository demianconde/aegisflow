"""Embeddings locais via Ollama (offline).

Usa o endpoint /api/embed do Ollama. Se não houver modelo configurado
(`AEGIS_EMBED_MODEL`) ou o Ollama estiver indisponível, retorna None e o cache
é simplesmente ignorado (fail-open) — nada quebra offline.
"""

from __future__ import annotations

import httpx

from app.config import get_settings
from app.logging_config import get_logger

_log = get_logger("embedder")
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


async def embed(text: str) -> list[float] | None:
    settings = get_settings()
    if not settings.embed_model:
        return None
    base = settings.embed_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{base}/api/embed",
                json={"model": settings.embed_model, "input": text},
            )
        if resp.status_code >= 400:
            _log.warning("embed_failed", status=resp.status_code)
            return None
        data = resp.json()
        vectors = data.get("embeddings") or []
        if vectors and isinstance(vectors[0], list):
            return [float(x) for x in vectors[0]]
        return None
    except httpx.HTTPError as exc:
        _log.warning("embed_unavailable", error=str(exc))
        return None
