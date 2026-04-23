"""Tests for app.x_store — persistencia de campañas X + cupos."""

from __future__ import annotations

import pytest

from app import x_store


@pytest.fixture
def _init(temp_db):
    x_store.init_x_tables()
    x_store._campaign_cache_ts = 0
    x_store._tier_cache_ts = 0
    yield


class TestInit:
    def test_creates_all_tables(self, temp_db):
        x_store.init_x_tables()
        assert x_store.get_tier_config()["tier"] == "disabled"
        campaigns = x_store.list_campaigns()
        assert {c["campaign_key"] for c in campaigns} == set(x_store.VALID_CAMPAIGN_KEYS)
        for c in campaigns:
            assert c["enabled"] is False  # defaults off

    def test_idempotent(self, temp_db):
        x_store.init_x_tables()
        x_store.init_x_tables()


class TestCampaignCRUD:
    def test_update_enabled_and_schedule(self, _init):
        ok = x_store.set_campaign_config(
            "topstory",
            enabled=True,
            schedule={"hour": 8, "minute": 45},
        )
        assert ok
        cfg = x_store.get_campaign_config("topstory")
        assert cfg["enabled"] is True
        assert cfg["schedule"]["hour"] == 8
        assert cfg["schedule"]["minute"] == 45

    def test_update_template_only(self, _init):
        ok = x_store.set_campaign_config(
            "cloud",
            template={"text": "Hola {date}", "hashtags": "#test", "attach_image": False},
        )
        assert ok
        cfg = x_store.get_campaign_config("cloud")
        assert cfg["template"]["text"] == "Hola {date}"
        assert cfg["template"]["attach_image"] is False

    def test_reject_unknown_key(self, _init):
        assert x_store.set_campaign_config("bogus", enabled=True) is False
        assert x_store.get_campaign_config("bogus") is None

    def test_list_preserves_canonical_order(self, _init):
        keys = [c["campaign_key"] for c in x_store.list_campaigns()]
        assert keys == ["cloud", "topstory", "weekly", "topics", "breaking"]


class TestTier:
    def test_basic_loads_defaults(self, _init):
        assert x_store.set_tier_config("basic") is True
        t = x_store.get_tier_config()
        assert t["tier"] == "basic"
        assert t["daily_cap"] == 50
        assert t["monthly_cap"] == 1500
        assert t["posting_allowed"] is True

    def test_pay_per_use_respects_input(self, _init):
        assert x_store.set_tier_config(
            "pay_per_use", daily_cap=5, monthly_cap=100, monthly_usd=12.50,
        ) is True
        t = x_store.get_tier_config()
        assert t["tier"] == "pay_per_use"
        assert t["daily_cap"] == 5
        assert t["monthly_cap"] == 100
        assert t["monthly_usd"] == pytest.approx(12.50)

    def test_legacy_custom_alias_maps_to_pay_per_use(self, _init):
        # set_tier_config acepta el nombre viejo "custom" y lo normaliza.
        assert x_store.set_tier_config(
            "custom", daily_cap=3, monthly_cap=30, monthly_usd=0,
        ) is True
        t = x_store.get_tier_config()
        assert t["tier"] == "pay_per_use"
        assert t["daily_cap"] == 3

    def test_disabled_forces_zero_and_disables_all(self, _init):
        x_store.set_tier_config("basic")
        x_store.set_campaign_config("topstory", enabled=True)
        assert x_store.get_campaign_config("topstory")["enabled"] is True

        assert x_store.set_tier_config("disabled") is True
        t = x_store.get_tier_config()
        assert t["tier"] == "disabled"
        assert t["daily_cap"] == 0
        assert t["posting_allowed"] is False
        assert x_store.get_campaign_config("topstory")["enabled"] is False

    def test_legacy_free_alias_maps_to_disabled(self, _init):
        # set_tier_config acepta el nombre viejo "free" y lo normaliza.
        assert x_store.set_tier_config("free") is True
        t = x_store.get_tier_config()
        assert t["tier"] == "disabled"
        assert t["posting_allowed"] is False

    def test_invalid_tier_rejected(self, _init):
        assert x_store.set_tier_config("enterprise") is False
        assert x_store.set_tier_config("basic", daily_cap=-1) is False


class TestCaps:
    def test_disabled_blocks(self, _init):
        x_store.set_tier_config("disabled")
        ok, reason = x_store.check_cap()
        assert ok is False
        assert reason == "disabled_by_tier"

    def test_basic_under_cap(self, _init):
        x_store.set_tier_config("basic")
        ok, reason = x_store.check_cap()
        assert ok is True
        assert reason == "ok"

    def test_daily_cap_reached(self, _init):
        x_store.set_tier_config("pay_per_use", daily_cap=2, monthly_cap=100, monthly_usd=0)
        for _ in range(2):
            x_store.log_x_post(campaign_key="topstory", status="ok")
        ok, reason = x_store.check_cap(extra_posts=1)
        assert ok is False
        assert reason == "daily_cap_reached"

    def test_monthly_cap_reached(self, _init):
        x_store.set_tier_config("pay_per_use", daily_cap=1000, monthly_cap=3, monthly_usd=0)
        for _ in range(3):
            x_store.log_x_post(campaign_key="weekly", status="ok")
        ok, reason = x_store.check_cap()
        assert ok is False
        assert reason == "monthly_cap_reached"

    def test_only_ok_counts(self, _init):
        x_store.set_tier_config("pay_per_use", daily_cap=2, monthly_cap=100, monthly_usd=0)
        # errors y skipped no consumen cupo
        for _ in range(5):
            x_store.log_x_post(campaign_key="topstory", status="error")
        ok, _ = x_store.check_cap(extra_posts=1)
        assert ok is True


class TestUsageLog:
    def test_log_and_query(self, _init):
        x_store.log_x_post(
            campaign_key="topstory",
            status="ok",
            post_id="1234",
            response_code=200,
            preview="Hola mundo",
        )
        items = x_store.query_x_usage(limit=10)
        assert len(items) == 1
        assert items[0]["campaign_key"] == "topstory"
        assert items[0]["post_id"] == "1234"
        assert items[0]["status"] == "ok"
        assert x_store.count_x_usage() == 1

    def test_filters(self, _init):
        x_store.log_x_post(campaign_key="topstory", status="ok")
        x_store.log_x_post(campaign_key="cloud", status="error", error_message="boom")
        assert x_store.count_x_usage(campaign_key="cloud") == 1
        assert x_store.count_x_usage(status="ok") == 1

    def test_last_run_updated(self, _init):
        x_store.log_x_post(campaign_key="topics", status="ok", post_id="99")
        cfg = x_store.get_campaign_config("topics")
        assert cfg["last_run_status"] == "ok"
        assert cfg["last_run_at"] is not None


class TestOAuthState:
    def test_roundtrip(self, _init):
        x_store.save_oauth_state(access_token="AAA", refresh_token="RRR", expires_at="2026-01-01T00:00:00")
        state = x_store.get_oauth_state()
        assert state["access_token"] == "AAA"
        assert state["refresh_token"] == "RRR"
        assert state["expires_at"] == "2026-01-01T00:00:00"

    def test_partial_update_preserves_other_fields(self, _init):
        x_store.save_oauth_state(access_token="AAA", refresh_token="RRR")
        x_store.save_oauth_state(handle="@vsnews")
        state = x_store.get_oauth_state()
        assert state["access_token"] == "AAA"
        assert state["refresh_token"] == "RRR"
        assert state["handle"] == "@vsnews"
