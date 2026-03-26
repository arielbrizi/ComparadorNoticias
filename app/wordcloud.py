"""
Genera datos para la nube de palabras a partir de los títulos de las noticias.
Extrae frecuencia de términos filtrando stopwords y tokens cortos.
Preserva acentos y ñ para mostrar palabras en español correcto.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.article_grouper import _STOPWORDS
from app.models import Article

MAX_WORDS = 80


def _strip_accents(text: str) -> str:
    """Remove diacritics (but keep ñ) for stopword matching only."""
    nfkd = unicodedata.normalize("NFD", text)
    return re.sub(r"[\u0300-\u036f]", "", nfkd)


_STOPWORDS_EXPANDED: set[str] = set()
for _sw in _STOPWORDS:
    _STOPWORDS_EXPANDED.add(_sw)
    _STOPWORDS_EXPANDED.add(_strip_accents(_sw))


def _tokenize_display(text: str) -> list[str]:
    """Lowercase, strip punctuation, filter stopwords — but keep accents and ñ."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    tokens = []
    for t in text.split():
        if len(t) <= 2:
            continue
        if t in _STOPWORDS_EXPANDED or _strip_accents(t) in _STOPWORDS_EXPANDED:
            continue
        tokens.append(t)
    return tokens


def build_wordcloud(
    articles: list[Article],
    hours: int = 24,
) -> list[list[str | int]]:
    """Return the most frequent terms from recent article titles.

    Each element is ``[word, frequency]``, sorted by frequency descending.
    Only articles published within the last *hours* are considered.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    counter: Counter[str] = Counter()
    for art in articles:
        if art.published and art.published.replace(tzinfo=art.published.tzinfo or timezone.utc) < cutoff:
            continue
        tokens = _tokenize_display(art.title)
        counter.update(tokens)

    return [[word, count] for word, count in counter.most_common(MAX_WORDS)]
