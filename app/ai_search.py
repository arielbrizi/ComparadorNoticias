"""AI-powered semantic search and topic extraction for news groups.

Uses Google Gemini as primary provider and Groq (Llama) as fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

from google import genai
from groq import AsyncGroq

from app.models import ArticleGroup

logger = logging.getLogger(__name__)

# ── Gemini (primary) ─────────────────────────────────────────────────────

_gemini_client: genai.Client | None = None
GEMINI_MODEL = "gemini-3-flash-preview"
MAX_RETRIES = 1
GEMINI_TIMEOUT = 30

_rate_limit_until: float = 0


def _get_gemini_client() -> genai.Client | None:
    global _gemini_client
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client


# ── Groq fallback ────────────────────────────────────────────────────────

_groq_client: AsyncGroq | None = None
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_MAX_PROMPT_CHARS = 10000


def _get_groq_client() -> AsyncGroq | None:
    global _groq_client
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None
    if _groq_client is None:
        _groq_client = AsyncGroq(api_key=api_key)
    return _groq_client


def _ai_available() -> bool:
    """True if at least one AI provider is configured."""
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY"))


def _build_context(
    groups: list[ArticleGroup],
    max_groups: int = 80,
    max_chars: int = 0,
) -> str:
    lines: list[str] = []
    total = 0
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

        if max_chars and total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
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


async def _call_gemini(prompt: str, timeout: float = GEMINI_TIMEOUT) -> str:
    """Call Gemini with automatic retry on 429 rate-limit errors."""
    global _rate_limit_until

    client = _get_gemini_client()
    if not client:
        raise RuntimeError("Gemini client not available")

    if time.time() < _rate_limit_until:
        remaining = int(_rate_limit_until - time.time())
        raise RuntimeError(f"Rate-limit cooldown active ({remaining}s left)")

    last_exc = None
    for attempt in range(1 + MAX_RETRIES):
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=GEMINI_MODEL, contents=prompt,
                ),
                timeout=timeout,
            )
            return response.text
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Gemini call timed out after {timeout}s"
            )
        except Exception as exc:
            last_exc = exc
            wait = _parse_retry_seconds(exc)
            if wait is not None:
                _rate_limit_until = time.time() + wait + 2
                if attempt < MAX_RETRIES:
                    logger.info("Gemini rate limited, retrying in %.0fs (attempt %d/%d)",
                                wait, attempt + 1, MAX_RETRIES)
                    await asyncio.sleep(wait)
                else:
                    raise
            else:
                raise
    raise last_exc  # unreachable, but keeps type-checkers happy


GROQ_SYSTEM_MSG = (
    "Respondé ÚNICAMENTE con JSON válido. "
    "Sin texto explicativo, sin markdown, sin bloques de código. Solo el objeto JSON."
)


async def _call_groq(prompt: str, timeout: float = 30) -> str:
    """Call Groq (Llama) as fallback provider."""
    client = _get_groq_client()
    if not client:
        raise RuntimeError("Groq client not available")

    if len(prompt) > GROQ_MAX_PROMPT_CHARS:
        logger.info("Truncating prompt for Groq (%d → %d chars)", len(prompt), GROQ_MAX_PROMPT_CHARS)
        prompt = prompt[:GROQ_MAX_PROMPT_CHARS] + "\n\n[contexto truncado por límite del modelo]"

    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": GROQ_SYSTEM_MSG},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
        ),
        timeout=timeout,
    )
    text = response.choices[0].message.content
    if not text:
        raise RuntimeError("Groq returned empty response")
    return text


async def _call_ai(prompt: str, timeout: float = GEMINI_TIMEOUT) -> tuple[str, str]:
    """Try Gemini first; if it fails, fall back to Groq.

    Returns (response_text, provider_name).
    """
    gemini_ok = _get_gemini_client() is not None
    groq_ok = _get_groq_client() is not None

    if not gemini_ok and not groq_ok:
        raise RuntimeError("No AI provider configured")

    if gemini_ok:
        try:
            text = await _call_gemini(prompt, timeout=timeout)
            return text, "Gemini"
        except Exception as exc:
            if not groq_ok:
                raise
            logger.warning("Gemini failed (%s), falling back to Groq", exc)

    text = await _call_groq(prompt, timeout=min(timeout, 60))
    return text, "Groq"


# ── Search ───────────────────────────────────────────────────────────────

SEARCH_PROMPT = """Sos un asistente de un comparador de noticias argentino llamado "Vs News".
El usuario busca: "{query}"

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
- Devolvé SOLO el JSON.

