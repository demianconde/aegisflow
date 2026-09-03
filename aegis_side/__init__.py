"""aegis_side — coletor de odds isolado (NAO faz parte do gateway AegisFlow).

Este pacote e deliberadamente independente de `app/`: nao importa nada do
gateway, nao toca no banco Postgres/Supabase e usa apenas httpx (ja presente
nas dependencias). Roda como um PROCESS GROUP separado no Fly ("collector"),
capturando snapshots de odds esportivas num SQLite proprio (em volume Fly).

Objetivo: aproveitar o servidor 24/7 do AegisFlow para acumular historico de
odds (1X2, escanteios, cartoes) sem risco algum ao produto principal.
"""
