# NexusGate

**LLM Gateway & Multi-Agent Proxy BYOK** — SaaS gerenciado que fica entre as aplicações dos clientes e os provedores de IA (OpenAI, Anthropic, Qwen), agregando: proxy BYOK, roteamento por custo, cache semântico e billing multi-tenant.

> Reescrita do protótipo `nexusGate` como produto real. Stack oficial: **Python 3.11 + FastAPI**. Entrega: **SaaS gerenciado**. Auth do painel: **Supabase**.

**Posicionamento:** gateway de IA **feito para o Brasil** — LGPD by design, dados no Brasil, PT-BR, preços em **BRL**, **Pix/boleto** e NF-e; **BYOK zero markup** sobre tokens; e **economia auditável** (relatório de ROI exportável). Paridade técnica com os concorrentes (fallback, observabilidade, cache, guardrails/PII) + diferenciação por região, compliance e transparência de preço.

## Arquitetura (resumo)

```
[App do cliente] --(x-api-key)--> [NexusGate API / FastAPI]
   Supabase (auth do painel)          | resolve tenant + descriptografa chave BYOK
   [Router de custo] -> [Cache semântico (Redis)] -> [Provider] -> OpenAI/Anthropic/Qwen (streaming)
   [Postgres: tenants, api_keys, provider_keys, usage_logs, subscriptions]   [Stripe]   [Dashboard]
```

Dois planos de autenticação distintos:
- **Painel**: usuários logam via Supabase.
- **Proxy de dados**: apps clientes autenticam com uma `x-api-key` do NexusGate (prefixo + hash).

## Requisitos
- Docker + Docker Compose (recomendado), ou
- Python 3.11, Postgres 16 e Redis Stack locais.

## Como rodar (Docker)

```bash
cp .env.example .env    # ajuste as variáveis
make up                 # sobe postgres + redis-stack + api (aplica migrations)
make logs               # acompanha a API
curl http://localhost:8000/health
```

## Como rodar (local, sem Docker)

```bash
make install
# suba Postgres e Redis (ex.: via docker compose up -d postgres redis)
make migrate            # alembic upgrade head
make dev                # uvicorn com reload
```

## Comandos úteis (Makefile)
- `make migrate` — aplica migrations
- `make revision m="mensagem"` — gera nova migration (autogenerate)
- `make lint` / `make test` — ruff / pytest

## API

**Painel (auth Supabase — `Authorization: Bearer <jwt>`):**
- `GET /v1/admin/me` — perfil do usuário + tenant
- `GET /v1/admin/keys` — lista as chaves de API do tenant
- `POST /v1/admin/keys` `{ "name": "..." }` — cria chave (retorna o valor em claro **só aqui**)
- `DELETE /v1/admin/keys/{id}` — revoga a chave

**Painel — credenciais BYOK (auth Supabase):**
- `GET /v1/admin/providers` — provedores conhecidos (atalhos de base_url/format)
- `GET/POST/DELETE /v1/admin/provider-keys` — CRUD das credenciais de provedor (cifradas at-rest). Campos: `provider`, `api_key` (opcional p/ locais), `base_url` (opcional), `format`, `label` (Nome da API), `default_model`.

**Proxy / plano de dados (auth `x-api-key: nxg_....`):**
- `GET /v1/whoami` — resolve o tenant a partir da chave (após rate limit).
- `POST /v1/chat/completions` — proxy compatível com o formato OpenAI. Resolve a credencial BYOK do tenant, chama o provedor real (qualquer LLM/local), faz streaming SSE (`stream: true`) e grava `usage_logs`. O provedor é inferido do modelo ou informado em `provider`.
  - **`model: "nexus-auto"`** ativa o roteamento: classifica a complexidade e aplica **local-first com escalonamento** — tenta o provedor **local** primeiro (gratuito) e, se falhar, **escala** para o hospedado (o **premium** em tarefas complexas). Headers de resposta expõem a decisão: `x-nexus-model`, `x-nexus-provider`, `x-nexus-complexity`, `x-nexus-routed` (`auto`/`escalated`), `x-nexus-local`.

> **Qualquer LLM, inclusive local:** cadastre uma credencial com `provider` conhecido (usa base_url padrão) ou `custom`/local com `base_url` própria (ex.: Ollama em `http://localhost:11434/v1`, sem API key). O painel detecta automaticamente se o endpoint é **local** e exibe o **modelo** configurado.
>
> **Ollama:** o provider `ollama` usa o `format: "ollama"` (API nativa `/api/chat`), com **thinking desligado** (`think: false`) e **contexto fixo** (`num_ctx: 8192`) — evita o crash de modelos com contexto declarado gigante (ex.: 262k) e mantém respostas rápidas. Defina o `default_model` para um modelo instalado (`ollama list`).

Rate limiting por tenant (janela de 1 min): free=60, pro=600, enterprise=6000 req/min.

## Login & Painel (Supabase)

