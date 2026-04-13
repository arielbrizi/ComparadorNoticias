"""Tests for app.ai_store — AI usage logging and provider config."""

from __future__ import annotations

import pytest

from app.ai_store import (
    VALID_EVENT_TYPES,
    VALID_PROVIDERS,
    compute_cost,
    get_provider_config,
    init_ai_tables,
    log_ai_usage,
    query_ai_cost_summary,
    query_ai_daily_cost,
    set_provider_config,
)


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    """Clear the in-memory config cache before each test."""
    monkeypatch.setattr("app.ai_store._config_cache", {})
    monkeypatch.setattr("app.ai_store._config_cache_ts", 0)


class TestComputeCost:
    def test_gemini_pricing(self):
        cost_in, cost_out = compute_cost("gemini-3-flash-preview", 1_000_000, 1_000_000)
        assert cost_in == pytest.approx(0.50)
        assert cost_out == pytest.approx(3.00)

    def test_groq_free_tier(self):
        cost_in, cost_out = compute_cost("llama-3.3-70b-versatile", 500_000, 200_000)
        assert cost_in == 0.0
        assert cost_out == 0.0

    def test_unknown_model_zero_cost(self):
        cost_in, cost_out = compute_cost("unknown-model", 100_000, 50_000)
        assert cost_in == 0.0
        assert cost_out == 0.0

    def test_zero_tokens(self):
        cost_in, cost_out = compute_cost("gemini-3-flash-preview", 0, 0)
        assert cost_in == 0.0
        assert cost_out == 0.0


class TestInitAndSeed:
    def test_init_creates_tables(self, temp_db):
        init_ai_tables()
        config = get_provider_config()
        assert set(config.keys()) == VALID_EVENT_TYPES
        for v in config.values():
            assert v == "gemini_fallback_groq"

    def test_init_is_idempotent(self, temp_db):
        init_ai_tables()
        init_ai_tables()
        config = get_provider_config()
        assert len(config) == len(VALID_EVENT_TYPES)


class TestLogAndQuery:
    @pytest.fixture(autouse=True)
    def _init(self, temp_db):
        init_ai_tables()

    def test_log_and_query_summary(self):
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
            provider="gemini",
            model="gemini-3-flash-preview",
            input_tokens=10000,
            output_tokens=200,
            latency_ms=800,
        )

        summary = query_ai_cost_summary()
        assert summary["totals"]["calls"] == 2
        assert summary["totals"]["input_tokens"] == 15000
        assert summary["totals"]["output_tokens"] == 300
        assert summary["totals"]["cost_total"] > 0

    def test_log_error(self):
        log_ai_usage(
            event_type="topics",
            provider="gemini",
            model="gemini-3-flash-preview",
            input_tokens=0,
            output_tokens=0,
            latency_ms=500,
            success=False,
            error_message="Rate limited",
        )
        summary = query_ai_cost_summary()
        assert summary["totals"]["calls"] == 1
        assert summary["totals"]["success_count"] == 0

    def test_query_daily_cost(self):
        log_ai_usage(
            event_type="topics",
            provider="gemini",
            model="gemini-3-flash-preview",
            input_tokens=5000,
            output_tokens=100,
            latency_ms=1000,
        )
        daily = query_ai_daily_cost()
        assert len(daily) == 1
        assert daily[0]["calls"] == 1
        assert daily[0]["cost_total"] > 0

    def test_by_event_breakdown(self):
        log_ai_usage(
            event_type="topics",
            provider="gemini",
            model="gemini-3-flash-preview",
            input_tokens=5000,
            output_tokens=100,
            latency_ms=500,
        )
        log_ai_usage(
            event_type="search",
            provider="groq",
            model="llama-3.3-70b-versatile",
            input_tokens=3000,
            output_tokens=80,
            latency_ms=300,
        )

        summary = query_ai_cost_summary()
        events = summary["by_event"]
        assert len(events) == 2
        event_types = {e["event_type"] for e in events}
        assert event_types == {"topics", "search"}


class TestProviderConfig:
    @pytest.fixture(autouse=True)
    def _init(self, temp_db):
        init_ai_tables()

    def test_default_config(self):
        config = get_provider_config()
        assert config["topics"] == "gemini_fallback_groq"
        assert config["search"] == "gemini_fallback_groq"

    def test_set_and_get(self):
        ok = set_provider_config("topics", "groq")
        assert ok is True
        config = get_provider_config()
        assert config["topics"] == "groq"

    def test_set_invalid_event_type(self):
        ok = set_provider_config("nonexistent", "groq")
        assert ok is False

    def test_set_invalid_provider(self):
        ok = set_provider_config("topics", "openai")
        assert ok is False

    def test_config_cache_invalidation(self):
        get_provider_config()
        set_provider_config("topics", "gemini")
        config = get_provider_config()
        assert config["topics"] == "gemini"
