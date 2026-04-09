"""Tests for the scheduled prefetch jobs (top story, weekly summary)."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import Article, ArticleGroup


def _make_groups():
    """Create minimal test groups for prefetch tests."""
    now = datetime(2026, 4, 8, 12, 0, tzinfo=timezone.utc)
    articles = [
        Article(
            id="a1", source="Clarín", source_color="#1a73e8",
            title="Noticia principal del día",
            summary="Resumen de la noticia principal.",
            link="https://example.com/1", category="portada", published=now,
        ),
        Article(
            id="a2", source="La Nación", source_color="#2d6a4f",
            title="Misma noticia desde otro medio",
            summary="Otro resumen de la misma noticia.",
            link="https://example.com/2", category="portada", published=now,
        ),
    ]
    return [
        ArticleGroup(
            group_id="grp001",
            representative_title=articles[0].title,
            category="portada",
            published=now,
            articles=articles,
        ),
    ]


class TestPrefetchTopStory:
    async def test_calls_ai_top_story(self, monkeypatch):
        from app import main

        groups = _make_groups()
        monkeypatch.setattr(main, "_groups", groups)
        monkeypatch.setattr(main, "_lock", asyncio.Lock())

        mock_result = {"ai_available": True, "story": {"title": "Test"}, "date": "2026-04-08"}
        mock_ai = AsyncMock(return_value=mock_result)
        monkeypatch.setattr("app.main.ai_top_story", mock_ai)
        monkeypatch.setattr("app.main.load_groups_from_db", lambda **kw: ([], []))

        await main.prefetch_top_story()

        mock_ai.assert_called_once()
        call_groups, call_today = mock_ai.call_args[0]
        assert len(call_groups) > 0
        assert len(call_today) == 10

    async def test_handles_failure_gracefully(self, monkeypatch):
        from app import main

        groups = _make_groups()
        monkeypatch.setattr(main, "_groups", groups)
        monkeypatch.setattr(main, "_lock", asyncio.Lock())

        mock_ai = AsyncMock(side_effect=RuntimeError("AI down"))
        monkeypatch.setattr("app.main.ai_top_story", mock_ai)
        monkeypatch.setattr("app.main.load_groups_from_db", lambda **kw: ([], []))

        await main.prefetch_top_story()

    async def test_skips_when_no_groups(self, monkeypatch):
        from app import main

        monkeypatch.setattr(main, "_groups", [])
        monkeypatch.setattr(main, "_lock", asyncio.Lock())

        mock_ai = AsyncMock()
        monkeypatch.setattr("app.main.ai_top_story", mock_ai)
        monkeypatch.setattr("app.main.load_groups_from_db", lambda **kw: ([], []))

        await main.prefetch_top_story()

        mock_ai.assert_not_called()


class TestPrefetchWeeklySummary:
    async def test_calls_ai_weekly_summary(self, monkeypatch):
        from app import main

        groups = _make_groups()
        monkeypatch.setattr("app.main.load_groups_from_db", lambda **kw: ([], groups))

        mock_result = {"themes": [{"label": "Test"}], "ai_available": True}
        mock_ai = AsyncMock(return_value=mock_result)
        monkeypatch.setattr("app.main.ai_weekly_summary", mock_ai)

        await main.prefetch_weekly_summary()

        mock_ai.assert_called_once()
        call_groups = mock_ai.call_args[0][0]
        assert len(call_groups) == 1

    async def test_handles_failure_gracefully(self, monkeypatch):
        from app import main

        groups = _make_groups()
        monkeypatch.setattr("app.main.load_groups_from_db", lambda **kw: ([], groups))

        mock_ai = AsyncMock(side_effect=RuntimeError("AI down"))
        monkeypatch.setattr("app.main.ai_weekly_summary", mock_ai)

        await main.prefetch_weekly_summary()

    async def test_skips_when_no_groups(self, monkeypatch):
        from app import main

        monkeypatch.setattr("app.main.load_groups_from_db", lambda **kw: ([], []))

        mock_ai = AsyncMock()
        monkeypatch.setattr("app.main.ai_weekly_summary", mock_ai)

        await main.prefetch_weekly_summary()

        mock_ai.assert_not_called()

    async def test_limits_groups_to_200(self, monkeypatch):
        from app import main

        big_groups = _make_groups() * 250
        monkeypatch.setattr("app.main.load_groups_from_db", lambda **kw: ([], big_groups))

        mock_result = {"themes": [], "ai_available": True}
        mock_ai = AsyncMock(return_value=mock_result)
        monkeypatch.setattr("app.main.ai_weekly_summary", mock_ai)

        await main.prefetch_weekly_summary()

        call_groups = mock_ai.call_args[0][0]
        assert len(call_groups) <= 200


class TestStartupPrefetch:
    async def test_waits_for_groups_then_runs(self, monkeypatch):
        from app import main

        groups = _make_groups()
        call_order = []

        async def _mock_top_story():
            call_order.append("top_story")

        async def _mock_weekly():
            call_order.append("weekly")

        monkeypatch.setattr(main, "_groups", groups)
        monkeypatch.setattr(main, "_lock", asyncio.Lock())
        monkeypatch.setattr("app.main.prefetch_top_story", _mock_top_story)
        monkeypatch.setattr("app.main.prefetch_weekly_summary", _mock_weekly)

        await main._startup_prefetch()

        assert call_order == ["top_story", "weekly"]
