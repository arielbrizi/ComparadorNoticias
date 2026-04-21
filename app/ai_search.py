"""AI-powered semantic search and topic extraction for news groups.

Supports three providers: Gemini (Google), Groq (Llama) and Ollama
(self-hosted, typically on Railway). Each event type can pick its own
provider + fallback from the admin panel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import httpx
from google import genai
from groq import AsyncGroq

from app.ai_store import (
    get_provider_config,
    is_in_quiet_hours,
    load_last_good_topics,
    log_ai_usage,
    save_last_good_topics,
)
from app.models import ArticleGroup
from app.search_utils import (
    extract_keywords,
    normalized_query_key,
    prioritize_groups_by_keywords,
)

logger = logging.getLogger(__name__)

# ── Gemini (primary) ─────────────────────────────────────────────────────

_gemini_client: genai.Client | None = None
GEMINI_MODEL = "gemini-3-flash-preview"
MAX_RETRIES = 1
GEMINI_TIMEOUT = 30

_rate_limit_until: float = 0


def get_rate_limit_state() -> dict:
    """Return current Gemini rate-limit cooldown state."""
    remaining = max(0.0, _rate_limit_until - time.time())
    return {"active": remaining > 0, "seconds_remaining": int(remaining)}


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


# ── Ollama (self-hosted) ─────────────────────────────────────────────────

_ollama_client: httpx.AsyncClient | None = None
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_MAX_PROMPT_CHARS = 12000
OLLAMA_TIMEOUT = 120
OLLAMA_NUM_CTX = 8192


def _get_ollama_base_url() -> str | None:
    """Return normalized Ollama base URL from env, or None if unconfigured."""
    url = os.environ.get("OLLAMA_BASE_URL")
    if not url:
        return None
    return url.rstrip("/")


def _get_ollama_client() -> httpx.AsyncClient | None:
    global _ollama_client
    base_url = _get_ollama_base_url()
    if not base_url:
        return None
    if _ollama_client is None:
        _ollama_client = httpx.AsyncClient(
            base_url=base_url, timeout=OLLAMA_TIMEOUT,
        )
    return _ollama_client


def _ai_available() -> bool:
    """True if at least one AI provider is configured."""
    return bool(
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GROQ_API_KEY")
        or os.environ.get("OLLAMA_BASE_URL")
    )


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


async def _call_gemini(
    prompt: str, timeout: float = GEMINI_TIMEOUT,
) -> tuple[str, int, int]:
    """Call Gemini with automatic retry on 429 rate-limit errors.

    Returns (text, input_tokens, output_tokens).
    """
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
            in_tok = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
            out_tok = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
            return response.text, in_tok, out_tok
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


JSON_SYSTEM_MSG = (
    "Respondé ÚNICAMENTE con JSON válido. "
    "Sin texto explicativo, sin markdown, sin bloques de código. Solo el objeto JSON."
)
# Kept as alias for backward compatibility with code/tests that import it.
GROQ_SYSTEM_MSG = JSON_SYSTEM_MSG


async def _call_groq(
    prompt: str, timeout: float = 30,
) -> tuple[str, int, int]:
    """Call Groq (Llama) as fallback provider.

    Returns (text, input_tokens, output_tokens).
    """
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
                {"role": "system", "content": JSON_SYSTEM_MSG},
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
    usage = response.usage
    in_tok = getattr(usage, "prompt_tokens", 0) or 0 if usage else 0
    out_tok = getattr(usage, "completion_tokens", 0) or 0 if usage else 0
    return text, in_tok, out_tok


async def _call_ollama(
    prompt: str, timeout: float = OLLAMA_TIMEOUT,
) -> tuple[str, int, int]:
    """Call self-hosted Ollama via its native /api/chat endpoint.

    Uses ``format: "json"`` to constrain output, mirrors Groq's JSON system
    prompt, and truncates inputs that exceed ``OLLAMA_MAX_PROMPT_CHARS`` to
    avoid blowing the model's context window.

    Returns (text, input_tokens, output_tokens).
    """
    client = _get_ollama_client()
    if not client:
        raise RuntimeError("Ollama client not available")

    if len(prompt) > OLLAMA_MAX_PROMPT_CHARS:
        logger.info(
            "Truncating prompt for Ollama (%d → %d chars)",
            len(prompt), OLLAMA_MAX_PROMPT_CHARS,
        )
        prompt = (
            prompt[:OLLAMA_MAX_PROMPT_CHARS]
            + "\n\n[contexto truncado por límite del modelo]"
        )

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": JSON_SYSTEM_MSG},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.3, "num_ctx": OLLAMA_NUM_CTX},
    }

    try:
        response = await asyncio.wait_for(
            client.post("/api/chat", json=payload),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"Ollama call timed out after {timeout}s")
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Ollama HTTP error: {exc}") from exc

    if response.status_code >= 400:
        raise RuntimeError(
            f"Ollama returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Ollama returned non-JSON body: {exc}") from exc

    text = ((data.get("message") or {}).get("content") or "").strip()
    if not text:
        raise RuntimeError("Ollama returned empty response")

    in_tok = int(data.get("prompt_eval_count") or 0)
    out_tok = int(data.get("eval_count") or 0)
    return text, in_tok, out_tok


# ── Provider routing ────────────────────────────────────────────────────

_PROVIDER_MODELS: dict[str, str] = {
    "gemini": GEMINI_MODEL,
    "groq": GROQ_MODEL,
    "ollama": OLLAMA_MODEL,
}

_PROVIDER_DISPLAY: dict[str, str] = {
    "gemini": "Gemini",
    "groq": "Groq",
    "ollama": "Ollama",
}


def _provider_chain(mode: str) -> list[str]:
    """Return the ordered list of providers to try for *mode*.

    Single-provider modes (``gemini``, ``groq``, ``ollama``) yield a
    one-element list. Fallback modes (``X_fallback_Y``) yield ``[X, Y]``.
    Unknown modes fall back to Gemini→Groq to stay safe.
    """
    if "_fallback_" in mode:
        primary, _, secondary = mode.partition("_fallback_")
        if primary in _PROVIDER_MODELS and secondary in _PROVIDER_MODELS:
            return [primary, secondary]
    if mode in _PROVIDER_MODELS:
        return [mode]
    return ["gemini", "groq"]


def _available_providers() -> dict[str, bool]:
    """Snapshot of which providers have credentials/URL configured right now."""
    return {
        "gemini": _get_gemini_client() is not None,
        "groq": _get_groq_client() is not None,
        "ollama": _get_ollama_client() is not None,
    }


async def _invoke_provider(
    provider: str, prompt: str, timeout: float,
) -> tuple[str, int, int]:
    """Dispatch a single provider call. Raises RuntimeError if unknown."""
    if provider == "gemini":
        return await _call_gemini(prompt, timeout=timeout)
    if provider == "groq":
        return await _call_groq(prompt, timeout=min(timeout, 60))
    if provider == "ollama":
        return await _call_ollama(prompt, timeout=max(timeout, OLLAMA_TIMEOUT))
    raise RuntimeError(f"Unknown provider: {provider}")


async def _run_provider_chain(
    event_type: str,
    chain: list[str],
    prompt_for: "callable[[str], str]",
    timeout: float,
) -> tuple[str, str]:
    """Walk *chain* in order, logging every attempt. Returns (text, display).

    - ``prompt_for(provider)`` returns the prompt tailored to that provider
      (Groq/Ollama typically get a shorter one to fit their context budget).
    - On success, logs via ``_log_success`` and returns immediately.
    - On failure, logs via ``_log_error`` and falls through to the next
      provider. Re-raises the last exception if every provider fails.
    """
    available = _available_providers()
    filtered = [p for p in chain if available.get(p)]

    if not filtered:
        raise RuntimeError(
            "No AI provider configured for mode "
            f"'{'_fallback_'.join(chain) if len(chain) > 1 else chain[0]}'"
        )

    last_exc: Exception | None = None
    for idx, provider in enumerate(filtered):
        t0 = time.time()
        model = _PROVIDER_MODELS[provider]
        try:
            text, in_tok, out_tok = await _invoke_provider(
                provider, prompt_for(provider), timeout,
            )
            _log_success(event_type, provider, model, in_tok, out_tok, t0)
            return text, _PROVIDER_DISPLAY[provider]
        except Exception as exc:
            last_exc = exc
            _log_error(event_type, provider, model, t0, exc)
            if idx + 1 < len(filtered):
                logger.warning(
                    "%s failed (%s), falling back to %s",
                    _PROVIDER_DISPLAY[provider], exc,
                    _PROVIDER_DISPLAY[filtered[idx + 1]],
                )
                continue
            raise

    assert last_exc is not None
    raise last_exc


async def _call_ai(
    prompt: str,
    timeout: float = GEMINI_TIMEOUT,
    event_type: str = "unknown",
) -> tuple[str, str]:
    """Call the AI provider(s) configured for *event_type*.

    Returns (response_text, provider_name).
    """
    if not _ai_available():
        raise RuntimeError("No AI provider configured")

    mode = get_provider_config().get(event_type, "gemini_fallback_groq")
    chain = _provider_chain(mode)
    return await _run_provider_chain(
        event_type, chain, lambda _p: prompt, timeout,
    )


def _log_success(
    event_type: str, provider: str, model: str,
    in_tok: int, out_tok: int, t0: float,
) -> None:
    log_ai_usage(
        event_type=event_type,
        provider=provider,
        model=model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        latency_ms=int((time.time() - t0) * 1000),
    )


def _log_error(
    event_type: str, provider: str, model: str,
    t0: float, exc: Exception,
) -> None:
    log_ai_usage(
        event_type=event_type,
        provider=provider,
        model=model,
        input_tokens=0,
        output_tokens=0,
        latency_ms=int((time.time() - t0) * 1000),
        success=False,
        error_message=str(exc)[:500],
    )


# ── Search ───────────────────────────────────────────────────────────────

SEARCH_PROMPT = """Sos un asistente de un comparador de noticias argentino llamado "Vs News".
El usuario busca: "{query}"
Palabras clave detectadas (ignorá stopwords como "últimos", "detalles", "dame", "status"): {keywords}