Noticias disponibles:
{context}"""


async def ai_news_search(query: str, groups: list[ArticleGroup]) -> dict:
    """Send query + groups context to AI and return structured results."""
    if not _ai_available():
        return {"ai_available": False, "error": "API key not configured"}

    cache_key = query.strip().lower()
    is_topic = cache_key in _get_cached_topic_labels()

    if is_topic and cache_key in _search_cache:
        logger.info("Search cache hit for topic: %s", query)
        return _search_cache[cache_key]

    context = _build_context(groups, max_groups=150)
    prompt = SEARCH_PROMPT.format(query=query, context=context)

    try:
        raw, provider = await _call_ai_search(prompt, query, groups)
        text = _clean_json_response(raw)
        result = json.loads(text)
        result["ai_available"] = True
        result["ai_provider"] = provider

        if is_topic:
            _search_cache[cache_key] = result
            logger.info("Search result cached for topic: %s", query)

        return result

    except json.JSONDecodeError as exc:
        logger.error("AI returned invalid JSON: %s — raw: %s", exc, text[:300])
        return {"ai_available": False, "error": "Invalid AI response"}
    except Exception as exc:
        logger.error("AI search failed: %s", exc)
        return {"ai_available": False, "error": str(exc)}


async def _call_ai_search(
    prompt: str, query: str, groups: list[ArticleGroup],
) -> tuple[str, str]:
    """Try Gemini with full context; fall back to Groq with a right-sized prompt."""
    gemini_ok = _get_gemini_client() is not None
    groq_ok = _get_groq_client() is not None

    if not gemini_ok and not groq_ok:
        raise RuntimeError("No AI provider configured")

    if gemini_ok:
        try:
            text = await _call_gemini(prompt, timeout=GEMINI_TIMEOUT)
            return text, "Gemini"
        except Exception as exc:
            if not groq_ok:
                raise
            logger.warning("Gemini failed (%s), falling back to Groq", exc)

    prompt_overhead = len(SEARCH_PROMPT.format(query=query, context=""))
    groq_context_budget = GROQ_MAX_PROMPT_CHARS - prompt_overhead - 100
    groq_context = _build_context(
        groups, max_groups=150, max_chars=max(groq_context_budget, 2000),
    )
    groq_prompt = SEARCH_PROMPT.format(query=query, context=groq_context)

    text = await _call_groq(groq_prompt, timeout=60)
    return text, "Groq"


# ── Trending topics ──────────────────────────────────────────────────────

_topics_cache: dict = {"topics": [], "ts": 0}
_search_cache: dict[str, dict] = {}
TOPICS_TTL = 3600  # 1 hour


def _get_cached_topic_labels() -> set[str]:
    """Return the current cached topic labels in lowercase for matching."""
    return {t["label"].strip().lower() for t in _topics_cache["topics"] if "label" in t}

TOPICS_PROMPT = """Sos un editor de un comparador de noticias argentino.
Analizá las noticias del día y extraé los 6 temas más importantes.

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
- Devolvé SOLO el JSON.

