"""Unit tests for helper functions in app.main."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.main import _current_week_bounds, _db_text_search, _ensure_aware, _resolve_client_ip


class TestEnsureAware:
    def test_naive_datetime_becomes_utc(self):
        naive = datetime(2025, 6, 15, 12, 0)
        result = _ensure_aware(naive)
        assert result.tzinfo is not None
        assert result.tzinfo == timezone.utc

    def test_aware_datetime_unchanged(self):
        from datetime import timedelta

        art = timezone(timedelta(hours=-3))
        aware = datetime(2025, 6, 15, 12, 0, tzinfo=art)
        result = _ensure_aware(aware)
        assert result.tzinfo == art

    def test_utc_datetime_unchanged(self):
        utc_dt = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        result = _ensure_aware(utc_dt)
        assert result is utc_dt


class TestResolveClientIp:
    def _make_request(self, headers: dict, client_host: str = "127.0.0.1"):
        req = MagicMock()
        req.headers = headers
        req.client = MagicMock()
        req.client.host = client_host
        return req

    def test_cf_connecting_ip(self):
        req = self._make_request({"cf-connecting-ip": "1.2.3.4"})
        assert _resolve_client_ip(req) == "1.2.3.4"

    def test_x_forwarded_for_first_ip(self):
        req = self._make_request({"x-forwarded-for": "5.6.7.8, 10.0.0.1"})
        assert _resolve_client_ip(req) == "5.6.7.8"

    def test_x_real_ip(self):
        req = self._make_request({"x-real-ip": "9.10.11.12"})
        assert _resolve_client_ip(req) == "9.10.11.12"

    def test_proxy_ip_skipped(self):
        req = self._make_request(
            {"x-forwarded-for": "100.64.1.2, 5.6.7.8"},
            client_host="127.0.0.1",
        )
        assert _resolve_client_ip(req) == "127.0.0.1"

    def test_fallback_to_client_host(self):
        req = self._make_request({}, client_host="192.168.1.1")
        assert _resolve_client_ip(req) == "192.168.1.1"

    def test_no_client(self):
        req = MagicMock()
        req.headers = {}
        req.client = None
        assert _resolve_client_ip(req) == ""

    def test_header_priority_cf_over_xff(self):
        req = self._make_request({
            "cf-connecting-ip": "1.1.1.1",
            "x-forwarded-for": "2.2.2.2",
        })
        assert _resolve_client_ip(req) == "1.1.1.1"


class TestCurrentWeekBounds:
    def test_returns_two_dates(self):
        start, end = _current_week_bounds()
        assert len(start) == 10
        assert len(end) == 10
        assert start <= end

    def test_start_is_monday(self):
        start, _ = _current_week_bounds()
        from datetime import datetime as dt
        monday = dt.fromisoformat(start)
        assert monday.weekday() == 0


class TestDbTextSearch:
    def test_returns_empty_on_exception(self, monkeypatch):
        monkeypatch.setattr(
            "app.main.text_search_groups",
            MagicMock(side_effect=RuntimeError("DB error")),
        )
        result = _db_text_search("test query")
        assert result == []

    def test_delegates_to_text_search_groups(self, monkeypatch):
        mock_groups = [MagicMock()]
        monkeypatch.setattr(
            "app.main.text_search_groups",
            MagicMock(return_value=mock_groups),
        )
        result = _db_text_search("test query", limit=5)
        assert result == mock_groups
