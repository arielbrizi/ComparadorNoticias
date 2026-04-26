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
    compute_daily_cap,
    count_ai_invocations,
    get_global_monthly_budget,
    get_ollama_timeout,
    get_provider_config,
    get_provider_limits,
    get_schedule_config,
    get_scheduler_config,
    init_ai_tables,
    is_default_provider_limit,
    list_distinct_providers,
    MAX_PROVIDER_CHAIN,
    query_ai_cost_summary,
    query_ai_daily_cost,
    query_ai_invocations,
    query_global_cost_summary,
    query_provider_cost_summary,
    query_provider_health,
    query_provider_usage,
    query_recent_ai_calls,
    reset_provider_limits,
    set_global_monthly_budget,
    set_ollama_timeout,
    set_provider_config,
    set_provider_limits,
    set_schedule_config,
    set_scheduler_interval,
    OLLAMA_TIMEOUT_DEFAULT,
    OLLAMA_TIMEOUT_MAX,
    OLLAMA_TIMEOUT_MIN,
    PROVIDER_LIMIT_DEFAULTS,
    SCHEDULER_DEFAULTS,
    VALID_EVENT_TYPES,
    VALID_PROVIDERS,
    VALID_SCHEDULER_INTERVALS,
)
from app.process_events_store import (
    count_process_events,
    init_process_events_table,
    list_known_components,
    log_process_event,
    query_process_events,
    purge_old_events as purge_old_process_events,
)
from app.infra_cost_store import (
    history as infra_cost_history,
    init_infra_cost_table,
    latest_snapshot as infra_latest_snapshot,
    purge_old_snapshots as purge_old_infra_snapshots,
    save_snapshot as save_infra_snapshot,
)
from app import railway_client, x_campaigns, x_client, x_store
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
    asyncio.create_task(_maybe_trigger_breaking())


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


async def _refresh_infra_costs() -> None:
    """Fetch Railway usage and persist a snapshot. Runs in a worker thread."""
    try:
        result = await asyncio.to_thread(railway_client.fetch_usage)
    except Exception as exc:
        logger.warning("refresh_infra_costs fetch failed: %s", exc)
        return

    if not result.get("available"):
        logger.info("Skipping infra snapshot (not available: %s)", result.get("reason"))
        return

    services = result.get("services") or []
    inserted = save_infra_snapshot(services)
    logger.info("Infra snapshot saved: %d rows", inserted)


