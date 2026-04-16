from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.config import JWT_ALGORITHM, JWT_SECRET
from app.models import Article, ArticleGroup
from app.tracking_store import init_tracking_table
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


def _make_token(user_id="u1", email="test@test.com", role="user", expired=False):
    exp = datetime.now(timezone.utc) + (timedelta(hours=-1) if expired else timedelta(hours=1))
    payload = {"sub": user_id, "email": email, "role": role, "exp": exp}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


class TestAuthMe:
    async def test_no_cookie_returns_null(self, client):
        resp = await client.get("/auth/me")
        assert resp.status_code == 200
        assert resp.json()["user"] is None

    async def test_valid_jwt_returns_user(self, client):
        user = upsert_user("test@test.com", "Test User", "https://img/test.jpg")
        token = _make_token(user_id=user["id"], email=user["email"])
        resp = await client.get("/auth/me", cookies={"vs_token": token})
        assert resp.status_code == 200
        data = resp.json()["user"]
        assert data["email"] == "test@test.com"
        assert data["name"] == "Test User"

    async def test_expired_jwt_returns_null(self, client):
        user = upsert_user("expired@test.com", "Expired", "")
        token = _make_token(user_id=user["id"], expired=True)
        resp = await client.get("/auth/me", cookies={"vs_token": token})
        assert resp.status_code == 200
        assert resp.json()["user"] is None

    async def test_invalid_jwt_returns_null(self, client):
        resp = await client.get("/auth/me", cookies={"vs_token": "garbage"})
        assert resp.status_code == 200
        assert resp.json()["user"] is None

    async def test_jwt_for_nonexistent_user_returns_null(self, client):
        token = _make_token(user_id="nonexistent")
        resp = await client.get("/auth/me", cookies={"vs_token": token})
        assert resp.status_code == 200
        assert resp.json()["user"] is None


class TestAuthLogout:
    async def test_logout_deletes_cookie(self, client):
        resp = await client.post("/auth/logout")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        cookies = resp.headers.get_list("set-cookie")
        assert any("vs_token" in c for c in cookies)


class TestGoogleOAuth:
    async def test_login_without_config_returns_501(self, client, monkeypatch):
        monkeypatch.setattr("app.auth.GOOGLE_CLIENT_ID", "")
        resp = await client.get("/auth/google/login", follow_redirects=False)
        assert resp.status_code == 501

    async def test_login_with_config_redirects(self, client, monkeypatch):
        monkeypatch.setattr("app.auth.GOOGLE_CLIENT_ID", "test-client-id")
        resp = await client.get("/auth/google/login", follow_redirects=False)
        assert resp.status_code == 200
        assert "accounts.google.com" in resp.text

    async def test_callback_error_redirects(self, client):
        resp = await client.get("/auth/google/callback?error=access_denied", follow_redirects=False)
        assert resp.status_code == 200
        assert "auth_error" in resp.text


class TestMagicLinks:
    async def test_request_with_invalid_email(self, client):
        resp = await client.post("/auth/magic/request", json={"email": "not-an-email"})
        assert resp.status_code == 400

    async def test_request_with_empty_email(self, client):
        resp = await client.post("/auth/magic/request", json={"email": ""})
        assert resp.status_code == 400

    async def test_request_without_resend_logs_url(self, client, monkeypatch):
        monkeypatch.setattr("app.auth.RESEND_API_KEY", "")
        resp = await client.post("/auth/magic/request", json={"email": "user@test.com"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    async def test_verify_with_valid_token(self, client):
        from app.auth import _serializer
        token = _serializer.dumps("magic@test.com", salt="magic-link")
        resp = await client.get(f"/auth/magic/verify?token={token}", follow_redirects=False)
        assert resp.status_code == 200
        assert 'location.replace("/")' in resp.text
        cookies = resp.headers.get_list("set-cookie")
        assert any("vs_token" in c for c in cookies)

    async def test_verify_with_invalid_token(self, client):
        resp = await client.get("/auth/magic/verify?token=garbage", follow_redirects=False)
        assert resp.status_code == 200
        assert "auth_error=invalid" in resp.text

    async def test_verify_with_empty_token(self, client):
        resp = await client.get("/auth/magic/verify?token=", follow_redirects=False)
        assert resp.status_code == 200
        assert "auth_error=no_token" in resp.text


class TestAdminEndpoints:
    async def test_dashboard_requires_auth(self, client):
        resp = await client.get("/api/admin/dashboard")
        assert resp.status_code == 403

    async def test_dashboard_rejects_regular_user(self, client):
        user = upsert_user("regular@test.com", "Regular", "")
        token = _make_token(user_id=user["id"], email=user["email"], role="user")
        resp = await client.get("/api/admin/dashboard", cookies={"vs_token": token})
        assert resp.status_code == 403

    async def test_dashboard_allows_admin(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        user = upsert_user("admin@test.com", "Admin", "")
        assert user["role"] == "admin"
        token = _make_token(user_id=user["id"], email=user["email"], role="admin")
        resp = await client.get("/api/admin/dashboard", cookies={"vs_token": token})
        assert resp.status_code == 200
        data = resp.json()
        assert "total_users" in data
        assert "usage" in data
        assert "features" in data
        assert "engagement" in data
        assert "sections" in data

    async def test_users_endpoint_requires_admin(self, client):
        resp = await client.get("/api/admin/users")
        assert resp.status_code == 403

    async def test_users_endpoint_allows_admin(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        user = upsert_user("admin@test.com", "Admin", "")
        token = _make_token(user_id=user["id"], email=user["email"], role="admin")
        resp = await client.get("/api/admin/users", cookies={"vs_token": token})
        assert resp.status_code == 200
        data = resp.json()
        assert "users" in data
        assert "total" in data

    async def test_popular_searches_requires_admin(self, client):
        resp = await client.get("/api/admin/popular-searches")
        assert resp.status_code == 403

    async def test_daily_activity_requires_admin(self, client):
        resp = await client.get("/api/admin/daily-activity")
        assert resp.status_code == 403

    async def test_top_content_requires_admin(self, client):
        resp = await client.get("/api/admin/top-content")
        assert resp.status_code == 403

    async def test_hourly_requires_admin(self, client):
        resp = await client.get("/api/admin/hourly")
        assert resp.status_code == 403

    async def test_top_content_allows_admin(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        user = upsert_user("admin@test.com", "Admin", "")
        token = _make_token(user_id=user["id"], email=user["email"], role="admin")
        resp = await client.get("/api/admin/top-content", cookies={"vs_token": token})
        assert resp.status_code == 200
        assert "content" in resp.json()

    async def test_hourly_allows_admin(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        user = upsert_user("admin@test.com", "Admin", "")
        token = _make_token(user_id=user["id"], email=user["email"], role="admin")
        resp = await client.get("/api/admin/hourly", cookies={"vs_token": token})
        assert resp.status_code == 200
        assert "hours" in resp.json()

    async def test_admin_page_redirects_non_admin(self, client):
        resp = await client.get("/admin", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/"
