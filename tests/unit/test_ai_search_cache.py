"""Unit tests for AI search cache management functions."""

from __future__ import annotations

import time
from unittest.mock import patch

from app.ai_search import (
    TOPICS_TTL,
    TOPSTORY_TTL,
    _get_cached_topic_labels,
    _last_good_topics,
    _topics_cache,
    _topstory_cache,
    _search_cache,
    invalidate_search_cache,
    is_public_topic_query,
    is_topics_cache_valid,
    is_topstory_cache_valid,
)


def _swap_topic_caches(live=None, fallback=None):
    """Context-manager-like helper: swap in temporary topic caches, return restore fn."""
    original_live = _topics_cache["topics"]
    original_fallback = _last_good_topics["topics"]
    _topics_cache["topics"] = live or []
    _last_good_topics["topics"] = fallback or []

    def _restore():
        _topics_cache["topics"] = original_live
        _last_good_topics["topics"] = original_fallback

    return _restore


class TestInvalidateSearchCache:
    def test_removes_existing_entry(self):
        _search_cache["test query"] = {"relevant_group_ids": ["g1"]}
        invalidate_search_cache("Test Query")
        assert "test query" not in _search_cache

    def test_noop_for_missing_entry(self):
        _search_cache.clear()
        invalidate_search_cache("nonexistent")

    def test_strips_whitespace(self):
        _search_cache["dólar"] = {"relevant_group_ids": ["g1"]}
        invalidate_search_cache("  dólar  ")
        assert "dólar" not in _search_cache


class TestGetCachedTopicLabels:
    def test_returns_labels_lowercase(self):
        restore = _swap_topic_caches(live=[
            {"label": "Dólar y Mercados", "emoji": "💰"},
            {"label": "Crisis Energética", "emoji": "⚡"},
        ])
        try:
            labels = _get_cached_topic_labels()
            assert "dólar y mercados" in labels
            assert "crisis energética" in labels
        finally:
            restore()

    def test_empty_cache(self):
        restore = _swap_topic_caches()
        try:
            assert _get_cached_topic_labels() == set()
        finally:
            restore()

    def test_skips_entries_without_label(self):
        restore = _swap_topic_caches(live=[
            {"emoji": "🔥"},
            {"label": "Tema válido", "emoji": "📰"},
        ])
        try:
            labels = _get_cached_topic_labels()
            assert len(labels) == 1
            assert "tema válido" in labels
        finally:
            restore()

    def test_includes_fallback_last_good_topics(self):
        """When live topics are empty (AI rate-limited) the labels from the
        last-good fallback must still be exposed as public, since the UI
        serves them via /api/topics."""
        restore = _swap_topic_caches(
            live=[],
            fallback=[
                {"label": "Dólar", "emoji": "💵"},
                {"label": "Inflación", "emoji": "📈"},
            ],
        )
        try:
            labels = _get_cached_topic_labels()
            assert "dólar" in labels
            assert "inflación" in labels
        finally:
            restore()

    def test_unions_live_and_fallback(self):
        restore = _swap_topic_caches(
            live=[{"label": "Live", "emoji": "🟢"}],
            fallback=[{"label": "Fallback", "emoji": "🟡"}],
        )
        try:
            labels = _get_cached_topic_labels()
            assert labels == {"live", "fallback"}
        finally:
            restore()


class TestIsPublicTopicQuery:
    """Public allowlist used by /api/search to let anonymous users query
    curated topic labels without login."""

    def test_matches_cached_topic_case_insensitive(self):
        restore = _swap_topic_caches(live=[{"label": "Dólar y Mercados", "emoji": "💰"}])
        try:
            assert is_public_topic_query("Dólar y Mercados") is True
            assert is_public_topic_query("dólar y mercados") is True
            assert is_public_topic_query("  DÓLAR Y MERCADOS  ") is True
        finally:
            restore()

    def test_matches_fallback_topic_when_live_empty(self):
        """Anonymous users must be able to click fallback topic chips even
        while both AI providers are rate-limited."""
        restore = _swap_topic_caches(
            live=[],
            fallback=[{"label": "Dólar", "emoji": "💵"}],
        )
        try:
            assert is_public_topic_query("Dólar") is True
        finally:
            restore()

    def test_rejects_unknown_query(self):
        restore = _swap_topic_caches(live=[{"label": "Dólar", "emoji": "💰"}])
        try:
            assert is_public_topic_query("cualquier cosa random") is False
        finally:
            restore()

    def test_rejects_empty_query(self):
        assert is_public_topic_query("") is False
        assert is_public_topic_query("   ") is False

    def test_no_topics_cached_rejects_all(self):
        restore = _swap_topic_caches()
        try:
            assert is_public_topic_query("cualquier tema") is False
        finally:
            restore()


class TestIsTopicsCacheValid:
    def test_empty_cache_invalid(self):
        original_topics = _topics_cache["topics"]
        original_ts = _topics_cache["ts"]
        try:
            _topics_cache["topics"] = []
            _topics_cache["ts"] = time.time()
            assert is_topics_cache_valid() is False
        finally:
            _topics_cache["topics"] = original_topics
            _topics_cache["ts"] = original_ts

    def test_fresh_cache_valid(self):
        original_topics = _topics_cache["topics"]
        original_ts = _topics_cache["ts"]
        try:
            _topics_cache["topics"] = [{"label": "Test"}]
            _topics_cache["ts"] = time.time()
            assert is_topics_cache_valid() is True
        finally:
            _topics_cache["topics"] = original_topics
            _topics_cache["ts"] = original_ts

    def test_expired_cache_invalid(self):
        original_topics = _topics_cache["topics"]
        original_ts = _topics_cache["ts"]
        try:
            _topics_cache["topics"] = [{"label": "Test"}]
            _topics_cache["ts"] = time.time() - TOPICS_TTL - 10
            assert is_topics_cache_valid() is False
        finally:
            _topics_cache["topics"] = original_topics
            _topics_cache["ts"] = original_ts


class TestIsTopstoryCacheValid:
    def test_empty_cache_invalid(self):
        original_data = _topstory_cache["data"]
        original_ts = _topstory_cache["ts"]
        try:
            _topstory_cache["data"] = None
            _topstory_cache["ts"] = time.time()
            assert is_topstory_cache_valid() is False
        finally:
            _topstory_cache["data"] = original_data
            _topstory_cache["ts"] = original_ts

    def test_fresh_cache_valid(self):
        original_data = _topstory_cache["data"]
        original_ts = _topstory_cache["ts"]
        try:
            _topstory_cache["data"] = {"story": "some data"}
            _topstory_cache["ts"] = time.time()
            assert is_topstory_cache_valid() is True
        finally:
            _topstory_cache["data"] = original_data
            _topstory_cache["ts"] = original_ts

    def test_expired_cache_invalid(self):
        original_data = _topstory_cache["data"]
        original_ts = _topstory_cache["ts"]
        try:
            _topstory_cache["data"] = {"story": "some data"}
            _topstory_cache["ts"] = time.time() - TOPSTORY_TTL - 10
            assert is_topstory_cache_valid() is False
        finally:
            _topstory_cache["data"] = original_data
            _topstory_cache["ts"] = original_ts