Noticias disponibles:
{context}"""


async def ai_topics(groups: list[ArticleGroup]) -> dict:
    """Extract trending topics from current news groups."""
    now = time.time()
    if _topics_cache["topics"] and (now - _topics_cache["ts"]) < TOPICS_TTL:
        cached_labels = [t["label"] for t in _topics_cache["topics"]
                         if t.get("label", "").strip().lower() in _search_cache]
        return {"topics": _topics_cache["topics"], "ai_available": True, "cached": True,
                "ai_provider": _topics_cache.get("ai_provider", "unknown"),
                "search_cached": cached_labels}

    if not _ai_available():
        return {"topics": [], "ai_available": False}

    context = _build_context(groups)
    prompt = TOPICS_PROMPT.format(context=context)

    try:
        raw, provider = await _call_ai(prompt)
        text = _clean_json_response(raw)
        result = json.loads(text)
        topics = result.get("topics", [])[:6]
        _topics_cache["topics"] = topics
        _topics_cache["ts"] = now
        _topics_cache["ai_provider"] = provider
        _search_cache.clear()
        logger.info("Topics regenerated via %s — search cache cleared (%d topics)", provider, len(topics))
        asyncio.create_task(_prefetch_topic_searches(topics, groups))
        return {"topics": topics, "ai_available": True, "cached": False, "ai_provider": provider,
                "search_cached": []}

    except Exception as exc:
        logger.error("AI topics failed: %s", exc)
        return {"topics": [], "ai_available": False}


_PREFETCH_DELAY = 2  # seconds between prefetch calls to avoid rate limits


async def _prefetch_topic_searches(
    topics: list[dict], groups: list[ArticleGroup],
) -> None:
    """Pre-warm _search_cache for each topic label after topic generation."""
    for i, topic in enumerate(topics):
        label = topic.get("label", "")
        if not label:
            continue
        try:
            await ai_news_search(label, groups)
        except Exception as exc:
            logger.warning("Prefetch failed for topic '%s': %s", label, exc)
        if i < len(topics) - 1:
            await asyncio.sleep(_PREFETCH_DELAY)
    logger.info("Topic prefetch complete: %d/%d cached", len(_search_cache), len(topics))


# ── Weekly summary ────────────────────────────────────────────────────

_weekly_cache: dict = {"data": None, "ts": 0, "week_key": ""}
WEEKLY_TTL = 3600  # 1 hour
WEEKLY_TIMEOUT = 120

WEEKLY_PROMPT = """Sos el editor jefe de un diario argentino preparando el resumen semanal.
Analizá todas las noticias de la semana ({week_start} a {week_end}) y extraé los temas más importantes.

Respondé ÚNICAMENTE con JSON válido (sin markdown, sin bloques de código):
{{
  "themes": [
    {{
      "label": "Título editorial del tema (3-8 palabras)",
      "emoji": "emoji representativo",
      "summary": "Resumen editorial de 3-5 oraciones en español argentino, neutro y profesional. Basate en los títulos y resúmenes disponibles.",
      "group_ids": ["id1", "id2", "id3"]
    }},
    ...
  ]
}}

Reglas:
- Elegí entre 5 y 10 temas según la densidad informativa de la semana.
- Ordenalos por importancia/cobertura (más fuentes y artículos = más importante).
- Cada label debe ser un título editorial conciso (ej: "Acuerdo Mercosur-UE sacude la industria").
- Cada summary debe ser informativo, neutral, en español argentino, y basarse SOLO en las noticias disponibles.
- Incluí en group_ids TODOS los grupos relevantes a cada tema.
- Elegí emojis representativos pero profesionales.
- Devolvé SOLO el JSON.

Noticias de la semana:
{context}"""


async def ai_weekly_summary(
    groups: list[ArticleGroup],
    week_start: str,
    week_end: str,
) -> dict:
    """Generate an editorial weekly summary from the week's news groups."""
    week_key = f"{week_start}_{week_end}"
    now = time.time()
    if (
        _weekly_cache["data"]
        and _weekly_cache["week_key"] == week_key
        and (now - _weekly_cache["ts"]) < WEEKLY_TTL
    ):
        return {**_weekly_cache["data"], "cached": True}

    if not _ai_available():
        return {"themes": [], "ai_available": False}

    if not groups:
        return {"themes": [], "ai_available": True, "week_start": week_start, "week_end": week_end}

    context = _build_context(groups, max_groups=120)
    prompt = WEEKLY_PROMPT.format(
        week_start=week_start,
        week_end=week_end,
        context=context,
    )

    try:
        raw, provider = await _call_ai(prompt, timeout=WEEKLY_TIMEOUT)
        text = _clean_json_response(raw)
        result = json.loads(text)
        themes = result.get("themes", [])[:10]

        groups_by_id = {g.group_id: g for g in groups}
        for theme in themes:
            gids = theme.get("group_ids", [])
            image = ""
            sources = set()
            for gid in gids:
                g = groups_by_id.get(gid)
                if g:
                    if not image and g.representative_image:
                        image = g.representative_image
                    for a in g.articles:
                        sources.add(a.source)
            theme["image"] = image
            theme["sources"] = sorted(sources)

        payload = {
            "themes": themes,
            "ai_available": True,
            "ai_provider": provider,
            "week_start": week_start,
            "week_end": week_end,
        }
        _weekly_cache["data"] = payload
        _weekly_cache["ts"] = now
        _weekly_cache["week_key"] = week_key
        logger.info("Weekly summary generated (%d themes)", len(themes))
        return payload

    except json.JSONDecodeError as exc:
        logger.error("AI weekly returned invalid JSON: %s — raw: %s", exc, text[:300])
        return {"themes": [], "ai_available": False, "error": "Invalid AI response"}
    except Exception as exc:
        logger.error("AI weekly summary failed: %s", exc)
        return {"themes": [], "ai_available": False, "error": str(exc)}


