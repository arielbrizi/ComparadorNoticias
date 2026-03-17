from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field


class Article(BaseModel):
    id: str = ""
    source: str
    source_color: str = ""
    title: str
    summary: str = ""
    link: str = ""
    image: str = ""
    category: str = "portada"
    published: datetime | None = None

    def short_summary(self, max_len: int = 280) -> str:
        text = self.summary or self.title
        if len(text) <= max_len:
            return text
        return text[: max_len - 1].rsplit(" ", 1)[0] + "…"


class ArticleGroup(BaseModel):
    """A group of articles from different sources covering the same topic."""
    group_id: str
    representative_title: str
    representative_image: str = ""
    category: str = "portada"
    published: datetime | None = None
    articles: list[Article] = Field(default_factory=list)
    source_count: int = 0

    def model_post_init(self, _context):
        self.source_count = len({a.source for a in self.articles})


class FeedStatus(BaseModel):
    source: str
    feed_url: str
    status: str  # "ok" | "error"
    article_count: int = 0
    error_message: str = ""
    fetched_at: datetime | None = None
