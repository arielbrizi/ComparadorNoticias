"""Integration tests for /api/admin/ai-invocations and /api/admin/process-events."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.ai_store import init_ai_tables, log_ai_usage
from app.config import JWT_ALGORITHM, JWT_SECRET
from app.infra_cost_store import init_infra_cost_table, save_snapshot
from app.process_events_store import init_process_events_table, log_process_event
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
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
    monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)

    from app.metrics_store import init_db
    from app.news_store import init_news_tables

    init_db()
    init_news_tables()
    init_users_table()
    init_tracking_table()
    init_ai_tables()
    init_process_events_table()
    init_infra_cost_table()

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


async def _admin_cookies(monkeypatch):
    monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
    admin = upsert_user("admin@test.com", "Admin", "")
    return {"vs_token": _make_admin_token(user_id=admin["id"], email=admin["email"])}


class TestAIInvocationsEndpoint:
    async def test_requires_admin(self, client):
        resp = await client.get("/api/admin/ai-invocations")
        assert resp.status_code == 403

    async def test_returns_empty(self, client, monkeypatch):
        cookies = await _admin_cookies(monkeypatch)
        resp = await client.get("/api/admin/ai-invocations", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["page"] == 1
        assert "filters" in data
        assert "providers" in data["filters"]
        assert "event_types" in data["filters"]

    async def test_pagination(self, client, monkeypatch):
        cookies = await _admin_cookies(monkeypatch)
        for i in range(5):
            log_ai_usage(
                event_type="topics", provider="gemini",
                model="gemini-3-flash-preview",
                input_tokens=100, output_tokens=20, latency_ms=100,
            )
        resp = await client.get(
            "/api/admin/ai-invocations?page=1&page_size=3", cookies=cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 3

    async def test_filter_by_provider(self, client, monkeypatch):
        cookies = await _admin_cookies(monkeypatch)
        log_ai_usage(event_type="topics", provider="gemini",
                     model="gemini-3-flash-preview",
                     input_tokens=10, output_tokens=5, latency_ms=50)
        log_ai_usage(event_type="search", provider="groq",
                     model="llama-3.3-70b-versatile",
                     input_tokens=10, output_tokens=5, latency_ms=50)
        resp = await client.get(
            "/api/admin/ai-invocations?provider=groq", cookies=cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["provider"] == "groq"

    async def test_filter_by_success_false(self, client, monkeypatch):
        cookies = await _admin_cookies(monkeypatch)
        log_ai_usage(event_type="topics", provider="gemini",
                     model="gemini-3-flash-preview",
                     input_tokens=10, output_tokens=5, latency_ms=50)
        log_ai_usage(event_type="topics", provider="gemini",
                     model="gemini-3-flash-preview",
                     input_tokens=0, output_tokens=0, latency_ms=10,
                     success=False, error_message="boom")
        resp = await client.get(
            "/api/admin/ai-invocations?success=false", cookies=cookies,
        )
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["success"] is False

    async def test_response_includes_preview_fields(self, client, monkeypatch):
        cookies = await _admin_cookies(monkeypatch)
        log_ai_usage(event_type="topics", provider="gemini",
                     model="gemini-3-flash-preview",
                     input_tokens=10, output_tokens=5, latency_ms=50)
        resp = await client.get("/api/admin/ai-invocations", cookies=cookies)
        data = resp.json()
        assert "prompt_preview" in data["items"][0]
        assert "response_preview" in data["items"][0]


class TestProcessEventsEndpoint:
    async def test_requires_admin(self, client):
        resp = await client.get("/api/admin/process-events")
        assert resp.status_code == 403

    async def test_returns_empty(self, client, monkeypatch):
        cookies = await _admin_cookies(monkeypatch)
        resp = await client.get("/api/admin/process-events", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert "filters" in data
        assert data["filters"]["statuses"] == ["ok", "error", "warning", "info"]

    async def test_returns_logged_events(self, client, monkeypatch):
        cookies = await _admin_cookies(monkeypatch)
        log_process_event(component="scheduler", event_type="refresh_news",
                          status="ok", duration_ms=500)
        log_process_event(component="ai", event_type="topics",
                          status="error", duration_ms=200, message="fail")

        resp = await client.get("/api/admin/process-events", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2

    async def test_filter_by_component(self, client, monkeypatch):
        cookies = await _admin_cookies(monkeypatch)
        log_process_event(component="scheduler", event_type="refresh_news")
        log_process_event(component="ai", event_type="topics")

        resp = await client.get(
            "/api/admin/process-events?component=ai", cookies=cookies,
        )
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["component"] == "ai"

    async def test_filter_by_status(self, client, monkeypatch):
        cookies = await _admin_cookies(monkeypatch)
        log_process_event(component="scheduler", event_type="x", status="ok")
        log_process_event(component="scheduler", event_type="y", status="error")
        log_process_event(component="scheduler", event_type="z", status="error")

        resp = await client.get(
            "/api/admin/process-events?status=error", cookies=cookies,
        )
        data = resp.json()
        assert data["total"] == 2

    async def test_pagination(self, client, monkeypatch):
        cookies = await _admin_cookies(monkeypatch)
        for i in range(7):
            log_process_event(component="scheduler", event_type=f"e{i}")

        resp = await client.get(
            "/api/admin/process-events?page=1&page_size=3", cookies=cookies,
        )
        data = resp.json()
        assert data["total"] == 7
        assert len(data["items"]) == 3

        resp2 = await client.get(
            "/api/admin/process-events?page=3&page_size=3", cookies=cookies,
        )
        data2 = resp2.json()
        assert len(data2["items"]) == 1


class TestInfraCostsEndpoint:
    async def test_requires_admin(self, client):
        resp = await client.get("/api/admin/infra-costs")
        assert resp.status_code == 403

    async def test_not_available_without_token(self, client, monkeypatch):
        cookies = await _admin_cookies(monkeypatch)
        resp = await client.get("/api/admin/infra-costs", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False
        assert data["reason"] == "no_token"

    async def test_returns_snapshot_when_configured(self, client, monkeypatch):
        cookies = await _admin_cookies(monkeypatch)
        monkeypatch.setenv("RAILWAY_API_TOKEN", "t")
        monkeypatch.setenv("RAILWAY_PROJECT_ID", "p")
        save_snapshot([
            {"service_name": "web", "service_id": "s1", "usd_month": 4.2, "raw": {}},
            {"service_name": "db", "service_id": "s2", "usd_month": 1.8, "raw": {}},
        ])
        resp = await client.get("/api/admin/infra-costs", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["total_usd_month"] == pytest.approx(6.0)
        assert len(data["services"]) == 2
        assert "history" in data