# ── Top story of the day ──────────────────────────────────────────────

_topstory_cache: dict = {"data": None, "ts": 0, "cache_key": ""}
TOPSTORY_TTL = 10800  # 3 hours
TOPSTORY_TIMEOUT = 60

TOPSTORY_PROMPT = """Sos el editor jefe de un diario argentino. Te presento la noticia con mayor \
cobertura del día (la que más medios cubrieron). Generá un análisis editorial profundo.

Respondé ÚNICAMENTE con JSON válido (sin markdown, sin bloques de código):
{{
  "title": "Título editorial impactante y periodístico (máximo 15 palabras)",
  "emoji": "emoji representativo",
  "summary": "Análisis editorial profundo de 4-6 oraciones en español argentino. \
Explicá el contexto, por qué es la noticia más importante, qué implica y hacia dónde puede ir. \
Tono profesional, informativo y neutro.",
  "key_points": [
    "Punto clave 1 (oración corta y directa)",
    "Punto clave 2",
    "Punto clave 3"
  ]
}}

Reglas:
- El título debe ser editorial, no repetir el título original.
- El summary debe ser un análisis, no una mera repetición de los titulares.
- Entre 3 y 5 key_points, cada uno una oración corta y directa.
- Todo en español argentino, profesional y neutro.
- Devolvé SOLO el JSON.

Noticia principal:
- Título representativo: {title}
- Fuentes que la cubrieron: {sources}
- Categoría: {category}

Titulares de cada medio:
{headlines}

Resúmenes disponibles:
{summaries}"""


async def ai_top_story(
    groups: list[ArticleGroup],
    today: str,
) -> dict:
    """Generate an editorial analysis of the day's top story (most covered)."""
    if not groups:
        return {"ai_available": True, "story": None, "date": today}

    top = groups[0]
    cache_key = f"{today}_{top.group_id}"
    now = time.time()
    if (
        _topstory_cache["data"]
        and _topstory_cache["cache_key"] == cache_key
        and (now - _topstory_cache["ts"]) < TOPSTORY_TTL
    ):
        return {**_topstory_cache["data"], "cached": True}

    if not _ai_available():
        return {"ai_available": False, "story": None, "date": today}

    headlines = "\n".join(
        f"- {a.source}: \"{a.title}\"" for a in top.articles
    )
    summaries = "\n".join(
        f"- {a.source}: {a.short_summary(200)}" for a in top.articles if a.summary
    )
    sources_str = ", ".join(a.source for a in top.articles)

    prompt = TOPSTORY_PROMPT.format(
        title=top.representative_title,
        sources=sources_str,
        category=top.category,
        headlines=headlines,
        summaries=summaries or "(sin resúmenes disponibles)",
    )

    try:
        raw, provider = await _call_ai(prompt, timeout=TOPSTORY_TIMEOUT)
        text = _clean_json_response(raw)
        result = json.loads(text)

        articles_data = [
            {"source": a.source, "title": a.title, "link": a.link, "source_color": a.source_color}
            for a in top.articles
        ]

        story = {
            "title": result.get("title", top.representative_title),
            "emoji": result.get("emoji", ""),
            "summary": result.get("summary", ""),
            "key_points": result.get("key_points", [])[:5],
            "original_title": top.representative_title,
            "image": top.representative_image,
            "sources": sorted({a.source for a in top.articles}),
            "articles": articles_data,
            "source_count": top.source_count,
            "category": top.category,
            "published": top.published.isoformat() if top.published else None,
            "group_id": top.group_id,
        }

        payload = {"ai_available": True, "ai_provider": provider, "story": story, "date": today}
        _topstory_cache["data"] = payload
        _topstory_cache["ts"] = now
        _topstory_cache["cache_key"] = cache_key
        logger.info("Top story generated: %s (%d sources)", story["title"][:60], top.source_count)
        return payload

    except json.JSONDecodeError as exc:
        logger.error("AI top story returned invalid JSON: %s — raw: %s", exc, text[:300])
        return {"ai_available": False, "story": None, "date": today, "error": "Invalid AI response"}
    except Exception as exc:
        logger.error("AI top story failed: %s", exc)
        return {"ai_available": False, "story": None, "date": today, "error": str(exc)}