def _wrap_job(func, component: str, event_type: str):
    """Return a wrapper (async or sync, matching *func*) that logs every run.

    Measures duration, catches exceptions so one failed job doesn't crash the
    scheduler, and records an ``ok``/``error`` row per execution in the
    ``process_events`` table.
    """
    import time as _time
    from functools import wraps
    import inspect

    def _log_ok(dur_ms: int) -> None:
        try:
            log_process_event(
                component=component, event_type=event_type,
                status="ok", duration_ms=dur_ms,
            )
        except Exception:
            pass

    def _log_err(dur_ms: int, exc: Exception) -> None:
        try:
            log_process_event(
                component=component, event_type=event_type,
                status="error", duration_ms=dur_ms,
                message=str(exc)[:500],
            )
        except Exception:
            pass

    if inspect.iscoroutinefunction(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            t0 = _time.monotonic()
            try:
                result = await func(*args, **kwargs)
                _log_ok(int((_time.monotonic() - t0) * 1000))
                return result
            except Exception as exc:
                _log_err(int((_time.monotonic() - t0) * 1000), exc)
                logger.exception("Scheduler job %s/%s failed", component, event_type)
                raise
        return async_wrapper

    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        t0 = _time.monotonic()
        try:
            result = func(*args, **kwargs)
            _log_ok(int((_time.monotonic() - t0) * 1000))
            return result
        except Exception as exc:
            _log_err(int((_time.monotonic() - t0) * 1000), exc)
            logger.exception("Scheduler job %s/%s failed", component, event_type)
            raise

    return sync_wrapper


# ── X campaign schedulers ────────────────────────────────────────────────────

# Map de campaign_key → job id en el scheduler. Les damos un prefijo para no
# chocar con los jobs del pipeline de IA / RSS.
_X_JOB_PREFIX = "x_campaign_"

_X_DAY_OF_WEEK_VALUES = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


async def _run_cloud_job():
    async with _lock:
        words = list(_wordcloud_cache)
    return await asyncio.to_thread(x_campaigns.run_cloud_campaign, words)


async def _run_topstory_job():
    today = datetime.now(ART).strftime("%Y-%m-%d")
    _articles_db, groups = load_groups_from_db(desde=today, hasta=today)
    if not groups:
        async with _lock:
            groups = list(_groups)
    result = await ai_top_story(groups, today)
    story = result.get("story") if isinstance(result, dict) else None
    return await asyncio.to_thread(x_campaigns.run_topstory_campaign, story)


async def _run_weekly_job():
    week_start, week_end = _current_week_bounds()
    _articles_db, groups = load_groups_from_db(desde=week_start, hasta=week_end)
    if len(groups) > 200:
        groups = groups[:200]
    weekly = await ai_weekly_summary(groups, week_start, week_end)
    return await asyncio.to_thread(
        x_campaigns.run_weekly_campaign, weekly,
        week_start=week_start, week_end=week_end,
    )


async def _run_topics_job():
    async with _lock:
        grps = list(_groups)
    topics = await ai_topics(grps) if grps else {"topics": []}
    return await asyncio.to_thread(x_campaigns.run_topics_campaign, topics)


_X_JOB_RUNNERS: dict[str, callable] = {
    "cloud": _run_cloud_job,
    "topstory": _run_topstory_job,
    "weekly": _run_weekly_job,
    "topics": _run_topics_job,
}


def _clamp_hour_minute(schedule: dict) -> tuple[int, int]:
    try:
        hour = int(schedule.get("hour", 9))
        minute = int(schedule.get("minute", 0))
    except (TypeError, ValueError):
        hour, minute = 9, 0
    hour = max(0, min(23, hour))
    minute = max(0, min(59, minute))
    return hour, minute


def reschedule_x_campaigns() -> None:
    """Sincroniza los jobs de X en el scheduler con la config actual en DB.

    - Agrega/actualiza un job cron por cada campaña scheduled habilitada.
    - Borra el job si la campaña está deshabilitada o cambió su tipo.
    - ``breaking`` no tiene job: se dispara desde ``refresh_news``.
    """
    for campaign in x_store.list_campaigns():
        key = campaign["campaign_key"]
        job_id = f"{_X_JOB_PREFIX}{key}"

        if key == "breaking" or key not in _X_JOB_RUNNERS:
            _remove_job_safe(job_id)
            continue

        if not campaign["enabled"]:
            _remove_job_safe(job_id)
            continue

        runner = _X_JOB_RUNNERS[key]
        schedule = campaign.get("schedule") or {}
        hour, minute = _clamp_hour_minute(schedule)

        kwargs: dict = {"hour": hour, "minute": minute}
        if key == "weekly":
            dow = str(schedule.get("day_of_week", "mon")).lower()
            if dow not in _X_DAY_OF_WEEK_VALUES:
                dow = "mon"
            kwargs["day_of_week"] = dow

        try:
            scheduler.add_job(
                _wrap_job(runner, "x", f"campaign_{key}"),
                "cron", id=job_id, replace_existing=True, **kwargs,
            )
            logger.info("X campaign %s scheduled (%s)", key, kwargs)
        except Exception as exc:
            logger.error("Failed to schedule x campaign %s: %s", key, exc)


def _remove_job_safe(job_id: str) -> None:
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass


async def _maybe_trigger_breaking():
    """Evalúa si corresponde un breaking post con los grupos actuales."""
    cfg = x_store.get_campaign_config("breaking")
    if not cfg or not cfg.get("enabled"):
        return
    if not x_client.is_configured():
        return

    sched = cfg.get("schedule") or {}
    min_sources = int(sched.get("min_source_count", 3) or 3)
    cats = sched.get("categories") or []

    async with _lock:
        grps = list(_groups)

    candidate = x_campaigns.pick_breaking_candidate(
        grps,
        min_source_count=min_sources,
        allowed_categories=cats,
    )
    if not candidate:
        return
    try:
        await asyncio.to_thread(x_campaigns.run_breaking_campaign, candidate)
    except Exception as exc:
        logger.warning("breaking campaign trigger failed: %s", exc)


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
        init_process_events_table()
    except Exception as exc:
        logger.error("init_process_events_table failed: %s", exc)
    try:
        init_infra_cost_table()
    except Exception as exc:
        logger.error("init_infra_cost_table failed: %s", exc)
    try:
        x_store.init_x_tables()
    except Exception as exc:
        logger.error("init_x_tables failed: %s", exc)

    try:
        log_process_event(component="lifespan", event_type="startup", status="info",
                          message="Application starting up")
    except Exception:
        pass

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

    scheduler.add_job(_wrap_job(refresh_news, "rss", "refresh_news"),
                      "interval", minutes=news_min, id="refresh_news", replace_existing=True)
    scheduler.add_job(_wrap_job(refresh_wordcloud, "scheduler", "refresh_wordcloud"),
                      "interval", hours=2)
    scheduler.add_job(_wrap_job(prefetch_top_story, "ai", "prefetch_top_story"),
                      "interval", hours=3)
    scheduler.add_job(_wrap_job(prefetch_topics, "ai", "prefetch_topics"),
                      "interval", minutes=topics_min, id="prefetch_topics", replace_existing=True)
    scheduler.add_job(_wrap_job(prefetch_weekly_summary, "ai", "prefetch_weekly_summary"),
                      "cron", hour=9, minute=15)
    scheduler.add_job(_wrap_job(prefetch_weekly_summary, "ai", "prefetch_weekly_summary"),
                      "cron", hour=18, minute=0)
    scheduler.add_job(_wrap_job(purge_old_news, "scheduler", "purge_old_news"),
                      "cron", hour=7, minute=0)
    scheduler.add_job(_wrap_job(purge_old_events, "scheduler", "purge_old_events"),
                      "cron", hour=6, minute=30)
    scheduler.add_job(_wrap_job(purge_old_process_events, "scheduler", "purge_old_process_events"),
                      "cron", hour=6, minute=45)

    if railway_client.is_configured():
        scheduler.add_job(_wrap_job(_refresh_infra_costs, "railway", "refresh_infra_costs"),
                          "interval", hours=1, id="refresh_infra_costs", replace_existing=True)
        scheduler.add_job(_wrap_job(purge_old_infra_snapshots, "railway", "purge_infra_snapshots"),
                          "cron", hour=7, minute=15)
        asyncio.create_task(_refresh_infra_costs())

    try:
        reschedule_x_campaigns()
    except Exception as exc:
        logger.error("reschedule_x_campaigns on startup failed: %s", exc)
    scheduler.add_job(_wrap_job(x_store.purge_old_x_usage, "x", "purge_x_usage"),
                      "cron", hour=7, minute=30)

    scheduler.start()
    try:
        log_process_event(component="lifespan", event_type="scheduler_started", status="info",
                          message=f"refresh_news={news_min}m, prefetch_topics={topics_min}m")
    except Exception:
        pass
    yield
    try:
        log_process_event(component="lifespan", event_type="shutdown", status="info",
                          message="Application shutting down")
    except Exception:
        pass
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


def _clamp_page(page: int, page_size: int) -> tuple[int, int]:
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 25), 200))
    return page, page_size


