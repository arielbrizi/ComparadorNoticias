from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.models import Article, ArticleGroup


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """HTTP client wired to the FastAPI app with test data, no scheduler."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("app.db._SQLITE_PATH", db_path)
    monkeypatch.setattr("app.db._use_pg", False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    from app.metrics_store import init_db
    from app.news_store import init_news_tables

    init_db()
    init_news_tables()

    from app import main

    @asynccontextmanager
    async def _test_lifespan(_app):
        yield

    monkeypatch.setattr(main.app.router, "lifespan_context", _test_lifespan)

    now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
    test_articles = [
        Article(
            id="art1", source="Clarín", source_color="#1a73e8",
            title="Inflación subió 3,5%",
            summary="El INDEC informó que la inflación de marzo fue del 3,5 por ciento mensual.",
            link="https://x.com/1", category="portada", published=now,
        ),
        Article(
            id="art2", source="La Nación", source_color="#2d6a4f",
            title="Inflación de marzo: 3,5%",
            summary="La inflación interanual se ubicó en el 42 por ciento acumulado.",
            link="https://x.com/2", category="portada", published=now,
        ),
    ]
    test_groups = [
        ArticleGroup(
            group_id="grp001",
            representative_title="Inflación subió 3,5%",
            category="portada",
            published=now,
            articles=test_articles,
        ),
    ]

    monkeypatch.setattr(main, "_articles", test_articles)
    monkeypatch.setattr(main, "_groups", test_groups)
    monkeypatch.setattr(main, "_statuses", [])
    monkeypatch.setattr(main, "_last_update", now)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestHealth:
    async def test_returns_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestApiFuentes:
    async def test_returns_sources(self, client):
        resp = await client.get("/api/fuentes")
        assert resp.status_code == 200
        data = resp.json()
        assert "Clarín" in data
        assert "color" in data["Clarín"]
        assert "categories" in data["Clarín"]


class TestApiCategorias:
    async def test_returns_categories(self, client):
        resp = await client.get("/api/categorias")
        assert resp.status_code == 200
        data = resp.json()
        assert "portada" in data


class TestApiNoticias:
    async def test_returns_all_articles(self, client):
        resp = await client.get("/api/noticias")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["articles"]) == 2

    async def test_filter_by_category(self, client):
        resp = await client.get("/api/noticias?categoria=portada")
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

    async def test_filter_by_nonexistent_category(self, client):
        resp = await client.get("/api/noticias?categoria=xyz")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    async def test_filter_by_source(self, client):
        resp = await client.get("/api/noticias", params={"fuente": "Clarín"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    async def test_pagination(self, client):
        resp = await client.get("/api/noticias?limit=1&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["articles"]) == 1


class TestApiGrupos:
    async def test_returns_groups(self, client):
        resp = await client.get("/api/grupos")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1

    async def test_solo_multifuente(self, client):
        resp = await client.get("/api/grupos?solo_multifuente=true")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    async def test_get_group_by_id(self, client):
        resp = await client.get("/api/grupo/grp001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["group_id"] == "grp001"

    async def test_group_not_found(self, client):
        resp = await client.get("/api/grupo/nonexistent")
        assert resp.status_code == 200
        assert "error" in resp.json()


class TestApiComparar:
    async def test_compare_group(self, client):
        resp = await client.get("/api/comparar/grp001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["group_id"] == "grp001"
        assert data["source_count"] == 2
        assert "sources" in data
        assert "headline_analysis" in data

    async def test_compare_not_found(self, client):
        resp = await client.get("/api/comparar/nonexistent")
        assert resp.status_code == 200
        assert "error" in resp.json()


class TestApiStatus:
    async def test_returns_status(self, client):
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_articles"] == 2
        assert data["total_groups"] == 1
        assert data["last_update"] is not None


class TestApiMetricas:
    async def test_returns_metrics(self, client):
        resp = await client.get("/api/metricas")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_groups" in data
        assert "first_publisher_ranking" in data
