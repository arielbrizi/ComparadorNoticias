import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai_search import (
    GEMINI_TIMEOUT,
    _build_context,
    _call_ai,
    _call_gemini,
    _call_groq,
    _clean_json_response,
    _last_good_topics,
    _prefetch_topic_searches,
    ai_top_story,
    _parse_retry_seconds,
    ai_news_search,
    ai_topics,
    ai_weekly_summary,
    restore_last_good_topics,
)


def _no_ai(monkeypatch):
    """Helper: disable both AI providers."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr("app.ai_search._gemini_client", None)
    monkeypatch.setattr("app.ai_search._groq_client", None)


def _mock_ai_store(monkeypatch, provider="gemini_fallback_groq"):
    """Stub out ai_store DB calls used by _call_ai / _call_ai_search."""
    monkeypatch.setattr(
        "app.ai_search.get_provider_config",
        lambda: {et: provider for et in
                 ("search", "search_prefetch", "topics", "weekly_summary", "top_story")},
    )
    monkeypatch.setattr("app.ai_search.log_ai_usage", lambda **kw: None)


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
        """Successful topic generation should update _last_good_topics."""
        monkeypatch.setattr("app.ai_search._topics_cache", {"topics": [], "ts": 0, "generated_at": ""})
        monkeypatch.setattr("app.ai_search._search_cache", {})
        monkeypatch.setattr("app.ai_search._last_good_topics", {
            "topics": [], "ai_provider": "", "generated_at": "",
        })
        monkeypatch.setattr("app.ai_search.save_last_good_topics", lambda *a, **kw: None)

        topics_json = '{"topics":[{"label":"Dólar","emoji":"💵"},{"label":"Inflación","emoji":"📈"}]}'
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
        assert len(result["topics"]) == 2
        assert mod._last_good_topics["topics"] == result["topics"]
        assert mod._last_good_topics["ai_provider"] == "Gemini"
        assert mod._last_good_topics["generated_at"] == result["generated_at"]

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
