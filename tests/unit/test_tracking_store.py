from __future__ import annotations

import pytest

from app.tracking_store import (
    init_tracking_table,
    log_events,
    purge_old_events,
    query_anonymous_daily,
    query_anonymous_engagement,
    query_anonymous_features,
    query_anonymous_hourly,
    query_anonymous_overview,
    query_anonymous_searches,
    query_anonymous_sections,
    query_anonymous_top_content,
    query_anonymous_top_visitors,
    query_daily_activity,
    query_engagement,
    query_feature_usage,
    query_hourly_distribution,
    query_popular_searches,
    query_sections_visited,
    query_top_content,
    query_usage_stats,
)


@pytest.fixture(autouse=True)
def _setup_db(temp_db):
    init_tracking_table()


def _make_events(types, ts="2026-03-28T12:00:00", data=None):
    return [{"type": t, "data": data or {"view": "test"}, "ts": ts} for t in types]


class TestLogEvents:
    def test_logs_batch(self):
        count = log_events(
            _make_events(["page_view", "group_click"]),
            user_id="u1",
            session_id="s1",
        )
        assert count == 2

    def test_logs_without_user(self):
        count = log_events(
            _make_events(["page_view"]),
            session_id="s-anon",
        )
        assert count == 1

    def test_empty_events(self):
        assert log_events([], session_id="s1") == 0


class TestQueryUsageStats:
    def test_returns_stats_with_page_views(self):
        log_events(_make_events(["page_view", "page_view", "ai_search"]), user_id="u1", session_id="s1")
        log_events(_make_events(["page_view"]), session_id="s2")

        stats = query_usage_stats()
        assert stats["total_events"] == 4
        assert stats["page_views"] == 3
        assert stats["unique_users"] == 1
        assert stats["unique_sessions"] == 2

    def test_date_filter(self):
        log_events(_make_events(["page_view"], ts="2026-01-01T12:00:00"), session_id="s1")
        log_events(_make_events(["page_view"], ts="2026-03-28T12:00:00"), session_id="s2")

        stats = query_usage_stats(desde="2026-03-01", hasta="2026-03-31")
        assert stats["total_events"] >= 1


class TestPopularSearches:
    def test_returns_top_queries(self):
        for _ in range(3):
            log_events(
                [{"type": "ai_search", "data": {"query": "dólar"}, "ts": "2026-03-28T12:00:00"}],
                session_id="s1",
            )
        log_events(
            [{"type": "ai_search", "data": {"query": "inflación"}, "ts": "2026-03-28T12:00:00"}],
            session_id="s2",
        )

        results = query_popular_searches(limit=10)
        assert len(results) >= 1
        assert results[0]["count"] >= results[-1]["count"]


class TestFeatureUsage:
    def test_returns_ranking_without_page_view(self):
        log_events(_make_events(["page_view", "page_view", "ai_search", "group_click"]), session_id="s1")
        ranking = query_feature_usage()
        features = [r["feature"] for r in ranking]
        assert "page_view" not in features
        assert "ai_search" in features
        assert "group_click" in features
        assert ranking[0]["count"] >= ranking[-1]["count"]


class TestSectionsVisited:
    def test_returns_sections(self):
        log_events(
            [
                {"type": "page_view", "data": {"view": "noticias"}, "ts": "2026-03-28T12:00:00"},
                {"type": "page_view", "data": {"view": "noticias"}, "ts": "2026-03-28T12:01:00"},
                {"type": "page_view", "data": {"view": "metricas"}, "ts": "2026-03-28T12:02:00"},
            ],
            session_id="s1",
        )
        sections = query_sections_visited()
        assert len(sections) >= 2
        assert sections[0]["section"] == "noticias"
        assert sections[0]["count"] == 2

    def test_date_filter(self):
        log_events(
            [{"type": "page_view", "data": {"view": "noticias"}, "ts": "2026-01-01T12:00:00"}],
            session_id="s1",
        )
        log_events(
            [{"type": "page_view", "data": {"view": "metricas"}, "ts": "2026-03-28T12:00:00"}],
            session_id="s2",
        )
        sections = query_sections_visited(desde="2026-03-01", hasta="2026-03-31")
        assert len(sections) == 1
        assert sections[0]["section"] == "metricas"


class TestTopContent:
    def test_returns_top_groups(self):
        for _ in range(3):
            log_events(
                [{"type": "group_click", "data": {"group_id": "g1", "title": "Dólar hoy"}, "ts": "2026-03-28T12:00:00"}],
                session_id="s1",
            )
        log_events(
            [{"type": "group_click", "data": {"group_id": "g2", "title": "Inflación"}, "ts": "2026-03-28T12:00:00"}],
            session_id="s2",
        )
        results = query_top_content(limit=10)
        assert len(results) == 2
        assert results[0]["title"] == "Dólar hoy"
        assert results[0]["count"] == 3


