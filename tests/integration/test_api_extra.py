"""Integration tests for API endpoints not covered in test_api.py:
/api/search, /api/topics, /api/wordcloud, /api/refresh,
plus date filters on /api/grupos, /api/status, combined filters on /api/noticias.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.models import Article, ArticleGroup


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("app.db._SQLITE_PATH", db_path)
    monkeypatch.setattr("app.db._use_pg", False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    from app.metrics_store import init_db
    from app.news_store import init_news_tables
    from app.tracking_store import init_tracking_table
    from app.user_store import init_users_table

    init_db()
    init_news_tables()
    init_users_table()
    init_tracking_table()

    from app import main

    @asynccontextmanager
    async def _test_lifespan(_app):
        yield

    monkeypatch.setattr(main.app.router, "lifespan_context", _test_lifespan)

    now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
    yesterday = now - timedelta(days=1)
    test_articles = [
        Article(
            id="art1", source="Clarín", source_color="#1a73e8",
            title="Inflación subió 3,5%",
            summary="El INDEC informó que la inflación de marzo fue del 3,5%.",
            link="https://x.com/1", category="portada", published=now,
        ),
        Article(
            id="art2", source="La Nación", source_color="#2d6a4f",
            title="Inflación de marzo: 3,5%",
            summary="La inflación interanual se ubicó en el 42%.",
            link="https://x.com/2", category="portada", published=now,
        ),
        Article(
            id="art3", source="Infobae", source_color="#e63946",
            title="River ganó el clásico 3-0",
            summary="River venció a Boca en el Monumental.",
            link="https://x.com/3", category="deportes", published=yesterday,
        ),
    ]
    test_groups = [
        ArticleGroup(
            group_id="grp001",
            representative_title="Inflación subió 3,5%",
            category="portada",
            published=now,
            articles=test_articles[:2],
        ),
        ArticleGroup(
            group_id="grp002",
            representative_title="River ganó el clásico 3-0",
            category="deportes",
            published=yesterday,
            articles=[test_articles[2]],
        ),
    ]

    monkeypatch.setattr(main, "_articles", test_articles)
    monkeypatch.setattr(main, "_groups", test_groups)
    monkeypatch.setattr(main, "_statuses", [])
    monkeypatch.setattr(main, "_last_update", now)
    monkeypatch.setattr(main, "_wordcloud_cache", [["inflación", 5], ["dólar", 3]])
    monkeypatch.setattr(main, "_wordcloud_updated", now)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestApiSearchNoAI:
    async def test_search_without_api_keys(self, client):
        resp = await client.get("/api/search?q=inflación")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ai_available"] is False

    async def test_search_query_too_short(self, client):
        resp = await client.get("/api/search?q=a")
        assert resp.status_code == 422


class TestApiSearchFallbackSummary:
    """When the AI says has_results=false but DB text search rescues matches,
    the endpoint must overwrite the AI's negative summary with a templated
    one based on the matched article titles."""

    async def test_overrides_summary_when_ai_says_no_but_db_finds_matches(
        self, client, monkeypatch,
    ):
        from app import main
        from app.news_store import save_articles_and_groups

        save_articles_and_groups(main._articles, main._groups)

        async def _ai_says_nothing(query, groups, **kwargs):
            return {
                "ai_available": True,
                "ai_provider": "Groq",
                "summary": "No se encontraron resultados relevantes.",
                "relevant_group_ids": [],
                "has_results": False,
            }

        monkeypatch.setattr(main, "ai_news_search", _ai_says_nothing)

        resp = await client.get("/api/search?q=dame los últimos detalles de la inflación")
        assert resp.status_code == 200
        data = resp.json()

        assert data["has_results"] is True
        assert data.get("summary_fallback") is True
        assert "No se encontraron" not in data["summary"]
        assert "inflación" in data["summary"].lower()
        assert any("inflación" in g["representative_title"].lower()
                   for g in data["matched_groups"])

    async def test_preserves_ai_summary_when_ai_finds_results(
        self, client, monkeypatch,
    ):
        from app import main
        from app.news_store import save_articles_and_groups

        save_articles_and_groups(main._articles, main._groups)

        async def _ai_says_yes(query, groups, **kwargs):
            return {
                "ai_available": True,
                "ai_provider": "Gemini",
                "summary": "El INDEC publicó la inflación mensual con suba del 3,5%.",
                "relevant_group_ids": ["grp001"],
                "has_results": True,
            }

        monkeypatch.setattr(main, "ai_news_search", _ai_says_yes)

        resp = await client.get("/api/search?q=inflación")
        data = resp.json()
        assert data["has_results"] is True
        assert "INDEC" in data["summary"]
        assert data.get("summary_fallback") is not True


class TestApiTopicsNoAI:
    async def test_topics_without_api_keys(self, client):
        resp = await client.get("/api/topics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ai_available"] is False
        assert data["topics"] == []


class TestApiWordcloud:
    async def test_returns_words(self, client):
        resp = await client.get("/api/wordcloud")
        assert resp.status_code == 200
        data = resp.json()
        assert "words" in data
        assert "updated_at" in data
        assert len(data["words"]) == 2
        assert data["words"][0][0] == "inflación"

    async def test_updated_at_not_null(self, client):
        resp = await client.get("/api/wordcloud")
        data = resp.json()
        assert data["updated_at"] is not None


class TestApiRefresh:
    async def test_refresh_triggers_update(self, client, monkeypatch):
        from app import main

        mock_refresh = AsyncMock()
        monkeypatch.setattr(main, "refresh_news", mock_refresh)

        resp = await client.post("/api/refresh")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        mock_refresh.assert_called_once()


class TestApiNoticiasFilterCombination:
    async def test_filter_by_source_and_category(self, client):
        resp = await client.get("/api/noticias?fuente=Infobae&categoria=deportes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["articles"][0]["source"] == "Infobae"

    async def test_filter_by_source_and_wrong_category(self, client):
        resp = await client.get("/api/noticias?fuente=Infobae&categoria=portada")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestApiGruposDateFilters:
    async def test_desde_filter(self, client):
        resp = await client.get("/api/grupos?desde=2025-06-15")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1

    async def test_hasta_filter_excludes_future(self, client):
        resp = await client.get("/api/grupos?hasta=2025-06-13")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    async def test_date_range_filter(self, client):
        resp = await client.get("/api/grupos?desde=2025-06-14&hasta=2025-06-15")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1

    async def test_category_filter(self, client):
        resp = await client.get("/api/grupos?categoria=deportes")
        assert resp.status_code == 200

    async def test_pagination(self, client):
        resp = await client.get("/api/grupos?limit=1&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["groups"]) <= 1


class TestApiStatusDateFilters:
    async def test_desde_filter(self, client):
        resp = await client.get("/api/status?desde=2025-06-15")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_articles"] >= 1

    async def test_hasta_excludes_future(self, client):
        resp = await client.get("/api/status?hasta=2025-06-13")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_articles"] == 0

    async def test_range_filter(self, client):
        resp = await client.get("/api/status?desde=2025-06-14&hasta=2025-06-16")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_articles"] >= 1
        assert "last_update" in data