@app.get("/api/admin/ai-invocations")
async def admin_ai_invocations(
    desde: str | None = None,
    hasta: str | None = None,
    provider: str | None = None,
    event_type: str | None = None,
    success: str | None = None,
    page: int = 1,
    page_size: int = 25,
    _admin: dict = Depends(require_admin),
):
    """Paginated list of AI invocations with optional filters."""
    page, page_size = _clamp_page(page, page_size)
    offset = (page - 1) * page_size

    success_bool: bool | None
    if success is None or success == "":
        success_bool = None
    elif str(success).lower() in ("1", "true", "yes"):
        success_bool = True
    elif str(success).lower() in ("0", "false", "no"):
        success_bool = False
    else:
        success_bool = None

    prov = provider or None
    ev = event_type or None

    items = query_ai_invocations(
        desde=desde, hasta=hasta, provider=prov, event_type=ev,
        success=success_bool, limit=page_size, offset=offset,
    )
    total = count_ai_invocations(
        desde=desde, hasta=hasta, provider=prov, event_type=ev, success=success_bool,
    )
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "filters": {
            "providers": list_distinct_providers(),
            "event_types": sorted(VALID_EVENT_TYPES),
        },
    }


@app.get("/api/admin/ollama-logs")
async def admin_ollama_logs(
    limit: int = 200,
    filter: str | None = None,
    service: str | None = None,
    _admin: dict = Depends(require_admin),
):
    """Return recent log lines from the Railway-hosted Ollama service.

    Backed by Railway's ``deploymentLogs`` GraphQL query. ``service`` lets
    the caller override the default service name (env
    ``RAILWAY_OLLAMA_SERVICE_NAME``, default ``"ollama"``) — useful when
    the team renamed the Railway service. ``filter`` is passed through to
    Railway (supports substring matches on message text).
    """
    if not railway_client.is_configured():
        return {"available": False, "reason": "no_token"}

    try:
        result = await asyncio.to_thread(
            railway_client.fetch_service_logs,
            service,
            limit=limit,
            filter=filter or None,
        )
    except Exception as exc:
        logger.warning("admin_ollama_logs failed: %s", exc)
        return {"available": False, "reason": f"error: {exc}"}

    return result


@app.get("/api/admin/infra-costs")
async def admin_infra_costs(_admin: dict = Depends(require_admin)):
    """Return the latest Railway cost snapshot plus a short daily history."""
    if not railway_client.is_configured():
        return {"available": False, "reason": "no_token"}

    snap = infra_latest_snapshot()
    hist = infra_cost_history(days=14)

    services = []
    for s in snap.get("services", []):
        services.append({
            "service_name": s.get("service_name"),
            "service_id": s.get("service_id"),
            "estimated_usd_month": s.get("estimated_usd_month"),
        })

    return {
        "available": True,
        "fetched_at": snap.get("fetched_at"),
        "services": services,
        "total_usd_month": snap.get("total_usd_month", 0.0),
        "history": hist,
    }


@app.post("/api/admin/infra-costs/refresh")
async def admin_infra_costs_refresh(_admin: dict = Depends(require_admin)):
    """Force an immediate fetch from Railway and persist a snapshot.

    Returns the raw result from `railway_client.fetch_usage`, plus the number
    of rows saved. Useful as a diagnostic tool from the admin panel to see
    the real reason when the scheduled refresh isn't producing snapshots.
    """
    if not railway_client.is_configured():
        return {"available": False, "reason": "no_token", "saved_rows": 0}

    try:
        result = await asyncio.to_thread(railway_client.fetch_usage)
    except Exception as exc:
        logger.warning("manual infra refresh failed: %s", exc)
        return {"available": False, "reason": f"error: {exc}", "saved_rows": 0}

    saved_rows = 0
    if result.get("available"):
        services = result.get("services") or []
        saved_rows = save_infra_snapshot(services)
        logger.info("Manual infra snapshot saved: %d rows", saved_rows)
        try:
            log_process_event(
                component="railway",
                event_type="manual_refresh",
                status="ok",
                details={"saved_rows": saved_rows, "services": len(services)},
            )
        except Exception:
            pass
    else:
        logger.info("Manual infra refresh skipped (not available: %s)", result.get("reason"))
        try:
            log_process_event(
                component="railway",
                event_type="manual_refresh",
                status="warning",
                message=f"Railway unavailable: {result.get('reason')}",
                details={"reason": result.get("reason")},
            )
        except Exception:
            pass

    return {**result, "saved_rows": saved_rows}


