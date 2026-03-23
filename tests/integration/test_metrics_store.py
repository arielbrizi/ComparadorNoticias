import pytest

from app.db import get_conn, query
from app.metrics_store import init_db, query_metrics, save_group_metrics


class TestMetricsStore:
    @pytest.fixture(autouse=True)
    def _setup_tables(self, temp_db):
        init_db()

    def test_init_creates_metric_events_table(self):
        with get_conn() as conn:
            rows = query(
                conn,
                "SELECT name FROM sqlite_master WHERE type='table' AND name='metric_events'",
            ).fetchall()
            assert len(rows) == 1

    def test_save_group_metrics(self, sample_groups):
        inserted = save_group_metrics(sample_groups)
        assert inserted > 0

    def test_query_metrics_structure(self, sample_groups):
        save_group_metrics(sample_groups)
        metrics = query_metrics()

        assert "first_publisher_ranking" in metrics
        assert "avg_reaction_time" in metrics
        assert "exclusivity_index" in metrics
        assert "total_groups" in metrics
        assert "multi_source_groups" in metrics
        assert "date_range" in metrics
        assert metrics["total_groups"] > 0

    def test_query_metrics_with_date_filter(self, sample_groups):
        save_group_metrics(sample_groups)

        metrics = query_metrics(desde="2025-06-15")
        assert metrics["total_groups"] > 0

        metrics_empty = query_metrics(desde="2099-01-01")
        assert metrics_empty["total_groups"] == 0

    def test_idempotent_save(self, sample_groups):
        first = save_group_metrics(sample_groups)
        second = save_group_metrics(sample_groups)
        assert first > 0
        assert second == 0

    def test_empty_groups(self):
        inserted = save_group_metrics([])
        assert inserted == 0
