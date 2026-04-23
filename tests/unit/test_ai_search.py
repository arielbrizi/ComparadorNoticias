import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.ai_search import (
    GEMINI_TIMEOUT,
    OLLAMA_MAX_PROMPT_CHARS,
    OllamaCallError,
    QuotaExhaustedError,
    _build_context,
    _call_ai,
    _call_gemini,
    _call_groq,
    _call_ollama,
    _clean_json_response,
    _last_good_topics,
    _prefetch_topic_searches,
    _provider_chain,
    _quota_blocked,
    ai_top_story,
    _parse_retry_seconds,
    ai_news_search,
    ai_topics,
    ai_weekly_summary,
    restore_last_good_topics,
)


def _no_ai(monkeypatch):
    """Helper: disable all AI providers."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setattr("app.ai_search._gemini_client", None)
    monkeypatch.setattr("app.ai_search._groq_client", None)
    monkeypatch.setattr("app.ai_search._ollama_client", None)


def _mock_ai_store(monkeypatch, provider=None, chain=None):
    """Stub out ai_store DB calls used by ``_call_ai`` / ``_call_ai_search``.

    *provider* accepts the old enum strings (``"gemini_fallback_groq"``) for
    backward compatibility with existing tests — they're translated to the
    equivalent ordered chain. *chain* takes precedence when given.

    The quota-guard lookups are also stubbed (no limits, zero usage) so the
    routing tests don't accidentally hit the real dev DB, which would make
    them flaky depending on recent AI usage.
    """
    if chain is None:
        if provider is None:
            chain = ["gemini", "groq"]
        elif "_fallback_" in provider:
            primary, _, secondary = provider.partition("_fallback_")
            chain = [primary, secondary]
        else:
            chain = [provider]
    monkeypatch.setattr(
        "app.ai_search.get_provider_config",
        lambda: {et: list(chain) for et in
                 ("search", "search_prefetch", "topics", "weekly_summary", "top_story")},
    )
    monkeypatch.setattr("app.ai_search.log_ai_usage", lambda **kw: None)
    monkeypatch.setattr(
        "app.ai_search.get_provider_limit",
        lambda _p, _m: {"rpm": None, "tpm": None, "rpd": None, "tpd": None},
    )
    monkeypatch.setattr(
        "app.ai_search.query_provider_usage",
        lambda _p: {"rpm_used": 0, "tpm_used": 0, "rpd_used": 0, "tpd_used": 0},
    )


class TestBuildContext:
    def test_includes_group_ids(self, sample_groups):
        context = _build_context(sample_groups)
        assert "abc1234567" in context

    def test_includes_source_names(self, sample_groups):
        context = _build_context(sample_groups)
        assert "Clarín" in context or "Infobae" in context

    def test_empty_groups(self):
        assert _build_context([]) == ""

    def test_respects_max_groups(self, sample_groups):
        context = _build_context(sample_groups, max_groups=1)
        lines = [line for line in context.split("\n") if line.strip()]
        assert len(lines) == 1

    def test_respects_max_chars(self, sample_groups):
        full = _build_context(sample_groups)
        limited = _build_context(sample_groups, max_chars=200)
        assert len(limited) <= 200
        assert len(limited) < len(full)


class TestCleanJsonResponse:
    def test_strips_markdown_json_block(self):
        text = '```json\n{"key": "value"}\n```'
        assert _clean_json_response(text) == '{"key": "value"}'

    def test_strips_generic_code_block(self):
        text = '```\n{"key": "value"}\n```'
        assert _clean_json_response(text) == '{"key": "value"}'

    def test_plain_json_unchanged(self):
        text = '{"key": "value"}'
        assert _clean_json_response(text) == '{"key": "value"}'

    def test_strips_surrounding_whitespace(self):
        text = '  {"key": "value"}  '
        assert _clean_json_response(text) == '{"key": "value"}'


class TestParseRetrySeconds:
    def test_extracts_retry_delay(self):
        exc = Exception("429 Too Many Requests. retry in 10s")
        result = _parse_retry_seconds(exc)
        assert result == 10.0

    def test_non_429_returns_none(self):
        exc = Exception("500 Internal Server Error")
        assert _parse_retry_seconds(exc) is None

    def test_caps_at_30_seconds(self):
        exc = Exception("429 Too Many Requests. retry in 60s")
        result = _parse_retry_seconds(exc)
        assert result == 30.0

    def test_default_5s_for_429_without_explicit_delay(self):
        exc = Exception("429 Too Many Requests")
        result = _parse_retry_seconds(exc)
        assert result == 5.0


class TestCallGemini:
    async def test_timeout_raises_runtime_error(self, monkeypatch):
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)

        async def _hang(*args, **kwargs):
            await asyncio.sleep(300)

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = _hang
        monkeypatch.setattr("app.ai_search._gemini_client", mock_client)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")

        with pytest.raises(RuntimeError, match="timed out"):
            await _call_gemini("test prompt", timeout=0.1)

    async def test_successful_call(self, monkeypatch):
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)

        mock_usage = MagicMock()
        mock_usage.prompt_token_count = 100
        mock_usage.candidates_token_count = 50
        mock_response = MagicMock()
        mock_response.text = '{"answer": "ok"}'
        mock_response.usage_metadata = mock_usage

        async def _fast(*args, **kwargs):
            return mock_response

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = _fast
        monkeypatch.setattr("app.ai_search._gemini_client", mock_client)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")

        text, in_tok, out_tok = await _call_gemini("test prompt")
        assert text == '{"answer": "ok"}'
        assert in_tok == 100
        assert out_tok == 50


class TestCallAiFallback:
    async def test_falls_back_to_groq_when_gemini_fails(self, monkeypatch):
        _mock_ai_store(monkeypatch)
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("GROQ_API_KEY", "fake")

        async def _gemini_fail(*a, **kw):
            raise RuntimeError("Gemini down")

        mock_gemini = MagicMock()
        mock_gemini.aio.models.generate_content = _gemini_fail
        monkeypatch.setattr("app.ai_search._gemini_client", mock_gemini)

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 80
        mock_usage.completion_tokens = 40
        mock_choice = MagicMock()
        mock_choice.message.content = '{"result": "from groq"}'
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage = mock_usage

        mock_groq = AsyncMock()
        mock_groq.chat.completions.create = AsyncMock(return_value=mock_resp)
        monkeypatch.setattr("app.ai_search._groq_client", mock_groq)

        text, provider = await _call_ai("test prompt", event_type="topics")
        assert text == '{"result": "from groq"}'
        assert provider == "Groq"

    async def test_uses_gemini_when_available(self, monkeypatch):
        _mock_ai_store(monkeypatch)
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")

        mock_usage = MagicMock()
        mock_usage.prompt_token_count = 100
        mock_usage.candidates_token_count = 50
        mock_response = MagicMock()
        mock_response.text = '{"result": "from gemini"}'
        mock_response.usage_metadata = mock_usage

        async def _fast(*a, **kw):
            return mock_response

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = _fast
        monkeypatch.setattr("app.ai_search._gemini_client", mock_client)

        text, provider = await _call_ai("test prompt", event_type="topics")
        assert text == '{"result": "from gemini"}'
        assert provider == "Gemini"

    async def test_groq_only_when_no_gemini_key(self, monkeypatch):
        _mock_ai_store(monkeypatch)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setattr("app.ai_search._gemini_client", None)
        monkeypatch.setenv("GROQ_API_KEY", "fake")

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 80
        mock_usage.completion_tokens = 40
        mock_choice = MagicMock()
        mock_choice.message.content = '{"result": "groq only"}'
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage = mock_usage

        mock_groq = AsyncMock()
        mock_groq.chat.completions.create = AsyncMock(return_value=mock_resp)
        monkeypatch.setattr("app.ai_search._groq_client", mock_groq)

        text, provider = await _call_ai("test prompt", event_type="topics")
        assert text == '{"result": "groq only"}'
        assert provider == "Groq"

    async def test_raises_when_no_provider(self, monkeypatch):
        _mock_ai_store(monkeypatch)
        _no_ai(monkeypatch)
        with pytest.raises(RuntimeError, match="No AI provider"):
            await _call_ai("test prompt", event_type="topics")

    async def test_groq_fallback_gemini_uses_groq_first(self, monkeypatch):
        _mock_ai_store(monkeypatch, provider="groq_fallback_gemini")
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("GROQ_API_KEY", "fake")

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 80
        mock_usage.completion_tokens = 40
        mock_choice = MagicMock()
        mock_choice.message.content = '{"result": "from groq primary"}'
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage = mock_usage

        mock_groq = AsyncMock()
        mock_groq.chat.completions.create = AsyncMock(return_value=mock_resp)
        monkeypatch.setattr("app.ai_search._groq_client", mock_groq)

        mock_gemini = MagicMock()
        monkeypatch.setattr("app.ai_search._gemini_client", mock_gemini)

        text, provider = await _call_ai("test prompt", event_type="topics")
        assert text == '{"result": "from groq primary"}'
        assert provider == "Groq"

    async def test_groq_fallback_gemini_falls_back(self, monkeypatch):
        _mock_ai_store(monkeypatch, provider="groq_fallback_gemini")
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("GROQ_API_KEY", "fake")

        mock_groq = AsyncMock()
        mock_groq.chat.completions.create = AsyncMock(side_effect=RuntimeError("Groq down"))
        monkeypatch.setattr("app.ai_search._groq_client", mock_groq)

        mock_usage = MagicMock()
        mock_usage.prompt_token_count = 100
        mock_usage.candidates_token_count = 50
        mock_response = MagicMock()
        mock_response.text = '{"result": "from gemini fallback"}'
        mock_response.usage_metadata = mock_usage

        async def _fast(*a, **kw):
            return mock_response

        mock_gemini = MagicMock()
        mock_gemini.aio.models.generate_content = _fast
        monkeypatch.setattr("app.ai_search._gemini_client", mock_gemini)

        text, provider = await _call_ai("test prompt", event_type="topics")
        assert text == '{"result": "from gemini fallback"}'
        assert provider == "Gemini"


class TestCallAiErrorLogging:
    """Both provider paths MUST persist errors to ai_usage_log.

    Regression: the final Groq call in the fallback arm was not wrapped
    in try/except, so 429 / quota errors silently bubbled up without
    leaving a row in ``ai_usage_log`` and the admin monitor kept showing
    Groq as "Operativo" with 100% success.
    """

    def _capture_log(self, monkeypatch):
        calls: list[dict] = []
        monkeypatch.setattr(
            "app.ai_search.log_ai_usage", lambda **kw: calls.append(kw),
        )
        return calls

    async def test_logs_groq_error_when_used_as_gemini_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "app.ai_search.get_provider_config",
            lambda: {et: "gemini_fallback_groq" for et in
                     ("search", "search_prefetch", "topics", "weekly_summary", "top_story")},
        )
        calls = self._capture_log(monkeypatch)
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("GROQ_API_KEY", "fake")

        async def _gemini_fail(*a, **kw):
            raise RuntimeError("Gemini down")

        mock_gemini = MagicMock()
        mock_gemini.aio.models.generate_content = _gemini_fail
        monkeypatch.setattr("app.ai_search._gemini_client", mock_gemini)

        mock_groq = AsyncMock()
        mock_groq.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("429 rate limit exceeded"),
        )
        monkeypatch.setattr("app.ai_search._groq_client", mock_groq)

        with pytest.raises(RuntimeError, match="429 rate limit"):
            await _call_ai("test prompt", event_type="topics")

        providers_logged = [(c["provider"], c["success"]) for c in calls]
        assert ("gemini", False) in providers_logged
        assert ("groq", False) in providers_logged
        groq_err = next(
            c for c in calls if c["provider"] == "groq" and c["success"] is False
        )
        assert "429" in (groq_err.get("error_message") or "")

    async def test_logs_groq_error_when_groq_only_mode(self, monkeypatch):
        monkeypatch.setattr(
            "app.ai_search.get_provider_config",
            lambda: {et: "groq" for et in
                     ("search", "search_prefetch", "topics", "weekly_summary", "top_story")},
        )
        calls = self._capture_log(monkeypatch)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setattr("app.ai_search._gemini_client", None)
        monkeypatch.setenv("GROQ_API_KEY", "fake")

        mock_groq = AsyncMock()
        mock_groq.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("429 tokens per day exceeded"),
        )
        monkeypatch.setattr("app.ai_search._groq_client", mock_groq)

        with pytest.raises(RuntimeError, match="429"):
            await _call_ai("test prompt", event_type="topics")

        groq_errors = [c for c in calls if c["provider"] == "groq" and c["success"] is False]
        assert len(groq_errors) == 1
        assert "429" in (groq_errors[0].get("error_message") or "")

    async def test_logs_gemini_error_when_used_as_groq_fallback(self, monkeypatch):
        monkeypatch.setattr(
            "app.ai_search.get_provider_config",
            lambda: {et: "groq_fallback_gemini" for et in
                     ("search", "search_prefetch", "topics", "weekly_summary", "top_story")},
        )
        calls = self._capture_log(monkeypatch)
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("GROQ_API_KEY", "fake")

        mock_groq = AsyncMock()
        mock_groq.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("Groq primary down"),
        )
        monkeypatch.setattr("app.ai_search._groq_client", mock_groq)

        async def _gemini_fail(*a, **kw):
            raise RuntimeError("Gemini fallback also down")

        mock_gemini = MagicMock()
        mock_gemini.aio.models.generate_content = _gemini_fail
        monkeypatch.setattr("app.ai_search._gemini_client", mock_gemini)

        with pytest.raises(RuntimeError):
            await _call_ai("test prompt", event_type="topics")

        providers_logged = [(c["provider"], c["success"]) for c in calls]
        assert ("groq", False) in providers_logged
        assert ("gemini", False) in providers_logged


class TestAiNewsSearch:
    async def test_returns_unavailable_without_api_key(self, sample_groups, monkeypatch):
        _no_ai(monkeypatch)
        result = await ai_news_search("inflación", sample_groups)
        assert result["ai_available"] is False


class TestAiTopics:
    async def test_returns_unavailable_without_api_key(self, sample_groups, monkeypatch):
        _no_ai(monkeypatch)
        result = await ai_topics(sample_groups)
        assert result["ai_available"] is False
        assert result["topics"] == []

    async def test_cached_response_includes_search_cached(self, sample_groups, monkeypatch):
        import time
        topics = [
            {"label": "Dólar", "emoji": "💵"},
            {"label": "Inflación", "emoji": "📈"},
            {"label": "Deporte", "emoji": "⚽"},
        ]
        monkeypatch.setattr("app.ai_search._topics_cache", {
            "topics": topics, "ts": time.time(), "ai_provider": "Test",
            "generated_at": "2026-04-13T12:00:00+00:00",
        })
        monkeypatch.setattr("app.ai_search._search_cache", {
            "dólar": {"summary": "ok", "has_results": True},
            "inflación": {"summary": "ok", "has_results": True},
        })

        result = await ai_topics(sample_groups)

        assert result["cached"] is True
        assert set(result["search_cached"]) == {"Dólar", "Inflación"}
        assert "Deporte" not in result["search_cached"]
        assert result["generated_at"] == "2026-04-13T12:00:00+00:00"

    async def test_fresh_response_has_empty_search_cached(self, sample_groups, monkeypatch):
        monkeypatch.setattr("app.ai_search._topics_cache", {"topics": [], "ts": 0, "generated_at": ""})
        monkeypatch.setattr("app.ai_search._search_cache", {})

        topics_json = '{"topics":[{"label":"Dólar","emoji":"💵"}]}'
        _setup_ai_mock(monkeypatch, topics_json)

        created_tasks = []
        import asyncio as _asyncio
        original_create_task = _asyncio.create_task
        def _spy(coro):
            task = original_create_task(coro)
            created_tasks.append(task)
            return task
        monkeypatch.setattr("app.ai_search.asyncio.create_task", _spy)

        result = await ai_topics(sample_groups)

        assert result["search_cached"] == []
        assert result["cached"] is False
        assert "generated_at" in result
        assert result["generated_at"]  # non-empty ISO string

        for task in created_tasks:
            task.cancel()
            try:
                await task
            except _asyncio.CancelledError:
                pass


    async def test_fallback_to_last_good_topics_when_ai_fails(self, sample_groups, monkeypatch):
        """When both providers fail, return last-good topics instead of empty."""
        monkeypatch.setattr("app.ai_search._topics_cache", {"topics": [], "ts": 0, "generated_at": ""})
        monkeypatch.setattr("app.ai_search._search_cache", {
            "dólar": {"summary": "cached", "relevant_group_ids": ["abc1234567"], "has_results": True},
        })

        fallback_topics = [
            {"label": "Dólar", "emoji": "💵"},
            {"label": "Inflación", "emoji": "📈"},
        ]
        monkeypatch.setattr("app.ai_search._last_good_topics", {
            "topics": fallback_topics,
            "ai_provider": "Gemini",
            "generated_at": "2026-04-13T10:00:00+00:00",
        })

        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        _mock_ai_store(monkeypatch)

        async def _fail(*a, **kw):
            raise RuntimeError("Gemini 503")

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = _fail
        monkeypatch.setattr("app.ai_search._gemini_client", mock_client)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setattr("app.ai_search._groq_client", None)

        result = await ai_topics(sample_groups)

        assert result["topics"] == fallback_topics
        assert result["ai_available"] is True
        assert result["fallback"] is True
        assert result["cached"] is True
        assert result["ai_provider"] == "Gemini"
        assert result["generated_at"] == "2026-04-13T10:00:00+00:00"
        assert "Dólar" in result["search_cached"]

    async def test_no_fallback_returns_empty_when_no_last_good(self, sample_groups, monkeypatch):
        """When both providers fail and there's no last-good cache, return empty."""
        monkeypatch.setattr("app.ai_search._topics_cache", {"topics": [], "ts": 0, "generated_at": ""})
        monkeypatch.setattr("app.ai_search._search_cache", {})
        monkeypatch.setattr("app.ai_search._last_good_topics", {
            "topics": [], "ai_provider": "", "generated_at": "",
        })

        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        _mock_ai_store(monkeypatch)

        async def _fail(*a, **kw):
            raise RuntimeError("Gemini 503")

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = _fail
        monkeypatch.setattr("app.ai_search._gemini_client", mock_client)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setattr("app.ai_search._groq_client", None)

        result = await ai_topics(sample_groups)

        assert result["topics"] == []
        assert result["ai_available"] is False

    async def test_successful_generation_saves_last_good(self, sample_groups, monkeypatch):
        """Successful topic generation with enough items should update _last_good_topics."""
        monkeypatch.setattr("app.ai_search._topics_cache", {"topics": [], "ts": 0, "generated_at": ""})
        monkeypatch.setattr("app.ai_search._search_cache", {})
        monkeypatch.setattr("app.ai_search._last_good_topics", {
            "topics": [], "ai_provider": "", "generated_at": "",
        })
        monkeypatch.setattr("app.ai_search.save_last_good_topics", lambda *a, **kw: None)

        topics_json = (
            '{"topics":['
            '{"label":"Dólar","emoji":"💵"},'
            '{"label":"Inflación","emoji":"📈"},'
            '{"label":"Elecciones","emoji":"🗳️"},'
            '{"label":"Energía","emoji":"⚡"}'
            ']}'
        )
        _setup_ai_mock(monkeypatch, topics_json)

        created_tasks = []
        original_create_task = asyncio.create_task
        def _spy(coro):
            task = original_create_task(coro)
            created_tasks.append(task)
            return task
        monkeypatch.setattr("app.ai_search.asyncio.create_task", _spy)

        import app.ai_search as mod
        result = await ai_topics(sample_groups)

        assert result["ai_available"] is True
        assert len(result["topics"]) == 4
        assert mod._last_good_topics["topics"] == result["topics"]
        assert mod._last_good_topics["ai_provider"] == "Gemini"
        assert mod._last_good_topics["generated_at"] == result["generated_at"]

        for task in created_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_degraded_run_does_not_overwrite_last_good(self, sample_groups, monkeypatch):
        """If AI returns fewer than MIN_LAST_GOOD_TOPICS, keep the previous
        last-good cache so a single bad run doesn't lock in a degraded fallback."""
        monkeypatch.setattr("app.ai_search._topics_cache", {"topics": [], "ts": 0, "generated_at": ""})
        monkeypatch.setattr("app.ai_search._search_cache", {})

        previous_good = [
            {"label": "Dólar", "emoji": "💵"},
            {"label": "Inflación", "emoji": "📈"},
            {"label": "Elecciones", "emoji": "🗳️"},
            {"label": "Energía", "emoji": "⚡"},
        ]
        monkeypatch.setattr("app.ai_search._last_good_topics", {
            "topics": list(previous_good),
            "ai_provider": "Gemini",
            "generated_at": "2026-04-13T10:00:00+00:00",
        })

        saved_calls = []
        monkeypatch.setattr(
            "app.ai_search.save_last_good_topics",
            lambda topics, provider, generated_at: saved_calls.append(topics),
        )

        degraded_json = '{"topics":[{"label":"Solo","emoji":"1️⃣"},{"label":"Dos","emoji":"2️⃣"}]}'
        _setup_ai_mock(monkeypatch, degraded_json)

        created_tasks = []
        original_create_task = asyncio.create_task
        def _spy(coro):
            task = original_create_task(coro)
            created_tasks.append(task)
            return task
        monkeypatch.setattr("app.ai_search.asyncio.create_task", _spy)

        import app.ai_search as mod
        result = await ai_topics(sample_groups)

        assert len(result["topics"]) == 2
        assert mod._last_good_topics["topics"] == previous_good
        assert mod._last_good_topics["ai_provider"] == "Gemini"
        assert mod._last_good_topics["generated_at"] == "2026-04-13T10:00:00+00:00"
        assert saved_calls == []

        for task in created_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class TestRestoreLastGoodTopics:
    def test_restores_from_db(self, monkeypatch):
        saved = {
            "topics": [{"label": "Test", "emoji": "🧪"}],
            "ai_provider": "Gemini",
            "generated_at": "2026-04-13T12:00:00+00:00",
        }
        monkeypatch.setattr("app.ai_search.load_last_good_topics", lambda: saved)
        monkeypatch.setattr("app.ai_search._last_good_topics", {
            "topics": [], "ai_provider": "", "generated_at": "",
        })

        import app.ai_search as mod
        restore_last_good_topics()

        assert mod._last_good_topics["topics"] == saved["topics"]
        assert mod._last_good_topics["ai_provider"] == "Gemini"

    def test_noop_when_db_empty(self, monkeypatch):
        monkeypatch.setattr("app.ai_search.load_last_good_topics", lambda: None)
        monkeypatch.setattr("app.ai_search._last_good_topics", {
            "topics": [], "ai_provider": "", "generated_at": "",
        })

        import app.ai_search as mod
        restore_last_good_topics()

        assert mod._last_good_topics["topics"] == []