@app.get("/api/admin/process-events")
async def admin_process_events(
    desde: str | None = None,
    hasta: str | None = None,
    component: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 25,
    _admin: dict = Depends(require_admin),
):
    """Paginated list of background process events (scheduler runs, lifespan, etc)."""
    page, page_size = _clamp_page(page, page_size)
    offset = (page - 1) * page_size

    comp = component or None
    stat = status or None

    items = query_process_events(
        desde=desde, hasta=hasta, component=comp, status=stat,
        limit=page_size, offset=offset,
    )
    total = count_process_events(
        desde=desde, hasta=hasta, component=comp, status=stat,
    )
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "filters": {
            "components": list_known_components(),
            "statuses": ["ok", "error", "warning", "info"],
        },
    }


@app.post("/api/admin/ai-config")
async def admin_ai_config_set(
    request: Request,
    _admin: dict = Depends(require_admin),
):
    """Update AI provider chain for a specific event type.

    Accepts ``{"event_type": "...", "providers": ["gemini", "groq", ...]}``
    with 1..4 unique provider keys. For backward compatibility a legacy
    payload with ``{"provider": "gemini_fallback_groq"}`` is also accepted
    and converted on the fly.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    event_type = body.get("event_type", "")
    providers_raw = body.get("providers")
    legacy = body.get("provider")

    if providers_raw is None and isinstance(legacy, str):
        if "_fallback_" in legacy:
            primary, _, secondary = legacy.partition("_fallback_")
            providers_raw = [primary, secondary]
        else:
            providers_raw = [legacy]

    if event_type not in VALID_EVENT_TYPES:
        return JSONResponse(
            {"error": f"Invalid event_type. Valid: {sorted(VALID_EVENT_TYPES)}"},
            status_code=400,
        )
    if not isinstance(providers_raw, list) or not providers_raw:
        return JSONResponse(
            {"error": "providers must be a non-empty list"},
            status_code=400,
        )
    if len(providers_raw) > MAX_PROVIDER_CHAIN:
        return JSONResponse(
            {"error": f"providers length must be ≤ {MAX_PROVIDER_CHAIN}"},
            status_code=400,
        )

    chain: list[str] = []
    for item in providers_raw:
        if not isinstance(item, str) or item not in VALID_PROVIDERS:
            return JSONResponse(
                {"error": f"Invalid provider. Valid: {sorted(VALID_PROVIDERS)}"},
                status_code=400,
            )
        if item in chain:
            return JSONResponse(
                {"error": "providers must not contain duplicates"},
                status_code=400,
            )
        chain.append(item)

    ok = set_provider_config(event_type, chain)
    if not ok:
        return JSONResponse({"error": "Failed to update"}, status_code=500)
    return {"ok": True, "event_type": event_type, "providers": chain}


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


@app.get("/api/ai-config")
async def ai_config_public():
    """Public AI config used by the frontend to size client-side timeouts.

    The browser needs to know how long the backend is willing to wait for an
    AI search so its ``AbortController`` timeout stays >= the server's, plus
    a small margin. Otherwise the client aborts while the server keeps
    working and logs a successful invocation the user never sees.
    """
    return {
        "search_timeout_seconds": get_ollama_timeout(),
    }


@app.get("/api/admin/ollama-config")
async def admin_ollama_config_get(_admin: dict = Depends(require_admin)):
    """Return the current Ollama invocation timeout and its allowed bounds."""
    return {
        "timeout_seconds": get_ollama_timeout(),
        "default": OLLAMA_TIMEOUT_DEFAULT,
        "min": OLLAMA_TIMEOUT_MIN,
        "max": OLLAMA_TIMEOUT_MAX,
    }


@app.post("/api/admin/ollama-config")
async def admin_ollama_config_set(
    request: Request,
    _admin: dict = Depends(require_admin),
):
    """Update the Ollama invocation timeout (seconds)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    timeout_seconds = body.get("timeout_seconds")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
        return JSONResponse(
            {"error": "timeout_seconds must be an integer"},
            status_code=400,
        )
    if timeout_seconds < OLLAMA_TIMEOUT_MIN or timeout_seconds > OLLAMA_TIMEOUT_MAX:
        return JSONResponse(
            {
                "error": (
                    f"timeout_seconds out of range "
                    f"[{OLLAMA_TIMEOUT_MIN}, {OLLAMA_TIMEOUT_MAX}]"
                )
            },
            status_code=400,
        )

    ok = set_ollama_timeout(timeout_seconds)
    if not ok:
        return JSONResponse({"error": "Failed to update"}, status_code=500)
    return {"ok": True, "timeout_seconds": timeout_seconds}