Respondé ÚNICAMENTE con JSON válido (sin markdown, sin bloques de código), con este formato:
{{
  "summary": "Resumen breve del tema buscado basado en las noticias (2-3 oraciones, español argentino)",
  "relevant_group_ids": ["id1", "id2"],
  "has_results": true
}}

Reglas:
- Tratá la consulta como lenguaje natural: lo que importa son las palabras clave, no la frase exacta.
  Ejemplos: "últimos detalles de la guerra" → buscá "guerra"; "dame el status del dólar hoy" → buscá "dólar".
- Incluí TODOS los grupos semánticamente relevantes a las palabras clave, aunque el título no las mencione literalmente.
- Si la búsqueda es un nombre de medio (ej: "Clarín"), incluí todos los grupos donde aparece como fuente.
- Si es un tema amplio (ej: "economía"), incluí todo lo relacionado.
- El resumen debe ser informativo, neutral y basado SOLO en los títulos disponibles.
- Si no hay resultados relevantes, poné has_results: false y explicá brevemente en summary.
- Devolvé SOLO el JSON.

Noticias disponibles:
{context}"""


def _format_keywords(query: str) -> str:
    kws = extract_keywords(query)
    if not kws:
        return "(ninguna — usá la consulta tal cual)"
    return ", ".join(kws)


async def ai_news_search(
    query: str,
    groups: list[ArticleGroup],
    *,
    event_type: str = "search",
) -> dict:
    """Send query + groups context to AI and return structured results."""
    if not _ai_available():
        return {"ai_available": False, "error": "API key not configured"}

    cache_key = query.strip().lower()
    norm_key = normalized_query_key(query)
    topic_labels = _get_cached_topic_labels()
    is_topic = cache_key in topic_labels or norm_key in topic_labels

    for lookup in (cache_key, norm_key):
        if is_topic and lookup in _search_cache:
            cached = _search_cache[lookup]
            cached_ids = set(cached.get("relevant_group_ids", []))
            if cached_ids:
                current_ids = {g.group_id for g in groups}
                if cached_ids & current_ids:
                    logger.info("Search cache hit for topic: %s", query)
                    return cached
                logger.info(
                    "Search cache stale for topic: %s (0/%d IDs match), refreshing",
                    query, len(cached_ids),
                )
                del _search_cache[lookup]
            else:
                logger.info("Search cache hit for topic: %s", query)
                return cached

    keywords = extract_keywords(query)
    keywords_str = _format_keywords(query)
    prioritized = prioritize_groups_by_keywords(groups, keywords)
    context = _build_context(prioritized, max_groups=150)
    prompt = SEARCH_PROMPT.format(query=query, keywords=keywords_str, context=context)

    try:
        raw, provider = await _call_ai_search(
            prompt, query, prioritized, event_type=event_type,
        )
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
    *, event_type: str = "search",
) -> tuple[str, str]:
    """Try primary provider with appropriate context; fall back if configured.

    Gemini receives the full precomputed prompt (big context budget).
    Groq and Ollama get a per-provider prompt rebuilt to fit their own
    ``*_MAX_PROMPT_CHARS`` budget, so we avoid mid-line truncation.
    """
    if not _ai_available():
        raise RuntimeError("No AI provider configured")

    mode = get_provider_config().get(event_type, "gemini_fallback_groq")
    chain = _provider_chain(mode)

    keywords_str = _format_keywords(query)
    prompt_overhead = len(
        SEARCH_PROMPT.format(query=query, keywords=keywords_str, context="")
    )

    def _compact_prompt(max_prompt_chars: int) -> str:
        budget = max_prompt_chars - prompt_overhead - 100
        context = _build_context(
            groups, max_groups=150, max_chars=max(budget, 2000),
        )
        return SEARCH_PROMPT.format(
            query=query, keywords=keywords_str, context=context,
        )

    def _prompt_for(provider: str) -> str:
        if provider == "groq":
            return _compact_prompt(GROQ_MAX_PROMPT_CHARS)
        if provider == "ollama":
            return _compact_prompt(OLLAMA_MAX_PROMPT_CHARS)
        return prompt

    return await _run_provider_chain(
        event_type, chain, _prompt_for, GEMINI_TIMEOUT,
    )


# ── Trending topics ──────────────────────────────────────────────────────

_topics_cache: dict = {"topics": [], "ts": 0, "generated_at": ""}
_search_cache: dict[str, dict] = {}
_last_good_topics: dict = {"topics": [], "ai_provider": "", "generated_at": ""}
TOPICS_TTL = 3600  # 1 hour
# Only persist as "last good" when AI returned at least this many topics,
# to avoid locking in a degraded run (e.g. truncated Groq prompt) as the
# forever-fallback when both providers are rate-limited.
MIN_LAST_GOOD_TOPICS = 4


def restore_last_good_topics() -> None:
    """Load persisted last-good topics from DB into memory (call on startup)."""
    saved = load_last_good_topics()
    if saved and saved["topics"]:
        _last_good_topics["topics"] = saved["topics"]
        _last_good_topics["ai_provider"] = saved["ai_provider"]
        _last_good_topics["generated_at"] = saved["generated_at"]
        logger.info(
            "Restored %d last-good topics from DB (provider=%s)",
            len(saved["topics"]),
            saved["ai_provider"],
        )


def is_topics_cache_valid() -> bool:
    """True if topics cache has data and hasn't expired."""
    return bool(_topics_cache.get("topics")) and (time.time() - _topics_cache.get("ts", 0)) < TOPICS_TTL


