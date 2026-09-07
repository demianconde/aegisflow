"""Boosted Research — segunda linha de predicao do Betflow.

Vertente independente do motor principal (Dixon-Coles / Betano). Combina:
  * Elo bivariado dinamico (forca de ataque e defesa por time, atualizado
    rodada a rodada pelo gradiente Poisson, com ajuste pela forca do oponente);
  * um refinador Gradient Boosting (XGBoost) que cruza a probabilidade "crua"
    do modelo-base com fatores contextuais (fadiga por dias de descanso, forma
    recente, mando de campo);
  * um motor de valor esperado (+EV) que compara as probabilidades do modelo
    com as odds de referencia (Pinnacle) e so emite sinal acima de uma margem
    de seguranca (default 4%).

O track record fica na mesma tabela `suggestions` marcado com strategy='boosted',
para comparacao direta, na mesma regua, com o motor principal (strategy='main').
"""
