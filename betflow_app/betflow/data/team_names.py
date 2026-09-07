"""Normalizacao de nomes de times entre fontes.

A SofaScore usa nomes "oficiais" ("Liverpool FC", "Nottingham Forest",
"Manchester City"), enquanto o football-data (base de treino do Dixon-Coles)
usa formas curtas ("Liverpool", "Nott'm Forest", "Man City"). Sem casar os dois,
o modelo nao reconhece o jogo.

Estrategia em camadas:
1. mapa explicito para os casos dificeis (aliases conhecidos);
2. normalizacao (minusculas, remove FC/AFC/acentos/pontuacao);
3. correspondencia fuzzy (difflib) contra os nomes que o modelo conhece.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

# aliases explicitos: nome SofaScore (normalizado) -> nome football-data
# (so os que a heuristica erraria; o resto e resolvido por fuzzy)
_ALIASES = {
    "nottingham forest": "Nott'm Forest",
    "manchester city": "Man City",
    "manchester united": "Man United",
    "manchester utd": "Man United",
    "tottenham hotspur": "Tottenham",
    "wolverhampton": "Wolves",
    "wolverhampton wanderers": "Wolves",
    "brighton hove albion": "Brighton",
    "brighton and hove albion": "Brighton",
    "west ham united": "West Ham",
    "newcastle united": "Newcastle",
    "leicester city": "Leicester",
    "ipswich town": "Ipswich",
    "leeds united": "Leeds",
    "sheffield united": "Sheffield United",
    "luton town": "Luton",
}

# palavras de sufixo/prefixo genericas removidas na normalizacao
_STOPWORDS = {"fc", "afc", "cf", "sc", "club", "ac", "the"}


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize(name: str) -> str:
    """Forma canonica para comparacao: minusc, sem acento/pontuacao/stopwords."""
    if not name:
        return ""
    txt = _strip_accents(name).lower()
    txt = re.sub(r"[^a-z0-9\s]", " ", txt)          # tira pontuacao
    tokens = [t for t in txt.split() if t not in _STOPWORDS]
    return " ".join(tokens).strip()


def match_team(sofa_name: str, known: list[str],
               threshold: float = 0.6) -> str | None:
    """Mapeia um nome da SofaScore para um nome conhecido pelo modelo.

    Retorna None se nenhuma correspondencia atingir o `threshold` (evita casar
    time errado). `known` sao os nomes que o Dixon-Coles conhece.
    """
    norm = normalize(sofa_name)
    if not norm:
        return None

    # 1) alias explicito
    if norm in _ALIASES and _ALIASES[norm] in known:
        return _ALIASES[norm]

    # 2) match exato apos normalizar ambos os lados
    norm_known = {normalize(k): k for k in known}
    if norm in norm_known:
        return norm_known[norm]

    # 3) alias -> tenta casar o valor do alias por normalizacao
    if norm in _ALIASES:
        alias_norm = normalize(_ALIASES[norm])
        if alias_norm in norm_known:
            return norm_known[alias_norm]

    # 4) fuzzy com trava anti-erro. Tokens genericos ('united', 'city'...) sao
    # comuns a varios times; casar por eles daria "Leeds United" -> "Man United".
    # Exigimos que exista pelo menos UM token DISTINTIVO em comum para aceitar
    # qualquer correspondencia fuzzy.
    generic = {"united", "city", "town", "wanderers", "albion", "rovers",
               "athletic", "hotspur", "county", "fc", "afc"}
    norm_tokens = set(norm.split())
    best, best_score = None, 0.0
    for kn, original in norm_known.items():
        kn_tokens = set(kn.split())
        shared_distinct = (norm_tokens & kn_tokens) - generic
        if not shared_distinct:
            continue  # sem token distintivo em comum -> nunca casa por fuzzy
        score = SequenceMatcher(None, norm, kn).ratio()
        if norm in kn or kn in norm:
            score = max(score, 0.85)
        if score > best_score:
            best, best_score = original, score
    return best if best_score >= threshold else None
