"""Integration tests for /api/admin/ai-cost and /api/admin/ai-config endpoints."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.ai_store import init_ai_tables, log_ai_usage
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


def _make_admin_token(user_id="admin1", email="admin@test.com"):
    exp = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = {"sub": user_id, "email": email, "role": "admin", "exp": exp}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _make_user_token(user_id="u1", email="user@test.com"):
    exp = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = {"sub": user_id, "email": email, "role": "user", "exp": exp}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


class TestAICostEndpoint:
    async def test_requires_admin(self, client):
        resp = await client.get("/api/admin/ai-cost")
        assert resp.status_code == 403

    async def test_forbidden_for_regular_user(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        user = upsert_user("user@test.com", "User", "")
        token = _make_user_token(user_id=user["id"], email=user["email"])
        resp = await client.get("/api/admin/ai-cost", cookies={"vs_token": token})
        assert resp.status_code == 403

    async def test_returns_empty_summary(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.get("/api/admin/ai-cost", cookies={"vs_token": token})
        assert resp.status_code == 200
        data = resp.json()

        assert data["summary"]["totals"]["calls"] == 0
        assert data["summary"]["totals"]["cost_total"] == 0
        assert data["daily"] == []

    async def test_returns_data_after_logging(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")

        log_ai_usage(
            event_type="topics",
            provider="gemini",
            model="gemini-3-flash-preview",
            input_tokens=5000,
            output_tokens=100,
            latency_ms=1200,
        )
        log_ai_usage(
            event_type="search",
            provider="groq",
            model="llama-3.3-70b-versatile",
            input_tokens=3000,
            output_tokens=80,
            latency_ms=400,
        )

        token = _make_admin_token(user_id=admin["id"], email=admin["email"])
        resp = await client.get("/api/admin/ai-cost", cookies={"vs_token": token})
        assert resp.status_code == 200
        data = resp.json()

        assert data["summary"]["totals"]["calls"] == 2
        assert data["summary"]["totals"]["input_tokens"] == 8000
        assert data["summary"]["totals"]["cost_total"] > 0
        assert len(data["summary"]["by_event"]) == 2
        assert len(data["daily"]) == 1


class TestAIConfigEndpoint:
    async def test_get_requires_admin(self, client):
        resp = await client.get("/api/admin/ai-config")
        assert resp.status_code == 403

    async def test_post_requires_admin(self, client):
        resp = await client.post("/api/admin/ai-config", json={
            "event_type": "topics", "provider": "groq",
        })
        assert resp.status_code == 403

    async def test_get_returns_config(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.get("/api/admin/ai-config", cookies={"vs_token": token})
        assert resp.status_code == 200
        data = resp.json()

        assert "config" in data
        assert "valid_providers" in data
        assert "valid_event_types" in data
        assert data["config"]["topics"] == "gemini_fallback_groq"

    async def test_set_updates_config(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-config",
            json={"event_type": "topics", "provider": "groq"},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        resp = await client.get("/api/admin/ai-config", cookies={"vs_token": token})
        assert resp.json()["config"]["topics"] == "groq"

    async def test_set_invalid_event_type(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-config",
            json={"event_type": "invalid", "provider": "groq"},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400

    async def test_set_invalid_provider(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-config",
            json={"event_type": "topics", "provider": "openai"},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400

    async def test_set_groq_fallback_gemini(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-config",
            json={"event_type": "topics", "provider": "groq_fallback_gemini"},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        resp = await client.get("/api/admin/ai-config", cookies={"vs_token": token})
        assert resp.json()["config"]["topics"] == "groq_fallback_gemini"

    async def test_config_returns_schedule(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.get("/api/admin/ai-config", cookies={"vs_token": token})
        assert resp.status_code == 200
        assert "schedule" in resp.json()


class TestAIScheduleEndpoint:
    async def test_requires_admin(self, client):
        resp = await client.post("/api/admin/ai-schedule", json={
            "event_type": "search_prefetch", "quiet_start": "00:00", "quiet_end": "06:00",
        })
        assert resp.status_code == 403

    async def test_set_schedule(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-schedule",
            json={"event_type": "search_prefetch", "quiet_start": "00:00", "quiet_end": "06:00"},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        resp = await client.get("/api/admin/ai-config", cookies={"vs_token": token})
        schedule = resp.json()["schedule"]
        assert schedule["search_prefetch"]["quiet_start"] == "00:00"
        assert schedule["search_prefetch"]["quiet_end"] == "06:00"

    async def test_clear_schedule(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        await client.post(
            "/api/admin/ai-schedule",
            json={"event_type": "search_prefetch", "quiet_start": "00:00", "quiet_end": "06:00"},
            cookies={"vs_token": token},
        )
        resp = await client.post(
            "/api/admin/ai-schedule",
            json={"event_type": "search_prefetch", "quiet_start": "", "quiet_end": ""},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200

        resp = await client.get("/api/admin/ai-config", cookies={"vs_token": token})
        assert "search_prefetch" not in resp.json()["schedule"]

    async def test_invalid_event_type(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-schedule",
            json={"event_type": "invalid", "quiet_start": "00:00", "quiet_end": "06:00"},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400

    async def test_invalid_time_format(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-schedule",
            json={"event_type": "search_prefetch", "quiet_start": "25:00", "quiet_end": "06:00"},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400
