"""Endpoint de proxy de LLM (plano de dados, auth por x-api-key).

Resolve a credencial BYOK do tenant, opcionalmente roteia por complexidade
(`model: "nexus-auto"`) com política **local-first + escalonamento**, chama o
provedor real (qualquer LLM/local), faz streaming SSE e grava o uso (com economia).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.api_key import get_api_tenant
from app.cache import CachedResponse, get_cache
from app.cache.semantic import prompt_text
from app.config import get_settings
from app.crypto import decrypt_secret
from app.db.models import ProviderKey, Tenant
from app.db.session import get_db
from app.metrics import inc
from app.providers.service import (
    ProviderError,
    ProviderService,
    Usage,
    now_ms,
    openai_error_chunk,
)
from app.routing.pricing import cost_usd, infer_provider
from app.routing.router import choose_route, estimate_complexity
from app.security.pii import redact_messages
from app.usage import record_usage

from .schemas import ChatCompletionRequest

router = APIRouter(prefix="/v1", tags=["proxy"])


@dataclass
class Attempt:
    record: ProviderKey
    provider: str
    model: str
    is_local: bool = False


@dataclass
class Plan:
    attempts: list[Attempt]  # ordem: primária → escalonamento
    baseline_model: str | None
    complexity: str | None
    routed: bool = False


async def _tenant_provider_keys(db: AsyncSession, tenant_id: uuid.UUID) -> list[ProviderKey]:
    result = await db.execute(
        select(ProviderKey)
        .where(ProviderKey.tenant_id == tenant_id)
        .order_by(ProviderKey.created_at.desc())
    )
    return list(result.scalars().all())


async def _plan(db: AsyncSession, tenant_id: uuid.UUID, body: ChatCompletionRequest) -> Plan:
    keys = await _tenant_provider_keys(db, tenant_id)

    if body.model == "nexus-auto":
        if not keys:
            raise HTTPException(400, "Nenhuma credencial de provedor cadastrada para rotear.")
        complexity = estimate_complexity([m.model_dump() for m in body.messages])
        route = choose_route(complexity, keys)
        if route is None:
            raise HTTPException(
                400,
                "Sem modelo elegível para roteamento. Defina 'default_model' nas "
                "credenciais locais/custom.",
            )
        attempts = [
            Attempt(route.provider_key, route.provider_key.provider, route.model, route.is_local)
        ]
        if route.escalation is not None:
            e = route.escalation
            attempts.append(Attempt(e.provider_key, e.provider_key.provider, e.model, e.is_local))
        return Plan(
            attempts=attempts,
            baseline_model=route.baseline_model,
            complexity=route.complexity,
            routed=True,
        )

    provider = body.provider or infer_provider(body.model)
    if not provider:
        raise HTTPException(
            400, "Não foi possível inferir o provedor pelo modelo. Envie o campo 'provider'."
        )
    record = next((k for k in keys if k.provider == provider), None)
    if record is None:
        raise HTTPException(
            400,
            f"Nenhuma credencial cadastrada para o provedor '{provider}'. "
            f"Cadastre uma em /v1/admin/provider-keys.",
        )
    return Plan(
        attempts=[Attempt(record, provider, body.model)],
        baseline_model=None,
        complexity=None,
    )


def _service(att: Attempt) -> ProviderService:
    api_key = decrypt_secret(att.record.ciphertext, att.record.nonce, att.record.dek_wrapped)
    return ProviderService(att.record.base_url, api_key, att.record.format)


def _saved(baseline_model: str | None, model: str, pt: int, ct: int) -> float:
    if not baseline_model or baseline_model == model:
        return 0.0
    return max(0.0, cost_usd(baseline_model, pt, ct) - cost_usd(model, pt, ct))


def _delta_content(chunk: bytes) -> str:
    """Extrai o texto de um chunk SSE 'data: {...}' (para acumular no cache)."""
    line = chunk.decode("utf-8", "replace").strip()
    if not line.startswith("data: "):
        return ""
    payload = line[6:].strip()
    if payload == "[DONE]":
        return ""
    try:
        obj = json.loads(payload)
        return (obj.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
    except (json.JSONDecodeError, IndexError, AttributeError):
        return ""


@router.post("/chat/completions")
async def chat_completions(
    body: ChatCompletionRequest,
    tenant: Tenant = Depends(get_api_tenant),
    db: AsyncSession = Depends(get_db),
):
    inc("nexus_requests_total")
    settings = get_settings()
    plan = await _plan(db, tenant.id, body)
    base_upstream: dict = {"messages": [m.model_dump() for m in body.messages]}
    if body.max_tokens is not None:
        base_upstream["max_tokens"] = body.max_tokens
    if body.temperature is not None:
        base_upstream["temperature"] = body.temperature

    def upstream_for(att: Attempt) -> dict:
        """Monta o payload; redige PII para provedores hospedados (LGPD) se ativado."""
        up = {**base_upstream, "model": att.model}
        if settings.pii_guard and not att.is_local:
            up["messages"] = redact_messages(up["messages"])
        return up

    request_id = uuid.uuid4().hex
    tenant_id = tenant.id
    baseline = plan.baseline_model
    cache = get_cache()
    cache_text = prompt_text(base_upstream["messages"])

    # ---------- cache semântico: tenta servir sem chamar o provedor ----------
    cached, cache_emb = await cache.lookup(str(tenant_id), cache_text)
    if cached is not None:
        await record_usage(
            tenant_id=tenant_id,
            request_id=request_id,
            provider=cached.provider,
            model_requested=body.model,
            model_used=cached.model,
            prompt_tokens=cached.prompt_tokens,
            completion_tokens=cached.completion_tokens,
            cost_usd=0.0,
            cost_saved_usd=cost_usd(cached.model, cached.prompt_tokens, cached.completion_tokens),
            cache_hit=True,
            latency_ms=0,
        )
        inc("nexus_cache_hits_total")
        inc("nexus_cost_saved_usd_total",
            cost_usd(cached.model, cached.prompt_tokens, cached.completion_tokens))
        ch = {
            "x-nexus-request-id": request_id,
            "x-nexus-model": cached.model,
            "x-nexus-provider": cached.provider,
            "x-nexus-cache": "hit",
        }
        if body.stream:

            async def cached_stream():
                chunk = {
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant", "content": cached.content}}
                    ],
                    "model": cached.model,
                }
                yield f"data: {json.dumps(chunk)}\n\n".encode()
                done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield f"data: {json.dumps(done)}\n\n".encode()
                yield b"data: [DONE]\n\n"

            return StreamingResponse(
                cached_stream(),
                media_type="text/event-stream",
                headers={**ch, "cache-control": "no-cache"},
            )
        payload = {
            "id": request_id,
            "object": "chat.completion",
            "model": cached.model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": cached.content},
                 "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": cached.prompt_tokens,
                "completion_tokens": cached.completion_tokens,
                "total_tokens": cached.prompt_tokens + cached.completion_tokens,
            },
        }
        return JSONResponse(payload, headers=ch)

    def headers_for(att: Attempt, escalated: bool) -> dict:
        h = {
            "x-nexus-request-id": request_id,
            "x-nexus-model": att.model,
            "x-nexus-provider": att.provider,
            "x-nexus-cache": "miss",
        }
        if plan.routed:
            h["x-nexus-complexity"] = plan.complexity or ""
            h["x-nexus-routed"] = "escalated" if escalated else "auto"
            h["x-nexus-local"] = "true" if att.is_local else "false"
        return h

    # ---------- streaming ----------
    if body.stream:
        first = plan.attempts[0]

        async def event_stream():
            started = now_ms()
            usage = Usage()
            used = first
            collected: list[str] = []
            ok = False
            for idx, att in enumerate(plan.attempts):
                used = att
                upstream = upstream_for(att)
                sent = False
                try:
                    async for chunk in _service(att).stream(upstream, usage):
                        sent = True
                        collected.append(_delta_content(chunk))
                        yield chunk
                    ok = True
                    break  # sucesso
                except (ProviderError, httpx.HTTPError) as exc:
                    if sent or idx == len(plan.attempts) - 1:
                        inc("nexus_errors_total")
                        yield openai_error_chunk(f"{type(exc).__name__}: {str(exc)[:300]}")
                        break
                    # local não deu conta → escala para o próximo (hospedado)
                    continue
            model_used = usage.model or used.model
            inc("nexus_prompt_tokens_total", usage.prompt_tokens)
            inc("nexus_completion_tokens_total", usage.completion_tokens)
            inc("nexus_cost_saved_usd_total",
                _saved(baseline, model_used, usage.prompt_tokens, usage.completion_tokens))
            if ok:
                content = "".join(collected)
                await cache.store(
                    str(tenant_id),
                    cache_emb,
                    CachedResponse(
                        content=content,
                        model=model_used,
                        provider=used.provider,
                        prompt_tokens=usage.prompt_tokens,
                        completion_tokens=usage.completion_tokens,
                    ),
                )
            await record_usage(
                tenant_id=tenant_id,
                request_id=request_id,
                provider=used.provider,
                model_requested=body.model,
                model_used=model_used,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cost_usd=cost_usd(model_used, usage.prompt_tokens, usage.completion_tokens),
                cost_saved_usd=_saved(
                    baseline, model_used, usage.prompt_tokens, usage.completion_tokens
                ),
                latency_ms=now_ms() - started,
            )

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={**headers_for(first, False), "cache-control": "no-cache"},
        )

    # ---------- resposta única ----------
    started = now_ms()
    last_exc: Exception | None = None
    for idx, att in enumerate(plan.attempts):
        try:
            result = await _service(att).complete(upstream_for(att))
        except (ProviderError, httpx.HTTPError) as exc:
            last_exc = exc
            continue  # local/primário não deu conta → tenta o próximo
        model_used = result.usage.model or att.model
        pt, ct = result.usage.prompt_tokens, result.usage.completion_tokens
        inc("nexus_prompt_tokens_total", pt)
        inc("nexus_completion_tokens_total", ct)
        inc("nexus_cost_saved_usd_total", _saved(baseline, model_used, pt, ct))
        await record_usage(
            tenant_id=tenant_id,
            request_id=request_id,
            provider=att.provider,
            model_requested=body.model,
            model_used=model_used,
            prompt_tokens=pt,
            completion_tokens=ct,
            cost_usd=cost_usd(model_used, pt, ct),
            cost_saved_usd=_saved(baseline, model_used, pt, ct),
            latency_ms=now_ms() - started,
        )
        # Passthrough só quando o upstream já é formato OpenAI; senão, normaliza.
        if result.raw and "choices" in result.raw:
            payload = result.raw
        else:
            payload = {
                "id": request_id,
                "object": "chat.completion",
                "model": result.model,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": result.content},
                     "finish_reason": "stop"}
                ],
                "usage": {
                    "prompt_tokens": result.usage.prompt_tokens,
                    "completion_tokens": result.usage.completion_tokens,
                    "total_tokens": result.usage.prompt_tokens + result.usage.completion_tokens,
                },
            }
        await cache.store(
            str(tenant_id),
            cache_emb,
            CachedResponse(
                content=result.content,
                model=model_used,
                provider=att.provider,
                prompt_tokens=pt,
                completion_tokens=ct,
            ),
        )
        return JSONResponse(payload, headers=headers_for(att, escalated=idx > 0))

    inc("nexus_errors_total")
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"Todos os provedores falharam. Último erro: {str(last_exc)[:300]}",
    )
