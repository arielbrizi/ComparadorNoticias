"""Tests for app.ai_store — AI usage logging and provider config."""

from __future__ import annotations

import pytest

from app.ai_store import (
    OLLAMA_TIMEOUT_DEFAULT,
    OLLAMA_TIMEOUT_MAX,
    OLLAMA_TIMEOUT_MIN,
    VALID_EVENT_TYPES,
    VALID_PROVIDERS,
    compute_cost,
    count_ai_invocations,
    get_ollama_timeout,
    get_provider_config,
    get_schedule_config,
    init_ai_tables,
    is_in_quiet_hours,
    list_distinct_providers,
    log_ai_usage,
    query_ai_cost_summary,
    query_ai_daily_cost,
    query_ai_invocations,
    set_ollama_timeout,
    set_provider_config,
    set_schedule_config,
)


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    """Clear the in-memory config cache before each test."""
    monkeypatch.setattr("app.ai_store._config_cache", {})
    monkeypatch.setattr("app.ai_store._config_cache_ts", 0)
    monkeypatch.setattr("app.ai_store._runtime_cache", {})
    monkeypatch.setattr("app.ai_store._runtime_cache_ts", 0)


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

    def test_ollama_models_are_free(self):
        for model in ("qwen3:8b", "qwen2.5:7b-instruct", "llama3.1:8b", "llama3.2:3b"):
            cost_in, cost_out = compute_cost(model, 500_000, 200_000)
            assert cost_in == 0.0, model
            assert cost_out == 0.0, model

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

    def test_set_groq_fallback_gemini(self):
        ok = set_provider_config("topics", "groq_fallback_gemini")
        assert ok is True
        config = get_provider_config()
        assert config["topics"] == "groq_fallback_gemini"

    def test_set_ollama_provider(self):
        ok = set_provider_config("topics", "ollama")
        assert ok is True
        config = get_provider_config()
        assert config["topics"] == "ollama"

    def test_set_ollama_fallback_modes(self):
        for mode in (
            "gemini_fallback_ollama",
            "ollama_fallback_gemini",
            "groq_fallback_ollama",
            "ollama_fallback_groq",
        ):
            ok = set_provider_config("topics", mode)
            assert ok is True, mode
            config = get_provider_config()
            assert config["topics"] == mode

    def test_valid_providers_contains_ollama_modes(self):
        expected = {
            "gemini", "groq", "ollama",
            "gemini_fallback_groq", "groq_fallback_gemini",
            "gemini_fallback_ollama", "ollama_fallback_gemini",
            "groq_fallback_ollama", "ollama_fallback_groq",
        }
        assert expected.issubset(VALID_PROVIDERS)


