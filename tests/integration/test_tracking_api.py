from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.config import JWT_ALGORITHM, JWT_SECRET
from app.models import Article, ArticleGroup
from app.tracking_store import init_tracking_table, query_usage_stats
from app.user_store import init_users_table, upsert_user


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
    test_articles = [
        Article(
            id="art1", source="Clarín", source_color="#1a73e8",
            title="Test article", link="https://x.com/1",
            category="portada", published=now,
        ),
    ]
    test_groups = [
        ArticleGroup(
            group_id="grp001", representative_title="Test",
            category="portada", published=now, articles=test_articles,
        ),
    ]
    monkeypatch.setattr(main, "_articles", test_articles)
    monkeypatch.setattr(main, "_groups", test_groups)
    monkeypatch.setattr(main, "_statuses", [])
    monkeypatch.setattr(main, "_last_update", now)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _make_token(user_id="u1", email="test@test.com", role="user"):
    exp = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = {"sub": user_id, "email": email, "role": role, "exp": exp}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


class TestTrackEndpoint:
    async def test_track_valid_events(self, client):
        resp = await client.post("/api/track", json={
            "session_id": "test-session",
            "events": [
                {"type": "page_view", "data": {"view": "noticias"}, "ts": "2026-03-28T12:00:00"},
                {"type": "group_click", "data": {"group_id": "grp001"}, "ts": "2026-03-28T12:01:00"},
            ],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["logged"] == 2

    async def test_track_anonymous_no_user_id(self, client):
        await client.post("/api/track", json={
            "session_id": "anon-session",
            "events": [{"type": "page_view", "data": {}, "ts": "2026-03-28T12:00:00"}],
        })
        stats = query_usage_stats()
        assert stats["unique_users"] == 0

    async def test_track_authenticated_has_user_id(self, client):
        user = upsert_user("tracker@test.com", "Tracker", "")
        token = _make_token(user_id=user["id"], email=user["email"])
        await client.post(
            "/api/track",
            json={
                "session_id": "auth-session",
                "events": [{"type": "page_view", "data": {}, "ts": "2026-03-28T12:00:00"}],
            },
            cookies={"vs_token": token},
        )
        stats = query_usage_stats()
        assert stats["unique_users"] >= 1

    async def test_track_missing_session_id(self, client):
        resp = await client.post("/api/track", json={
            "events": [{"type": "page_view"}],
        })
        assert resp.status_code == 400

    async def test_track_empty_events(self, client):
        resp = await client.post("/api/track", json={
            "session_id": "s1",
            "events": [],
        })
        assert resp.status_code == 400

    async def test_track_invalid_json(self, client):
        resp = await client.post(
            "/api/track",
            content="not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400
