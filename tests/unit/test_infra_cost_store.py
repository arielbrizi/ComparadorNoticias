"""Tests for app.infra_cost_store — Railway cost snapshots."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from app.ai_store import init_ai_tables
from app.infra_cost_store import (
    ART,
    get_blocked_keys,
    get_current_spend,
    get_infra_limits,
    history,
    init_infra_cost_table,
    latest_snapshot,
    purge_old_snapshots,
    reset_spend_cache,
    save_snapshot,
    set_infra_limits,
)


@pytest.fixture
def _init(temp_db):
    init_infra_cost_table()
    yield


class TestInit:
    def test_creates_table(self, temp_db):
        init_infra_cost_table()
        snap = latest_snapshot()
        assert snap == {"fetched_at": None, "services": [], "total_usd_month": 0.0}

    def test_idempotent(self, temp_db):
        init_infra_cost_table()
        init_infra_cost_table()


class TestSaveSnapshot:
    def test_save_and_read(self, _init):
        rows = [
            {"service_name": "web", "service_id": "svc-1", "usd_month": 4.5, "raw": {"mem": "1GB"}},
            {"service_name": "db", "service_id": "svc-2", "usd_month": 3.0, "raw": {}},
        ]
        assert save_snapshot(rows) == 2
        snap = latest_snapshot()
        assert snap["total_usd_month"] == pytest.approx(7.5)
        names = {s["service_name"] for s in snap["services"]}
        assert names == {"web", "db"}

    def test_save_empty_noop(self, _init):
        assert save_snapshot([]) == 0
        assert latest_snapshot()["services"] == []

    def test_latest_returns_newest_batch(self, _init):
        save_snapshot([{"service_name": "a", "usd_month": 1.0, "raw": {}}])
        time.sleep(1.1)  # snapshot timestamp has second resolution
        save_snapshot([{"service_name": "b", "usd_month": 2.0, "raw": {}}])
        snap = latest_snapshot()
        names = [s["service_name"] for s in snap["services"]]
        assert names == ["b"]
        assert snap["total_usd_month"] == pytest.approx(2.0)

    def test_handles_unserializable_raw(self, _init):
        class NotJson:
            pass
        assert save_snapshot([{
            "service_name": "x", "usd_month": 1.0, "raw": {"obj": NotJson()},
        }]) == 1


class TestHistory:
    def test_uses_latest_snapshot_per_day(self, _init):
        """Multiple refreshes in the same day must NOT accumulate."""
        save_snapshot([
            {"service_name": "web", "usd_month": 1.0, "raw": {}},
            {"service_name": "db",  "usd_month": 0.5, "raw": {}},
        ])
        time.sleep(1.1)  # ensure a distinct fetched_at (second resolution)
        save_snapshot([
            {"service_name": "web", "usd_month": 2.0, "raw": {}},
            {"service_name": "db",  "usd_month": 1.0, "raw": {}},
        ])
        rows = history(days=30)
        assert len(rows) == 1
        # Only the latest snapshot (2.0 + 1.0) counts, not 1.5 + 3.0 = 4.5
        assert rows[0]["estimated_usd_month"] == pytest.approx(3.0)

    def test_sums_services_within_latest_snapshot(self, _init):
        save_snapshot([
            {"service_name": "a", "usd_month": 1.25, "raw": {}},
            {"service_name": "b", "usd_month": 2.75, "raw": {}},
        ])
        rows = history(days=30)
        assert len(rows) == 1
        assert rows[0]["estimated_usd_month"] == pytest.approx(4.0)

    def test_empty_returns_empty_list(self, _init):
        assert history() == []


class TestPurge:
    def test_recent_kept(self, _init):
        save_snapshot([{"service_name": "x", "usd_month": 1.0, "raw": {}}])
        purge_old_snapshots(days=90)
        assert latest_snapshot()["services"]


# ── Limits & spend ─────────────────────────────────────────────────────


@pytest.fixture
def _init_full(temp_db):
    """Initialize both ai_runtime_config and infra_cost_snapshot tables."""
    init_ai_tables()
    init_infra_cost_table()
    reset_spend_cache()
    yield
    reset_spend_cache()


def _aggregate_row(usd_month: float) -> dict:
    """Build a Railway "project total" row mirroring _normalize_services."""
    return {
        "service_name": "Proyecto",
        "service_id": "proj-1",
        "usd_month": usd_month,
        "raw": {"_aggregate": True, "breakdown": {}},
    }


class TestInfraLimits:
    def test_default_is_empty(self, _init_full):
        assert get_infra_limits() == {"daily_max": None, "monthly_max": None}

    def test_set_and_get(self, _init_full):
        assert set_infra_limits(daily_max=1.5, monthly_max=30.0) is True
        lim = get_infra_limits()
        assert lim["daily_max"] == pytest.approx(1.5)
        assert lim["monthly_max"] == pytest.approx(30.0)

    def test_clear_with_none(self, _init_full):
        set_infra_limits(daily_max=1.0, monthly_max=10.0)
        set_infra_limits(daily_max=None, monthly_max=None)
        lim = get_infra_limits()
        assert lim["daily_max"] is None
        assert lim["monthly_max"] is None

    def test_rejects_negative(self, _init_full):
        assert set_infra_limits(daily_max=-1.0, monthly_max=10.0) is False
        # On rejection no fields should have been written.
        lim = get_infra_limits()
        assert lim == {"daily_max": None, "monthly_max": None}

    def test_rejects_bool(self, _init_full):
        assert set_infra_limits(daily_max=True, monthly_max=10.0) is False  # type: ignore[arg-type]
        assert set_infra_limits(daily_max=1.0, monthly_max=False) is False  # type: ignore[arg-type]

    def test_rejects_string(self, _init_full):
        assert set_infra_limits(daily_max="1.0", monthly_max=10.0) is False  # type: ignore[arg-type]


class TestCurrentSpend:
    def test_no_data_returns_none(self, _init_full):
        spend = get_current_spend()
        assert spend["today_usd"] is None
        assert spend["month_usd"] is None
        assert spend["fetched_at"] is None

    def test_single_snapshot_today_no_baseline(self, _init_full):
        save_snapshot([_aggregate_row(15.0)])
        reset_spend_cache()
        spend = get_current_spend()
        # With only one snapshot we can't compute today's delta yet.
        assert spend["month_usd"] == pytest.approx(15.0)
        assert spend["today_usd"] is None

    def test_today_delta_computed_from_two_snapshots_same_day(self, _init_full):
        save_snapshot([_aggregate_row(10.0)])
        time.sleep(1.1)
        save_snapshot([_aggregate_row(12.5)])
        reset_spend_cache()
        spend = get_current_spend()
        # Latest = 12.5, earliest of today = 10.0 → today_usd = 2.5
        assert spend["month_usd"] == pytest.approx(12.5)
        assert spend["today_usd"] == pytest.approx(2.5)

    def test_today_delta_clamped_to_zero(self, _init_full):
        """If Railway reports a lower number after rounding, don't go negative."""
        save_snapshot([_aggregate_row(10.0)])
        time.sleep(1.1)
        save_snapshot([_aggregate_row(9.5)])
        reset_spend_cache()
        spend = get_current_spend()
        assert spend["today_usd"] == pytest.approx(0.0)

    def test_today_uses_only_aggregate_rows(self, _init_full):
        """Per-service rows (no _aggregate flag) must NOT be picked up."""
        save_snapshot([
            {"service_name": "web", "usd_month": 5.0, "raw": {}},
            _aggregate_row(8.0),
        ])
        reset_spend_cache()
        spend = get_current_spend()
        # month_usd uses the aggregate row, not the per-service value.
        assert spend["month_usd"] == pytest.approx(8.0)


