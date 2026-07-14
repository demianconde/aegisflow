# Deploy rápido no Railway — modo captação de lead

Caminho mais curto para colocar a **landing + formulário de interesse** no ar em
`aegisflow.tech`, sem gerenciar servidor. Só precisa da **API + Postgres**.

> Redis é **opcional** aqui: o rate limit é *fail-open* (`AEGIS_RATELIMIT_FAIL_CLOSED=false`),
> então sem Redis o lead ainda é gravado. Supabase e Stripe **não** são necessários para
> captar lead (só entram no painel/login e no billing).

## 1. Subir o código para o GitHub
```bash
git add -A
git commit -m "chore: config de deploy (Railway)"
git push
```
(Repo: `github.com/demianconde/aegisflow`.)

## 2. Criar o projeto no Railway
1. https://railway.app → **New Project → Deploy from GitHub repo** → selecione o repositório.
2. O Railway detecta o `Dockerfile`/`railway.json` e faz o build sozinho.
3. **+ New → Database → PostgreSQL** (banco gerenciado no mesmo projeto).

## 3. Variáveis de ambiente (serviço da API)
| Variável | Valor |
|---|---|
| `AEGIS_ENV` | `production` |
| `AEGIS_LOG_JSON` | `true` |
| `DATABASE_URL` | copie a do Postgres e **troque o esquema para `postgresql+asyncpg://...`** ⚠️ |
| `AEGIS_CORS_ORIGINS` | `https://aegisflow.tech,https://www.aegisflow.tech` |

> ⚠️ **Pegadinha:** o Railway entrega `DATABASE_URL` como `postgresql://...`, mas o app
> exige o driver async: insira `+asyncpg` logo após `postgresql`.

No boot, o container roda `alembic upgrade head` e cria a tabela `leads` automaticamente.

## 4. Testar no domínio temporário
O Railway gera um `*.up.railway.app`. Confirme:
- `GET /` → landing (200)
- `GET /health` → `{"status":"ok"}`
- Envie o formulário e verifique o lead no Postgres: `SELECT * FROM leads;`

## 5. Apontar o domínio da GoDaddy
No Railway: **Settings → Networking → Custom Domain** → adicione `www.aegisflow.tech`
(o Railway mostra um alvo CNAME). Na GoDaddy (**DNS Management**):

| Tipo | Nome | Valor |
|---|---|---|
| CNAME | `www` | `<alvo>.up.railway.app` (o que o Railway indicar) |

O apex/raiz (`aegisflow.tech`) **não** aceita CNAME na GoDaddy. Use **Domain Forwarding**
(`aegisflow.tech → https://www.aegisflow.tech`, redirecionamento permanente 301).
TLS/HTTPS é emitido automaticamente pelo Railway.

## Onde ficam os leads
Gravados no Postgres (tabela `leads`). Dá para consultar pelo console do banco no Railway
(`SELECT * FROM leads ORDER BY created_at DESC;`) ou, mais tarde, pelo console do dono em
`/gestaoaegis` (exige configurar `AEGIS_OWNER_EMAILS` + Supabase).

## Quando for além de captar lead
Para ligar login, BYOK e billing, siga o `DEPLOY.md` (Supabase, `AEGIS_MASTER_KEY` em secret
manager, Stripe webhook, Redis para cache semântico) e o checklist de segurança.
