"""Cache semântico por tenant: reaproveita respostas de prompts similares.

Store em memória (por processo) com similaridade de cosseno e TTL. Em produção,
trocar por Redis Stack (RediSearch/HNSW) — a interface get()/put() é a mesma.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from app.cache.embedder import embed
from app.config import get_settings


@dataclass
class CachedResponse:
    content: str
    model: str
    provider: str
    prompt_tokens: int
    completion_tokens: int


@dataclass
class _Entry:
    embedding: list[float]
    response: CachedResponse
    ts: float


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=False)) / (na * nb)


class SemanticCache:
    def __init__(self) -> None:
        self._store: dict[str, list[_Entry]] = {}

    def _prune(self, tenant: str, now: float, ttl: int) -> None:
        items = self._store.get(tenant)
        if items:
            self._store[tenant] = [e for e in items if now - e.ts <= ttl]

    async def lookup(
        self, tenant_id: str, text: str
    ) -> tuple[CachedResponse | None, list[float] | None]:
        """Retorna (resposta_em_cache, embedding). O embedding é reaproveitado no store."""
        settings = get_settings()
        if not settings.cache_enabled:
            return None, None
        emb = await embed(text)
        if emb is None:
            return None, None
        now = time.time()
        self._prune(tenant_id, now, settings.cache_ttl_seconds)
        best: _Entry | None = None
        best_score = 0.0
        for entry in self._store.get(tenant_id, []):
            score = _cosine(emb, entry.embedding)
            if score > best_score:
                best_score, best = score, entry
        if best and best_score >= settings.cache_threshold:
            return best.response, emb
        return None, emb

    async def store(
        self, tenant_id: str, embedding: list[float] | None, response: CachedResponse
    ) -> None:
        """Guarda a resposta usando o embedding já calculado no lookup (sem recalcular)."""
        if embedding is None or not get_settings().cache_enabled or not response.content:
            return
        self._store.setdefault(tenant_id, []).append(
            _Entry(embedding=embedding, response=response, ts=time.time())
        )


_cache = SemanticCache()


def get_cache() -> SemanticCache:
    return _cache


def prompt_text(messages: list[dict]) -> str:
    """Texto para embutir: concatena as mensagens (foco na última do usuário)."""
    parts = [str(m.get("content", "")) for m in messages]
    return "\n".join(parts).strip()