def is_topstory_cache_valid() -> bool:
    """True if top story cache has data and hasn't expired."""
    return bool(_topstory_cache.get("data")) and (time.time() - _topstory_cache.get("ts", 0)) < TOPSTORY_TTL


def invalidate_search_cache(query: str) -> None:
    """Remove a specific query from the search cache (e.g. stale group IDs)."""
    key = query.strip().lower()
    if key in _search_cache:
        del _search_cache[key]
        logger.debug("Invalidated search cache for: %s", query)


def _get_cached_topic_labels() -> set[str]:
    """Return all topic labels exposed publicly via /api/topics (lowercase).

    Includes both the live ``_topics_cache`` and the ``_last_good_topics``
    fallback, since the endpoint returns the latter when both AI providers
    are rate-limited. Anonymous users are allowed to search any label they
    could see in the UI, regardless of which source served it.
    """
    live = {t["label"].strip().lower()
            for t in _topics_cache["topics"] if "label" in t}
    fallback = {t["label"].strip().lower()
                for t in _last_good_topics["topics"] if "label" in t}
    return live | fallback


def is_public_topic_query(query: str) -> bool:
    """True if query matches one of the day's cached topic labels.

    Used as the allowlist for anonymous searches: free-form queries require
    login, but clicking a curated "Temas del día" chip stays public because
    its label was generated by the system.
    """
    if not query:
        return False
    cache_key = query.strip().lower()
    norm_key = normalized_query_key(query)
    topic_labels = _get_cached_topic_labels()
    return cache_key in topic_labels or norm_key in topic_labels

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
                "search_cached": cached_labels,
                "generated_at": _topics_cache.get("generated_at", "")}

    if not _ai_available():
        return {"topics": [], "ai_available": False}

    context = _build_context(groups)
    prompt = TOPICS_PROMPT.format(context=context)

    try:
        raw, provider = await _call_ai(prompt, event_type="topics")
        text = _clean_json_response(raw)
        result = json.loads(text)
        topics = result.get("topics", [])[:6]
        generated_at = datetime.now(timezone.utc).isoformat()
        _topics_cache["topics"] = topics
        _topics_cache["ts"] = now
        _topics_cache["ai_provider"] = provider
        _topics_cache["generated_at"] = generated_at
        _search_cache.clear()

        if len(topics) >= MIN_LAST_GOOD_TOPICS:
            _last_good_topics["topics"] = topics
            _last_good_topics["ai_provider"] = provider
            _last_good_topics["generated_at"] = generated_at
            save_last_good_topics(topics, provider, generated_at)
        elif topics:
            logger.warning(
                "Topics run returned only %d items (< %d), NOT persisting as last-good",
                len(topics), MIN_LAST_GOOD_TOPICS,
            )

        logger.info("Topics regenerated via %s — search cache cleared (%d topics)", provider, len(topics))
        asyncio.create_task(_prefetch_topic_searches(topics, groups))
        return {"topics": topics, "ai_available": True, "cached": False, "ai_provider": provider,
                "search_cached": [],
                "generated_at": generated_at}

    except Exception as exc:
        logger.error("AI topics failed: %s", exc)

        fallback = _last_good_topics["topics"]
        if fallback:
            logger.info(
                "Returning %d last-good topics as fallback (provider=%s)",
                len(fallback),
                _last_good_topics["ai_provider"],
            )
            cached_labels = [
                t["label"] for t in fallback
                if t.get("label", "").strip().lower() in _search_cache
            ]
            return {
                "topics": fallback,
                "ai_available": True,
                "cached": True,
                "fallback": True,
                "ai_provider": _last_good_topics["ai_provider"],
                "search_cached": cached_labels,
                "generated_at": _last_good_topics["generated_at"],
            }

        return {"topics": [], "ai_available": False}


