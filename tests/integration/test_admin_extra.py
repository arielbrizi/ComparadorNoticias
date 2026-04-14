"""Integration tests for admin endpoints and static pages not covered elsewhere:
/api/admin/debug-headers, /api/admin/purge-proxy-events, /admin for actual admins,
/, /privacy, /terms static pages.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.config import JWT_ALGORITHM, JWT_SECRET
from app.models import Article, ArticleGroup
from app.tracking_store import init_tracking_table, log_events
from app.user_store import init_users_table, upsert_user


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("app.db._SQLITE_PATH", db_path)
    monkeypatch.setattr("app.db._use_pg", False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    from app.ai_store import init_ai_tables
    from app.metrics_store import init_db
    from app.news_store import init_news_tables

    init_db()
    init_news_tables()
    init_users_table()
    init_tracking_table()
    init_ai_tables()

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


class TestDebugHeaders:
    async def test_requires_admin(self, client):
        resp = await client.get("/api/admin/debug-headers")
        assert resp.status_code == 403

    async def test_returns_headers_for_admin(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_token(user_id=admin["id"], email=admin["email"], role="admin")
        resp = await client.get(
            "/api/admin/debug-headers",
            cookies={"vs_token": token},
            headers={"x-forwarded-for": "1.2.3.4"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "client_host" in data
        assert "all_headers" in data
        assert data["x-forwarded-for"] == "1.2.3.4"


class TestPurgeProxyEvents:
    async def test_requires_admin(self, client):
        resp = await client.post("/api/admin/purge-proxy-events")
        assert resp.status_code == 403

    async def test_purges_proxy_ips(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")

        log_events(
            [{"type": "page_view", "data": {}, "ts": "2026-03-28T12:00:00"}],
            session_id="proxy-session",
            ip_address="100.64.1.2",
        )
        log_events(
            [{"type": "page_view", "data": {}, "ts": "2026-03-28T12:00:00"}],
            session_id="real-session",
            ip_address="5.6.7.8",
        )

        token = _make_token(user_id=admin["id"], email=admin["email"], role="admin")
        resp = await client.post(
            "/api/admin/purge-proxy-events",
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] >= 1

    async def test_purge_with_no_proxy_events(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_token(user_id=admin["id"], email=admin["email"], role="admin")
        resp = await client.post(
            "/api/admin/purge-proxy-events",
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 0


class TestAdminPageAccess:
    async def test_admin_page_serves_html_for_admin(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_token(user_id=admin["id"], email=admin["email"], role="admin")
        resp = await client.get("/admin", cookies={"vs_token": token})
        assert resp.status_code == 200

    async def test_admin_page_redirects_regular_user(self, client):
        user = upsert_user("user@test.com", "User", "")
        token = _make_token(user_id=user["id"], email=user["email"], role="user")
        resp = await client.get("/admin", cookies={"vs_token": token}, follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/"


class TestStaticPages:
    async def test_index_page(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200

    async def test_privacy_page(self, client):
        resp = await client.get("/privacy")
        assert resp.status_code == 200

    async def test_terms_page(self, client):
        resp = await client.get("/terms")
        assert resp.status_code == 200


class TestAdminPopularSearchesAllowsAdmin:
    async def test_returns_data(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_token(user_id=admin["id"], email=admin["email"], role="admin")
        resp = await client.get(
            "/api/admin/popular-searches",
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        assert "searches" in resp.json()


class TestAdminDailyActivityAllowsAdmin:
    async def test_returns_data(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_token(user_id=admin["id"], email=admin["email"], role="admin")
        resp = await client.get(
            "/api/admin/daily-activity",
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        assert "days" in resp.json()

    async def test_date_filter(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")

        log_events(
            [{"type": "page_view", "data": {}, "ts": "2026-03-28T12:00:00"}],
            session_id="s1",
            user_id=admin["id"],
        )

        token = _make_token(user_id=admin["id"], email=admin["email"], role="admin")
        resp = await client.get(
            "/api/admin/daily-activity?desde=2026-03-28&hasta=2026-03-28",
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["days"]) >= 1


class TestAdminDashboardDateFilter:
    async def test_with_date_range(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_token(user_id=admin["id"], email=admin["email"], role="admin")
        resp = await client.get(
            "/api/admin/dashboard?desde=2026-03-01&hasta=2026-03-31",
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "usage" in data
        assert "features" in data