# ── AI provider limits (quota guard) ─────────────────────────────────────────

_QUOTA_LIMIT_FIELDS = ("rpm", "tpm", "rpd", "tpd")


def _build_provider_limit_row(provider: str, model: str) -> dict:
    """Merge configured limits with live usage for the UI."""
    limits = get_provider_limits().get((provider, model)) or {}
    usage = query_provider_usage(provider)
    cost = query_provider_cost_summary(provider)

    blocked_by: list[str] = []
    for f in _QUOTA_LIMIT_FIELDS:
        lim = limits.get(f)
        used = usage.get(f + "_used", 0)
        if lim is not None and used >= lim:
            blocked_by.append(f)

    monthly_usd = limits.get("monthly_usd")
    month_used = float(cost.get("month_used", 0.0) or 0.0)
    today_used = float(cost.get("today_used", 0.0) or 0.0)
    daily_cap = compute_daily_cap(monthly_usd, month_used) if monthly_usd is not None else None

    if monthly_usd is not None and month_used >= float(monthly_usd):
        blocked_by.append("monthly_usd")
    if daily_cap is not None and today_used >= daily_cap:
        blocked_by.append("daily_usd")

    return {
        "provider": provider,
        "model": model,
        "rpm": limits.get("rpm"),
        "tpm": limits.get("tpm"),
        "rpd": limits.get("rpd"),
        "tpd": limits.get("tpd"),
        "monthly_usd": monthly_usd,
        "rpm_used": usage.get("rpm_used", 0),
        "tpm_used": usage.get("tpm_used", 0),
        "rpd_used": usage.get("rpd_used", 0),
        "tpd_used": usage.get("tpd_used", 0),
        "monthly_usd_used": round(month_used, 6),
        "daily_usd_used": round(today_used, 6),
        "daily_usd_cap": round(daily_cap, 6) if daily_cap is not None else None,
        "is_default": is_default_provider_limit(provider, model),
        "blocked_by": blocked_by,
    }


def _build_global_budget_row() -> dict:
    """Snapshot of the global USD budget + live cost (month/day)."""
    budget = get_global_monthly_budget()
    cost = query_global_cost_summary()
    month_used = float(cost.get("month_used", 0.0) or 0.0)
    today_used = float(cost.get("today_used", 0.0) or 0.0)
    daily_cap = compute_daily_cap(budget, month_used) if budget is not None else None

    blocked_by: list[str] = []
    if budget is not None and month_used >= float(budget):
        blocked_by.append("monthly_usd_global")
    if daily_cap is not None and today_used >= daily_cap:
        blocked_by.append("daily_usd_global")

    return {
        "monthly_usd": budget,
        "monthly_usd_used": round(month_used, 6),
        "daily_usd_used": round(today_used, 6),
        "daily_usd_cap": round(daily_cap, 6) if daily_cap is not None else None,
        "blocked_by": blocked_by,
    }


@app.get("/api/admin/ai-limits")
async def admin_ai_limits_get(_admin: dict = Depends(require_admin)):
    """Return configured limits + live usage for each (provider, model)."""
    items = [
        _build_provider_limit_row(provider, model)
        for (provider, model) in PROVIDER_LIMIT_DEFAULTS.keys()
    ]
    return {
        "items": items,
        "defaults": {
            f"{p}/{m}": dict(v) for (p, m), v in PROVIDER_LIMIT_DEFAULTS.items()
        },
        "global": _build_global_budget_row(),
    }