class TestEngagement:
    def test_returns_metrics(self):
        log_events(
            [
                {"type": "page_view", "data": {"view": "noticias"}, "ts": "2026-03-28T12:00:00"},
                {"type": "page_view", "data": {"view": "metricas"}, "ts": "2026-03-28T12:05:00"},
                {"type": "group_click", "data": {}, "ts": "2026-03-28T12:06:00"},
            ],
            user_id="u1",
            session_id="s1",
        )
        log_events(
            [{"type": "page_view", "data": {"view": "noticias"}, "ts": "2026-03-28T13:00:00"}],
            session_id="s2",
        )

        eng = query_engagement()
        assert eng["total_sessions"] == 2
        assert eng["avg_pages_per_session"] > 0
        assert 0 <= eng["bounce_rate"] <= 100
        assert eng["avg_events_per_session"] > 0
        assert eng["avg_duration_seconds"] >= 0

    def test_bounce_rate_calculation(self):
        log_events(
            [{"type": "page_view", "data": {"view": "noticias"}, "ts": "2026-03-28T12:00:00"}],
            session_id="s-bounce",
        )
        eng = query_engagement()
        assert eng["bounce_rate"] == 100.0

    def test_empty_returns_zeros(self):
        eng = query_engagement()
        assert eng["total_sessions"] == 0
        assert eng["bounce_rate"] == 0


class TestHourlyDistribution:
    def test_returns_hours_with_offset(self):
        log_events(
            [
                {"type": "page_view", "data": {}, "ts": "2026-03-28T09:00:00"},
                {"type": "page_view", "data": {}, "ts": "2026-03-28T09:30:00"},
                {"type": "page_view", "data": {}, "ts": "2026-03-28T14:00:00"},
            ],
            session_id="s1",
        )
        hours = query_hourly_distribution(utc_offset=-3)
        assert len(hours) >= 2
        hour_6 = next((h for h in hours if h["hour"] == 6), None)
        assert hour_6 is not None
        assert hour_6["events"] == 2

    def test_no_offset(self):
        log_events(
            [{"type": "page_view", "data": {}, "ts": "2026-03-28T09:00:00"}],
            session_id="s1",
        )
        hours = query_hourly_distribution(utc_offset=0)
        hour_9 = next((h for h in hours if h["hour"] == 9), None)
        assert hour_9 is not None
        assert hour_9["events"] == 1


class TestDailyActivity:
    def test_returns_daily_data_with_pageviews(self):
        log_events(
            [
                {"type": "page_view", "data": {}, "ts": "2026-03-28T12:00:00"},
                {"type": "group_click", "data": {}, "ts": "2026-03-28T12:01:00"},
            ],
            user_id="u1",
            session_id="s1",
        )
        log_events(_make_events(["page_view"], ts="2026-03-27T12:00:00"), session_id="s2")

        days = query_daily_activity()
        assert len(days) >= 2
        assert "day" in days[0]
        assert "sessions" in days[0]
        assert "page_views" in days[0]
        day_28 = next(d for d in days if d["day"] == "2026-03-28")
        assert day_28["page_views"] == 1
        assert day_28["events"] == 2


class TestPurge:
    def test_purges_old_events(self):
        log_events(_make_events(["page_view"], ts="2020-01-01T12:00:00"), session_id="s-old")
        log_events(_make_events(["page_view"], ts="2026-03-28T12:00:00"), session_id="s-new")

        deleted = purge_old_events(days=90)
        assert deleted >= 1

        stats = query_usage_stats()
        assert stats["total_events"] >= 1


# ── Anonymous query tests ────────────────────────────────────────────────


def _seed_mixed_traffic():
    """Insert events from both logged-in and anonymous users with distinct IPs."""
    log_events(
        [
            {"type": "page_view", "data": {"view": "noticias"}, "ts": "2026-03-28T10:00:00"},
            {"type": "group_click", "data": {"group_id": "g1", "title": "Dólar"}, "ts": "2026-03-28T10:05:00"},
            {"type": "ai_search", "data": {"query": "inflación"}, "ts": "2026-03-28T10:10:00"},
        ],
        user_id="u1",
        session_id="s-auth",
        ip_address="10.0.0.1",
    )
    log_events(
        [
            {"type": "page_view", "data": {"view": "noticias"}, "ts": "2026-03-28T11:00:00"},
            {"type": "page_view", "data": {"view": "metricas"}, "ts": "2026-03-28T11:05:00"},
            {"type": "group_click", "data": {"group_id": "g2", "title": "Elecciones"}, "ts": "2026-03-28T11:10:00"},
        ],
        session_id="s-anon1",
        ip_address="192.168.1.100",
    )
    log_events(
        [
            {"type": "page_view", "data": {"view": "noticias"}, "ts": "2026-03-28T12:00:00"},
            {"type": "ai_search", "data": {"query": "dólar"}, "ts": "2026-03-28T12:05:00"},
        ],
        session_id="s-anon2",
        ip_address="192.168.1.200",
    )


