"""
Agrupa artículos de diferentes fuentes que tratan sobre la misma noticia.
Usa fuzzy matching sobre títulos para detectar cobertura compartida.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import timedelta

from rapidfuzz import fuzz

from app.config import SIMILARITY_THRESHOLD
from app.models import Article, ArticleGroup

_STOPWORDS = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del", "al",
    "en", "con", "por", "para", "que", "se", "su", "sus", "es", "fue", "son",
    "como", "más", "pero", "ya", "muy", "sin", "sobre", "entre", "hasta",
    "desde", "tras", "ante", "bajo", "este", "esta", "estos", "estas", "ese",
    "esa", "esos", "esas", "hay", "ser", "han", "ha", "hoy", "qué", "cuál",
    "y", "o", "a", "e", "le", "lo", "no", "si", "mi", "me", "te", "nos",
    "buenos", "aires", "argentina", "argentino", "argentina", "gobierno",
    "lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo",
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
    "septiembre", "octubre", "noviembre", "diciembre", "2025", "2026",
    "vivo", "hoy", "ayer", "cuanto", "cotizan", "precio",
    "hora", "dia", "noche", "semana", "minuto", "asi", "paso",
    "sera", "todas", "todos", "donde", "cuando", "quien", "quienes",
    "dijo", "segun", "tambien", "puede", "hace", "tiene", "todo",
    "cada", "otra", "otro", "otras", "otros", "nuevo", "nueva",
}


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = re.sub(r"[\u0300-\u036f]", "", text)  # strip accents
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = [t for t in text.split() if t not in _STOPWORDS and len(t) > 2]
    return " ".join(tokens)


def _extract_key_tokens(text: str) -> set[str]:
    return set(_normalize(text).split())


def _titles_similar(a: str, b: str) -> float:
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0

    words_a = set(na.split())
    words_b = set(nb.split())
    common = words_a & words_b

    if len(common) < 2:
        return 0.0

    jaccard = len(common) / len(words_a | words_b)
    if jaccard < 0.3:
        return 0.0

    fuzzy = fuzz.token_sort_ratio(na, nb)

    return fuzzy * (0.85 + 0.15 * jaccard)


_DAILY_QUOTE_RE = re.compile(
    r"(d[oó]lar|blue|cotiza|riesgo pa[ií]s|merval|acciones|bonos"
    r"|cripto|bitcoin|soja|ma[ií]z|trigo|pesos?|moneda)",
    re.IGNORECASE,
)


def _is_daily_quote(art: Article) -> bool:
    """Detect articles about daily prices/quotes that shouldn't cross days."""
    return bool(_DAILY_QUOTE_RE.search(art.title))


def _time_compatible(a: Article, b: Article, max_hours: int = 48) -> bool:
    if not a.published or not b.published:
        return True
    if _is_daily_quote(a) or _is_daily_quote(b):
        return abs(a.published - b.published) <= timedelta(hours=14)
    return abs(a.published - b.published) <= timedelta(hours=max_hours)


def group_articles(articles: list[Article]) -> list[ArticleGroup]:
    """
    Group articles covering the same news story using fuzzy title matching.
    Returns groups sorted by number of sources (most coverage first), then by date.
    """
    if not articles:
        return []

    groups: list[list[Article]] = []
    assigned: set[int] = set()

    for i, art_a in enumerate(articles):
        if i in assigned:
            continue

        group = [art_a]
        assigned.add(i)
        sources_in_group = {art_a.source}

        for j, art_b in enumerate(articles):
            if j in assigned:
                continue
            if art_b.source in sources_in_group:
                continue
            if not _time_compatible(art_a, art_b):
                continue

            scores = [
                _titles_similar(member.title, art_b.title)
                for member in group
            ]
            avg_score = sum(scores) / len(scores)
            if avg_score >= SIMILARITY_THRESHOLD:
                group.append(art_b)
                assigned.add(j)
                sources_in_group.add(art_b.source)

        groups.append(group)

    result: list[ArticleGroup] = []
    for group in groups:
        representative = max(group, key=lambda a: len(a.summary))
        most_recent = max(
            (a.published for a in group if a.published),
            default=None,
        )
        gid = hashlib.md5(
            representative.title.encode()
        ).hexdigest()[:10]

        result.append(
            ArticleGroup(
                group_id=gid,
                representative_title=representative.title,
                representative_image=next(
                    (a.image for a in group if a.image), ""
                ),
                category=representative.category,
                published=most_recent,
                articles=sorted(group, key=lambda a: a.source),
            )
        )

    result.sort(
        key=lambda g: (
            -g.source_count,
            -(g.published.timestamp() if g.published else 0),
        )
    )

    return result
