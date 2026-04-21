"""Integration tests for /api/admin/ai-monitor."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.ai_store import init_ai_tables, log_ai_usage
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


class TestAIMonitorEndpoint:
    async def test_requires_admin(self, client):
        resp = await client.get("/api/admin/ai-monitor")
        assert resp.status_code == 403

    async def test_forbidden_for_regular_user(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        user = upsert_user("user@test.com", "User", "")
        token = _make_user_token(user_id=user["id"], email=user["email"])
        resp = await client.get(
            "/api/admin/ai-monitor", cookies={"vs_token": token},
        )
        assert resp.status_code == 403

    async def test_returns_empty_when_no_calls(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.get(
            "/api/admin/ai-monitor", cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()

        assert data["recent_calls"] == []
        assert len(data["providers"]) == 3
        names = {p["provider"] for p in data["providers"]}
        assert names == {"gemini", "groq", "ollama"}

        for p in data["providers"]:
            assert p["configured"] is False
            assert p["status"] == "red"
            assert p["recent_calls"] == 0
            assert p["last_success"] is None
            assert p["last_error"] is None

    async def test_returns_providers_and_recent_calls(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        for i in range(6):
            log_ai_usage(
                event_type="topics",
                provider="gemini",
                model="gemini-3-flash-preview",
                input_tokens=1000 + i,
                output_tokens=50,
                latency_ms=500,
            )

        log_ai_usage(
            event_type="search",
            provider="groq",
            model="llama-3.3-70b-versatile",
            input_tokens=2000,
            output_tokens=80,
            latency_ms=300,
        )

        resp = await client.get(
            "/api/admin/ai-monitor", cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()

        assert len(data["recent_calls"]) == 5
        ids = [c["id"] for c in data["recent_calls"]]
        assert ids == sorted(ids, reverse=True)

        latest = data["recent_calls"][0]
        assert latest["provider"] == "groq"
        assert latest["success"] is True
        assert "created_at" in latest

        by_provider = {p["provider"]: p for p in data["providers"]}
        gemini = by_provider["gemini"]
        groq = by_provider["groq"]

        assert gemini["configured"] is True
        assert gemini["recent_calls"] == 6
        assert gemini["recent_success_count"] == 6
        assert gemini["success_rate"] == 1.0
        assert gemini["status"] == "green"
        assert gemini["last_success"] is not None
        assert gemini["last_success"]["event_type"] == "topics"
        assert gemini["last_error"] is None

        assert groq["configured"] is True
        assert groq["status"] == "green"
        assert groq["last_success"]["event_type"] == "search"

    async def test_reflects_errors_as_red(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        log_ai_usage(
            event_type="topics",
            provider="gemini",
            model="gemini-3-flash-preview",
            input_tokens=0,
            output_tokens=0,
            latency_ms=100,
            success=False,
            error_message="429 Quota exceeded: tokens per minute",
        )

        resp = await client.get(
            "/api/admin/ai-monitor", cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()

        gemini = next(p for p in data["providers"] if p["provider"] == "gemini")
        assert gemini["status"] == "red"
        assert gemini["last_error"] is not None
        assert "429" in gemini["last_error"]["error_message"]
        assert gemini["errors_last_window"] >= 1

    async def test_detects_rate_limit(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        from app import ai_search

        monkeypatch.setattr(ai_search, "_rate_limit_until", time.time() + 30)

        resp = await client.get(
            "/api/admin/ai-monitor", cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()

        gemini = next(p for p in data["providers"] if p["provider"] == "gemini")
        assert gemini["rate_limit_active"] is True
        assert gemini["rate_limit_seconds_remaining"] > 0
        assert gemini["status"] == "red"

        groq = next(p for p in data["providers"] if p["provider"] == "groq")
        assert groq["rate_limit_active"] is False
