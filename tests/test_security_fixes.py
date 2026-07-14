"""Testes das correções da avaliação de segurança."""

from __future__ import annotations

import pytest

from app.config import Settings, get_settings


def _settings(**over) -> Settings:
    get_settings.cache_clear()
    base = {"AEGIS_ENV": "production", "AEGIS_DEV_MODE": "false"}
    base.update(over)
    return Settings(**{k: v for k, v in base.items()})


def test_default_env_is_production():
    # Sem .env, o default deve ser production (safe default).
    s = Settings(_env_file=None)
    assert s.env == "production"
    assert s.is_production
    assert not s.dev_bypass_enabled


def test_dev_bypass_never_in_production():
    s = Settings(_env_file=None, AEGIS_ENV="production", AEGIS_DEV_MODE="true")
    assert s.dev_bypass_enabled is False  # jamais em produção


def test_dev_bypass_only_in_dev():
    s = Settings(_env_file=None, AEGIS_ENV="development", AEGIS_DEV_MODE="true")
    assert s.dev_bypass_enabled is True


def test_fail_closed_effective_in_production():
    s = Settings(_env_file=None, AEGIS_ENV="production")
    assert s.ratelimit_fail_closed_effective is True
    d = Settings(_env_file=None, AEGIS_ENV="development")
    assert d.ratelimit_fail_closed_effective is False


@pytest.mark.asyncio
async def test_ssrf_async_blocks_private():
    from app.security.net import validate_endpoint_async

    with pytest.raises(ValueError):
        await validate_endpoint_async("http://169.254.169.254/latest", allow_private=False)
    # allow_private não levanta
    await validate_endpoint_async("http://localhost:11434", allow_private=True)
