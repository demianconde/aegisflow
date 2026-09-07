"""Subprojeto isolado, montado em /betflow.

Pacote autocontido: mantem seus proprios modulos (`betflow`, `config`) e banco
SQLite proprio, sem tocar no gateway principal. O carregamento e sempre
protegido (ver mount()) para que uma falha aqui nunca derrube a app principal.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# Deixa `betflow.*` e `config` (usados internamente pelo subprojeto) resolviveis
# sem colidir com o namespace do gateway (que usa `app.config`).
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def build_asgi():
    """Cria a app Flask do subprojeto e a embrulha como ASGI (WSGI->ASGI).

    Retorna a app ASGI pronta para `app.mount("/betflow", ...)`. A app Flask ja
    inicializa seu banco (store.init_db) na criacao. Levanta se algo faltar; o
    chamador (app.main) trata com try/except para nao contaminar o gateway.
    """
    from a2wsgi import WSGIMiddleware

    from betflow.web import create_app

    flask_app = create_app()
    return WSGIMiddleware(flask_app)
