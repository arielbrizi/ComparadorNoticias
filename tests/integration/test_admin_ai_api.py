"""Integration tests for /api/admin/ai-cost, /api/admin/ai-config and /api/admin/scheduler-config endpoints."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.ai_store import (
    get_scheduler_config,
    init_ai_tables,
    load_last_good_topics,
    log_ai_usage,
    save_last_good_topics,
    set_scheduler_interval,
    SCHEDULER_DEFAULTS,
    VALID_SCHEDULER_INTERVALS,
)
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


class TestLastGoodTopicsPersistence:
    async def test_save_and_load_round_trip(self, temp_db):
        init_ai_tables()
        topics = [{"label": "Dólar", "emoji": "💵"}, {"label": "Inflación", "emoji": "📈"}]
        save_last_good_topics(topics, "Gemini", "2026-04-13T10:00:00+00:00")

        loaded = load_last_good_topics()
        assert loaded is not None
        assert loaded["topics"] == topics
        assert loaded["ai_provider"] == "Gemini"
        assert loaded["generated_at"] == "2026-04-13T10:00:00+00:00"

    async def test_save_overwrites_previous(self, temp_db):
        init_ai_tables()
        save_last_good_topics([{"label": "Old", "emoji": "📰"}], "Groq", "2026-04-12T08:00:00+00:00")
        save_last_good_topics([{"label": "New", "emoji": "🆕"}], "Gemini", "2026-04-13T10:00:00+00:00")

        loaded = load_last_good_topics()
        assert loaded is not None
        assert len(loaded["topics"]) == 1
        assert loaded["topics"][0]["label"] == "New"
        assert loaded["ai_provider"] == "Gemini"

    async def test_load_returns_none_when_empty(self, temp_db):
        init_ai_tables()
        loaded = load_last_good_topics()
        assert loaded is None

    async def test_save_ignores_empty_topics(self, temp_db):
        init_ai_tables()
        save_last_good_topics([], "Gemini", "2026-04-13T10:00:00+00:00")
        loaded = load_last_good_topics()
        assert loaded is None


class TestSchedulerConfigStore:
    async def test_defaults_returned_when_no_db_rows(self, temp_db):
        init_ai_tables()
        config = get_scheduler_config()
        assert config["refresh_news"] == SCHEDULER_DEFAULTS["refresh_news"]
        assert config["prefetch_topics"] == SCHEDULER_DEFAULTS["prefetch_topics"]

    async def test_set_and_get_interval(self, temp_db):
        init_ai_tables()
        ok = set_scheduler_interval("refresh_news", 30)
        assert ok is True
        config = get_scheduler_config()
        assert config["refresh_news"] == 30

    async def test_set_invalid_job_key(self, temp_db):
        init_ai_tables()
        ok = set_scheduler_interval("nonexistent_job", 10)
        assert ok is False

    async def test_set_invalid_interval(self, temp_db):
        init_ai_tables()
        ok = set_scheduler_interval("refresh_news", 999)
        assert ok is False

    async def test_overwrite_interval(self, temp_db):
        init_ai_tables()
        set_scheduler_interval("prefetch_topics", 120)
        set_scheduler_interval("prefetch_topics", 240)
        config = get_scheduler_config()
        assert config["prefetch_topics"] == 240


class TestSchedulerConfigEndpoint:
    async def test_get_requires_admin(self, client):
        resp = await client.get("/api/admin/scheduler-config")
        assert resp.status_code == 403

    async def test_post_requires_admin(self, client):
        resp = await client.post("/api/admin/scheduler-config", json={
            "job_key": "refresh_news", "interval_minutes": 30,
        })
        assert resp.status_code == 403

    async def test_get_returns_config(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.get("/api/admin/scheduler-config", cookies={"vs_token": token})
        assert resp.status_code == 200
        data = resp.json()
        assert "config" in data
        assert "defaults" in data
        assert "valid_intervals" in data
        assert data["config"]["refresh_news"] == SCHEDULER_DEFAULTS["refresh_news"]

    async def test_set_updates_interval(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/scheduler-config",
            json={"job_key": "refresh_news", "interval_minutes": 30},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        resp = await client.get("/api/admin/scheduler-config", cookies={"vs_token": token})
        assert resp.json()["config"]["refresh_news"] == 30

    async def test_set_invalid_job_key(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/scheduler-config",
            json={"job_key": "invalid_job", "interval_minutes": 10},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400

    async def test_set_invalid_interval(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/scheduler-config",
            json={"job_key": "refresh_news", "interval_minutes": 999},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400

    async def test_set_topics_interval(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/scheduler-config",
            json={"job_key": "prefetch_topics", "interval_minutes": 120},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200

        resp = await client.get("/api/admin/scheduler-config", cookies={"vs_token": token})
        assert resp.json()["config"]["prefetch_topics"] == 120