@app.post("/api/admin/ai-limits")
async def admin_ai_limits_set(
    request: Request,
    _admin: dict = Depends(require_admin),
):
    """Update (or reset) the quota limits for a (provider, model) pair.

    Body shape::

        {
            "provider": "gemini",
            "model": "gemini-3-flash-preview",
            "rpm": 10 | null, "tpm": 250000 | null,
            "rpd": 250 | null, "tpd": null,
            "reset": false
        }

    ``reset: true`` deletes the override row and restores defaults. Otherwise
    each of the 4 fields accepts an integer >= 0 or ``null`` ("sin límite").
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    provider = body.get("provider", "")
    model = body.get("model", "")

    if provider not in {"gemini", "groq", "ollama"}:
        return JSONResponse(
            {"error": "Invalid provider. Valid: gemini, groq, ollama"},
            status_code=400,
        )
    if not isinstance(model, str) or not model.strip():
        return JSONResponse({"error": "model is required"}, status_code=400)

    if body.get("reset"):
        ok = reset_provider_limits(provider, model)
        if not ok:
            return JSONResponse({"error": "Failed to reset"}, status_code=500)
        return {
            "ok": True,
            "item": _build_provider_limit_row(provider, model),
            "reset": True,
        }

    parsed: dict[str, int | None] = {}
    for field in _QUOTA_LIMIT_FIELDS:
        raw = body.get(field, None)
        if raw is None:
            parsed[field] = None
            continue
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return JSONResponse(
                {"error": f"{field} must be a non-negative integer or null"},
                status_code=400,
            )
        parsed[field] = raw

    monthly_raw = body.get("monthly_usd", None)
    monthly_value: float | None
    if monthly_raw is None:
        monthly_value = None
    elif isinstance(monthly_raw, bool) or not isinstance(monthly_raw, (int, float)):
        return JSONResponse(
            {"error": "monthly_usd must be a non-negative number or null"},
            status_code=400,
        )
    else:
        if monthly_raw < 0:
            return JSONResponse(
                {"error": "monthly_usd must be a non-negative number or null"},
                status_code=400,
            )
        monthly_value = float(monthly_raw)

    ok = set_provider_limits(
        provider, model,
        parsed["rpm"], parsed["tpm"], parsed["rpd"], parsed["tpd"],
        monthly_usd=monthly_value,
    )
    if not ok:
        return JSONResponse({"error": "Failed to update"}, status_code=500)
    return {"ok": True, "item": _build_provider_limit_row(provider, model)}


@app.get("/api/admin/ai-budget-global")
async def admin_ai_budget_global_get(_admin: dict = Depends(require_admin)):
    """Return the global USD/month budget + live month/day spend."""
    return {"global": _build_global_budget_row()}


@app.post("/api/admin/ai-budget-global")
async def admin_ai_budget_global_set(
    request: Request,
    _admin: dict = Depends(require_admin),
):
    """Update or reset the global USD/month budget.

    Body shape::

        {"monthly_usd": 50.0}     # set
        {"monthly_usd": null}     # clear
        {"reset": true}           # alias for clear
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    if body.get("reset"):
        ok = set_global_monthly_budget(None)
        if not ok:
            return JSONResponse({"error": "Failed to reset"}, status_code=500)
        return {"ok": True, "reset": True, "global": _build_global_budget_row()}

    raw = body.get("monthly_usd", None)
    if raw is None:
        ok = set_global_monthly_budget(None)
    elif isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return JSONResponse(
            {"error": "monthly_usd must be a non-negative number or null"},
            status_code=400,
        )
    elif raw < 0:
        return JSONResponse(
            {"error": "monthly_usd must be a non-negative number or null"},
            status_code=400,
        )
    else:
        ok = set_global_monthly_budget(float(raw))

    if not ok:
        return JSONResponse({"error": "Failed to update"}, status_code=500)
    return {"ok": True, "global": _build_global_budget_row()}


# ── X / Twitter admin ────────────────────────────────────────────────────────


_X_CAMPAIGN_KEYS = sorted(x_store.VALID_CAMPAIGN_KEYS)
_X_TIERS = sorted(x_store.VALID_TIERS)


@app.get("/api/admin/x-status")
async def admin_x_status(_admin: dict = Depends(require_admin)):
    """Return current OAuth/account + tier/usage status for the admin panel."""
    oauth = x_store.get_oauth_state()
    tier = x_store.get_tier_config()
    posts_today = x_store.count_posts_today()
    posts_month = x_store.count_posts_this_month()

    handle = oauth.get("handle") or os.environ.get("TWITTER_ACCOUNT_HANDLE") or None

    return {
        "configured": x_client.is_configured(),
        "handle": handle,
        "token_updated_at": oauth.get("updated_at"),
        "token_expires_at": oauth.get("expires_at"),
        "tier": tier,
        "valid_tiers": _X_TIERS,
        "tier_defaults": x_store.TIER_DEFAULTS,
        "usage": {
            "posts_today": posts_today,
            "posts_this_month": posts_month,
            "daily_cap": tier["daily_cap"],
            "monthly_cap": tier["monthly_cap"],
        },
    }


@app.post("/api/admin/x-refresh-handle")
async def admin_x_refresh_handle(_admin: dict = Depends(require_admin)):
    """Force a ``GET /2/users/me`` so the handle shown in the panel is fresh."""
    if not x_client.is_configured():
        return JSONResponse({"error": "not_configured"}, status_code=400)
    try:
        me = await asyncio.to_thread(x_client.get_me)
    except x_client.XClientError as exc:
        return JSONResponse(
            {"error": "x_api_error", "message": str(exc), "status_code": exc.status_code},
            status_code=502,
        )
    except Exception as exc:
        return JSONResponse({"error": "unexpected", "message": str(exc)}, status_code=500)
    return {"ok": True, "me": me}


@app.get("/api/admin/x-campaigns")
async def admin_x_campaigns(_admin: dict = Depends(require_admin)):
    """List campaigns (enabled state, schedule, template, last run)."""
    items = x_store.list_campaigns()
    for item in items:
        item["defaults"] = x_store.CAMPAIGN_DEFAULTS.get(item["campaign_key"], {})
    return {
        "items": items,
        "valid_keys": _X_CAMPAIGN_KEYS,
        "can_post": x_store.get_tier_config()["posting_allowed"],
    }


