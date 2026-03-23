from datetime import datetime, timezone

from app.models import Article, ArticleGroup, FeedStatus


class TestArticle:
    def test_create_with_defaults(self):
        art = Article(source="Test", title="Título de prueba")
        assert art.source == "Test"
        assert art.title == "Título de prueba"
        assert art.id == ""
        assert art.category == "portada"
        assert art.summary == ""
        assert art.link == ""
        assert art.image == ""

    def test_create_with_all_fields(self):
        pub = datetime(2025, 6, 15, tzinfo=timezone.utc)
        art = Article(
            id="abc123",
            source="Clarín",
            source_color="#1a73e8",
            title="Test",
            summary="Summary text",
            link="https://example.com",
            image="https://img.com/a.jpg",
            category="economia",
            published=pub,
        )
        assert art.id == "abc123"
        assert art.published == pub
        assert art.category == "economia"
        assert art.image == "https://img.com/a.jpg"

    def test_short_summary_no_truncation(self):
        art = Article(source="X", title="T", summary="Texto corto.")
        assert art.short_summary() == "Texto corto."

    def test_short_summary_truncation(self):
        long_text = "palabra " * 60
        art = Article(source="X", title="T", summary=long_text.strip())
        result = art.short_summary(50)
        assert len(result) <= 50
        assert result.endswith("…")

    def test_short_summary_uses_title_when_no_summary(self):
        art = Article(source="X", title="El título como resumen")
        assert art.short_summary() == "El título como resumen"


class TestArticleGroup:
    def test_source_count_calculated(self):
        articles = [
            Article(source="A", title="T1"),
            Article(source="B", title="T2"),
            Article(source="C", title="T3"),
        ]
        group = ArticleGroup(
            group_id="g1",
            representative_title="T1",
            articles=articles,
        )
        assert group.source_count == 3

    def test_source_count_deduplicates(self):
        articles = [
            Article(source="A", title="T1"),
            Article(source="A", title="T2"),
            Article(source="B", title="T3"),
        ]
        group = ArticleGroup(
            group_id="g1",
            representative_title="T1",
            articles=articles,
        )
        assert group.source_count == 2

    def test_empty_articles(self):
        group = ArticleGroup(group_id="g1", representative_title="T")
        assert group.source_count == 0
        assert group.articles == []

    def test_defaults(self):
        group = ArticleGroup(group_id="g1", representative_title="T")
        assert group.representative_image == ""
        assert group.category == "portada"
        assert group.published is None


class TestFeedStatus:
    def test_create_ok(self):
        fs = FeedStatus(source="Test", feed_url="https://x.com/rss", status="ok")
        assert fs.status == "ok"
        assert fs.article_count == 0
        assert fs.error_message == ""

    def test_create_error(self):
        fs = FeedStatus(
            source="Test",
            feed_url="https://x.com/rss",
            status="error",
            error_message="Connection timeout",
        )
        assert fs.status == "error"
        assert fs.error_message == "Connection timeout"

    def test_with_fetched_at(self):
        now = datetime.now(timezone.utc)
        fs = FeedStatus(
            source="Test",
            feed_url="https://x.com/rss",
            status="ok",
            fetched_at=now,
        )
        assert fs.fetched_at == now
