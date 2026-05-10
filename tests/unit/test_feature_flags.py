"""Tests for app.feature_flags — get/set/registry semantics."""

from __future__ import annotations

import pytest

from app import feature_flags
from app.ai_store import init_ai_tables
from app.feature_flags import (
    FEATURE_FLAGS,
    describe_flags,
    get_all_flags,
    get_flag,
    is_known_flag,
    set_flag,
)


@pytest.fixture(autouse=True)
def _reset_runtime_cache(monkeypatch):
    """Avoid cross-test bleed in the underlying ai_store runtime cache."""
    monkeypatch.setattr("app.ai_store._runtime_cache", {})
    monkeypatch.setattr("app.ai_store._runtime_cache_ts", 0)


@pytest.fixture
def _init_db(temp_db):
    init_ai_tables()


class TestRegistry:
    def test_hero_search_is_registered(self):
        assert "hero_search" in FEATURE_FLAGS
        meta = FEATURE_FLAGS["hero_search"]
        assert "label" in meta
        assert "description" in meta
        assert meta.get("default") is True

    def test_is_known_flag(self):
        assert is_known_flag("hero_search") is True
        assert is_known_flag("nope") is False
        assert is_known_flag("") is False


class TestGetFlag:
    def test_returns_default_when_unset(self, _init_db):
        assert get_flag("hero_search") is True

    def test_unknown_flag_raises(self, _init_db):
        with pytest.raises(KeyError):
            get_flag("does_not_exist")

    def test_returns_persisted_false(self, _init_db):
        assert set_flag("hero_search", False) is True
        assert get_flag("hero_search") is False

    def test_returns_persisted_true(self, _init_db):
        assert set_flag("hero_search", False) is True
        assert set_flag("hero_search", True) is True
        assert get_flag("hero_search") is True

    def test_unparseable_value_falls_back_to_default(self, _init_db, monkeypatch):
        from app.ai_store import _set_runtime_value
        _set_runtime_value("feature_flag.hero_search", "garbage")
        monkeypatch.setattr("app.ai_store._runtime_cache_ts", 0)
        assert get_flag("hero_search") is True


class TestSetFlag:
    def test_unknown_flag_returns_false(self, _init_db):
        assert set_flag("nope", True) is False

    def test_non_bool_returns_false(self, _init_db):
        assert set_flag("hero_search", "true") is False  # type: ignore[arg-type]
        assert set_flag("hero_search", 1) is False  # type: ignore[arg-type]
        assert set_flag("hero_search", None) is False  # type: ignore[arg-type]


class TestGetAllAndDescribe:
    def test_get_all_returns_every_registered_flag(self, _init_db):
        flags = get_all_flags()
        assert set(flags.keys()) == set(FEATURE_FLAGS.keys())
        for v in flags.values():
            assert isinstance(v, bool)

    def test_describe_includes_metadata(self, _init_db):
        rows = describe_flags()
        assert len(rows) == len(FEATURE_FLAGS)
        row = next(r for r in rows if r["name"] == "hero_search")
        assert row["label"]
        assert row["description"]
        assert row["enabled"] is True
        assert row["default"] is True

    def test_describe_reflects_persisted_value(self, _init_db):
        set_flag("hero_search", False)
        rows = describe_flags()
        row = next(r for r in rows if r["name"] == "hero_search")
        assert row["enabled"] is False
        assert row["default"] is True


class TestParseBool:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1", True), ("0", False),
            ("true", True), ("false", False),
            ("TRUE", True), ("False", False),
            ("yes", True), ("no", False),
            ("on", True), ("off", False),
            ("  1  ", True), ("  0  ", False),
        ],
    )
    def test_known_truthy_falsy(self, raw, expected):
        assert feature_flags._parse_bool(raw) is expected

    @pytest.mark.parametrize("raw", ["", "garbage", "2", "ok", None])
    def test_unparseable_returns_none(self, raw):
        assert feature_flags._parse_bool(raw) is None
