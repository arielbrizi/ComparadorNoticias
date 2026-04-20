"""Query normalization helpers for text and AI search.

Extracts content-bearing keywords from natural-language queries like
"últimos detalles de la guerra" or "dame el status del dólar hoy" so
downstream matching doesn't require every stopword to appear in the
article title/summary.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

from app.article_grouper import _STOPWORDS as _BASE_STOPWORDS

if TYPE_CHECKING:
    from app.models import ArticleGroup


# Extra stopwords common in conversational/interrogative search queries
# (commands, filler words, time hints). Kept separate from article_grouper
# _STOPWORDS so we don't affect grouping behavior.
_SEARCH_EXTRA_STOPWORDS: set[str] = {
    "dame", "decime", "contame", "mostrame", "traeme", "quiero", "necesito",
    "por favor", "porfa", "porfavor",
    "ultimo", "ultimos", "ultima", "ultimas",
    "status", "estado", "estados", "situacion", "situaciones",
    "novedad", "novedades", "detalle", "detalles", "resumen", "resumenes",
    "info", "informacion", "informe", "informes",
    "noticia", "noticias", "nota", "notas",
    "pasa", "pasando", "paso", "pasado", "pasaron",
    "acerca", "respecto", "relacionado", "relacionados",
    "cual", "cuales", "cuanto", "cuantos", "cuanta", "cuantas",
    "porque", "por que",
}


_STOPWORDS_NORMALIZED: set[str] = set()
for _sw in list(_BASE_STOPWORDS) + list(_SEARCH_EXTRA_STOPWORDS):
    _STOPWORDS_NORMALIZED.add(_sw.lower())


_MIN_KEYWORD_LEN = 3


def _strip_accents(text: str) -> str:
    """Lowercase + remove accents for accent-insensitive comparison."""
    lowered = text.lower()
    decomposed = unicodedata.normalize("NFD", lowered)
    return re.sub(r"[\u0300-\u036f]", "", decomposed)


def _tokenize(query: str) -> list[str]:
    """Split a query into lowercase tokens, keeping original accents/casing lost."""
    cleaned = re.sub(r"[^\w\s]", " ", query, flags=re.UNICODE)
    return [t for t in cleaned.lower().split() if t]


def extract_keywords(query: str) -> list[str]:
    """Return content-bearing keywords from a natural-language query.

    - Lowercases and strips punctuation.
    - Removes Spanish stopwords (accent-insensitive match) and short tokens.
    - Preserves original token order, deduplicated.
    - Returns an empty list if the query has no content-bearing tokens
      (e.g. "qué pasa hoy"). Callers should treat that as "no results".
    """
    if not query or not query.strip():
        return []

    tokens = _tokenize(query)
    if not tokens:
        return []

    keywords: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        if len(tok) < _MIN_KEYWORD_LEN:
            continue
        normalized = _strip_accents(tok)
        if normalized in _STOPWORDS_NORMALIZED:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        keywords.append(tok)

    return keywords


def normalized_query_key(query: str) -> str:
    """Canonical lowercase+accent-stripped key for caches.

    Two semantically equivalent queries like "Guerra" and "guerra" share
    the same key. Intended as a *secondary* cache key alongside the raw
    `query.strip().lower()` used today, not a replacement.
    """
    return _strip_accents(query.strip())


def group_matches_keywords(group: "ArticleGroup", keywords: list[str]) -> bool:
    """True if any keyword appears in the group's title, article titles,
    or article summaries (accent-insensitive)."""
    if not keywords:
        return False
    haystack_parts = [group.representative_title or ""]
    for a in group.articles:
        haystack_parts.append(a.title or "")
        haystack_parts.append(a.summary or "")
    haystack = _strip_accents(" ".join(haystack_parts))
    for kw in keywords:
        if _strip_accents(kw) in haystack:
            return True
    return False


def prioritize_groups_by_keywords(
    groups: list["ArticleGroup"], keywords: list[str],
) -> list["ArticleGroup"]:
    """Return groups reordered so keyword-matching ones come first.

    Preserves original relative order within each partition. Useful to
    ensure keyword-relevant groups fit in a truncated prompt context
    (e.g. Groq's 10k-char budget).
    """
    if not keywords:
        return list(groups)
    matching: list[ArticleGroup] = []
    others: list[ArticleGroup] = []
    for g in groups:
        (matching if group_matches_keywords(g, keywords) else others).append(g)
    return matching + others


def build_fallback_summary(
    matched_titles: list[str], keywords: list[str], total: int | None = None,
) -> str:
    """Build a short natural-language summary from matched article titles.

    Used when the AI provider returns `has_results: false` but the DB text
    search did find relevant groups — avoids the UX glitch of showing the
    AI's negative message alongside positive results.
    """
    matched_titles = [t.strip() for t in matched_titles if t and t.strip()]
    if not matched_titles:
        return ""
    n = total if total is not None else len(matched_titles)
    sample = matched_titles[:3]
    noun = "noticia" if n == 1 else "noticias"
    kw_phrase = ""
    if keywords:
        kw_phrase = f" sobre {', '.join(keywords[:3])}"
    headline_list = "; ".join(f"«{t}»" for t in sample)
    return (
        f"Encontramos {n} {noun}{kw_phrase}. "
        f"Principales titulares: {headline_list}."
    )
