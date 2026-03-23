from pathlib import Path

import feedparser
import httpx
import respx

from app.feed_reader import (
    _clean_html,
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
