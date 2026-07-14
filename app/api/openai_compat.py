"""Endpoints compatíveis com OpenAI: /v1/models e /v1/embeddings (BYOK)."""

from __future__ import annotations

import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_key import ApiContext, get_api_context
from app.config import get_settings
from app.crypto import decrypt_secret
from app.db.models import ProviderKey
from app.db.session import get_db
from app.routing.pricing import infer_provider
from app.security.net import validate_endpoint_async

from .schemas import EmbeddingsRequest

router = APIRouter(prefix="/v1", tags=["proxy"])

_TIMEOUT = httpx.Timeout(30.0, connect=8.0)
# Anthropic não tem endpoint de listagem/embeddings; lista estática mínima.
_ANTHROPIC_MODELS = ["claude-3-5-sonnet", "claude-3-5-haiku", "claude-3-opus"]


async def _provider_keys(db: AsyncSession, tenant_id: uuid.UUID) -> list[ProviderKey]:
    res = await db.execute(select(ProviderKey).where(ProviderKey.tenant_id == tenant_id))
    return list(res.scalars().all())


def _key_secret(rec: ProviderKey) -> str:
    return decrypt_secret(rec.ciphertext, rec.nonce, rec.dek_wrapped)


async def _list_provider_models(rec: ProviderKey, allow_private: bool) -> list[str]:
    try:
        await validate_endpoint_async(rec.base_url, allow_private)
    except ValueError:
        return []
    base = rec.base_url.rstrip("/")
    api_key = _key_secret(rec)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            if rec.format == "anthropic":
                return _ANTHROPIC_MODELS
            if rec.format == "ollama":
                b = base[:-3] if base.endswith("/v1") else base
                r = await client.get(f"{b}/api/tags")
                if r.status_code >= 400:
                    return []
                return [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
            # openai-compatível
            headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
            r = await client.get(f"{base}/models", headers=headers)
            if r.status_code >= 400:
                return []
            return [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]
    except httpx.HTTPError:
        return []


@router.get("/models")
async def list_models(
    ctx: ApiContext = Depends(get_api_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Lista os modelos disponíveis nos provedores cadastrados do tenant (formato OpenAI)."""
    settings = get_settings()
    keys = await _provider_keys(db, ctx.tenant.id)
    data: list[dict] = []
    seen: set[str] = set()
    for rec in keys:
        for model in await _list_provider_models(rec, settings.allow_private_endpoints):
            if model and model not in seen:
                seen.add(model)
                data.append({"id": model, "object": "model", "owned_by": rec.provider})
    return {"object": "list", "data": data}


@router.post("/embeddings")
async def embeddings(
    body: EmbeddingsRequest,
    ctx: ApiContext = Depends(get_api_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Gera embeddings via BYOK (OpenAI-compatível ou Ollama)."""
    provider = body.provider or infer_provider(body.model) or "openai"
    keys = await _provider_keys(db, ctx.tenant.id)
    rec = next((k for k in keys if k.provider == provider), None)
    if rec is None:
        raise HTTPException(400, f"Nenhuma credencial para o provedor '{provider}'.")
    if rec.format == "anthropic":
        raise HTTPException(400, "Anthropic não oferece endpoint de embeddings.")
    try:
        await validate_endpoint_async(rec.base_url, get_settings().allow_private_endpoints)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    base = rec.base_url.rstrip("/")
    api_key = _key_secret(rec)
    inputs = body.input if isinstance(body.input, list) else [body.input]
    fail = "Falha no provedor de embeddings."

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            if rec.format == "ollama":
                b = base[:-3] if base.endswith("/v1") else base
                r = await client.post(
                    f"{b}/api/embed", json={"model": body.model, "input": inputs}
                )
                if r.status_code >= 400:
                    raise HTTPException(status.HTTP_502_BAD_GATEWAY, fail)
                vectors = r.json().get("embeddings", [])
            else:
                headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
                r = await client.post(
                    f"{base}/embeddings",
                    headers=headers,
                    json={"model": body.model, "input": inputs},
                )
                if r.status_code >= 400:
                    raise HTTPException(status.HTTP_502_BAD_GATEWAY, fail)
                vectors = [d.get("embedding", []) for d in r.json().get("data", [])]
    except httpx.HTTPError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Falha ao contatar o provedor.") from exc

    data = [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)]
    return {"object": "list", "data": data, "model": body.model}
