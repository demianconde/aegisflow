# Stripe — Homologação (test mode)

## Connect é necessário? **Não.**
A doc do Stripe é explícita: *"If you're a subscription-based SaaS business, but don't
extend Stripe products or payment processing to your merchants, you don't need Connect."*

O AegisFlow cobra **assinatura própria** dos tenants (modelo BYOK zero-markup). Isso é
**Stripe Billing** (Checkout + Subscriptions) — já implementado. **Connect** só entraria se
os seus clientes fossem receber pagamentos dos **clientes deles** (marketplace), o que não
é o caso.

## Passo a passo (homologação / test mode)

1. **Conta Stripe** em modo de **teste** (toggle "Test mode" no dashboard).
2. **Chaves de teste** — Developers → API keys:
   - `STRIPE_SECRET_KEY=sk_test_...`
3. **Preencha o `.env`:**
   ```bash
   STRIPE_SECRET_KEY=sk_test_xxx
   AEGIS_STRIPE_PAYMENT_METHODS=card     # cartão é o método recorrente confiável
   ```
4. **Smoke test** (cria uma sessão de checkout de teste e imprime a URL):
   ```bash
   python scripts/stripe_smoke.py pro
   ```
   Abra a URL e pague com um cartão de teste: `4242 4242 4242 4242`, validade futura, CVC qualquer.
5. **Webhook** — encaminhe eventos para a API local com o Stripe CLI:
   ```bash
   stripe login
   stripe listen --forward-to http://localhost:8000/v1/billing/webhook
   # copie o "whsec_..." que ele imprime para o .env:
   #   STRIPE_WEBHOOK_SECRET=whsec_...
   ```
   Reinicie a API. Ao concluir um checkout de teste, o evento
   `checkout.session.completed` atualiza o plano do tenant (via metadata).

## Fluxo no produto
- Painel → aba **Plano & Cobrança** → escolher plano pago → a API cria a sessão de
  Checkout (`POST /v1/admin/billing/plan`) e redireciona para o Stripe.
- Após pagamento, o **webhook** (`POST /v1/billing/webhook`) aplica o plano
  (`checkout.session.completed` / `customer.subscription.updated`).
- Em **produção sem Stripe**, o upgrade pago é bloqueado (501) — nunca é liberado de graça.

## Notas de pagamento (Brasil)
- **Cartão**: suporta assinatura recorrente. Use este em homologação.
- **Pix**: é **one-time** no Stripe — **não** suporta subscription recorrente. Para Pix
  recorrente seria preciso um fluxo de faturas one-time por período (ou um PSP local como
  Pagar.me/Asaas). Fica como evolução; não bloqueia a homologação por cartão.
- **Boleto**: suportado em alguns fluxos BR; se quiser testar, use
  `AEGIS_STRIPE_PAYMENT_METHODS=card,boleto`.

## Cartões de teste úteis
- Sucesso: `4242 4242 4242 4242`
- Requer autenticação (3DS): `4000 0025 0000 3155`
- Recusado: `4000 0000 0000 9995`