class TestAiWeeklySummary:
    async def test_returns_unavailable_without_api_key(self, sample_groups, monkeypatch):
        _no_ai(monkeypatch)
        result = await ai_weekly_summary(sample_groups, "2026-03-17", "2026-03-23")
        assert result["ai_available"] is False
        assert result["themes"] == []

    async def test_returns_empty_for_no_groups(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        monkeypatch.setattr("app.ai_search._gemini_client", MagicMock())
        result = await ai_weekly_summary([], "2026-03-17", "2026-03-23")
        assert result["ai_available"] is True
        assert result["themes"] == []
        assert result["week_start"] == "2026-03-17"
        assert result["week_end"] == "2026-03-23"

    async def test_cache_hit(self, sample_groups, monkeypatch):
        import time
        monkeypatch.setattr("app.ai_search._weekly_cache", {
            "data": {
                "themes": [{"label": "Test", "emoji": "🧪", "summary": "Test", "group_ids": [], "image": "", "sources": []}],
                "ai_available": True,
                "week_start": "2026-03-17",
                "week_end": "2026-03-23",
                "generated_at": "2026-03-23T09:15:00+00:00",
            },
            "ts": time.time(),
            "week_key": "2026-03-17_2026-03-23",
        })
        result = await ai_weekly_summary(sample_groups, "2026-03-17", "2026-03-23")
        assert result["cached"] is True
        assert len(result["themes"]) == 1
        assert result["generated_at"] == "2026-03-23T09:15:00+00:00"

    async def test_cache_hit_backfills_generated_at(self, sample_groups, monkeypatch):
        import time
        cache_ts = time.time() - 100
        monkeypatch.setattr("app.ai_search._weekly_cache", {
            "data": {
                "themes": [{"label": "Old", "emoji": "📅", "summary": "Old", "group_ids": [], "image": "", "sources": []}],
                "ai_available": True,
                "week_start": "2026-03-17",
                "week_end": "2026-03-23",
            },
            "ts": cache_ts,
            "week_key": "2026-03-17_2026-03-23",
        })
        result = await ai_weekly_summary(sample_groups, "2026-03-17", "2026-03-23")
        assert result["cached"] is True
        assert "generated_at" in result

    async def test_cache_miss_different_week(self, sample_groups, monkeypatch):
        import time
        monkeypatch.setattr("app.ai_search._weekly_cache", {
            "data": {
                "themes": [{"label": "Old", "emoji": "📅", "summary": "Old", "group_ids": [], "image": "", "sources": []}],
                "ai_available": True,
                "week_start": "2026-03-10",
                "week_end": "2026-03-16",
            },
            "ts": time.time(),
            "week_key": "2026-03-10_2026-03-16",
        })
        _no_ai(monkeypatch)
        result = await ai_weekly_summary(sample_groups, "2026-03-17", "2026-03-23")
        assert result["ai_available"] is False


class TestAiTopStory:
    async def test_returns_unavailable_without_api_key(self, sample_groups, monkeypatch):
        _no_ai(monkeypatch)
        result = await ai_top_story(sample_groups, "2026-03-24")
        assert result["ai_available"] is False
        assert result["story"] is None

    async def test_returns_none_story_for_no_groups(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        monkeypatch.setattr("app.ai_search._gemini_client", MagicMock())
        result = await ai_top_story([], "2026-03-24")
        assert result["ai_available"] is True
        assert result["story"] is None
        assert result["date"] == "2026-03-24"

    async def test_cache_hit(self, sample_groups, monkeypatch):
        import time
        top = sample_groups[0]
        monkeypatch.setattr("app.ai_search._topstory_cache", {
            "data": {
                "ai_available": True,
                "story": {
                    "title": "Test editorial", "emoji": "🔥",
                    "summary": "Resumen test", "key_points": ["Punto 1"],
                    "original_title": top.representative_title,
                    "image": "", "sources": ["Clarín"], "articles": [],
                    "source_count": 2, "category": "portada",
                    "published": None, "group_id": top.group_id,
                },
                "date": "2026-03-24",
                "generated_at": "2026-03-24T10:00:00+00:00",
            },
            "ts": time.time(),
            "cache_key": "2026-03-24",
        })
        result = await ai_top_story(sample_groups, "2026-03-24")
        assert result["cached"] is True
        assert result["story"]["title"] == "Test editorial"
        assert result["generated_at"] == "2026-03-24T10:00:00+00:00"

    async def test_cache_hit_backfills_generated_at(self, sample_groups, monkeypatch):
        import time
        cache_ts = time.time() - 100
        top = sample_groups[0]
        monkeypatch.setattr("app.ai_search._topstory_cache", {
            "data": {
                "ai_available": True,
                "story": {
                    "title": "Test editorial", "emoji": "🔥",
                    "summary": "Resumen test", "key_points": ["Punto 1"],
                    "original_title": top.representative_title,
                    "image": "", "sources": ["Clarín"], "articles": [],
                    "source_count": 2, "category": "portada",
                    "published": None, "group_id": top.group_id,
                },
                "date": "2026-03-24",
            },
            "ts": cache_ts,
            "cache_key": "2026-03-24",
        })
        result = await ai_top_story(sample_groups, "2026-03-24")
        assert result["cached"] is True
        assert "generated_at" in result

    async def test_cache_miss_different_day(self, sample_groups, monkeypatch):
        import time
        top = sample_groups[0]
        monkeypatch.setattr("app.ai_search._topstory_cache", {
            "data": {
                "ai_available": True,
                "story": {"title": "Yesterday", "emoji": "📰"},
                "date": "2026-03-23",
            },
            "ts": time.time(),
            "cache_key": "2026-03-23",
        })
        _no_ai(monkeypatch)
        result = await ai_top_story(sample_groups, "2026-03-24")
        assert result["ai_available"] is False


def _setup_ai_mock(monkeypatch, search_response=None):
    """Configure a fake Gemini that returns a valid search JSON."""
    _mock_ai_store(monkeypatch)
    monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr("app.ai_search._groq_client", None)

    if search_response is None:
        search_response = '{"summary":"Resumen","relevant_group_ids":["abc1234567"],"has_results":true}'

    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 100
    mock_usage.candidates_token_count = 50
    mock_response = MagicMock()
    mock_response.text = search_response
    mock_response.usage_metadata = mock_usage

    async def _fast(*a, **kw):
        return mock_response

    mock_client = MagicMock()
    mock_client.aio.models.generate_content = _fast
    monkeypatch.setattr("app.ai_search._gemini_client", mock_client)
    return mock_client


class TestPrefetchTopicSearches:
    async def test_prefetch_caches_all_topics(self, sample_groups, monkeypatch):
        topics = [
            {"label": "Dólar y mercados", "emoji": "💵"},
            {"label": "Inflación marzo", "emoji": "📈"},
            {"label": "Superclásico", "emoji": "⚽"},
        ]
        monkeypatch.setattr("app.ai_search._topics_cache", {"topics": topics, "ts": 0})
        monkeypatch.setattr("app.ai_search._search_cache", {})
        monkeypatch.setattr("app.ai_search._PREFETCH_CONCURRENCY", 10)
        _setup_ai_mock(monkeypatch)

        import app.ai_search as mod
        await _prefetch_topic_searches(topics, sample_groups)

        assert "dólar y mercados" in mod._search_cache
        assert "inflación marzo" in mod._search_cache
        assert "superclásico" in mod._search_cache

    async def test_prefetch_continues_on_failure(self, sample_groups, monkeypatch):
        topics = [
            {"label": "Tema OK 1", "emoji": "✅"},
            {"label": "Tema FAIL", "emoji": "❌"},
            {"label": "Tema OK 2", "emoji": "✅"},
        ]
        monkeypatch.setattr("app.ai_search._topics_cache", {"topics": topics, "ts": 0})
        monkeypatch.setattr("app.ai_search._search_cache", {})
        monkeypatch.setattr("app.ai_search._PREFETCH_CONCURRENCY", 10)

        call_count = 0
        original_search = ai_news_search.__wrapped__ if hasattr(ai_news_search, "__wrapped__") else None

        async def _mock_search(query, groups, **kwargs):
            nonlocal call_count
            call_count += 1
            if query.strip().lower() == "tema fail":
                raise RuntimeError("Simulated failure")
            return {
                "summary": "ok", "relevant_group_ids": [], "has_results": True,
                "ai_available": True, "ai_provider": "Mock",
            }

        monkeypatch.setattr("app.ai_search.ai_news_search", _mock_search)

        await _prefetch_topic_searches(topics, sample_groups)

        assert call_count == 3

    async def test_prefetch_skips_empty_labels(self, sample_groups, monkeypatch):
        topics = [
            {"label": "Tema real", "emoji": "✅"},
            {"label": "", "emoji": "❓"},
            {"emoji": "🚫"},
        ]
        monkeypatch.setattr("app.ai_search._topics_cache", {"topics": topics, "ts": 0})
        monkeypatch.setattr("app.ai_search._search_cache", {})
        monkeypatch.setattr("app.ai_search._PREFETCH_CONCURRENCY", 10)

        calls = []
        async def _mock_search(query, groups, **kwargs):
            calls.append(query)
            return {
                "summary": "ok", "relevant_group_ids": [], "has_results": True,
                "ai_available": True, "ai_provider": "Mock",
            }

        monkeypatch.setattr("app.ai_search.ai_news_search", _mock_search)

        await _prefetch_topic_searches(topics, sample_groups)

        assert calls == ["Tema real"]

    async def test_prefetch_skipped_during_quiet_hours(self, sample_groups, monkeypatch):
        topics = [{"label": "Test topic", "emoji": "🧪"}]
        monkeypatch.setattr("app.ai_search._topics_cache", {"topics": topics, "ts": 0})
        monkeypatch.setattr("app.ai_search._search_cache", {})
        monkeypatch.setattr("app.ai_search._PREFETCH_CONCURRENCY", 10)
        monkeypatch.setattr("app.ai_search.is_in_quiet_hours", lambda et: True)

        calls = []
        async def _mock_search(query, groups, **kwargs):
            calls.append(query)
            return {"summary": "ok", "relevant_group_ids": [], "has_results": True,
                    "ai_available": True, "ai_provider": "Mock"}

        monkeypatch.setattr("app.ai_search.ai_news_search", _mock_search)

        await _prefetch_topic_searches(topics, sample_groups)

        assert calls == []

    async def test_topics_launches_prefetch_task(self, sample_groups, monkeypatch):
        import time
        monkeypatch.setattr("app.ai_search._topics_cache", {"topics": [], "ts": 0, "generated_at": ""})
        monkeypatch.setattr("app.ai_search._search_cache", {})

        topics_json = '{"topics":[{"label":"Dólar","emoji":"💵"},{"label":"Inflación","emoji":"📈"}]}'
        _setup_ai_mock(monkeypatch, topics_json)

        created_tasks = []
        original_create_task = asyncio.create_task

        def _spy_create_task(coro):
            task = original_create_task(coro)
            created_tasks.append(task)
            return task

        monkeypatch.setattr("app.ai_search.asyncio.create_task", _spy_create_task)

        result = await ai_topics(sample_groups)

        assert result["ai_available"] is True
        assert len(result["topics"]) == 2
        assert len(created_tasks) == 1

        for task in created_tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


# ── Ollama provider ──────────────────────────────────────────────────────


def _mock_ollama_response(content: str, prompt_tokens: int = 120, eval_tokens: int = 40):
    """Build a MagicMock httpx.Response that _call_ollama can consume."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "message": {"role": "assistant", "content": content},
        "prompt_eval_count": prompt_tokens,
        "eval_count": eval_tokens,
    }
    return resp


class TestCallOllama:
    async def test_successful_call(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake:11434")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            return_value=_mock_ollama_response('{"answer":"ok"}', 100, 25),
        )
        monkeypatch.setattr("app.ai_search._ollama_client", mock_client)

        text, in_tok, out_tok = await _call_ollama("hola")
        assert text == '{"answer":"ok"}'
        assert in_tok == 100
        assert out_tok == 25
        mock_client.post.assert_awaited_once()
        call_args = mock_client.post.call_args
        assert call_args.args[0] == "/api/chat"
        body = call_args.kwargs["json"]
        assert body["stream"] is False
        assert body["format"] == "json"
        assert body["messages"][-1]["content"] == "hola"

    async def test_raises_when_not_configured(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        monkeypatch.setattr("app.ai_search._ollama_client", None)
        with pytest.raises(RuntimeError, match="Ollama client not available"):
            await _call_ollama("hola")

    async def test_raises_on_http_error(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake:11434")
        error_resp = MagicMock()
        error_resp.status_code = 500
        error_resp.text = "internal error"
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=error_resp)
        monkeypatch.setattr("app.ai_search._ollama_client", mock_client)
        with pytest.raises(OllamaCallError, match="HTTP 500") as exc_info:
            await _call_ollama("hola")
        assert exc_info.value.error_type == "HTTPStatusError"
        assert exc_info.value.phase == "response"
        assert exc_info.value.http_status == 500

    async def test_raises_on_empty_response(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake:11434")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_mock_ollama_response("", 10, 0))
        monkeypatch.setattr("app.ai_search._ollama_client", mock_client)
        with pytest.raises(OllamaCallError, match="empty response") as exc_info:
            await _call_ollama("hola")
        assert exc_info.value.error_type == "EmptyResponse"
        assert exc_info.value.phase == "response"

    async def test_read_timeout_maps_to_read_phase(self, monkeypatch):
        """A httpx.ReadTimeout means the request reached Ollama but the model
        didn't respond — surfaced as phase=read with request_sent_at set."""
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake:11434")

        async def _raise_read_timeout(*a, **kw):
            raise httpx.ReadTimeout("read timed out")

        mock_client = MagicMock()
        mock_client.post = _raise_read_timeout
        monkeypatch.setattr("app.ai_search._ollama_client", mock_client)
        with pytest.raises(OllamaCallError, match="read timeout") as exc_info:
            await _call_ollama("hola", timeout=0.05)
        assert exc_info.value.error_type == "ReadTimeout"
        assert exc_info.value.phase == "read"
        assert exc_info.value.request_sent_at is not None

    async def test_connect_timeout_maps_to_connect_phase(self, monkeypatch):
        """A httpx.ConnectTimeout means the request never reached Ollama —
        surfaced as phase=connect with no request_sent_at timestamp."""
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake:11434")

        async def _raise_connect_timeout(*a, **kw):
            raise httpx.ConnectTimeout("could not connect")

        mock_client = MagicMock()
        mock_client.post = _raise_connect_timeout
        monkeypatch.setattr("app.ai_search._ollama_client", mock_client)
        with pytest.raises(OllamaCallError, match="connect timeout") as exc_info:
            await _call_ollama("hola")
        assert exc_info.value.error_type == "ConnectTimeout"
        assert exc_info.value.phase == "connect"

    async def test_connect_error_maps_to_connect_phase(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake:11434")

        async def _raise_connect_error(*a, **kw):
            raise httpx.ConnectError("connection refused")

        mock_client = MagicMock()
        mock_client.post = _raise_connect_error
        monkeypatch.setattr("app.ai_search._ollama_client", mock_client)
        with pytest.raises(OllamaCallError, match="connect error") as exc_info:
            await _call_ollama("hola")
        assert exc_info.value.error_type == "ConnectError"
        assert exc_info.value.phase == "connect"

    async def test_truncates_long_prompt(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake:11434")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            return_value=_mock_ollama_response('{"ok":true}'),
        )
        monkeypatch.setattr("app.ai_search._ollama_client", mock_client)

        huge = "x" * (OLLAMA_MAX_PROMPT_CHARS + 500)
        await _call_ollama(huge)
        sent = mock_client.post.call_args.kwargs["json"]["messages"][-1]["content"]
        assert len(sent) < len(huge)
        assert "[contexto truncado" in sent


class TestProviderChain:
    def test_single_provider_list(self):
        assert _provider_chain(["gemini"]) == ["gemini"]
        assert _provider_chain(["groq"]) == ["groq"]
        assert _provider_chain(["ollama"]) == ["ollama"]

    def test_two_provider_chain(self):
        assert _provider_chain(["gemini", "groq"]) == ["gemini", "groq"]
        assert _provider_chain(["ollama", "gemini"]) == ["ollama", "gemini"]
        assert _provider_chain(["groq", "ollama"]) == ["groq", "ollama"]

    def test_full_chain(self):
        assert _provider_chain(["ollama", "gemini", "groq"]) == [
            "ollama", "gemini", "groq",
        ]

    def test_dedupe_preserves_order(self):
        assert _provider_chain(["gemini", "gemini", "groq"]) == ["gemini", "groq"]

    def test_unknown_providers_filtered(self):
        assert _provider_chain(["bogus", "groq"]) == ["groq"]

    def test_empty_defaults_to_gemini_groq(self):
        assert _provider_chain([]) == ["gemini", "groq"]
        assert _provider_chain(["bogus", "nonexistent"]) == ["gemini", "groq"]


class TestCallAiOllamaRouting:
    """Routing tests that cover the new Ollama-aware fallback modes."""

    async def test_ollama_only_mode(self, monkeypatch):
        _mock_ai_store(monkeypatch, provider="ollama")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake:11434")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setattr("app.ai_search._gemini_client", None)
        monkeypatch.setattr("app.ai_search._groq_client", None)

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            return_value=_mock_ollama_response('{"result":"from ollama"}'),
        )
        monkeypatch.setattr("app.ai_search._ollama_client", mock_client)

        text, provider = await _call_ai("test prompt", event_type="topics")
        assert text == '{"result":"from ollama"}'
        assert provider == "Ollama"

    async def test_gemini_fallback_ollama_falls_back(self, monkeypatch):
        _mock_ai_store(monkeypatch, provider="gemini_fallback_ollama")
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake:11434")

        async def _gemini_fail(*a, **kw):
            raise RuntimeError("Gemini down")

        mock_gemini = MagicMock()
        mock_gemini.aio.models.generate_content = _gemini_fail
        monkeypatch.setattr("app.ai_search._gemini_client", mock_gemini)

        mock_ollama = AsyncMock()
        mock_ollama.post = AsyncMock(
            return_value=_mock_ollama_response('{"result":"from ollama"}'),
        )
        monkeypatch.setattr("app.ai_search._ollama_client", mock_ollama)

        text, provider = await _call_ai("test prompt", event_type="topics")
        assert text == '{"result":"from ollama"}'
        assert provider == "Ollama"

    async def test_ollama_fallback_gemini_uses_ollama_first(self, monkeypatch):
        _mock_ai_store(monkeypatch, provider="ollama_fallback_gemini")
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake:11434")

        mock_ollama = AsyncMock()
        mock_ollama.post = AsyncMock(
            return_value=_mock_ollama_response('{"result":"ollama primary"}'),
        )
        monkeypatch.setattr("app.ai_search._ollama_client", mock_ollama)

        mock_gemini = MagicMock()
        monkeypatch.setattr("app.ai_search._gemini_client", mock_gemini)

        text, provider = await _call_ai("test prompt", event_type="topics")
        assert text == '{"result":"ollama primary"}'
        assert provider == "Ollama"
        mock_gemini.aio.models.generate_content.assert_not_called()

    async def test_ollama_unavailable_uses_fallback(self, monkeypatch):
        """mode=ollama_fallback_groq without OLLAMA_BASE_URL should go straight to Groq."""
        _mock_ai_store(monkeypatch, provider="ollama_fallback_groq")
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        monkeypatch.setattr("app.ai_search._ollama_client", None)
        monkeypatch.setenv("GROQ_API_KEY", "fake")

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 50
        mock_usage.completion_tokens = 25
        mock_choice = MagicMock()
        mock_choice.message.content = '{"result":"groq only"}'
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage = mock_usage
        mock_groq = AsyncMock()
        mock_groq.chat.completions.create = AsyncMock(return_value=mock_resp)
        monkeypatch.setattr("app.ai_search._groq_client", mock_groq)

        text, provider = await _call_ai("test prompt", event_type="topics")
        assert text == '{"result":"groq only"}'
        assert provider == "Groq"

    async def test_no_providers_available_raises(self, monkeypatch):
        _mock_ai_store(monkeypatch, provider="gemini_fallback_ollama")
        _no_ai(monkeypatch)
        with pytest.raises(RuntimeError, match="No AI provider"):
            await _call_ai("test prompt", event_type="topics")

    async def test_both_fail_logs_both_errors(self, monkeypatch):
        """Regression: when Gemini and Ollama both fail, both must be logged
        to ai_usage_log — otherwise the admin monitor shows stale status.
        """
        _mock_ai_store(monkeypatch, provider="gemini_fallback_ollama")
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://fake:11434")

        calls: list[dict] = []
        monkeypatch.setattr(
            "app.ai_search.log_ai_usage",
            lambda **kw: calls.append(kw),
        )

        async def _gemini_fail(*a, **kw):
            raise RuntimeError("Gemini down")

        mock_gemini = MagicMock()
        mock_gemini.aio.models.generate_content = _gemini_fail
        monkeypatch.setattr("app.ai_search._gemini_client", mock_gemini)

        mock_ollama = AsyncMock()
        mock_ollama.post = AsyncMock(side_effect=RuntimeError("Ollama 503"))
        monkeypatch.setattr("app.ai_search._ollama_client", mock_ollama)

        with pytest.raises(RuntimeError, match="Ollama 503"):
            await _call_ai("test prompt", event_type="topics")

        logged = [(c["provider"], c["success"]) for c in calls]
        assert ("gemini", False) in logged
        assert ("ollama", False) in logged


def _stub_limits(monkeypatch, per_provider: dict[str, dict[str, int | None]]):
    """Stub get_provider_limit to return fixed limits per provider."""
    def _fake(provider, _model):
        return per_provider.get(provider, {"rpm": None, "tpm": None, "rpd": None, "tpd": None})
    monkeypatch.setattr("app.ai_search.get_provider_limit", _fake)


def _stub_usage(monkeypatch, per_provider: dict[str, dict[str, int]]):
    """Stub query_provider_usage to return fixed usage per provider."""
    def _fake(provider):
        return per_provider.get(provider, {"rpm_used": 0, "tpm_used": 0, "rpd_used": 0, "tpd_used": 0})
    monkeypatch.setattr("app.ai_search.query_provider_usage", _fake)


class TestQuotaBlocked:
    def test_no_limits_returns_none(self, monkeypatch):
        _stub_limits(monkeypatch, {})
        _stub_usage(monkeypatch, {})
        assert _quota_blocked("gemini") is None

    def test_ollama_never_blocked(self, monkeypatch):
        _stub_limits(monkeypatch, {"ollama": {"rpm": 1, "tpm": None, "rpd": None, "tpd": None}})
        _stub_usage(monkeypatch, {"ollama": {"rpm_used": 999, "tpm_used": 0, "rpd_used": 0, "tpd_used": 0}})
        assert _quota_blocked("ollama") is None

    def test_rpm_exceeded(self, monkeypatch):
        _stub_limits(monkeypatch, {"gemini": {"rpm": 10, "tpm": None, "rpd": None, "tpd": None}})
        _stub_usage(monkeypatch, {"gemini": {"rpm_used": 10, "tpm_used": 0, "rpd_used": 0, "tpd_used": 0}})
        assert _quota_blocked("gemini") == "rpm"

    def test_tpm_exceeded(self, monkeypatch):
        _stub_limits(monkeypatch, {"gemini": {"rpm": None, "tpm": 1000, "rpd": None, "tpd": None}})
        _stub_usage(monkeypatch, {"gemini": {"rpm_used": 0, "tpm_used": 2000, "rpd_used": 0, "tpd_used": 0}})
        assert _quota_blocked("gemini") == "tpm"

    def test_rpd_exceeded(self, monkeypatch):
        _stub_limits(monkeypatch, {"gemini": {"rpm": None, "tpm": None, "rpd": 100, "tpd": None}})
        _stub_usage(monkeypatch, {"gemini": {"rpm_used": 0, "tpm_used": 0, "rpd_used": 100, "tpd_used": 0}})
        assert _quota_blocked("gemini") == "rpd"

    def test_tpd_exceeded(self, monkeypatch):
        _stub_limits(monkeypatch, {"gemini": {"rpm": None, "tpm": None, "rpd": None, "tpd": 10_000}})
        _stub_usage(monkeypatch, {"gemini": {"rpm_used": 0, "tpm_used": 0, "rpd_used": 0, "tpd_used": 10_001}})
        assert _quota_blocked("gemini") == "tpd"

    def test_under_limit(self, monkeypatch):
        _stub_limits(monkeypatch, {"gemini": {"rpm": 10, "tpm": 1000, "rpd": 100, "tpd": 10_000}})
        _stub_usage(monkeypatch, {"gemini": {"rpm_used": 5, "tpm_used": 500, "rpd_used": 50, "tpd_used": 5000}})
        assert _quota_blocked("gemini") is None

    def test_unknown_provider_returns_none(self, monkeypatch):
        _stub_limits(monkeypatch, {})
        _stub_usage(monkeypatch, {})
        assert _quota_blocked("totally-fake") is None

    def test_store_error_does_not_block(self, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("db down")
        monkeypatch.setattr("app.ai_search.get_provider_limit", _boom)
        assert _quota_blocked("gemini") is None


def _mock_ollama_success(monkeypatch):
    from unittest.mock import AsyncMock
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(
        return_value=_mock_ollama_response('{"result":"ollama ok"}'),
    )
    monkeypatch.setattr("app.ai_search._ollama_client", mock_client)
    return mock_client


class TestQuotaGuardChain:
    """End-to-end behaviour of _run_provider_chain with the precheck active."""

    async def test_skips_primary_when_over_quota(self, monkeypatch):
        _mock_ai_store(monkeypatch, provider="gemini_fallback_groq")
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("GROQ_API_KEY", "fake")
        _stub_limits(monkeypatch, {"gemini": {"rpm": 10, "tpm": None, "rpd": None, "tpd": None}})
        _stub_usage(monkeypatch, {"gemini": {"rpm_used": 10, "tpm_used": 0, "rpd_used": 0, "tpd_used": 0}})

        calls: list[dict] = []
        monkeypatch.setattr(
            "app.ai_search.log_ai_usage",
            lambda **kw: calls.append(kw),
        )

        mock_gemini = MagicMock()
        mock_gemini.aio.models.generate_content = AsyncMock(
            return_value=MagicMock(text="should not be called", usage_metadata=MagicMock()),
        )
        monkeypatch.setattr("app.ai_search._gemini_client", mock_gemini)

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 5
        mock_choice = MagicMock()
        mock_choice.message.content = '{"from":"groq"}'
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage = mock_usage
        mock_groq = AsyncMock()
        mock_groq.chat.completions.create = AsyncMock(return_value=mock_resp)
        monkeypatch.setattr("app.ai_search._groq_client", mock_groq)

        text, provider = await _call_ai("test prompt", event_type="topics")
        assert text == '{"from":"groq"}'
        assert provider == "Groq"

        mock_gemini.aio.models.generate_content.assert_not_called()
        gemini_calls = [c for c in calls if c["provider"] == "gemini"]
        assert len(gemini_calls) == 1
        assert gemini_calls[0]["success"] is False
        assert gemini_calls[0]["error_type"] == "QuotaExhausted:rpm"
        assert gemini_calls[0]["error_phase"] == "precheck"

    async def test_last_in_chain_called_even_if_blocked(self, monkeypatch):
        """The last provider gets a real shot even if the precheck trips."""
        _mock_ai_store(monkeypatch, provider="gemini_fallback_groq")
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("GROQ_API_KEY", "fake")
        _stub_limits(monkeypatch, {
            "gemini": {"rpm": 10, "tpm": None, "rpd": None, "tpd": None},
            "groq": {"rpd": 5, "rpm": None, "tpm": None, "tpd": None},
        })
        _stub_usage(monkeypatch, {
            "gemini": {"rpm_used": 10, "tpm_used": 0, "rpd_used": 0, "tpd_used": 0},
            "groq": {"rpm_used": 0, "tpm_used": 0, "rpd_used": 999, "tpd_used": 0},
        })

        calls: list[dict] = []
        monkeypatch.setattr("app.ai_search.log_ai_usage", lambda **kw: calls.append(kw))

        mock_gemini = MagicMock()
        monkeypatch.setattr("app.ai_search._gemini_client", mock_gemini)

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 5
        mock_choice = MagicMock()
        mock_choice.message.content = '{"from":"groq-last-resort"}'
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage = mock_usage
        mock_groq = AsyncMock()
        mock_groq.chat.completions.create = AsyncMock(return_value=mock_resp)
        monkeypatch.setattr("app.ai_search._groq_client", mock_groq)

        text, provider = await _call_ai("test prompt", event_type="topics")
        assert text == '{"from":"groq-last-resort"}'
        assert provider == "Groq"
        mock_groq.chat.completions.create.assert_called_once()

    async def test_single_provider_mode_still_tries_when_blocked(self, monkeypatch):
        """Mode=gemini (no fallback): the precheck treats it as the last link."""
        _mock_ai_store(monkeypatch, provider="gemini")
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        _stub_limits(monkeypatch, {"gemini": {"rpm": 1, "tpm": None, "rpd": None, "tpd": None}})
        _stub_usage(monkeypatch, {"gemini": {"rpm_used": 99, "tpm_used": 0, "rpd_used": 0, "tpd_used": 0}})

        monkeypatch.setattr("app.ai_search.log_ai_usage", lambda **kw: None)

        mock_response = MagicMock()
        mock_response.text = '{"ok":true}'
        mock_response.usage_metadata = MagicMock()
        mock_response.usage_metadata.prompt_token_count = 10
        mock_response.usage_metadata.candidates_token_count = 5

        mock_gemini = MagicMock()
        mock_gemini.aio.models.generate_content = AsyncMock(return_value=mock_response)
        monkeypatch.setattr("app.ai_search._gemini_client", mock_gemini)

        text, provider = await _call_ai("test prompt", event_type="topics")
        assert text == '{"ok":true}'
        assert provider == "Gemini"
        mock_gemini.aio.models.generate_content.assert_called_once()

    async def test_no_limits_configured_does_not_skip(self, monkeypatch):
        _mock_ai_store(monkeypatch, provider="gemini_fallback_groq")
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("GROQ_API_KEY", "fake")
        _stub_limits(monkeypatch, {})
        _stub_usage(monkeypatch, {})
        monkeypatch.setattr("app.ai_search.log_ai_usage", lambda **kw: None)

        mock_response = MagicMock()
        mock_response.text = '{"from":"gemini"}'
        mock_response.usage_metadata = MagicMock()
        mock_response.usage_metadata.prompt_token_count = 10
        mock_response.usage_metadata.candidates_token_count = 5

        mock_gemini = MagicMock()
        mock_gemini.aio.models.generate_content = AsyncMock(return_value=mock_response)
        monkeypatch.setattr("app.ai_search._gemini_client", mock_gemini)

        text, provider = await _call_ai("test prompt", event_type="topics")
        assert text == '{"from":"gemini"}'
        assert provider == "Gemini"


class TestQuotaExhaustedError:
    def test_carries_limit_name(self):
        exc = QuotaExhaustedError("cupo", limit_name="rpd")
        assert exc.limit_name == "rpd"
        assert str(exc) == "cupo"
