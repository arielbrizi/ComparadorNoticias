"""
Lector de feeds RSS con fallback a scraping básico.
Obtiene artículos de cada fuente configurada.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone

import feedparser
import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from app.config import FETCH_TIMEOUT, MAX_ARTICLES_PER_FEED, SOURCES, USER_AGENT
from app.models import Article, FeedStatus

logger = logging.getLogger(__name__)


def _parse_date(entry) -> datetime | None:
    for field in ("published", "updated", "created"):
        raw = entry.get(field)
        if raw:
            try:
                return dateparser.parse(raw)
            except (ValueError, TypeError):
                continue
    return None


def _extract_image(entry) -> str:
    # media:content or media:thumbnail
    for media in entry.get("media_content", []):
        url = media.get("url", "")
        if url:
            return url
    media_thumb = entry.get("media_thumbnail", [])
    if media_thumb:
        return media_thumb[0].get("url", "")

    # enclosure
    for enc in entry.get("enclosures", []):
        if enc.get("type", "").startswith("image"):
            return enc.get("href", enc.get("url", ""))

    # first <img> in content/summary HTML
    for field in ("content", "summary"):
        html = ""
        val = entry.get(field)
        if isinstance(val, list):
            html = val[0].get("value", "") if val else ""
        elif isinstance(val, str):
            html = val
        if html:
            soup = BeautifulSoup(html, "lxml")
            img = soup.find("img")
            if img and img.get("src"):
                return img["src"]

    return ""


def _clean_html(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    return soup.get_text(separator=" ", strip=True)


def _make_id(source: str, link: str) -> str:
    return hashlib.md5(f"{source}:{link}".encode()).hexdigest()[:12]


def _parse_feed_entries(
    feed_data: feedparser.FeedParserDict,
    source_name: str,
    source_color: str,
    category: str,
) -> list[Article]:
    articles: list[Article] = []
    for entry in feed_data.entries[:MAX_ARTICLES_PER_FEED]:
        link = entry.get("link", "")
        title = entry.get("title", "").strip()
        if not title:
            continue

        summary_html = ""
        if entry.get("summary"):
            summary_html = entry["summary"]
        elif entry.get("content"):
            summary_html = entry["content"][0].get("value", "")
        elif entry.get("description"):
            summary_html = entry["description"]

        articles.append(
            Article(
                id=_make_id(source_name, link),
                source=source_name,
                source_color=source_color,
                title=title,
                summary=_clean_html(summary_html),
                link=link,
                image=_extract_image(entry),
                category=category,
                published=_parse_date(entry),
            )
        )
    return articles


async def fetch_single_feed(
    client: httpx.AsyncClient,
    source_name: str,
    source_color: str,
    category: str,
    feed_url: str,
) -> tuple[list[Article], FeedStatus]:
    status = FeedStatus(
        source=source_name,
        feed_url=feed_url,
        status="ok",
        fetched_at=datetime.now(timezone.utc),
    )
    try:
        resp = await client.get(feed_url)
        resp.raise_for_status()
        feed_data = feedparser.parse(resp.text)
        if feed_data.bozo and not feed_data.entries:
            raise ValueError(f"Feed inválido: {feed_data.bozo_exception}")
        articles = _parse_feed_entries(feed_data, source_name, source_color, category)
        status.article_count = len(articles)
        return articles, status
    except Exception as exc:
        logger.warning("Error fetching %s [%s]: %s", source_name, feed_url, exc)
        status.status = "error"
        status.error_message = str(exc)
        return [], status


async def fetch_all_feeds(
    categories: list[str] | None = None,
) -> tuple[list[Article], list[FeedStatus]]:
    """Fetch articles from all configured sources, optionally filtered by category."""
    all_articles: list[Article] = []
    all_statuses: list[FeedStatus] = []

    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        tasks = []
        for source_name, source_cfg in SOURCES.items():
            color = source_cfg.get("color", "#888")
            for cat, url in source_cfg["feeds"].items():
                if categories and cat not in categories:
                    continue
                tasks.append(
                    fetch_single_feed(client, source_name, color, cat, url)
                )

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error("Task exception: %s", result)
                continue
            articles, status = result
            all_articles.extend(articles)
            all_statuses.append(status)

    # De-duplicate by link
    seen_links: set[str] = set()
    unique: list[Article] = []
    for art in all_articles:
        if art.link and art.link not in seen_links:
            seen_links.add(art.link)
            unique.append(art)
        elif not art.link:
            unique.append(art)

    unique.sort(key=lambda a: a.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    return unique, all_statuses
