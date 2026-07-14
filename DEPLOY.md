# Deploy — AegisFlow

Guia para colocar o AegisFlow em produção como SaaS gerenciado.

## Stack de produção
- **API**: container Python/FastAPI (uvicorn) — ver `Dockerfile`.
- **Postgres 16**: banco principal (multi-tenant, chaves, uso, assinaturas).
- **Redis Stack**: rate limiting, quota mensal e (futuro) cache vetorial.
- **Reverse proxy/TLS**: Nginx/Caddy/Traefik na frente, HTTPS obrigatório.

## Subir com Docker Compose (ambiente único)
```bash
cp .env.example .env      # preencha os valores de produção (ver abaixo)
docker compose up -d --build
docker compose logs -f api
```
O container aplica as migrations (`alembic upgrade head`) no boot e sobe a API na porta 8000.

## Variáveis de ambiente (produção)
```bash
AEGIS_ENV=production          # DESLIGA o modo dev (bypass de login não funciona)
AEGIS_LOG_JSON=true
DATABASE_URL=postgresql+asyncpg://USER:SENHA@HOST:5432/aegisflow
REDIS_URL=redis://HOST:6379/0

# Auth do painel
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_ANON_KEY=...        # anon public

# BYOK — chave mestra (KEK). NÃO gere/guarde em arquivo:
# use um secret manager (AWS KMS/Secrets Manager, GCP Secret Manager, Vault).
AEGIS_MASTER_KEY=<base64 de 32 bytes>

# Cache semântico (opcional): embeddings via Ollama ou serviço dedicado
AEGIS_EMBED_URL=http://ollama:11434
AEGIS_EMBED_MODEL=nomic-embed-text

# Segurança (LGPD)
AEGIS_PII_GUARD=true         # redige PII antes de enviar a provedores hospedados

# Billing
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
```

## Checklist de segurança (antes de ir ao ar)
- [ ] `AEGIS_ENV=production` (desliga o bypass de login dev).
- [ ] `AEGIS_MASTER_KEY` vinda de secret manager, **nunca** em git/imagem. Rotação planejada.
- [ ] Postgres e Redis com senha, rede privada, backups e TLS.
- [ ] HTTPS no proxy; HSTS; CORS restrito ao domínio do painel (hoje `*` fora de prod).
- [ ] Webhook do Stripe apontando para `/v1/billing/webhook` com `STRIPE_WEBHOOK_SECRET`.
- [ ] Logs sem PII/segredos (já garantido) + `AEGIS_PII_GUARD=true`.
- [ ] Rodar `pytest` e uma `security-review` do diff.

## Migrations
```bash
docker compose exec api alembic upgrade head      # aplicar
docker compose exec api alembic revision --autogenerate -m "descricao"  # nova
```

## Observabilidade
- `GET /health` (liveness) e `GET /health/ready` (readiness — checa o banco).
- `GET /metrics` (Prometheus): requests, cache hits, erros, tokens, economia.

## Pendências para GA (produção robusta)
- Migrar o **cache vetorial** de memória para Redis Stack (multi-instância).
- **Cloud KMS** gerenciado para a KEK (hoje é env var).
- Testes de carga + circuit breaker por provedor.
- Pix/boleto: conta Stripe BR com esses métodos habilitados (ou PSP local: Pagar.me/Asaas).
