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
    ai_top_story,
    _parse_retry_seconds,
    ai_news_search,
    ai_topics,
    ai_weekly_summary,
)


def _no_ai(monkeypatch):
    """Helper: disable both AI providers."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr("app.ai_search._gemini_client", None)
    monkeypatch.setattr("app.ai_search._groq_client", None)


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

        mock_response = MagicMock()
        mock_response.text = '{"answer": "ok"}'

        async def _fast(*args, **kwargs):
            return mock_response

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = _fast
        monkeypatch.setattr("app.ai_search._gemini_client", mock_client)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")

        result = await _call_gemini("test prompt")
        assert result == '{"answer": "ok"}'


class TestCallAiFallback:
    async def test_falls_back_to_groq_when_gemini_fails(self, monkeypatch):
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("GROQ_API_KEY", "fake")

        async def _gemini_fail(*a, **kw):
            raise RuntimeError("Gemini down")

        mock_gemini = MagicMock()
        mock_gemini.aio.models.generate_content = _gemini_fail
        monkeypatch.setattr("app.ai_search._gemini_client", mock_gemini)

        mock_choice = MagicMock()
        mock_choice.message.content = '{"result": "from groq"}'
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]

        mock_groq = AsyncMock()
        mock_groq.chat.completions.create = AsyncMock(return_value=mock_resp)
        monkeypatch.setattr("app.ai_search._groq_client", mock_groq)

        text, provider = await _call_ai("test prompt")
        assert text == '{"result": "from groq"}'
        assert provider == "Groq"

    async def test_uses_gemini_when_available(self, monkeypatch):
        monkeypatch.setattr("app.ai_search._rate_limit_until", 0)
        monkeypatch.setenv("GEMINI_API_KEY", "fake")

        mock_response = MagicMock()
        mock_response.text = '{"result": "from gemini"}'

        async def _fast(*a, **kw):
            return mock_response

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = _fast
        monkeypatch.setattr("app.ai_search._gemini_client", mock_client)

        text, provider = await _call_ai("test prompt")
        assert text == '{"result": "from gemini"}'
        assert provider == "Gemini"

    async def test_groq_only_when_no_gemini_key(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setattr("app.ai_search._gemini_client", None)
        monkeypatch.setenv("GROQ_API_KEY", "fake")

        mock_choice = MagicMock()
        mock_choice.message.content = '{"result": "groq only"}'
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]

        mock_groq = AsyncMock()
        mock_groq.chat.completions.create = AsyncMock(return_value=mock_resp)
        monkeypatch.setattr("app.ai_search._groq_client", mock_groq)

        text, provider = await _call_ai("test prompt")
        assert text == '{"result": "groq only"}'
        assert provider == "Groq"

    async def test_raises_when_no_provider(self, monkeypatch):
        _no_ai(monkeypatch)
        with pytest.raises(RuntimeError, match="No AI provider"):
            await _call_ai("test prompt")


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
            },
            "ts": time.time(),
            "week_key": "2026-03-17_2026-03-23",
        })
        result = await ai_weekly_summary(sample_groups, "2026-03-17", "2026-03-23")
        assert result["cached"] is True
        assert len(result["themes"]) == 1

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
            },
            "ts": time.time(),
            "cache_key": f"2026-03-24_{top.group_id}",
        })
        result = await ai_top_story(sample_groups, "2026-03-24")
        assert result["cached"] is True
        assert result["story"]["title"] == "Test editorial"

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
            "cache_key": f"2026-03-23_{top.group_id}",
        })
        _no_ai(monkeypatch)
        result = await ai_top_story(sample_groups, "2026-03-24")
        assert result["ai_available"] is False