class TestAnonymousOverview:
    def test_counts_only_anonymous(self):
        _seed_mixed_traffic()
        ov = query_anonymous_overview()
        assert ov["unique_visitors"] == 2
        assert ov["unique_sessions"] == 2
        assert ov["total_events"] == 5
        assert ov["page_views"] == 3

    def test_ratio_calculation(self):
        _seed_mixed_traffic()
        ov = query_anonymous_overview()
        assert 0 < ov["anon_ratio"] < 100

    def test_empty_db(self):
        ov = query_anonymous_overview()
        assert ov["unique_visitors"] == 0
        assert ov["anon_ratio"] == 0

    def test_date_filter(self):
        log_events(
            _make_events(["page_view"], ts="2026-01-15T12:00:00"),
            session_id="s-old",
            ip_address="10.0.0.5",
        )
        log_events(
            _make_events(["page_view"], ts="2026-03-28T12:00:00"),
            session_id="s-new",
            ip_address="10.0.0.6",
        )
        ov = query_anonymous_overview(desde="2026-03-01", hasta="2026-03-31")
        assert ov["unique_visitors"] == 1
        assert ov["unique_sessions"] == 1


class TestAnonymousEngagement:
    def test_returns_engagement_for_anon_only(self):
        _seed_mixed_traffic()
        eng = query_anonymous_engagement()
        assert eng["total_sessions"] == 2
        assert eng["avg_pages_per_session"] > 0
        assert 0 <= eng["bounce_rate"] <= 100

    def test_bounce_rate_single_page(self):
        log_events(
            [{"type": "page_view", "data": {"view": "noticias"}, "ts": "2026-03-28T12:00:00"}],
            session_id="s-bounce",
            ip_address="10.0.0.5",
        )
        eng = query_anonymous_engagement()
        assert eng["bounce_rate"] == 100.0

    def test_empty_returns_zeros(self):
        eng = query_anonymous_engagement()
        assert eng["total_sessions"] == 0
        assert eng["bounce_rate"] == 0


class TestAnonymousSections:
    def test_returns_sections_for_anon(self):
        _seed_mixed_traffic()
        sections = query_anonymous_sections()
        names = [s["section"] for s in sections]
        assert "noticias" in names
        assert "metricas" in names
        noticias = next(s for s in sections if s["section"] == "noticias")
        assert noticias["count"] == 2


class TestAnonymousFeatures:
    def test_excludes_page_view(self):
        _seed_mixed_traffic()
        features = query_anonymous_features()
        types = [f["feature"] for f in features]
        assert "page_view" not in types
        assert "group_click" in features[0]["feature"] or "ai_search" in features[0]["feature"]


class TestAnonymousTopContent:
    def test_returns_top_groups(self):
        _seed_mixed_traffic()
        content = query_anonymous_top_content()
        assert len(content) == 1
        assert content[0]["title"] == "Elecciones"

    def test_date_filter(self):
        _seed_mixed_traffic()
        content = query_anonymous_top_content(desde="2026-04-01", hasta="2026-04-30")
        assert len(content) == 0


class TestAnonymousSearches:
    def test_returns_anon_searches(self):
        _seed_mixed_traffic()
        searches = query_anonymous_searches()
        assert len(searches) == 1
        assert searches[0]["query"] == "dólar"


class TestAnonymousDaily:
    def test_returns_daily_with_visitor_count(self):
        _seed_mixed_traffic()
        days = query_anonymous_daily()
        assert len(days) >= 1
        day_28 = next(d for d in days if d["day"] == "2026-03-28")
        assert day_28["visitors"] == 2
        assert day_28["sessions"] == 2
        assert day_28["page_views"] == 3

    def test_date_filter(self):
        _seed_mixed_traffic()
        days = query_anonymous_daily(desde="2026-04-01", hasta="2026-04-30")
        assert len(days) == 0


class TestAnonymousHourly:
    def test_returns_hourly_for_anon(self):
        _seed_mixed_traffic()
        hours = query_anonymous_hourly(utc_offset=0)
        assert len(hours) >= 2
        hour_11 = next((h for h in hours if h["hour"] == 11), None)
        assert hour_11 is not None
        assert hour_11["events"] == 3

    def test_with_offset(self):
        _seed_mixed_traffic()
        hours = query_anonymous_hourly(utc_offset=-3)
        hour_8 = next((h for h in hours if h["hour"] == 8), None)
        assert hour_8 is not None


class TestAnonymousTopVisitors:
    def test_returns_full_ips(self):
        _seed_mixed_traffic()
        visitors = query_anonymous_top_visitors()
        assert len(visitors) == 2
        ips = [v["ip"] for v in visitors]
        assert "192.168.1.100" in ips
        assert "192.168.1.200" in ips
        for v in visitors:
            assert v["sessions"] >= 1
            assert v["events"] >= 1

    def test_sorted_by_events_desc(self):
        _seed_mixed_traffic()
        visitors = query_anonymous_top_visitors()
        assert visitors[0]["events"] >= visitors[1]["events"]

    def test_empty_db(self):
        visitors = query_anonymous_top_visitors()
        assert visitors == []
