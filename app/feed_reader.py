"""
Lector de feeds RSS con fallback a scraping básico.
Obtiene artículos de cada fuente configurada.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timezone

import feedparser
import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from app.config import FETCH_TIMEOUT, MAX_ARTICLES_PER_FEED, SOURCES, USER_AGENT
from app.models import Article, FeedStatus

logger = logging.getLogger(__name__)

OG_IMAGE_TIMEOUT = 8
OG_IMAGE_CONCURRENCY = 10


def _normalize_title(title: str) -> str:
    """Lowercase, strip accents and punctuation for dedup comparison."""
    text = title.lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = re.sub(r"[\u0300-\u036f]", "", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def _parse_date(entry) -> datetime | None:
    for field in ("published", "updated", "created"):
        raw = entry.get(field)
        if raw:
            try:
                dt = dateparser.parse(raw)
                if dt is None:
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
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
    exclude_link_re: str = "",
) -> list[Article]:
    articles: list[Article] = []
    _exclude = re.compile(exclude_link_re, re.IGNORECASE) if exclude_link_re else None
    for entry in feed_data.entries[:MAX_ARTICLES_PER_FEED]:
        link = entry.get("link", "")
        if _exclude and link and _exclude.search(link):
            continue
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
    exclude_link_re: str = "",
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
        articles = _parse_feed_entries(
            feed_data, source_name, source_color, category,
            exclude_link_re=exclude_link_re,
        )
        status.article_count = len(articles)
        return articles, status
    except Exception as exc:
        logger.warning("Error fetching %s [%s]: %s", source_name, feed_url, exc)
        status.status = "error"
        status.error_message = str(exc)
        return [], status


async def _fetch_og_image(client: httpx.AsyncClient, url: str) -> str:
    """Fetch the og:image meta tag from an article page."""
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text[:50_000]
        soup = BeautifulSoup(html, "lxml")
        tag = soup.find("meta", property="og:image") or soup.find(
            "meta", attrs={"name": "og:image"}
        )
        if tag:
            content = tag.get("content", "")
            if content:
                return content
        tag_tw = soup.find("meta", attrs={"name": "twitter:image"})
        if tag_tw:
            content = tag_tw.get("content", "")
            if content:
                return content
    except Exception:
        pass
    return ""


async def _fill_missing_images(
    client: httpx.AsyncClient,
    articles: list[Article],
) -> None:
    """Best-effort: scrape og:image for articles that have no image from RSS."""
    missing = [a for a in articles if not a.image and a.link]
    if not missing:
        return

    sem = asyncio.Semaphore(OG_IMAGE_CONCURRENCY)

    async def _resolve(art: Article) -> None:
        async with sem:
            img = await _fetch_og_image(client, art.link)
            if img:
                art.image = img

    await asyncio.gather(*(_resolve(a) for a in missing))


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
            exclude_re = source_cfg.get("exclude_link_re", "")
            for cat, url in source_cfg["feeds"].items():
                if categories and cat not in categories:
                    continue
                tasks.append(
                    fetch_single_feed(
                        client, source_name, color, cat, url,
                        exclude_link_re=exclude_re,
                    )
                )

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error("Task exception: %s", result)
                continue
            articles, status = result
            all_articles.extend(articles)
            all_statuses.append(status)

    # De-duplicate by link AND by (source + normalized title)
    seen_links: set[str] = set()
    seen_source_title: set[str] = set()
    unique: list[Article] = []
    for art in all_articles:
        if art.link and art.link in seen_links:
            continue
        st_key = f"{art.source}::{_normalize_title(art.title)}"
        if st_key in seen_source_title:
            continue
        if art.link:
            seen_links.add(art.link)
        seen_source_title.add(st_key)
        unique.append(art)

    # Fallback: scrape og:image for articles still missing images
    async with httpx.AsyncClient(
        timeout=OG_IMAGE_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        await _fill_missing_images(client, unique)

    unique.sort(key=lambda a: a.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    return unique, all_statuses
