"""Modelos ORM do NexusGate (modelo de dados multi-tenant).

Cobre as entidades das Fases 1-5; nesta Fase 0 servem de base para as migrations.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    plan: Mapped[str] = mapped_column(String(50), nullable=False, default="free")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Guardrails (config por tenant)
    guardrail_pii: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    guardrail_blocked_terms: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    users: Mapped[list[User]] = relationship(back_populates="tenant")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    supabase_user_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="member")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    tenant: Mapped[Tenant] = relationship(back_populates="users")


class NexusApiKey(Base):
    """Chave que as aplicações clientes usam para chamar o proxy (x-api-key)."""

    __tablename__ = "nexus_api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_prefix: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Chave virtual: limites por chave (nulo = usa o do plano / sem limite).
    monthly_budget_usd: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    rpm_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allowed_models: Mapped[str | None] = mapped_column(String(1000), nullable=True)  # csv
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProviderKey(Base):
    """Credencial BYOK de um provedor de LLM, criptografada at-rest.

    Suporta qualquer LLM: `format` define o protocolo (openai-compatível ou
    anthropic) e `base_url` o endpoint (inclui modelos locais, ex.: Ollama).
    A chave em si pode ser vazia (endpoints locais sem autenticação).
    """

    __tablename__ = "provider_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    format: Mapped[str] = mapped_column(String(20), nullable=False, default="openai")
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_model: Mapped[str | None] = mapped_column(String(150), nullable=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dek_wrapped: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UsageLog(Base):
    __tablename__ = "usage_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("nexus_api_keys.id", ondelete="SET NULL"), nullable=True, index=True
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model_requested: Mapped[str] = mapped_column(String(100), nullable=False)
    model_used: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    cost_saved_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ok")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_preview: Mapped[str | None] = mapped_column(String(500), nullable=True)
    response_preview: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class Lead(Base):
    """Lead de captação (formulário de interesse da landing)."""

    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    message: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    monthly_spend: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="new")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    plan: Mapped[str] = mapped_column(String(50), nullable=False, default="free")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Plano agendado (downgrade) que entra em vigor no vencimento do período atual.
    pending_plan: Mapped[str | None] = mapped_column(String(50), nullable=True)
