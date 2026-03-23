import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.gemini_search import (
    GEMINI_TIMEOUT,
    _build_context,
    _call_gemini,
    _clean_json_response,
    _parse_retry_seconds,
    gemini_search,
    gemini_topics,
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
        monkeypatch.setattr("app.gemini_search._rate_limit_until", 0)
        monkeypatch.setattr("app.gemini_search.GEMINI_TIMEOUT", 0.1)

        async def _hang(*args, **kwargs):
            await asyncio.sleep(300)

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = _hang

        with pytest.raises(RuntimeError, match="timed out"):
            await _call_gemini(mock_client, "test prompt")

    async def test_successful_call(self, monkeypatch):
        monkeypatch.setattr("app.gemini_search._rate_limit_until", 0)

        mock_response = MagicMock()
        mock_response.text = '{"answer": "ok"}'

        async def _fast(*args, **kwargs):
            return mock_response

        mock_client = MagicMock()
        mock_client.aio.models.generate_content = _fast

        result = await _call_gemini(mock_client, "test prompt")
        assert result == '{"answer": "ok"}'


class TestGeminiSearch:
    async def test_returns_unavailable_without_api_key(self, sample_groups, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setattr("app.gemini_search._client", None)
        result = await gemini_search("inflación", sample_groups)
        assert result["ai_available"] is False


class TestGeminiTopics:
    async def test_returns_unavailable_without_api_key(self, sample_groups, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setattr("app.gemini_search._client", None)
        result = await gemini_topics(sample_groups)
        assert result["ai_available"] is False
        assert result["topics"] == []
