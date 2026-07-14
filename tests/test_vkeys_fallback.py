"""Testes de allowlist (chave virtual) e cadeia de fallback."""

from __future__ import annotations

from dataclasses import dataclass

from app.api.chat import Attempt, _apply_allowlist, _fallback_attempts


@dataclass
class FakeProviderKey:
    provider: str
    base_url: str | None = None
    default_model: str | None = None


@dataclass
class FakeApiKey:
    allowed_models: str | None = None


def _att(provider, model):
    return Attempt(FakeProviderKey(provider), provider, model)


def test_allowlist_none_passthrough():
    atts = [_att("openai", "gpt-4o"), _att("google", "gemini-2.5-pro")]
    assert _apply_allowlist(atts, FakeApiKey(allowed_models=None)) == atts


def test_allowlist_filters():
    atts = [_att("openai", "gpt-4o-mini"), _att("google", "gemini-2.5-pro")]
    out = _apply_allowlist(atts, FakeApiKey(allowed_models="gpt-4o-mini"))
    assert len(out) == 1 and out[0].model == "gpt-4o-mini"


def test_allowlist_blocks_all():
    atts = [_att("google", "gemini-2.5-pro")]
    out = _apply_allowlist(atts, FakeApiKey(allowed_models="gpt-4o"))
    assert out == []


def test_fallback_parsing():
    keys = [FakeProviderKey("openai"), FakeProviderKey("google")]
    # "provider:model" e "model" (infere provider)
    out = _fallback_attempts(keys, ["google:gemini-2.5-flash", "gpt-4o-mini"])
    assert [(a.provider, a.model) for a in out] == [
        ("google", "gemini-2.5-flash"),
        ("openai", "gpt-4o-mini"),
    ]


def test_fallback_skips_unknown_provider():
    keys = [FakeProviderKey("openai")]
    out = _fallback_attempts(keys, ["anthropic:claude-3-5-sonnet", "gpt-4o-mini"])
    # anthropic não cadastrado → ignora; openai fica
    assert [a.model for a in out] == ["gpt-4o-mini"]
