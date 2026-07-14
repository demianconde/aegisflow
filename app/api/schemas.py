"""Schemas Pydantic de request/response da API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MeResponse(BaseModel):
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: str
    plan: str


class ApiKeyCreate(BaseModel):
    name: str


class ApiKeyInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key_prefix: str
    name: str
    created_at: datetime
    revoked_at: datetime | None = None
    monthly_budget_usd: float | None = None
    rpm_limit: int | None = None
    allowed_models: str | None = None


class ApiKeyCreated(ApiKeyInfo):
    # Chave completa em claro — retornada APENAS na criação.
    api_key: str


# ---------- Provider keys (BYOK) ----------
class ProviderKeyCreate(BaseModel):
    provider: str  # openai, anthropic, qwen, ollama, custom, ...
    api_key: str | None = None  # opcional (endpoints locais podem não exigir)
    base_url: str | None = None  # sobrescreve o default do provedor (obrigatório p/ custom/local)
    format: str | None = None  # "openai" | "anthropic" (default vem do registry)
    label: str | None = None  # "Nome da API"
    default_model: str | None = None  # modelo padrão (útil p/ locais)


class ProviderKeyInfo(BaseModel):
    id: uuid.UUID
    provider: str
    provider_label: str  # nome amigável detectado do registry
    format: str
    base_url: str | None = None
    label: str | None = None
    default_model: str | None = None
    is_local: bool = False
    created_at: datetime


# ---------- Chat completions ----------
class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    provider: str | None = None  # se omitido, é inferido do nome do modelo
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    # Cadeia de fallback: itens "provider:model" ou "model" tentados se o principal falhar.
    fallback: list[str] | None = None


class EmbeddingsRequest(BaseModel):
    model: str
    input: str | list[str]
    provider: str | None = None


# ---------- Chaves virtuais / limites por chave ----------
class ApiKeyLimits(BaseModel):
    monthly_budget_usd: float | None = None
    rpm_limit: int | None = None
    allowed_models: str | None = None  # csv de modelos permitidos (vazio = todos)
