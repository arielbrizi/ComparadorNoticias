"""Integration tests for /api/admin/ai-cost, /api/admin/ai-config and /api/admin/scheduler-config endpoints."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.ai_store import (
    get_global_monthly_budget,
    get_provider_limit,
    get_scheduler_config,
    init_ai_tables,
    load_last_good_topics,
    log_ai_usage,
    save_last_good_topics,
    set_global_monthly_budget,
    set_provider_limits,
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
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)

    from app.metrics_store import init_db
    from app.news_store import init_news_tables

    init_db()
    init_news_tables()
    init_users_table()
    init_tracking_table()
    init_ai_tables()

    # Reset module-level caches so a fresh ``temp_db`` per test isn't shadowed
    # by data from previous tests still living in process memory.
    monkeypatch.setattr("app.ai_store._limits_cache", {})
    monkeypatch.setattr("app.ai_store._limits_cache_ts", 0)
    monkeypatch.setattr("app.ai_store._usage_cache", {})
    monkeypatch.setattr("app.ai_store._cost_cache", {})
    monkeypatch.setattr("app.ai_store._runtime_cache", {})
    monkeypatch.setattr("app.ai_store._runtime_cache_ts", 0)
    monkeypatch.setattr("app.ai_store._config_cache", {})
    monkeypatch.setattr("app.ai_store._config_cache_ts", 0)

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
        assert data["config"]["topics"] == ["gemini", "groq"]
        assert set(data["valid_providers"]) == {"gemini", "groq", "ollama"}

    async def test_set_updates_config(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-config",
            json={"event_type": "topics", "providers": ["groq"]},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["providers"] == ["groq"]

        resp = await client.get("/api/admin/ai-config", cookies={"vs_token": token})
        assert resp.json()["config"]["topics"] == ["groq"]

    async def test_set_invalid_event_type(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-config",
            json={"event_type": "invalid", "providers": ["groq"]},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400

    async def test_set_invalid_provider(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-config",
            json={"event_type": "topics", "providers": ["openai"]},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400

    async def test_set_empty_chain_rejected(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-config",
            json={"event_type": "topics", "providers": []},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400

    async def test_set_duplicate_providers_rejected(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-config",
            json={"event_type": "topics", "providers": ["groq", "groq"]},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400

    async def test_set_three_provider_chain(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-config",
            json={"event_type": "topics", "providers": ["ollama", "gemini", "groq"]},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["providers"] == ["ollama", "gemini", "groq"]

        resp = await client.get("/api/admin/ai-config", cookies={"vs_token": token})
        assert resp.json()["config"]["topics"] == ["ollama", "gemini", "groq"]

    async def test_legacy_provider_string_still_accepted(self, client, monkeypatch):
        """Back-compat: old clients sending ``provider: "X_fallback_Y"`` keep working."""
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-config",
            json={"event_type": "topics", "provider": "groq_fallback_gemini"},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["providers"] == ["groq", "gemini"]

        resp = await client.get("/api/admin/ai-config", cookies={"vs_token": token})
        assert resp.json()["config"]["topics"] == ["groq", "gemini"]

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


class TestAILimitsBudget:
    """The /api/admin/ai-limits endpoint exposes the monthly_usd field per pair
    plus the global budget block; both are settable via POST."""

    async def test_get_includes_monthly_usd_and_global(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.get("/api/admin/ai-limits", cookies={"vs_token": token})
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "global" in data
        gemini = next(i for i in data["items"] if i["provider"] == "gemini")
        assert "monthly_usd" in gemini
        assert "monthly_usd_used" in gemini
        assert "daily_usd_used" in gemini
        assert "daily_usd_cap" in gemini
        # Defaults: no presupuesto.
        assert gemini["monthly_usd"] is None
        assert gemini["daily_usd_cap"] is None
        assert data["global"]["monthly_usd"] is None
        assert data["global"]["daily_usd_cap"] is None

    async def test_post_sets_monthly_usd(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-limits",
            json={
                "provider": "gemini",
                "model": "gemini-3-flash-preview",
                "rpm": None, "tpm": None, "rpd": None, "tpd": None,
                "monthly_usd": 25.5,
            },
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["item"]["monthly_usd"] == 25.5
        assert body["item"]["daily_usd_cap"] is not None  # cap derivado activo
        # Persistencia a través de get_provider_limit.
        lim = get_provider_limit("gemini", "gemini-3-flash-preview")
        assert lim["monthly_usd"] == 25.5

    async def test_post_clears_monthly_usd_with_null(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        # Primero seteamos algo.
        set_provider_limits(
            "gemini", "gemini-3-flash-preview", None, None, None, None,
            monthly_usd=42.0,
        )
        resp = await client.post(
            "/api/admin/ai-limits",
            json={
                "provider": "gemini",
                "model": "gemini-3-flash-preview",
                "rpm": None, "tpm": None, "rpd": None, "tpd": None,
                "monthly_usd": None,
            },
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        lim = get_provider_limit("gemini", "gemini-3-flash-preview")
        assert lim["monthly_usd"] is None

    async def test_post_rejects_negative_budget(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-limits",
            json={
                "provider": "gemini",
                "model": "gemini-3-flash-preview",
                "rpm": None, "tpm": None, "rpd": None, "tpd": None,
                "monthly_usd": -5,
            },
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400

    async def test_blocked_by_includes_daily_usd_when_overspent(
        self, client, monkeypatch,
    ):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        # Presupuesto chiquito y un consumo que ya lo agotó.
        set_provider_limits(
            "gemini", "gemini-3-flash-preview", None, None, None, None,
            monthly_usd=0.01,
        )
        # 1M input + 1M output @ Gemini = $3.50, mucho mayor a $0.01.
        log_ai_usage(
            event_type="topics", provider="gemini",
            model="gemini-3-flash-preview",
            input_tokens=1_000_000, output_tokens=1_000_000, latency_ms=10,
        )
        # Cache de costo: el helper se invalida con el set_provider_limits, pero
        # log_ai_usage no toca el cache. Forzamos invalidación pre-test.
        from app.ai_store import invalidate_provider_usage_cache
        invalidate_provider_usage_cache()

        resp = await client.get("/api/admin/ai-limits", cookies={"vs_token": token})
        data = resp.json()
        gemini = next(i for i in data["items"] if i["provider"] == "gemini")
        assert "monthly_usd" in gemini["blocked_by"]
        # daily_usd también dispara porque cap deriva en 0 al agotarse el mes.
        assert "daily_usd" in gemini["blocked_by"]


class TestAIBudgetGlobalEndpoint:
    async def test_get_requires_admin(self, client):
        resp = await client.get("/api/admin/ai-budget-global")
        assert resp.status_code == 403

    async def test_post_requires_admin(self, client):
        resp = await client.post(
            "/api/admin/ai-budget-global", json={"monthly_usd": 50.0},
        )
        assert resp.status_code == 403

    async def test_get_returns_global_block(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.get(
            "/api/admin/ai-budget-global", cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "global" in data
        assert data["global"]["monthly_usd"] is None
        assert data["global"]["monthly_usd_used"] == 0.0

    async def test_set_and_get_roundtrip(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-budget-global", json={"monthly_usd": 100.0},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["global"]["monthly_usd"] == 100.0
        assert body["global"]["daily_usd_cap"] is not None

        assert get_global_monthly_budget() == 100.0

    async def test_reset_clears_budget(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        set_global_monthly_budget(75.0)
        resp = await client.post(
            "/api/admin/ai-budget-global", json={"reset": True},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["global"]["monthly_usd"] is None
        assert get_global_monthly_budget() is None

    async def test_null_clears_budget(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        set_global_monthly_budget(75.0)
        resp = await client.post(
            "/api/admin/ai-budget-global", json={"monthly_usd": None},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 200
        assert get_global_monthly_budget() is None

    async def test_rejects_negative(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-budget-global", json={"monthly_usd": -1},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400

    async def test_rejects_non_numeric(self, client, monkeypatch):
        monkeypatch.setattr("app.user_store.ADMIN_EMAILS", ["admin@test.com"])
        admin = upsert_user("admin@test.com", "Admin", "")
        token = _make_admin_token(user_id=admin["id"], email=admin["email"])

        resp = await client.post(
            "/api/admin/ai-budget-global", json={"monthly_usd": "fifty"},
            cookies={"vs_token": token},
        )
        assert resp.status_code == 400
