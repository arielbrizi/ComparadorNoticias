"""Gemini-powered semantic search and topic extraction for news groups."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

from google import genai

from app.models import ArticleGroup

logger = logging.getLogger(__name__)

_client: genai.Client | None = None
MODEL = "gemini-3-flash-preview"
MAX_RETRIES = 1
GEMINI_TIMEOUT = 30

_rate_limit_until: float = 0


def _get_client() -> genai.Client | None:
    global _client
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set — AI search disabled")
        return None
    if _client is None:
        _client = genai.Client(api_key=api_key)
    return _client


def _build_context(groups: list[ArticleGroup], max_groups: int = 150) -> str:
    lines = []
    for g in groups[:max_groups]:
        sources = ", ".join(a.source for a in g.articles)
        extra_titles = [
            a.title for a in g.articles
            if a.title != g.representative_title
        ]
        summary = ""
        for a in g.articles:
            if a.summary:
                summary = a.short_summary(160)
                break

        line = (
            f"- ID:{g.group_id} | {g.representative_title} "
            f"| Cat:{g.category} | Fuentes:{sources}"
        )
        if extra_titles:
            line += f" | También: {'; '.join(extra_titles[:3])}"
        if summary:
            line += f" | Resumen: {summary}"
        lines.append(line)
    return "\n".join(lines)


def _parse_retry_seconds(exc: Exception) -> float | None:
    """Extract retry delay from a 429 error message."""
    msg = str(exc)
    if "429" not in msg:
        return None
    match = re.search(r"retry in ([\d.]+)s", msg, re.IGNORECASE)
    if match:
        return min(float(match.group(1)), 30)
    match = re.search(r"retryDelay.*?(\d+)s", msg)
    if match:
        return min(float(match.group(1)), 30)
    return 5.0


def _clean_json_response(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


async def _call_gemini(client: genai.Client, prompt: str) -> str:
    """Call Gemini with automatic retry on 429 rate-limit errors."""
    global _rate_limit_until

    if time.time() < _rate_limit_until:
        remaining = int(_rate_limit_until - time.time())
        raise RuntimeError(f"Rate-limit cooldown active ({remaining}s left)")

    last_exc = None
    for attempt in range(1 + MAX_RETRIES):
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=MODEL, contents=prompt,
                ),
                timeout=GEMINI_TIMEOUT,
            )
            return response.text
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Gemini call timed out after {GEMINI_TIMEOUT}s"
            )
        except Exception as exc:
            last_exc = exc
            wait = _parse_retry_seconds(exc)
            if wait is not None:
                _rate_limit_until = time.time() + wait + 2
                if attempt < MAX_RETRIES:
                    logger.info("Rate limited, retrying in %.0fs (attempt %d/%d)",
                                wait, attempt + 1, MAX_RETRIES)
                    await asyncio.sleep(wait)
                else:
                    raise
            else:
                raise
    raise last_exc  # unreachable, but keeps type-checkers happy


# ── Search ───────────────────────────────────────────────────────────────

SEARCH_PROMPT = """Sos un asistente de un comparador de noticias argentino llamado "Vs News".
El usuario busca: "{query}"

Noticias disponibles:
{context}

Respondé ÚNICAMENTE con JSON válido (sin markdown, sin bloques de código), con este formato:
{{
  "summary": "Resumen breve del tema buscado basado en las noticias (2-3 oraciones, español argentino)",
  "relevant_group_ids": ["id1", "id2"],
  "has_results": true
}}

Reglas:
- Incluí TODOS los grupos semánticamente relevantes, no solo los que mencionan las palabras exactas.
- Si la búsqueda es un nombre de medio (ej: "Clarín"), incluí todos los grupos donde aparece como fuente.
- Si es un tema amplio (ej: "economía"), incluí todo lo relacionado.
- El resumen debe ser informativo, neutral y basado SOLO en los títulos disponibles.
- Si no hay resultados relevantes, poné has_results: false y explicá brevemente en summary.
- Devolvé SOLO el JSON."""


async def gemini_search(query: str, groups: list[ArticleGroup]) -> dict:
    """Send query + groups context to Gemini and return structured results."""
    client = _get_client()
    if not client:
        return {"ai_available": False, "error": "API key not configured"}

    cache_key = query.strip().lower()
    is_topic = cache_key in _get_cached_topic_labels()

    if is_topic and cache_key in _search_cache:
        logger.info("Search cache hit for topic: %s", query)
        return _search_cache[cache_key]

    context = _build_context(groups)
    prompt = SEARCH_PROMPT.format(query=query, context=context)

    try:
        raw = await _call_gemini(client, prompt)
        text = _clean_json_response(raw)
        result = json.loads(text)
        result["ai_available"] = True

        if is_topic:
            _search_cache[cache_key] = result
            logger.info("Search result cached for topic: %s", query)

        return result

    except json.JSONDecodeError as exc:
        logger.error("Gemini returned invalid JSON: %s — raw: %s", exc, text[:300])
        return {"ai_available": False, "error": "Invalid AI response"}
    except Exception as exc:
        logger.error("Gemini search failed: %s", exc)
        return {"ai_available": False, "error": str(exc)}


# ── Trending topics ──────────────────────────────────────────────────────

_topics_cache: dict = {"topics": [], "ts": 0}
_search_cache: dict[str, dict] = {}
TOPICS_TTL = 3600  # 1 hour


def _get_cached_topic_labels() -> set[str]:
    """Return the current cached topic labels in lowercase for matching."""
    return {t["label"].strip().lower() for t in _topics_cache["topics"] if "label" in t}

TOPICS_PROMPT = """Sos un editor de un comparador de noticias argentino.
Analizá las noticias del día y extraé los 6 temas más importantes.

Noticias disponibles:
{context}

Respondé ÚNICAMENTE con JSON válido (sin markdown, sin bloques de código):
{{
  "topics": [
    {{"label": "Nombre corto del tema (2-5 palabras)", "emoji": "emoji representativo"}},
    ...
  ]
}}

Reglas:
- Exactamente 6 temas, ordenados por importancia/cobertura.
- Cada label debe ser conciso y funcionar como término de búsqueda (ej: "Dólar y mercados", "Crisis energética").
- Elegí emojis representativos pero profesionales.
- Basate en la cantidad de fuentes y artículos por tema para determinar importancia.
- Devolvé SOLO el JSON."""


async def gemini_topics(groups: list[ArticleGroup]) -> dict:
    """Extract trending topics from current news groups."""
    now = time.time()
    if _topics_cache["topics"] and (now - _topics_cache["ts"]) < TOPICS_TTL:
        return {"topics": _topics_cache["topics"], "ai_available": True, "cached": True}

    client = _get_client()
    if not client:
        return {"topics": [], "ai_available": False}

    context = _build_context(groups)
    prompt = TOPICS_PROMPT.format(context=context)

    try:
        raw = await _call_gemini(client, prompt)
        text = _clean_json_response(raw)
        result = json.loads(text)
        topics = result.get("topics", [])[:6]
        _topics_cache["topics"] = topics
        _topics_cache["ts"] = now
        _search_cache.clear()
        logger.info("Topics regenerated — search cache cleared (%d topics)", len(topics))
        return {"topics": topics, "ai_available": True, "cached": False}

    except Exception as exc:
        logger.error("Gemini topics failed: %s", exc)
        return {"topics": [], "ai_available": False}
