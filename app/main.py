"""
Comparador de Noticias — API principal.
Agrega noticias de medios argentinos y permite compararlas.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.article_grouper import group_articles, is_event_expired
from app.auth import get_current_user, require_admin, require_login, router as auth_router
from app.comparator import compare_group_articles
from app.config import CATEGORIES, SOURCES
from app.feed_reader import fetch_all_feeds
from app.ai_search import (
    ai_news_search,
    ai_topics,
    ai_top_story,
    ai_weekly_summary,
    get_rate_limit_state,
    GEMINI_MODEL,
    GROQ_MODEL,
    OLLAMA_MODEL,
    is_public_topic_query,
    is_topstory_cache_valid,
    is_topics_cache_valid,
    restore_last_good_topics,
)
from app.ai_store import (
    get_provider_config,
    get_schedule_config,
    get_scheduler_config,
    init_ai_tables,
    query_ai_cost_summary,
    query_ai_daily_cost,
    query_provider_health,
    query_recent_ai_calls,
    set_provider_config,
    set_schedule_config,
    set_scheduler_interval,
    SCHEDULER_DEFAULTS,
    VALID_EVENT_TYPES,
    VALID_PROVIDERS,
    VALID_SCHEDULER_INTERVALS,
)
from app.search_utils import build_fallback_summary, extract_keywords
from app.wordcloud import build_wordcloud
from app.metrics_store import init_db, query_metrics, save_group_metrics
from app.models import Article, ArticleGroup, FeedStatus
from app.news_store import (
    init_news_tables,
    load_groups_from_db,
    purge_old_news,
    save_articles_and_groups,
    text_search_groups,
)
from app.tracking_store import (
    init_tracking_table,
    log_events,
    purge_old_events,
    purge_proxy_ip_events,
    query_anonymous_daily,
    query_anonymous_engagement,
    query_anonymous_features,
    query_anonymous_hourly,
    query_anonymous_overview,
    query_anonymous_searches,
    query_anonymous_sections,
    query_anonymous_top_content,
    query_anonymous_top_visitors,
    query_daily_activity,
    query_engagement,
    query_feature_usage,
    query_hourly_distribution,
    query_popular_searches,
    query_sections_visited,
    query_top_content,
    query_usage_stats,
)
from app.user_store import count_users, init_users_table, list_users

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = str(BASE_DIR / "static")

ART = timezone(timedelta(hours=-3))


# ── Asset cache busting ──────────────────────────────────────────────────────

_ASSET_HASHES: dict[str, str] = {}
_HTML_CACHE: dict[str, str] = {}
_BUST_RE = re.compile(r'(/static/(?:css|js)/[^"?\s]+)\?v=\d+')


def _compute_asset_hashes() -> dict[str, str]:
    """Build a map of static asset paths to short content hashes."""
    assets = {}
    static = Path(STATIC_DIR)
    for pattern in ("css/*.css", "js/*.js"):
        for filepath in static.glob(pattern):
            rel = filepath.relative_to(static).as_posix()
            digest = hashlib.md5(filepath.read_bytes()).hexdigest()[:10]
            assets[rel] = digest
    return assets


def _bust_cache(html: str) -> str:
    """Replace manual ?v=N query strings with content-based hashes."""
    def _replace(m: re.Match) -> str:
        path = m.group(1)
        rel = path[len("/static/"):]
        h = _ASSET_HASHES.get(rel, "")
        return f"{path}?h={h}" if h else m.group(0)
    return _BUST_RE.sub(_replace, html)


def _serve_html(filename: str) -> HTMLResponse:
    """Serve an HTML file with cache-busted asset URLs."""
    if filename not in _HTML_CACHE:
        filepath = os.path.join(STATIC_DIR, filename)
        raw = Path(filepath).read_text(encoding="utf-8")
        _HTML_CACHE[filename] = _bust_cache(raw) if _ASSET_HASHES else raw
    return HTMLResponse(
        _HTML_CACHE[filename],
        headers={"Cache-Control": "no-cache"},
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ensure_aware(dt: datetime) -> datetime:
    """Guarantee a datetime is timezone-aware (assume UTC if naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# ── In-memory store ──────────────────────────────────────────────────────────

_articles: list[Article] = []
_groups: list[ArticleGroup] = []
_statuses: list[FeedStatus] = []
_last_update: datetime | None = None
_wordcloud_cache: list = []
_wordcloud_updated: datetime | None = None
_lock = asyncio.Lock()


def _db_text_search(query: str, limit: int = 20) -> list[ArticleGroup]:
    """Fallback text search in DB when AI finds no results."""
    try:
        return text_search_groups(query, limit=limit)
    except Exception as exc:
        logger.warning("DB text search failed: %s", exc)
        return []


async def refresh_news():
    global _articles, _groups, _statuses, _last_update
    logger.info("Actualizando noticias de %d fuentes…", len(SOURCES))
    articles, statuses = await fetch_all_feeds()
    groups = group_articles(articles)
    async with _lock:
        _articles = articles
        _groups = groups
        _statuses = statuses
        _last_update = datetime.now(timezone.utc)
    save_group_metrics(groups)
    try:
        save_articles_and_groups(articles, groups)
    except Exception as exc:
        logger.error("Failed to persist news to DB: %s", exc)
    ok = sum(1 for s in statuses if s.status == "ok")
    logger.info(
        "Listo: %d artículos, %d grupos, %d/%d feeds OK",
        len(articles),
        len(groups),
        ok,
        len(statuses),
    )
    await refresh_wordcloud()
    asyncio.create_task(_post_refresh_catchup())


async def refresh_wordcloud():
    global _wordcloud_cache, _wordcloud_updated
    async with _lock:
        arts = list(_articles)
    words = build_wordcloud(arts)
    async with _lock:
        _wordcloud_cache = words
        _wordcloud_updated = datetime.now(timezone.utc)
    logger.info("Word cloud actualizada: %d términos", len(words))


async def _post_refresh_catchup():
    """After news refresh, fill any AI caches that are still empty."""
    try:
        if not is_topics_cache_valid():
            logger.info("Post-refresh: topics cache empty, triggering prefetch")
            await prefetch_topics()
        if not is_topstory_cache_valid():
            logger.info("Post-refresh: top story cache empty, triggering prefetch")
            await prefetch_top_story()
    except Exception as exc:
        logger.warning("Post-refresh catch-up failed: %s", exc)


async def _startup_prefetch():
    """Wait for initial news load, then pre-warm top story, weekly, and topics caches.

    Each prefetch is isolated so one failure doesn't block others.
    A brief pause between calls reduces rate-limiting risk.
    """
    for _ in range(30):
        async with _lock:
            has_groups = bool(_groups)
        if has_groups:
            break
        await asyncio.sleep(2)
    else:
        logger.warning("Startup prefetch: no groups available after 60s wait")

    for name, fn in [
        ("top_story", prefetch_top_story),
        ("weekly_summary", prefetch_weekly_summary),
        ("topics", prefetch_topics),
    ]:
        try:
            await fn()
        except Exception as exc:
            logger.error("Startup prefetch '%s' failed: %s", name, exc)
        await asyncio.sleep(1)


async def prefetch_top_story():
    """Pre-warm the top story cache so the first visitor gets an instant response."""
    today = datetime.now(ART).strftime("%Y-%m-%d")
    _articles_db, groups = load_groups_from_db(desde=today, hasta=today)
    if not groups:
        async with _lock:
            groups = list(_groups)
    if not groups:
        return
    try:
        result = await ai_top_story(groups, today)
        cached = result.get("cached", False)
        logger.info("Top story prefetch done (cached=%s)", cached)
    except Exception as exc:
        logger.warning("Top story prefetch failed: %s", exc)


async def prefetch_weekly_summary():
    """Pre-warm the weekly summary cache so the first visitor gets an instant response."""
    now = datetime.now(ART)
    days_since_monday = now.weekday()
    monday = now - timedelta(days=days_since_monday)
    week_start = monday.strftime("%Y-%m-%d")
    week_end = now.strftime("%Y-%m-%d")

    _articles_db, groups = load_groups_from_db(desde=week_start, hasta=week_end)
    if not groups:
        return
    if len(groups) > 200:
        groups = groups[:200]
    try:
        result = await ai_weekly_summary(groups, week_start, week_end, force=True)
        cached = result.get("cached", False)
        logger.info("Weekly summary prefetch done (cached=%s)", cached)
    except Exception as exc:
        logger.warning("Weekly summary prefetch failed: %s", exc)


async def prefetch_topics():
    """Pre-warm the topics cache and trigger background search prefetch."""
    async with _lock:
        grps = list(_groups)
    if not grps:
        try:
            today = datetime.now(ART).strftime("%Y-%m-%d")
            _, grps = load_groups_from_db(desde=today)
        except Exception as exc:
            logger.warning("prefetch_topics DB fallback failed: %s", exc)
    if not grps:
        return
    try:
        result = await ai_topics(grps)
        cached = result.get("cached", False)
        logger.info(
            "Topics prefetch done (cached=%s, topics=%d)",
            cached,
            len(result.get("topics", [])),
        )
    except Exception as exc:
        logger.warning("Topics prefetch failed: %s", exc)


# ── Lifecycle ────────────────────────────────────────────────────────────────

scheduler = AsyncIOScheduler(timezone=ART)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _articles, _groups, _ASSET_HASHES, _HTML_CACHE
    _ASSET_HASHES = _compute_asset_hashes()
    logger.info("Asset hashes computed: %s", _ASSET_HASHES)
    _HTML_CACHE.clear()
    try:
        init_db()
    except Exception as exc:
        logger.error("init_db failed, starting without metrics persistence: %s", exc)
    try:
        init_news_tables()
    except Exception as exc:
        logger.error("init_news_tables failed: %s", exc)
    try:
        init_users_table()
    except Exception as exc:
        logger.error("init_users_table failed: %s", exc)
    try:
        init_tracking_table()
    except Exception as exc:
        logger.error("init_tracking_table failed: %s", exc)
    try:
        init_ai_tables()
    except Exception as exc:
        logger.error("init_ai_tables failed: %s", exc)

    try:
        restore_last_good_topics()
    except Exception as exc:
        logger.error("restore_last_good_topics failed: %s", exc)

    try:
        today = datetime.now(ART).strftime("%Y-%m-%d")
        db_articles, db_groups = load_groups_from_db(desde=today)
        if db_articles:
            async with _lock:
                _articles = db_articles
                _groups = db_groups
            logger.info("Loaded %d articles from DB on startup", len(db_articles))
    except Exception as exc:
        logger.error("Failed to load news from DB on startup: %s", exc)

    asyncio.create_task(refresh_news())
    asyncio.create_task(_startup_prefetch())

    sched_cfg = get_scheduler_config()
    news_min = sched_cfg.get("refresh_news", SCHEDULER_DEFAULTS["refresh_news"])
    topics_min = sched_cfg.get("prefetch_topics", SCHEDULER_DEFAULTS["prefetch_topics"])

    scheduler.add_job(refresh_news, "interval", minutes=news_min, id="refresh_news", replace_existing=True)
    scheduler.add_job(refresh_wordcloud, "interval", hours=2)
    scheduler.add_job(prefetch_top_story, "interval", hours=3)
    scheduler.add_job(prefetch_topics, "interval", minutes=topics_min, id="prefetch_topics", replace_existing=True)
    scheduler.add_job(prefetch_weekly_summary, "cron", hour=9, minute=15)
    scheduler.add_job(prefetch_weekly_summary, "cron", hour=18, minute=0)
    scheduler.add_job(purge_old_news, "cron", hour=7, minute=0)
    scheduler.add_job(purge_old_events, "cron", hour=6, minute=30)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Vs News",
    description="Más contexto, menos relato — Agrega y compara noticias de los principales medios argentinos",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(auth_router)


# ── API routes ───────────────────────────────────────────────────────────────

@app.get("/api/noticias")
async def get_noticias(
    categoria: str | None = Query(None, description="Filtrar por categoría"),
    fuente: str | None = Query(None, description="Filtrar por fuente"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    async with _lock:
        arts = list(_articles)

    if categoria:
        arts = [a for a in arts if a.category == categoria]
    if fuente:
        arts = [a for a in arts if a.source == fuente]

    total = len(arts)
    arts = arts[offset : offset + limit]
    return {"total": total, "articles": arts}


@app.get("/api/grupos")
async def get_grupos(
    categoria: str | None = Query(None),
    solo_multifuente: bool = Query(False, description="Solo noticias con 2+ fuentes"),
    desde: str | None = Query(None, description="Fecha inicio YYYY-MM-DD"),
    hasta: str | None = Query(None, description="Fecha fin YYYY-MM-DD"),
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    async with _lock:
        grps = list(_groups)

    now_utc = datetime.now(timezone.utc)
    grps = [g for g in grps if not is_event_expired(g, now_utc)]

    if categoria:
        grps = [g for g in grps if g.category == categoria]
    if solo_multifuente:
        grps = [g for g in grps if g.source_count >= 2]
    if desde:
        desde_dt = datetime.fromisoformat(desde).replace(tzinfo=ART)
        grps = [
            g for g in grps
            if g.published and _ensure_aware(g.published) >= desde_dt
        ]
    if hasta:
        hasta_dt = datetime.fromisoformat(hasta).replace(tzinfo=ART) + timedelta(days=1)
        grps = [
            g for g in grps
            if g.published and _ensure_aware(g.published) < hasta_dt
        ]

    total = len(grps)
    grps = grps[offset : offset + limit]
    return {"total": total, "groups": grps}


@app.get("/api/grupo/{group_id}")
async def get_grupo(group_id: str):
    async with _lock:
        for g in _groups:
            if g.group_id == group_id:
                return g
    return {"error": "Grupo no encontrado"}


@app.get("/api/comparar/{group_id}")
async def comparar(group_id: str):
    """Devuelve la comparación de contenido de un grupo de noticias."""
    async with _lock:
        grupo = next((g for g in _groups if g.group_id == group_id), None)

    if not grupo:
        return {"error": "Grupo no encontrado"}

    analysis = compare_group_articles(grupo.articles)

    return {
        "group_id": grupo.group_id,
        "representative_title": grupo.representative_title,
        **analysis,
    }


@app.get("/api/fuentes")
async def get_fuentes():
    return {
        name: {"color": cfg["color"], "logo": cfg.get("logo", ""), "categories": list(cfg["feeds"].keys())}
        for name, cfg in SOURCES.items()
    }


@app.get("/api/categorias")
async def get_categorias():
    return CATEGORIES


@app.get("/api/status")
async def get_status(
    desde: str | None = Query(None, description="Fecha inicio YYYY-MM-DD"),
    hasta: str | None = Query(None, description="Fecha fin YYYY-MM-DD"),
):
    async with _lock:
        arts = list(_articles)
        grps = list(_groups)

    if desde:
        desde_dt = datetime.fromisoformat(desde).replace(tzinfo=ART)
        arts = [a for a in arts if a.published and _ensure_aware(a.published) >= desde_dt]
        grps = [g for g in grps if g.published and _ensure_aware(g.published) >= desde_dt]
    if hasta:
        hasta_dt = datetime.fromisoformat(hasta).replace(tzinfo=ART) + timedelta(days=1)
        arts = [a for a in arts if a.published and _ensure_aware(a.published) < hasta_dt]
        grps = [g for g in grps if g.published and _ensure_aware(g.published) < hasta_dt]

    return {
        "last_update": _last_update.isoformat() if _last_update else None,
        "total_articles": len(arts),
        "total_groups": len(grps),
        "multi_source_groups": sum(1 for g in grps if g.source_count >= 2),
        "feeds": _statuses,
    }


@app.get("/api/metricas")
async def get_metricas(
    desde: str | None = Query(None, description="Fecha inicio YYYY-MM-DD"),
    hasta: str | None = Query(None, description="Fecha fin YYYY-MM-DD"),
    _user: dict = Depends(require_login),
):
    """Métricas de agenda con filtro por rango de fechas (historial en SQLite)."""
    return query_metrics(desde=desde, hasta=hasta)


@app.get("/api/search")
async def ai_search(
    q: str = Query(..., min_length=2, description="Search query"),
    user: dict | None = Depends(get_current_user),
):
    """Semantic search powered by AI (Gemini/Groq).

    Free-form queries require a logged-in user. Anonymous requests are only
    allowed when the query matches one of the day's curated topic labels
    (the chips/suggestions UI), which are public by design.

    Searches ALL in-memory groups regardless of date so the AI can find
    articles from any day within the retention window.
    """
    if not user and not is_public_topic_query(q):
        return JSONResponse(
            {
                "error": "login_required",
                "message": "Iniciá sesión para buscar",
                "ai_available": False,
            },
            status_code=401,
        )

    async with _lock:
        grps = list(_groups)

    by_id = {g.group_id: g for g in grps}

    result = await ai_news_search(q, grps)
    ai_had_results = bool(result.get("has_results", False))
    ids = set(result.get("relevant_group_ids", []))

    if ids:
        result["matched_groups"] = [
            by_id[gid].model_dump(mode="json") for gid in ids if gid in by_id
        ]

    db_groups = _db_text_search(q)
    existing = {g["group_id"] for g in result.get("matched_groups", [])}
    for g in db_groups:
        if g.group_id not in existing:
            result.setdefault("matched_groups", []).append(
                g.model_dump(mode="json")
            )
            result.setdefault("relevant_group_ids", []).append(g.group_id)

    matched_groups = result.get("matched_groups", [])
    if matched_groups and not ai_had_results:
        result["has_results"] = True
        titles = [g.get("representative_title", "") for g in matched_groups]
        fallback = build_fallback_summary(
            titles, extract_keywords(q), total=len(matched_groups),
        )
        if fallback:
            result["summary"] = fallback
            result["summary_fallback"] = True

    return result


@app.get("/api/topics")
async def trending_topics():
    """Top topics of the day, powered by AI (cached 1h)."""
    async with _lock:
        grps = list(_groups)
    return await ai_topics(grps)


def _current_week_bounds() -> tuple[str, str]:
    now = datetime.now(ART)
    days_since_monday = now.weekday()
    monday = now - timedelta(days=days_since_monday)
    week_start = monday.strftime("%Y-%m-%d")
    week_end = now.strftime("%Y-%m-%d")
    return week_start, week_end


@app.get("/api/weekly-range")
async def weekly_range():
    """Rango de fechas de la semana en curso (lunes a hoy, ART) sin llamar a la IA."""
    week_start, week_end = _current_week_bounds()
    return {"week_start": week_start, "week_end": week_end}


@app.get("/api/weekly-summary")
async def weekly_summary():
    """Resumen semanal editorial generado por IA (lunes actual hasta hoy)."""
    week_start, week_end = _current_week_bounds()

    _articles_db, groups = load_groups_from_db(desde=week_start, hasta=week_end)
    if len(groups) > 200:
        groups = groups[:200]
    return await ai_weekly_summary(groups, week_start, week_end)


@app.get("/api/top-story")
async def top_story():
    """La noticia más importante del día, generada por IA."""
    today = datetime.now(ART).strftime("%Y-%m-%d")
    _articles_db, groups = load_groups_from_db(desde=today, hasta=today)
    if not groups:
        async with _lock:
            groups = list(_groups)
    return await ai_top_story(groups, today)


@app.post("/api/refresh")
async def manual_refresh():
    await refresh_news()
    return {"status": "ok", "total_articles": len(_articles)}


@app.get("/api/wordcloud")
async def get_wordcloud():
    """Términos más frecuentes en títulos de las últimas 24 horas."""
    async with _lock:
        words = list(_wordcloud_cache)
        updated = _wordcloud_updated
    return {
        "words": words,
        "updated_at": updated.isoformat() if updated else None,
    }


# ── Tracking ─────────────────────────────────────────────────────────────────

_IP_HEADERS = (
    "cf-connecting-ip",
    "x-forwarded-for",
    "x-real-ip",
    "forwarded",
)


def _resolve_client_ip(request: Request) -> str:
    for header in _IP_HEADERS:
        value = request.headers.get(header, "")
        if value:
            ip = value.split(",")[0].strip()
            if ip and not ip.startswith("100.64."):
                return ip
    return request.client.host if request.client else ""

@app.post("/api/track")
async def track_events(request: Request, user: dict | None = Depends(get_current_user)):
    """Receive a batch of usage events from the frontend."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    session_id = body.get("session_id", "")
    events = body.get("events", [])
    if not session_id or not events or not isinstance(events, list):
        return JSONResponse({"error": "session_id and events required"}, status_code=400)

    user_id = user["id"] if user else None
    ip = _resolve_client_ip(request)
    ua = request.headers.get("user-agent", "")[:500]

    try:
        count = log_events(events, user_id=user_id, session_id=session_id, ip_address=ip, user_agent=ua)
    except Exception as exc:
        logger.error("Failed to log tracking events: %s", exc)
        return JSONResponse({"error": "Failed to log events"}, status_code=500)

    return {"ok": True, "logged": count}


# ── Admin API ────────────────────────────────────────────────────────────────

@app.get("/api/admin/dashboard")
async def admin_dashboard(
    desde: str | None = Query(None),
    hasta: str | None = Query(None),
    _admin: dict = Depends(require_admin),
):
    """Aggregated admin dashboard stats."""
    stats = query_usage_stats(desde=desde, hasta=hasta)
    features = query_feature_usage(desde=desde, hasta=hasta)
    engagement = query_engagement(desde=desde, hasta=hasta)
    sections = query_sections_visited(desde=desde, hasta=hasta)
    return {
        "total_users": count_users(),
        "usage": stats,
        "features": features,
        "engagement": engagement,
        "sections": sections,
    }


@app.get("/api/admin/users")
async def admin_users(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: dict = Depends(require_admin),
):
    return {"users": list_users(limit=limit, offset=offset), "total": count_users()}


@app.get("/api/admin/popular-searches")
async def admin_popular_searches(
    limit: int = Query(20, ge=1, le=100),
    _admin: dict = Depends(require_admin),
):
    return {"searches": query_popular_searches(limit=limit)}


@app.get("/api/admin/top-content")
async def admin_top_content(
    limit: int = Query(20, ge=1, le=100),
    _admin: dict = Depends(require_admin),
):
    return {"content": query_top_content(limit=limit)}


@app.get("/api/admin/daily-activity")
async def admin_daily_activity(
    desde: str | None = Query(None),
    hasta: str | None = Query(None),
    _admin: dict = Depends(require_admin),
):
    return {"days": query_daily_activity(desde=desde, hasta=hasta)}


@app.get("/api/admin/hourly")
async def admin_hourly(
    desde: str | None = Query(None),
    hasta: str | None = Query(None),
    _admin: dict = Depends(require_admin),
):
    return {"hours": query_hourly_distribution(desde=desde, hasta=hasta)}


@app.get("/api/admin/anonymous")
async def admin_anonymous(
    desde: str | None = Query(None),
    hasta: str | None = Query(None),
    _admin: dict = Depends(require_admin),
):
    """Full anonymous visitors dashboard in a single call."""
    return {
        "overview": query_anonymous_overview(desde=desde, hasta=hasta),
        "engagement": query_anonymous_engagement(desde=desde, hasta=hasta),
        "sections": query_anonymous_sections(desde=desde, hasta=hasta),
        "features": query_anonymous_features(desde=desde, hasta=hasta),
        "top_content": query_anonymous_top_content(desde=desde, hasta=hasta),
        "searches": query_anonymous_searches(desde=desde, hasta=hasta),
        "daily": query_anonymous_daily(desde=desde, hasta=hasta),
        "hourly": query_anonymous_hourly(desde=desde, hasta=hasta),
        "top_visitors": query_anonymous_top_visitors(desde=desde, hasta=hasta),
    }


@app.get("/api/admin/debug-headers")
async def admin_debug_headers(request: Request, _admin: dict = Depends(require_admin)):
    """Show request headers for debugging IP resolution behind proxies."""
    return {
        "client_host": request.client.host if request.client else None,
        "x-forwarded-for": request.headers.get("x-forwarded-for"),
        "x-real-ip": request.headers.get("x-real-ip"),
        "forwarded": request.headers.get("forwarded"),
        "all_headers": dict(request.headers),
    }


@app.post("/api/admin/purge-proxy-events")
async def admin_purge_proxy_events(_admin: dict = Depends(require_admin)):
    """Delete anonymous tracking events that have Railway proxy IPs (100.64.x.x)."""
    deleted = purge_proxy_ip_events()
    return {"deleted": deleted}


@app.get("/api/admin/ai-cost")
async def admin_ai_cost(
    desde: str | None = Query(None),
    hasta: str | None = Query(None),
    _admin: dict = Depends(require_admin),
):
    """AI usage cost summary and daily breakdown."""
    return {
        "summary": query_ai_cost_summary(desde=desde, hasta=hasta),
        "daily": query_ai_daily_cost(desde=desde, hasta=hasta),
    }


@app.get("/api/admin/ai-config")
async def admin_ai_config_get(_admin: dict = Depends(require_admin)):
    """Return current AI provider configuration per event type."""
    config = get_provider_config()
    schedule = get_schedule_config()
    return {
        "config": config,
        "schedule": schedule,
        "valid_providers": sorted(VALID_PROVIDERS),
        "valid_event_types": sorted(VALID_EVENT_TYPES),
    }


_PROVIDER_ENV_KEYS: dict[str, str] = {
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "ollama": "OLLAMA_BASE_URL",
}


def _build_provider_status(provider_key: str, model: str) -> dict:
    """Build a health snapshot for a single AI provider.

    Combines env-var presence, the in-memory Gemini rate-limit cooldown
    and recent rows from ``ai_usage_log`` to derive a traffic-light status.
    """
    env_key = _PROVIDER_ENV_KEYS.get(provider_key, "")
    configured = bool(os.environ.get(env_key)) if env_key else False
    health = query_provider_health(provider_key)

    rate_limit = {"active": False, "seconds_remaining": 0}
    if provider_key == "gemini":
        rate_limit = get_rate_limit_state()

    last_success = health.get("last_success")
    last_error = health.get("last_error")
    recent_calls = health.get("recent_calls", 0) or 0
    recent_success = health.get("recent_success_count", 0) or 0
    success_rate = (recent_success / recent_calls) if recent_calls else None
    errors_last_window = health.get("errors_last_window", 0) or 0

    def _is_newer(a: dict | None, b: dict | None) -> bool:
        if not a:
            return False
        if not b:
            return True
        return (a.get("created_at") or "") > (b.get("created_at") or "")

    last_call_was_error = _is_newer(last_error, last_success)

    if not configured:
        status = "red"
    elif rate_limit["active"]:
        status = "red"
    elif last_call_was_error:
        status = "red"
    elif success_rate is not None and success_rate < 0.8:
        status = "amber"
    elif errors_last_window > 0:
        status = "amber"
    else:
        status = "green"

    return {
        "provider": provider_key,
        "model": model,
        "configured": configured,
        "status": status,
        "rate_limit_active": rate_limit["active"],
        "rate_limit_seconds_remaining": rate_limit["seconds_remaining"],
        "last_success": last_success,
        "last_error": last_error,
        "recent_calls": recent_calls,
        "recent_success_count": recent_success,
        "success_rate": round(success_rate, 4) if success_rate is not None else None,
        "errors_last_window": errors_last_window,
    }


@app.get("/api/admin/ai-monitor")
async def admin_ai_monitor(_admin: dict = Depends(require_admin)):
    """Live AI-provider health snapshot plus the 5 most recent invocations."""
    return {
        "providers": [
            _build_provider_status("gemini", GEMINI_MODEL),
            _build_provider_status("groq", GROQ_MODEL),
            _build_provider_status("ollama", OLLAMA_MODEL),
        ],
        "recent_calls": query_recent_ai_calls(limit=5),
    }


@app.post("/api/admin/ai-config")
async def admin_ai_config_set(
    request: Request,
    _admin: dict = Depends(require_admin),
):
    """Update AI provider for a specific event type."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    event_type = body.get("event_type", "")
    provider = body.get("provider", "")

    if event_type not in VALID_EVENT_TYPES:
        return JSONResponse(
            {"error": f"Invalid event_type. Valid: {sorted(VALID_EVENT_TYPES)}"},
            status_code=400,
        )
    if provider not in VALID_PROVIDERS:
        return JSONResponse(
            {"error": f"Invalid provider. Valid: {sorted(VALID_PROVIDERS)}"},
            status_code=400,
        )

    ok = set_provider_config(event_type, provider)
    if not ok:
        return JSONResponse({"error": "Failed to update"}, status_code=500)
    return {"ok": True, "event_type": event_type, "provider": provider}


@app.post("/api/admin/ai-schedule")
async def admin_ai_schedule_set(
    request: Request,
    _admin: dict = Depends(require_admin),
):
    """Update quiet-hours schedule for a specific event type."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    event_type = body.get("event_type", "")
    quiet_start = body.get("quiet_start", "")
    quiet_end = body.get("quiet_end", "")

    if event_type not in VALID_EVENT_TYPES:
        return JSONResponse(
            {"error": f"Invalid event_type. Valid: {sorted(VALID_EVENT_TYPES)}"},
            status_code=400,
        )

    ok = set_schedule_config(event_type, quiet_start, quiet_end)
    if not ok:
        return JSONResponse({"error": "Invalid schedule values"}, status_code=400)
    return {"ok": True, "event_type": event_type, "quiet_start": quiet_start, "quiet_end": quiet_end}


@app.get("/api/admin/scheduler-config")
async def admin_scheduler_config_get(_admin: dict = Depends(require_admin)):
    """Return current scheduler interval configuration."""
    config = get_scheduler_config()
    return {
        "config": config,
        "defaults": SCHEDULER_DEFAULTS,
        "valid_intervals": {k: v for k, v in VALID_SCHEDULER_INTERVALS.items()},
    }


@app.post("/api/admin/scheduler-config")
async def admin_scheduler_config_set(
    request: Request,
    _admin: dict = Depends(require_admin),
):
    """Update a scheduler job interval and reschedule the running job."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    job_key = body.get("job_key", "")
    interval_minutes = body.get("interval_minutes")

    if job_key not in VALID_SCHEDULER_INTERVALS:
        return JSONResponse(
            {"error": f"Invalid job_key. Valid: {sorted(VALID_SCHEDULER_INTERVALS)}"},
            status_code=400,
        )
    if not isinstance(interval_minutes, int) or interval_minutes not in VALID_SCHEDULER_INTERVALS[job_key]:
        return JSONResponse(
            {"error": f"Invalid interval. Valid: {VALID_SCHEDULER_INTERVALS[job_key]}"},
            status_code=400,
        )

    ok = set_scheduler_interval(job_key, interval_minutes)
    if not ok:
        return JSONResponse({"error": "Failed to update"}, status_code=500)

    try:
        scheduler.reschedule_job(job_key, trigger="interval", minutes=interval_minutes)
        logger.info("Rescheduled job %s to every %d minutes", job_key, interval_minutes)
    except Exception as exc:
        logger.warning("Failed to reschedule job %s: %s", job_key, exc)

    return {"ok": True, "job_key": job_key, "interval_minutes": interval_minutes}


# ── Health check ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


# ── Page helpers ──────────────────────────────────────────────────────────


# ── Admin page ───────────────────────────────────────────────────────────

@app.get("/admin")
async def admin_page(user: dict | None = Depends(get_current_user)):
    if not user or user.get("role") != "admin":
        return RedirectResponse("/")
    return _serve_html("admin.html")


@app.get("/privacy")
async def privacy_page():
    return _serve_html("privacy.html")


@app.get("/terms")
async def terms_page():
    return _serve_html("terms.html")


# ── Static files ─────────────────────────────────────────────────────────

_STATIC_CACHE_HEADER = b"public, max-age=31536000, immutable"


class _CacheStaticMiddleware:
    """Wrap StaticFiles to add aggressive Cache-Control for hashed assets."""

    def __init__(self, app_inner):
        self.app_inner = app_inner

    async def __call__(self, scope, receive, send):
        async def send_with_cache(message):
            if message["type"] == "http.response.start":
                headers = [
                    (k, v) for k, v in message.get("headers", [])
                    if k.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", _STATIC_CACHE_HEADER))
                message = {**message, "headers": headers}
            await send(message)
        await self.app_inner(scope, receive, send_with_cache)


_static_app = StaticFiles(directory=STATIC_DIR)
app.mount("/static", _CacheStaticMiddleware(_static_app), name="static")


@app.get("/")
async def index():
    return _serve_html("index.html")
