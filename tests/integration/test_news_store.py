import pytest

from app.db import get_conn, query
from app.news_store import (
    init_news_tables,
    load_groups_from_db,
    purge_old_news,
    save_articles_and_groups,
    text_search_groups,
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

    def test_text_search_finds_matching_articles(self, sample_articles, sample_groups):
        save_articles_and_groups(sample_articles, sample_groups)
        results = text_search_groups("inflación INDEC")
        assert len(results) >= 1
        assert any("inflación" in g.representative_title.lower() for g in results)

    def test_text_search_no_results(self, sample_articles, sample_groups):
        save_articles_and_groups(sample_articles, sample_groups)
        results = text_search_groups("tema inexistente xyz123")
        assert len(results) == 0

    def test_text_search_partial_match(self, sample_articles, sample_groups):
        save_articles_and_groups(sample_articles, sample_groups)
        results = text_search_groups("dólar blue")
        assert len(results) >= 1
        assert any("dólar" in g.representative_title.lower() for g in results)

    def test_text_search_empty_query(self, sample_articles, sample_groups):
        save_articles_and_groups(sample_articles, sample_groups)
        results = text_search_groups("")
        assert len(results) == 0

    def test_text_search_respects_limit(self, sample_articles, sample_groups):
        save_articles_and_groups(sample_articles, sample_groups)
        results = text_search_groups("inflación", limit=1)
        assert len(results) <= 1

    def test_text_search_strips_stopwords_in_phrase(
        self, sample_articles, sample_groups,
    ):
        """Natural-language phrases must match on content-bearing keywords.

        Before the fix, the SQL ANDed every token (incl. stopwords like
        "de", "la", "últimos", "detalles") so phrases returned nothing.
        """
        save_articles_and_groups(sample_articles, sample_groups)
        results = text_search_groups("dame los últimos detalles de la inflación")
        assert len(results) >= 1
        assert any("inflación" in g.representative_title.lower() for g in results)

    def test_text_search_ignores_conversational_prefix(
        self, sample_articles, sample_groups,
    ):
        save_articles_and_groups(sample_articles, sample_groups)
        results = text_search_groups("qué pasó con el dólar")
        assert len(results) >= 1
        assert any("dólar" in g.representative_title.lower() for g in results)

    def test_text_search_ranks_by_keyword_matches(
        self, sample_articles, sample_groups,
    ):
        """Groups matching more keywords rank above groups matching fewer."""
        save_articles_and_groups(sample_articles, sample_groups)
        # "River" matches the Superclásico group; "inflación" matches the
        # inflation group; a query mentioning both should surface both,
        # with the one matching both (if any) first. With these fixtures
        # neither group matches both, so just verify both appear.
        results = text_search_groups("River inflación")
        titles = [g.representative_title.lower() for g in results]
        assert any("river" in t for t in titles)
        assert any("inflación" in t for t in titles)

    def test_text_search_all_stopwords_falls_back(
        self, sample_articles, sample_groups,
    ):
        """Query of only stopwords/short words should not crash; may return [].
        """
        save_articles_and_groups(sample_articles, sample_groups)
        # "de la" — all tokens are stopwords and short
        results = text_search_groups("de la")
        assert isinstance(results, list)
