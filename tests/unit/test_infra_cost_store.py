"""Tests for app.infra_cost_store — Railway cost snapshots."""

from __future__ import annotations

import time

import pytest

from app.infra_cost_store import (
    history,
    init_infra_cost_table,
    latest_snapshot,
    purge_old_snapshots,
    save_snapshot,
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
    def test_groups_by_day(self, _init):
        save_snapshot([{"service_name": "a", "usd_month": 1.0, "raw": {}}])
        save_snapshot([{"service_name": "a", "usd_month": 1.5, "raw": {}}])
        rows = history(days=30)
        assert len(rows) == 1
        assert rows[0]["estimated_usd_month"] == pytest.approx(2.5)

    def test_empty_returns_empty_list(self, _init):
        assert history() == []


class TestPurge:
    def test_recent_kept(self, _init):
        save_snapshot([{"service_name": "x", "usd_month": 1.0, "raw": {}}])
        purge_old_snapshots(days=90)
        assert latest_snapshot()["services"]
