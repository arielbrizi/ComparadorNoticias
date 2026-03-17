"""
Comparador de contenido entre artículos de diferentes fuentes.
En lugar de diffs palabra a palabra, analiza qué información
es exclusiva de cada fuente y qué comparten.
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

from app.models import Article


def _normalize_for_compare(text: str) -> str:
    text = text.lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = re.sub(r"[\u0300-\u036f]", "", text)
    return text


def _split_sentences(text: str) -> list[str]:
    if not text:
        return []
    raw = re.split(r'(?<=[.!?…])\s+', text.strip())
    sentences = []
    for s in raw:
        s = s.strip()
        if len(s) > 15:
            sentences.append(s)
    return sentences


def _sentence_is_in(sentence: str, other_text: str, threshold: int = 60) -> bool:
    norm_s = _normalize_for_compare(sentence)
    norm_t = _normalize_for_compare(other_text)

    if not norm_s or not norm_t:
        return False

    if norm_s in norm_t:
        return True

    other_sentences = _split_sentences(other_text)
    for other_s in other_sentences:
        norm_o = _normalize_for_compare(other_s)
        score = fuzz.token_sort_ratio(norm_s, norm_o)
        if score >= threshold:
            return True
        if fuzz.partial_ratio(norm_s, norm_o) >= 75:
            return True

    return False


def _extract_key_data(text: str) -> list[str]:
    """Extract numbers, percentages, quotes, and proper nouns as key data points."""
    data_points = []

    numbers = re.findall(
        r'(?:\$\s?)?[\d.,]+\s?(?:%|por ciento|puntos|millones|billones|pesos|dólares|USD)',
        text, re.IGNORECASE
    )
    data_points.extend(numbers)

    quotes = re.findall(r'[""«]([^""»]{10,})[""»]', text)
    data_points.extend(quotes)
    quotes2 = re.findall(r'"([^"]{10,})"', text)
    data_points.extend(quotes2)

    return data_points


def compare_group_articles(articles: list[Article]) -> dict:
    """
    Analyze a group of articles to find:
    - How each source frames the story (headline analysis)
    - What info is exclusive to each source
    - What key data each source includes
    """
    if not articles:
        return {"sources": [], "analysis": {}}

    sources_data = []

    all_summaries = {a.source: a.summary or "" for a in articles}

    for art in articles:
        sentences = _split_sentences(art.summary or "")
        other_sources = [
            a for a in articles if a.source != art.source
        ]
        other_text = " ".join(a.summary or "" for a in other_sources)

        exclusive_sentences = []
        shared_sentences = []
        for sent in sentences:
            if other_sources and not _sentence_is_in(sent, other_text):
                exclusive_sentences.append(sent)
            else:
                shared_sentences.append(sent)

        key_data = _extract_key_data(art.summary or "")
        other_data_text = " ".join(a.summary or "" for a in other_sources).lower()
        exclusive_data = [
            d for d in key_data
            if d.lower() not in other_data_text
        ]

        sources_data.append({
            "source": art.source,
            "source_color": art.source_color,
            "title": art.title,
            "summary": art.summary or "",
            "link": art.link,
            "image": art.image,
            "published": art.published.isoformat() if art.published else None,
            "exclusive_content": exclusive_sentences,
            "shared_content": shared_sentences,
            "exclusive_data": exclusive_data,
        })

    titles = [a.title for a in articles]
    headline_analysis = _analyze_headlines(titles, [a.source for a in articles])

    has_exclusive = any(
        s["exclusive_content"] or s["exclusive_data"]
        for s in sources_data
    )

    return {
        "sources": sources_data,
        "headline_analysis": headline_analysis,
        "has_exclusive_content": has_exclusive,
        "source_count": len(articles),
    }


def _analyze_headlines(titles: list[str], sources: list[str]) -> dict:
    """Analyze how each source frames the same story via their headline."""
    if len(titles) < 2:
        return {"different_framing": False, "details": []}

    unique_titles = len(set(titles))
    framing_details = []

    for title, source in zip(titles, sources):
        tone = _detect_tone(title)
        focus = _detect_focus(title)
        framing_details.append({
            "source": source,
            "title": title,
            "tone": tone,
            "focus": focus,
        })

    return {
        "different_framing": unique_titles > 1,
        "details": framing_details,
    }


def _detect_tone(title: str) -> str:
    title_lower = title.lower()

    alarm_words = [
        "crisis", "colapso", "caen", "derrumbe", "alerta", "peligro",
        "grave", "emergencia", "desplome", "peor", "dramático",
    ]
    positive_words = [
        "sube", "crece", "récord", "logro", "avanza", "mejor",
        "superávit", "éxito", "celebra",
    ]
    neutral_words = [
        "cómo", "cuánto", "qué es", "paso a paso", "en vivo",
    ]

    if any(w in title_lower for w in alarm_words):
        return "alarmista"
    if any(w in title_lower for w in positive_words):
        return "positivo"
    if any(w in title_lower for w in neutral_words):
        return "informativo"
    return "neutral"


def _detect_focus(title: str) -> str:
    title_lower = title.lower()

    if any(w in title_lower for w in ["milei", "gobierno", "diputado", "senado", "oficialismo", "oposición"]):
        return "político"
    if any(w in title_lower for w in ["dólar", "mercado", "bonos", "acciones", "riesgo país", "inflación", "precio"]):
        return "económico"
    if any(w in title_lower for w in ["muerto", "víctima", "accidente", "violencia", "inseguridad"]):
        return "policial"
    if any(w in title_lower for w in ["gol", "torneo", "partido", "selección", "copa"]):
        return "deportivo"
    return "general"
