"""Servidor web local do Betflow (bet tracker / paper trading).

Expoe o motor estatistico via HTTP e mantem banca + historico de apostas em
SQLite. NAO coloca apostas reais em casas (elas nao expoem API publica p/ isso):
o fluxo e sugerir o value bet + stake (Kelly), voce aposta na casa e REGISTRA
aqui para acompanhar banca, ROI e P&L.
"""
from betflow.web.app import create_app

__all__ = ["create_app"]
