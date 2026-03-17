"""
Comparador de Noticias — API principal.
Agrega noticias de medios argentinos y permite compararlas.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.article_grouper import group_articles
from app.comparator import compare_group_articles
from app.config import CATEGORIES, SOURCES
from app.feed_reader import fetch_all_feeds
from app.models import Article, ArticleGroup, FeedStatus

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
_lock = asyncio.Lock()


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
    ok = sum(1 for s in statuses if s.status == "ok")
    logger.info(
        "Listo: %d artículos, %d grupos, %d/%d feeds OK",
        len(articles),
        len(groups),
        ok,
        len(statuses),
    )


# ── Lifecycle ────────────────────────────────────────────────────────────────

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await refresh_news()
    scheduler.add_job(refresh_news, "interval", minutes=10)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Comparador de Noticias",
    description="Agrega y compara noticias de los principales medios argentinos",
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
    limit: int = Query(60, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    async with _lock:
        grps = list(_groups)

    if categoria:
        grps = [g for g in grps if g.category == categoria]
    if solo_multifuente:
        grps = [g for g in grps if g.source_count >= 2]

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
        name: {"color": cfg["color"], "categories": list(cfg["feeds"].keys())}
        for name, cfg in SOURCES.items()
    }


@app.get("/api/categorias")
async def get_categorias():
    return CATEGORIES


@app.get("/api/status")
async def get_status():
    async with _lock:
        return {
            "last_update": _last_update.isoformat() if _last_update else None,
            "total_articles": len(_articles),
            "total_groups": len(_groups),
            "multi_source_groups": sum(1 for g in _groups if g.source_count >= 2),
            "feeds": _statuses,
        }


@app.post("/api/refresh")
async def manual_refresh():
    await refresh_news()
    return {"status": "ok", "total_articles": len(_articles)}


# ── Static files ─────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")