def _validate_schedule_payload(campaign_key: str, schedule: dict) -> tuple[bool, str]:
    """Validate the schedule dict for a given campaign. Returns (ok, reason)."""
    if not isinstance(schedule, dict):
        return False, "schedule must be an object"

    if campaign_key == "breaking":
        msc = schedule.get("min_source_count", 3)
        if not isinstance(msc, int) or isinstance(msc, bool) or not 1 <= msc <= 20:
            return False, "min_source_count must be int in [1,20]"
        cats = schedule.get("categories", [])
        if not isinstance(cats, list) or not all(isinstance(c, str) for c in cats):
            return False, "categories must be a list of strings"
        cd = schedule.get("cooldown_minutes", 60)
        if not isinstance(cd, int) or isinstance(cd, bool) or not 0 <= cd <= 24 * 60:
            return False, "cooldown_minutes must be int in [0,1440]"
        return True, "ok"

    hour = schedule.get("hour", 9)
    minute = schedule.get("minute", 0)
    if not isinstance(hour, int) or isinstance(hour, bool) or not 0 <= hour <= 23:
        return False, "hour must be int in [0,23]"
    if not isinstance(minute, int) or isinstance(minute, bool) or not 0 <= minute <= 59:
        return False, "minute must be int in [0,59]"
    if campaign_key == "weekly":
        dow = str(schedule.get("day_of_week", "mon")).lower()
        if dow not in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}:
            return False, "day_of_week must be one of mon..sun"
    return True, "ok"


def _validate_template_payload(campaign_key: str, template: dict) -> tuple[bool, str]:
    if not isinstance(template, dict):
        return False, "template must be an object"
    text = template.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return False, "template.text must be a non-empty string"
    if len(text) > 600:
        return False, "template.text too long (max 600 chars)"
    hashtags = template.get("hashtags", "")
    if not isinstance(hashtags, str) or len(hashtags) > 200:
        return False, "template.hashtags must be string ≤ 200 chars"
    if "attach_image" in template and not isinstance(template["attach_image"], bool):
        return False, "template.attach_image must be boolean"
    if "thread" in template and not isinstance(template["thread"], bool):
        return False, "template.thread must be boolean"
    if "thread_max_posts" in template:
        tmp = template["thread_max_posts"]
        if not isinstance(tmp, int) or isinstance(tmp, bool) or not 1 <= tmp <= 10:
            return False, "template.thread_max_posts must be int in [1,10]"
    return True, "ok"


@app.post("/api/admin/x-campaigns")
async def admin_x_campaigns_set(
    request: Request,
    _admin: dict = Depends(require_admin),
):
    """Update a single campaign's config and reschedule its job."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    campaign_key = body.get("campaign_key", "")
    if campaign_key not in x_store.VALID_CAMPAIGN_KEYS:
        return JSONResponse(
            {"error": f"Invalid campaign_key. Valid: {_X_CAMPAIGN_KEYS}"},
            status_code=400,
        )

    enabled = body.get("enabled")
    schedule = body.get("schedule")
    template = body.get("template")

    if enabled is not None and not isinstance(enabled, bool):
        return JSONResponse({"error": "enabled must be boolean"}, status_code=400)

    if schedule is not None:
        ok, reason = _validate_schedule_payload(campaign_key, schedule)
        if not ok:
            return JSONResponse({"error": reason}, status_code=400)

    if template is not None:
        ok, reason = _validate_template_payload(campaign_key, template)
        if not ok:
            return JSONResponse({"error": reason}, status_code=400)

    if enabled and not x_store.get_tier_config()["posting_allowed"]:
        return JSONResponse(
            {"error": "Tier actual no permite postear (Apagado). Cambiá el tier primero."},
            status_code=400,
        )

    ok = x_store.set_campaign_config(
        campaign_key,
        enabled=enabled,
        schedule=schedule,
        template=template,
    )
    if not ok:
        return JSONResponse({"error": "Failed to update"}, status_code=500)

    try:
        reschedule_x_campaigns()
    except Exception as exc:
        logger.warning("reschedule_x_campaigns after update failed: %s", exc)

    return {"ok": True, "item": x_store.get_campaign_config(campaign_key)}


@app.get("/api/admin/x-tier")
async def admin_x_tier_get(_admin: dict = Depends(require_admin)):
    return {
        "tier": x_store.get_tier_config(),
        "defaults": x_store.TIER_DEFAULTS,
        "valid_tiers": _X_TIERS,
    }


@app.post("/api/admin/x-tier")
async def admin_x_tier_set(
    request: Request,
    _admin: dict = Depends(require_admin),
):
    """Update tier + caps. Moving to ``disabled`` apaga todas las campañas."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    tier = body.get("tier", "")
    if tier not in x_store.VALID_TIERS:
        return JSONResponse(
            {"error": f"Invalid tier. Valid: {_X_TIERS}"},
            status_code=400,
        )

    def _int_or_none(v):
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return "invalid"
        if v < 0:
            return "invalid"
        return int(v)

    daily_raw = _int_or_none(body.get("daily_cap"))
    monthly_raw = _int_or_none(body.get("monthly_cap"))
    if daily_raw == "invalid" or monthly_raw == "invalid":
        return JSONResponse(
            {"error": "daily_cap / monthly_cap must be non-negative integers"},
            status_code=400,
        )

    usd = body.get("monthly_usd")
    if usd is not None:
        if isinstance(usd, bool) or not isinstance(usd, (int, float)) or usd < 0:
            return JSONResponse({"error": "monthly_usd must be non-negative number"}, status_code=400)
        usd = float(usd)

    ok = x_store.set_tier_config(
        tier,
        daily_cap=daily_raw,
        monthly_cap=monthly_raw,
        monthly_usd=usd,
    )
    if not ok:
        return JSONResponse({"error": "Failed to update"}, status_code=500)

    try:
        reschedule_x_campaigns()
    except Exception as exc:
        logger.warning("reschedule_x_campaigns after tier change failed: %s", exc)

    return {"ok": True, "tier": x_store.get_tier_config()}


