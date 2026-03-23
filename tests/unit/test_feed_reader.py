from pathlib import Path

import feedparser
import httpx
import respx

from app.feed_reader import (
    _clean_html,
    _extract_image,
    _fetch_og_image,
    _fill_missing_images,
    _make_id,
    _normalize_title,
    _parse_date,
    _parse_feed_entries,
    fetch_single_feed,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


class TestNormalizeTitle:
    def test_strips_accents(self):
        assert _normalize_title("Inflación económica") == "inflacion economica"

    def test_lowercase(self):
        assert _normalize_title("HELLO WORLD") == "hello world"

    def test_strips_punctuation(self):
        result = _normalize_title("¿Cómo está?")
        assert result == "como esta"

    def test_collapses_whitespace(self):
        assert _normalize_title("  muchos   espacios  ") == "muchos espacios"


class TestParseDate:
    def test_published_field(self):
        entry = {"published": "Mon, 15 Jun 2025 12:00:00 +0000"}
        dt = _parse_date(entry)
        assert dt is not None
        assert dt.year == 2025
        assert dt.month == 6
        assert dt.tzinfo is not None

    def test_updated_field(self):
        entry = {"updated": "2025-06-15T12:00:00Z"}
        dt = _parse_date(entry)
        assert dt is not None

    def test_no_date_fields(self):
        assert _parse_date({}) is None

    def test_invalid_date_returns_none(self):
        entry = {"published": "not-a-valid-date-at-all"}
        assert _parse_date(entry) is None


class TestCleanHtml:
    def test_strips_tags(self):
        assert "Hello world" in _clean_html("<p>Hello <b>world</b></p>")

    def test_empty_input(self):
        assert _clean_html("") == ""

    def test_preserves_plain_text(self):
        assert _clean_html("plain text") == "plain text"

    def test_strips_nested_html(self):
        html = "<div><p>A <a href='#'>link</a></p></div>"
        result = _clean_html(html)
        assert "link" in result
        assert "<" not in result


class TestMakeId:
    def test_deterministic(self):
        id1 = _make_id("Clarín", "https://example.com/article")
        id2 = _make_id("Clarín", "https://example.com/article")
        assert id1 == id2

    def test_different_sources_produce_different_ids(self):
        id1 = _make_id("Clarín", "https://example.com/article")
        id2 = _make_id("Infobae", "https://example.com/article")
        assert id1 != id2

    def test_length_is_12(self):
        result = _make_id("Source", "https://example.com")
        assert len(result) == 12


class TestParseFeedEntries:
    def test_parses_sample_feed(self):
        xml = (FIXTURES_DIR / "sample_feed.xml").read_text(encoding="utf-8")
        feed_data = feedparser.parse(xml)
        articles = _parse_feed_entries(feed_data, "TestSource", "#aabbcc", "portada")
        assert len(articles) == 2
        assert articles[0].source == "TestSource"
        assert articles[0].source_color == "#aabbcc"
        assert articles[0].category == "portada"

    def test_skips_entries_with_empty_title(self):
        xml = (FIXTURES_DIR / "sample_feed.xml").read_text(encoding="utf-8")
        feed_data = feedparser.parse(xml)
        articles = _parse_feed_entries(feed_data, "X", "#000", "portada")
        titles = [a.title for a in articles]
        assert all(t.strip() for t in titles)

    def test_article_fields_populated(self):
        xml = (FIXTURES_DIR / "sample_feed.xml").read_text(encoding="utf-8")
        feed_data = feedparser.parse(xml)
        articles = _parse_feed_entries(feed_data, "Src", "#000", "economia")
        first = articles[0]
        assert first.id
        assert first.title
        assert first.link
        assert first.category == "economia"


class TestFetchSingleFeed:
    async def test_successful_fetch(self):
        xml = (FIXTURES_DIR / "sample_feed.xml").read_text(encoding="utf-8")
        async with respx.mock:
            respx.get("https://test.com/feed").mock(
                return_value=httpx.Response(200, text=xml)
            )
            async with httpx.AsyncClient() as client:
                articles, status = await fetch_single_feed(
                    client, "TestSource", "#000", "portada", "https://test.com/feed"
                )
        assert status.status == "ok"
        assert status.article_count == 2
        assert len(articles) == 2

    async def test_http_error_returns_error_status(self):
        async with respx.mock:
            respx.get("https://test.com/feed").mock(
                return_value=httpx.Response(500, text="Server Error")
            )
            async with httpx.AsyncClient() as client:
                articles, status = await fetch_single_feed(
                    client, "TestSource", "#000", "portada", "https://test.com/feed"
                )
        assert status.status == "error"
        assert len(articles) == 0
        assert status.error_message

    async def test_invalid_xml_returns_error_status(self):
        async with respx.mock:
            respx.get("https://test.com/feed").mock(
                return_value=httpx.Response(200, text="this is not xml at all")
            )
            async with httpx.AsyncClient() as client:
                articles, status = await fetch_single_feed(
                    client, "TestSource", "#000", "portada", "https://test.com/feed"
                )
        assert len(articles) == 0


class TestExtractImage:
    def test_media_content(self):
        entry = {"media_content": [{"url": "https://img.com/photo.jpg"}]}
        assert _extract_image(entry) == "https://img.com/photo.jpg"

    def test_media_thumbnail(self):
        entry = {"media_thumbnail": [{"url": "https://img.com/thumb.jpg"}]}
        assert _extract_image(entry) == "https://img.com/thumb.jpg"

    def test_enclosure(self):
        entry = {
            "enclosures": [
                {"type": "image/jpeg", "href": "https://img.com/enc.jpg"}
            ]
        }
        assert _extract_image(entry) == "https://img.com/enc.jpg"

    def test_img_in_content(self):
        entry = {
            "content": [
                {"value": '<p>Text <img src="https://img.com/inline.jpg"/> more</p>'}
            ]
        }
        assert _extract_image(entry) == "https://img.com/inline.jpg"

    def test_img_in_summary_string(self):
        entry = {"summary": '<p><img src="https://img.com/sum.jpg"/></p>'}
        assert _extract_image(entry) == "https://img.com/sum.jpg"

    def test_no_image_returns_empty(self):
        entry = {"title": "No image here", "summary": "Plain text summary"}
        assert _extract_image(entry) == ""

    def test_priority_media_content_over_enclosure(self):
        entry = {
            "media_content": [{"url": "https://img.com/media.jpg"}],
            "enclosures": [
                {"type": "image/jpeg", "href": "https://img.com/enc.jpg"}
            ],
        }
        assert _extract_image(entry) == "https://img.com/media.jpg"


class TestFetchOgImage:
    async def test_extracts_og_image(self):
        html = """
        <html><head>
            <meta property="og:image" content="https://img.com/og.jpg"/>
        </head><body></body></html>
        """
        async with respx.mock:
            respx.get("https://example.com/article").mock(
                return_value=httpx.Response(200, text=html)
            )
            async with httpx.AsyncClient() as client:
                result = await _fetch_og_image(client, "https://example.com/article")
        assert result == "https://img.com/og.jpg"

    async def test_falls_back_to_twitter_image(self):
        html = """
        <html><head>
            <meta name="twitter:image" content="https://img.com/tw.jpg"/>
        </head><body></body></html>
        """
        async with respx.mock:
            respx.get("https://example.com/article").mock(
                return_value=httpx.Response(200, text=html)
            )
            async with httpx.AsyncClient() as client:
                result = await _fetch_og_image(client, "https://example.com/article")
        assert result == "https://img.com/tw.jpg"

    async def test_returns_empty_on_http_error(self):
        async with respx.mock:
            respx.get("https://example.com/article").mock(
                return_value=httpx.Response(404)
            )
            async with httpx.AsyncClient() as client:
                result = await _fetch_og_image(client, "https://example.com/article")
        assert result == ""

    async def test_returns_empty_when_no_meta_tags(self):
        html = "<html><head><title>No image</title></head><body></body></html>"
        async with respx.mock:
            respx.get("https://example.com/article").mock(
                return_value=httpx.Response(200, text=html)
            )
            async with httpx.AsyncClient() as client:
                result = await _fetch_og_image(client, "https://example.com/article")
        assert result == ""


class TestFillMissingImages:
    async def test_fills_articles_without_images(self):
        from app.models import Article

        html = '<html><head><meta property="og:image" content="https://img.com/og.jpg"/></head></html>'
        art = Article(
            source="Test", title="T", link="https://example.com/a1", image=""
        )
        async with respx.mock:
            respx.get("https://example.com/a1").mock(
                return_value=httpx.Response(200, text=html)
            )
            async with httpx.AsyncClient() as client:
                await _fill_missing_images(client, [art])
        assert art.image == "https://img.com/og.jpg"

    async def test_skips_articles_that_already_have_images(self):
        from app.models import Article

        art = Article(
            source="Test",
            title="T",
            link="https://example.com/a1",
            image="https://existing.com/img.jpg",
        )
        async with respx.mock:
            async with httpx.AsyncClient() as client:
                await _fill_missing_images(client, [art])
        assert art.image == "https://existing.com/img.jpg"

    async def test_skips_articles_without_link(self):
        from app.models import Article

        art = Article(source="Test", title="T", link="", image="")
        async with respx.mock:
            async with httpx.AsyncClient() as client:
                await _fill_missing_images(client, [art])
        assert art.image == ""
