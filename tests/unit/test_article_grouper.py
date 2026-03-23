from datetime import datetime, timedelta, timezone

from app.article_grouper import (
    _is_daily_quote,
    _normalize,
    _time_compatible,
    _titles_similar,
    group_articles,
)
from app.config import SIMILARITY_THRESHOLD
from app.models import Article


class TestNormalize:
    def test_strips_accents(self):
        assert "inflacion" in _normalize("Inflación")

    def test_removes_stopwords(self):
        result = _normalize("el de la en con por")
        assert result == ""

    def test_removes_short_tokens(self):
        result = _normalize("ya no es un ok")
        assert result == ""

    def test_keeps_significant_words(self):
        result = _normalize("inflación económica récord")
        assert "inflacion" in result
        assert "economica" in result
        assert "record" in result

    def test_strips_punctuation(self):
        result = _normalize("¿Crisis? ¡Sí!")
        assert "crisis" in result


class TestTitlesSimilar:
    def test_identical_titles(self):
        title = "La inflación de marzo fue del 3,5% según el INDEC"
        score = _titles_similar(title, title)
        assert score > 80

    def test_similar_titles_above_threshold(self):
        score = _titles_similar(
            "La inflación de marzo fue del 3,5% según el INDEC",
            "Inflación de marzo: el INDEC reportó una suba del 3,5%",
        )
        assert score >= SIMILARITY_THRESHOLD

    def test_different_titles_below_threshold(self):
        score = _titles_similar(
            "La inflación de marzo fue del 3,5%",
            "River venció 3-0 a Boca en el Superclásico",
        )
        assert score < SIMILARITY_THRESHOLD

    def test_empty_title_returns_zero(self):
        assert _titles_similar("", "Algo") == 0.0
        assert _titles_similar("Algo", "") == 0.0

    def test_both_empty_returns_zero(self):
        assert _titles_similar("", "") == 0.0


class TestIsDailyQuote:
    def test_detects_dollar(self):
        art = Article(source="X", title="Dólar blue: a cuánto cotiza hoy")
        assert _is_daily_quote(art)

    def test_detects_crypto(self):
        art = Article(source="X", title="Bitcoin superó los USD 100.000")
        assert _is_daily_quote(art)

    def test_detects_riesgo_pais(self):
        art = Article(source="X", title="El riesgo país bajó a 800 puntos")
        assert _is_daily_quote(art)

    def test_non_quote_article(self):
        art = Article(source="X", title="Milei viajó a Estados Unidos")
        assert not _is_daily_quote(art)


class TestTimeCompatible:
    def test_close_times_compatible(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        a = Article(source="A", title="T", published=now)
        b = Article(source="B", title="T", published=now + timedelta(hours=2))
        assert _time_compatible(a, b)

    def test_far_times_incompatible(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        a = Article(source="A", title="T", published=now)
        b = Article(source="B", title="T", published=now + timedelta(hours=72))
        assert not _time_compatible(a, b)

    def test_none_dates_are_compatible(self):
        a = Article(source="A", title="T")
        b = Article(source="B", title="T")
        assert _time_compatible(a, b)

    def test_daily_quote_short_window(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        a = Article(source="A", title="Dólar blue hoy", published=now)
        b = Article(source="B", title="Cotización dólar", published=now + timedelta(hours=20))
        assert not _time_compatible(a, b)

    def test_daily_quote_within_window(self):
        now = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)
        a = Article(source="A", title="Dólar blue hoy", published=now)
        b = Article(source="B", title="Cotización dólar", published=now + timedelta(hours=5))
        assert _time_compatible(a, b)


class TestGroupArticles:
    def test_empty_input(self):
        assert group_articles([]) == []

    def test_single_article(self):
        art = Article(
            source="A", title="Test noticia",
            published=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        groups = group_articles([art])
        assert len(groups) == 1
        assert len(groups[0].articles) == 1

    def test_similar_articles_grouped(self, sample_articles):
        groups = group_articles(sample_articles)
        inflation_group = None
        for g in groups:
            titles_lower = [a.title.lower() for a in g.articles]
            if any("inflación" in t or "inflacion" in t for t in titles_lower):
                inflation_group = g
                break
        assert inflation_group is not None
        assert len(inflation_group.articles) == 2
        sources = {a.source for a in inflation_group.articles}
        assert "Clarín" in sources
        assert "La Nación" in sources

    def test_different_articles_separate(self, sample_articles):
        groups = group_articles(sample_articles)
        assert len(groups) >= 3

    def test_same_source_not_grouped_together(self, make_article):
        articles = [
            make_article(source="Clarín", title="Inflación subió 3,5% según el INDEC publicó hoy"),
            make_article(source="Clarín", title="La inflación de marzo fue del 3,5% reportó INDEC oficialmente"),
        ]
        groups = group_articles(articles)
        assert len(groups) == 2

    def test_groups_sorted_by_source_count_desc(self, sample_articles):
        groups = group_articles(sample_articles)
        source_counts = [g.source_count for g in groups]
        assert source_counts == sorted(source_counts, reverse=True)

    def test_group_has_representative_title(self, sample_articles):
        groups = group_articles(sample_articles)
        for g in groups:
            assert g.representative_title
            assert g.group_id
