"""
Comparador de Noticias — API principal.
Agrega noticias de medios argentinos y permite compararlas.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
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
    is_topstory_cache_valid,
    is_topics_cache_valid,
)
from app.ai_store import (
    get_provider_config,
    get_schedule_config,
    init_ai_tables,
    query_ai_cost_summary,
    query_ai_daily_cost,
    set_provider_config,
    set_schedule_config,
    VALID_EVENT_TYPES,
    VALID_PROVIDERS,
)
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
    global _articles, _groups
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
    scheduler.add_job(refresh_news, "interval", minutes=10)
    scheduler.add_job(refresh_wordcloud, "interval", hours=2)
    scheduler.add_job(prefetch_top_story, "interval", hours=3)
    scheduler.add_job(prefetch_topics, "interval", hours=1)
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
):
    """Semantic search powered by AI (Gemini/Groq).

    Searches ALL in-memory groups regardless of date so the AI can find
    articles from any day within the retention window.
    """
    async with _lock:
        grps = list(_groups)

    by_id = {g.group_id: g for g in grps}

    result = await ai_news_search(q, grps)
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
    if result.get("matched_groups") and not result.get("has_results", False):
        result["has_results"] = True

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
    # Limitar contexto para que la IA responda a tiempo
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


# ── Health check ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


# ── Admin page ───────────────────────────────────────────────────────────────

@app.get("/admin")
async def admin_page(user: dict | None = Depends(get_current_user)):
    if not user or user.get("role") != "admin":
        return RedirectResponse("/")
    return FileResponse(os.path.join(STATIC_DIR, "admin.html"))


@app.get("/privacy")
async def privacy_page():
    return FileResponse(os.path.join(STATIC_DIR, "privacy.html"))


@app.get("/terms")
async def terms_page():
    return FileResponse(os.path.join(STATIC_DIR, "terms.html"))


# ── Static files ─────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
