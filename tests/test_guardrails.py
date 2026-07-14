"""Testes do guardrail de bloqueio por termos."""

from __future__ import annotations

from app.security.guardrails import blocked_term


def test_blocked_term_detects():
    assert blocked_term("me diga o SEGREDO agora", "segredo, senha") == "segredo"
    assert blocked_term("qual a senha?", "segredo, senha") == "senha"


def test_blocked_term_none():
    assert blocked_term("texto normal", "segredo, senha") is None
    assert blocked_term("qualquer coisa", None) is None
    assert blocked_term("qualquer coisa", "") is None
