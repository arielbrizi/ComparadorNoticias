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
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.article_grouper import group_articles, is_event_expired
from app.comparator import compare_group_articles
from app.config import CATEGORIES, SOURCES
from app.feed_reader import fetch_all_feeds
from app.ai_search import ai_news_search, ai_topics, ai_top_story, ai_weekly_summary
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


async def refresh_wordcloud():
    global _wordcloud_cache, _wordcloud_updated
    async with _lock:
        arts = list(_articles)
    words = build_wordcloud(arts)
    async with _lock:
        _wordcloud_cache = words
        _wordcloud_updated = datetime.now(timezone.utc)
    logger.info("Word cloud actualizada: %d términos", len(words))


# ── Lifecycle ────────────────────────────────────────────────────────────────

scheduler = AsyncIOScheduler()


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
    scheduler.add_job(refresh_news, "interval", minutes=10)
    scheduler.add_job(refresh_wordcloud, "interval", hours=2)
    scheduler.add_job(purge_old_news, "cron", hour=7, minute=0)
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

    result = await ai_news_search(q, grps)

    ids = set(result.get("relevant_group_ids", []))
    by_id = {g.group_id: g for g in grps}

    if ids:
        result["matched_groups"] = [
            by_id[gid].model_dump(mode="json") for gid in ids if gid in by_id
        ]

    if not ids or not result.get("matched_groups"):
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


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


# ── Static files ─────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
