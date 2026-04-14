"""Unit tests for AI search cache management functions."""

from __future__ import annotations

import time
from unittest.mock import patch

from app.ai_search import (
    TOPICS_TTL,
    TOPSTORY_TTL,
    _get_cached_topic_labels,
    _topics_cache,
    _topstory_cache,
    _search_cache,
    invalidate_search_cache,
    is_topics_cache_valid,
    is_topstory_cache_valid,
)


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
        original = _topics_cache["topics"]
        try:
            _topics_cache["topics"] = [
                {"label": "Dólar y Mercados", "emoji": "💰"},
                {"label": "Crisis Energética", "emoji": "⚡"},
            ]
            labels = _get_cached_topic_labels()
            assert "dólar y mercados" in labels
            assert "crisis energética" in labels
        finally:
            _topics_cache["topics"] = original

    def test_empty_cache(self):
        original = _topics_cache["topics"]
        try:
            _topics_cache["topics"] = []
            assert _get_cached_topic_labels() == set()
        finally:
            _topics_cache["topics"] = original

    def test_skips_entries_without_label(self):
        original = _topics_cache["topics"]
        try:
            _topics_cache["topics"] = [
                {"emoji": "🔥"},
                {"label": "Tema válido", "emoji": "📰"},
            ]
            labels = _get_cached_topic_labels()
            assert len(labels) == 1
            assert "tema válido" in labels
        finally:
            _topics_cache["topics"] = original


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
