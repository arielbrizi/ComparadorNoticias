"""Integration tests for /api/feature-flags and /api/admin/feature-flags."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.ai_store import init_ai_tables
from app.config import JWT_ALGORITHM, JWT_SECRET
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
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)

    from app.metrics_store import init_db
    from app.news_store import init_news_tables

    init_db()
    init_news_tables()
    init_users_table()
    init_tracking_table()
    init_ai_tables()

    monkeypatch.setattr("app.ai_store._runtime_cache", {})
    monkeypatch.setattr("app.ai_store._runtime_cache_ts", 0)

    from app import main

    @asynccontextmanager
    async def _test_lifespan(_app):
        yield

    monkeypatch.setattr(main.app.router, "lifespan_context", _test_lifespan)

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _make_admin_token(user_id="admin1", email="admin@test.com"):
    exp = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = {"sub": user_id, "email": email, "role": "admin", "exp": exp}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _make_user_token(user_id="u1", email="user@test.com"):
    exp = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = {"sub": user_id, "email": email, "role": "user", "exp": exp}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


class TestPublicFeatureFlags:
    async def test_returns_default_values(self, client):
        resp = await client.get("/api/feature-flags")
        assert resp.status_code == 200
        data = resp.json()
        assert "flags" in data
        assert data["flags"]["hero_search"] is True

    async def test_no_auth_required(self, client):
        # Sin cookie alguna debe responder 200 igual.
        resp = await client.get("/api/feature-flags")
        assert resp.status_code == 200


class TestServerSideFlagInjection:
    """Verifica que GET / inyecta las clases de feature flag en <html>
    para evitar el flash de hero search al cargar la página."""

    async def test_flag_enabled_no_class(self, client, monkeypatch):
        # Default: hero_search habilitado → no debe aparecer la clase off.
        monkeypatch.setattr("app.ai_store._runtime_cache_ts", 0)
        resp = await client.get("/")
        assert resp.status_code == 200
        html = resp.text
        assert "ff-hero-search-off" not in html
        assert "__INITIAL_FLAG_CLASSES__" not in html  # placeholder reemplazado

    async def test_flag_disabled_injects_class(self, client, monkeypatch):
        from app.feature_flags import set_flag
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        # Forzar el flag a OFF y bustear el cache de runtime.
        assert set_flag("hero_search", False) is True
        monkeypatch.setattr("app.ai_store._runtime_cache_ts", 0)

        # También bustear el HTML cache que cachea el template raw.
        from app import main
        main._HTML_CACHE.clear()

        resp = await client.get("/")
        assert resp.status_code == 200
        html = resp.text
        # El <html> raíz debe tener la clase aplicada antes de cualquier paint.
        assert 'class="ff-hero-search-off"' in html
        assert "__INITIAL_FLAG_CLASSES__" not in html


class TestAdminFeatureFlagsGet:
    async def test_requires_admin(self, client):
        resp = await client.get("/api/admin/feature-flags")
        assert resp.status_code == 403

    async def test_forbidden_for_regular_user(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        user = upsert_user("user@test.com", "User", "")
        token = _make_user_token(user_id=user["id"], email=user["email"])
        resp = await client.get(
            "/api/admin/feature-flags", cookies={"vs_token": token},
        )
        assert resp.status_code == 403

    async def test_returns_registry_for_admin(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])
        resp = await client.get(
            "/api/admin/feature-flags", cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "flags" in data
        names = {f["name"] for f in data["flags"]}
        assert "hero_search" in names
        hero = next(f for f in data["flags"] if f["name"] == "hero_search")
        assert hero["enabled"] is True
        assert hero["label"]
        assert hero["description"]


class TestAdminFeatureFlagsSet:
    async def test_requires_admin(self, client):
        resp = await client.post(
            "/api/admin/feature-flags",
            json={"name": "hero_search", "enabled": False},
        )
        assert resp.status_code == 403

    async def test_forbidden_for_regular_user(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        user = upsert_user("user@test.com", "User", "")
        token = _make_user_token(user_id=user["id"], email=user["email"])
        resp = await client.post(
            "/api/admin/feature-flags",
            json={"name": "hero_search", "enabled": False},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 403

    async def test_admin_can_disable_and_public_reflects(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/feature-flags",
            json={"name": "hero_search", "enabled": False},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["enabled"] is False

        # Forzar refresh del cache (TTL 30s) leyendo directamente la DB.
        monkeypatch.setattr("app.ai_store._runtime_cache_ts", 0)

        public = await client.get("/api/feature-flags")
        assert public.status_code == 200
        assert public.json()["flags"]["hero_search"] is False

    async def test_admin_can_re_enable(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        await client.post(
            "/api/admin/feature-flags",
            json={"name": "hero_search", "enabled": False},
            cookies={"vs_token": token},
        )
        monkeypatch.setattr("app.ai_store._runtime_cache_ts", 0)
        resp = await client.post(
            "/api/admin/feature-flags",
            json={"name": "hero_search", "enabled": True},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        monkeypatch.setattr("app.ai_store._runtime_cache_ts", 0)

        public = await client.get("/api/feature-flags")
        assert public.json()["flags"]["hero_search"] is True

    async def test_rejects_invalid_json(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])
        resp = await client.post(
            "/api/admin/feature-flags",
            content="not-json",
            headers={"Content-Type": "application/json"},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400

    async def test_rejects_unknown_flag(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])
        resp = await client.post(
            "/api/admin/feature-flags",
            json={"name": "does_not_exist", "enabled": False},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400
        assert "Unknown" in resp.json()["error"]

    async def test_rejects_non_bool_enabled(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])
        resp = await client.post(
            "/api/admin/feature-flags",
            json={"name": "hero_search", "enabled": "true"},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400

    async def test_rejects_missing_name(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])
        resp = await client.post(
            "/api/admin/feature-flags",
            json={"enabled": False},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400
