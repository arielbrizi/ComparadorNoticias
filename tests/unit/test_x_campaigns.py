"""Tests for app.x_campaigns — runners de cada campaña con mocks de x_client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app import x_campaigns, x_client, x_store


@pytest.fixture
def _init(temp_db):
    x_store.init_x_tables()
    x_store._campaign_cache_ts = 0
    x_store._tier_cache_ts = 0
    x_store.set_tier_config("basic")
    yield


def _fake_post_tweet(post_id: str = "999"):
    def _impl(text, media_ids=None):
        return x_client.PostResult(post_id=post_id, text=text, raw={})
    return _impl


class TestTopstoryRunner:
    def test_disabled_skipped(self, _init):
        result = x_campaigns.run_topstory_campaign({"title": "t", "group_id": "g1"})
        assert result.ok is False
        assert result.status == "skipped"

    def test_posts_when_enabled(self, _init):
        x_store.set_campaign_config("topstory", enabled=True)
        story = {"title": "Noticia del día", "summary": "Un resumen breve", "group_id": "abc"}
        with patch("app.x_campaigns.x_client.post_tweet", side_effect=_fake_post_tweet("42")) as mock_post:
            result = x_campaigns.run_topstory_campaign(story)
        assert result.ok is True
        assert result.post_ids == ["42"]
        body = mock_post.call_args.args[0]
        assert "Noticia del día" in body
        assert "?g=abc" in body
        logs = x_store.query_x_usage(limit=5)
        assert logs[0]["status"] == "ok"

    def test_no_story_logs_skipped(self, _init):
        x_store.set_campaign_config("topstory", enabled=True)
        result = x_campaigns.run_topstory_campaign({"title": ""})
        assert result.status == "skipped"

    def test_cap_reached_blocks_post(self, _init):
        x_store.set_tier_config("custom", daily_cap=1, monthly_cap=100, monthly_usd=0)
        x_store.set_campaign_config("topstory", enabled=True)
        x_store.log_x_post(campaign_key="topstory", status="ok", post_id="111")

        with patch("app.x_campaigns.x_client.post_tweet") as mock_post:
            result = x_campaigns.run_topstory_campaign({"title": "Otra", "group_id": "g"})
        mock_post.assert_not_called()
        assert result.status == "daily_cap_reached"


class TestTopicsRunner:
    def test_thread_when_enabled(self, _init):
        x_store.set_campaign_config(
            "topics",
            enabled=True,
            template={
                "text": "{topics_list}",
                "hashtags": "#test",
                "thread": True,
                "thread_max_posts": 5,
            },
        )
        data = {"topics": [
            {"label": "Dólar", "emoji": "💵"},
            {"label": "Inflación", "emoji": "📈"},
            {"label": "Milei", "emoji": "🇦🇷"},
        ]}
        responses = iter(["100", "101", "102", "103", "104"])
        def _impl(text, media_ids=None):
            return x_client.PostResult(post_id=next(responses), text=text, raw={})

        with patch("app.x_campaigns.x_client.post_thread") as mock_thread:
            mock_thread.return_value = [
                x_client.PostResult(post_id="100", text="a", raw={}),
                x_client.PostResult(post_id="101", text="b", raw={}),
                x_client.PostResult(post_id="102", text="c", raw={}),
            ]
            result = x_campaigns.run_topics_campaign(data)

        assert result.ok is True
        assert mock_thread.called
        # Usage log acumula posts_count = 3 para el thread.
        logs = x_store.query_x_usage(limit=5)
        assert logs[0]["posts_count"] == 3


class TestWeeklyRunner:
    def test_single_post_fallback(self, _init):
        x_store.set_campaign_config(
            "weekly",
            enabled=True,
            template={
                "text": "{week_start} a {week_end}: {summary}",
                "hashtags": "#test",
                "thread": False,
            },
        )
        weekly = {"themes": [{"label": "Tema 1", "summary": "Resumen corto"}]}
        with patch("app.x_campaigns.x_client.post_tweet", side_effect=_fake_post_tweet("w1")) as mock_post:
            result = x_campaigns.run_weekly_campaign(
                weekly, week_start="2026-04-20", week_end="2026-04-26",
            )
        assert result.ok is True
        assert "2026-04-20" in mock_post.call_args.args[0]


class TestCloudRunner:
    def test_skips_without_words(self, _init):
        x_store.set_campaign_config("cloud", enabled=True)
        result = x_campaigns.run_cloud_campaign([])
        assert result.status == "skipped"

    def test_image_upload_failure_logs_error(self, _init):
        x_store.set_campaign_config(
            "cloud",
            enabled=True,
            template={
                "text": "Nube {date}: {top_words}",
                "hashtags": "#test",
                "attach_image": True,
            },
        )
        with patch("app.x_campaigns.x_client.upload_media", side_effect=RuntimeError("fake upload")):
            with patch("app.wordcloud.render_png", return_value=b"png"):
                with patch("app.x_campaigns.x_client.post_tweet") as mock_post:
                    result = x_campaigns.run_cloud_campaign([["inflacion", 10], ["dolar", 5]])
        mock_post.assert_not_called()
        assert result.ok is False
        assert "image_upload_failed" in result.reason

    def test_success_attaches_media(self, _init):
        x_store.set_campaign_config(
            "cloud",
            enabled=True,
            template={
                "text": "Nube {date}: {top_words}",
                "hashtags": "#test",
                "attach_image": True,
            },
        )
        with patch("app.x_campaigns.x_client.upload_media", return_value="mid-1") as up, \
             patch("app.wordcloud.render_png", return_value=b"png"), \
             patch("app.x_campaigns.x_client.post_tweet", side_effect=_fake_post_tweet("c1")) as mock_post:
            result = x_campaigns.run_cloud_campaign([["dolar", 8]])

        assert result.ok is True
        up.assert_called_once()
        assert mock_post.call_args.kwargs["media_ids"] == ["mid-1"]


class TestBreakingRunner:
    def test_respects_min_source_count(self, _init, sample_groups):
        x_store.set_campaign_config(
            "breaking",
            enabled=True,
            schedule={"min_source_count": 10, "categories": [], "cooldown_minutes": 0},
        )
        group = sample_groups[0]
        with patch("app.x_campaigns.x_client.post_tweet") as mock_post:
            result = x_campaigns.run_breaking_campaign(group)
        mock_post.assert_not_called()
        assert result.status == "skipped"
        assert result.reason == "below_min_source_count"

    def test_posts_when_criteria_met(self, _init, sample_groups):
        x_store.set_campaign_config(
            "breaking",
            enabled=True,
            schedule={"min_source_count": 1, "categories": [], "cooldown_minutes": 0},
        )
        x_campaigns._last_breaking_at = None
        x_campaigns._last_breaking_group_id = None
        with patch("app.x_campaigns.x_client.post_tweet", side_effect=_fake_post_tweet("b1")) as mock_post:
            result = x_campaigns.run_breaking_campaign(sample_groups[0])
        assert result.ok is True
        assert mock_post.called


class TestPickBreakingCandidate:
    def test_chooses_freshest_multi_source(self, _init, sample_groups):
        x_campaigns._last_breaking_group_id = None
        candidate = x_campaigns.pick_breaking_candidate(
            sample_groups,
            min_source_count=2,
            allowed_categories=[],
            max_age_minutes=60 * 24 * 365 * 10,  # sin filtro de edad para el fixture
        )
        # Sólo el primer grupo tiene 2 fuentes.
        assert candidate is not None
        assert candidate.group_id == sample_groups[0].group_id

    def test_filters_by_category(self, _init, sample_groups):
        x_campaigns._last_breaking_group_id = None
        candidate = x_campaigns.pick_breaking_candidate(
            sample_groups,
            min_source_count=1,
            allowed_categories=["deportes"],
            max_age_minutes=60 * 24 * 365 * 10,
        )
        assert candidate is not None
        assert candidate.category == "deportes"
