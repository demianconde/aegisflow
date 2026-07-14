"""Ponto de entrada da API do NexusGate (FastAPI app factory)."""

from __future__ import annotations

import hmac
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import (
    admin,
    billing,
    chat,
    health,
    leads,
    openai_compat,
    owner,
    playground,
    provider_keys,
    proxy,
    usage,
)
from app.config import get_settings
from app.logging_config import configure_logging, get_logger
from app.metrics import render_prometheus

PUBLIC_DIR = Path(__file__).parent / "public"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    log = get_logger("nexusgate")
    log.info("startup", version=__version__, env=settings.env)
    if settings.dev_bypass_enabled:
        log.warning(
            "DEV_BYPASS_ATIVO: login pode ser contornado. NUNCA use em produção "
            "(defina NEXUS_ENV=production e NEXUS_DEV_MODE=false)."
        )
    yield
    log.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    # Em produção, esconde a documentação interativa (reduz o mapa da API p/ atacante).
    docs_kwargs: dict = {}
    if settings.is_production:
        docs_kwargs = {"docs_url": None, "redoc_url": None, "openapi_url": None}
    app = FastAPI(
        title="NexusGate",
        version=__version__,
        description="LLM Gateway & Multi-Agent Proxy BYOK",
        lifespan=lifespan,
        **docs_kwargs,
    )

    # Em produção, use uma allowlist (NEXUS_CORS_ORIGINS). Em dev, libera tudo.
    cors_origins = settings.cors_origin_list if settings.is_production else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=cors_origins != ["*"],  # credenciais não combinam com "*"
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Injeta um request_id no contexto de log de cada requisição."""
        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        return response

    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(provider_keys.router)
    app.include_router(usage.router)
    app.include_router(billing.router)
    app.include_router(playground.router)
    app.include_router(leads.router)
    app.include_router(owner.router)
    app.include_router(chat.router)
    app.include_router(openai_compat.router)
    app.include_router(proxy.router)

    @app.get("/", include_in_schema=False)
    async def landing() -> FileResponse:
        return FileResponse(PUBLIC_DIR / "landing.html")

    @app.get("/login", include_in_schema=False)
    async def login_page() -> FileResponse:
        return FileResponse(PUBLIC_DIR / "login.html")

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard_page() -> FileResponse:
        return FileResponse(PUBLIC_DIR / "dashboard.html")

    # Console do dono — rota "secreta" (não linkada em lugar nenhum). Aceita a
    # versão acentuada e a ASCII (a acentuada é percent-encoded pelo browser).
    @app.get("/gestaonexus", include_in_schema=False)
    @app.get("/gestãonexus", include_in_schema=False)
    async def owner_console() -> FileResponse:
        return FileResponse(PUBLIC_DIR / "owner.html")

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> PlainTextResponse:
        token = settings.metrics_token
        # Em produção, exige token; sem token configurado, o endpoint fica desabilitado.
        if settings.is_production and not token:
            return PlainTextResponse("not found", status_code=404)
        if token:
            auth = request.headers.get("authorization", "")
            if not hmac.compare_digest(auth, f"Bearer {token}"):
                return PlainTextResponse("unauthorized", status_code=401)
        return PlainTextResponse(render_prometheus())

    @app.get("/public-config", include_in_schema=False)
    async def public_config() -> JSONResponse:
        """Config pública (anon key do Supabase é destinada ao browser).

        NÃO expõe o token de bypass — o sentinela é público por natureza (JS) e só
        funciona quando o backend está com dev_bypass ligado (jamais em produção).
        """
        return JSONResponse(
            {
                "supabase_url": settings.supabase_url or "",
                "supabase_anon_key": settings.supabase_anon_key or "",
                "configured": bool(settings.supabase_url and settings.supabase_anon_key),
                "dev_mode": settings.dev_bypass_enabled,
            }
        )

    # Assets estáticos (css/js/vendor) em app/public/.
    app.mount("/static", StaticFiles(directory=PUBLIC_DIR), name="static")

    return app


app = create_app()
