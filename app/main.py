"""Ponto de entrada da API do AegisFlow (FastAPI app factory)."""

from __future__ import annotations

import hmac
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
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
    seed,
    usage,
)
from app.config import get_settings
from app.logging_config import configure_logging, get_logger
from app.metrics import render_prometheus

PUBLIC_DIR = Path(__file__).parent / "public"

# Artigos do blog: slug (URL) -> arquivo em public/. Usado nas rotas e no sitemap.
ARTICLES = {
    "melhor-ferramenta-otimizacao-de-tokens-de-ia-no-brasil": "artigo-otimizacao-tokens.html",
    "token-maxxing-vazamento-de-caixa-em-ia": "artigo-token-maxxing.html",
    "o-que-e-llm-gateway-ai-proxy": "artigo-llm-gateway.html",
    "como-reduzir-custos-de-ia-guia-para-ctos": "artigo-reduzir-custos-ia.html",
    "ia-e-lgpd-como-usar-llms-em-conformidade": "artigo-ia-lgpd.html",
    "reduzir-custo-cursor-copilot-empresa": "artigo-cursor-copilot.html",
    "tokenizacao-e-uso-otimizado-de-ia-para-empresas": "artigo-tokenizacao-empresas.html",
    "ia-no-desenvolvimento-eficiencia-de-tokens-e-lgpd": "artigo-ia-dev-tokens-lgpd.html",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    log = get_logger("aegisflow")
    log.info("startup", version=__version__, env=settings.env)
    if settings.dev_bypass_enabled:
        log.warning(
            "DEV_BYPASS_ATIVO: login pode ser contornado. NUNCA use em produção "
            "(defina AEGIS_ENV=production e AEGIS_DEV_MODE=false)."
        )
    yield
    log.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    # Swagger/ReDoc/OpenAPI desativados — a documentação oficial é /documentacao.
    app = FastAPI(
        title="AegisFlow",
        version=__version__,
        description="LLM Gateway & Multi-Agent Proxy BYOK",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Compressão gzip (HTML/CSS/JS/JSON) — melhora LCP/Core Web Vitals.
    app.add_middleware(GZipMiddleware, minimum_size=500)

    # Em produção, use uma allowlist (AEGIS_CORS_ORIGINS). Em dev, libera tudo.
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
        """request_id no log + redirect www→apex + cache de estáticos + HSTS."""
        # Canonicalização de host: www.aegisflow.tech → aegisflow.tech (evita duplicação).
        host = request.headers.get("host", "")
        if host.startswith("www."):
            target = f"https://{host[4:]}{request.url.path}"
            if request.url.query:
                target += f"?{request.url.query}"
            return RedirectResponse(target, status_code=301)

        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        # Estáticos versionados (?v=) podem ser cacheados por muito tempo.
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    app.include_router(health.router)
    # Seed dashboard (so em dev mode)
    if settings.dev_bypass_enabled:
        app.include_router(seed.router)
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

    # HTML sempre revalidado (evita servir páginas antigas do cache do navegador).
    def _html(name: str) -> FileResponse:
        return FileResponse(PUBLIC_DIR / name, headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    async def landing() -> FileResponse:
        return _html("landing.html")

    @app.get("/login", include_in_schema=False)
    async def login_page() -> FileResponse:
        return _html("login.html")

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard_page() -> FileResponse:
        return _html("dashboard.html")

    @app.get("/documentacao", include_in_schema=False)
    @app.get("/docs-aegis", include_in_schema=False)
    async def docs_page() -> FileResponse:
        return _html("docs.html")

    # Blog / artigos (SEO). URLs "bonitas" mapeadas para arquivos em public/ via ARTICLES.
    @app.get("/artigos", include_in_schema=False)
    async def articles_index() -> FileResponse:
        return _html("artigos.html")

    @app.get("/artigos/{slug}", include_in_schema=False)
    async def article(slug: str) -> FileResponse:
        filename = ARTICLES.get(slug)
        if filename is None:
            raise HTTPException(status_code=404, detail="Artigo não encontrado")
        return _html(filename)

    @app.get("/termos", include_in_schema=False)
    async def terms_page() -> FileResponse:
        return _html("termos.html")

    @app.get("/privacidade", include_in_schema=False)
    async def privacy_page() -> FileResponse:
        return _html("privacidade.html")

    @app.get("/robots.txt", include_in_schema=False)
    async def robots() -> PlainTextResponse:
        return PlainTextResponse(
            "User-agent: *\nAllow: /\nSitemap: https://aegisflow.tech/sitemap.xml\n"
        )

    @app.get("/sitemap.xml", include_in_schema=False)
    async def sitemap() -> PlainTextResponse:
        base = "https://aegisflow.tech"
        lastmod = datetime.now(UTC).date().isoformat()
        paths = [("/", "1.0"), ("/artigos", "0.8")]
        paths += [(f"/artigos/{slug}", "0.9") for slug in ARTICLES]
        paths += [("/documentacao", "0.6"), ("/termos", "0.3"), ("/privacidade", "0.3")]
        urls = "".join(
            f"<url><loc>{base}{p}</loc><lastmod>{lastmod}</lastmod>"
            f"<changefreq>weekly</changefreq><priority>{prio}</priority></url>"
            for p, prio in paths
        )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{urls}</urlset>"
        )
        return PlainTextResponse(xml, media_type="application/xml")

    # Console do dono — rota "secreta" (não linkada em lugar nenhum). Aceita a
    # versão acentuada e a ASCII (a acentuada é percent-encoded pelo browser).
    @app.get("/gestaoaegis", include_in_schema=False)
    @app.get("/gestãoaegis", include_in_schema=False)
    async def owner_console() -> FileResponse:
        return _html("owner.html")

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
