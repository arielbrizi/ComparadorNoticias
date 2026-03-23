import pytest

from app.db import get_conn, query
from app.news_store import (
    init_news_tables,
    load_groups_from_db,
    purge_old_news,
    save_articles_and_groups,
)


class TestNewsStore:
    @pytest.fixture(autouse=True)
    def _setup_tables(self, temp_db):
        init_news_tables()

    def test_init_creates_articles_table(self):
        with get_conn() as conn:
            rows = query(
                conn,
                "SELECT name FROM sqlite_master WHERE type='table' AND name='articles'",
            ).fetchall()
            assert len(rows) == 1

    def test_init_creates_article_groups_table(self):
        with get_conn() as conn:
            rows = query(
                conn,
                "SELECT name FROM sqlite_master WHERE type='table' AND name='article_groups'",
            ).fetchall()
            assert len(rows) == 1

    def test_save_articles_and_groups(self, sample_articles, sample_groups):
        art_count, grp_count = save_articles_and_groups(sample_articles, sample_groups)
        assert art_count >= 0
        assert grp_count >= 0

        with get_conn() as conn:
            rows = query(conn, "SELECT COUNT(*) as cnt FROM articles").fetchone()
            assert rows["cnt"] == len(sample_articles)

    def test_load_groups_from_db(self, sample_articles, sample_groups):
        save_articles_and_groups(sample_articles, sample_groups)
        articles, groups = load_groups_from_db()
        assert len(articles) == len(sample_articles)
        assert len(groups) >= 1

    def test_load_with_date_filter(self, sample_articles, sample_groups):
        save_articles_and_groups(sample_articles, sample_groups)

        articles, groups = load_groups_from_db(desde="2025-06-15")
        assert len(articles) > 0

        articles_empty, _ = load_groups_from_db(desde="2099-01-01")
        assert len(articles_empty) == 0

    def test_purge_old_news(self, sample_articles, sample_groups):
        save_articles_and_groups(sample_articles, sample_groups)
        deleted = purge_old_news(days=0)
        assert deleted > 0

        articles, groups = load_groups_from_db()
        assert len(articles) == 0

    def test_upsert_idempotent(self, sample_articles, sample_groups):
        save_articles_and_groups(sample_articles, sample_groups)
        save_articles_and_groups(sample_articles, sample_groups)

        with get_conn() as conn:
            rows = query(conn, "SELECT COUNT(*) as cnt FROM articles").fetchone()
            assert rows["cnt"] == len(sample_articles)