_PREFETCH_CONCURRENCY = 2


async def _prefetch_topic_searches(
    topics: list[dict], groups: list[ArticleGroup],
) -> None:
    """Pre-warm _search_cache for each topic label after topic generation."""
    if is_in_quiet_hours("search_prefetch"):
        logger.info("Search prefetch skipped — inside quiet hours")
        return

    sem = asyncio.Semaphore(_PREFETCH_CONCURRENCY)

    async def _fetch_one(label: str) -> None:
        async with sem:
            try:
                await ai_news_search(label, groups, event_type="search_prefetch")
            except Exception as exc:
                logger.warning("Prefetch failed for topic '%s': %s", label, exc)

    labels = [t.get("label", "") for t in topics if t.get("label")]
    await asyncio.gather(*[_fetch_one(lbl) for lbl in labels])
    logger.info("Topic prefetch complete: %d/%d cached", len(_search_cache), len(labels))


# ── Weekly summary ────────────────────────────────────────────────────

_weekly_cache: dict = {"data": None, "ts": 0, "week_key": ""}
WEEKLY_TTL = 86400  # 24 h — effectively never expires; prefetches force renewal
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
    *,
    force: bool = False,
) -> dict:
    """Generate an editorial weekly summary from the week's news groups."""
    week_key = f"{week_start}_{week_end}"
    now = time.time()
    if (
        not force
        and _weekly_cache["data"]
        and _weekly_cache["week_key"] == week_key
        and (now - _weekly_cache["ts"]) < WEEKLY_TTL
    ):
        result = {**_weekly_cache["data"], "cached": True}
        if "generated_at" not in result:
            result["generated_at"] = datetime.fromtimestamp(
                _weekly_cache["ts"], tz=timezone.utc,
            ).isoformat()
        return result

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
        raw, provider = await _call_ai(prompt, timeout=WEEKLY_TIMEOUT, event_type="weekly_summary")
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
            "generated_at": datetime.now(timezone.utc).isoformat(),
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
    cache_key = today
    now = time.time()
    if (
        _topstory_cache["data"]
        and _topstory_cache["cache_key"] == cache_key
        and (now - _topstory_cache["ts"]) < TOPSTORY_TTL
    ):
        result = {**_topstory_cache["data"], "cached": True}
        if "generated_at" not in result:
            result["generated_at"] = datetime.fromtimestamp(
                _topstory_cache["ts"], tz=timezone.utc,
            ).isoformat()
        return result

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
        raw, provider = await _call_ai(prompt, timeout=TOPSTORY_TIMEOUT, event_type="top_story")
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

        payload = {
            "ai_available": True,
            "ai_provider": provider,
            "story": story,
            "date": today,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
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