Páginas web (sem build, sem CDN — Supabase JS servido localmente em `/static/vendor/`):
- `/` — landing (página de vendas)
- `/login` — login/cadastro via Supabase (senha, cadastro e link mágico)
- `/dashboard` — painel protegido: perfil/plano e CRUD de chaves de API
- `/public-config` — expõe ao browser a URL + anon key do Supabase (a anon key é pública por design)

**Para habilitar o login**, crie um projeto no [Supabase](https://supabase.com), pegue em *Project Settings → API* a **Project URL** e a **anon public key**, e preencha no `.env`:

```bash
SUPABASE_URL=https://xxxxxxxx.supabase.co
SUPABASE_ANON_KEY=eyJ...   # anon public (não a service_role)
```

Reinicie a API. Sem essas variáveis, `/login` mostra um aviso e o backend rejeita tokens (o painel exige um usuário Supabase válido). O fluxo: usuário loga no Supabase → recebe JWT → o painel chama `/v1/admin/*` com `Authorization: Bearer <jwt>` → o backend valida no Supabase e provisiona tenant/usuário no primeiro acesso.

### Modo desenvolvimento (acessar o painel sem login)

Para validar o painel sem configurar o Supabase, ative o modo dev no `.env`:

```bash
NEXUS_DEV_MODE=true
DATABASE_URL=sqlite+aiosqlite:///./nexus_dev.db   # dev sem Postgres/Docker
```

Rode `alembic upgrade head` e reinicie. Em `/login` aparece o botão **"Entrar sem login (dev)"**, que dá acesso a um tenant/usuário de desenvolvimento. O bypass usa o token sentinela `dev-local-access` (header `Authorization: Bearer dev-local-access`) e **só funciona quando `NEXUS_DEV_MODE=true` e `NEXUS_ENV` ≠ `production`** — em produção é ignorado.

## Estrutura
```
app/
  config.py            # settings (pydantic-settings)
  logging_config.py    # logging estruturado (sem PII/segredos)
  main.py              # app factory FastAPI + middleware de request_id
  redis_client.py      # conexão Redis compartilhada
  ratelimit.py         # rate limiting por tenant
  auth/                # supabase (painel) + api_key (proxy) + parsing de token
  db/                  # engine async, sessão e modelos ORM
  api/                 # rotas: health, admin (painel), proxy (dados)
alembic/               # migrations
tests/                 # pytest
```

## Roadmap (fases)
- **F0 Fundação** ✅ scaffold, DB async + Alembic, /health, Docker, CI
- **F1 Multi-tenant + Auth** ✅ Supabase real, provisionamento de tenants/users, CRUD de x-api-key, rate-limiting por tenant
- **F2 Proxy BYOK real** ✅ envelope encryption (AES-256-GCM), CRUD de credenciais de provedor, `/v1/chat/completions` com streaming e gravação de uso. Suporta **qualquer LLM**: OpenAI-compatível (OpenAI, Qwen, Groq, DeepSeek, Together, OpenRouter, Gemini) e **locais** (Ollama, LM Studio, vLLM, LocalAI), além de Anthropic.
- **F3 Roteamento + Economia** ✅ catálogo de preços (60+ LLMs), aba **Uso & Economia** (tokens/custo por LLM + comparação "se tudo rodasse no mesmo LLM") e **`nexus-auto`**: roteamento por complexidade **local-first com escalonamento** (local primeiro; se não der conta, escala para o pago), com `cost_saved` gravado
- **F4 Cache semântico real** ✅ embeddings locais (Ollama) + store vetorial por tenant; serve prompts similares sem chamar o provedor (`x-nexus-cache: hit`) e contabiliza a economia
- **F5 Billing** ✅ planos em BRL (quotas mensais + rpm por plano), aba **Plano & Cobrança**, checkout Stripe com **Pix/boleto/cartão** (opcional, atrás de chave) + modo dev
- **F6 Hardening** ✅ redação de PII (LGPD) + guardrail para provedores hospedados, `/metrics` (Prometheus), retries em erros transitórios
- **Captação de leads + console do dono** ✅ landing com **formulário de interesse** (pré-lançamento, sem login); `POST /v1/leads` público; **console do dono** oculto em `/gestaonexus` (login Google/Supabase, gate por `NEXUS_OWNER_EMAILS`) com MRR, assinaturas e gestão de leads
- **Recursos avançados** ✅ observabilidade (logs de requisição + tracing, opt-in de prévias com redação PII); `/v1/models` e `/v1/embeddings` (OpenAI-compat, BYOK); **chaves virtuais** (orçamento mensal, rpm e allowlist de modelos por chave); **Playground** no painel; **fallback** configurável (`fallback: ["provider:model", ...]`)
- **Deploy / GA** 🚧 ver [DEPLOY.md](DEPLOY.md) — cache vetorial em Redis, cloud KMS, testes de carga, Pix em produção

## Segurança
- Nunca comitar `.env` nem segredos. Chaves BYOK são criptografadas at-rest (envelope encryption, Fase 2).
- Logs não registram prompts, segredos ou PII.
