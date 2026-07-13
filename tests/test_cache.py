"""Testes do cache semântico (com embedder simulado, sem rede)."""

from __future__ import annotations

import pytest

from app.cache import semantic
from app.cache.semantic import CachedResponse, SemanticCache, _cosine, prompt_text
from app.config import get_settings


def test_cosine():
    assert _cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert _cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert _cosine([1, 0], []) == 0.0


def test_prompt_text():
    text = prompt_text([{"role": "system", "content": "a"}, {"role": "user", "content": "b"}])
    assert text == "a\nb"


async def test_cache_hit_and_miss(monkeypatch):
    get_settings.cache_clear()

    # embedder determinístico: vetor baseado na 1a letra
    async def fake_embed(text: str):
        return [1.0, 0.0] if text.startswith("ola") else [0.0, 1.0]

    monkeypatch.setattr(semantic, "embed", fake_embed)
    cache = SemanticCache()

    hit, emb = await cache.lookup("t1", "ola mundo")  # vazio → miss, mas retorna embedding
    assert hit is None and emb is not None
    await cache.store("t1", emb, CachedResponse("oi!", "m", "p", 5, 3))

    hit2, _ = await cache.lookup("t1", "ola pessoal")  # mesmo vetor → hit
    assert hit2 is not None and hit2.content == "oi!"

    hit3, _ = await cache.lookup("t1", "xyz outro")  # vetor diferente → miss
    assert hit3 is None
    hit4, _ = await cache.lookup("t2", "ola mundo")  # tenant diferente → isolado
    assert hit4 is None
