"""Tests for app.process_events_store — process/scheduler event log."""

from __future__ import annotations

import time

import pytest

from app.process_events_store import (
    count_process_events,
    init_process_events_table,
    list_known_components,
    log_process_event,
    purge_old_events,
    query_process_events,
)


@pytest.fixture
def _init(temp_db):
    init_process_events_table()
    yield


class TestInit:
    def test_creates_table(self, temp_db):
        init_process_events_table()
        log_process_event(component="scheduler", event_type="refresh_news")
        assert count_process_events() == 1

    def test_idempotent(self, temp_db):
        init_process_events_table()
        init_process_events_table()
        log_process_event(component="scheduler", event_type="refresh_news")
        assert count_process_events() == 1


class TestLogProcessEvent:
    def test_basic_insert(self, _init):
        log_process_event(
            component="scheduler",
            event_type="refresh_news",
            status="ok",
            duration_ms=1234,
            message="Fetched 42 articles",
        )
        rows = query_process_events()
        assert len(rows) == 1
        row = rows[0]
        assert row["component"] == "scheduler"
        assert row["event_type"] == "refresh_news"
        assert row["status"] == "ok"
        assert row["duration_ms"] == 1234
        assert row["message"] == "Fetched 42 articles"

    def test_invalid_status_becomes_info(self, _init):
        log_process_event(component="scheduler", event_type="foo", status="DOES_NOT_EXIST")
        rows = query_process_events()
        assert rows[0]["status"] == "info"

    def test_status_lowercased(self, _init):
        log_process_event(component="scheduler", event_type="foo", status="ERROR")
        rows = query_process_events()
        assert rows[0]["status"] == "error"

    def test_details_json_serialized(self, _init):
        log_process_event(
            component="ai",
            event_type="call",
            details={"tokens": 100, "provider": "gemini"},
        )
        rows = query_process_events()
        assert "gemini" in rows[0]["details_json"]
        assert "100" in rows[0]["details_json"]

    def test_swallows_unserializable_details(self, _init):
        class NotJson:
            pass

        log_process_event(
            component="ai",
            event_type="call",
            details={"obj": NotJson()},
        )
        rows = query_process_events()
        assert len(rows) == 1

    def test_long_message_truncated(self, _init):
        long = "x" * 5000
        log_process_event(component="scheduler", event_type="foo", message=long)
        rows = query_process_events()
        assert len(rows[0]["message"]) <= 2000

    def test_swallows_db_errors(self, _init, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr("app.process_events_store.get_conn", boom)
        log_process_event(component="scheduler", event_type="foo")


class TestQueryProcessEvents:
    def test_orders_newest_first(self, _init):
        log_process_event(component="scheduler", event_type="a")
        time.sleep(0.01)
        log_process_event(component="scheduler", event_type="b")
        time.sleep(0.01)
        log_process_event(component="scheduler", event_type="c")
        rows = query_process_events()
        assert [r["event_type"] for r in rows] == ["c", "b", "a"]

    def test_filter_by_component(self, _init):
        log_process_event(component="scheduler", event_type="x")
        log_process_event(component="ai", event_type="y")
        log_process_event(component="rss", event_type="z")
        rows = query_process_events(component="ai")
        assert len(rows) == 1
        assert rows[0]["event_type"] == "y"

    def test_filter_by_status(self, _init):
        log_process_event(component="scheduler", event_type="a", status="ok")
        log_process_event(component="scheduler", event_type="b", status="error")
        log_process_event(component="scheduler", event_type="c", status="error")
        rows = query_process_events(status="error")
        assert len(rows) == 2

    def test_pagination(self, _init):
        for i in range(10):
            log_process_event(component="scheduler", event_type=f"e{i}")
        page1 = query_process_events(limit=3, offset=0)
        page2 = query_process_events(limit=3, offset=3)
        assert len(page1) == 3
        assert len(page2) == 3
        assert page1[0]["event_type"] != page2[0]["event_type"]

    def test_count(self, _init):
        for i in range(5):
            log_process_event(component="scheduler", event_type=f"e{i}")
        assert count_process_events() == 5
        assert count_process_events(component="ai") == 0


class TestListKnownComponents:
    def test_returns_distinct_sorted(self, _init):
        log_process_event(component="scheduler", event_type="a")
        log_process_event(component="ai", event_type="b")
        log_process_event(component="scheduler", event_type="c")
        log_process_event(component="lifespan", event_type="d")
        assert list_known_components() == ["ai", "lifespan", "scheduler"]


class TestPurge:
    def test_purge_keeps_recent(self, _init):
        log_process_event(component="scheduler", event_type="keep")
        purge_old_events(days=30)
        assert count_process_events() == 1