class TestScheduleConfig:
    @pytest.fixture(autouse=True)
    def _init(self, temp_db):
        init_ai_tables()

    def test_empty_by_default(self):
        schedule = get_schedule_config()
        assert schedule == {}

    def test_set_and_get(self):
        ok = set_schedule_config("search_prefetch", "00:00", "06:00")
        assert ok is True
        schedule = get_schedule_config()
        assert schedule["search_prefetch"] == {"quiet_start": "00:00", "quiet_end": "06:00"}

    def test_clear_schedule(self):
        set_schedule_config("search_prefetch", "00:00", "06:00")
        ok = set_schedule_config("search_prefetch", "", "")
        assert ok is True
        schedule = get_schedule_config()
        assert "search_prefetch" not in schedule

    def test_invalid_event_type(self):
        ok = set_schedule_config("invalid", "00:00", "06:00")
        assert ok is False

    def test_invalid_time_format(self):
        assert set_schedule_config("search_prefetch", "25:00", "06:00") is False
        assert set_schedule_config("search_prefetch", "abc", "06:00") is False

    def test_mismatched_empty(self):
        assert set_schedule_config("search_prefetch", "00:00", "") is False
        assert set_schedule_config("search_prefetch", "", "06:00") is False

    def test_update_existing(self):
        set_schedule_config("search_prefetch", "00:00", "06:00")
        set_schedule_config("search_prefetch", "22:00", "07:00")
        schedule = get_schedule_config()
        assert schedule["search_prefetch"] == {"quiet_start": "22:00", "quiet_end": "07:00"}

    def test_is_in_quiet_hours_no_config(self):
        assert is_in_quiet_hours("search_prefetch") is False

    def test_is_in_quiet_hours_inside(self, monkeypatch):
        from datetime import datetime, timezone, timedelta
        set_schedule_config("search_prefetch", "00:00", "06:00")
        ART = timezone(timedelta(hours=-3))
        fake_now = datetime(2026, 4, 13, 3, 30, tzinfo=ART)
        monkeypatch.setattr("app.ai_store.datetime", type("FakeDT", (), {
            "now": staticmethod(lambda tz=None: fake_now),
            "strftime": datetime.strftime,
        })())
        assert is_in_quiet_hours("search_prefetch") is True

    def test_is_in_quiet_hours_outside(self, monkeypatch):
        from datetime import datetime, timezone, timedelta
        set_schedule_config("search_prefetch", "00:00", "06:00")
        ART = timezone(timedelta(hours=-3))
        fake_now = datetime(2026, 4, 13, 10, 0, tzinfo=ART)
        monkeypatch.setattr("app.ai_store.datetime", type("FakeDT", (), {
            "now": staticmethod(lambda tz=None: fake_now),
            "strftime": datetime.strftime,
        })())
        assert is_in_quiet_hours("search_prefetch") is False

    def test_is_in_quiet_hours_wraps_midnight(self, monkeypatch):
        from datetime import datetime, timezone, timedelta
        set_schedule_config("search_prefetch", "22:00", "06:00")
        ART = timezone(timedelta(hours=-3))
        fake_now = datetime(2026, 4, 13, 23, 30, tzinfo=ART)
        monkeypatch.setattr("app.ai_store.datetime", type("FakeDT", (), {
            "now": staticmethod(lambda tz=None: fake_now),
            "strftime": datetime.strftime,
        })())
        assert is_in_quiet_hours("search_prefetch") is True


