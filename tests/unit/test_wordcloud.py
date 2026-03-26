"""Tests for app.wordcloud — word frequency extraction from article titles."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.wordcloud import build_wordcloud


def test_basic_frequency(make_article):
    now = datetime.now(timezone.utc)
    articles = [
        make_article(title="Milei anunció un ajuste fiscal", published=now),
        make_article(title="Milei habló sobre el ajuste del presupuesto", published=now),
        make_article(title="El presidente anunció nuevas medidas económicas", published=now),
    ]
    result = build_wordcloud(articles)
    words = {w: c for w, c in result}
    assert "milei" in words
    assert words["milei"] == 2
    assert "ajuste" in words
    assert words["ajuste"] == 2


def test_preserves_accents_and_enie(make_article):
    now = datetime.now(timezone.utc)
    articles = [
        make_article(title="Córdoba celebró 50 años de historia", published=now),
        make_article(title="Años de inflación afectan la economía", published=now),
    ]
    result = build_wordcloud(articles)
    words = {w for w, _ in result}
    assert "años" in words, f"Expected 'años' but got {words}"
    assert "córdoba" in words, f"Expected 'córdoba' but got {words}"
    assert "economía" in words, f"Expected 'economía' but got {words}"
    assert "anos" not in words
    assert "cordoba" not in words


def test_returns_sorted_by_frequency(make_article):
    now = datetime.now(timezone.utc)
    articles = [
        make_article(title="Dólar blue sube fuerte", published=now),
        make_article(title="Dólar oficial sin cambios", published=now),
        make_article(title="Dólar blue y oficial en alza", published=now),
        make_article(title="Inflación preocupa al mercado", published=now),
    ]
    result = build_wordcloud(articles)
    assert len(result) > 0
    counts = [c for _, c in result]
    assert counts == sorted(counts, reverse=True)


def test_excludes_old_articles(make_article):
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=30)
    articles = [
        make_article(title="Noticia vieja irrelevante economía", published=old),
        make_article(title="Noticia fresca importante economía", published=now),
    ]
    result = build_wordcloud(articles, hours=24)
    words = {w: c for w, c in result}
    assert "vieja" not in words
    assert "fresca" in words


def test_empty_list():
    result = build_wordcloud([])
    assert result == []


def test_stopwords_excluded(make_article):
    now = datetime.now(timezone.utc)
    articles = [
        make_article(title="El gobierno anunció que se trabaja en una reforma", published=now),
    ]
    result = build_wordcloud(articles)
    words = {w for w, _ in result}
    for stopword in ("el", "que", "en", "una", "gobierno"):
        assert stopword not in words


def test_articles_without_published_are_included():
    from app.models import Article
    art = Article(source="Test", title="Tema especial sin fecha publicación", published=None)
    result = build_wordcloud([art])
    words = {w for w, _ in result}
    assert "tema" in words
    assert "especial" in words


def test_max_words_limit(make_article):
    now = datetime.now(timezone.utc)
    articles = [
        make_article(title=f"palabra{i} única distinta especial", published=now)
        for i in range(100)
    ]
    result = build_wordcloud(articles)
    assert len(result) <= 80
