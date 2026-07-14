"""Configuração central da aplicação, carregada de variáveis de ambiente / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App — default seguro: production (dev precisa setar NEXUS_ENV=development).
    env: str = Field(default="production", alias="NEXUS_ENV")
    port: int = Field(default=8000, alias="NEXUS_PORT")
    log_level: str = Field(default="INFO", alias="NEXUS_LOG_LEVEL")
    log_json: bool = Field(default=False, alias="NEXUS_LOG_JSON")

    # Modo de desenvolvimento: habilita bypass de login no painel.
    # NUNCA tem efeito em produção (ver dev_bypass_enabled).
    dev_mode: bool = Field(default=False, alias="NEXUS_DEV_MODE")

    # Infra
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/nexusgate",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # Supabase (Fase 1)
    supabase_url: str | None = Field(default=None, alias="SUPABASE_URL")
    supabase_anon_key: str | None = Field(default=None, alias="SUPABASE_ANON_KEY")

    # Envelope encryption (Fase 2)
    master_key: str | None = Field(default=None, alias="NEXUS_MASTER_KEY")

    # Segurança (Fase 6): redige PII antes de enviar a provedores hospedados.
    pii_guard: bool = Field(default=False, alias="NEXUS_PII_GUARD")
    # Observabilidade: registra prévia (redigida) de prompt/resposta nos logs. Opt-in.
    log_content: bool = Field(default=False, alias="NEXUS_LOG_CONTENT")
    # Permite endpoints de provedor em rede privada/local (self-host). Em SaaS: false.
    allow_private_endpoints: bool = Field(default=False, alias="NEXUS_ALLOW_PRIVATE_ENDPOINTS")
    # Rate limit / quota: se true, bloqueia quando o Redis está indisponível (fail-closed).
    ratelimit_fail_closed: bool = Field(default=False, alias="NEXUS_RATELIMIT_FAIL_CLOSED")
    # CORS: origens permitidas em produção (separadas por vírgula).
    cors_origins: str = Field(default="", alias="NEXUS_CORS_ORIGINS")
    # Token opcional para proteger /metrics.
    metrics_token: str | None = Field(default=None, alias="NEXUS_METRICS_TOKEN")

    # Cache semântico (Fase 4)
    cache_enabled: bool = Field(default=True, alias="NEXUS_CACHE_ENABLED")
    cache_threshold: float = Field(default=0.92, alias="NEXUS_CACHE_THRESHOLD")
    cache_ttl_seconds: int = Field(default=7 * 24 * 3600, alias="NEXUS_CACHE_TTL")
    cache_max_entries: int = Field(default=1000, alias="NEXUS_CACHE_MAX_ENTRIES")  # teto por tenant
    embed_url: str = Field(default="http://localhost:11434", alias="NEXUS_EMBED_URL")
    embed_model: str | None = Field(default=None, alias="NEXUS_EMBED_MODEL")

    # Stripe (Fase 5) — em homologação use chaves de TESTE (sk_test_... / whsec_...).
    stripe_secret_key: str | None = Field(default=None, alias="STRIPE_SECRET_KEY")
    stripe_webhook_secret: str | None = Field(default=None, alias="STRIPE_WEBHOOK_SECRET")
    # Métodos de pagamento do Checkout (assinatura): cartão é o confiável p/ recorrência.
    stripe_payment_methods: str = Field(default="card", alias="NEXUS_STRIPE_PAYMENT_METHODS")

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"

    @property
    def dev_bypass_enabled(self) -> bool:
        """Bypass de login só vale em dev — jamais em produção."""
        return self.dev_mode and not self.is_production

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def ratelimit_fail_closed_effective(self) -> bool:
        """Em produção, falha fechado por padrão (Redis é obrigatório)."""
        return self.ratelimit_fail_closed or self.is_production


@lru_cache
def get_settings() -> Settings:
    """Retorna a instância única de Settings (cacheada)."""
    return Settings()
