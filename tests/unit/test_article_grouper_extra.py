"""Additional unit tests for article_grouper helpers: _extract_key_tokens, _is_next_day."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.article_grouper import _extract_key_tokens, _is_next_day


class TestExtractKeyTokens:
    def test_basic_extraction(self):
        tokens = _extract_key_tokens("La inflación subió fuerte")
        assert "inflacion" in tokens
        assert "subio" in tokens
        assert "fuerte" in tokens

    def test_filters_stopwords(self):
        tokens = _extract_key_tokens("El gobierno de Argentina lanzó plan")
        assert "el" not in tokens
        assert "gobierno" not in tokens

    def test_strips_accents(self):
        tokens = _extract_key_tokens("Córdoba celebró un récord económico")
        assert "cordoba" in tokens
        assert "celebro" in tokens
        assert "record" in tokens

    def test_empty_string(self):
        assert _extract_key_tokens("") == set()

    def test_only_stopwords(self):
        tokens = _extract_key_tokens("el la los de en con")
        assert tokens == set()

    def test_returns_set(self):
        tokens = _extract_key_tokens("milei milei milei")
        assert isinstance(tokens, set)
        assert "milei" in tokens


class TestIsNextDay:
    def test_same_day_returns_false(self):
        pub = datetime(2025, 6, 15, 10, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 15, 23, 59, tzinfo=timezone.utc)
        assert _is_next_day(pub, now) is False

    def test_next_day_returns_true(self):
        pub = datetime(2025, 6, 15, 22, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 16, 1, 0, tzinfo=timezone.utc)
        assert _is_next_day(pub, now) is True

    def test_two_days_later_returns_true(self):
        pub = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 17, 12, 0, tzinfo=timezone.utc)
        assert _is_next_day(pub, now) is True

    def test_naive_datetimes_treated_as_utc(self):
        pub = datetime(2025, 6, 15, 12, 0)
        now = datetime(2025, 6, 16, 12, 0)
        assert _is_next_day(pub, now) is True

    def test_earlier_date_returns_false(self):
        pub = datetime(2025, 6, 16, 12, 0, tzinfo=timezone.utc)
        now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        assert _is_next_day(pub, now) is False
