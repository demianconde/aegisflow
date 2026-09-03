# aegis_side — Coletor de odds (isolado do gateway AegisFlow)

Worker que aproveita o servidor 24/7 do AegisFlow no Fly para **coletar odds**
(1X2, escanteios, cartoes) da SofaScore e acumular historico para calculo de CLV
(projeto Betflow). **Nao faz parte** da API do gateway: e um *process group*
separado (`collector`), sem HTTP, sem tocar no Postgres/Supabase.

## Garantias de isolamento
- Nao importa nada de `app/`. So usa `httpx` (ja no requirements) + `sqlite3`.
- Grava num SQLite proprio em volume Fly dedicado (`betflow_odds` → `/data`).
- So roda se voce escalar a maquina do processo `collector` (padrao: 0 = desligado).
- Nenhuma mudanca no comportamento da API; o deploy continua igual.

## Rodar local (teste)
```powershell
$env:BETFLOW_RAPIDAPI_KEY="<sua_chave>"; $env:BETFLOW_ODDS_DB=".\odds_test.db"
$env:BETFLOW_LEAGUES="E0"; $env:BETFLOW_MAX_FIXTURES="2"
python -m aegis_side.run_collector once
```

## Deploy no Fly (mesmo app aegisflow)

```bash
# 1. secrets do coletor (nao afetam o gateway)
fly secrets set -a aegisflow \
  BETFLOW_RAPIDAPI_KEY="<sua_chave_rapidapi>" \
  BETFLOW_LEAGUES="E0,BSA" BETFLOW_MAX_FIXTURES="10" \
  BETFLOW_COLLECT_INTERVAL="7200"

# 2. cria o volume do SQLite (1GB e de sobra) na regiao gru
fly volumes create betflow_odds --size 1 --region gru -a aegisflow

# 3. deploy normal (sobe API + define o novo process group)
fly deploy -a aegisflow --regions gru

# 4. LIGA o coletor (1 maquina do process group). A API segue com as dela.
fly scale count collector=1 -a aegisflow

# acompanhar / desligar
fly logs -a aegisflow                      # veja linhas "[collector ...]"
fly scale count collector=0 -a aegisflow   # desliga o coletor
```

## Baixar os dados coletados
```bash
fly ssh console -a aegisflow -C "cat /data/odds_history.db" > odds_history.db
# depois, no Betflow, analise offline (CLV) com pandas/scipy no seu PC.
```

## Custo
- 1 maquina extra `shared-cpu-1x` fica quase idle (dorme entre coletas).
- Consome cota RapidAPI: ~1 credito por jogo por rodada. Ajuste
  `BETFLOW_MAX_FIXTURES` e `BETFLOW_COLLECT_INTERVAL`.