class TestInvocationsQuery:
    @pytest.fixture(autouse=True)
    def _init(self, temp_db):
        init_ai_tables()

    def _seed(self):
        import time as _t
        log_ai_usage(
            event_type="topics", provider="gemini",
            model="gemini-3-flash-preview", input_tokens=100, output_tokens=20,
            latency_ms=100,
        )
        _t.sleep(0.01)
        log_ai_usage(
            event_type="search", provider="groq",
            model="llama-3.3-70b-versatile", input_tokens=50, output_tokens=10,
            latency_ms=200,
        )
        _t.sleep(0.01)
        log_ai_usage(
            event_type="search", provider="gemini",
            model="gemini-3-flash-preview", input_tokens=0, output_tokens=0,
            latency_ms=50, success=False, error_message="Rate limited",
        )

    def test_query_returns_newest_first(self):
        self._seed()
        rows = query_ai_invocations()
        assert [r["event_type"] for r in rows] == ["search", "search", "topics"]
        assert rows[0]["success"] is False

    def test_filter_by_provider(self):
        self._seed()
        rows = query_ai_invocations(provider="groq")
        assert len(rows) == 1
        assert rows[0]["provider"] == "groq"

    def test_filter_by_event_type(self):
        self._seed()
        rows = query_ai_invocations(event_type="topics")
        assert len(rows) == 1

    def test_filter_by_success_false(self):
        self._seed()
        rows = query_ai_invocations(success=False)
        assert len(rows) == 1
        assert rows[0]["error_message"] == "Rate limited"

    def test_filter_by_success_true(self):
        self._seed()
        rows = query_ai_invocations(success=True)
        assert len(rows) == 2
        assert all(r["success"] for r in rows)

    def test_pagination(self):
        self._seed()
        page1 = query_ai_invocations(limit=2, offset=0)
        page2 = query_ai_invocations(limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 1

    def test_count(self):
        self._seed()
        assert count_ai_invocations() == 3
        assert count_ai_invocations(provider="gemini") == 2
        assert count_ai_invocations(success=False) == 1

    def test_list_distinct_providers(self):
        self._seed()
        providers = list_distinct_providers()
        assert set(providers) == {"gemini", "groq"}


class TestPreviews:
    @pytest.fixture(autouse=True)
    def _init(self, temp_db):
        init_ai_tables()

    def test_previews_off_by_default(self, monkeypatch):
        monkeypatch.setattr("app.ai_store._AI_LOG_PREVIEWS", False)
        log_ai_usage(
            event_type="topics", provider="gemini",
            model="gemini-3-flash-preview", input_tokens=10, output_tokens=5,
            latency_ms=100,
            prompt_preview="hello prompt",
            response_preview="hello response",
        )
        rows = query_ai_invocations()
        assert rows[0]["prompt_preview"] is None
        assert rows[0]["response_preview"] is None

    def test_previews_saved_when_enabled(self, monkeypatch):
        monkeypatch.setattr("app.ai_store._AI_LOG_PREVIEWS", True)
        log_ai_usage(
            event_type="topics", provider="gemini",
            model="gemini-3-flash-preview", input_tokens=10, output_tokens=5,
            latency_ms=100,
            prompt_preview="hello prompt",
            response_preview="hello response",
        )
        rows = query_ai_invocations()
        assert rows[0]["prompt_preview"] == "hello prompt"
        assert rows[0]["response_preview"] == "hello response"

    def test_previews_truncated_to_2000(self, monkeypatch):
        monkeypatch.setattr("app.ai_store._AI_LOG_PREVIEWS", True)
        huge = "x" * 5000
        log_ai_usage(
            event_type="topics", provider="gemini",
            model="gemini-3-flash-preview", input_tokens=10, output_tokens=5,
            latency_ms=100,
            prompt_preview=huge,
            response_preview=huge,
        )
        rows = query_ai_invocations()
        assert len(rows[0]["prompt_preview"]) == 2000
        assert len(rows[0]["response_preview"]) == 2000

    def test_empty_previews_stored_as_null(self, monkeypatch):
        monkeypatch.setattr("app.ai_store._AI_LOG_PREVIEWS", True)
        log_ai_usage(
            event_type="topics", provider="gemini",
            model="gemini-3-flash-preview", input_tokens=10, output_tokens=5,
            latency_ms=100,
            prompt_preview="",
            response_preview=None,
        )
        rows = query_ai_invocations()
        assert rows[0]["prompt_preview"] is None
        assert rows[0]["response_preview"] is None


class TestOllamaErrorPromptPersistence:
    """Prompt preview is force-persisted on failed Ollama calls even when the
    global AI_LOG_PREVIEWS flag is off, so timeouts can be diagnosed without
    flipping it for every invocation."""

    @pytest.fixture(autouse=True)
    def _init(self, temp_db, monkeypatch):
        init_ai_tables()
        monkeypatch.setattr("app.ai_store._AI_LOG_PREVIEWS", False)

    def test_ollama_error_persists_prompt(self):
        log_ai_usage(
            event_type="search", provider="ollama",
            model="qwen3:8b", input_tokens=0, output_tokens=0,
            latency_ms=120000, success=False,
            error_message="Ollama read timeout after 120s",
            prompt_preview="prompt que se envio a ollama",
            response_preview="should be dropped",
        )
        rows = query_ai_invocations()
        assert rows[0]["prompt_preview"] == "prompt que se envio a ollama"
        assert rows[0]["response_preview"] is None

    def test_ollama_success_does_not_persist_prompt(self):
        log_ai_usage(
            event_type="search", provider="ollama",
            model="qwen3:8b", input_tokens=100, output_tokens=50,
            latency_ms=2000,
            prompt_preview="prompt feliz",
            response_preview="respuesta feliz",
        )
        rows = query_ai_invocations()
        assert rows[0]["prompt_preview"] is None
        assert rows[0]["response_preview"] is None

    def test_gemini_error_does_not_persist_prompt(self):
        log_ai_usage(
            event_type="search", provider="gemini",
            model="gemini-3-flash-preview", input_tokens=0, output_tokens=0,
            latency_ms=500, success=False,
            error_message="Rate limited",
            prompt_preview="prompt gemini",
        )
        rows = query_ai_invocations()
        assert rows[0]["prompt_preview"] is None

    def test_fallback_provider_with_ollama_persists_prompt(self):
        for mode in (
            "gemini_fallback_ollama",
            "ollama_fallback_gemini",
            "groq_fallback_ollama",
            "ollama_fallback_groq",
        ):
            log_ai_usage(
                event_type="search", provider=mode,
                model="qwen3:8b", input_tokens=0, output_tokens=0,
                latency_ms=120000, success=False,
                error_message="timeout",
                prompt_preview=f"prompt via {mode}",
            )
        rows = query_ai_invocations()
        assert len(rows) == 4
        for r in rows:
            assert r["prompt_preview"] == f"prompt via {r['provider']}", r["provider"]

    def test_ollama_error_prompt_truncated_to_2000(self):
        huge = "x" * 5000
        log_ai_usage(
            event_type="search", provider="ollama",
            model="qwen3:8b", input_tokens=0, output_tokens=0,
            latency_ms=120000, success=False,
            error_message="timeout",
            prompt_preview=huge,
        )
        rows = query_ai_invocations()
        assert len(rows[0]["prompt_preview"]) == 2000


class TestOllamaTimeoutConfig:
    """Ollama invocation timeout is admin-configurable via ai_runtime_config."""

    @pytest.fixture(autouse=True)
    def _init(self, temp_db):
        init_ai_tables()

    def test_default_when_no_row(self):
        assert get_ollama_timeout() == OLLAMA_TIMEOUT_DEFAULT

    def test_set_and_get_roundtrip(self):
        assert set_ollama_timeout(300) is True
        assert get_ollama_timeout() == 300

    def test_update_overwrites_previous(self):
        set_ollama_timeout(180)
        set_ollama_timeout(240)
        assert get_ollama_timeout() == 240

    def test_rejects_below_min(self):
        assert set_ollama_timeout(OLLAMA_TIMEOUT_MIN - 1) is False
        assert get_ollama_timeout() == OLLAMA_TIMEOUT_DEFAULT

    def test_rejects_above_max(self):
        assert set_ollama_timeout(OLLAMA_TIMEOUT_MAX + 1) is False
        assert get_ollama_timeout() == OLLAMA_TIMEOUT_DEFAULT

    def test_accepts_bounds(self):
        assert set_ollama_timeout(OLLAMA_TIMEOUT_MIN) is True
        assert get_ollama_timeout() == OLLAMA_TIMEOUT_MIN
        assert set_ollama_timeout(OLLAMA_TIMEOUT_MAX) is True
        assert get_ollama_timeout() == OLLAMA_TIMEOUT_MAX

    def test_rejects_non_int(self):
        assert set_ollama_timeout("180") is False  # type: ignore[arg-type]
        assert set_ollama_timeout(180.5) is False  # type: ignore[arg-type]
        assert set_ollama_timeout(True) is False
        assert get_ollama_timeout() == OLLAMA_TIMEOUT_DEFAULT

    def test_out_of_range_stored_value_falls_back_to_default(self, monkeypatch):
        # Simulate a value that somehow drifted outside the allowed range
        # (e.g. manual DB edit or range tightening in a future release).
        monkeypatch.setattr(
            "app.ai_store._get_runtime_value", lambda _k: "99999",
        )
        assert get_ollama_timeout() == OLLAMA_TIMEOUT_DEFAULT

    def test_unparseable_stored_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setattr(
            "app.ai_store._get_runtime_value", lambda _k: "not-a-number",
        )
        assert get_ollama_timeout() == OLLAMA_TIMEOUT_DEFAULT