@app.get("/api/admin/x-usage")
async def admin_x_usage(
    desde: str | None = None,
    hasta: str | None = None,
    campaign_key: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 25,
    _admin: dict = Depends(require_admin),
):
    """Paginated X usage log (posts + errors) with cap counters."""
    page, page_size = _clamp_page(page, page_size)
    offset = (page - 1) * page_size

    ck = campaign_key if campaign_key in x_store.VALID_CAMPAIGN_KEYS else None
    st = status if status in x_store.VALID_CAMPAIGN_STATUSES else None

    items = x_store.query_x_usage(
        desde=desde, hasta=hasta, campaign_key=ck, status=st,
        limit=page_size, offset=offset,
    )
    total = x_store.count_x_usage(
        desde=desde, hasta=hasta, campaign_key=ck, status=st,
    )
    tier = x_store.get_tier_config()

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "counters": {
            "posts_today": x_store.count_posts_today(),
            "posts_this_month": x_store.count_posts_this_month(),
            "daily_cap": tier["daily_cap"],
            "monthly_cap": tier["monthly_cap"],
        },
        "filters": {
            "campaigns": _X_CAMPAIGN_KEYS,
            "statuses": sorted(x_store.VALID_CAMPAIGN_STATUSES),
        },
    }


@app.post("/api/admin/x-test-post")
async def admin_x_test_post(
    request: Request,
    _admin: dict = Depends(require_admin),
):
    """Run a campaign right now bypassing the ``enabled`` flag (cap still applies)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    campaign_key = body.get("campaign_key", "")
    if campaign_key not in x_store.VALID_CAMPAIGN_KEYS:
        return JSONResponse(
            {"error": f"Invalid campaign_key. Valid: {_X_CAMPAIGN_KEYS}"},
            status_code=400,
        )
    if not x_client.is_configured():
        return JSONResponse(
            {"error": "X client not configured (missing TWITTER_* env vars)"},
            status_code=400,
        )

    try:
        if campaign_key == "cloud":
            async with _lock:
                words = list(_wordcloud_cache)
            result = await asyncio.to_thread(
                x_campaigns.run_cloud_campaign, words, test=True,
            )
        elif campaign_key == "topstory":
            today = datetime.now(ART).strftime("%Y-%m-%d")
            _articles_db, groups = load_groups_from_db(desde=today, hasta=today)
            if not groups:
                async with _lock:
                    groups = list(_groups)
            ts = await ai_top_story(groups, today)
            story = ts.get("story") if isinstance(ts, dict) else None
            result = await asyncio.to_thread(
                x_campaigns.run_topstory_campaign, story, test=True,
            )
        elif campaign_key == "weekly":
            week_start, week_end = _current_week_bounds()
            _articles_db, groups = load_groups_from_db(desde=week_start, hasta=week_end)
            if len(groups) > 200:
                groups = groups[:200]
            weekly = await ai_weekly_summary(groups, week_start, week_end)
            result = await asyncio.to_thread(
                x_campaigns.run_weekly_campaign, weekly,
                week_start=week_start, week_end=week_end, test=True,
            )
        elif campaign_key == "topics":
            async with _lock:
                grps = list(_groups)
            topics = await ai_topics(grps) if grps else {"topics": []}
            result = await asyncio.to_thread(
                x_campaigns.run_topics_campaign, topics, test=True,
            )
        elif campaign_key == "breaking":
            cfg = x_store.get_campaign_config("breaking") or {}
            sched = cfg.get("schedule") or {}
            async with _lock:
                grps = list(_groups)
            candidate = x_campaigns.pick_breaking_candidate(
                grps,
                min_source_count=int(sched.get("min_source_count", 3) or 3),
                allowed_categories=sched.get("categories") or [],
            )
            if not candidate:
                return {"ok": False, "status": "skipped", "reason": "no_candidate"}
            result = await asyncio.to_thread(
                x_campaigns.run_breaking_campaign, candidate, test=True,
            )
        else:
            return JSONResponse({"error": "Unsupported campaign"}, status_code=400)
    except Exception as exc:
        logger.exception("admin_x_test_post failed")
        return JSONResponse({"error": "unexpected", "message": str(exc)}, status_code=500)

    return {
        "ok": result.ok,
        "status": result.status,
        "reason": result.reason,
        "post_ids": result.post_ids or [],
        "message": result.message,
    }


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