class TestBlockedKeys:
    def test_no_limits_means_not_blocked(self, _init_full):
        save_snapshot([_aggregate_row(100.0)])
        time.sleep(1.1)
        save_snapshot([_aggregate_row(105.0)])
        reset_spend_cache()
        assert get_blocked_keys() == []

    def test_monthly_limit_exceeded(self, _init_full):
        set_infra_limits(daily_max=None, monthly_max=20.0)
        save_snapshot([_aggregate_row(25.0)])
        reset_spend_cache()
        assert "monthly" in get_blocked_keys()

    def test_daily_limit_exceeded(self, _init_full):
        set_infra_limits(daily_max=1.0, monthly_max=None)
        save_snapshot([_aggregate_row(10.0)])
        time.sleep(1.1)
        save_snapshot([_aggregate_row(11.5)])
        reset_spend_cache()
        # today_usd = 1.5 ≥ daily_max = 1.0
        assert "daily" in get_blocked_keys()

    def test_below_limits_not_blocked(self, _init_full):
        set_infra_limits(daily_max=10.0, monthly_max=100.0)
        save_snapshot([_aggregate_row(20.0)])
        time.sleep(1.1)
        save_snapshot([_aggregate_row(21.0)])
        reset_spend_cache()
        assert get_blocked_keys() == []

    def test_no_baseline_means_daily_not_blocked(self, _init_full):
        """Without 2+ snapshots in the day we can't compute today's delta;
        the guard must stay permissive (returns no blocks)."""
        set_infra_limits(daily_max=0.01, monthly_max=None)
        save_snapshot([_aggregate_row(50.0)])
        reset_spend_cache()
        assert "daily" not in get_blocked_keys()
