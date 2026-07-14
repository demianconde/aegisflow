"""Guardrails de conteúdo (por tenant): bloqueio por termos + redação de PII.

A redação de PII vive em `security.pii`; aqui fica a política de bloqueio por termos.
"""

from __future__ import annotations


def blocked_term(text: str, blocked_csv: str | None) -> str | None:
    """Retorna o primeiro termo bloqueado encontrado no texto, ou None."""
    if not blocked_csv:
        return None
    low = text.lower()
    for term in (t.strip().lower() for t in blocked_csv.split(",")):
        if term and term in low:
            return term
    return None
